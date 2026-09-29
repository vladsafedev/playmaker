#!/usr/bin/env bash
# review-board.sh — fan a work package's diff out to independent reviewer agents,
# then collect their JSON verdicts. Part of the playmaker-coach skill.
#
#   review-board.sh <wp-label> <base-ref> [options]     dispatch a review round
#   review-board.sh --collect <wp-label>                gather the verdicts
#
# Options:
#   --risk routine|normal|high|seams   how many reviewers and which lenses (default: normal; seams = joints between WPs)
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
#   normal  agy      gemini-3.1-pro-high        correctness
#   normal  opencode zai-coding-plan/glm-5.3    contracts
#   high    agy      gemini-3.1-pro-high        correctness
#   high    codex    -                          risk
#   high    kimi     kimi-code/k3-256k          contracts
# Lines are an ordered preference list: after skipping --impl-agent the script seats the first N
# (routine 1, normal 2, high 3, seams 1) and holds the rest in reserve — keep count+1 lines per class
# so the skip still fills the board. Fewer than N eligible lines → the script stops
# (PM_REVIEW_ALLOW_SHORT=1 overrides, and board.md must say why).
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
case "$risk" in routine|normal|high|seams) ;; *) die "--risk must be routine, normal, high or seams" ;; esac

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
  for extra in "${roster[@]:$required}"; do note "reserve (not seated): ${extra//|/ }"; done
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
  printf 'risk=%s\nround=%s\nrequired=%s\nbase=%s\npatch=%s\n' "$risk" "$round" "$required" "$base" "$(basename "$patch")" > "$dir/board.env"
fi

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
      echo "Re-run the acceptance gate yourself and report its real exit status — do not trust the"
      echo "implementer's claim that it passed:"
      echo "  $gate"
      echo "If your environment cannot run it (binaries forbidden, sandbox), set gate_rerun to null and"
      echo "list the gate under unverifiable — never file 'could not run the gate' as a finding."
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
