#!/usr/bin/env python3
"""ledger.py — one JSON line per work package: the evidence the coach routes on.

  ledger.py add    wp=<label> class=<c> impl_lane=<lane> [k=v ...]   append a WP row (unknown → "-")
  ledger.py escape wp=<label> commit=<later-fix> [note=...]           a later fix for what a board passed
  ledger.py stats  [class=<c>] [repo=<r>] [since=YYYY-MM-DD]          per lane × class, per reviewer
  ledger.py tail   [n=10]                                             last rows, one line each

Lives in ~/.playmaker (outside the skill dir, which `playmaker skill install --force` overwrites).
Fields: see ~/.playmaker/policy.md, «Ledger». Lists are comma-separated on the command line.
"""
import json, os, sys, datetime, subprocess, collections, pathlib

LEDGER = pathlib.Path(os.environ.get("PM_LEDGER", pathlib.Path.home() / ".playmaker" / "ledger.jsonl"))
FIELDS = ["date","repo","wp","class","risk","impl_lane","impl_model","gate_first_pass","reviewers",
          "blocking_r1","blocking_accepted","blocking_rejected","cycles","rounds","wall_min",
          "quota_note","outcome","commit","note","escaped_from"]
LISTS = {"reviewers","escaped_from"}
INTS  = {"blocking_r1","blocking_accepted","blocking_rejected","cycles","rounds","wall_min"}
ENUM  = {
  "class": {"mechanical","feature","terminal-heavy","repo-recon","architecture","high-risk"},
  "risk": {"routine","normal","high","seams","-"},
  "outcome": {"landed","abandoned","escalated","open"},
  "gate_first_pass": {"y","n","-"},
}

def die(msg): print(f"ledger: {msg}", file=sys.stderr); sys.exit(2)

def kv(argv):
    out = {}
    for a in argv:
        if "=" not in a: die(f"expected k=v, got {a!r}")
        k, v = a.split("=", 1); out[k.strip()] = v.strip()
    return out

def repo_name():
    try:
        top = subprocess.run(["git","rev-parse","--show-toplevel"],capture_output=True,text=True,check=True).stdout.strip()
        return pathlib.Path(top).name
    except Exception:
        return pathlib.Path.cwd().name

def rows():
    if not LEDGER.exists(): return []
    out = []
    for i, line in enumerate(LEDGER.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"): continue
        try: out.append(json.loads(line))
        except json.JSONDecodeError: die(f"{LEDGER}:{i} is not JSON")
    return out

def write_all(items):
    LEDGER.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in items))

def normalize(d):
    row = {}
    for f in FIELDS:
        v = d.get(f, "-")
        if f in LISTS:
            v = [] if v in ("-", "", None) else ([x.strip() for x in v.split(",") if x.strip()] if isinstance(v, str) else list(v))
        elif f in INTS:
            if v in ("-", "", None): v = "-"
            else:
                try: v = int(v)
                except ValueError: die(f"{f} must be an integer or '-', got {v!r}")
        row[f] = v
    for f, allowed in ENUM.items():
        if row[f] not in allowed: die(f"{f}={row[f]!r} — allowed: {sorted(allowed)}")
    return row

def cmd_add(argv):
    d = kv(argv)
    for req in ("wp","class","impl_lane"):
        if req not in d: die(f"{req}= is required")
    d.setdefault("date", datetime.date.today().isoformat())
    d.setdefault("repo", repo_name())
    d.setdefault("outcome", "open")
    d.setdefault("risk", "-")
    row = normalize(d)
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as fh: fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"ledger: +1 → {LEDGER} ({row['repo']}/{row['wp']} {row['class']} {row['impl_lane']} {row['outcome']})")

def cmd_escape(argv):
    d = kv(argv)
    if "wp" not in d or "commit" not in d: die("wp= and commit= are required")
    items = rows(); hit = [r for r in items if r["wp"] == d["wp"] and r.get("repo", "-") in (d.get("repo", r.get("repo")),)]
    if not hit: die(f"no row for wp={d['wp']!r}")
    r = hit[-1]; r.setdefault("escaped_from", [])
    r["escaped_from"].append(d["commit"] + (f" ({d['note']})" if d.get("note") else ""))
    write_all(items); print(f"ledger: {r['repo']}/{r['wp']} escaped_from += {d['commit']}")

def pct(n, d): return f"{100*n/d:3.0f}%" if d else "  — "

def cmd_stats(argv):
    f = kv(argv); items = rows()
    if f.get("class"): items = [r for r in items if r["class"] == f["class"]]
    if f.get("repo"): items = [r for r in items if r["repo"] == f["repo"]]
    if f.get("since"): items = [r for r in items if r["date"] >= f["since"]]
    if not items: print("ledger: no rows"); return
    print(f"{len(items)} rows · {LEDGER}\n")
    print(f"{'impl lane / class':34} {'n':>3} {'gate1st':>7} {'landed':>7} {'r1 blk':>7} {'cycles':>7} {'escaped':>7}")
    groups = collections.defaultdict(list)
    for r in items: groups[(r["impl_lane"], r["class"])].append(r)
    for (lane, cls), g in sorted(groups.items()):
        known_gate = [r for r in g if r["gate_first_pass"] in ("y","n")]
        blk = [r["blocking_r1"] for r in g if isinstance(r["blocking_r1"], int)]
        cyc = [r["cycles"] for r in g if isinstance(r["cycles"], int)]
        print(f"{lane+' / '+cls:34} {len(g):>3} {pct(sum(r['gate_first_pass']=='y' for r in known_gate), len(known_gate)):>7} "
              f"{pct(sum(r['outcome']=='landed' for r in g), len(g)):>7} "
              f"{(sum(blk)/len(blk)):>7.1f}" if blk else f"{lane+' / '+cls:34} {len(g):>3} {pct(sum(r['gate_first_pass']=='y' for r in known_gate), len(known_gate)):>7} {pct(sum(r['outcome']=='landed' for r in g), len(g)):>7} {'—':>7}", end="")
        print(f" {(sum(cyc)/len(cyc)) if cyc else float('nan'):>7.1f} {sum(bool(r.get('escaped_from')) for r in g):>7}")
    print()
    rev = collections.Counter(); rev_blk = collections.Counter()
    for r in items:
        for name in r["reviewers"]:
            rev[name] += 1
            if isinstance(r["blocking_accepted"], int) and r["blocking_accepted"] > 0: rev_blk[name] += 1
    print(f"{'reviewer (boards sat on)':34} {'n':>3} {'on boards with accepted blocking':>34}")
    for name, n in rev.most_common():
        print(f"{name:34} {n:>3} {rev_blk[name]:>34}")

def cmd_tail(argv):
    n = int(kv(argv).get("n", 10)); items = rows()[-n:]
    for r in items:
        print(f"{r['date']} {r['repo']:9} {r['wp']:16} {r['class']:13} {r['risk']:7} {r['impl_lane']:9} gate1st={r['gate_first_pass']} "
              f"r1blk={r['blocking_r1']} cyc={r['cycles']} {r['outcome']:9} {r['commit']}")

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h","--help"): print(__doc__); sys.exit(0)
    cmd, argv = sys.argv[1], sys.argv[2:]
    {"add": cmd_add, "escape": cmd_escape, "stats": cmd_stats, "tail": cmd_tail}.get(cmd, lambda a: die(f"unknown command {cmd!r}"))(argv)
