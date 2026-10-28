#!/usr/bin/env bash
# arun engine: the remote side of arun's commands. Not installed anywhere: arun feeds it
# over ssh, so the remote machine always runs the version of the controlling machine:
#   ssh <host> bash -s -- <run root, relative to $HOME> <command> [args] < engine/remote.sh
#
# Commands
#   status               driver state, phases, journal tail, diff size, load, jobs, disk
#   files                tar stream of the small state files (for the mirror)
#   running              exit 0 if the run's driver or any of its processes runs
#   next                 the phase to resume: the first stopped one, else the first not done
#   prepare-resume N     set phase N's stop aside and note the new rulings in its journal
#   start                start the driver detached (own process group) and check it runs
#   stop                 stop the driver, its sessions and their jobs
set -u
ROOT="$HOME/${1:?run root relative to the home directory}"
CMD="${2:?command}"
shift 2
DRIVER="$ROOT/engine/driver.sh"

driver_pid() {
    # the driver's own bash (not its subshells: those have the driver as parent)
    local p cmd
    for p in $(pgrep -u "$USER" -f '^bash (.*/)?engine/driver\.sh$' 2>/dev/null); do
        cmd="$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null)"
        [ "$cmd" = "bash $DRIVER " ] || continue
        # a subshell of the driver has the same command line as its parent
        [ "$(tr '\0' ' ' < "/proc/$(ps -o ppid= -p "$p" | tr -d ' ')/cmdline" 2>/dev/null)" = "$cmd" ] && continue
        echo "$p"
        return
    done
}

run_pids() {
    # run_pids [driver pid]: every process of the run. The driver exports ARUN_ROOT and its
    # sessions, their shells and their background jobs inherit it, also the ones that left
    # the driver's process group or were orphaned (timeout, setsid, nohup). The driver's
    # process tree is added for anything that cleared its environment.
    local p tree="${1:-}" kids
    {
        for p in $(pgrep -u "$USER" 2>/dev/null); do
            [ "$p" = "$$" ] && continue
            tr '\0' '\n' 2>/dev/null < "/proc/$p/environ" | grep -qx "ARUN_ROOT=$ROOT" && echo "$p"
        done
        kids="$tree"
        while [ -n "$kids" ]; do
            kids="$(for p in $kids; do pgrep -P "$p"; done 2>/dev/null)"
            tree="$tree $kids"
        done
        for p in $tree; do echo "$p"; done
    } | sort -un | tr '\n' ' '
}

conf() {
    # conf NAME DEFAULT: a value of spec/run.conf
    local v=""
    [ -f "$ROOT/spec/run.conf" ] && v="$(. "$ROOT/spec/run.conf" >/dev/null 2>&1; eval "echo \"\${$1:-}\"")"
    echo "${v:-$2}"
}

phases() { conf PHASES "0 1 2 3 4 5 6 7 8 9 10"; }

case "$CMD" in
status)
    echo "== run $ROOT on $(hostname), $(date '+%Y-%m-%d %H:%M:%S %Z')"
    [ -d "$ROOT" ] || { echo "no such run directory"; exit 0; }
    pid="$(driver_pid)"
    if [ -n "$pid" ]; then echo "driver: running (pid $pid)"; else echo "driver: NOT running"; fi
    n_run="$(run_pids "$pid" | wc -w)"
    if [ -z "$pid" ] && [ "$n_run" -gt 0 ]; then
        echo "left over: $n_run processes of the run without a driver (arun stop ends them)"
    fi
    echo
    echo "== driver log (last lines)"
    tail -n 25 "$ROOT/logs/driver.log" 2>/dev/null || echo "no driver log yet"
    echo
    echo "== phases"
    for n in $(phases); do
        if [ -e "$ROOT/state/phase_$n.done" ]; then s="done"
        elif [ -s "$ROOT/state/phase_$n.status" ]; then s="$(head -n 1 "$ROOT/state/phase_$n.status")"
        elif [ -s "$ROOT/state/phase_$n.snapshot.json" ]; then s="in progress"
        else s="pending"; fi
        printf 'phase %-3s %s\n' "$n" "$s"
    done
    j="$(ls -t "$ROOT"/state/journal_phase_*.md 2>/dev/null | head -n 1)"
    if [ -n "$j" ]; then
        echo
        echo "== $(basename "$j"), last lines (the session's own notes)"
        tail -n 30 "$j"
    fi
    echo
    echo "== working tree against base HEAD $(cut -c1-12 "$ROOT/state/base_head" 2>/dev/null) (nothing is committed)"
    git -C "$ROOT/repo" --no-optional-locks diff --stat HEAD 2>/dev/null | tail -n 1
    echo "new files: $(git -C "$ROOT/repo" --no-optional-locks ls-files --others --exclude-standard 2>/dev/null | wc -l)"
    echo
    echo "== machine: load $(cut -d' ' -f1-3 /proc/loadavg) on $(nproc) cores, $(free -g 2>/dev/null | awk '/^Mem:/ {print $3 "/" $2 " GB used"}')"
    echo "== jobs of the run (top by CPU)"
    jp="$(conf JOB_PATTERN 'make|cmake|ctest')"
    ps -u "$USER" -o pid,etime,pcpu,rss,args --sort=-pcpu 2>/dev/null |
        grep -E "$jp" | grep -v -e grep -e claude | cut -c1-170 | head -n 10
    echo
    echo "== disk: $(df -h "$ROOT" | awk 'NR==2 {print $4 " free of " $2 " on " $6 " (" $1 ")"}')"
    # one pass over the run directory, three levels deep (sizes in kB)
    du -k --max-depth=3 "$ROOT" 2>/dev/null | awk -v r="$ROOT" -v now="$(date +%s)" '
        function h(k) { return k >= 1048576 ? sprintf("%.1fG", k/1048576) : (k >= 1024 ? sprintf("%.0fM", k/1024) : k "K") }
        $2 == r { total = $1; next }
        index($2, r "/") == 1 { p = substr($2, length(r) + 2); d = gsub("/", "/", p)
            if (d == 0) top[p] = $1
            else if (p ~ /^scratch\// && d <= 2) scr[p] = $1 }
        END {
            printf "run directory %s; by folder:\n", h(total)
            n = 0; for (p in top) srt[++n] = sprintf("%015d %s", top[p], p)
            m = asort_desc(srt, n)
            for (i = 1; i <= n && i <= 12; i++) { split(srt[i], a, " "); printf "  %-6s %s\n", h(a[1]+0), a[2] }
            if (length(scr) > 0) {
                print "largest in scratch/, two levels deep:"
                n = 0; delete srt; for (p in scr) srt[++n] = sprintf("%015d %s", scr[p], p)
                asort_desc(srt, n)
                for (i = 1; i <= n && i <= 25; i++) { split(srt[i], a, " "); printf "  %-6s %s\n", h(a[1]+0), a[2] }
            }
        }
        function asort_desc(arr, n,   i, j, t) {   # insertion sort, largest first (no gawk asort)
            for (i = 2; i <= n; i++) { t = arr[i]; j = i - 1
                while (j > 0 && arr[j] < t) { arr[j+1] = arr[j]; j-- } arr[j+1] = t }
            return n
        }'
    echo "the home directory (the run writes only inside its own directory):"
    for e in "$HOME"/* "$HOME"/.[!.]*; do
        [ -e "$e" ] || continue
        t='~'; name="${e/#$HOME/$t}"
        case "$ROOT/" in
            "$e"/*) echo "-1 $name (holds this run, see above)" ;;
            *) echo "$(du -sk "$e" 2>/dev/null | cut -f1) $name" ;;
        esac
    done | sort -rn | head -n 12 | awk '{ k = $1; $1 = ""; s = k < 0 ? "" : (k >= 1048576 ? sprintf("%.1fG", k/1048576) : (k >= 1024 ? sprintf("%.0fM", k/1024) : k "K")); printf "  %-6s%s\n", s, $0 }'
    ;;
files)
    cd "$ROOT" || exit 3
    shopt -s nullglob
    f=(state/*.status state/*.done state/journal_*.md state/base_head state/launch_status.txt
       logs/driver.log logs/preflight.txt logs/phase_*_fence.txt logs/cleanup_phase_*.txt
       spec/rulings.md RUN_STATUS.md)
    e=()
    for x in "${f[@]}"; do [ -e "$x" ] && e+=("$x"); done
    [ ${#e[@]} -eq 0 ] && exit 4
    exec tar -cf - "${e[@]}"
    ;;
running)
    # the driver, or any process of the run (jobs left behind count: arun stop ends them)
    [ -n "$(driver_pid)" ] || [ -n "$(run_pids)" ]
    ;;
next)
    for n in $(phases); do
        if [ -s "$ROOT/state/phase_$n.status" ] && head -n 1 "$ROOT/state/phase_$n.status" | grep -q '^STOPPED'; then
            echo "$n"; exit 0
        fi
    done
    for n in $(phases); do [ -e "$ROOT/state/phase_$n.done" ] || { echo "$n"; exit 0; }; done
    ;;
prepare-resume)
    n="${1:?phase}"
    cd "$ROOT" || exit 1
    if [ -s "state/phase_$n.status" ]; then
        mv "state/phase_$n.status" "state/phase_$n.status.stopped_$(date +%Y%m%d_%H%M%S)"
        echo "phase $n: stop set aside"
    fi
    if [ -s "state/journal_phase_$n.md" ] && ! tail -n 3 "state/journal_phase_$n.md" | grep -q 'Maintainer ruling'; then
        printf '\n## Maintainer ruling received %s\nSee spec/rulings.md. A stop report above is answered there; continue the phase under it.\n' \
            "$(date '+%Y-%m-%d %H:%M')" >> "state/journal_phase_$n.md"
    fi
    ;;
start)
    [ -z "$(driver_pid)" ] || { echo "the driver is already running (pid $(driver_pid))"; exit 0; }
    [ -z "$(run_pids)" ] || { echo "$(run_pids | wc -w) processes of the run are still running without a driver; end them first (arun stop)"; exit 1; }
    [ -x "$DRIVER" ] || [ -f "$DRIVER" ] || { echo "no driver at $DRIVER"; exit 1; }
    cd "$ROOT" || exit 1
    mkdir -p logs
    before="$(cat logs/driver.log 2>/dev/null | wc -l)"
    nohup setsid bash "$DRIVER" >> logs/driver.out 2>&1 < /dev/null &
    sleep 8
    if [ -n "$(driver_pid)" ]; then
        echo "driver running (pid $(driver_pid)); last lines of its log:"
        tail -n 4 logs/driver.log
    elif tail -n +"$((before + 1))" logs/driver.log 2>/dev/null | grep -q 'driver start'; then
        echo "the driver started and has already ended; last lines of its log:"
        tail -n 6 logs/driver.log
    else
        echo "the driver did not start; its output:"
        tail -n 30 logs/driver.out
        exit 1
    fi
    ;;
stop)
    pid="$(driver_pid)"
    [ -n "$pid" ] && echo "driver: pid $pid" || echo "no driver running"
    pids="$(run_pids "$pid")"
    if [ -n "$pids" ]; then
        echo "stopping $(echo "$pids" | wc -w) processes of the run:"
        ps -o pid=,etime=,args= -p "$(echo $pids | tr ' ' ',')" 2>/dev/null | cut -c1-150 | sed 's/^/  /'
        kill -TERM $pids 2>/dev/null
        for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
            left=""; for p in $pids; do kill -0 "$p" 2>/dev/null && left="$left $p"; done
            [ -z "$left" ] && break
            sleep 1
        done
        if [ -n "$left" ]; then kill -KILL $left 2>/dev/null; echo "killed what ignored TERM:$left"; fi
        echo "stopped"
    fi
    ;;
*)
    echo "remote.sh: unknown command $CMD" >&2
    exit 64
    ;;
esac
