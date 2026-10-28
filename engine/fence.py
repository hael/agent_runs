#!/usr/bin/env python3
"""arun engine: the fence of a run and its raw diffs. Runs on the remote machine.

Nothing in a run is committed, staged or stashed: the change lives in the working
tree of the run's copy and is handed over as raw diffs.

Modes
  fence.py hook                  PreToolUse hook: reads the tool call as JSON on stdin,
                                 exits 2 with a reason on stderr to block it, 0 to allow.
  fence.py snapshot PHASE        Record the working-tree state at the start of PHASE:
                                 state/phase_N.snapshot.json and a copy of every changed
                                 file under state/phase_N.start/.
  fence.py check PHASE           Every file PHASE changed (against its snapshot) must be one
                                 the plan's file table assigns to PHASE; exit 1 if not.
  fence.py phasediff PHASE OUT   Unified diff of what PHASE changed, against its snapshot.
  fence.py fulldiff OUT          Complete raw diff of the working tree against HEAD,
                                 new files included (git diff --binary format).
  fence.py list PHASE            Print the allow rules for PHASE (debugging).

The allow list is the plan's file table: the markdown table under the heading
ALLOW_TABLE of spec/run.conf, whose first column names files (paths under PATH_BASE
unless they start with a top-level directory of the repository) and whose last column
names the phases ("each" for all). It is read at call time, so a row a session adds takes
effect at once. COMPANION (".f90:_tester") lets a listed x.f90 also cover x_tester.f90.

Configuration: spec/run.conf of the run (PLAN, PLAN_DONE, ALLOW_TABLE, PHASES, PATH_BASE,
COMPANION). Environment: ARUN_PHASE, ARUN_REPO, ARUN_ROOT (set by driver.sh).
"""
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

FILE_EXT = re.compile(r'\.[A-Za-z][A-Za-z0-9_]{0,9}$')   # a backticked name with an extension is a file

# git may only be used to read: nothing is committed, staged, stashed or moved.
GIT_READONLY = {
    'status', 'diff', 'log', 'show', 'blame', 'annotate', 'ls-files', 'ls-tree', 'grep',
    'rev-parse', 'rev-list', 'cat-file', 'describe', 'shortlog', 'merge-base', 'name-rev',
    'whatchanged', 'show-ref', 'for-each-ref', 'count-objects', 'check-ignore',
    'check-attr', 'var', 'version', 'help', 'diff-tree', 'diff-files', 'diff-index',
    'range-diff', 'format-patch', 'archive',
}
GIT_GLOBAL_OPT = (r'(?:\s+(?:-C\s+\S+|-c\s+\S+|--git-dir(?:=|\s+)\S+|--work-tree(?:=|\s+)\S+'
                  r'|--no-pager|--no-optional-locks|--paginate|-P|--literal-pathspecs'
                  r'|--no-replace-objects))*')
GIT_CALL = re.compile(r'(?:^|[\s;&|(`$/])git' + GIT_GLOBAL_OPT + r'\s+([A-Za-z][\w-]*)([^;&|\n)]*)')
GIT_HINT = ('git is read-only in this run (status, diff, log, show, ls-files, grep, ...): '
            'restore a file with "git show <rev>:<path> > <path>", move with mv, delete with rm; '
            'nothing is committed, staged or stashed')

# Other shell commands a run must never issue.
DENY_BASH = [
    (r'\bsudo\b', 'no sudo'),
    (r'\b(pkill|killall)\b', 'kill your own jobs by PID only'),
    (r'\b(crontab|systemctl|shutdown|reboot)\b', 'no system changes'),
    (r'(^|[\s;&|(])(ssh|scp)\s', 'the run stays on this machine'),
    (r'\brm\s+-[A-Za-z]*[rR][A-Za-z]*\s+(-[A-Za-z]+\s+)*(/|~|\$HOME|\$\{HOME\})/?(\s|$|;)',
     'refusing to remove / or the home directory'),
    (r'(^|[\s;&|(])(rm|mv)\s[^;&|]*\.git(/|\s|$)', 'the .git directory is not touched'),
]


def env(name, default=''):
    return os.environ.get(name, default)


def repo_root():
    return os.path.realpath(env('ARUN_REPO') or os.path.join(run_root(), 'repo'))


def run_root():
    root = env('ARUN_ROOT')
    if not root:  # engine/fence.py inside the run root
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.realpath(root)


_CONF = None


def conf(key, default=''):
    """A value of spec/run.conf (KEY=VALUE lines, optional quotes, # comments)."""
    global _CONF
    if _CONF is None:
        _CONF = {}
        path = os.path.join(run_root(), 'spec', 'run.conf')
        if os.path.isfile(path):
            for line in open(path, encoding='utf-8'):
                line = line.strip()
                m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$', line)
                if not m or line.startswith('#'):
                    continue
                val = m.group(2).strip()
                if val[:1] in ('"', "'") and val.find(val[0], 1) > 0:
                    # quoted value, possibly followed by a comment
                    val = val[1:val.find(val[0], 1)]
                else:
                    # unquoted value up to a comment (which may be all there is: KEY=   # ...)
                    val = re.split(r'(?:^|\s)#', val, 1)[0].strip()
                _CONF[m.group(1)] = val
    return _CONF.get(key, default) or default


def all_phases():
    return {int(x) for x in conf('PHASES', ' '.join(str(i) for i in range(11))).split()}


def report_path():
    """The review report of the last phase: <completed plan>_report.md (see driver.sh)."""
    base = conf('PLAN_DONE') or conf('PLAN') or ('doc/' + conf('RUN', 'run'))
    return (base[:-3] if base.endswith('.md') else base) + '_report.md'


def plan_paths():
    return [p for p in (conf('PLAN'), conf('PLAN_DONE'), report_path()) if p]


def state_dir():
    return os.path.join(run_root(), 'state')


def git(*args, check=True, stdin=None):
    return subprocess.run(['git', '--no-optional-locks', '-C', repo_root()] + list(args),
                          check=check, input=stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True).stdout


def plan_text():
    for rel in [p for p in (conf('PLAN'), conf('PLAN_DONE')) if p]:
        path = os.path.join(repo_root(), rel)
        if os.path.isfile(path):
            with open(path, encoding='utf-8') as fh:
                return fh.read()
    return ''


# ----------------------------------------------------------------------------- file table rules

def split_cells(line):
    line = line.strip()
    if line.startswith('|'):
        line = line[1:]
    if line.endswith('|'):
        line = line[:-1]
    return [c.strip() for c in re.split(r'(?<!\\)\|', line)]


def parse_phases(cell):
    cell = cell.lower()
    if 'each' in cell:
        return all_phases()
    return {int(x) for x in re.findall(r'\d+', cell)}


_TRACKED = None


def tracked_files():
    global _TRACKED
    if _TRACKED is None:
        try:
            _TRACKED = set(git('ls-files').splitlines())
        except Exception:
            _TRACKED = set()
    return _TRACKED


_TOP = None


def top_dirs():
    """Top-level directories of the repository; a path starting with one is taken as is."""
    global _TOP
    if _TOP is None:
        try:
            _TOP = tuple(d + '/' for d in git('ls-tree', '-d', '--name-only', 'HEAD').splitlines() if d)
        except Exception:
            _TOP = ()
    return _TOP


def path_base():
    """PATH_BASE of run.conf: where bare paths of the file table live ('' = repository root)."""
    base = conf('PATH_BASE').strip('/')
    return base + '/' if base else ''


def resolve(tok, base_dir):
    """Map a path as the plan's file table writes it to a repository path.

    Paths are under PATH_BASE unless they start with a top-level directory. A bare file
    name sits beside the previous path of the row, or directly under PATH_BASE; whichever
    exists wins, a new file takes the row's directory."""
    tok = tok.strip()
    if top_dirs() and tok.startswith(top_dirs()):
        return tok
    if '/' in tok:
        return path_base() + tok
    candidates = []
    if base_dir:
        candidates.append(base_dir.rstrip('/') + '/' + tok)
    candidates.append(path_base() + tok)
    for c in candidates:
        if c in tracked_files() or os.path.exists(os.path.join(repo_root(), c)):
            return c
    return candidates[0]


def path_tokens(cell):
    toks = []
    for tok in re.findall(r'`([^`]+)`', cell):
        if any(ch in tok for ch in ' =(){}:,%') or tok.startswith('-'):
            continue
        if tok.endswith('/') or '/' in tok or FILE_EXT.search(tok):
            toks.append(tok)
    return toks


def rules_for(phase, text=None):
    """Return (exact paths, glob patterns, directory prefixes) that phase may edit."""
    text = plan_text() if text is None else text
    exact = set(plan_paths())
    globs, prefixes = set(), set()
    heading = conf('ALLOW_TABLE')
    m = re.search(r'^' + re.escape(heading) + r'\s*$', text, re.M) if heading else None
    if not m:
        return exact, globs, prefixes
    rest = text[m.end():]
    end = re.search(r'^##? ', rest, re.M)
    block = rest[:end.start()] if end else rest
    for line in block.splitlines():
        if not line.startswith('|'):
            continue
        cells = split_cells(line)
        if len(cells) < 3 or cells[0] == 'File' or re.fullmatch(r'[-\s]*', cells[0]):
            continue
        if phase not in parse_phases(cells[-1]):
            continue
        resolved, base_dir = [], None
        for tok in path_tokens(cells[0]):
            r = resolve(tok, base_dir)
            resolved.append(r)
            if not r.endswith('/'):
                base_dir = os.path.dirname(r) or base_dir
        # file names in the disposition cell (replacement files) live beside the row's file
        for tok in path_tokens(cells[1]):
            resolved.append(resolve(tok, base_dir))
        for r in resolved:
            if r.endswith('/'):
                prefixes.add(r)
            elif any(ch in r for ch in '*?['):
                globs.add(r)
            else:
                exact.add(r)
                comp = conf('COMPANION')         # ".f90:_tester": x.f90 also allows x_tester.f90
                if ':' in comp:
                    cext, csuffix = comp.split(':', 1)
                    stem, ext = os.path.splitext(r)
                    if ext.lower() == cext.lower() and not stem.endswith(csuffix):
                        exact.add(stem + csuffix + ext)
    return exact, globs, prefixes


def allowed_in_repo(rel, phase, rules=None):
    exact, globs, prefixes = rules if rules is not None else rules_for(phase)
    if rel in exact:
        return True
    if any(fnmatch.fnmatch(rel, g) for g in globs):
        return True
    if any(rel.startswith(p) for p in prefixes):
        return True
    if rel.startswith(('build/', 'build_')):   # build trees are not source edits
        return True
    return False


# ----------------------------------------------------------------------------- hook

def classify(path, cwd):
    """Return ('repo', rel) | ('run', abs) | ('protected', abs) | ('other', abs)."""
    if not os.path.isabs(path):
        path = os.path.join(cwd or os.getcwd(), path)
    absp = os.path.realpath(path)
    repo = repo_root()
    if absp == repo or absp.startswith(repo + os.sep):
        rel = os.path.relpath(absp, repo).replace(os.sep, '/')
        if rel == '.git' or rel.startswith('.git/'):
            return 'other', absp
        return 'repo', rel
    root = run_root()
    for sub in ('scratch', 'state', 'logs', 'review'):
        d = os.path.join(root, sub)
        if absp == d or absp.startswith(d + os.sep):
            return 'run', absp
    if absp == root or absp.startswith(root + os.sep):
        return 'protected', absp      # engine/ and spec/ belong to arun and the maintainer
    if absp.startswith('/tmp/'):
        return 'run', absp
    return 'other', absp


def git_violation(cmd):
    for m in GIT_CALL.finditer(cmd):
        sub, args = m.group(1), m.group(2).strip()
        words = args.split()
        if sub in GIT_READONLY:
            continue
        if sub == 'branch' and all(w in ('-a', '-r', '-v', '-vv', '--list', '--all', '--remotes',
                                         '--show-current', '--no-color') for w in words):
            continue
        if sub == 'config' and words and words[0] in ('--get', '--get-all', '--get-regexp',
                                                      '--list', '-l'):
            continue
        if sub == 'stash' and words and words[0] in ('list', 'show'):
            continue
        if sub == 'remote' and all(w in ('-v', 'show') for w in words):
            continue
        if sub == 'worktree' and words[:1] == ['list']:
            continue
        return 'git %s' % sub
    return None


def deny(msg):
    sys.stderr.write('arun fence: ' + msg + '\n')
    sys.exit(2)


def hook():
    try:
        data = json.load(sys.stdin)
    except Exception as exc:  # fail open; the post-phase check still runs
        sys.stderr.write('arun fence: could not parse hook input (%s); allowing\n' % exc)
        sys.exit(0)
    tool = data.get('tool_name', '')
    tin = data.get('tool_input') or {}
    cwd = data.get('cwd') or os.getcwd()
    try:
        phase = int(env('ARUN_PHASE', '-1'))
    except ValueError:
        phase = -1
    if tool in ('Edit', 'MultiEdit', 'Write', 'NotebookEdit'):
        path = tin.get('file_path') or tin.get('notebook_path') or ''
        if not path:
            sys.exit(0)
        where, p = classify(path, cwd)
        if where == 'run':
            sys.exit(0)
        if where == 'protected':
            deny('%s belongs to arun and the maintainer (engine/, spec/); the run writes only the '
                 'repository, scratch/, state/, logs/ and review/' % p)
        if where == 'other':
            deny('%s is outside the run copy %s and its scratch/state/logs/review directories; '
                 'other checkouts and data on this machine are read-only, .git is not touched'
                 % (p, repo_root()))
        if phase < 0:
            deny('ARUN_PHASE is not set; refusing repository edits')
        if not allowed_in_repo(p, phase):
            deny('phase %d may not edit %s: the plan\'s file table ("%s") does not assign it '
                 'to phase %d. If the phase needs it, first add a row for it with this phase to '
                 'that table, then edit it.' % (phase, p, conf('ALLOW_TABLE', '?'), phase))
        sys.exit(0)
    if tool == 'Bash':
        cmd = tin.get('command') or ''
        bad = git_violation(cmd)
        if bad:
            deny('command refused (%s): %s. %s' % (bad, cmd[:200], GIT_HINT))
        for pattern, why in DENY_BASH:
            if re.search(pattern, cmd):
                deny('command refused (%s): %s' % (why, cmd[:200]))
        sys.exit(0)
    sys.exit(0)


# ----------------------------------------------------------------------------- snapshots

def current_changed():
    """Paths that differ from HEAD now: modified, deleted or new (not ignored)."""
    paths = set(p for p in git('diff', '--name-only', 'HEAD').splitlines() if p)
    paths.update(p for p in git('ls-files', '--others', '--exclude-standard').splitlines() if p)
    return paths


def hashes(paths):
    """Content hash of each path in the working tree (None if absent)."""
    paths = sorted(paths)
    present = [p for p in paths if os.path.isfile(os.path.join(repo_root(), p))]
    out = {p: None for p in paths}
    if present:
        shas = git('hash-object', '--stdin-paths', stdin='\n'.join(present) + '\n').split()
        out.update(dict(zip(present, shas)))
    return out


def head_hash(path):
    sha = git('rev-parse', '-q', '--verify', 'HEAD:' + path, check=False).strip()
    return sha or None


def snapshot_files(phase):
    base = os.path.join(state_dir(), 'phase_%d' % phase)
    return base + '.snapshot.json', base + '.start'


def snapshot(phase):
    jpath, mirror = snapshot_files(phase)
    snap = hashes(current_changed())
    if os.path.isdir(mirror):
        shutil.rmtree(mirror)
    os.makedirs(mirror)
    for p, sha in snap.items():
        if sha is not None:
            dst = os.path.join(mirror, p)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(repo_root(), p), dst)
    with open(jpath, 'w') as fh:
        json.dump(snap, fh, indent=1, sort_keys=True)
    print('phase %d snapshot: %d paths differ from HEAD' % (phase, len(snap)))


def load_snapshot(phase):
    jpath, mirror = snapshot_files(phase)
    with open(jpath) as fh:
        return json.load(fh), mirror


def changed_in_phase(phase):
    snap, mirror = load_snapshot(phase)
    now_changed = current_changed()
    now = hashes(now_changed)
    changed = []
    for p in sorted(now_changed | set(snap)):
        start = snap[p] if p in snap else head_hash(p)
        cur = now[p] if p in now_changed else head_hash(p)
        if start != cur:
            changed.append(p)
    return changed, snap, mirror


def check(phase):
    rules = rules_for(phase)
    changed, _, _ = changed_in_phase(phase)
    bad = [p for p in changed if not allowed_in_repo(p, phase, rules)]
    if bad:
        print('phase %d changed files that the plan\'s file table does not assign to it:' % phase)
        for p in bad:
            print('  ' + p)
        return 1
    print('phase %d changed %d files, each assigned to it by the plan\'s file table' % (phase, len(changed)))
    staged = git('diff', '--cached', '--name-only').strip()
    if staged:
        print('the index holds staged changes, which the run never makes:\n' + staged)
        return 1
    return 0


def unified(old, new, rel):
    res = subprocess.run(['diff', '-u', '--label', 'a/' + rel, '--label', 'b/' + rel,
                          old or '/dev/null', new or '/dev/null'],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    return res.stdout


def phasediff(phase, out_path):
    changed, snap, mirror = changed_in_phase(phase)
    with open(out_path, 'w') as out, tempfile.TemporaryDirectory() as tmp:
        for p in changed:
            if p in snap:
                old = os.path.join(mirror, p) if snap[p] is not None else None
            else:
                old = None
                if head_hash(p):
                    old = os.path.join(tmp, 'head_' + p.replace('/', '__'))
                    blob = subprocess.run(['git', '--no-optional-locks', '-C', repo_root(), 'show',
                                           'HEAD:' + p], stdout=subprocess.PIPE, check=True).stdout
                    with open(old, 'wb') as fh:
                        fh.write(blob)
            cur = os.path.join(repo_root(), p)
            new = cur if os.path.isfile(cur) else None
            out.write(unified(old, new, p))
    print('phase %d: %d files in %s' % (phase, len(changed), out_path))


def fulldiff(out_path):
    with open(out_path, 'w') as out:
        out.write(git('diff', '--binary', 'HEAD'))
        for p in sorted(git('ls-files', '--others', '--exclude-standard').splitlines()):
            if not p:
                continue
            res = subprocess.run(['git', '--no-optional-locks', '-C', repo_root(), 'diff',
                                  '--no-index', '--binary', '/dev/null', p],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 universal_newlines=True)
            out.write(res.stdout)
    print('full diff against HEAD in %s' % out_path)


def main(argv):
    mode = argv[1] if len(argv) > 1 else ''
    if mode == 'hook':
        hook()
    elif mode == 'snapshot' and len(argv) == 3:
        snapshot(int(argv[2]))
    elif mode == 'check' and len(argv) == 3:
        sys.exit(check(int(argv[2])))
    elif mode == 'phasediff' and len(argv) == 4:
        phasediff(int(argv[2]), argv[3])
    elif mode == 'fulldiff' and len(argv) == 3:
        fulldiff(argv[2])
    elif mode == 'list' and len(argv) == 3:
        exact, globs, prefixes = rules_for(int(argv[2]))
        for p in sorted(exact):
            print('file   ' + p)
        for g in sorted(globs):
            print('glob   ' + g)
        for d in sorted(prefixes):
            print('dir    ' + d)
    else:
        sys.stderr.write(__doc__)
        sys.exit(64)


if __name__ == '__main__':
    main(sys.argv)
