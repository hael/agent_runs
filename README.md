# arun: autonomous Claude Code runs on a remote machine

`arun` carries out a written plan on a copy of a git checkout, unattended, on a remote
machine, phase by phase, with Claude Code in headless mode. You write the plan and a short
run spec on the controlling machine (a Mac), launch, and come back to a complete raw diff.
Nothing is ever committed.

It is project-neutral: the project's conventions (where bare paths in a file table live,
what may be built and run, which build trees to leave out of the copy) are settings of the
run, and `templates/` holds one starting spec per project. `templates/simple` is the
profile for the SIMPLE cryo-EM code base; `templates/generic` fits any repository.

[SETUP.md](SETUP.md) prepares a new machine pair (the Mac and the remote machine) and runs
a smoke test. This file describes daily use.

## Layout

```
~/agent_runs/             a clone of this repository
  arun                    the one command (run it from the Mac)
  arun.conf               this Mac's defaults: HOST, LOCAL_REPO, TEMPLATE (not tracked; see arun.conf.example)
  engine/                 the remote side; arun copies it into every run, never edit it there
    driver.sh               runs the phases, resumes sessions, writes the review diffs
    fence.py                PreToolUse hook (file table, read-only git), snapshots, diffs
    cleanup.py              disk policy: low-disk hook, sweep of scratch/ after each DONE phase
    preflight.sh            machine record, Claude install/login check, fence probes
    remote.sh               status/start/stop/... on the remote machine (fed over ssh)
    session_view.py         readable view of a session's stream-json log
    prompt_base.md          the rules every session gets
    settings.template.json  Claude Code settings: permissions and the fence hook
  templates/<name>/       the four files of a new run spec, one directory per project
  runs/<run>/             one directory per run (not tracked)
    run.conf                host, remote directory, plan, file table, phases, project knobs
    prompt.md               what this run is (added to every session's prompt)
    phases.md               notes per phase ("## Phase N" sections)
    rulings.md              your decisions during the run (read first by every session)
    live/                   the status mirror (written by arun sync, read by Claude on the Mac)
    result/                 what arun fetch copied back (diffs, reports)
  var/                    sync.log, launchd.log, the sync lock (not tracked)
```

On the remote machine a run lives in `~/<REMOTE_ROOT>` (default `~/agent_runs/<run>`):

```
engine/  spec/          copies pushed by arun (spec/ = the four files of runs/<run>/)
repo/                   the private copy of the checkout the run edits
state/                  phase status, journals, session ids, snapshots, base_head
logs/                   driver.log, preflight.txt, the sessions' stream-json logs
review/                 phase_N.diff, full_after_N.diff, full.diff
scratch/                builds, test projects, job logs of the sessions
```

The remote machine holds only copies, the run's own state and its own Claude credentials
(`~/.config/arun/env`, which arun never copies). The commands below assume the clone is at
`~/agent_runs`; arun works from wherever it is checked out.

## Commands

```
~/agent_runs/arun new <run> [template]  create runs/<run>/ from templates/<template>/ (default: TEMPLATE of arun.conf)
~/agent_runs/arun launch <run>          copy the checkout and the spec, preflight, start the driver
~/agent_runs/arun resume <run> [phase]  after a stop or a new ruling: push spec/engine, restart
~/agent_runs/arun rulings <run>         send rulings.md now, also while the run runs (read at each phase start)
~/agent_runs/arun stop <run>            stop the driver, its Claude session and every job of the run
~/agent_runs/arun status [<run>]        live state over ssh; without <run>: all runs from the mirror
~/agent_runs/arun watch <run> [lines]   follow the current session (Ctrl-C stops watching, not the run)
~/agent_runs/arun fetch <run>           copy review/ and the reports to runs/<run>/result/
~/agent_runs/arun sync [<run>...]       refresh runs/<run>/live/ now
~/agent_runs/arun mirror install        refresh the mirror every 10 minutes (LaunchAgent); uninstall, status
~/agent_runs/arun list                  every run with its last mirrored state
```

## Starting a run

1. Write the plan in the repository. It needs phases with exit criteria and a file table:
   a markdown table under a heading of your choice whose first column names the files and
   whose last column lists the phases allowed to edit them (`each` for all). Bare paths are
   read under `PATH_BASE`; a path that starts with a top-level directory of the repository
   is taken as it is. With `COMPANION=".f90:_tester"` a listed `x.f90` also covers
   `x_tester.f90`.
2. `~/agent_runs/arun new <run>` (or `... new <run> generic` for a repository the default
   template does not fit), then fill in `runs/<run>/run.conf` (at least `PLAN`,
   `PLAN_DONE`, `ALLOW_TABLE`, `PHASES`, and `LOCAL_REPO` if it is not the default of
   `arun.conf`), `prompt.md` (which plan sections define phases, tests, checks; which open
   decisions are taken how; what may be built and run) and `phases.md` (anything a phase
   needs beyond the plan).
3. Check that the fence reads the file table as you meant it. This prints the files phase 0
   may edit, with their full repository paths:

   ```bash
   d=$(mktemp -d); mkdir "$d/spec"; cp ~/agent_runs/runs/<run>/run.conf "$d/spec/"
   ln -s <the run's LOCAL_REPO> "$d/repo"
   ARUN_ROOT=$d python3 ~/agent_runs/engine/fence.py list 0; rm -rf "$d"
   ```

4. `~/agent_runs/arun launch <run>`. It copies the tracked files of `LOCAL_REPO` as they
   are in the working tree (uncommitted edits included, untracked files and the
   `COPY_EXCLUDES` patterns left out), runs the preflight and starts the driver. The first
   phase starts a fresh session; later phases continue the previous phase's session
   (`CONTINUE_SESSION=1`).
5. `~/agent_runs/arun mirror install` once, so that Claude on the Mac can answer "how is
   the run doing" from `runs/<run>/live/` without ssh.

## While it runs

A phase ends when its session writes `state/phase_N.status` starting with `DONE` or
`STOPPED: <reason>`. On DONE the driver checks that HEAD did not move and that every file
the phase changed is in the file table for that phase, writes the phase diff and goes on.
On STOPPED (or a failed check) the run stops and waits for you: read the stop report
(`~/agent_runs/arun status <run>`, or the journal in `live/remote/state/`), write your
decision into `runs/<run>/rulings.md`, then `~/agent_runs/arun resume <run>`. The stopped
phase continues in its own session with the ruling. Usage limits are waited out, crashed
sessions resumed. A ruling for a run that is going (no stop) goes out with
`~/agent_runs/arun rulings <run>`; the session reads it when the next phase starts.

When all phases are done, `~/agent_runs/arun fetch <run>` and review
`runs/<run>/result/review/full.diff`. It applies to the commit in `result/base_head`.

Reopening a finished phase N: the driver skips phases with `state/phase_N.done`, so remove
it; the fence compares each phase against `state/phase_M.snapshot.json` taken when phase M
first started, so after reopening N remove `state/phase_M.snapshot.json` of every later
phase M that already started (the driver re-snapshots when the file is missing), else the
fence attributes N's new edits to M. Then `~/agent_runs/arun resume <run> N`.

## Disk

Runs produce a lot of data (test projects, volumes, partial files), so cleanup is part of
the engine, not left to the session's memory:

- The base rules tell every session to delete a test run's bulky outputs once its numbers
  are in the journal, keep logs and scripts, and put what a later phase needs in
  `scratch/keep/`.
- After every DONE phase the driver deletes each file over `CLEAN_MIN_MB` (1) under
  `scratch/` that is not text (logs, scripts, tables, sources stay), except inside build
  trees (directories with `CMakeCache.txt`) and `scratch/keep/`; the list goes to
  `logs/cleanup_phase_N.txt`. It skips the sweep while any process of the run is alive.
- Below `DISK_MIN_GB` (20) free on the run's file system, a hook lets only inspection and
  cleanup commands run (rm, du, df, ls, find, ps, kill, ...) until space is freed; a
  session that cannot free enough stops with "STOPPED: disk full".
- `~/agent_runs/arun status <run>` shows free space, the run directory by folder, the
  largest folders of `scratch/`, and the home directory, so you can see what to clean
  outside the run.

## What the run may do

Edits: only the repository files the plan's file table assigns to the current phase (a new
file is allowed once the session has added its row), plus `scratch/`, `state/`, `logs/`,
`review/` and `/tmp`. `engine/`, `spec/`, `.git` and anything else on the machine are
refused by the hook. Shell: git is read-only (status, diff, log, show, ...); sudo, ssh/scp,
pkill/killall, crontab/systemctl, `rm -rf` of `/`, `~` or `.git` are refused. What may be
built and run on the machine is said in the run's `prompt.md` (the templates carry a line
for it). The review diffs include everything that differs from the base HEAD, so nothing
can hide from review.

The last phase also writes the review report into the repository next to the completed
plan, as `<PLAN_DONE without .md>_report.md`, so it is part of the reviewed diff;
`~/agent_runs/arun fetch` copies it to `runs/<run>/result/`.

These are rails against mistakes, not a sandbox: the session runs as you on the remote
machine, can read anything you can read, and the hook matches shell commands by pattern.
Keep a password on sudo there and give the machine a credential with its own spend limit.

## Knobs (run.conf)

Machine and copy: `HOST`, `LOCAL_REPO` (defaults from `arun.conf`), `REMOTE_ROOT`,
`COPY_EXCLUDES`. Plan: `PLAN`, `PLAN_DONE`, `ALLOW_TABLE`, `PHASES`. Project: `PROJECT`,
`MAINTAINER` (named in the base prompt), `PATH_BASE`, `COMPANION` (the fence), `TOOLS`,
`CHECKOUT_MARKER` (the preflight), `JOB_PATTERN` (the job list of `arun status`). Session:
`CONTINUE_SESSION` (1), `FAST_MODE` (the preflight records whether the account actually
gets it; 0 with a cloud provider), `PHASE_MAX_TURNS` (3000), `PHASE_TIMEOUT` (48h), `MODEL`
(account default), `BUDGET_USD` (none). Disk: `DISK_MIN_GB` (20), `CLEANUP` (1; 0 turns
the sweep off), `CLEAN_MIN_MB` (1). Also read by the driver: `MAX_RESUMES` (40),
`LIMIT_SLEEP` (1800 s), `RETRY_SLEEP` (60 s).

The Mac needs ssh and rsync; the remote machine needs bash, git, python3 (3.6 is enough),
rsync, flock, timeout and setsid. Claude Code is installed by the preflight if it is
missing and must have a credential (SETUP.md, step 5 of the remote machine).
