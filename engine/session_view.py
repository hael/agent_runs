#!/usr/bin/env python3
"""arun engine: a readable view of what a run's current Claude session is doing.

Fed over ssh by arun (status, watch, sync), on the remote machine:
    ssh <host> "env ARUN_ROOT=<run root> python3 - [--follow] [--last N]" < session_view.py
Reads the newest session log in $ARUN_ROOT/logs and prints one line per message, tool
call and tool error. --follow keeps printing as the session goes on, switches to the
next log when a new session or phase starts, and ends with the ssh link.
"""
import glob
import json
import os
import re
import sys
import time

ROOT = os.path.expanduser(os.environ.get('ARUN_ROOT') or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
WIDTH = 160


def one_line(text, width=WIDTH):
    text = re.sub(r'\s+', ' ', str(text)).strip()
    return text if len(text) <= width else text[:width - 3] + '...'


def newest_log():
    logs = [p for p in glob.glob(os.path.join(ROOT, 'logs', 'phase_*.jsonl'))]
    return max(logs, key=os.path.getmtime) if logs else None


def describe_tool(name, inp):
    inp = inp or {}
    if name == 'Bash':
        s = 'bash   ' + one_line(inp.get('command', ''), WIDTH - 7)
        if inp.get('run_in_background'):
            s += '   [background]'
        return s
    if name in ('Edit', 'MultiEdit'):
        return 'edit   ' + inp.get('file_path', '?')
    if name == 'Write':
        return 'write  ' + inp.get('file_path', '?')
    if name == 'Read':
        return 'read   ' + inp.get('file_path', '?')
    if name in ('Grep', 'Glob'):
        return '%-6s %s %s' % (name.lower(), inp.get('pattern', ''), inp.get('path', '') or '')
    if name in ('Task', 'Agent'):
        return 'agent  ' + one_line(inp.get('description', '') or inp.get('prompt', ''), WIDTH - 7)
    if name == 'TodoWrite':
        todos = inp.get('todos') or []
        done = sum(1 for t in todos if t.get('status') == 'completed')
        active = [t.get('content', '') for t in todos if t.get('status') == 'in_progress']
        return 'todo   %d/%d done%s' % (done, len(todos), ('; now: ' + one_line(active[0], 100)) if active else '')
    return '%-6s %s' % (name, one_line(json.dumps(inp), WIDTH - 7))


def result_text(content):
    if isinstance(content, list):
        return ' '.join(c.get('text', '') for c in content if isinstance(c, dict))
    return str(content or '')


def render(ev):
    out = []
    kind = ev.get('type')
    if kind == 'system' and ev.get('subtype') == 'init':
        out.append('-- session %s (%s)' % (ev.get('session_id', '?'), ev.get('model', '?')))
    elif kind == 'assistant':
        for block in (ev.get('message') or {}).get('content') or []:
            btype = block.get('type')
            if btype == 'text' and block.get('text', '').strip():
                out.append('say    ' + one_line(block['text'], WIDTH - 7))
            elif btype == 'tool_use':
                out.append(describe_tool(block.get('name', '?'), block.get('input')))
    elif kind == 'user':
        content = (ev.get('message') or {}).get('content')
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'tool_result' and block.get('is_error'):
                    out.append('  ! ' + one_line(result_text(block.get('content')), WIDTH - 4))
    elif kind == 'result':
        out.append('-- session ended: %s, %s turns, cost %s' % (
            ev.get('subtype', '?'), ev.get('num_turns', '?'), ev.get('total_cost_usd', '?')))
    return out


def lines_of(path, start=0):
    with open(path, errors='replace') as fh:
        fh.seek(start)
        data = fh.read()
        pos = fh.tell()
    # keep an incomplete last line for the next read
    if data and not data.endswith('\n'):
        cut = data.rfind('\n') + 1
        pos -= len(data) - cut
        data = data[:cut]
    events = []
    for line in data.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    return events, pos


def header(path):
    m = re.search(r'phase_(\d+)_', os.path.basename(path))
    phase = m.group(1) if m else '?'
    print('== phase %s, %s' % (phase, path))
    status = os.path.join(ROOT, 'state', 'phase_%s.status' % phase)
    if os.path.exists(status):
        with open(status) as fh:
            print('   status: ' + one_line(fh.readline()))


def main(argv):
    follow = '--follow' in argv
    last = 30
    if '--last' in argv:
        last = int(argv[argv.index('--last') + 1])
    path = newest_log()
    if not path:
        print('no session log yet in %s/logs' % ROOT)
        return
    header(path)
    events, pos = lines_of(path)
    rendered = [l for ev in events for l in render(ev)]
    for l in rendered[-last:]:
        print(l)
    sys.stdout.flush()
    while follow:
        time.sleep(3)
        if os.getppid() == 1:      # the ssh link is gone
            return
        newer = newest_log()
        if newer and newer != path:
            path, pos = newer, 0
            print()
            header(path)
        events, pos = lines_of(path, pos)
        for ev in events:
            for l in render(ev):
                print(l)
        sys.stdout.flush()


if __name__ == '__main__':
    try:
        main(sys.argv)
    except (KeyboardInterrupt, BrokenPipeError):
        pass
