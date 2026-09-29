#!/usr/bin/env python3
"""ledger-hook.py — PostToolUse hook (matcher "Bash"): one ledger row per landed WP commit.

Runs on EVERY Bash call: the non-commit path imports only json/re/sys/os and exits in <100 ms.
On a `git commit` it matches the commit to a reviewed WP under .playmaker/reviews/, derives the
mechanical fields, asks a junior model (codex exec, read-only) to fill the fields left as "-",
and appends the row via ledger.py add (PM_LEDGER respected). Never blocks the commit: always
exit 0, no stderr on the normal path.

Dev flags (no stdin needed with --selftest):
  --selftest            canned case: --cwd /Users/shulyugin/Sites/lectella --commit d5c0be27
                        --wp gpb2-be --dry-run --no-junior; exit 0 only if the row checks out
  --cwd DIR             override the event's cwd
  --commit SHA          override HEAD (used by the selftest)
  --wp LABEL            force the WP match (skip patch overlap)
  --dry-run             print the row JSON, append nothing
  --no-junior / --junior  force the junior model off/on
"""
import json
import os
import re
import sys

GIT_RE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+|--git-dir=\S+\s+|-c\s+\S+\s+)*([a-z][a-z-]*)")
DIFF_GIT_RE = re.compile(r"^diff --git a/(\S+) b/", re.M)
DRY_RUN_RE = re.compile(r"(^|\s)--dry-run(\s|$)")
LOG = os.path.expanduser("~/.playmaker/logs/ledger-hook.log")
CONFIG = os.path.expanduser("~/.playmaker/config.toml")
DEFAULT_JUNIOR = "codex:gpt-5.3-codex-spark"
BUDGET_S = 90
JUNIOR_TIMEOUT_S = 60

FILLABLE = ("gate_first_pass", "blocking_accepted", "blocking_rejected", "cycles",
            "class", "impl_lane", "impl_model")
CLASS_ENUM = {"mechanical", "feature", "terminal-heavy", "repo-recon",
              "architecture", "high-risk", "-"}


def emit(context):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                             "additionalContext": context}}, ensure_ascii=False))


def log(msg):
    try:
        import datetime
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as fh:
            stamp = datetime.datetime.now().isoformat(timespec="seconds")
            fh.write(f"{stamp} {msg}\n")
    except Exception:
        pass


def is_commit_command(cmd):
    # --dry-run only as a standalone token of the git command, outside quoted text:
    # a commit MESSAGE mentioning it must not be skipped
    unquoted = re.sub(r'"[^"]*"|\'[^\']*\'', "", cmd)
    if DRY_RUN_RE.search(unquoted):
        return False
    m = GIT_RE.search(cmd)
    return bool(m) and m.group(1) == "commit"


def parse_args(argv):
    a = {"cwd": None, "commit": None, "wp": None, "dry_run": False,
         "no_junior": False, "junior": False, "selftest": False}
    i = 0
    while i < len(argv):
        k = argv[i]
        if k in ("--cwd", "--commit", "--wp"):
            a[k[2:].replace("-", "_")] = argv[i + 1]
            i += 2
        elif k == "--dry-run":
            a["dry_run"] = True
            i += 1
        elif k == "--no-junior":
            a["no_junior"] = True
            i += 1
        elif k == "--junior":
            a["junior"] = True
            i += 1
        elif k == "--selftest":
            a["selftest"] = True
            i += 1
        else:
            i += 1
    return a


def short_reviewer(lane, model):
    """lane|model from sessions.txt → ledger short form.

    kimi-code/k3-256k → kimi-k3, zai-coding-plan/glm-5.3 → glm-5.3, "-" → the lane itself.
    """
    lane = (lane or "").strip()
    model = (model or "").strip()
    if not model or model == "-":
        return lane
    base = model.split("/")[-1]
    if lane == "kimi":
        return "kimi-" + base.split("-")[0]
    if lane == "opencode":
        return base
    if lane == "agy":
        s = re.sub(r"\d+(\.\d+)*", "", base)
        s = re.sub(r"-(high|medium|low|thinking)$", "", s, flags=re.I)
        s = re.sub(r"-+", "-", s).strip("-").lower()
        return "agy-" + s if s else lane
    return lane if base == "-" else base


def patch_files(path):
    try:
        with open(path, errors="replace") as fh:
            return set(DIFF_GIT_RE.findall(fh.read()))
    except OSError:
        return set()


def find_review_dirs(cwd, git):
    toplevel = git("rev-parse", "--show-toplevel") or cwd
    common = git("rev-parse", "--git-common-dir")
    if common and not os.path.isabs(common):
        common = os.path.abspath(os.path.join(cwd, common))
    cands = []
    for base in (toplevel, os.path.dirname(common) if common else None):
        if not base:
            continue
        p = os.path.join(base, ".playmaker", "reviews")
        if os.path.isdir(p) and p not in cands:
            cands.append(p)
    return toplevel, cands


def match_wp(cands, files, wp, now):
    """Return the review dir: forced by --wp, else max patch-file overlap, ties → newest patch."""
    import glob
    if wp:
        for reviews in cands:
            d = os.path.join(reviews, wp)
            if os.path.isdir(d):
                return d
        return None
    best = None
    for reviews in cands:
        for name in os.listdir(reviews):
            d = os.path.join(reviews, name)
            if not os.path.isdir(d):
                continue
            patches = glob.glob(os.path.join(d, "diff-r*.patch"))
            if not patches:
                continue
            newest = max(os.path.getmtime(p) for p in patches)
            if now - max(os.path.getmtime(d), newest) > 14 * 86400:
                continue
            pfiles = set()
            for p in patches:
                pfiles |= patch_files(p)
            ov = len(pfiles & files)
            if ov and (best is None or (ov, newest) > best[0]):
                best = ((ov, newest), d)
    return best[1] if best else None


def read(path, cap=None):
    try:
        with open(path, errors="replace") as fh:
            t = fh.read()
        return t[:cap] if cap else t
    except OSError:
        return ""


def spec_header(spec_text):
    """class / impl_lane / impl_model / risk from the spec header (item 8); risk may also sit in
    the title of pre-header specs ('# WP x — … (risk high)')."""
    cls = lane = model = risk = None
    for line in spec_text.splitlines()[:15]:
        m = re.match(r"^class:\s*(\S+)", line)
        if m:
            cls = m.group(1)
        m = re.match(r"^impl:\s*(\S+)\s+(\S+)", line)
        if m:
            lane, model = m.group(1), m.group(2)
        m = re.match(r"^risk:\s*(\S+)", line)
        if m:
            risk = m.group(1).lower()
    if not risk:
        title = spec_text.splitlines()[0] if spec_text else ""
        m = re.search(r"\brisk[:\s]+(routine|normal|high|seams)\b", title, re.I)
        if m:
            risk = m.group(1).lower()
    return cls, lane, model, risk


def board_env(d):
    env = {}
    for line in read(os.path.join(d, "board.env")).splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def reviewers(d):
    """Union of lanes over sessions.txt and every archived sessions.txt, short form, order kept."""
    import glob
    out = []
    archived = sorted(glob.glob(os.path.join(d, "*", "sessions.txt")))
    for path in archived + [os.path.join(d, "sessions.txt")]:
        for line in read(path).splitlines():
            parts = line.split("|")
            if len(parts) >= 3 and parts[1].strip():
                s = short_reviewer(parts[1], parts[2])
                if s and s not in out:
                    out.append(s)
    return out


def blocking_r1(d, rounds):
    """Blocking findings across round-1 verdicts (oldest archive when rounds > 1), deduped by
    file:line — two reviewers on the same finding count once."""
    import glob
    verdicts = []
    if rounds > 1:
        archives = [
            a for a in glob.glob(os.path.join(d, "*"))
            if os.path.isdir(a) and re.match(r"^(archive-|r\d+-archive)", os.path.basename(a))
        ]
        if archives:
            oldest = min(archives, key=os.path.getmtime)
            verdicts = glob.glob(os.path.join(oldest, "verdict-*.json"))
    if not verdicts:
        verdicts = glob.glob(os.path.join(d, "verdict-*.json"))
    seen = set()
    for path in verdicts:
        try:
            v = json.loads(read(path))
        except ValueError:
            continue
        for f in v.get("findings", []):
            if f.get("severity") == "blocking":
                seen.add((f.get("file"), f.get("line")))
    return len(seen) if verdicts else "-"


def junior_config():
    """~/.playmaker/config.toml, [ledger] junior = "codex:gpt-5.3-codex-spark" (or "none").
    Tiny regex parse — python 3.9 has no tomllib. Missing config → the default junior."""
    in_ledger = False
    for line in read(CONFIG).splitlines():
        s = line.strip()
        if s.startswith("["):
            in_ledger = s == "[ledger]"
        elif in_ledger:
            m = re.match(r'junior\s*=\s*"([^"]*)"', s)
            if m:
                return m.group(1).strip()
    return DEFAULT_JUNIOR


def junior_prompt(d, wp, row, env):
    import glob
    parts = [
        "You are the ledger junior: refine one ledger row for a work package (WP)",
        "that just landed. The mechanical draft below already has values; fields marked \"-\"",
        "are unknown. Fill ONLY fields that are \"-\" in the draft, from the evidence below.",
        "Never change a set value.",
        "gate_first_pass: y if the first gate run passed before any fix, n if a fix-r0 (pre-review",
        "fix) exists or the first review round failed the gate. blocking_accepted /",
        "blocking_rejected: of the blocking findings raised, how many the coach accepted (fixed)",
        "vs rejected (dropped). cycles: fix cycles after round 1. class / impl_lane / impl_model:",
        "WP class and the lane+model that implemented it.",
        "Reply with ONLY this JSON object, no prose, no fence:",
        '{"gate_first_pass":"y|n|-","blocking_accepted":int|"-","blocking_rejected":int|"-",'
        '"cycles":int|"-","class":"…|-","impl_lane":"…|-","impl_model":"…|-",'
        '"confidence":"high|medium|low"}',
        "",
        "MECHANICAL DRAFT:",
        json.dumps(row, ensure_ascii=False),
        "",
        "SPEC (spec.md):",
        read(os.path.join(d, "spec.md"), 8000),
        "",
        "BOARD.ENV: " + json.dumps(env),
    ]
    for path in sorted(glob.glob(os.path.join(d, "fix-r*.md"))):
        parts.append(f"\n{os.path.basename(path)}:\n{read(path, 4000)}")
    verdicts = sorted(glob.glob(os.path.join(d, "verdict-*.json"))
                      + glob.glob(os.path.join(d, "*", "verdict-*.json")))
    blob = ""
    for path in verdicts:
        blob += f"\n--- {os.path.relpath(path, d)} ---\n{read(path, 6000)}"
        if len(blob) > 24000:
            break
    parts.append("\nVERDICTS (all rounds):" + blob[:24000])
    for reviews in (os.path.dirname(d),):
        board = os.path.join(os.path.dirname(reviews), "board.md")
        lines = [ln for ln in read(board).splitlines() if wp in ln]
        if lines:
            parts.append("\nBOARD.md lines mentioning this WP:\n" + "\n".join(lines[:5]))
    return "\n".join(parts)


def run_junior(d, wp, row, env, args, t0):
    """Return (name, error). On success the fillable "-" fields of row are updated in place."""
    import subprocess
    import tempfile
    import time
    if args["no_junior"]:
        return None, "disabled (--no-junior)"
    cfg = junior_config()
    if args["junior"] and cfg == "none":
        cfg = DEFAULT_JUNIOR
    if cfg == "none":
        return None, "config: none"
    if ":" not in cfg:
        return None, f"bad config: {cfg!r}"
    tool, model = cfg.split(":", 1)
    if tool != "codex":
        return None, f"unknown junior tool {tool!r}"
    if time.time() - t0 > BUDGET_S - JUNIOR_TIMEOUT_S - 5:
        return None, "no budget left"
    out = tempfile.NamedTemporaryFile(prefix="ledger-junior-", suffix=".txt", delete=False)
    out.close()
    try:
        proc = subprocess.run(["codex", "exec", "-m", model, "-s", "read-only", "-C", d,
                               "--skip-git-repo-check", "--output-last-message", out.name,
                               junior_prompt(d, wp, row, env)],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=JUNIOR_TIMEOUT_S)
        raw = read(out.name)
        err = (proc.stderr or proc.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return None, f"timeout {JUNIOR_TIMEOUT_S}s"
    except OSError as e:
        return None, f"codex exec failed: {e}"
    finally:
        try:
            os.unlink(out.name)
        except OSError:
            pass
    jr = None
    dec = json.JSONDecoder()
    for j in range(len(raw) - 1, -1, -1):
        if raw[j] == "{":
            try:
                obj, _ = dec.raw_decode(raw[j:])
            except ValueError:
                continue
            if isinstance(obj, dict) and "confidence" in obj:
                jr = obj
                break
    if jr is None:
        m = re.search(r'"message":"([^"]+)"', err)
        reason = m.group(1) if m else (err.splitlines()[-1] if err else "no JSON in reply")
        return None, reason[:120]
    for k in FILLABLE:
        v = jr.get(k, "-")
        if row.get(k) not in ("-", "", None) or v in ("-", "", None):
            continue
        if k == "gate_first_pass" and v in ("y", "n"):
            row[k] = v
        elif k in ("blocking_accepted", "blocking_rejected", "cycles"):
            try:
                row[k] = int(v)
            except (TypeError, ValueError):
                pass
        elif k == "class" and v in CLASS_ENUM:
            row[k] = v
        elif k in ("impl_lane", "impl_model") and isinstance(v, str) and v.strip():
            row[k] = v.strip()
    return cfg, None


def build_row(d, sha, toplevel, now):
    import datetime
    import glob
    wp = os.path.basename(d)
    spec_text = read(os.path.join(d, "spec.md"))
    cls, lane, model, risk = spec_header(spec_text)
    env = board_env(d)
    if not risk:
        risk = env.get("risk", "-")
    rounds = len(glob.glob(os.path.join(d, "diff-r*.patch")))
    cycles = len([p for p in glob.glob(os.path.join(d, "fix-r*.md"))
                  if re.match(r"fix-r[1-9]\d*\.md$", os.path.basename(p))])
    spec_mtime = None
    try:
        spec_mtime = os.path.getmtime(os.path.join(d, "spec.md"))
    except OSError:
        pass
    row = {
        "date": datetime.date.today().isoformat(),
        "repo": os.path.basename(toplevel.rstrip("/")) or "-",
        "wp": wp,
        "class": cls if cls in CLASS_ENUM - {"-"} else "-",
        "risk": risk if risk in ("routine", "normal", "high", "seams") else "-",
        "impl_lane": lane or "-",
        "impl_model": model or "-",
        "gate_first_pass": "n" if os.path.exists(os.path.join(d, "fix-r0.md")) else "-",
        "reviewers": reviewers(d),
        "blocking_r1": blocking_r1(d, rounds),
        "blocking_accepted": "-",
        "blocking_rejected": "-",
        "cycles": cycles,
        "rounds": rounds,
        "wall_min": round((now - spec_mtime) / 60) if spec_mtime else "-",
        "outcome": "landed",
        "commit": sha,
    }
    return wp, row, env


def ledger_path():
    return os.environ.get("PM_LEDGER", os.path.expanduser("~/.playmaker/ledger.jsonl"))


def already_recorded(wp, sha):
    for line in read(ledger_path()).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("wp") == wp and r.get("commit") == sha:
            return True
    return False


def append_row(row):
    import subprocess
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger.py")
    kv = []
    for k, v in row.items():
        if isinstance(v, list):
            v = ",".join(v)
        kv.append(f"{k}={v}")
    return subprocess.run([sys.executable, script, "add"] + kv,
                          capture_output=True, text=True, timeout=30)


def elapsed_ms(t0):
    import time
    return int((time.time() - t0) * 1000)


def handle_commit(cwd, args, t0, tool_output=None):
    import subprocess
    import time

    def git(*a):
        try:
            return subprocess.run(["git", "-C", cwd] + list(a),
                                  capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            return ""

    if not args["commit"]:
        # A FAILED `git commit` (pre-commit hook, "nothing to commit", bad flags) leaves HEAD
        # unmoved; matching the old HEAD would ledger a landing that never happened. HEAD
        # freshness is the ONLY failure detector — the tool_output text cannot be trusted
        # (a successful commit echoes its own message; tool_output may also arrive as a dict).
        # An explicit --commit <sha> (--selftest, coach replay) bypasses this check on purpose.
        try:
            fresh = time.time() - int(git("log", "-1", "--format=%ct", "HEAD")) <= 180
        except ValueError:
            fresh = False
        if not fresh:
            log(f"cwd={cwd} HEAD not fresh — the commit did not happen")
            return

    toplevel, cands = find_review_dirs(cwd, git)
    sha = args["commit"] or git("rev-parse", "--short", "HEAD")
    files = set(git("show", "--name-only", "--format=", args["commit"] or "HEAD").split())
    files.discard("")
    d = match_wp(cands, files, args["wp"], time.time())
    if not d:
        if not args["wp"]:
            emit(f"[ledger] commit {sha} matched no reviewed WP — if it landed one: "
                 "ledger.py add wp=… class=… impl_lane=…")
            log(f"cwd={cwd} sha={sha} nomatch total_ms={elapsed_ms(t0)}")
        return
    if already_recorded(os.path.basename(d), sha):
        log(f"cwd={cwd} sha={sha} wp={os.path.basename(d)} dup total_ms={elapsed_ms(t0)}")
        return
    wp, row, env = build_row(d, sha, toplevel, time.time())
    tj = time.time()
    junior, jerr = run_junior(d, wp, row, env, args, t0)
    j_ms = int((time.time() - tj) * 1000)
    junior_state = junior if junior else f"none: {jerr}"
    row["note"] = f"auto-hook; junior={junior_state}"
    if args["dry_run"]:
        print(json.dumps(row, ensure_ascii=False, indent=2))
        log(f"cwd={cwd} sha={sha} wp={wp} dry-run junior={junior or jerr}({j_ms}ms) "
            f"total_ms={elapsed_ms(t0)}")
        return row
    rc = append_row(row)
    if rc.returncode != 0:
        log(f"cwd={cwd} sha={sha} wp={wp} add-failed: {rc.stderr.strip()[:200]}")
        return
    emit(f"[ledger] {row['repo']}/{wp} ← {sha}: {row['class']} {row['impl_lane']} "
         f"gate1st={row['gate_first_pass']} r1blk={row['blocking_r1']} cycles={row['cycles']} "
         f"landed (junior: {junior or 'none'}) — поправить: ledger.py fix wp={wp} k=v")
    log(f"cwd={cwd} sha={sha} wp={wp} ok junior={junior or jerr}({j_ms}ms) "
        f"total_ms={elapsed_ms(t0)}")
    return row


def selftest():
    args = {"cwd": "/Users/shulyugin/Sites/lectella", "commit": "d5c0be27",
            "wp": "gpb2-be", "dry_run": True, "no_junior": True, "junior": False}
    # dry-run appends nothing; never even look at the real ledger
    os.environ.setdefault("PM_LEDGER", "/dev/null")
    import time
    row = handle_commit(args["cwd"], args, time.time())
    fails = []
    if not row:
        fails.append("no row built")
    else:
        if row["wp"] != "gpb2-be":
            fails.append(f"wp={row['wp']!r}")
        if row["risk"] != "high":
            fails.append(f"risk={row['risk']!r}")
        if row["rounds"] != 2:
            fails.append(f"rounds={row['rounds']!r}")
        if row["cycles"] != 1:
            fails.append(f"cycles={row['cycles']!r}")
        if row["gate_first_pass"] != "n":
            fails.append(f"gate_first_pass={row['gate_first_pass']!r}")
        if row["blocking_r1"] != 1:
            fails.append(f"blocking_r1={row['blocking_r1']!r}")
        rev = ",".join(row["reviewers"])
        if "kimi" not in rev or "glm" not in rev:
            fails.append(f"reviewers={rev!r}")
    if fails:
        print("selftest FAILED: " + "; ".join(fails), file=sys.stderr)
        return 1
    print("selftest OK")
    return 0


def main():
    import time
    t0 = time.time()
    args = parse_args(sys.argv[1:])
    if args["selftest"]:
        sys.exit(selftest())
    try:
        raw = sys.stdin.read()
        ev = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return
    if ev.get("tool_name") != "Bash":
        return
    cmd = (ev.get("tool_input") or {}).get("command") or ""
    if not args["commit"] and not is_commit_command(cmd):
        return
    cwd = args["cwd"] or ev.get("cwd") or os.getcwd()
    try:
        handle_commit(cwd, args, t0, tool_output=ev.get("tool_output"))
    except Exception as e:
        log(f"cwd={cwd} error={e!r} total_ms={elapsed_ms(t0)}")


if __name__ == "__main__":
    main()
    sys.exit(0)
