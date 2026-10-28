#!/usr/bin/env python3
"""arun engine: the disk policy of a run. Runs on the remote machine.

Two parts, both automatic:

  cleanup.py guard         PreToolUse hook (Bash). Below DISK_MIN_GB free on the run's file
                           system it allows only inspection and cleanup commands (rm, du,
                           df, ls, find, ps, kill, cat, tail, grep, ...) and tells the
                           session to free space first; exit 2 blocks, 0 allows.
  cleanup.py sweep PHASE   Called by the driver when PHASE ends DONE: deletes every file
                           over CLEAN_MIN_MB under scratch/ that is not text (logs, scripts,
                           tables, sources), except in build trees (directories holding
                           CMakeCache.txt) and in scratch/keep/. Skipped while any process
                           of the run is still alive. The list goes to
                           logs/cleanup_phase_N.txt.
  cleanup.py report        Free space and the largest folders of scratch/ (debugging).

Configuration (spec/run.conf): DISK_MIN_GB (20), CLEANUP (1; 0 turns the sweep off),
CLEAN_MIN_MB (1).
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fence import conf, run_root  # noqa: E402  (same directory)

# Text the sweep keeps whatever its size: the evidence and the means to rerun.
KEEP_EXT = {
    '.log', '.txt', '.out', '.err', '.md', '.tsv', '.csv', '.json', '.sh', '.py', '.pl',
    '.f90', '.F90', '.inc', '.inf', '.diff', '.patch', '.cmake', '.yaml', '.yml', '.xml',
    '.html', '.cfg', '.conf', '.ini',
}
# What may run while the disk is short: look, find, delete, stop jobs.
CLEAN_CMDS = {
    'rm', 'rmdir', 'unlink', 'du', 'df', 'ls', 'find', 'stat', 'file', 'ps', 'pgrep', 'kill',
    'cd', 'pwd', 'echo', 'printf', 'cat', 'head', 'tail', 'grep', 'egrep', 'wc', 'sort',
    'uniq', 'cut', 'awk', 'sed', 'xargs', 'true', 'date', 'tree', 'basename', 'dirname',
}
SEGMENT_SPLIT = re.compile(r'&&|\|\||[;|\n]')
ASSIGNMENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=\S*$')


def human(nbytes):
    return '%.1f GB' % (nbytes / 1e9) if nbytes >= 1e9 else '%.0f MB' % (nbytes / 1e6)


def gb_free(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1e9


def mount_of(path):
    path = os.path.realpath(path)
    while not os.path.ismount(path):
        path = os.path.dirname(path)
    return path


def disk_min():
    try:
        return float(conf('DISK_MIN_GB', '20'))
    except ValueError:
        return 20.0


def is_cleanup_command(cmd):
    for seg in SEGMENT_SPLIT.split(cmd):
        words = seg.strip().lstrip('({').split()
        while words and ASSIGNMENT.match(words[0]):
            words = words[1:]
        if not words:
            continue
        if os.path.basename(words[0]) not in CLEAN_CMDS:
            return False
    return True


def guard():
    import json
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if data.get('tool_name') != 'Bash':
        sys.exit(0)
    root = run_root()
    free, need = gb_free(root), disk_min()
    if free >= need:
        sys.exit(0)
    cmd = (data.get('tool_input') or {}).get('command') or ''
    if is_cleanup_command(cmd):
        sys.exit(0)
    sys.stderr.write(
        'arun disk guard: only %.1f GB free on %s; the run keeps at least %g GB free. Free space '
        'before anything else: delete the outputs of finished test and benchmark runs under '
        '%s/scratch (volumes, stacks, projects, partial files; keep their logs and scripts) and '
        'build trees you no longer use; `du -h --max-depth=2 %s/scratch | sort -rh | head -25` '
        'shows the largest. Until then only inspection and cleanup commands run (rm, du, df, ls, '
        'find, ps, kill, cat, tail, grep, ...). If the space cannot be freed inside the run '
        'directory, stop the phase (STOPPED: disk full) and say what holds the space.\n'
        % (free, mount_of(root), need, root, root))
    sys.exit(2)


def run_processes():
    """Processes of the run other than this one and its ancestors (the driver)."""
    root = run_root()
    marks = {('ARUN_ROOT=' + root).encode()}
    mine, p = set(), os.getpid()
    while p > 1:
        mine.add(p)
        try:
            with open('/proc/%d/stat' % p) as fh:
                p = int(fh.read().rsplit(')', 1)[1].split()[1])
        except Exception:
            break
    found = []
    for d in os.listdir('/proc'):
        if not d.isdigit() or int(d) in mine:
            continue
        try:
            with open('/proc/%s/environ' % d, 'rb') as fh:
                env = set(fh.read().split(b'\0'))
            if env & marks:
                with open('/proc/%s/cmdline' % d, 'rb') as fh:
                    found.append('%s %s' % (d, fh.read().replace(b'\0', b' ').decode(errors='replace')[:120]))
        except Exception:
            continue
    return found


def bulky_files(scratch, min_bytes):
    keep_dir = os.path.join(scratch, 'keep')
    for top, dirs, files in os.walk(scratch):
        if top == keep_dir or 'CMakeCache.txt' in files:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if os.path.join(top, d) != keep_dir]
        for f in files:
            path = os.path.join(top, f)
            if os.path.splitext(f)[1] in KEEP_EXT or os.path.islink(path):
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size >= min_bytes:
                yield size, path


def sweep(phase):
    root = run_root()
    scratch = os.path.join(root, 'scratch')
    if conf('CLEANUP', '1') == '0':
        print('phase %d cleanup: off (CLEANUP=0)' % phase)
        return
    if not os.path.isdir(scratch):
        return
    alive = run_processes()
    if alive:
        print('phase %d cleanup: skipped, %d processes of the run are still alive:' % (phase, len(alive)))
        for line in alive[:10]:
            print('  ' + line)
        return
    try:
        min_bytes = float(conf('CLEAN_MIN_MB', '1')) * 1e6
    except ValueError:
        min_bytes = 1e6
    before = gb_free(root)
    removed = []
    for size, path in bulky_files(scratch, min_bytes):
        try:
            os.remove(path)
            removed.append((size, path))
        except OSError:
            pass
    removed.sort(reverse=True)
    total = sum(s for s, _ in removed)
    report = os.path.join(root, 'logs', 'cleanup_phase_%d.txt' % phase)
    with open(report, 'w') as fh:
        fh.write('phase %d cleanup: %d files, %s; free %.1f -> %.1f GB on %s\n'
                 % (phase, len(removed), human(total), before, gb_free(root), mount_of(root)))
        for size, path in removed[:5000]:
            fh.write('%10.1f MB  %s\n' % (size / 1e6, os.path.relpath(path, root)))
        if len(removed) > 5000:
            fh.write('... and %d more\n' % (len(removed) - 5000))
    print('phase %d cleanup: freed %s in %d files under scratch/ (list in %s); %.1f GB free'
          % (phase, human(total), len(removed), report, gb_free(root)))


def report():
    root = run_root()
    print('%.1f GB free on %s (minimum %g GB)' % (gb_free(root), mount_of(root), disk_min()))
    scratch = os.path.join(root, 'scratch')
    sizes = {}
    for size, path in bulky_files(scratch, 0):
        rel = os.path.relpath(path, scratch).split(os.sep)
        key = os.sep.join(rel[:2]) if len(rel) > 2 else rel[0]
        sizes[key] = sizes.get(key, 0) + size
    for key, size in sorted(sizes.items(), key=lambda kv: -kv[1])[:25]:
        print('%8.2f GB  scratch/%s (what a sweep would delete)' % (size / 1e9, key))


def main(argv):
    mode = argv[1] if len(argv) > 1 else ''
    if mode == 'guard':
        guard()
    elif mode == 'sweep' and len(argv) == 3:
        sweep(int(argv[2]))
    elif mode == 'report':
        report()
    else:
        sys.stderr.write(__doc__)
        sys.exit(64)


if __name__ == '__main__':
    main(sys.argv)
