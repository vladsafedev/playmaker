#!/usr/bin/env bash
# review-decision.sh — ask independent senior lanes what breaks in one decision artefact.
#
# Usage: review-decision.sh <slug> --question FILE [--lanes codex,opencode,muse]
#        [--deadline 10m] [--opus] [--dry-run]
#
# The roster is ./.playmaker/reviewers.conf, falling back to ~/.playmaker/reviewers.conf:
#   decision <lane> <model|-> <lens>
# The first three lines are seats; `claude` is included only with --opus.  It is a
# read-only, advisory review: it never stops the cores of a work wave.
# Exit: 0 agreement; 2 split; 1 fewer than two valid answers or an invocation error.

set -euo pipefail

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
note() { printf '%s\n' "$*" >&2; }

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  sed -n '2,/^set -euo pipefail$/p' "$0" | sed '$d; s/^# \{0,1\}//'
  exit 0
fi

command -v playmaker >/dev/null 2>&1 || die "playmaker is not on PATH"

dur_secs() {
  case "$1" in
    *[!0-9smh]*|'') die "bad duration: $1" ;;
    *h) echo $(( ${1%h} * 3600 )) ;;
    *m) echo $(( ${1%m} * 60 )) ;;
    *s) echo "${1%s}" ;;
    *) echo "$1" ;;
  esac
}

# Runs a command with a wall-clock cap. macOS does not provide timeout by default.
run_timed() {
  local secs="$1"; shift
  if command -v timeout >/dev/null 2>&1; then timeout "$secs" "$@" 2>&1
  else
    python3 - "$secs" "$@" <<'PY'
import subprocess, sys
try:
    p = subprocess.run(sys.argv[2:], capture_output=True, text=True, timeout=int(sys.argv[1]))
except subprocess.TimeoutExpired:
    print("timed out", file=sys.stderr); sys.exit(124)
sys.stdout.write(p.stdout or ""); sys.stderr.write(p.stderr or ""); sys.exit(p.returncode)
PY
  fi
}

slug="${1:-}"; [[ -n "$slug" && "$slug" != -* ]] || die "usage: review-decision.sh <slug> --question FILE [options]"
shift
question=""; lanes_arg=""; deadline_arg=""; opus=0; dry=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --question) question="${2:-}"; shift 2 ;;
    --lanes) lanes_arg="${2:-}"; shift 2 ;;
    --deadline) deadline_arg="${2:-}"; shift 2 ;;
    --opus) opus=1; shift ;;
    --dry-run) dry=1; shift ;;
    *) die "unknown option: $1" ;;
  esac
done
[[ -n "$question" && -f "$question" ]] || die "--question FILE is required and must exist"
[[ "$slug" =~ ^[A-Za-z0-9._-]+$ ]] || die "slug may contain only letters, numbers, dot, underscore and dash"

cwd="$PWD"; conf=""
for f in "$cwd/.playmaker/reviewers.conf" "$HOME/.playmaker/reviewers.conf"; do [[ -f "$f" ]] && { conf="$f"; break; }; done
[[ -n "$conf" ]] || die "no reviewers.conf found in .playmaker or ~/.playmaker"

# Repo config wins over personal config. Only [decision_review] deadline is read.
if [[ -z "$deadline_arg" ]]; then
  deadline_arg=$(python3 - "$cwd" <<'PY'
import os, re, sys
def get(path):
    try: text=open(path, encoding='utf-8').read()
    except OSError: return None
    m=re.search(r'^\s*\[decision_review\]\s*$([\s\S]*?)(?=^\s*\[|\Z)', text, re.M)
    if not m: return None
    x=re.search(r'^\s*deadline\s*=\s*["\']?([^"\'\s#]+)', m.group(1), re.M)
    return x.group(1) if x else None
print(get(os.path.expanduser('~/.playmaker/config.toml')) or '')
print(get(os.path.join(sys.argv[1], '.playmaker', 'config.toml')) or '')
PY
)
  deadline_arg=$(printf '%s\n' "$deadline_arg" | awk 'NF {v=$0} END {print v}')
fi
deadline_arg="${deadline_arg:-10m}"; deadline=$(dur_secs "$deadline_arg"); (( deadline > 0 )) || die "deadline must be positive"

seats=()
requested=",${lanes_arg},"
while read -r kind lane model lens extra; do
  [[ -z "${kind:-}" || "$kind" == \#* || "$kind" != decision ]] && continue
  [[ -z "${lane:-}" || -z "${model:-}" || -z "${lens:-}" ]] && continue
  if [[ "$lane" == claude ]]; then   # --opus adds ONLY the first claude line, as a 4th seat
    if [[ $opus -eq 1 && -z "${opus_seat:-}" ]]; then opus_seat="$lane|$model|$lens"; fi
    continue
  fi
  if [[ -n "$lanes_arg" ]]; then [[ "$requested" == *",$lane,"* ]] || continue
  elif [[ ${#seats[@]} -ge 3 ]]; then continue
  fi
  seats+=("$lane|$model|$lens")
done < "$conf"
[[ -n "${opus_seat:-}" ]] && seats+=("$opus_seat")
[[ ${#seats[@]} -ge 2 ]] || die "fewer than two live decision seats in $conf — one opinion is not a decision review"

dir="$cwd/.playmaker/decision-reviews/$slug"; mkdir -p "$dir"
rm -f "$dir"/answer-*.json "$dir"/raw-*.txt "$dir"/summary.md   # a re-run never aggregates stale answers
cp "$question" "$dir/question.md"
if [[ $opus -eq 1 ]]; then note "--opus adds a coordinator-bucket seat; use it deliberately."; fi

build_prompt() { # lane lens output-path
  local lane="$1" lens="$2" out="$3" role_file=""
  if [[ -f "$cwd/.playmaker/roles/$lens.md" ]]; then
    role_file="$cwd/.playmaker/roles/$lens.md"
  elif [[ -f "$HOME/.playmaker/roles/$lens.md" ]]; then
    role_file="$HOME/.playmaker/roles/$lens.md"
  fi
  {
    echo "You are one independent senior seat in a decision review. Change nothing — read-only."
    echo "One session only. Answer within ${deadline_arg}. Your lens: ${lens}."
    if [[ -n "$role_file" ]]; then
      echo
      echo "ROLE FOR THE ${lens} LENS:"
      cat "$role_file"
      # a file without a final newline must not merge with the next line
      if [[ -n "$(tail -c1 "$role_file")" ]]; then echo; fi
    fi
    echo "Your job is to find WHAT BREAKS in the concrete artefact, not to summarize it or vote by taste."
    echo "Cite the artefact (a heading, path, diff hunk, number, or quoted option) in every break's where field."
    echo
    echo "THE COACH'S QUESTION (artefact, options, and one question):"
    echo "---"; cat "$question"; echo "---"
    echo
    echo "OUTPUT exactly one JSON object, no markdown or prose, matching this contract:"
    echo '{"recommendation":"<option or free text, one sentence>","confidence":"high|medium|low","strongest_counter":"<one sentence>","breaks":[{"what":"…","where":"<artefact ref>","severity":"blocking|major|minor"}],"would_change_my_mind":"<one sentence>"}'
  } > "$out"
}

for seat in "${seats[@]}"; do
  IFS='|' read -r lane model lens <<< "$seat"
  build_prompt "$lane" "$lens" "$dir/prompt-$lane.md"
done

if [[ $dry -eq 1 ]]; then
  printf 'Decision review %s — deadline %s — seats:\n' "$slug" "$deadline_arg"
  for seat in "${seats[@]}"; do IFS='|' read -r lane model lens <<< "$seat"; printf '  %s · %s · %s\n' "$lane" "$lens" "$model"; cat "$dir/prompt-$lane.md"; done
  exit 0
fi

today=$(date +%F); log="$cwd/.playmaker/decision-reviews/log.md"; touch "$log"
prior=$(awk -F' · ' -v d="$today" '$1==d {n++} END {print n+0}' "$log")
(( prior >= 2 )) && note "warning: this is decision review $((prior + 1)) today; policy budget is two without the owner's word"

# Dispatch is deliberately fanned out before any wait.
for seat in "${seats[@]}"; do
  IFS='|' read -r lane model lens <<< "$seat"
  ( cmd=(playmaker dispatch "$lane" --read-only --batch "decision-$slug" --prompt-file "$dir/prompt-$lane.md" --json)
    [[ "$model" != - ]] && cmd+=(--model "$model")
    set +e
    "${cmd[@]}" >"$dir/dispatch-$lane.txt" 2>&1
    dispatch_rc=$?
    set -e
    printf '%s\n' "$dispatch_rc" >"$dir/dispatch-$lane.rc"
  ) &
done
wait

printf '%s\n' "${seats[@]}" > "$dir/seats.txt"
deadline_at=$(( $(date +%s) + deadline ))
for seat in "${seats[@]}"; do
  IFS='|' read -r lane model lens <<< "$seat"
  dispatch_rc=$(cat "$dir/dispatch-$lane.rc" 2>/dev/null || echo 1)
  if [[ "$dispatch_rc" != 0 ]]; then
    printf 'no answer (dispatch failed)\n' > "$dir/raw-$lane.txt"
    continue
  fi
  sid=$(python3 - "$dir/dispatch-$lane.txt" <<'PY'
import json, re, sys
s=open(sys.argv[1], encoding='utf-8', errors='replace').read().strip()
try:
 d=json.loads(s); print(d.get('id') or d.get('session_id') or '')
except Exception:
 m=re.findall(r'[A-Za-z0-9][A-Za-z0-9._-]{5,}', s); print(m[-1] if m else '')
PY
)
  if [[ -z "$sid" ]]; then printf 'no answer (dispatch failed)\n' > "$dir/raw-$lane.txt"; continue; fi
  rem=$(( deadline_at - $(date +%s) ))
  if (( rem <= 0 )); then printf 'no answer (deadline elapsed)\n' > "$dir/raw-$lane.txt"; continue; fi
  getrc=0
  getout=$(run_timed "$rem" playmaker get "$sid" --wait --json) || getrc=$?
  printf '%s' "$getout" > "$dir/get-$lane.json"
  raw=$(python3 - "$dir/get-$lane.json" <<'PY'
import json, os, sys
try: d=json.load(open(sys.argv[1], encoding='utf-8'))
except Exception: print(''); raise SystemExit
x=d.get('output') or d.get('final') or d.get('output_path') or ''
if isinstance(x, str) and os.path.isfile(x):
 print(open(x, encoding='utf-8', errors='replace').read(), end='')
elif isinstance(x, str): print(x, end='')
PY
)
  [[ -n "$raw" && $getrc -eq 0 ]] || raw="no answer (session failed or timed out)"
  printf '%s' "$raw" > "$dir/raw-$lane.txt"
  python3 - "$dir/raw-$lane.txt" "$dir/answer-$lane.json" <<'PY' || true
import json, sys
s=open(sys.argv[1], encoding='utf-8', errors='replace').read(); candidates=[]
for i, c in enumerate(s):
 if c != '{': continue
 try: candidates.append(json.JSONDecoder().raw_decode(s[i:])[0])
 except ValueError: pass
def ok(d):
 return isinstance(d,dict) and isinstance(d.get('recommendation'),str) and d['confidence'] in ('high','medium','low') and isinstance(d.get('strongest_counter'),str) and isinstance(d.get('would_change_my_mind'),str) and isinstance(d.get('breaks'),list) and all(isinstance(x,dict) and isinstance(x.get('what'),str) and isinstance(x.get('where'),str) and x.get('severity') in ('blocking','major','minor') for x in d['breaks'])
for d in reversed(candidates):
 if ok(d): json.dump(d, open(sys.argv[2],'w',encoding='utf-8'), ensure_ascii=False, indent=2); raise SystemExit
raise SystemExit(1)
PY
done

python3 - "$dir" "$slug" "$today" <<'PY'
import json, os, re, sys
d, slug, today = sys.argv[1:]; seats=[]
for line in open(os.path.join(d,'seats.txt'),encoding='utf-8'):
 lane, model, lens=line.rstrip('\n').split('|'); p=os.path.join(d,f'answer-{lane}.json')
 try: answer=json.load(open(p,encoding='utf-8'))
 except Exception: answer=None
 seats.append((lane,lens,answer))
q=open(os.path.join(d,'question.md'),encoding='utf-8').read()
labels=set(re.findall(r'^\s*(?:[-*]\s*)?([A-Za-z0-9]+)\s*[):]', q, re.M))
valid=[x for x in seats if x[2] is not None]
norm=[]
for _,_,a in valid:
 r=a['recommendation'].strip().casefold()
 hits=[x for x in labels if re.match(re.escape(x.casefold())+r'(?:\s|[):,.—-]|$)',r)]
 norm.append(hits[0] if len(hits)==1 else None)
agree=len(valid)>=2 and all(norm) and len(set(norm))==1
verdict='Agreement' if agree else 'Split'
sev={'minor':1,'major':2,'blocking':3}; breaks={}
for _,_,a in valid:
 for b in a['breaks']:
  k=b['where']; old=breaks.get(k)
  if old is None or sev[b['severity']]>sev[old['severity']]: breaks[k]=b
out=[f'# Decision review — {slug}','', '## Seats','']
for lane,lens,a in seats:
 out.append(f'- {lane} · {lens} · {a["recommendation"] if a else "no answer"} · {a["confidence"] if a else "-"}')
out += ['',f'## {verdict}','', 'Every valid recommendation names the same listed option.' if agree else 'Recommendations do not all name the same listed option, or fewer than two answers were valid.', '', '## Breaks (deduplicated by where)','']
if breaks:
 for b in sorted(breaks.values(),key=lambda x:(-sev[x['severity']],x['where'])): out.append(f'- {b["severity"]} · {b["where"]} — {b["what"]}')
else: out.append('- None reported.')
out += ['', '## Strongest counters','']
for lane,_,a in valid: out.append(f'- {lane}: {a["strongest_counter"]}')
out += ['', '## Would change my mind','']
for lane,_,a in valid: out.append(f'- {lane}: {a["would_change_my_mind"]}')
open(os.path.join(d,'summary.md'),'w',encoding='utf-8').write('\n'.join(out)+'\n')
open(os.path.join(os.path.dirname(d),'log.md'),'a',encoding='utf-8').write(f'{today} · {slug} · {len(seats)} seats/{len(valid)} valid · {verdict}\n')
print('\n'.join(out))
sys.exit(0 if agree else (1 if len(valid)<2 else 2))
PY
