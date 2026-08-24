#!/usr/bin/env bash
# review-board.sh — fan a work package's diff out to independent reviewer agents,
# then collect their JSON verdicts. Part of the playmaker-coach skill.
#
#   review-board.sh <wp-label> <base-ref> [options]     dispatch a review round
#   review-board.sh --collect <wp-label>                gather the verdicts
#
# Options:
#   --risk routine|normal|high   how many reviewers and which lenses (default: normal)
#   --cwd DIR                    repo/worktree under review (default: $PWD)
#   --paths "a b c"              limit the diff to these pathspecs
#   --spec FILE                  the WP spec: scope + acceptance criteria + done-condition
#                                (default: .playmaker/reviews/<wp>/spec.md)
#   --gate "CMD"                 the acceptance command reviewers must re-run
#   --impl-agent LANE            lane that implemented the WP; it is excluded from reviewing
#   --round N                    review round number (default: 1). The diff is always against
#                                <base-ref>, so a later round is cumulative unless you commit
#                                round 1 and pass that commit as the base.
#   --dry-run                    print the prompts and the dispatches, run nothing
#
# Reviewer roster comes from the first of these that exists:
#   ./.playmaker/reviewers.conf     ~/.playmaker/reviewers.conf
# Format — one reviewer per line, comments with '#':
#   <risk> <lane> <model|-> <lens>
# e.g.
#   normal  agy  gemini-3.1-pro-high        correctness
#   normal  agy  claude-opus-4-6-thinking   contracts
#   high    agy  gemini-3.1-pro-high        correctness
#   high    agy  claude-opus-4-6-thinking   contracts
#   high    codex -                         risk
# Model names must be copied from `agy models` / `opencode models`, never typed from memory.

set -euo pipefail

die() { printf '%s\n' "error: $*" >&2; exit 1; }
note() { printf '%s\n' "$*" >&2; }

command -v playmaker >/dev/null 2>&1 || die "playmaker is not on PATH"

# ── collect mode ──────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--collect" ]]; then
  wp="${2:-}"; [[ -n "$wp" ]] || die "usage: review-board.sh --collect <wp-label>"
  dir=".playmaker/reviews/$wp"
  [[ -f "$dir/sessions.txt" ]] || die "no dispatched round found at $dir/sessions.txt"
  pending=0
  while IFS='|' read -r id lane model lens; do
    [[ -n "${id:-}" ]] || continue
    status=$(playmaker get "$id" --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))' 2>/dev/null || echo "")
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
    # tolerate a fenced or prose-wrapped object: take the outermost {...}
    printf '%s' "$out" | python3 -c '
import json, re, sys
raw = sys.stdin.read()
start, end = raw.find("{"), raw.rfind("}")
if start == -1 or end <= start:
    sys.exit(1)
try:
    obj = json.loads(raw[start:end + 1])
except json.JSONDecodeError:
    sys.exit(1)
json.dump(obj, sys.stdout, indent=2, ensure_ascii=False)
' > "$tmp" 2>/dev/null \
      || { rm -f "$tmp"; note "! $lane/$lens ($id): did not return the JSON contract — re-prompt once, then drop"; continue; }
    mv "$tmp" "$dir/verdict-$lane-$lens.json"
    note "✓ $dir/verdict-$lane-$lens.json"
  done < "$dir/sessions.txt"
  [[ $pending -eq 0 ]] || note "$pending reviewer(s) still running"
  ls "$dir"/verdict-*.json >/dev/null 2>&1 && {
    note ""
    note "blocking findings:"
    python3 - "$dir" <<'PY' >&2
import glob, json, os, sys
d = sys.argv[1]
rows = []
for path in sorted(glob.glob(os.path.join(d, "verdict-*.json"))):
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

# ── dispatch mode ─────────────────────────────────────────────────────────────
wp="${1:-}"; base="${2:-}"
[[ -n "$wp" && -n "$base" ]] || die "usage: review-board.sh <wp-label> <base-ref> [--risk ...]"
shift 2

risk=normal; cwd="$PWD"; paths=""; spec=""; gate=""; impl_agent=""; round=1; dry=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --risk) risk="$2"; shift 2 ;;
    --cwd) cwd="$2"; shift 2 ;;
    --paths) paths="$2"; shift 2 ;;
    --spec) spec="$2"; shift 2 ;;
    --gate) gate="$2"; shift 2 ;;
    --impl-agent) impl_agent="$2"; shift 2 ;;
    --round) round="$2"; shift 2 ;;
    --dry-run) dry=1; shift ;;
    *) die "unknown option: $1" ;;
  esac
done
case "$risk" in routine|normal|high) ;; *) die "--risk must be routine, normal or high" ;; esac

dir="$cwd/.playmaker/reviews/$wp"
mkdir -p "$dir"
spec="${spec:-$dir/spec.md}"
[[ -f "$spec" ]] || die "no WP spec at $spec — write scope, acceptance criteria and the done-condition there first (reviewers cannot refute what was never specified)"

patch="$dir/diff-r$round.patch"
# shellcheck disable=SC2086 # paths is an intentional word-split pathspec list
git -C "$cwd" diff "$base" -- $paths > "$patch"
[[ -s "$patch" ]] || die "empty diff between $base and the working tree — nothing to review"
note "diff under review: $patch ($(wc -l < "$patch" | tr -d ' ') lines)"

conf=""
for c in "$cwd/.playmaker/reviewers.conf" "$HOME/.playmaker/reviewers.conf"; do
  [[ -f "$c" ]] && { conf="$c"; break; }
done

roster=()
if [[ -n "$conf" ]]; then
  while read -r r lane model lens; do
    [[ -z "${r:-}" || "$r" == \#* ]] && continue
    [[ "$r" == "$risk" ]] || continue
    [[ -n "$impl_agent" && "$lane" == "$impl_agent" ]] && { note "skipping $lane — it implemented this WP"; continue; }
    roster+=("$lane|$model|$lens")
  done < "$conf"
else
  note "no reviewers.conf found — falling back to a single claude/sonnet reviewer."
  note "Create ~/.playmaker/reviewers.conf for real lane diversity (see the header of this script)."
  roster+=("claude|sonnet|correctness")
fi
[[ ${#roster[@]} -gt 0 ]] || die "roster for risk '$risk' is empty in $conf"

contract='{"wp":"<label>","reviewer":"<lane/model>","lens":"<lens>","verdict":"pass|pass_with_nits|fail","findings":[{"severity":"blocking|major|minor","file":"path","line":0,"claim":"one sentence","scenario":"inputs or state -> wrong behaviour","suggested_fix":"one sentence","confidence":"high|medium|low"}],"gate_rerun":"cmd + exit status or null","unverifiable":["..."]}'

batch="review-$wp-r$round"
: > "$dir/sessions.txt"

for entry in "${roster[@]}"; do
  IFS='|' read -r lane model lens <<< "$entry"
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
    echo "THE DIFF UNDER REVIEW (paths are relative to the repo root, $cwd):"
    echo "  .playmaker/reviews/$wp/$(basename "$patch")"
    echo "Read that patch, and read the files it touches in their current state."
    if [[ "$round" -gt 1 ]]; then
      echo "This is round $round. The patch is the work package as it stands now, including the fixes"
      echo "made after the previous round — it is a diff against $base, not a delta. Check that the"
      echo "previous findings were actually fixed and whether the fixes introduced new problems;"
      echo "do not re-open settled points. (For a true delta, commit after round 1 and pass that"
      echo "commit as the base-ref.)"
    fi
    echo
    echo "YOUR LENS: $lens"
    case "$lens" in
      correctness) echo "Hunt bugs and regressions: edge cases, error paths, null/empty/boundary values, ordering, concurrency, idempotency, and anything the change broke elsewhere." ;;
      contracts)   echo "Hunt interface breakage: types, API shapes, DB schema and migrations, event payloads, callers this change forgot, backwards compatibility." ;;
      risk)        echo "Hunt the expensive failure: authorization, data integrity and loss, money-flow correctness, secrets, and irreversible operations." ;;
      conventions) echo "Hunt divergence from this repo's own patterns, decorative or missing tests, dead code, and leftover TODOs." ;;
      *)           echo "Review through the '$lens' lens." ;;
    esac
    echo "Review through that lens first; mention anything outside it only if it is blocking."
    echo
    if [[ -n "$gate" ]]; then
      echo "Re-run the acceptance gate yourself and report its real exit status — do not trust the"
      echo "implementer's claim that it passed:"
      echo "  $gate"
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
  cmd+=(--prompt "$(cat "$prompt_file")")

  if [[ $dry -eq 1 ]]; then
    note "── would dispatch: $lane ${model:+($model)} / $lens — prompt at $prompt_file"
    continue
  fi

  out=$("${cmd[@]}")
  id=$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("session_id",""))' 2>/dev/null || true)
  [[ -n "$id" ]] || { note "! could not parse a session id from: $out"; continue; }
  printf '%s|%s|%s|%s\n' "$id" "$lane" "$model" "$lens" >> "$dir/sessions.txt"
  note "→ $lane/${model:--} [$lens] session $id"
done

[[ $dry -eq 1 ]] && exit 0
note ""
note "batch: $batch — collect when it drains:"
note "  $0 --collect $wp"
