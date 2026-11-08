#!/usr/bin/env bash
# review-board.sh — fan a work package's diff out to independent reviewer agents,
# then collect their JSON verdicts. Part of the playmaker-coach skill.
#
#   review-board.sh <wp-label> <base-ref> [options]     dispatch a review round
#   review-board.sh --collect <wp-label> [--cwd DIR]    gather the verdicts
#   review-board.sh --auto <wp-label> <base-ref> --impl-id <sid> [options]
#                                                        run the whole review loop unattended
#   review-board.sh --adjudicate <wp-label> [--cwd DIR] mechanical adjudication alone (helper)
#
# Options:
#   --risk routine|normal|high|seams   how many reviewers and which lenses (default: normal; seams = joints between WPs)
#   --cwd DIR                    repo/worktree under review (default: $PWD)
#   --paths "a b c"              limit the diff to these pathspecs
#   --spec FILE                  the WP spec: scope + acceptance criteria + done-condition
#                                (default: .playmaker/reviews/<wp>/spec.md)
#   --gate "CMD"                 the acceptance command reviewers must re-run
#   --gate-prepassed <path|->    (passed by --auto) the CLI already ran the gate on the
#                                implementer's session and it passed: reviewers are told NOT
#                                to re-run it. <path> is the gate log, '-' when unrecorded.
#   --impl-agent LANE            lane that implemented the WP; it is excluded from reviewing
#   --round N                    review round number (default: 1). The diff is always against
#                                <base-ref>, so a later round is cumulative unless you commit
#                                round 1 and pass that commit as the base.
#   --no-precheck                skip the precheck step (default: it runs before every board)
#   --precheck-only              run only the precheck and exit with its status (2 = fail)
#   --dry-run                    print the prompts and the dispatches, run nothing
#
# Before the board is dispatched a PRECHECK runs: one cheap lane checks the spec's
# ## ACCEPTANCE section against the diff (see references/review-board.md, «Precheck»).
# A fail exits 2 with a numbered fix list and no reviewer is dispatched; when no cheap
# lane answers the script warns and dispatches the board anyway.
#
# Reviewer roster comes from the first of these that exists:
#   ./.playmaker/reviewers.conf     ~/.playmaker/reviewers.conf
# Format — one reviewer per line, comments with '#':
#   <risk> <lane> <model|-> <lens> [<effort>]
# The optional fifth field is the seat's per-dispatch reasoning effort
# (low|medium|high|xhigh|max|ultra; absent or '-' = none), passed as
# `playmaker dispatch --effort <value>`; a '#' starts a trailing comment.
# e.g.
#   normal  agy      gemini-3.1-pro-high        correctness
#   normal  opencode zai-coding-plan/glm-5.3    contracts
#   high    agy      gemini-3.1-pro-high        correctness
#   high    codex    -                          risk
#   high    codex    gpt-6-astra                risk ultra
#   high    kimi     kimi-code/k3-256k          contracts
# Lines are an ordered preference list: after skipping --impl-agent the script seats the first N
# (routine 1, normal 2, high 3, seams 1) and holds the rest in reserve — keep count+1 lines per class
# so the skip still fills the board. Fewer than N eligible lines → the script stops
# (PM_REVIEW_ALLOW_SHORT=1 overrides, and board.md must say why).
# A `precheck` risk-class keyword holds the cheap lanes for the precheck — an ordered preference
# list, first lane to answer wins, e.g.
#   precheck opencode zai-coding-plan/glm-5.3-flash     precheck
#   precheck agy      gemini-3.8-flash-low              precheck
# Model names must be copied from `agy models` / `opencode models`, never typed from memory.
#
# ── --auto: the whole review loop without the coach ───────────────────────────
#
#   review-board.sh --auto <wp-label> <base-ref> --impl-id <session> [options]
#
#     --impl-id <session>     the implementer's playmaker session (a prefix is fine; every fix
#                              follow-up continues its thread)
#     --risk R                as in the one-round mode (default: normal)
#     --gate "CMD"            the acceptance gate; re-attached to every fix follow-up so the
#                              CLI re-runs it and the row carries gate_status
#     --impl-agent LANE       lane that implemented the WP (default: the --impl-id lane)
#     --paths "a b"           REQUIRED: the WP's pathspecs — diffs, git add -N and the wip
#                              commits cover these ONLY (a shared worktree is never swept)
#     --package-base REF       precheck the whole WP from REF; REF must be an ancestor of
#                              <base-ref> and is persisted in the WP's board.env
#     --max-cycles N          fix-list budget, default 2 (config [board] max_cycles)
#     --seat-deadline 20m     the COMMON per-board wait window, default 20m (config
#                              [board] seat_deadline; 1200, "1200s", "20m", "2h"): counted
#                              from the board dispatch; every seat — and a substitute —
#                              waits only the time remaining in that window
#     --cwd DIR / --dry-run   as elsewhere; --dry-run prints the plan and runs nothing
#
# The loop, in the foreground (the coach backgrounds it), every step appended (timestamped) to
# .playmaker/reviews/<wp>/auto.log:
#   1. wait the implementer (playmaker get --wait). failed/killed or no_changes on a write task
#      → exit 4 (reroute). gate_status fail/timeout is handled whether or not --gate was
#      passed — the gate command comes from --gate, else from the implementer session's own
#      gate_cmd, else exit 4 «implementer gate failed and no gate command is known»:
#      the gate log tail goes back as a fix list via `playmaker continue --after --gate
#      --expect-changes`; that consumes a cycle and loops. A failed gate never reaches the board.
#   2. precheck (as above) — always against the WHOLE package (the original base, frozen
#      as a hash): acceptance items describe the WP, not the round's delta; a delta-based
#      precheck would mark everything delivered in round 1 as missing on later rounds.
#      Reviewers still get the delta. New files get `git add -N` right before the precheck's
#      diff is built — only after the implementer is done, so a failed implementer never
#      leaves the index dirty. fail → its numbered fix list goes back the same way (a cycle).
#   3. wip snapshot after the precheck passes: `git commit -m "wip: <wp> rN (auto)"` of the WP
#      paths only — squash before landing. Round N diffs the previous wip commit (delta only).
#   4. dispatch the board as today. When the implementer's row carries gate_status=pass the
#      reviewer prompt says the CLI already ran the gate and asks them NOT to re-run it.
#   5. wait every seat in ONE common --seat-deadline window counted from the board
#      dispatch: each seat — and a substitute — gets only the time remaining in that
#      window, so a hung board cannot stretch to N × the deadline. failed/killed/timeout
#      is substituted ONCE from the roster's reserve (first not-yet-used eligible line),
#      else recorded as a missing seat.
#      A [limits] refusal (max_parallel= / min_free_ram_gb=) on any dispatch — seat, precheck
#      or the fix follow-up — is temporary: «waiting for a lane slot», retry every 30 s up to
#      the seat deadline, only then a dead seat. A seat the board could not dispatch at all is
#      substituted the same way (recorded with an empty id in sessions.txt).
#   6. --collect, then mechanical adjudication: blocking findings only; one without file+line+
#      scenario is dropped; one on a file outside the round's diff is dropped (environmental);
#      dedupe by file:line keeping the longer scenario. Zero blocking AND a full board →
#      auto=green cycles=<n> in board.env, summary printed, exit 0.
#   7. otherwise the fix list (numbered `file:line — claim -> suggested_fix` + the standard
#      tail) goes back via continue --after; the follow-up's gate result loops step 1; round N+1
#      reviews the delta.
# Exit codes: 0 green · 3 stopped — cycle budget exhausted with blocking findings left, a board
# that could not fill its seats, or a fix round that changed nothing (escalate per policy) ·
# 4 implementer failed, or a hard dispatch failure (unknown lane, auth, binary) — reroute/fix
# the lane. Config: [board] max_cycles / seat_deadline in
# ~/.playmaker/config.toml or <cwd>/.playmaker/config.toml (repo wins).
# --adjudicate <wp> runs step 6's mechanical pass alone on whatever verdict-*.json and the
# newest diff-r*.patch sit in the review dir, and prints the fix list — a helper for demos.

set -euo pipefail

die() { printf '%s\n' "error: $*" >&2; exit 1; }
note() { printf '%s\n' "$*" >&2; }

# Parse and validate the entire roster before any caller selects rows. Keep this
# function identical in review-board.sh and review-decision.sh. Output records:
# kind|lane|model|lens|effort|line-number (effort is empty when absent or '-').
parse_roster() {
  local file="$1" line lineno=0 token effort
  local -a tokens fields
  while IFS= read -r line || [[ -n "$line" ]]; do
    lineno=$((lineno + 1))
    IFS=$' \t\n' read -r -a tokens <<< "$line"
    fields=()
    for token in ${tokens[@]+"${tokens[@]}"}; do  # bash 3.2: an empty array is unbound under set -u
      [[ "$token" == \#* ]] && break
      fields+=("$token")
    done
    # Blank/comment lines and incomplete rows cannot seat a reviewer.
    [[ ${#fields[@]} -ge 4 ]] || continue
    effort="${fields[4]:-}"
    case "$effort" in
      ''|-) effort="" ;;
      low|medium|high|xhigh|max|ultra) ;;
      *) die "bad effort '$effort' ($file line $lineno): $line" ;;
    esac
    if [[ ${#fields[@]} -gt 5 ]]; then
      die "unexpected field '${fields[5]}' after effort ($file line $lineno): $line"
    fi
    printf '%s|%s|%s|%s|%s|%s\n' "${fields[0]}" "${fields[1]}" "${fields[2]}" "${fields[3]}" "$effort" "$lineno"
  done < "$file"
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  sed -n '2,/^set -euo pipefail$/p' "$0" | sed '$d; s/^# \{0,1\}//'
  exit 0
fi

# run_timed SECS CMD… — run CMD under a hard wall-clock cap (SECONDS), stderr merged into
# stdout, returning its exit code (124 = timed out). Uses `timeout` where installed,
# python3 otherwise (macOS ships no timeout).
run_timed() {
  local secs="$1"; shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$secs" "$@" 2>&1
  else
    python3 - "$secs" "$@" <<'PY'
import subprocess, sys
try:
    r = subprocess.run(sys.argv[2:], capture_output=True, text=True, timeout=int(sys.argv[1]))
except subprocess.TimeoutExpired:
    print(f"timed out after {sys.argv[1]}s", file=sys.stderr)
    sys.exit(124)
sys.stdout.write(r.stdout or "")
sys.stderr.write(r.stderr or "")
sys.exit(r.returncode)
PY
  fi
}

command -v playmaker >/dev/null 2>&1 || die "playmaker is not on PATH"

# row_field ROW-JSON FIELD — echo one field of a playmaker session row ("" when absent or the
# row is not JSON). Single-line scalar fields only (status, gate_status, id, …).
row_field() {
  local v=""
  v=$(printf '%s' "$1" | python3 -c 'import json,sys
try:
    d = json.loads(sys.stdin.read())
except ValueError:
    sys.exit(1)
v = d.get(sys.argv[1])
if isinstance(v, bool):
    v = int(v)
print("" if v is None else v)' "$2" 2>/dev/null) || v=""
  printf '%s\n' "$v"
}

# dur_secs "20m"|"1200s"|"2h"|1200 → seconds
dur_secs() {
  local v="$1"
  case "$v" in
    *[!0-9smh]*) die "bad duration: $v" ;;
    *h) echo $(( ${v%h} * 3600 )) ;;
    *m) echo $(( ${v%m} * 60 )) ;;
    *s) echo "${v%s}" ;;
    ''|*[!0-9]*) die "bad duration: $v" ;;
    *) echo "$v" ;;
  esac
}

# pm_note — pm_run_limits logs its retry lines through this; the --auto block redefines
# it to alog so they are also appended to auto.log
pm_note() { note "$@"; }

# pm_run_limits SECS CMD… — run a playmaker dispatch/continue. A [limits] refusal
# (max_parallel= / min_free_ram_gb= in the output) is temporary: log «waiting for a lane
# slot», sleep 30 s and retry, up to SECS total. Success → the command's stdout, rc 0.
# Failure → stdout+stderr combined, rc 200 when it was limits-refusal until the deadline
# (the caller treats that as a dead seat), otherwise the command's own exit code (hard
# failure: unknown lane, auth, binary — the caller stops with the reason).
pm_run_limits() {
  local secs="$1"; shift
  local rc=0 out="" mixed="" first="" tmpf
  local deadline=$(( $(date +%s) + secs ))
  tmpf=$(mktemp "${TMPDIR:-/tmp}/pm-run.XXXXXX")
  while :; do
    out=$("$@" 2>"$tmpf") && rc=0 || rc=$?
    if [[ $rc -eq 0 ]]; then
      rm -f "$tmpf"
      printf '%s' "$out"
      return 0
    fi
    mixed="$out"
    if [[ -s "$tmpf" ]]; then
      if [[ -n "$mixed" ]]; then
        mixed="$mixed
$(cat "$tmpf")"
      else
        mixed="$(cat "$tmpf")"
      fi
    fi
    if printf '%s' "$mixed" | grep -q -E 'max_parallel=|min_free_ram_gb='; then
      first=$(printf '%s\n' "$mixed" | grep -m1 -E 'max_parallel=|min_free_ram_gb=')
      if [[ $(date +%s) -lt $deadline ]]; then
        pm_note "· [limits] ${first:0:120} — waiting for a lane slot (retry in 30s)"
        sleep 30
        continue
      fi
      printf '%s' "$mixed"
      rm -f "$tmpf"
      return 200
    fi
    printf '%s' "$mixed"
    rm -f "$tmpf"
    return "$rc"
  done
}

# ── collect mode ──────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--collect" ]]; then
  wp="${2:-}"; [[ -n "$wp" ]] || die "usage: review-board.sh --collect <wp-label> [--cwd DIR]"
  shift 2
  cwd="$PWD"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --cwd) cwd="$2"; shift 2 ;;
      *) die "unknown option: $1" ;;
    esac
  done
  dir="$cwd/.playmaker/reviews/$wp"
  [[ -f "$dir/sessions.txt" ]] || die "no dispatched round found at $dir/sessions.txt"
  pending=0; dead=0; got=0
  while IFS='|' read -r id lane model lens; do
    [[ -n "${id:-}" ]] || continue
    status=$(playmaker get "$id" --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))' 2>/dev/null || echo "")
    if [[ "$status" == "failed" || "$status" == "killed" ]]; then
      note "! $lane/$lens ($id): $status — DEAD (quota, auth or crash: playmaker logs $id). Seat a substitute: dispatch prompt-$lane-$lens.md on another lane --read-only and append '<id>|<lane>|<model>|$lens' to sessions.txt"
      dead=$((dead + 1))
      continue
    fi
    if [[ "$status" != "done" && "$status" != "no_changes" ]]; then
      note "· $lane/$lens ($id): $status — not ready"
      pending=$((pending + 1))
      continue
    fi
    out=$(playmaker get "$id" --json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("output") or d.get("final") or "")') \
      || { note "! $lane/$lens ($id): could not read the session output — retry --collect"; continue; }
    # Parse into a temp file and only then publish it: a reviewer that answers in prose must not
    # leave a zero-byte verdict-*.json behind, or every later --collect chokes on it.
    tmp="$dir/.verdict-$lane-$lens.partial"
    # tolerate a fenced or prose-wrapped object: take the LAST object that parses AND carries the
    # contract — a reasoning block or a quoted example earlier in the text must not win
    printf '%s' "$out" | python3 -c '
import json, sys
raw = sys.stdin.read()
dec = json.JSONDecoder()
found = None
for j in range(len(raw) - 1, -1, -1):
    if raw[j] != "{":
        continue
    try:
        obj, _ = dec.raw_decode(raw[j:])
    except json.JSONDecodeError:
        continue
    if isinstance(obj, dict) and "verdict" in obj and "findings" in obj:
        found = obj
        break
if found is None:
    sys.exit(1)
json.dump(found, sys.stdout, indent=2, ensure_ascii=False)
' > "$tmp" 2>/dev/null \
      || { rm -f "$tmp"; note "! $lane/$lens ($id): did not return the JSON contract — re-prompt once, then drop"; continue; }
    mv "$tmp" "$dir/verdict-$lane-$lens.json"
    note "✓ $dir/verdict-$lane-$lens.json"
    got=$((got + 1))
  done < "$dir/sessions.txt"
  [[ $pending -eq 0 ]] || note "$pending reviewer(s) still running"
  required=""; [[ -f "$dir/board.env" ]] && required=$(sed -n 's/^required=//p' "$dir/board.env")
  if [[ -n "$required" ]]; then
    if [[ $got -lt $required ]]; then
      note "!! BOARD INCOMPLETE: $got of $required verdicts (dead: $dead, pending: $pending) — not a green board until the count is met or the shortfall is recorded in board.md"
    else
      note "verdicts: $got/$required"
    fi
  fi
  ls "$dir"/verdict-*.json >/dev/null 2>&1 && {
    note ""
    note "blocking findings:"
    python3 - "$dir" <<'PY' >&2
import glob, json, os, sys
d = sys.argv[1]
rows = []
patches = sorted(glob.glob(os.path.join(d, "diff-r*.patch")), key=os.path.getmtime)
cutoff = os.path.getmtime(patches[-1]) if patches else 0
for path in sorted(glob.glob(os.path.join(d, "verdict-*.json"))):
    if os.path.getmtime(path) < cutoff:
        print(f"  [stale] {os.path.basename(path)} predates the current patch — ignored (older rounds live in archive-*/)")
        continue
    try:
        with open(path) as fh:
            v = json.load(fh)
    except (OSError, json.JSONDecodeError):
        print(f"  [!] {os.path.basename(path)} is not readable JSON — ignored")
        continue
    for f in v.get("findings", []):
        if f.get("severity") == "blocking":
            rows.append((v.get("reviewer", os.path.basename(path)), f))
if not rows:
    print("  none")
for reviewer, f in rows:
    print(f"  [{reviewer}] {f.get('file')}:{f.get('line')} — {f.get('claim')}")
    print(f"      scenario: {f.get('scenario')}  (confidence: {f.get('confidence')})")
PY
  }
  exit 0
fi

# ── mechanical adjudication (shared by --auto and --adjudicate) ───────────────
# adj_json DIR [PATCH] — one JSON object on stdout:
#   blocking_count / blocking[] (deduped, evidence-checked, in-diff only), the dropped lists,
#   the non-blocking leftovers. PATCH empty → the newest diff-r*.patch in DIR wins.
adj_json() {
  python3 - "$1" "${2:-}" <<'PY'
import glob, json, os, re, sys

d, patch = sys.argv[1], sys.argv[2]
if not patch:
    patches = sorted(glob.glob(os.path.join(d, "diff-r*.patch")), key=os.path.getmtime)
    if not patches:
        sys.stderr.write("no diff-r*.patch under %s\n" % d)
        sys.exit(1)
    patch = patches[-1]

files = set()
with open(patch, encoding="utf-8", errors="replace") as fh:
    for line in fh:
        m = re.match(r"^\+\+\+ b/(.+)$", line.rstrip("\n"))
        if m:
            files.add(m.group(1).strip())
        m = re.match(r"^--- a/(.+)$", line.rstrip("\n"))
        if m:
            files.add(m.group(1).strip())

def norm(p):
    p = str(p or "").strip()
    return p[2:] if p.startswith("./") else p

blocking, noev, envdrop, leftovers = [], [], [], []
verdicts = 0
for path in sorted(glob.glob(os.path.join(d, "verdict-*.json"))):
    try:
        with open(path, encoding="utf-8") as fh:
            v = json.load(fh)
    except (OSError, ValueError):
        continue
    if not isinstance(v, dict):
        continue
    verdicts += 1
    rev = str(v.get("reviewer") or os.path.basename(path))
    for f in v.get("findings") or []:
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity") or "")
        e = {
            "file": norm(f.get("file")),
            "line": f.get("line"),
            "claim": str(f.get("claim") or "").strip(),
            "scenario": str(f.get("scenario") or "").strip(),
            "fix": str(f.get("suggested_fix") or "").strip(),
            "reviewer": rev,
        }
        if sev == "blocking":
            if not e["file"] or isinstance(e["line"], bool) or not isinstance(e["line"], int) or not e["scenario"]:
                noev.append("%s:%s — %s [%s]" % (e["file"] or "?", e["line"], e["claim"], rev))
                continue
            if e["file"] not in files:
                envdrop.append("%s:%s — %s [%s]" % (e["file"], e["line"], e["claim"], rev))
                continue
            blocking.append(e)
        elif sev in ("major", "minor"):
            leftovers.append("%s:%s — %s (%s) [%s]" % (e["file"], e["line"], e["claim"], sev, rev))

merged, dups = {}, []
for e in blocking:
    k = (e["file"], e["line"])
    if k not in merged:
        keep = dict(e)
        keep["reviewers"] = [e["reviewer"]]
        merged[k] = keep
        continue
    keep = merged[k]
    keep["reviewers"] = keep["reviewers"] + [e["reviewer"]]
    if len(e["scenario"]) > len(keep["scenario"]):
        dups.append("%s:%s — %s [%s]" % (keep["file"], keep["line"], keep["claim"], keep["reviewer"]))
        new = dict(e)
        new["reviewers"] = keep["reviewers"]
        merged[k] = new
    else:
        dups.append("%s:%s — %s [%s]" % (e["file"], e["line"], e["claim"], e["reviewer"]))

final = [merged[k] for k in sorted(merged.keys(), key=lambda t: (t[0], t[1]))]
json.dump(
    {
        "patch": os.path.basename(patch),
        "verdicts": verdicts,
        "diff_files": sorted(files),
        "blocking_count": len(final),
        "blocking": [
            {
                "file": e["file"],
                "line": e["line"],
                "claim": e["claim"],
                "scenario": e["scenario"],
                "suggested_fix": e["fix"],
                "reviewers": ",".join(e["reviewers"]),
            }
            for e in final
        ],
        "dropped_no_evidence": noev,
        "dropped_environmental": envdrop,
        "dropped_duplicate": dups,
        "leftovers": sorted(leftovers, key=lambda s: (0 if "(major)" in s else 1, s)),
    },
    sys.stdout,
    indent=2,
    ensure_ascii=False,
)
PY
}

# fixlist_from_adj ADJ-JSON ROUND GATE — the coach-format fix list on stdout
fixlist_from_adj() {
  python3 - "$1" "$2" "$3" <<'PY'
import json, sys
adj = json.loads(sys.argv[1])
rnd, gate = sys.argv[2], sys.argv[3]
items = adj.get("blocking") or []
L = ["Review round %s found %d blocking issue(s). Fix exactly these, nothing else:" % (rnd, len(items)), ""]
for i, e in enumerate(items, 1):
    fix = e.get("suggested_fix") or e.get("scenario") or "see scenario"
    L.append("%d. %s:%s — %s -> %s" % (i, e["file"], e["line"], e["claim"], fix))
L.append("")
if gate:
    L.append("Do not refactor anything not listed. Re-run this gate until it exits 0 and paste the output: %s" % gate)
else:
    L.append("Do not refactor anything not listed. Re-run your acceptance checks until they pass and paste the output.")
L.append("Reply with a one-line note per item saying what you changed.")
print("\n".join(L))
PY
}

# ── adjudicate mode — the mechanical pass alone (helper/demos) ────────────────
if [[ "${1:-}" == "--adjudicate" ]]; then
  wp="${2:-}"; [[ -n "$wp" ]] || die "usage: review-board.sh --adjudicate <wp-label> [--cwd DIR]"
  shift 2
  cwd="$PWD"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --cwd) cwd="$2"; shift 2 ;;
      *) die "unknown option: $1" ;;
    esac
  done
  dir="$cwd/.playmaker/reviews/$wp"
  adj=$(adj_json "$dir" "") || die "nothing to adjudicate under $dir"
  python3 - "$adj" <<'PY'
import json, sys
adj = json.loads(sys.argv[1])
print("adjudication over %s (%d verdict file(s)):" % (adj.get("patch", "?"), adj.get("verdicts", 0)))
print("  blocking after the mechanical pass: %d" % adj.get("blocking_count", 0))
print("  dropped: %d without evidence, %d environmental, %d duplicates" % (
    len(adj.get("dropped_no_evidence") or []),
    len(adj.get("dropped_environmental") or []),
    len(adj.get("dropped_duplicate") or [])))
for tag, lst in (("no evidence", "dropped_no_evidence"), ("environmental", "dropped_environmental"), ("duplicate", "dropped_duplicate")):
    for e in adj.get(lst) or []:
        print("    [%s] %s" % (tag, e))
lo = adj.get("leftovers") or []
if lo:
    print("  non-blocking leftovers:")
    for l in lo:
        print("    - %s" % l)
else:
    print("  non-blocking leftovers: none")
print("")
if adj.get("blocking_count", 0) == 0:
    print("GREEN — zero blocking findings.")
else:
    print("fix list:")
    for i, e in enumerate(adj.get("blocking") or [], 1):
        fix = e.get("suggested_fix") or e.get("scenario") or "see scenario"
        print("%d. %s:%s — %s -> %s" % (i, e["file"], e["line"], e["claim"], fix))
PY
  exit 0
fi

# ── auto mode: the whole review loop unattended ───────────────────────────────
if [[ "${1:-}" == "--auto" ]]; then
  wp="${2:-}"; base="${3:-}"
  [[ -n "$wp" && -n "$base" ]] || die "usage: review-board.sh --auto <wp-label> <base-ref> --impl-id <session> [options]"
  shift 3
  impl_id=""; risk=normal; cwd="$PWD"; paths=""; gate=""; impl_agent=""; package_base_arg=""
  max_cycles=""; seat_deadline_arg=""; dry=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --impl-id) impl_id="$2"; shift 2 ;;
      --risk) risk="$2"; shift 2 ;;
      --cwd) cwd="$2"; shift 2 ;;
      --paths) paths="$2"; shift 2 ;;
      --package-base) package_base_arg="$2"; shift 2 ;;
      --gate) gate="$2"; shift 2 ;;
      --impl-agent) impl_agent="$2"; shift 2 ;;
      --max-cycles) max_cycles="$2"; shift 2 ;;
      --seat-deadline) seat_deadline_arg="$2"; shift 2 ;;
      --dry-run) dry=1; shift ;;
      *) die "unknown option: $1" ;;
    esac
  done
  case "$risk" in routine|normal|high|seams) ;; *) die "--risk must be routine, normal, high or seams" ;; esac
  [[ -n "$impl_id" ]] || die "--auto needs --impl-id <session> — the implementer's playmaker session"
  conf=""
  for c in "$cwd/.playmaker/reviewers.conf" "$HOME/.playmaker/reviewers.conf"; do
    if [[ -f "$c" ]]; then conf="$c"; break; fi
  done
  roster_rows=""
  # Capture only a fully validated roster; never hide parser failure in < <(...).
  if [[ -n "$conf" ]]; then roster_rows=$(parse_roster "$conf") || exit $?; fi

  dir="$cwd/.playmaker/reviews/$wp"
  mkdir -p "$dir"
  log="$dir/auto.log"
  alog() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$log" >&2; }
  pm_note() { alog "$@"; }   # pm_run_limits retries land in auto.log too

  spec="$dir/spec.md"
  # --paths is optional: without it the WP's files come from the spec's «## SCOPE» bullets
  # (`- path …`), so wip commits and git add -N still cover the WP ONLY — never everything a
  # shared worktree happens to hold. No --paths and no SCOPE paths → refuse.
  if [[ -z "$paths" && -f "$spec" ]]; then
    paths=$(awk '/^## SCOPE/{f=1;next} /^## /{f=0} f && /^- /{print $2}' "$spec" | tr -d '`' | grep -v '^$' | tr '\n' ' ' | sed 's/ *$//')
    [[ -n "$paths" ]] && note "--paths not given — taken from the spec's SCOPE: $paths"
  fi
  [[ -n "$paths" ]] || die "--auto needs the WP's paths: pass --paths \"<pathspecs>\" or list them as \`- path\` bullets under «## SCOPE» in $spec"
  [[ -f "$spec" ]] || die "no WP spec at $spec — write scope, acceptance criteria and the done-condition there first (reviewers cannot refute what was never specified)"

  # config defaults: [board] max_cycles / seat_deadline — repo .playmaker/config.toml wins over ~/.playmaker
  cfg=$(python3 - "$cwd" <<'PY'
import os, re, sys

def read(p):
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""

text = read(os.path.expanduser("~/.playmaker/config.toml")) + "\n" + read(os.path.join(sys.argv[1], ".playmaker", "config.toml"))
mc = sd = None
sec = None
for line in text.splitlines():
    s = line.strip()
    m = re.match(r"^\[([A-Za-z0-9_.-]+)\]$", s)
    if m:
        sec = m.group(1)
        continue
    if sec != "board":
        continue
    m = re.match(r"^([A-Za-z0-9_]+)\s*=\s*(.+?)\s*(?:#.*)?$", s)
    if not m:
        continue
    k, v = m.group(1), m.group(2).strip().strip("'\"")
    if k == "max_cycles":
        try:
            mc = int(v)
        except ValueError:
            pass
    elif k == "seat_deadline":
        m2 = re.match(r"^(\d+(?:\.\d+)?)\s*([smh]?)$", v)
        if m2:
            sd = int(float(m2.group(1)) * {"s": 1, "m": 60, "h": 3600, "": 1}[m2.group(2)])
print("%d %d" % (mc if mc is not None else 2, sd if sd is not None else 1200))
PY
) || cfg="2 1200"
  read -r cfg_mc cfg_sd <<< "$cfg"
  if [[ -z "$max_cycles" ]]; then max_cycles=$cfg_mc; fi
  if [[ -z "$seat_deadline_arg" ]]; then seat_deadline_arg="${cfg_sd}s"; fi
  seat_deadline=$(dur_secs "$seat_deadline_arg")
  export PM_SEAT_DEADLINE="$seat_deadline"   # the sub-runs' [limits] retries use the same cap

  prow=$(playmaker get "$impl_id" --json 2>/dev/null) || die "cannot read session $impl_id — playmaker get failed"
  impl_sid=$(row_field "$prow" id); impl_sid="${impl_sid:-$impl_id}"
  impl_lane=$(row_field "$prow" agent)
  impl_status0=$(row_field "$prow" status)
  if [[ -z "$impl_agent" ]]; then impl_agent="$impl_lane"; fi

  # Freeze the run's base before resolving the package base. A symbolic base such as HEAD
  # must not move when this auto run later creates its wip commit.
  base_ref=$(git -C "$cwd" rev-parse --verify "$base^{commit}" 2>/dev/null) \
    || die "bad base ref: $base — rev-parse found no commit"

  # The precheck sees the explicit paths plus every path named by the spec's SCOPE. Keep
  # first occurrence order; the wip commit and board dispatch continue to use $paths only.
  scope_paths=$(awk '/^## SCOPE/{f=1;next} /^## /{f=0} f && /^- /{print $2}' "$spec" | tr -d '`' | grep -v '^$' || true)
  precheck_paths=$(printf '%s\n%s\n' "${paths// /$'\n'}" "$scope_paths" | awk 'NF && !seen[$0]++' | tr '\n' ' ' | sed 's/ *$//')

  # run lines go to auto.log too; a dry run writes nothing, so it only prints them
  plog() { if [[ $dry -eq 1 ]]; then note "$@"; else alog "$@"; fi; }
  envf="$dir/board.env"
  package_base_source="this run"
  package_base="$base_ref"
  package_base_would_write=0
  recorded_package_base=""
  [[ -f "$envf" ]] && recorded_package_base=$(sed -n 's/^package_base=//p' "$envf" | tail -1)
  if [[ -n "$package_base_arg" ]]; then
    package_base=$(git -C "$cwd" rev-parse --verify "$package_base_arg^{commit}" 2>/dev/null) \
      || die "--package-base $package_base_arg does not resolve to a commit"
    git -C "$cwd" merge-base --is-ancestor "$package_base" "$base_ref" 2>/dev/null \
      || die "--package-base $package_base_arg ($package_base) must be an ancestor of run base $base_ref"
    package_base_source="--package-base"
    package_base_would_write=1
  elif [[ -n "$recorded_package_base" ]] && git -C "$cwd" merge-base --is-ancestor "$recorded_package_base" "$base_ref" 2>/dev/null; then
    package_base=$(git -C "$cwd" rev-parse --verify "$recorded_package_base^{commit}")
    package_base_source="board.env"
  else
    package_base_would_write=1
    if [[ -n "$recorded_package_base" ]]; then
      plog "package_base in board.env is not an ancestor of run base; reset to $base_ref"
    fi
  fi
  plog "precheck base: $package_base (source: $package_base_source)"
  if [[ "$precheck_paths" != "$paths" ]]; then
    plog "precheck paths: $precheck_paths"
  fi
  if [[ $package_base_would_write -eq 1 ]]; then
    if [[ $dry -eq 1 ]]; then
      note "would write package_base=$package_base to board.env"
    else
      touch "$envf"
      grep -v '^package_base=' "$envf" >"$envf.tmp" 2>/dev/null || true
      mv "$envf.tmp" "$envf"
      printf 'package_base=%s\n' "$package_base" >>"$envf"
    fi
  fi

  roster_preview() { # "kind|lane|model|lens|effort" per line: first N seated, the rest reserve
    local r lane model lens effort lineno kind need seated=0
    case "$risk" in routine|seams) need=1 ;; high) need=3 ;; *) need=2 ;; esac
    while IFS='|' read -r r lane model lens effort lineno; do
      [[ "$r" == "$risk" ]] || continue
      [[ -n "$impl_agent" && "$lane" == "$impl_agent" ]] && continue
      kind=reserve
      if [[ $seated -lt $need ]]; then kind=seat; seated=$((seated + 1)); fi
      printf '%s|%s|%s|%s|%s\n' "$kind" "$lane" "$model" "$lens" "$effort"
    done <<< "$roster_rows"
  }

  if [[ $dry -eq 1 ]]; then
    note ""
    note "── auto dry-run: $wp"
    note "  base: $base   risk: $risk   cwd: $cwd"
    note "  paths (required): $paths"
    note "  precheck paths: $precheck_paths"
    note "  gate: ${gate:--}"
    note "  spec: $spec"
    note "  implementer: ${impl_sid:0:8} lane=$impl_lane status=$impl_status0"
    note "  steps: wait implementer → CLI gate check (fail → gate-log fix list) → precheck"
    note "         (fail → fix list) → wip commit r1 → dispatch board → wait every seat in one"
    note "         common ${seat_deadline}s window from dispatch (one substitution per dead"
    note "         seat, only the remaining time) → collect → adjudicate"
    note "         → fix list via 'playmaker continue --after --gate --expect-changes' → delta round"
    note "  cycle budget: $max_cycles fix lists (gate + precheck + board share it)"
    note "  seat deadline: ${seat_deadline}s per seat"
    note "  limits: a [limits] refusal (max_parallel / min_free_ram_gb) on any dispatch waits for"
    note "         a lane slot and retries every 30s up to the seat deadline, then the seat is dead"
    note "         (substituted once); a hard dispatch failure (unknown lane, auth, binary) exits 4"
    if [[ -n "$conf" ]]; then
      roster_preview | while IFS='|' read -r kind lane model lens effort; do
        if [[ -n "$effort" ]]; then
          note "  $kind: $lane ${model:--} / $lens · effort $effort"
        else
          note "  $kind: $lane ${model:--} / $lens"
        fi
      done
      pc_line=""
      while IFS='|' read -r kind lane model lens effort lineno; do
        [[ "$kind" == precheck ]] || continue
        pc_line="$kind $lane $model $lens"
        [[ -n "$effort" ]] && pc_line="$pc_line · effort $effort"
        break
      done <<< "$roster_rows"
      note "  precheck lane: ${pc_line:-(none in the roster — the board runs without a precheck)}"
    else
      note "  !! no reviewers.conf — the board cannot be seated"
    fi
    note "  fix-list format: numbered 'file:line — claim -> suggested_fix' + the standard tail"
    note "nothing dispatched — dry run"
    exit 0
  fi

  impl_gate_status=""; impl_gate_log=""
  round=1; cycles=0; cyc_gate=0; cyc_pre=0; cyc_board=0
  seats_dispatched=0; subst_count=0; missing_seats=0
  drop_noev=0; drop_env=0; drop_dup=0
  prev_wip=""; adj=""; fixf_last=""

  write_env() { # $1 — the auto=… line, replacing any older one
    local envf="$dir/board.env"
    touch "$envf"
    grep -v '^auto=' "$envf" > "$envf.tmp" 2>/dev/null || true
    mv "$envf.tmp" "$envf"
    printf '%s\n' "$1" >> "$envf"
  }

  summary() { # $1 — the result line
    note ""
    note "═══ auto summary: $wp"
    note "  $1"
    note "  rounds: $round   cycles: $cycles/$max_cycles (gate $cyc_gate, precheck $cyc_pre, board $cyc_board)"
    note "  seats: $seats_dispatched dispatched, $subst_count substituted, $missing_seats missing"
    note "  dropped findings: $drop_noev without evidence, $drop_env environmental, $drop_dup duplicates"
    if [[ -n "$adj" ]]; then
      note "  non-blocking leftovers (last round's verdicts):"
      leftovers=$(printf '%s' "$adj" | python3 -c 'import json,sys
try:
    d = json.loads(sys.stdin.read())
except ValueError:
    d = {}
for l in d.get("leftovers") or []:
    print(l)' 2>/dev/null) || leftovers=""
      if [[ -n "$leftovers" ]]; then
        while IFS= read -r l; do note "    - $l"; done <<< "$leftovers"
      else
        note "    none"
      fi
    fi
    if [[ -n "$fixf_last" ]]; then note "  last fix list: $fixf_last"; fi
    note "  log: $log"
  }

  stop3() { # $1 reason — stop, the coach escalates
    write_env "auto=stop cycles=$cycles"
    alog "!! STOP: $1"
    summary "STOP — $1"
    exit 3
  }

  impl_exit4() { # $1 reason — reroute the implementer
    write_env "auto=stop cycles=$cycles"
    alog "!! IMPLEMENTER FAILED: $1"
    summary "IMPLEMENTER FAILED — $1 (reroute the WP)"
    exit 4
  }

  settle_gate() { # $1 row json — wait for gate_status once the CLI has a gate_cmd but no result yet
    local row="$1" has gc deadline
    has=$(row_field "$row" gate_cmd); gc=$(row_field "$row" gate_status)
    if [[ -n "$has" && -z "$gc" ]]; then
      alog "gate running on ${impl_sid:0:8} — waiting for gate_status (cap ${seat_deadline}s)"
      deadline=$(( $(date +%s) + seat_deadline ))
      while [[ $(date +%s) -lt $deadline ]]; do
        sleep 10
        row=$(playmaker get "$impl_sid" --json 2>/dev/null) || continue
        gc=$(row_field "$row" gate_status)
        if [[ -n "$gc" ]]; then break; fi
      done
    fi
    printf '%s' "$row"
  }

  wait_seat() { # $1 session id, $2 seconds remaining in the board window → done|no_changes|failed|killed|timeout|error
    local out rc
    local remaining="$2"
    if ! [[ "$remaining" -gt 0 ]]; then printf 'timeout\n'; return 0; fi
    out=$(run_timed "$remaining" playmaker get "$1" --wait --json 2>/dev/null) && rc=0 || rc=$?
    if [[ $rc -eq 124 ]]; then printf 'timeout\n'; return 0; fi
    if [[ $rc -ne 0 ]]; then printf 'error\n'; return 0; fi
    printf '%s\n' "$(row_field "$out" status)"
  }

  reserve_pick() { # "lane|model|effort" of the first not-yet-used eligible roster line, or nothing
    local r lane model lens effort lineno sid slane rest used="|"
    if [[ -f "$dir/sessions.txt" ]]; then
      while IFS='|' read -r sid slane rest; do
        [[ -n "$slane" ]] && used="$used$slane|"
      done < "$dir/sessions.txt"
    fi
    while IFS='|' read -r r lane model lens effort lineno; do
      [[ "$r" == "$risk" ]] || continue
      [[ -n "$impl_agent" && "$lane" == "$impl_agent" ]] && continue
      [[ "$used" == *"|$lane|"* ]] && continue
      printf '%s|%s|%s\n' "$lane" "$model" "$effort"
      break
    done <<< "$roster_rows"
  }

  substitute_seat() { # $1 dead lane, $2 lens, $3 board deadline (epoch) — one substitution
    local reserve rlane rmodel reffort pf out nid st2 first sub_rc remaining remaining2
    local -a sub_cmd
    remaining=$(( $3 - $(date +%s) ))
    if ! [[ "$remaining" -gt 0 ]]; then
      missing_seats=$((missing_seats + 1))
      alog "round $round: !! no time left in the board window — seat $1/$2 recorded MISSING"
      return 0
    fi
    reserve=""
    if [[ -n "$conf" ]]; then
      reserve=$(reserve_pick) || die "cannot pick a reserve from $conf (see the error above)"
    fi
    if [[ -z "$reserve" ]]; then
      missing_seats=$((missing_seats + 1))
      alog "round $round: !! no reserve line left — seat $1/$2 recorded MISSING"
      return 0
    fi
    IFS='|' read -r rlane rmodel reffort <<< "$reserve"
    pf="$dir/prompt-$1-$2.md"
    if [[ ! -f "$pf" ]]; then
      missing_seats=$((missing_seats + 1))
      alog "round $round: !! no prompt file for $1/$2 — seat MISSING"
      return 0
    fi
    alog "round $round: substituting $1 → $rlane (reserve, same lens $2, ${remaining}s left in the window)"
    sub_cmd=(playmaker dispatch "$rlane" --json --batch "review-$wp-r$round" --read-only --cwd "$cwd" --prompt "$(cat "$pf")")
    if [[ "$rmodel" != "-" && -n "$rmodel" ]]; then sub_cmd+=(--model "$rmodel"); fi
    if [[ -n "$reffort" ]]; then sub_cmd+=(--effort "$reffort"); fi
    sub_rc=0
    out=$(pm_run_limits "$remaining" "${sub_cmd[@]}") || sub_rc=$?
    if [[ $sub_rc -eq 200 ]]; then
      missing_seats=$((missing_seats + 1))
      alog "round $round: !! substitute $rlane: no lane slot within the board window — seat MISSING"
      return 0
    fi
    if [[ $sub_rc -ne 0 ]]; then
      first=$(printf '%s\n' "$out" | head -1)
      impl_exit4 "substitute dispatch on $rlane failed (exit $sub_rc): ${first:0:160}"
    fi
    nid=$(row_field "$out" session_id)
    if [[ -z "$nid" ]]; then
      missing_seats=$((missing_seats + 1))
      alog "round $round: !! substitute dispatch on $rlane gave no session id — seat MISSING"
      return 0
    fi
    printf '%s|%s|%s|%s\n' "$nid" "$rlane" "$rmodel" "$2" >> "$dir/sessions.txt"
    subst_count=$((subst_count + 1))
    seats_dispatched=$((seats_dispatched + 1))
    remaining2=$(( $3 - $(date +%s) ))
    st2=$(wait_seat "$nid" "$remaining2")
    if [[ "$st2" == "done" || "$st2" == "no_changes" ]]; then
      alog "round $round: ✓ substitute $rlane/$2 $st2"
    else
      missing_seats=$((missing_seats + 1))
      alog "round $round: !! substitute $rlane/$2 $st2 — seat MISSING (no second substitution)"
    fi
  }

  send_continue() { # $1 prompt file — enqueue the fix list; impl_sid becomes the follow-up session
    local out item row st as qsid cap first
    local -a cmd
    cmd=(playmaker continue "$impl_sid" --after --prompt-file "$1" --expect-changes --json)
    if [[ -n "$gate" ]]; then cmd+=(--gate "$gate"); fi
    alog "fix list $(basename "$1") → playmaker continue --after (cycle $cycles/$max_cycles)"
    cont_rc=0
    out=$(pm_run_limits "$seat_deadline" "${cmd[@]}") || cont_rc=$?
    if [[ $cont_rc -eq 200 ]]; then
      impl_exit4 "no lane slot for the fix follow-up within ${seat_deadline}s ([limits] kept refusing)"
    fi
    if [[ $cont_rc -ne 0 ]]; then
      first=$(printf '%s\n' "$out" | head -1)
      impl_exit4 "playmaker continue refused (exit $cont_rc): ${first:0:160}"
    fi
    item=$(printf '%s' "$out" | python3 -c 'import json,sys
try:
    print(json.loads(sys.stdin.read()).get("item", ""))
except ValueError:
    print("")' 2>/dev/null)
    if [[ -z "$item" ]]; then die "no queue item id in continue output: $out"; fi
    alog "queued as #$item — waiting for delivery on the implementer thread"
    # a thread whose last session ended failed never takes a queued item by design
    # (the queue waits for done/no_changes) — drain it with --force once so the fix
    # list is delivered even after e.g. a dropped API socket
    trow=$(playmaker get "$impl_sid" --json 2>/dev/null) || trow=""
    tst=$(row_field "$trow" status)
    if [[ "$tst" == "failed" ]]; then
      treason=$(row_field "$trow" fail_reason)
      alog "implementer thread blocked (last session failed: ${treason:-unknown}) — draining with --force"
      playmaker queue drain --thread "$impl_sid" --force >/dev/null 2>&1 || true
    fi
    qsid=""
    cap=$(( $(date +%s) + 1800 ))
    while :; do
      row=$(playmaker queue show "$item" --json 2>/dev/null) || row=""
      if [[ -n "$row" ]]; then
        st=$(row_field "$row" status); as=$(row_field "$row" delivered_as)
        if [[ "$st" == "delivered" && -n "$as" ]]; then qsid="$as"; break; fi
        if [[ "$st" == "cancelled" ]]; then die "queue item #$item was cancelled — the fix list was never delivered"; fi
      fi
      if [[ $(date +%s) -gt $cap ]]; then die "queue item #$item was not delivered within 30m — check 'playmaker queue show $item'"; fi
      playmaker queue drain --thread "$impl_sid" >/dev/null 2>&1 || true
      sleep 15
    done
    impl_sid="$qsid"
    alog "fix session ${impl_sid:0:8} — waiting for the worker"
  }

  build_gate_fixlist() { # $1 out file, $2 gate status, $3 gate log path
    {
      echo "The acceptance gate run by the playmaker CLI after your session finished with status: $2."
      if [[ -n "$3" && -f "$3" ]]; then
        echo "The tail of its log:"
        echo ""
        tail -n 40 "$3" | sed 's/^/  /'
        echo ""
      else
        echo "Its log is unavailable — re-run the gate yourself to see the failure."
      fi
      echo ""
      echo "Fix the failure and nothing else. Do not refactor anything not needed for the gate."
      if [[ -n "$gate" ]]; then
        echo "Re-run this gate until it exits 0 and paste the output: $gate"
      else
        echo "Re-run your acceptance checks until they pass and paste the output."
      fi
      echo "Reply with a one-line note saying what you changed."
    } > "$1"
  }

  build_precheck_fixlist() { # $1 out file, $2 precheck json, $3 round
    {
      echo "The precheck found acceptance items of the WP spec with no evidence in your diff."
      echo "Fix exactly these, nothing else:"
      echo ""
      python3 - "$2" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        d = json.load(fh)
except (OSError, ValueError):
    d = {}
for i, m in enumerate(d.get("missing") or [], 1):
    item = str(m.get("item", "")).strip() or "(unnamed acceptance item)"
    why = str(m.get("why", "")).strip()
    print(("%d. %s — %s" % (i, item, why)) if why else ("%d. %s" % (i, item)))
PY
      echo ""
      if [[ -n "$gate" ]]; then
        echo "Do not refactor anything not listed. Re-run this gate until it exits 0 and paste the output: $gate"
      else
        echo "Do not refactor anything not listed. Re-run your acceptance checks until they pass and paste the output."
      fi
      echo "Reply with a one-line note per item saying what you changed."
    } > "$1"
  }

  alog "══ auto start: wp=$wp base=$base impl=${impl_sid:0:8} lane=$impl_agent risk=$risk gate=${gate:--} max_cycles=$max_cycles seat_deadline=${seat_deadline}s"

  while :; do
    # ── the implementer and its CLI gate ──
    # (nothing touches the index before this point: a failed implementer must not leave
    # intent-to-add entries behind)
    while :; do
      alog "round $round: waiting for implementer ${impl_sid:0:8}"
      playmaker list --reap --json >/dev/null 2>&1 || true
      prow=$(playmaker get "$impl_sid" --wait --json 2>/dev/null) || die "playmaker get $impl_sid failed"
      prow=$(settle_gate "$prow")
      st=$(row_field "$prow" status); gs=$(row_field "$prow" gate_status)
      impl_gate_status="$gs"; impl_gate_log=$(row_field "$prow" gate_log_path)
      case "$st" in
        failed|killed) impl_exit4 "session ${impl_sid:0:8} $st ($(row_field "$prow" fail_reason))" ;;
        no_changes)    impl_exit4 "session ${impl_sid:0:8} finished with NO changes on a write task" ;;
      esac
      if [[ "$st" != "done" ]]; then impl_exit4 "session ${impl_sid:0:8} in unexpected state: $st"; fi
      if [[ -z "$gate" ]]; then
        # with or without --gate, the row's own gate_cmd is the gate: on fail it rides the
        # fix follow-up, on pass it lets the reviewers skip the re-run (--gate-prepassed)
        row_gate_cmd=$(row_field "$prow" gate_cmd)
        if [[ -n "$row_gate_cmd" ]]; then
          gate="$row_gate_cmd"
          alog "round $round: --gate absent — adopting the implementer session's gate_cmd as the gate"
        fi
      fi
      if [[ "$gs" == "fail" || "$gs" == "timeout" ]]; then
        if [[ -z "$gate" ]]; then
          impl_exit4 "implementer gate failed ($gs) and no gate command is known"
        fi
        if [[ $cycles -ge $max_cycles ]]; then stop3 "cycle budget exhausted and the gate still fails ($gs)"; fi
        cycles=$((cycles + 1)); cyc_gate=$((cyc_gate + 1))
        fixf="$dir/fix-gate-c$cycles.md"
        build_gate_fixlist "$fixf" "$gs" "$impl_gate_log"
        alog "round $round: CLI gate $gs — fix list $(basename "$fixf") (cycle $cycles/$max_cycles)"
        fixf_last="$fixf"
        send_continue "$fixf"
        continue
      fi
      alog "round $round: implementer done (gate_status=${gs:--})"
      break
    done

    # ── precheck ──
    # new files enter the index only now — after the implementer is done and its gate
    # passed — but still BEFORE the precheck builds its diff, so the precheck sees them
    # shellcheck disable=SC2086 # paths (required) is an intentional word-split pathspec list
    untracked=$(git -C "$cwd" ls-files --others --exclude-standard -- $paths 2>/dev/null | grep -v -E '(^|/)\.playmaker/' || true)
    if [[ -n "$untracked" ]]; then
      # shellcheck disable=SC2086
      git -C "$cwd" add -N -- $untracked
      alog "round $round: git add -N (new files): ${untracked//$'\n'/, }"
    fi
    pc_base="$package_base" # the precheck ALWAYS checks acceptance against the whole
                          # package (persisted base): acceptance items describe the WP, not
                          # the round's delta — a delta-based precheck marks everything
                          # delivered in round 1 as missing on every later round. The
                          # reviewers still get the delta (diff-rN.patch is rewritten by
                          # the dispatch step below).
    alog "round $round: precheck against $pc_base (whole package)"
    pc_args=(--cwd "$cwd" --risk "$risk" --impl-agent "$impl_agent" --round "$round" --precheck-only)
    if [[ -n "$precheck_paths" ]]; then pc_args+=(--paths "$precheck_paths"); fi
    if [[ -n "$gate" ]]; then pc_args+=(--gate "$gate"); fi
    pc_rc=0
    "$0" "$wp" "$pc_base" "${pc_args[@]}" || pc_rc=$?
    if [[ $pc_rc -eq 2 ]]; then
      if [[ $cycles -ge $max_cycles ]]; then stop3 "precheck failing and the cycle budget is exhausted"; fi
      cycles=$((cycles + 1)); cyc_pre=$((cyc_pre + 1))
      fixf="$dir/fix-pre-r$round-c$cycles.md"
      build_precheck_fixlist "$fixf" "$dir/precheck-r$round.json" "$round"
      alog "round $round: precheck FAILED — fix list $(basename "$fixf") (cycle $cycles/$max_cycles)"
      fixf_last="$fixf"
      send_continue "$fixf"
      continue
    fi
    if [[ $pc_rc -ne 0 ]]; then die "precheck sub-run failed with exit $pc_rc"; fi
    pc_st=""
    if [[ -f "$dir/board.env" ]]; then
      pc_st=$(sed -n 's/^precheck=//p' "$dir/board.env" | tail -1)
    fi
    alog "round $round: precheck ${pc_st:--}"

    # ── wip snapshot, then the board ──
    if [[ $round -eq 1 ]]; then dispatch_base="$base_ref"; else dispatch_base="$prev_wip"; fi
    # --paths is required: the commit covers the WP's files only, never the shared tree
    commit_paths=()
    # shellcheck disable=SC2086
    for p in $paths; do commit_paths+=("$p"); done
    if git -C "$cwd" commit -q -m "wip: $wp r$round (auto)" -- "${commit_paths[@]}" 2>/dev/null; then
      prev_wip=$(git -C "$cwd" rev-parse HEAD)
      alog "round $round: wip commit ${prev_wip:0:8} (board base: $dispatch_base)"
    else
      if [[ $round -eq 1 ]]; then
        die "nothing to commit for round 1 — the WP tree is empty relative to $base"
      else
        stop3 "the fix round changed nothing (empty delta vs ${prev_wip:0:8}) — the fix list was not applied"
      fi
    fi

    dsp_args=(--risk "$risk" --cwd "$cwd" --impl-agent "$impl_agent" --round "$round" --no-precheck)
    if [[ -n "$paths" ]]; then dsp_args+=(--paths "$paths"); fi
    if [[ -n "$gate" ]]; then dsp_args+=(--gate "$gate"); fi
    if [[ -n "$gate" && "$impl_gate_status" == "pass" ]]; then
      dsp_args+=(--gate-prepassed "${impl_gate_log:--}")
      alog "round $round: dispatching the board (base $dispatch_base; the CLI gate passed — reviewers will NOT re-run it)"
    else
      alog "round $round: dispatching the board (base $dispatch_base)"
    fi
    dsp_rc=0
    dsp_err="$dir/.dispatch-r$round.err"
    # Stream/capture stderr without process substitution (/dev/fd may be blocked).
    # FD 3 preserves stdout; PIPESTATUS keeps the dispatch's own exit status.
    {
      "$0" "$wp" "$dispatch_base" "${dsp_args[@]}" 2>&1 1>&3 3>&- \
        | tee "$dsp_err" >&2 || dsp_rc=${PIPESTATUS[0]}
    } 3>&1
    if [[ $dsp_rc -ne 0 ]]; then
      reason=$(tail -n 1 "$dsp_err" 2>/dev/null)
      impl_exit4 "board dispatch failed (exit $dsp_rc): ${reason:-see the lines above}"
    fi

    # ── wait every seat; substitute a dead one once from the reserve ──
    # one COMMON window: --seat-deadline counted from the board dispatch — each seat
    # (and its substitute) waits only the time remaining in that window, so a hung
    # board cannot stretch to N × the configured deadline
    board_deadline=$(( $(date +%s) + seat_deadline ))
    required=""
    if [[ -f "$dir/board.env" ]]; then
      required=$(sed -n 's/^required=//p' "$dir/board.env" | tail -1)
    fi
    orig_seats=()
    dead_seats=()
    while IFS='|' read -r sid slane smodel slens; do
      if [[ -n "${sid:-}" ]]; then
        orig_seats+=("$sid|$slane|$smodel|$slens")
      elif [[ -n "${slane:-}" ]]; then
        dead_seats+=("$slane|$slens")   # the board could not dispatch it (limits to the deadline)
      fi
    done < "$dir/sessions.txt"
    if [[ -z "$required" ]]; then required=$(( ${#orig_seats[@]} + ${#dead_seats[@]} )); fi
    seats_dispatched=$((seats_dispatched + ${#orig_seats[@]}))
    if [[ ${#dead_seats[@]} -gt 0 ]]; then
      for dead in "${dead_seats[@]}"; do
        IFS='|' read -r dslane dslens <<< "$dead"
        alog "round $round: ! $dslane/$dslens was never dispatched — dead seat"
        substitute_seat "$dslane" "$dslens" "$board_deadline"
      done
    fi
    if [[ ${#orig_seats[@]} -gt 0 ]]; then
      for entry in "${orig_seats[@]}"; do
        IFS='|' read -r sid slane smodel slens <<< "$entry"
        remaining=$(( board_deadline - $(date +%s) ))
        alog "round $round: seat $slane/$slens (${sid:0:8}) — waiting (${remaining}s left in the window)"
        st=$(wait_seat "$sid" "$remaining")
        if [[ "$st" == "done" || "$st" == "no_changes" ]]; then
          alog "round $round: ✓ $slane/$slens $st"
          continue
        fi
        alog "round $round: ! $slane/$slens $st"
        substitute_seat "$slane" "$slens" "$board_deadline"
      done
    fi

    # ── collect + mechanical adjudication ──
    alog "round $round: collecting verdicts"
    "$0" --collect "$wp" --cwd "$cwd" || true
    got=0
    for f in "$dir"/verdict-*.json; do
      if [[ -f "$f" ]]; then got=$((got + 1)); fi
    done
    adj=$(adj_json "$dir" "$dir/diff-r$round.patch") || die "adjudication failed — no patch at diff-r$round.patch"
    blocking_n=$(row_field "$adj" blocking_count); blocking_n="${blocking_n:-0}"
    vn=$(python3 -c 'import json,sys
try: print(len(json.loads(sys.stdin.read()).get("dropped_no_evidence") or []))
except ValueError: print(0)' <<< "$adj") || vn=0
    ve=$(python3 -c 'import json,sys
try: print(len(json.loads(sys.stdin.read()).get("dropped_environmental") or []))
except ValueError: print(0)' <<< "$adj") || ve=0
    vd=$(python3 -c 'import json,sys
try: print(len(json.loads(sys.stdin.read()).get("dropped_duplicate") or []))
except ValueError: print(0)' <<< "$adj") || vd=0
    vn="${vn:-0}"; ve="${ve:-0}"; vd="${vd:-0}"
    drop_noev=$((drop_noev + vn)); drop_env=$((drop_env + ve)); drop_dup=$((drop_dup + vd))

    if [[ "$blocking_n" -eq 0 ]]; then
      if [[ "$got" -lt "$required" ]]; then
        stop3 "zero blocking findings but the board is incomplete ($got/$required verdicts) — not a green board"
      fi
      write_env "auto=green cycles=$cycles"
      alog "GREEN — zero blocking findings after round $round"
      summary "GREEN — zero blocking findings (auto=green cycles=$cycles in board.env)"
      exit 0
    fi
    alog "round $round: $blocking_n blocking finding(s) after adjudication ($got/$required verdicts)"

    fixf="$dir/fix-r$round.md"
    fixlist_from_adj "$adj" "$round" "$gate" > "$fixf"
    fixf_last="$fixf"
    if [[ $cycles -ge $max_cycles ]]; then
      stop3 "cycle budget exhausted ($cycles/$max_cycles) with $blocking_n blocking finding(s) left — fix list at $fixf; escalate per policy"
    fi
    cycles=$((cycles + 1)); cyc_board=$((cyc_board + 1))
    send_continue "$fixf"
    round=$((round + 1))
  done
fi

# ── dispatch mode ─────────────────────────────────────────────────────────────
wp="${1:-}"; base="${2:-}"
[[ -n "$wp" && -n "$base" ]] || die "usage: review-board.sh <wp-label> <base-ref> [--risk ...]"
shift 2

risk=normal; cwd="$PWD"; paths=""; spec=""; gate=""; gate_prepassed=""; impl_agent=""; round=1; dry=0
no_precheck=0; precheck_only=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --risk) risk="$2"; shift 2 ;;
    --cwd) cwd="$2"; shift 2 ;;
    --paths) paths="$2"; shift 2 ;;
    --spec) spec="$2"; shift 2 ;;
    --gate) gate="$2"; shift 2 ;;
    --gate-prepassed) gate_prepassed="$2"; shift 2 ;;
    --impl-agent) impl_agent="$2"; shift 2 ;;
    --round) round="$2"; shift 2 ;;
    --dry-run) dry=1; shift ;;
    --no-precheck) no_precheck=1; shift ;;
    --precheck-only) precheck_only=1; shift ;;
    *) die "unknown option: $1" ;;
  esac
done
case "$risk" in routine|normal|high|seams) ;; *) die "--risk must be routine, normal, high or seams" ;; esac
[[ $no_precheck -eq 1 && $precheck_only -eq 1 ]] && die "pass either --no-precheck or --precheck-only, not both"

conf=""
for c in "$cwd/.playmaker/reviewers.conf" "$HOME/.playmaker/reviewers.conf"; do
  [[ -f "$c" ]] && { conf="$c"; break; }
done
roster_rows=""
if [[ -n "$conf" ]]; then roster_rows=$(parse_roster "$conf") || exit $?; fi

dir="$cwd/.playmaker/reviews/$wp"
mkdir -p "$dir"
spec="${spec:-$dir/spec.md}"
[[ -f "$spec" ]] || die "no WP spec at $spec — write scope, acceptance criteria and the done-condition there first (reviewers cannot refute what was never specified)"
grep -q '^class:' "$spec" || note "spec has no class: header — the ledger hook will record class=-"

patch="$dir/diff-r$round.patch"
# shellcheck disable=SC2086 # paths is an intentional word-split pathspec list
git -C "$cwd" diff "$base" -- $paths > "$patch"
[[ -s "$patch" ]] || die "empty diff between $base and the working tree — nothing to review"
note "diff under review: $patch ($(wc -l < "$patch" | tr -d ' ') lines)"
# shellcheck disable=SC2086
untracked=$(git -C "$cwd" ls-files --others --exclude-standard -- ${paths:-.} 2>/dev/null | grep -v -E '(^|/)\.playmaker/' || true)
if [[ -n "$untracked" ]]; then
  note "!! UNTRACKED files under the review paths are NOT in the patch (git diff cannot see them):"
  printf '     %s\n' $untracked >&2
  note "   if any belong to the WP: git -C \"$cwd\" add -N <file>… and re-run; otherwise say so in board.md"
fi

# ── precheck: a cheap spec-vs-diff coverage check before the board ────────────
# One cheap lane reads the spec's ## ACCEPTANCE section against diff-rN.patch and answers in
# seconds. fail → exit 2 with a numbered fix list, no reviewer dispatched; unavailable → warn
# and continue — the precheck must never block the pipeline by being down.
pc_record_env() {
  # board.env: keep every other key, refresh the precheck pair
  local envf="$dir/board.env"
  touch "$envf"
  grep -v -E '^precheck(_lane)?=' "$envf" >"$envf.tmp" 2>/dev/null || true
  mv "$envf.tmp" "$envf"
  printf 'precheck=%s\nprecheck_lane=%s\n' "$1" "$2" >>"$envf"
}

pc_status=""; pc_lane=""; pc_secs=0; pjson="$dir/precheck-r$round.json"
if [[ $no_precheck -eq 0 ]]; then
  acceptance=$(awk '/^## /{if (in_sec) exit; if ($0 ~ /^## ACCEPTANCE/) in_sec=1} in_sec' "$spec")
  done_when=$(awk '/^## /{if (in_sec) exit; if ($0 ~ /^## DONE when/) in_sec=1} in_sec' "$spec")
  if [[ -z "$acceptance" ]]; then
    pc_status=unavailable
    note "precheck: unavailable (spec has no ## ACCEPTANCE section — nothing to check)"
  else
    precheck_prompt="$dir/precheck-prompt.md"
    {
      echo "You are the PRECHECK for a work package: a fast, cheap coverage check of its diff"
      echo "against the spec's acceptance list. You did NOT write it. Change nothing — read-only."
      echo
      echo "THE ACCEPTANCE LIST (from the WP spec):"
      echo '---'
      printf '%s\n' "$acceptance"
      echo '---'
      if [[ -n "$done_when" ]]; then
        echo
        echo "THE DONE-CONDITION:"
        printf '%s\n' "$done_when"
      fi
      echo
      echo "THE DIFF UNDER REVIEW (the only admissible evidence; paths are relative to the repo"
      echo "root, $cwd):"
      echo "  .playmaker/reviews/$wp/$(basename "$patch")"
      echo "Read that patch file and check every acceptance item against it."
      echo
      echo "RULES:"
      echo "  - Read-only: run nothing that changes anything."
      echo "  - The diff is the ONLY evidence. An acceptance item with no evidence in the diff is missing."
      echo "  - Style is not a reason to mark an item missing."
      echo "  - A shell command in the acceptance section (a gate such as \`pytest -q tests/x.py\`, \`bash -n …\`) is RUN by the CLI and its result is not your concern; it never means \"add or modify that file\". Only an item that explicitly names a NEW test, file or behaviour needs evidence in the diff."
      echo
      echo "OUTPUT: exactly one JSON object, no prose before or after, matching this shape:"
      echo '{"covered":[{"item":"<acceptance item>","evidence":"file:line or test name"}],"missing":[{"item":"<acceptance item>","why":"one sentence"}],"verdict":"pass|fail"}'
    } > "$precheck_prompt"

    pc_roster=()
    if [[ -n "$conf" ]]; then
      while IFS='|' read -r r lane model _lens pc_effort lineno; do
        [[ "$r" == "precheck" ]] || continue
        pc_roster+=("$lane|$model|$pc_effort")
      done <<< "$roster_rows"
    fi

    if [[ $dry -eq 1 ]]; then
      pc_dry_lane="none — no precheck lane in the roster"
      if [[ ${#pc_roster[@]} -gt 0 ]]; then
        IFS='|' read -r pc_dry_l pc_dry_m pc_dry_e <<< "${pc_roster[0]}"
        pc_dry_lane="$pc_dry_l/$pc_dry_m"
        [[ -n "$pc_dry_e" ]] && pc_dry_lane="$pc_dry_lane · effort $pc_dry_e"
      fi
      note "── precheck would dispatch: $pc_dry_lane — prompt at $precheck_prompt:"
      cat "$precheck_prompt" >&2
    elif [[ ${#pc_roster[@]} -eq 0 ]]; then
      pc_status=unavailable
      note "precheck: unavailable (no precheck lines in ${conf:-any reviewers.conf})"
    else
      pc_reasons=""
      for entry in "${pc_roster[@]}"; do
        IFS='|' read -r lane model pc_eff <<< "$entry"
        cmd=(playmaker dispatch "$lane" --sync --read-only --batch "precheck-$wp-r$round" --cwd "$cwd" --prompt-file "$precheck_prompt")
        [[ "$model" != "-" && -n "$model" ]] && cmd+=(--model "$model")
        [[ -n "$pc_eff" ]] && cmd+=(--effort "$pc_eff")
        pc_eff_note=""; [[ -n "$pc_eff" ]] && pc_eff_note=" · effort $pc_eff"
        note "precheck → $lane/${model:--}$pc_eff_note …"
        pc_t0=$(date +%s); pc_rc=0
        pc_wait="${PM_SEAT_DEADLINE:-1200}"
        pc_deadline=$(( $(date +%s) + pc_wait ))
        while :; do
          pc_out=$(run_timed 120 "${cmd[@]}" 2>&1) && pc_rc=0 || pc_rc=$?
          if [[ $pc_rc -ne 0 ]] \
             && printf '%s' "$pc_out" | grep -q -E 'max_parallel=|min_free_ram_gb=' \
             && [[ $(date +%s) -lt $pc_deadline ]]; then
            note "· precheck $lane refused by [limits] — waiting for a lane slot (retry in 30s)"
            sleep 30
            continue
          fi
          break
        done
        pc_secs=$(( $(date +%s) - pc_t0 ))
        printf '%s\n' "$pc_out" > "$dir/precheck-r$round.txt"
        pc_reason="$lane: exit $pc_rc"
        if [[ $pc_rc -eq 124 ]]; then
          pc_reason="$lane: timed out after 120s"
        elif [[ $pc_rc -ne 0 ]]; then
          pc_first=${pc_out%%$'\n'*}
          [[ -n "${pc_first:-}" ]] && pc_reason="$lane: ${pc_first:0:80}"
        fi
        usable=0
        if [[ $pc_rc -eq 0 ]]; then
          # same tolerant last-object parse as the verdicts, plus FULL contract validation:
          # verdict ∈ {pass, fail}; covered and missing are lists; every missing element is an
          # object with string item and why; fail <=> missing non-empty. Anything else
          # (e.g. {"verdict":"pass","missing":"none"}) is an unusable answer -> next lane.
          printf '%s' "$pc_out" | python3 -c '
import json, sys

def valid(d):
    if not isinstance(d, dict):
        return False
    v = d.get("verdict")
    if v not in ("pass", "fail"):
        return False
    cov, mis = d.get("covered"), d.get("missing")
    if not isinstance(cov, list) or not isinstance(mis, list):
        return False
    for m in mis:
        if not isinstance(m, dict) or not isinstance(m.get("item"), str) or not isinstance(m.get("why"), str):
            return False
    for c in cov:
        if not isinstance(c, dict):
            return False
        if not (isinstance(c.get("item"), str) and c["item"].strip()):
            return False
        if not (isinstance(c.get("evidence"), str) and c["evidence"].strip()):
            return False
    if v == "fail":
        return bool(mis)
    # a pass must rest on evidence: at least one well-formed covered entry and nothing missing
    return bool(cov) and not mis

raw = sys.stdin.read()
dec = json.JSONDecoder()
found = None
for j in range(len(raw) - 1, -1, -1):
    if raw[j] != "{":
        continue
    try:
        obj, _ = dec.raw_decode(raw[j:])
    except json.JSONDecodeError:
        continue
    if isinstance(obj, dict) and "verdict" in obj and "missing" in obj:
        found = obj
        break
if found is None or not valid(found):
    sys.exit(1)
json.dump(found, sys.stdout, indent=2, ensure_ascii=False)
' > "$pjson" 2>/dev/null && usable=1
          [[ $usable -eq 0 ]] && pc_reason="$lane: answer violates the precheck contract (no valid JSON object)"
        fi
        if [[ $usable -eq 1 ]]; then
          pc_missing_n=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["missing"]))' "$pjson" 2>/dev/null || echo 0)
          pc_lane="$lane/${model:--}"
          if [[ "$pc_missing_n" -gt 0 ]]; then pc_status=fail; else pc_status=pass; fi
          break
        fi
        pc_reasons="${pc_reasons:+$pc_reasons; }$pc_reason"
      done
      if [[ -z "$pc_status" ]]; then
        pc_status=unavailable
        note "precheck: unavailable ($pc_reasons)"
      fi
    fi
  fi

  if [[ "$pc_status" == "fail" ]]; then
    pc_record_env fail "$pc_lane"
    note ""
    note "precheck FAILED — acceptance items with no evidence in $(basename "$patch"):"
    python3 - "$pjson" <<'PY' >&2
import json, sys
d = json.load(open(sys.argv[1]))
for i, m in enumerate(d.get("missing") or [], 1):
    item = str(m.get("item", "")).strip() or "(unnamed acceptance item)"
    why = str(m.get("why", "")).strip()
    print(f"{i}. {item} — {why}" if why else f"{i}. {item}")
PY
    note ""
    note "precheck FAILED — board not dispatched; send the list with playmaker continue <impl-id>"
    exit 2
  fi

  if [[ "$pc_status" == "pass" ]]; then
    note "precheck: pass ($pc_lane, ${pc_secs}s)"
  fi

  if [[ $precheck_only -eq 1 ]]; then
    [[ -n "$pc_status" ]] && pc_record_env "$pc_status" "$pc_lane"
    exit 0
  fi
fi

roster=()
if [[ -n "$conf" ]]; then
  while IFS='|' read -r r lane model lens effort lineno; do
    [[ "$r" == "$risk" ]] || continue
    [[ -n "$impl_agent" && "$lane" == "$impl_agent" ]] && { note "skipping $lane — it implemented this WP"; continue; }
    roster+=("$lane|$model|$lens|$effort")
  done <<< "$roster_rows"
else
  die "no reviewers.conf found — create ~/.playmaker/reviewers.conf (see the header of this script). There is no default: a silent fallback used to draw the Anthropic reserve."
fi
[[ ${#roster[@]} -gt 0 ]] || die "roster for risk '$risk' is empty in $conf"
case "$risk" in routine) required=1 ;; normal) required=2 ;; high) required=3 ;; seams) required=1 ;; esac
if [[ ${#roster[@]} -lt $required ]]; then
  if [[ "${PM_REVIEW_ALLOW_SHORT:-0}" == "1" ]]; then
    note "!! short board: ${#roster[@]} of $required seats for risk '$risk' — proceeding on PM_REVIEW_ALLOW_SHORT=1; record why in board.md"
    required=${#roster[@]}
  else
    die "only ${#roster[@]} eligible reviewer line(s) for risk '$risk' in $conf, $required required — add a line on another lane, or set PM_REVIEW_ALLOW_SHORT=1 and record the shortfall in board.md"
  fi
elif [[ ${#roster[@]} -gt $required ]]; then
  for extra in "${roster[@]:$required}"; do
    IFS='|' read -r xl xm xn xe <<< "$extra"
    if [[ -n "$xe" ]]; then
      note "reserve (not seated): $xl · $xm · $xn · effort $xe"
    else
      note "reserve (not seated): $xl $xm $xn"
    fi
  done
  roster=("${roster[@]:0:$required}")
fi

contract='{"wp":"<label>","reviewer":"<lane/model>","lens":"<lens>","verdict":"pass|pass_with_nits|fail","findings":[{"severity":"blocking|major|minor","file":"path","line":0,"claim":"one sentence","scenario":"inputs or state -> wrong behaviour","suggested_fix":"one sentence","confidence":"high|medium|low"}],"gate_rerun":"cmd + exit status or null","unverifiable":["..."]}'

batch="review-$wp-r$round"
if [[ $dry -eq 0 ]]; then
  if ls "$dir"/verdict-*.json >/dev/null 2>&1; then
    arch="$dir/archive-$(date -u +%Y%m%dT%H%M%SZ)"; mkdir -p "$arch"
    mv "$dir"/verdict-*.json "$arch"/ 2>/dev/null || true
    [[ -f "$dir/sessions.txt" ]] && cp "$dir/sessions.txt" "$arch/sessions.txt"
    note "previous verdicts archived to $arch"
  fi
  : > "$dir/sessions.txt"
  saved_package_base=""
  [[ -f "$dir/board.env" ]] && saved_package_base=$(sed -n 's/^package_base=//p' "$dir/board.env" | tail -1)
  printf 'risk=%s\nround=%s\nrequired=%s\nbase=%s\npatch=%s\n' "$risk" "$round" "$required" "$base" "$(basename "$patch")" > "$dir/board.env"
  [[ -n "$saved_package_base" ]] && printf 'package_base=%s\n' "$saved_package_base" >> "$dir/board.env"
  [[ -n "$pc_status" ]] && pc_record_env "$pc_status" "$pc_lane"
fi

for entry in "${roster[@]}"; do
  IFS='|' read -r lane model lens effort <<< "$entry"
  prompt_file="$dir/prompt-$lane-$lens.md"
  {
    echo "You are reviewing one work package. You did NOT write it. Your job is to REFUTE it against"
    echo "its acceptance criteria, not to summarize it. Change nothing — this review is read-only."
    echo
    echo "WHAT WAS ASKED (the work package spec):"
    echo '---'
    cat "$spec"
    echo '---'
    echo
    echo "HARD RULE — this checkout is SHARED with other running agents. NEVER change git state: no git reset / checkout / switch / stash / clean / restore / apply / rebase / commit, no rm of files. Read the patch file and the working tree as they are; run only read-only commands and the gate. If you need the base version of a file, use \`git show $base:<path>\` — never move the tree. No edits to tracked files. Scratch files, if you must, only under /tmp/$wp/ — nothing inside $cwd."
    echo "THE DIFF UNDER REVIEW (paths are relative to the repo root, $cwd):"
    echo "  .playmaker/reviews/$wp/$(basename "$patch")"
    echo "Read that patch, and read the files it touches in their current state."
    if [[ "$round" -gt 1 ]]; then
      echo "This is round $round. The patch is the diff against $base: if that is the round-$((round-1)) state,"
      echo "you are reading only the delta since the previous round; otherwise it is the whole package"
      echo "including the fixes. Check that the previous findings were actually fixed and whether the"
      echo "fixes introduced new problems; do not re-open settled points, and refute against the spec"
      echo "above as it stands NOW — the coach updates it when a ruling changed the scope."
    fi
    echo
    echo "YOUR LENS: $lens"
    case "$lens" in
      correctness) echo "Hunt bugs and regressions: edge cases, error paths, null/empty/boundary values, ordering, concurrency, idempotency, and anything the change broke elsewhere." ;;
      contracts)   echo "Hunt interface breakage: types, API shapes, DB schema and migrations, event payloads, callers this change forgot, backwards compatibility." ;;
      risk)        echo "Hunt the expensive failure: authorization, data integrity and loss, money-flow correctness, secrets, and irreversible operations. FALSIFY the acceptance criteria: for each one you attack, give the command, input or repro that would break it — not only the argument. A pass from you lists what you tried." ;;
      seams)       echo "Review ONLY the seams between the work packages listed in the spec: changed interfaces and types, duplicated or conflicting assumptions, migration order versus consumers, event and config names, shared state. Do not re-review a package's internals — each already passed its own board." ;;
      conventions) echo "Hunt divergence from this repo's own patterns, decorative or missing tests, dead code, and leftover TODOs." ;;
      *)           echo "Review through the '$lens' lens." ;;
    esac
    echo "Review through that lens first; mention anything outside it only if it is blocking."
    echo
    if [[ -n "$gate" ]]; then
      if [[ -n "$gate_prepassed" ]]; then
        echo "The acceptance gate was already run by the playmaker CLI right after the implementer"
        echo "finished and it PASSED (exit 0)."
        if [[ "$gate_prepassed" != "-" ]]; then
          echo "Log: $gate_prepassed"
        fi
        echo "Do NOT re-run it — spend your budget on the code. Set gate_rerun to \"cli:pass\"."
      else
        echo "Re-run the acceptance gate yourself and report its real exit status — do not trust the"
        echo "implementer's claim that it passed:"
        echo "  $gate"
        echo "If your environment cannot run it (binaries forbidden, sandbox), set gate_rerun to null and"
        echo "list the gate under unverifiable — never file 'could not run the gate' as a finding."
      fi
    else
      echo "No acceptance gate was provided. Verify what you can with read-only commands and list"
      echo "what you could not check under \"unverifiable\"."
    fi
    echo
    echo "RULES:"
    echo "  - Every finding needs file, line, and a concrete failure scenario (inputs or state -> wrong"
    echo "    behaviour). No evidence, no finding."
    echo "  - \"blocking\" means the code is WRONG — a bug, a broken contract, a security or data-integrity"
    echo "    hole, or a missed acceptance criterion. Not \"I would have written it differently\"."
    echo "  - Style preferences are \"minor\" and never block."
    echo "  - A clean pass is a legitimate result. Do not invent findings to look thorough."
    echo
    echo "OUTPUT: exactly one JSON object, no prose before or after, matching this shape:"
    echo "$contract"
    echo "Set \"wp\" to \"$wp\" and \"reviewer\" to \"$lane/${model:--}\"."
  } > "$prompt_file"

  cmd=(playmaker dispatch "$lane" --json --batch "$batch" --read-only --cwd "$cwd")
  [[ "$model" != "-" && -n "$model" ]] && cmd+=(--model "$model")
  [[ -n "$effort" ]] && cmd+=(--effort "$effort")
  cmd+=(--prompt "$(cat "$prompt_file")")

  if [[ $dry -eq 1 ]]; then
    eff_note=""; [[ -n "$effort" ]] && eff_note=" · effort $effort"
    note "── would dispatch: $lane ${model:+($model)} / $lens$eff_note — prompt at $prompt_file"
    continue
  fi

  seat_rc=0
  out=$(pm_run_limits "${PM_SEAT_DEADLINE:-1200}" "${cmd[@]}") || seat_rc=$?
  if [[ $seat_rc -eq 200 ]]; then
    note "! $lane/$lens: no lane slot within ${PM_SEAT_DEADLINE:-1200}s — dead seat (recorded in sessions.txt with an empty id; --auto substitutes it, by hand seat a reserve line yourself)"
    printf '|%s|%s|%s\n' "$lane" "$model" "$lens" >> "$dir/sessions.txt"
    continue
  fi
  if [[ $seat_rc -ne 0 ]]; then
    first=$(printf '%s\n' "$out" | head -1)
    note "! dispatch of $lane/$lens failed (exit $seat_rc): ${first:0:160}"
    exit 4
  fi
  id=$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("session_id",""))' 2>/dev/null || true)
  [[ -n "$id" ]] || { note "! could not parse a session id from: $out"; continue; }
  printf '%s|%s|%s|%s\n' "$id" "$lane" "$model" "$lens" >> "$dir/sessions.txt"
  eff_note=""; [[ -n "$effort" ]] && eff_note=" · effort $effort"
  note "→ $lane/${model:--} [$lens]$eff_note session $id"
done

[[ $dry -eq 1 ]] && exit 0
note ""
note "batch: $batch — collect when it drains:"
note "  $0 --collect $wp"
