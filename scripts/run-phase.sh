#!/usr/bin/env bash
# run-phase.sh — execute one IMPLEMENTATION_PLAN.md phase headlessly and log everything.
#
# Usage:
#   scripts/run-phase.sh 0                 # run Phase 0
#   scripts/run-phase.sh 6 --resume        # continue the previous phase's session
#   DRY_RUN=1 scripts/run-phase.sh 3       # print the prompt and exit without calling claude
#
# Artifacts written to logs/:
#   phase-<N>-<stamp>.jsonl    raw stream-json event log (every tool call + result)
#   phase-<N>-<stamp>.md       human-readable transcript
#   phase-<N>-<stamp>.gate.txt output of the full verification gate
#   phase-<N>-<stamp>.meta     session id, cost, duration, exit status

set -uo pipefail

PHASE="${1:-}"
if [[ -z "$PHASE" ]]; then
  echo "usage: scripts/run-phase.sh <phase-number> [--resume]" >&2
  exit 64
fi
shift || true

RESUME=""
for arg in "$@"; do
  [[ "$arg" == "--resume" ]] && RESUME="yes"
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
BASE="$LOGDIR/phase-${PHASE}-${STAMP}"
LAST_SESSION_FILE="$LOGDIR/.last-session-id"

read -r -d '' PROMPT <<EOF
Read CLAUDE.md and PROJECT_SPEC.md in full, then execute **Phase ${PHASE}** of IMPLEMENTATION_PLAN.md.

Scope rules:
- Implement ONLY Phase ${PHASE}. Do not create or modify any file listed under a later phase's
  "Target Files". If a later phase's code seems necessary, stop and say so instead of building it.
- Follow every rule in CLAUDE.md. Add no dependency that is not listed in CLAUDE.md section 2.
- Put every constant in config.py. Put every SQL string in src/data/queries.py.

Completion rules:
- When the implementation is done, run this phase's "Verification Command" from IMPLEMENTATION_PLAN.md.
- Then run the full gate: ruff format --check . && ruff check . && mypy src app.py && pytest
- If either fails, fix the underlying code. Never disable a lint rule, add a # type: ignore,
  mark a test xfail, or weaken an assertion to make the gate pass.
- Do not commit to git; the runner handles commits.

Report rules:
- Finish your final message with a short summary of what you created, any SPEC-GAP decisions you
  made, and then EXACTLY ONE final line in this form:
  PHASE ${PHASE}: PASS
  or
  PHASE ${PHASE}: FAIL - <one-line reason>
EOF

if [[ -n "${DRY_RUN:-}" ]]; then
  printf '%s\n' "$PROMPT"
  exit 0
fi

CLAUDE_ARGS=(
  -p "$PROMPT"
  --output-format stream-json
  --verbose
  --include-partial-messages
  --permission-mode acceptEdits
  --allowedTools "Read,Write,Edit,Glob,Grep,Bash(python *),Bash(python3 *),Bash(pip *),Bash(pytest *),Bash(ruff *),Bash(mypy *),Bash(mkdir *),Bash(ls *),Bash(cat *),Bash(touch *),Bash(streamlit *),Bash(curl *)"
)

if [[ -n "$RESUME" && -f "$LAST_SESSION_FILE" ]]; then
  CLAUDE_ARGS+=(--resume "$(cat "$LAST_SESSION_FILE")")
  echo "==> resuming session $(cat "$LAST_SESSION_FILE")"
fi

echo "==> Phase ${PHASE} starting; raw log: ${BASE}.jsonl"
claude "${CLAUDE_ARGS[@]}" | tee "${BASE}.jsonl"
CLAUDE_EXIT="${PIPESTATUS[0]}"

# --- render a human-readable transcript -------------------------------------
if command -v jq >/dev/null 2>&1; then
  jq -r '
    if .type == "assistant" then
      (.message.content[]? | select(.type=="text") | "\n### assistant\n" + .text),
      (.message.content[]? | select(.type=="tool_use")
        | "\n#### tool: " + .name + "\n```\n"
          + ((.input.command // .input.file_path // (.input|tostring))|tostring)
          + "\n```")
    elif .type == "user" then
      (.message.content[]? | select(.type=="tool_result")
        | "\n#### result\n```\n"
          + ((.content // "")|if type=="array" then map(.text? // "")|join("\n") else tostring end)
          + "\n```")
    elif .type == "result" then
      "\n---\n**session_id:** " + (.session_id // "n/a")
      + "\n**cost_usd:** " + ((.total_cost_usd // 0)|tostring)
      + "\n**duration_ms:** " + ((.duration_ms // 0)|tostring)
    else empty end
  ' "${BASE}.jsonl" > "${BASE}.md" 2>/dev/null

  SESSION_ID="$(jq -r 'select(.type=="result") | .session_id' "${BASE}.jsonl" 2>/dev/null | tail -1)"
  [[ -n "$SESSION_ID" && "$SESSION_ID" != "null" ]] && printf '%s' "$SESSION_ID" > "$LAST_SESSION_FILE"

  {
    echo "phase=${PHASE}"
    echo "stamp=${STAMP}"
    echo "session_id=${SESSION_ID:-unknown}"
    echo "claude_exit=${CLAUDE_EXIT}"
    jq -r 'select(.type=="result") | "cost_usd=" + ((.total_cost_usd // 0)|tostring)
           + "\nduration_ms=" + ((.duration_ms // 0)|tostring)' "${BASE}.jsonl" 2>/dev/null | tail -2
  } > "${BASE}.meta"
else
  echo "NOTE: jq not installed — skipping transcript rendering. Install with: brew install jq"
  cp "${BASE}.jsonl" "${BASE}.md"
  printf 'phase=%s\nstamp=%s\nclaude_exit=%s\n' "$PHASE" "$STAMP" "$CLAUDE_EXIT" > "${BASE}.meta"
fi

# --- independent verification: never trust the model's self-report -----------
echo "==> running the full gate independently"
{
  echo "### ruff format --check ."; ruff format --check .            ; echo "exit=$?"
  echo "### ruff check .";          ruff check .                     ; echo "exit=$?"
  echo "### mypy src app.py";       mypy src app.py 2>/dev/null || mypy src ; echo "exit=$?"
  echo "### pytest";                pytest -q                        ; echo "exit=$?"
} 2>&1 | tee "${BASE}.gate.txt"

if grep -q "^exit=[^0]" "${BASE}.gate.txt"; then
  echo ""
  echo "!!  PHASE ${PHASE} GATE FAILED — see ${BASE}.gate.txt"
  echo "!!  Do NOT start the next phase. Fix with:"
  echo "!!    scripts/run-phase.sh ${PHASE} --resume"
  exit 1
fi

git add -A
git commit -q -m "Phase ${PHASE}: $(sed -n "s/^## Phase ${PHASE} — //p" IMPLEMENTATION_PLAN.md | head -1)

Executed via claude -p. Logs: logs/phase-${PHASE}-${STAMP}.*" || echo "(nothing to commit)"

echo ""
echo "==> PHASE ${PHASE} PASSED. Logs: ${BASE}.{jsonl,md,gate.txt,meta}"
