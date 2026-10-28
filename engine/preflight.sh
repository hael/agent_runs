#!/usr/bin/env bash
# arun engine: preflight of a run, on the remote machine. Run by `arun launch` as
#   bash <root>/engine/preflight.sh
# Records the machine in logs/preflight.txt, installs the Claude Code CLI if it is
# missing, checks that it answers (is logged in), picks the permission arguments the
# driver uses (state/perm_args), and checks that the fence hook refuses an edit outside
# the plan's file table and a git write. Exit 0 only if all of that holds.
set -u
_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=/dev/null
. "$_ROOT/spec/run.conf"
# this machine's credentials and provider settings; arun never copies this file
# shellcheck source=/dev/null
if [ -f "$HOME/.config/arun/env" ]; then set -a; . "$HOME/.config/arun/env"; set +a; fi
# the run's own paths, set after run.conf so that nothing in it can redirect them
ROOT="$_ROOT"; ENGINE="$ROOT/engine"; SPEC="$ROOT/spec"; REPO="$ROOT/repo"; LOGS="$ROOT/logs"; STATE="$ROOT/state"
unset LOCAL_REPO
PHASES="${PHASES:-0 1 2 3 4 5 6 7 8 9 10}"
FIRST="${PHASES%% *}"
mkdir -p "$LOGS" "$STATE" "$ROOT/scratch" "$ROOT/review"
REPORT="$LOGS/preflight.txt"
: > "$REPORT"
say() { echo "$*" | tee -a "$REPORT"; }

say "== machine"
{ uname -a; grep -E '^(NAME|VERSION)=' /etc/os-release 2>/dev/null; echo "nproc $(nproc)"; uptime
  free -g 2>/dev/null | head -n 2; df -h "$ROOT" | tail -n 1; } >> "$REPORT" 2>&1
say "== tools"
# shellcheck disable=SC2086
for t in git python3 rsync flock timeout setsid ${TOOLS:-}; do
    printf '%-9s %s\n' "$t" "$(command -v "$t" 2>/dev/null || echo MISSING)" >> "$REPORT"
done
# shellcheck disable=SC2086
{ for t in ${TOOLS:-}; do "$t" --version 2>&1 | head -n 1; done
  python3 --version 2>&1; git --version; } >> "$REPORT" 2>&1
if type module >/dev/null 2>&1; then
    { echo "-- modules"; module -t avail 2>&1 | head -n 60; } >> "$REPORT" 2>&1
fi
if [ -n "${CHECKOUT_MARKER:-}" ]; then
    { echo "-- other checkouts (read-only for the run)"
      find "$HOME" -maxdepth 4 -name "$CHECKOUT_MARKER" -not -path "$ROOT/*" 2>/dev/null | head -n 20; } >> "$REPORT"
fi
for t in git python3 rsync flock timeout setsid; do
    command -v "$t" >/dev/null || { say "missing required tool: $t"; exit 2; }
done
[ -d "$REPO/.git" ] || { say "no repository at $REPO"; exit 2; }

CLAUDE="$(command -v claude || echo "$HOME/.local/bin/claude")"
if [ ! -x "$CLAUDE" ]; then
    say "== installing Claude Code (native installer)"
    curl -fsSL https://claude.ai/install.sh | bash >> "$REPORT" 2>&1 || true
    CLAUDE="$HOME/.local/bin/claude"
    [ -x "$CLAUDE" ] || { say "could not install Claude Code; see $REPORT"; exit 2; }
fi
say "claude: $CLAUDE ($("$CLAUDE" --version 2>/dev/null | head -n 1))"
# the credential the run will use, in Claude Code's order of precedence (the last match wins)
auth="subscription login (~/.claude/.credentials.json)"
[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && auth="subscription token (CLAUDE_CODE_OAUTH_TOKEN)"
[ -n "${ANTHROPIC_API_KEY:-}" ]       && auth="Anthropic API key (ANTHROPIC_API_KEY)"
[ -n "${CLAUDE_CODE_USE_FOUNDRY:-}" ] && auth="Microsoft Foundry"
[ -n "${CLAUDE_CODE_USE_VERTEX:-}" ]  && auth="Google Vertex AI"
[ -n "${CLAUDE_CODE_USE_BEDROCK:-}" ] && auth="Amazon Bedrock"
say "auth: $auth"

FAST_MODE="${FAST_MODE:-1}"
python3 - "$ENGINE/settings.template.json" "$STATE/settings.json" "$ENGINE" "$FAST_MODE" <<'PY'
import json, sys
template, out, engine, fast = sys.argv[1:5]
settings = json.loads(open(template).read().replace('@ENGINE@', engine))
settings['fastMode'] = fast == '1'
json.dump(settings, open(out, 'w'), indent=2)
PY

ask() {
    # ask "<prompt>" <permission args...>: prints the result text, returns 0 if the call succeeded
    local prompt="$1"; shift
    local out
    out="$(cd "$REPO" && ARUN_PHASE="$FIRST" ARUN_ROOT="$ROOT" ARUN_REPO="$REPO" \
        timeout 600 "$CLAUDE" -p "$prompt" --output-format json --max-turns 4 \
        --settings "$STATE/settings.json" "$@" < /dev/null 2>>"$REPORT")"
    echo "$out" >> "$REPORT"
    echo "$out" > "$STATE/last_probe.json"
    python3 -c '
import json, sys
try:
    r = json.loads(sys.stdin.read())
except Exception:
    sys.exit(1)
print(r.get("result", ""))
sys.exit(1 if r.get("is_error") else 0)' <<< "$out"
}

say "== login and permission mode"
if [ "$(id -u)" = 0 ]; then
    candidates=("--permission-mode dontAsk" "--permission-mode acceptEdits")
else
    candidates=("--permission-mode bypassPermissions" "--dangerously-skip-permissions" "--permission-mode dontAsk")
fi
chosen=""
for c in "${candidates[@]}"; do
    # shellcheck disable=SC2086
    reply="$(ask "Reply with the single word OK and nothing else." $c)" && case "$reply" in *OK*) chosen="$c";; esac
    say "  $c -> ${reply:-<no reply>}"
    [ -n "$chosen" ] && break
done
if [ -z "$chosen" ]; then
    say "Claude Code does not answer here. If it is not logged in, log in once:"
    say "  ssh -t $(hostname -s) $CLAUDE      (then /login, then /exit)"
    say "and run arun launch again. Details in $REPORT."
    exit 3
fi
echo "$chosen" > "$STATE/perm_args"
say "permission arguments: $chosen"
fast_state="$(python3 -c '
import json, sys
try:
    r = json.load(open(sys.argv[1]))
except Exception:
    r = {}
print(r.get("fast_mode_state", "?"), (r.get("usage") or {}).get("speed", "?"))' "$STATE/last_probe.json")"
say "fast mode: requested $FAST_MODE, the session reports $fast_state"
if [ "$FAST_MODE" = 1 ] && [ "${fast_state%% *}" != on ]; then
    say "  warning: fast mode is not on (account or model may not offer it); the run goes on at standard speed"
fi

say "== fence hook"
probe="$REPO/arun_fence_probe_$$.txt"
# shellcheck disable=SC2086
reply="$(ask "Use the Write tool to create the file $probe with the content x. If the tool call is refused, reply REFUSED and do not try another way; otherwise reply WRITTEN." $chosen)"
say "  write outside the plan's file table -> ${reply:-<no reply>}"
if [ -e "$probe" ]; then
    rm -f "$probe"
    say "the fence hook did not block a write outside the plan's file table; not starting"
    exit 4
fi
# shellcheck disable=SC2086
# a harmless git write (a dry-run push), so that a hook failure cannot leave a commit behind
reply="$(ask "Run exactly this shell command with the Bash tool: git push --dry-run origin HEAD . If the tool call is refused, reply REFUSED and do not try another way; otherwise reply RAN." $chosen)"
say "  git push --dry-run -> ${reply:-<no reply>}"
case "$reply" in *REFUSED*) ;; *) say "the fence hook did not refuse a git write; not starting"; exit 4;; esac
say "preflight passed"
