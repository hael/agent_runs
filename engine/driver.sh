#!/usr/bin/env bash
# arun engine: the driver of one run, on the remote machine.
#
# Started by `arun launch` / `arun resume` as   bash <root>/engine/driver.sh   (detached,
# in its own process group, so `arun stop` ends it together with its sessions and jobs).
# Runs the phases of spec/run.conf in order with Claude Code: a fresh session for the
# first phase, then each phase continues the previous phase's session (CONTINUE_SESSION=1)
# or starts its own (0). A phase counts as done only when its session wrote
# state/phase_N.status starting "DONE", HEAD and the index are untouched, and every file
# the phase changed is one the plan's file table assigns to it; anything else stops the run
# with a report. Nothing is committed: after every phase the driver writes
# review/phase_N.diff and review/full_after_N.diff, at the end review/full.diff.
# After a DONE phase, cleanup.py sweeps the bulky data out of scratch/ (see there).
# Restartable: done phases are skipped, an unfinished phase resumes its own session.
set -u

_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=/dev/null
. "$_ROOT/spec/run.conf"
# this machine's credentials and provider settings; arun never copies this file
# shellcheck source=/dev/null
if [ -f "$HOME/.config/arun/env" ]; then set -a; . "$HOME/.config/arun/env"; set +a; fi
# the run's own paths, set after run.conf so that nothing in it can redirect them
# (LOCAL_REPO there is the controlling machine's checkout; the run works only on its copy in repo/)
ROOT="$_ROOT"
ENGINE="$ROOT/engine"
SPEC="$ROOT/spec"
REPO="$ROOT/repo"
LOGS="$ROOT/logs"
STATE="$ROOT/state"
SCRATCH="$ROOT/scratch"
REVIEW="$ROOT/review"
unset LOCAL_REPO
RUN="${RUN:-$(basename "$ROOT")}"
HOST="${HOST:-$(hostname)}"
PHASES="${PHASES:-0 1 2 3 4 5 6 7 8 9 10}"
PHASE_MAX_TURNS="${PHASE_MAX_TURNS:-3000}"
PHASE_TIMEOUT="${PHASE_TIMEOUT:-48h}"
MAX_RESUMES="${MAX_RESUMES:-40}"
LIMIT_SLEEP="${LIMIT_SLEEP:-1800}"
RETRY_SLEEP="${RETRY_SLEEP:-60}"
CONTINUE_SESSION="${CONTINUE_SESSION:-1}"
FAST_MODE="${FAST_MODE:-1}"
CLAUDE="${CLAUDE_BIN:-$(command -v claude || echo "$HOME/.local/bin/claude")}"
export ARUN_ROOT="$ROOT" ARUN_REPO="$REPO"

mkdir -p "$LOGS" "$STATE" "$SCRATCH" "$REVIEW"
exec 9>"$STATE/driver.lock"
if ! flock -n 9; then
    echo "another driver holds $STATE/driver.lock" >&2
    exit 1
fi

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOGS/driver.log"; }
fence() { python3 "$ENGINE/fence.py" "$@"; }

setup_repo() {
    cd "$REPO" || { log "no repository at $REPO"; exit 1; }
    [ -s "$STATE/base_head" ] && return
    git --no-optional-locks rev-parse HEAD > "$STATE/base_head"
    # what already differed from HEAD in the copied checkout; part of the reviewed diff
    git --no-optional-locks status --porcelain > "$STATE/launch_status.txt"
    log "base HEAD $(cut -c1-12 "$STATE/base_head"); nothing will be committed"
    if [ -s "$STATE/launch_status.txt" ]; then
        log "differences from HEAD at launch (part of the reviewed diff):"
        sed 's/^/    /' "$STATE/launch_status.txt" | tee -a "$LOGS/driver.log"
    fi
}

make_settings() {
    python3 - "$ENGINE/settings.template.json" "$STATE/settings.json" "$ENGINE" "$FAST_MODE" <<'PY'
import json, sys
template, out, engine, fast = sys.argv[1:5]
settings = json.loads(open(template).read().replace('@ENGINE@', engine))
settings['fastMode'] = fast == '1'
json.dump(settings, open(out, 'w'), indent=2)
PY
}

REPORT="${PLAN_DONE:-${PLAN:-doc/$RUN}}"; REPORT="${REPORT%.md}_report.md"   # the review report, in the repo
LAST_PHASE="${PHASES##* }"

fill() {
    # placeholders of the prompt files
    sed -e "s#@REPORT@#$REPORT#g" -e "s#@PHASE@#$1#g" -e "s#@ROOT@#$ROOT#g" -e "s#@REPO@#$REPO#g" -e "s#@RUN@#$RUN#g" \
        -e "s#@HOST@#$HOST#g" -e "s#@PLAN@#${PLAN:-the plan}#g" -e "s#@DISK_MIN_GB@#${DISK_MIN_GB:-20}#g" \
        -e "s#@PROJECT@#${PROJECT:-$RUN}#g" -e "s#@MAINTAINER@#${MAINTAINER:-the maintainer}#g"
}

phase_prompt() {
    local n="$1"
    {
        cat "$ENGINE/prompt_base.md"
        printf '\n## This run\n\n'
        cat "$SPEC/prompt.md"
        printf '\n## Notes for Phase %s\n\n' "$n"
        awk -v h="## Phase $n" '$0 == h {on=1; next} /^## Phase / {on=0} on' "$SPEC/phases.md"
        if [ "$n" = "$LAST_PHASE" ]; then
            printf '\n## The review report (last phase)\n\n'
            echo "When this phase's exit criteria are met, and before the status file, write the review report @REPORT@ in the repository, beside the completed plan (the fence allows it): what changed, phase by phase, pointing to @ROOT@/review/phase_N.diff; every decision taken under delegation and where it is recorded; every deviation from the plan and why; the evidence against the baseline; what is left open for the maintainer. Short enough to read in one sitting; the plan's progress section holds the detail."
        fi
    } | fill "$n"
}

resume_prompt() {
    local n="$1"
    echo "Continue Phase $n where this session left off. First read @ROOT@/spec/rulings.md (the maintainer may have added rulings since you started; they override the plan) and @ROOT@/state/journal_phase_$n.md, check on any job you started, and carry on under the same instructions. Nothing is committed. Finish by writing @ROOT@/state/phase_$n.status as instructed." | fill "$n"
}

session_id_of() {
    python3 - "$1" <<'PY' 2>/dev/null
import json, sys
sid = ''
for line in open(sys.argv[1], errors='replace'):
    try:
        ev = json.loads(line)
    except Exception:
        continue
    sid = ev.get('session_id') or sid
print(sid)
PY
}

result_of() {
    # "<subtype> <is_error> <cost> <turns> fast=<state>/<speed>" of the final result event
    python3 - "$1" <<'PY' 2>/dev/null
import json, sys
res = None
for line in open(sys.argv[1], errors='replace'):
    try:
        ev = json.loads(line)
    except Exception:
        continue
    if ev.get('type') == 'result':
        res = ev
if res:
    speed = (res.get('usage') or {}).get('speed', '?')
    print(res.get('subtype', '?'), res.get('is_error', '?'), res.get('total_cost_usd', '?'),
          res.get('num_turns', '?'), 'fast=%s/%s' % (res.get('fast_mode_state', '?'), speed))
PY
}

limit_hit() {
    # only the end of the session log and its stderr: test output can contain such numbers
    { tail -n 20 "$1"; cat "$1.stderr"; } 2>/dev/null |
        grep -qiE 'usage limit|rate.?limit|"api_error_status": ?(429|529)|overloaded|quota|exceeded your|ThrottlingException|RESOURCE_EXHAUSTED|too many requests'
}

run_claude() {
    # mode new: a fresh session with the phase prompt; resume: continue this phase's own
    # session; continue: the phase prompt sent into the previous phase's session
    local n="$1" out="$2" sid="$3" mode="$4"
    local -a args perm
    case "$mode" in
        resume)   args=(-p "$(resume_prompt "$n")" --resume "$sid") ;;
        continue) args=(-p "$(phase_prompt "$n")" --resume "$sid") ;;
        *)        args=(-p "$(phase_prompt "$n")") ;;
    esac
    args+=(--output-format stream-json --verbose --max-turns "$PHASE_MAX_TURNS" --settings "$STATE/settings.json")
    if [ -s "$STATE/perm_args" ]; then          # chosen by preflight.sh
        read -r -a perm < "$STATE/perm_args"
        args+=("${perm[@]}")
    fi
    [ -n "${MODEL:-}" ] && args+=(--model "$MODEL")
    [ -n "${BUDGET_USD:-}" ] && args+=(--max-budget-usd "$BUDGET_USD")
    (
        cd "$REPO" || exit 1
        ARUN_PHASE="$n" timeout --signal=TERM --kill-after=10m "$PHASE_TIMEOUT" \
            "$CLAUDE" "${args[@]}" < /dev/null > "$out" 2> "$out.stderr"
    )
}

stop_phase() {
    log "phase $1: $2"
    printf '\nSTOPPED by the driver: %s\n' "$2" >> "$STATE/phase_$1.status"
    return 1
}

run_phase() {
    local n="$1" prev="$2" sid="" mode="new" attempt=0 out rc status new_sid
    [ -s "$STATE/phase_$n.snapshot.json" ] || fence snapshot "$n" >> "$LOGS/driver.log" 2>&1
    if [ ! -s "$STATE/phase_$n.session" ]; then
        # a session stopped from outside (arun stop) never got recorded: take it from its log
        out="$(ls -t "$LOGS"/phase_"${n}"_*.jsonl 2>/dev/null | head -n 1)"
        [ -n "$out" ] && new_sid="$(session_id_of "$out")" && [ -n "$new_sid" ] &&
            echo "$new_sid" > "$STATE/phase_$n.session"
    fi
    if [ -s "$STATE/phase_$n.session" ]; then
        sid="$(cat "$STATE/phase_$n.session")"; mode="resume"
    elif [ "$CONTINUE_SESSION" = 1 ] && [ -n "$prev" ] && [ -e "$STATE/phase_$prev.done" ] &&
         [ -s "$STATE/phase_$prev.session" ]; then
        sid="$(cat "$STATE/phase_$prev.session")"; mode="continue"
    fi
    log "phase $n: start ($mode${sid:+ $sid})"
    while [ ! -s "$STATE/phase_$n.status" ]; do
        out="$LOGS/phase_${n}_$(date +%Y%m%d_%H%M%S).jsonl"
        run_claude "$n" "$out" "$sid" "$mode"
        rc=$?
        new_sid="$(session_id_of "$out")"
        if [ -n "$new_sid" ]; then
            sid="$new_sid"; mode="resume"
            echo "$sid" > "$STATE/phase_$n.session"
        fi
        log "phase $n: session ended rc=$rc result=[$(result_of "$out")] log=$out"
        [ -s "$STATE/phase_$n.status" ] && break
        attempt=$((attempt + 1))
        if [ "$attempt" -gt "$MAX_RESUMES" ]; then
            printf 'STOPPED: the session ended %d times without a status\n' "$MAX_RESUMES" > "$STATE/phase_$n.status"
            break
        fi
        if limit_hit "$out"; then
            log "phase $n: usage or rate limit; sleeping ${LIMIT_SLEEP}s"
            sleep "$LIMIT_SLEEP"
        else
            sleep "$RETRY_SLEEP"
        fi
    done
    status="$(head -n 1 "$STATE/phase_$n.status")"
    # review material first, whatever the outcome
    fence phasediff "$n" "$REVIEW/phase_$n.diff" >> "$LOGS/driver.log" 2>&1
    fence fulldiff "$REVIEW/full_after_$n.diff" >> "$LOGS/driver.log" 2>&1
    if [ "$(git -C "$REPO" --no-optional-locks rev-parse HEAD)" != "$(cat "$STATE/base_head")" ]; then
        stop_phase "$n" "HEAD moved; the run never commits"
        return 1
    fi
    if ! ARUN_PHASE="$n" fence check "$n" > "$LOGS/phase_${n}_fence.txt" 2>&1; then
        stop_phase "$n" "file fence or index check failed, see $LOGS/phase_${n}_fence.txt"
        return 1
    fi
    case "$status" in
        DONE*)
            touch "$STATE/phase_$n.done"
            log "phase $n: DONE ($(grep -c '^--- ' "$REVIEW/phase_$n.diff" 2>/dev/null || echo 0) files in review/phase_$n.diff)"
            log "$(python3 "$ENGINE/cleanup.py" sweep "$n" 2>&1)"
            return 0 ;;
        *)
            log "phase $n: $status"
            return 1 ;;
    esac
}

summary() {
    {
        echo "# $RUN: $1"
        echo
        echo "Generated $(date '+%Y-%m-%d %H:%M:%S') on $(hostname)."
        echo "Repository $REPO, base HEAD $(cat "$STATE/base_head" 2>/dev/null). Nothing is committed."
        echo "Review: $REVIEW (full.diff or the last full_after_N.diff; phase_N.diff per phase)."
        echo
        for k in $PHASES; do
            if [ -s "$STATE/phase_$k.status" ]; then
                echo "## Phase $k"
                echo
                sed -n '1,40p' "$STATE/phase_$k.status"
                echo
            fi
        done
        echo "## Files that differ from HEAD"
        echo
        echo '```'
        git -C "$REPO" --no-optional-locks status --porcelain 2>/dev/null
        echo '```'
    } > "$ROOT/RUN_STATUS.md"
}

main() {
    local n prev=""
    setup_repo
    make_settings
    log "driver start: run $RUN, phases $PHASES, $("$CLAUDE" --version 2>/dev/null | head -n 1), fast mode $FAST_MODE"
    log "disk: $(python3 "$ENGINE/cleanup.py" report 2>&1 | head -n 1)"
    for n in $PHASES; do
        if [ -e "$STATE/phase_$n.done" ]; then
            log "phase $n: already done"
            prev="$n"
            continue
        fi
        if ! run_phase "$n" "$prev"; then
            summary "stopped in phase $n"
            log "run stopped in phase $n; see $ROOT/RUN_STATUS.md and $REVIEW"
            exit 1
        fi
        summary "phase $n done"
        prev="$n"
    done
    fence fulldiff "$REVIEW/full.diff" >> "$LOGS/driver.log" 2>&1
    summary "all phases done"
    log "all phases done; complete raw diff in $REVIEW/full.diff"
}

main "$@"
