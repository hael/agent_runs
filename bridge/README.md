# bridge: a code-compile-test loop between Claude's sandbox and this machine

Claude in the desktop app runs in a Linux sandbox without compilers and without network; it can only
read and write the folders connected to it. The bridge turns a connected folder into a job queue: Claude
drops shell requests into `var/bridge/requests/`, a server loop running in your Terminal executes them
one at a time in your login environment (gfortran, cmake, Homebrew PATH) and writes the log and exit
status back, and Claude polls for the result. Nothing else is needed on either side.

## Start the server (once per Terminal session)

```bash
bash ~/src/agent_runs/bridge/serve.sh
```

It prints what it runs and keeps running; Ctrl-C stops it (a running job is killed with it). Make sure
the Claude app has the folder `~/src/agent_runs` connected (and the repository the jobs build).
`var/` is not tracked by git, so the queue, logs and results stay on this machine.

## What Claude calls (from the sandbox, through the mount)

```
run.py status                            heartbeat age, queue, last results, the server's PATH
run.py run [--cwd DIR] [--timeout S] [--max S] [--tail N] -- 'make -C build -j8 2>&1 | tail -40'
run.py run ... < script.sh               a whole script on stdin
run.py submit ... -- CMD ; run.py wait ID  for jobs longer than one tool call (the client returns 124
                                         after --max seconds, default 540; `wait ID` continues)
run.py log ID ; run.py kill ; run.py stop
```

`--cwd` defaults to `~/src/SIMPLE`; `--timeout` (default 2 h) is enforced by the server, which kills the
job's whole process tree. Exit status of the client = the job's rc; 124 = still running (wait again),
125 = the server's heartbeat is stale (it died or the Mac slept), 126 = no server at all.

## Files

```
bridge/serve.sh           the server (bash 3.2 suffices; perl and caffeinate are used when present)
bridge/run.py             the client (python3)
var/bridge/requests/      <id>.sh queued (written as .sh.part, then renamed); STOP, KILL are commands
var/bridge/running/       the request being run; var/bridge/done/ afterwards
var/bridge/results/       <id>.log (live), <id>.start, <id>.end, <id>.rc (written last)
var/bridge/heartbeat      epoch seconds, refreshed every second by the server
var/bridge/serve.log      what the server did; var/bridge/environment.txt  its PATH and tools
```

Anyone who can write to `var/bridge/requests/` runs commands as you. The folder is yours and the
sandbox's only; keep it that way.
