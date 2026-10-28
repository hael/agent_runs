# Setting up arun on a new Mac and a new remote machine

This prepares a Linux machine that runs Claude Code unattended on a written plan, and a
Mac that controls it over ssh. At the end a smoke test runs a toy plan through the whole
chain. [README.md](README.md) covers daily use.

What you need: a Linux machine you can leave running (x86-64 or ARM64; Claude Code
documents Ubuntu 20.04+ and Debian 10+, Oracle Linux 8 works too), on which you have
sudo and your own account; a Mac on the same network that reaches it with ssh; and a way
to pay for Claude (a subscription, an Anthropic API key, or an institutional cloud
account; step 5 below covers each). The agent runs under your own account on the machine,
never as root.

The remote machine is called `workbox` below; use the name you give it in `~/.ssh/config`.

## Part 1. The remote machine

### Step 1. Install the tools

```bash
# RHEL, Rocky, Alma, Oracle Linux
sudo dnf install -y git python3 rsync util-linux coreutils curl
# Ubuntu, Debian
sudo apt-get install -y git python3 rsync util-linux coreutils curl
```

These give bash, git, python3 (3.6 or newer), rsync, flock, timeout, setsid and curl,
which arun needs. Add what the project needs to build and test.

### Step 2. Keep the machine up and the run alive

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
sudo loginctl enable-linger "$USER"
```

The first line stops desktop power management from suspending the machine in the middle
of a run. The second keeps your processes alive after the last ssh session closes, on
systems where logind ends them at logout. arun already starts the driver with nohup and
setsid, which is enough on most systems; lingering is cheap insurance. A reboot ends the
run; nothing is lost, `arun resume <run>` continues it.

### Step 3. Put the runs on a large local disk

A run keeps its copy of the repository, its builds and its test data under
`~/agent_runs/<run>`; a run on a large code base can fill tens of GB in a day. If your
home directory is small, or on a network share with a quota, point `~/agent_runs` to a
local disk:

```bash
sudo mkdir -p /scratch/"$USER"/agent_runs && sudo chown "$USER": /scratch/"$USER"/agent_runs
ln -s /scratch/"$USER"/agent_runs ~/agent_runs
```

arun keeps a disk floor (`DISK_MIN_GB`, 20 GB by default): below it the session may only
inspect and clean up.

### Step 4. Install Claude Code

```bash
curl -fsSL https://claude.ai/install.sh | bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
~/.local/bin/claude --version
~/.local/bin/claude doctor
```

The installer puts `claude` in `~/.local/bin` and updates it in the background. A run can
last days, and a version change in the middle of a run makes its sessions harder to
compare; step 5 turns background updates off. Update by hand between runs with
`claude update`.

### Step 5. Give Claude Code its credentials

Claude Code finds its credentials in its environment. arun's driver and preflight read one
file on the machine, `~/.config/arun/env`, and export what it holds to every session.
arun never copies this file; it stays on the machine. Create it first:

```bash
mkdir -p ~/.config/arun
install -m 600 /dev/null ~/.config/arun/env
echo 'DISABLE_AUTOUPDATER=1' >> ~/.config/arun/env
```

Then add one of the routes below. If more than one is set, a cloud provider wins over an
API key, and an API key wins over a subscription. An `ANTHROPIC_API_KEY` anywhere in your
environment on the machine silently takes over from a subscription in headless mode; keep
it out of `~/.bashrc` and other start-up files.

**A. Subscription, logged in once** (Pro, Max, Team or Enterprise). From the Mac:

```bash
ssh -t workbox ~/.local/bin/claude
# in Claude Code: /login, open the link on the Mac, paste the code back, then /exit
```

The login is stored in `~/.claude/.credentials.json`. Runs count against the same usage
limits as your own use of Claude. When a limit is hit, the driver sleeps (`LIMIT_SLEEP`,
30 minutes) and continues.

**B. Subscription, long-lived token.** Run `claude setup-token` on the Mac or on the
machine. It prints a long-lived token for headless use and does not store it. Add it to
the file:

```bash
CLAUDE_CODE_OAUTH_TOKEN=<the token>
```

**C. Anthropic API key** (Claude Console). Billing is per token. Create the key in a
workspace of its own with a monthly spend limit, so that no run can overspend. Set
`BUDGET_USD` in a run's `run.conf` to cap each session as well.

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

**D. Amazon Bedrock** (an institutional AWS account):

```bash
CLAUDE_CODE_USE_BEDROCK=1
AWS_PROFILE=claude          # a profile in ~/.aws with Bedrock access
                            # (or AWS_BEARER_TOKEN_BEDROCK=<a Bedrock API key> instead)
AWS_REGION=us-east-1        # optional; otherwise the profile's region
ANTHROPIC_MODEL=<an inference profile ID, such as us.anthropic.claude-...>
```

The AWS account needs access to the Anthropic models enabled in the Bedrock console, and
an IAM policy that allows `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream`,
`bedrock:ListInferenceProfiles` and `bedrock:GetInferenceProfile`.

**E. Google Vertex AI:**

```bash
CLAUDE_CODE_USE_VERTEX=1
CLOUD_ML_REGION=global                  # or your project's region, such as us-east5
ANTHROPIC_VERTEX_PROJECT_ID=<the Google Cloud project ID>
GOOGLE_APPLICATION_CREDENTIALS=$HOME/.config/gcloud/agent-sa.json
                                        # or run `gcloud auth application-default login` once
```

The project needs the Vertex AI API and the Claude models enabled.

**F. Microsoft Foundry:**

```bash
CLAUDE_CODE_USE_FOUNDRY=1
ANTHROPIC_FOUNDRY_RESOURCE=<the resource name, not a URL>
ANTHROPIC_FOUNDRY_API_KEY=<the key>     # leave out to sign in with Entra ID
ANTHROPIC_MODEL=<your Azure deployment name>
```

Fast mode exists only with a subscription or an Anthropic API key. With a cloud provider,
set `FAST_MODE=0` in `run.conf`. Variable names change between Claude Code versions; if a
route does not answer, check it against the pages under https://code.claude.com/docs/en/
(authentication, amazon-bedrock, google-vertex-ai, microsoft-foundry, env-vars).

Check from the Mac that the machine answers headless, the way the driver will call it:

```bash
ssh workbox 'set -a; . ~/.config/arun/env; set +a; ~/.local/bin/claude -p "Reply with OK" --output-format json'
```

The reply is JSON with `"result": "OK"` and `"is_error": false`. In an interactive session
on the machine, `/status` shows which credential is in use.

## Part 2. Connect the Mac

Make a key if you have none:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519
```

Give the machine a short name in `~/.ssh/config`:

```text
Host workbox
    HostName workbox.example.org        # the machine's name or IP address
    User <your user on the machine>
    IdentityFile ~/.ssh/id_ed25519
    UseKeychain yes
    AddKeysToAgent yes
    ServerAliveInterval 30
    ServerAliveCountMax 4
```

Install the key, load it into the Mac's agent, and test:

```bash
ssh-copy-id -i ~/.ssh/id_ed25519.pub workbox
ssh-add --apple-use-keychain ~/.ssh/id_ed25519
ssh -o BatchMode=yes workbox true && echo ok
```

The last line must print `ok` without asking for anything. The mirror runs from a
LaunchAgent and cannot type a passphrase.

## Part 3. Install arun on the Mac

```bash
git clone <this repository's URL> ~/agent_runs
cp ~/agent_runs/arun.conf.example ~/agent_runs/arun.conf
```

Edit `~/agent_runs/arun.conf`: `HOST` is the name from `~/.ssh/config`, `LOCAL_REPO` the
checkout arun copies when a run names none, `TEMPLATE` the directory of `templates/` that
`arun new` starts from (`simple` for the SIMPLE code base, `generic` otherwise). The file
is not tracked; `runs/` and `var/` are not either, so `git pull` never touches a run.

Check the syntax and install the mirror:

```bash
cd ~/agent_runs && bash -n arun engine/*.sh && python3 -m py_compile engine/*.py && echo syntax ok
~/agent_runs/arun mirror install
```

`arun mirror install` first checks that ssh to the machine works without a prompt. It then
installs the LaunchAgent `local.<your user>.arun.mirror`, which runs `arun sync` every ten
minutes and writes `runs/<run>/live/` for every run that is not idle.

To let Claude on the Mac report on runs, connect the folder `~/agent_runs` in the Claude
app and tell it where the mirror is: "Runs on workbox are mirrored in
~/agent_runs/runs/<run>/live/; read synced_at.txt first for freshness, then status.txt and
session_tail.txt." Claude's own sandbox cannot reach the machine, so the mirror is how it
sees runs.

## Part 4. A smoke test

Before the first real plan, run a toy plan through the whole chain. It takes a few minutes
and checks the credential, the fence, the diffs and the mirror.

Make a tiny repository with a plan:

```bash
mkdir -p ~/src/arun_smoke && cd ~/src/arun_smoke && git init -q
cat > plan.md <<'PLAN'
# Smoke test plan

## Phases

Phase 0. Write `calc.py` with a function `add(a, b)` that returns a + b, and
`test_calc.py`, which checks that add(2, 3) == 5 and prints OK.
Exit: `python3 test_calc.py` prints OK.

Phase 1. Add `mul(a, b)` to `calc.py` and a check of mul(2, 3) == 6 to the test.
Exit: `python3 test_calc.py` prints OK.

### File table

| File | Change | Phases |
|------|--------|--------|
| `calc.py` | new | each |
| `test_calc.py` | new | each |

## Progress

(none yet)
PLAN
git add plan.md && git commit -qm "smoke test plan"
~/agent_runs/arun new smoke generic
printf '# Phase notes for smoke\n' > ~/agent_runs/runs/smoke/phases.md
```

In `~/agent_runs/runs/smoke/run.conf` set:

```bash
LOCAL_REPO=~/src/arun_smoke
PLAN=plan.md
PLAN_DONE=plan.md
ALLOW_TABLE="### File table"
PHASES="0 1"
PHASE_TIMEOUT=1h
TOOLS=
JOB_PATTERN="python3"
```

Replace `~/agent_runs/runs/smoke/prompt.md` by:

```text
The plan @PLAN@ defines two phases under "Phases", each with its exit check.
You may run python3 on this machine. Record each phase under "Progress" in
the plan.
```

Then run it:

```bash
~/agent_runs/arun launch smoke        # copies, runs the preflight, starts the driver
~/agent_runs/arun watch smoke         # follow the session; Ctrl-C stops watching, not the run
~/agent_runs/arun status smoke        # when it has ended
~/agent_runs/arun fetch smoke
cd ~/src/arun_smoke && git apply --check ~/agent_runs/runs/smoke/result/review/full.diff && echo applies
```

What to expect. The preflight prints the credential in use (`auth: ...`), the permission
arguments it chose, `write outside the plan's file table -> REFUSED` and
`git push --dry-run -> REFUSED`, then `preflight passed`. Both phases end `DONE` within
minutes. `full.diff` adds `calc.py`, `test_calc.py`, the Progress entries and the review
report `plan_report.md`.

Afterwards remove the toy run: `ssh workbox rm -rf agent_runs/smoke`, then
`rm -rf ~/agent_runs/runs/smoke ~/src/arun_smoke`.

## When something goes wrong

- **The preflight says Claude Code does not answer.** The credential is missing or wrong.
  Repeat the check at the end of Part 1, step 5. For a subscription login, log in again
  with `ssh -t workbox ~/.local/bin/claude`.
- **The preflight says the fence hook did not block a write.** python3 is missing on the
  machine, or the engine copy is incomplete. Check `logs/preflight.txt` in the run
  directory on the machine.
- **The mirror goes stale** (`live/synced_at.txt` says FAILED, or is old). The reason is in
  `live/sync_error.txt`. Most often the ssh key is not in the Mac's agent after a restart:
  `ssh-add --apple-use-keychain`. A VPN on the Mac or a network change can also cut the
  route ("Operation timed out"). The run goes on regardless; only the mirror stops.
- **The run waits on a usage limit.** The driver sleeps 30 minutes and tries again, up to
  `MAX_RESUMES` times. Nothing to do unless it is a spend cap, which also looks like a
  limit: raise the cap or stop the run.
- **The disk fills.** Below `DISK_MIN_GB` the session may only clean up.
  `~/agent_runs/arun status <run>` shows what holds the space, inside and outside the run.
- **The machine rebooted.** `~/agent_runs/arun resume <run>` continues the phase in its
  own session.
- **A phase stopped with "file fence or index check failed".** The phase changed a file its
  row in the file table does not allow, perhaps through the shell. `logs/phase_N_fence.txt`
  names the files. Add the row with a ruling, or tell the session to undo the change, then
  resume.
- **`arun mirror` on a Linux controller.** The mirror is a macOS LaunchAgent; elsewhere run
  `arun sync` from cron (`*/10 * * * * ~/agent_runs/arun sync`). Everything else in arun
  is plain bash, ssh and rsync.
