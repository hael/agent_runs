#!/usr/bin/env python3
"""bridge/run.py: the sandbox side of the build bridge (see serve.sh for the protocol).

    run.py status                          the server's heartbeat age, queue and the last results
    run.py run    [opts] -- CMD...         submit CMD (one shell command line) and wait for it
    run.py run    [opts] < script.sh       submit a whole script read from stdin and wait
    run.py submit [opts] -- CMD... | < script.sh      submit only; prints the request id
    run.py wait   ID [--max S] [--tail N]  wait for ID (default 540 s), print the log tail and the rc
    run.py log    ID [--tail N]            the log as it is now
    run.py stop | kill                     stop the server | kill the running job

options of run/submit: --cwd DIR (default ~/src/SIMPLE)  --timeout S (job limit on the server, default 7200)
                       --max S (how long this call waits, default 540; a tool call is capped at 600)
                       --tail N (log lines printed, default 60)  --name TAG (part of the id)
exit status: the job's rc; 124 still running when --max expired (call `wait ID` again); 125 server dead;
             126 no server heartbeat at all.
The bridge directory is var/bridge beside this file's parent (override with BRIDGE_DIR), so the same
paths work on the machine and through the sandbox's mount.
"""
import argparse
import os
import random
import string
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.environ.get('BRIDGE_DIR') or os.path.join(os.path.dirname(HERE), 'var', 'bridge')
STALE = 30  # seconds without a heartbeat: the server is dead


def p(*parts):
    return os.path.join(BRIDGE, *parts)


def heartbeat_age():
    try:
        with open(p('heartbeat')) as f:
            return time.time() - float(f.read().strip() or 0)
    except (OSError, ValueError):
        return None


def tail(path, n):
    try:
        with open(path, errors='replace') as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    return lines[-n:] if n > 0 else lines


def new_id(tag):
    stamp = time.strftime('%Y%m%d-%H%M%S')
    rnd = ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(4))
    tag = ''.join(c if c.isalnum() or c in '-_' else '_' for c in (tag or 'job'))[:24]
    return '%s-%s-%s' % (stamp, tag, rnd)


def submit(body, cwd, timeout, tag):
    for d in ('requests', 'running', 'done', 'results'):
        os.makedirs(p(d), exist_ok=True)
    rid = new_id(tag)
    script = '#!/usr/bin/env bash\n#cwd: %s\n#timeout: %d\nset -o pipefail\n%s\n' % (cwd, timeout, body.rstrip('\n'))
    part = p('requests', rid + '.sh.part')
    with open(part, 'w') as f:
        f.write(script)
    os.replace(part, p('requests', rid + '.sh'))
    return rid


def state_of(rid):
    if os.path.exists(p('results', rid + '.rc')):
        return 'done'
    if os.path.exists(p('running', rid + '.sh')):
        return 'running'
    if os.path.exists(p('requests', rid + '.sh')):
        return 'queued'
    return 'unknown'


def wait(rid, max_s, ntail):
    t0 = time.time()
    last_report = t0
    while True:
        st = state_of(rid)
        if st == 'done':
            break
        age = heartbeat_age()
        if age is None:
            print('bridge: no server heartbeat in %s (start serve.sh on the machine)' % BRIDGE)
            return 126
        if age > STALE:
            print('bridge: server heartbeat is %.0f s old: the server is dead; request %s is %s' % (age, rid, st))
            return 125
        if time.time() - t0 >= max_s:
            print('bridge: %s still %s after %.0f s; the log so far:' % (rid, st, time.time() - t0))
            for line in tail(p('results', rid + '.log'), ntail):
                print(line)
            print('bridge: call `run.py wait %s` to keep waiting' % rid)
            return 124
        if time.time() - last_report >= 60:
            last = tail(p('results', rid + '.log'), 1)
            print('bridge: %s %s, %.0f s ... %s' % (rid, st, time.time() - t0, last[0] if last else ''))
            sys.stdout.flush()
            last_report = time.time()
        time.sleep(2)
    with open(p('results', rid + '.rc')) as f:
        rc = int(f.read().strip() or 1)
    elapsed = ''
    try:
        with open(p('results', rid + '.start')) as f1, open(p('results', rid + '.end')) as f2:
            elapsed = ' in %d s' % (int(f2.read().strip()) - int(f1.read().strip()))
    except (OSError, ValueError):
        pass
    lines = tail(p('results', rid + '.log'), ntail)
    total = len(tail(p('results', rid + '.log'), 0))
    if total > len(lines):
        print('bridge: ... %d earlier lines in %s' % (total - len(lines), p('results', rid + '.log')))
    for line in lines:
        print(line)
    print('bridge: %s rc=%d%s' % (rid, rc, elapsed))
    return rc


def status():
    age = heartbeat_age()
    if age is None:
        print('server: no heartbeat (not running) in %s' % BRIDGE)
    else:
        print('server: heartbeat %.0f s ago (%s)' % (age, 'alive' if age <= STALE else 'DEAD'))
    try:
        with open(p('environment.txt')) as f:
            print('environment:', ' | '.join(l.strip() for l in f if l.strip())[:400])
    except OSError:
        pass
    for d in ('requests', 'running'):
        try:
            items = sorted(x[:-3] for x in os.listdir(p(d)) if x.endswith('.sh'))
        except OSError:
            items = []
        print('%-9s %s' % (d + ':', ' '.join(items) if items else '-'))
    try:
        rcs = sorted(x for x in os.listdir(p('results')) if x.endswith('.rc'))[-5:]
    except OSError:
        rcs = []
    for x in rcs:
        with open(p('results', x)) as f:
            print('result:   %s rc=%s' % (x[:-3], f.read().strip()))
    return 0


def main():
    argv = sys.argv[1:]
    cmd = None
    if '--' in argv:
        i = argv.index('--')
        cmd, argv = ' '.join(argv[i + 1:]), argv[:i]
    ap = argparse.ArgumentParser(add_help=True, usage=__doc__)
    ap.add_argument('verb', choices=['status', 'run', 'submit', 'wait', 'log', 'stop', 'kill'])
    ap.add_argument('rest', nargs='*')
    ap.add_argument('--cwd', default='~/src/SIMPLE')
    ap.add_argument('--timeout', type=int, default=7200)
    ap.add_argument('--max', type=float, default=540)
    ap.add_argument('--tail', type=int, default=60)
    ap.add_argument('--name', default='')
    a = ap.parse_args(argv)
    if a.verb == 'status':
        return status()
    if a.verb in ('stop', 'kill'):
        os.makedirs(p('requests'), exist_ok=True)
        open(p('requests', a.verb.upper()), 'w').close()
        print('bridge: %s requested' % a.verb)
        return 0
    if a.verb in ('run', 'submit'):
        body = cmd if cmd is not None else sys.stdin.read()
        if not body.strip():
            print('bridge: nothing to run (give a command after -- or a script on stdin)')
            return 2
        tag = a.name or body.strip().split()[0].split('/')[-1]
        rid = submit(body, a.cwd, a.timeout, tag)
        print('bridge: submitted %s' % rid)
        if a.verb == 'submit':
            return 0
        return wait(rid, a.max, a.tail)
    if not a.rest:
        print('bridge: %s needs a request id' % a.verb)
        return 2
    rid = a.rest[0]
    if a.verb == 'wait':
        return wait(rid, a.max, a.tail)
    for line in tail(p('results', rid + '.log'), a.tail):
        print(line)
    print('bridge: %s is %s' % (rid, state_of(rid)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
