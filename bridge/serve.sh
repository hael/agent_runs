#!/usr/bin/env bash
# bridge/serve.sh: run shell requests that Claude's sandbox drops into var/bridge/requests, one at a time,
# in this login shell's environment, and write each one's log and exit status back. Start it in a Terminal
# on the machine that has the toolchain and leave it running:
#
#     bash ~/src/agent_runs/bridge/serve.sh          # Ctrl-C stops it; so does: touch var/bridge/requests/STOP
#
# Protocol (the sandbox side is bridge/run.py; both read the directories relative to this file):
#   requests/<id>.sh   a bash script, written as <id>.sh.part and renamed; its first lines may carry
#                      "#cwd: <dir>" and "#timeout: <seconds>" (defaults: $HOME and 7200)
#   running/<id>.sh    the request while it runs; done/<id>.sh afterwards
#   results/<id>.log   stdout+stderr as the job writes it; results/<id>.rc the exit status, written last
#   heartbeat          epoch seconds, refreshed every loop; the client calls the server dead when it is stale
# Anyone who can write requests/ runs commands as you: keep the directory yours. macOS /bin/bash 3.2 suffices.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE="${BRIDGE_DIR:-$HERE/../var/bridge}"
mkdir -p "$BRIDGE/requests" "$BRIDGE/running" "$BRIDGE/done" "$BRIDGE/results"
BRIDGE="$(cd "$BRIDGE" && pwd)"
POLL="${BRIDGE_POLL:-1}"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$BRIDGE/serve.log" >&2; }

# one server per bridge directory (mkdir is atomic)
if ! mkdir "$BRIDGE/lock" 2>/dev/null; then
    other="$(cat "$BRIDGE/serve.pid" 2>/dev/null)"
    if [ -n "$other" ] && kill -0 "$other" 2>/dev/null; then
        echo "serve.sh: another server (pid $other) holds $BRIDGE/lock" >&2
        exit 1
    fi
    rm -rf "$BRIDGE/lock" && mkdir "$BRIDGE/lock" || exit 1
fi
echo $$ > "$BRIDGE/serve.pid"
JOB_PID=
cleanup() {
    if [ -n "$JOB_PID" ]; then kill -TERM -- "-$JOB_PID" 2>/dev/null; fi
    rm -rf "$BRIDGE/lock" "$BRIDGE/serve.pid" "$BRIDGE/heartbeat"
    log "server stopped"
}
trap 'exit 0' INT TERM
trap 'cleanup' EXIT

heartbeat() {
    date +%s > "$BRIDGE/heartbeat.tmp" && mv -f "$BRIDGE/heartbeat.tmp" "$BRIDGE/heartbeat"
}

directive() {  # directive <file> <name> <default>: the value of "#<name>: value" in the file's first 10 lines
    local v
    v="$(head -n 10 "$1" | sed -n "s/^#$2:[[:space:]]*//p" | head -n 1)"
    printf '%s' "${v:-$3}"
}

run_request() {  # run_request <id>
    local id="$1" script cwd tmo t0 rc wrc elapsed
    script="$BRIDGE/running/$id.sh"
    mv "$BRIDGE/requests/$id.sh" "$script" || return
    cwd="$(directive "$script" cwd "$HOME")"
    tmo="$(directive "$script" timeout 7200)"
    cwd="${cwd/#\~/$HOME}"
    rm -f "$BRIDGE/results/$id.rc"
    date +%s > "$BRIDGE/results/$id.start"
    log "start $id (cwd $cwd, timeout ${tmo}s)"
    if [ ! -d "$cwd" ]; then
        echo "serve.sh: no such directory: $cwd" > "$BRIDGE/results/$id.log"
        rc=127
    else
        # the job leads its own session (perl's setsid survives the exec), so a timeout kills the whole tree;
        # caffeinate keeps a Mac awake while it runs
        local keepawake=""
        command -v caffeinate >/dev/null 2>&1 && keepawake="caffeinate -i"
        ( cd "$cwd" && exec perl -e 'use POSIX; POSIX::setsid(); exec @ARGV or die "exec: $!"' $keepawake bash "$script" ) \
            > "$BRIDGE/results/$id.log" 2>&1 < /dev/null &
        JOB_PID=$!
        t0=$(date +%s)
        rc=
        while kill -0 "$JOB_PID" 2>/dev/null; do
            sleep "$POLL"
            heartbeat
            elapsed=$(( $(date +%s) - t0 ))
            if [ "$elapsed" -ge "$tmo" ]; then
                log "timeout $id after ${elapsed}s: killing its session"
                kill -TERM -- "-$JOB_PID" 2>/dev/null
                sleep 5
                kill -KILL -- "-$JOB_PID" 2>/dev/null
                rc=124
            fi
            if [ -e "$BRIDGE/requests/KILL" ]; then
                log "KILL: ending $id"
                rm -f "$BRIDGE/requests/KILL"
                kill -TERM -- "-$JOB_PID" 2>/dev/null
                sleep 5
                kill -KILL -- "-$JOB_PID" 2>/dev/null
                rc=130
            fi
        done
        wait "$JOB_PID" 2>/dev/null
        wrc=$?
        [ -n "$rc" ] || rc=$wrc
        JOB_PID=
    fi
    mv -f "$script" "$BRIDGE/done/$id.sh"
    date +%s > "$BRIDGE/results/$id.end"
    echo "$rc" > "$BRIDGE/results/$id.rc.tmp" && mv -f "$BRIDGE/results/$id.rc.tmp" "$BRIDGE/results/$id.rc"
    log "done  $id rc=$rc"
}

log "server started: $BRIDGE (pid $$, host $(hostname), $(uname -sm))"
{ echo "PATH=$PATH"; command -v gfortran cmake make python3 2>&1; gfortran --version 2>/dev/null | head -n 1; } \
    > "$BRIDGE/environment.txt"
while :; do
    heartbeat
    if [ -e "$BRIDGE/requests/STOP" ]; then
        rm -f "$BRIDGE/requests/STOP"
        log "STOP requested"
        exit 0
    fi
    next=""
    for f in "$BRIDGE"/requests/*.sh; do
        [ -e "$f" ] || continue
        next="$(basename "$f" .sh)"
        break
    done
    if [ -n "$next" ]; then
        run_request "$next"
    else
        sleep "$POLL"
    fi
done
