#!/usr/bin/env python3
"""ledger.py — one JSON line per work package: the evidence the coach routes on.

  ledger.py add    wp=<label> class=<c> impl_lane=<lane> [k=v ...]   append a WP row
  ledger.py add    wp=<label> --from-session <id> [k=v ...]          fill fields from state.db
  ledger.py fix    wp=<label> [repo=<r>] [commit=<sha>] k=v ...      update the LAST matching row
  ledger.py fix    wp=<label> --from-session <id> [k=v ...]          ... from state.db too
  ledger.py escape wp=<label> commit=<later-fix> [note=...]          a later fix for a passed board
  ledger.py stats  [class=<c>] [repo=<r>] [since=YYYY-MM-DD]         per lane × class, per reviewer
  ledger.py reviewers [roots=<d>,...] [since=YYYY-MM-DD] [lens=<l>]  per reviewer × lens
  ledger.py tail   [n=10]                                            last rows, one line each

Unknown fields land as "-". Lives in ~/.playmaker (outside the skill dir, which
`playmaker skill install --force` overwrites). Fields: see ~/.playmaker/policy.md, «Ledger».
Lists are comma-separated on the command line.

--from-session resolves a playmaker session id (unique prefix OK) in
~/.playmaker/state.db (env PLAYMAKER_STATE_DB overrides, e.g. for tests), walks
to the thread root, and fills impl_lane, impl_model, wall_min, gate_first_pass,
cycles and quota_note unless the same field is given explicitly as k=v.
"""
import collections
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import sqlite3
import subprocess
import sys

DEFAULT_LEDGER = pathlib.Path.home() / ".playmaker" / "ledger.jsonl"
LEDGER = pathlib.Path(os.environ.get("PM_LEDGER", DEFAULT_LEDGER))
LOCK = LEDGER.with_name(LEDGER.name + ".lock")
TMP = LEDGER.with_name(LEDGER.name + ".tmp")
FIELDS = ["date", "repo", "wp", "class", "risk", "impl_lane", "impl_model", "gate_first_pass",
          "reviewers", "blocking_r1", "blocking_accepted", "blocking_rejected", "cycles", "rounds",
          "wall_min", "quota_note", "outcome", "commit", "note", "escaped_from"]
LISTS = {"reviewers", "escaped_from"}
INTS = {"blocking_r1", "blocking_accepted", "blocking_rejected", "cycles", "rounds", "wall_min"}
ENUM = {
    "class": {"mechanical", "feature", "terminal-heavy", "repo-recon",
              "architecture", "high-risk", "-"},
    "risk": {"routine", "normal", "high", "seams", "-"},
    "outcome": {"landed", "abandoned", "escalated", "open"},
    "gate_first_pass": {"y", "n", "-"},
}


def die(msg):
    print(f"ledger: {msg}", file=sys.stderr)
    sys.exit(2)


def kv(argv):
    out = {}
    for a in argv:
        if "=" not in a:
            die(f"expected k=v, got {a!r}")
        k, v = a.split("=", 1)
        out[k.strip()] = v.strip()
    return out


FROM_SESSION_ORDER = ["impl_lane", "impl_model", "wall_min", "gate_first_pass",
                      "cycles", "quota_note"]


def parse_from_session(argv):
    """Split `--from-session <id>` / `--from-session=<id>` out of argv."""
    ident, rest = None, []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--from-session":
            if i + 1 >= len(argv):
                die("--from-session needs a session id")
            ident, i = argv[i + 1], i + 2
            continue
        if a.startswith("--from-session="):
            ident, i = a.split("=", 1)[1], i + 1
            continue
        rest.append(a)
        i += 1
    return ident, rest


def state_db_path():
    return pathlib.Path(os.environ.get("PLAYMAKER_STATE_DB")
                        or pathlib.Path.home() / ".playmaker" / "state.db")


def _ts(value):
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt


def session_not_found(msg):
    print(f"ledger: {msg}", file=sys.stderr)
    sys.exit(1)


def collect_from_session(ident):
    """The five/five+ ledger fields a playmaker thread already knows."""
    db = state_db_path()
    if not db.exists():
        session_not_found(f"no state db at {db} (--from-session {ident!r})")
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM sessions")]
    except sqlite3.Error as exc:
        session_not_found(f"cannot read sessions from {db}: {exc}")
    finally:
        conn.close()
    exact = [r for r in rows if r["id"] == ident]
    if exact:
        session = exact[0]
    else:
        hits = [r for r in rows if str(r["id"]).startswith(ident)]
        if len(hits) > 1:
            session_not_found(f"--from-session {ident!r} matches {len(hits)} sessions; "
                              "use a longer prefix")
        if not hits:
            session_not_found(f"--from-session: no session {ident!r} in {db}")
        session = hits[0]

    by_id = {r["id"]: r for r in rows}
    root, seen = session, set()
    while root.get("parent_id") and root["parent_id"] in by_id and root["parent_id"] not in seen:
        seen.add(root["id"])
        root = by_id[root["parent_id"]]
    thread, children = [root], collections.defaultdict(list)
    for r in rows:
        if r.get("parent_id"):
            children[r["parent_id"]].append(r)
    i = 0
    while i < len(thread):
        thread.extend(children[thread[i]["id"]])
        i += 1

    end = max((t for t in (_ts(r.get("finished_at")) for r in thread) if t), default=None)
    if end is None or any(r.get("status") in ("running", "pending") for r in thread):
        end = datetime.datetime.now(datetime.UTC)
    start = _ts(root.get("started_at"))
    gate = root.get("gate_status")
    fills = {
        "impl_lane": root["agent"],
        "impl_model": root.get("model") or "-",
        "wall_min": str(int(round((end - start).total_seconds() / 60))) if start else "-",
        "gate_first_pass": "y" if gate == "pass" else ("n" if gate in ("fail", "timeout") else "-"),
        "cycles": str(sum(1 for r in thread if r.get("parent_id"))),
    }
    fallback = root.get("fallback_of")
    if fallback:
        fills["quota_note"] = f"fallback_of={str(fallback)[:8]}"
    return fills


def apply_from_session(d, fills):
    """Fill fields the command line did not give explicitly; say which on stderr."""
    applied = {k: str(v) for k, v in fills.items() if k not in d}
    d.update(applied)
    if applied:
        order = [f for f in FROM_SESSION_ORDER if f in applied]
        print("[ledger] from-session: " + " ".join(f"{k}={applied[k]}" for k in order),
              file=sys.stderr)
    return applied


def repo_name():
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True).stdout.strip()
        return pathlib.Path(top).name
    except Exception:
        return pathlib.Path.cwd().name


def rows():
    if not LEDGER.exists():
        return []
    out = []
    contents = LEDGER.read_text()
    lines = contents.splitlines(keepends=True)
    for i, raw_line in enumerate(lines, 1):
        is_trailing_fragment = i == len(lines) and not raw_line.endswith(("\n", "\r"))
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            if is_trailing_fragment:
                print(f"ledger: {LEDGER}:{i} trailing partial line skipped", file=sys.stderr)
                continue
            die(f"{LEDGER}:{i} is not JSON")
    return out


def write_all(items):
    with TMP.open("w") as fh:
        fh.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in items))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(TMP, LEDGER)


def writer_lock():
    """Hold the ledger's inter-process writer lock for one mutation."""
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = LOCK.open("a")
    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
    return lock_fh


def normalize(d):
    row = {}
    for f in FIELDS:
        v = d.get(f, "-")
        if f in LISTS:
            if v in ("-", "", None):
                v = []
            elif isinstance(v, str):
                v = [x.strip() for x in v.split(",") if x.strip()]
            else:
                v = list(v)
        elif f in INTS:
            if v in ("-", "", None):
                v = "-"
            else:
                try:
                    v = int(v)
                except ValueError:
                    die(f"{f} must be an integer or '-', got {v!r}")
        row[f] = v
    for f, allowed in ENUM.items():
        if row[f] not in allowed:
            die(f"{f}={row[f]!r} — allowed: {sorted(allowed)}")
    return row


def reviewer_key(lane, model):
    m = (model or "").strip()
    if lane == "agy":
        if not m or m == "-" or ("gemini" in m and "pro" in m):
            return "agy-gemini-pro"
        return "agy-" + m
    if lane == "opencode":
        if not m or m == "-" or "glm-5.3" in m:
            return "glm-5.3"
        return m.split("/")[-1]
    if lane == "kimi":
        return "kimi-k3"
    if lane == "claude":
        if not m or m == "-" or "opus" in m:
            return "opus"
        return m
    return lane


def cmd_add(argv, fills=None):
    d = kv(argv)
    apply_from_session(d, fills or {})
    for req in ("wp", "class", "impl_lane"):
        if req not in d:
            die(f"{req}= is required")
    d.setdefault("date", datetime.date.today().isoformat())
    d.setdefault("repo", repo_name())
    d.setdefault("outcome", "open")
    d.setdefault("risk", "-")
    row = normalize(d)
    with writer_lock() as lock_fh:
        try:
            with LEDGER.open("a") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    print(f"ledger: +1 → {LEDGER} "
          f"({row['repo']}/{row['wp']} {row['class']} {row['impl_lane']} {row['outcome']})")


def cmd_fix(argv, fills=None):
    d = kv(argv)
    if "wp" not in d:
        die("wp= is required")
    sel = {k: d.pop(k) for k in ("wp", "repo", "commit") if k in d}
    apply_from_session(d, fills or {})
    if not d:
        die("nothing to update — pass k=v fields")
    for k in d:
        if k not in FIELDS:
            die(f"unknown field {k!r}")
    with writer_lock() as lock_fh:
        try:
            items = rows()
            hit = [r for r in items if r["wp"] == sel["wp"]
                   and ("repo" not in sel or r.get("repo") == sel["repo"])
                   and ("commit" not in sel or r.get("commit") == sel["commit"])]
            if not hit:
                die(f"no row matching {sel}")
            r = hit[-1]
            merged = dict(r)
            merged.update(d)
            row = normalize(merged)
            r.clear()
            r.update(row)
            write_all(items)
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    print(f"ledger: fixed {r['repo']}/{r['wp']} commit={r['commit']} ({', '.join(sorted(d))})")


def cmd_escape(argv):
    d = kv(argv)
    if "wp" not in d or "commit" not in d:
        die("wp= and commit= are required")
    with writer_lock() as lock_fh:
        try:
            items = rows()
            hit = [r for r in items
                   if r["wp"] == d["wp"] and r.get("repo", "-") in (d.get("repo", r.get("repo")),)]
            if not hit:
                die(f"no row for wp={d['wp']!r}")
            r = hit[-1]
            r.setdefault("escaped_from", [])
            r["escaped_from"].append(d["commit"] + (f" ({d['note']})" if d.get("note") else ""))
            write_all(items)
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    print(f"ledger: {r['repo']}/{r['wp']} escaped_from += {d['commit']}")


def pct(n, d):
    return f"{100 * n / d:3.0f}%" if d else "  — "


def cmd_stats(argv):
    f = kv(argv)
    items = rows()
    if f.get("class"):
        items = [r for r in items if r["class"] == f["class"]]
    if f.get("repo"):
        items = [r for r in items if r["repo"] == f["repo"]]
    if f.get("since"):
        items = [r for r in items if r["date"] >= f["since"]]
    if not items:
        print("ledger: no rows")
        return
    print(f"{len(items)} rows · {LEDGER}\n")
    stuck = [r for r in items
             if r["outcome"] == "open" and r.get("commit", "-") not in ("-", "")]
    if stuck:
        wps = ", ".join(r["wp"] for r in stuck)
        print(f"! {len(stuck)} open row(s) carry a commit — the hook wrote them and "
              f"nobody closed them: {wps} … (ledger.py fix wp=… outcome=landed)\n")
    print(f"{'impl lane / class':34} {'n':>3} {'open':>4} {'gate1st':>7} {'landed':>7} "
          f"{'r1 blk':>7} {'cycles':>7} {'escaped':>7}")
    groups = collections.defaultdict(list)
    for r in items:
        groups[(r["impl_lane"], r["class"])].append(r)
    for (lane, cls), g in sorted(groups.items()):
        closed = [r for r in g if r["outcome"] in ("landed", "abandoned", "escalated")]
        n_open = sum(r["outcome"] == "open" for r in g)
        known_gate = [r for r in closed if r["gate_first_pass"] in ("y", "n")]
        blk = [r["blocking_r1"] for r in closed if isinstance(r["blocking_r1"], int)]
        cyc = [r["cycles"] for r in closed if isinstance(r["cycles"], int)]
        gate1st = pct(sum(r["gate_first_pass"] == "y" for r in known_gate), len(known_gate))
        landed = pct(sum(r["outcome"] == "landed" for r in closed), len(closed))
        head = f"{lane + ' / ' + cls:34} {len(g):>3} {n_open:>4} {gate1st:>7} {landed:>7}"
        if blk:
            head += f" {(sum(blk) / len(blk)):>7.1f}"
        else:
            head += f" {'—':>7}"
        avg_cyc = (sum(cyc) / len(cyc)) if cyc else float("nan")
        head += f" {avg_cyc:>7.1f} {sum(bool(r.get('escaped_from')) for r in g):>7}"
        if len(closed) < 5:
            head += " *"
        print(head)
    print("* fewer than 5 closed rows — not enough evidence to remove a lane "
          "(policy step 3); measure instead")
    print()
    rev = collections.Counter()
    rev_known = collections.Counter()
    rev_pos = collections.Counter()
    for r in items:
        seats = []
        for name in r["reviewers"]:
            key = reviewer_key(name, "")
            if key not in seats:
                seats.append(key)
        for key in seats:
            rev[key] += 1
            if isinstance(r["blocking_accepted"], int):
                rev_known[key] += 1
                if r["blocking_accepted"] > 0:
                    rev_pos[key] += 1
    head = "reviewer (boards sat on — board-level, not this reviewer's findings)"
    print(f"{head:34} {'boards':>6} {'acc known':>9} {'acc>0':>6}")
    for name, n in rev.most_common():
        print(f"{name:34} {n:>6} {rev_known[name]:>9} {rev_pos[name]:>6}")
    repo = repo_name()
    print(f"per-reviewer findings by lens: ledger.py reviewers  (run in the repo root; "
          f"default roots = . and ../{repo}-wt)")


def verdict_paths(roots):
    found = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            if "node_modules" in dirnames:
                dirnames.remove("node_modules")
            for fn in filenames:
                if fn.startswith("verdict-") and fn.endswith(".json"):
                    found.append(pathlib.Path(dirpath) / fn)
    return found


def split_verdict_name(fn):
    rest = fn[len("verdict-"):-len(".json")]
    toks = rest.split("-")
    if len(toks) < 2:
        return None
    lane, lens = toks[0], toks[1]
    suffix = ""
    if len(toks) > 2 and toks[-1].startswith("r") and toks[-1][1:].isdigit():
        suffix = "-" + toks[-1]
    return lane, lens, suffix


def finding_point(fb):
    if not isinstance(fb, dict):
        return None
    sev = fb.get("severity")
    if sev not in ("blocking", "major", "minor", "nit"):
        return None
    try:
        line = int(fb.get("line") or 0)
    except (TypeError, ValueError):
        line = 0
    return sev, fb.get("file") or "", line


def cmd_reviewers(argv):
    f = kv(argv)
    if f.get("roots"):
        roots = [r.strip() for r in f["roots"].split(",") if r.strip()]
    else:
        roots = [str(pathlib.Path.cwd())]
        wt = str(pathlib.Path.cwd()) + "-wt"
        if pathlib.Path(wt).is_dir():
            roots.append(wt)
    since = f.get("since", "")
    lens_only = f.get("lens", "")
    seen = {}
    skipped = 0
    for p in verdict_paths(roots):
        try:
            raw = p.read_bytes()
            data = json.loads(raw)
        except (OSError, ValueError):
            skipped += 1
            continue
        if not isinstance(data, dict):
            skipped += 1
            continue
        if since:
            try:
                mtime = datetime.date.fromtimestamp(p.stat().st_mtime).isoformat()
            except OSError:
                continue
            if mtime < since:
                continue
        parts = p.parts
        # the last `.playmaker/reviews` pair — a root may itself sit under a dir named `reviews`
        ri = next((i + 1 for i in range(len(parts) - 2, -1, -1)
                   if parts[i] == ".playmaker" and parts[i + 1] == "reviews"), None)
        if ri is None or ri + 1 >= len(parts) - 1:
            continue
        name = split_verdict_name(p.name)
        if name is None:
            continue
        flane, flens, suffix = name
        parent = p.parent.name
        round_id = (parent if parent.startswith("archive-") else "final") + suffix
        key = (parts[ri + 1], round_id, p.name, hashlib.sha256(raw).hexdigest())
        if key not in seen:
            seen[key] = (data, parts[ri + 1], round_id, flane, flens)
    recs = []
    for data, wp, round_id, flane, flens in seen.values():
        lens = data.get("lens") or flens
        if not isinstance(lens, str) or not lens:
            lens = flens
        rv = data.get("reviewer")
        if isinstance(rv, str) and "/" in rv:
            model = rv.split("/", 1)[1]
        else:
            model = ""
        finds = data.get("findings")
        if not isinstance(finds, list):
            finds = []
        pts = [pt for pt in (finding_point(fb) for fb in finds) if pt]
        recs.append({
            "wp": wp, "round": round_id, "lens": lens,
            "who": reviewer_key(flane, model),
            "says_pass": data.get("verdict") in ("pass", "pass_with_nits"),
            "has_blk": any(s == "blocking" for s, _, _ in pts),
            "blk": [(fl, ln) for s, fl, ln in pts if s == "blocking"],
            "sig": [(fl, ln) for s, fl, ln in pts if s in ("blocking", "major")],
            "major": sum(s == "major" for s, _, _ in pts),
            "minor": sum(s in ("minor", "nit") for s, _, _ in pts),
        })
    print(f"{len(recs)} verdict files · {skipped} skipped · roots {', '.join(roots)}\n")
    if not recs:
        print("ledger: no verdicts")
        return
    for rc in recs:
        uniq = 0
        for fl, ln in rc["blk"]:
            hit = any(o is not rc and o["wp"] == rc["wp"] and o["round"] == rc["round"]
                      and fl and fl == ofl and abs(ln - oln) <= 20
                      for o in recs for ofl, oln in o["sig"])
            if not hit:
                uniq += 1
        rc["unique"] = uniq
        same = [o for o in recs if o is not rc and o["wp"] == rc["wp"]
                and o["round"] == rc["round"] and o["has_blk"]]
        rc["miss_same"] = 1 if rc["says_pass"] and any(o["lens"] == rc["lens"]
                                                       for o in same) else 0
        rc["miss_other"] = 1 if rc["says_pass"] and any(o["lens"] != rc["lens"]
                                                        for o in same) else 0
    groups = collections.defaultdict(list)
    for rc in recs:
        # filter the printed rows only: unique and missed compare against every seat
        if lens_only and rc["lens"] != lens_only:
            continue
        groups[(rc["lens"], rc["who"])].append(rc)
    print(f"{'lens / reviewer':34} {'verdicts':>8} {'boards':>6} {'with blk':>8} "
          f"{'blk':>5} {'unique':>6} {'major':>5} {'minor':>5} "
          f"{'missed same':>11} {'missed other':>12}")
    for (lens, who), g in sorted(groups.items(), key=lambda it: (it[0][0], -len(it[1]), it[0][1])):
        line = (f"{lens + ' / ' + who:34} {len(g):>8} {len({r['wp'] for r in g}):>6} "
                f"{sum(r['has_blk'] for r in g):>8} {sum(len(r['blk']) for r in g):>5} "
                f"{sum(r['unique'] for r in g):>6} {sum(r['major'] for r in g):>5} "
                f"{sum(r['minor'] for r in g):>5} {sum(r['miss_same'] for r in g):>11} "
                f"{sum(r['miss_other'] for r in g):>12}")
        if len(g) < 5:
            line += " *"
        print(line)
    print("* fewer than 5 verdicts on this lens — a reason to measure, not to judge")


def cmd_tail(argv):
    n = int(kv(argv).get("n", 10))
    items = rows()[-n:]
    for r in items:
        print(f"{r['date']} {r['repo']:9} {r['wp']:16} {r['class']:13} {r['risk']:7} "
              f"{r['impl_lane']:9} gate1st={r['gate_first_pass']} "
              f"r1blk={r['blocking_r1']} cyc={r['cycles']} {r['outcome']:9} {r['commit']}")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    cmd, argv = sys.argv[1], sys.argv[2:]
    commands = {"add": cmd_add, "fix": cmd_fix, "escape": cmd_escape,
                "stats": cmd_stats, "reviewers": cmd_reviewers, "tail": cmd_tail}
    if cmd not in commands:
        die(f"unknown command {cmd!r}")
    fills = {}
    if cmd in ("add", "fix"):
        from_session, argv = parse_from_session(argv)
        if from_session is not None:
            fills = collect_from_session(from_session)
        commands[cmd](argv, fills)
    else:
        commands[cmd](argv)
