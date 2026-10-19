# Send a Cursor agent response to the daemon as a stop_event.
# stdin = cursor_normalize.py's agent_response JSON; argv[1] = socket path.

def main():
    import os as _os
    import signal as _signal
    # Wall-clock ceiling, self-enforced. GNU coreutils 'timeout' is absent on stock
    # macOS, where wrapping this invocation in it meant the hook delivered nothing
    # at all. Armed before any work so it bounds file reads too, not just the
    # socket. Exit 75 (EX_TEMPFAIL) marks a ceiling hit.
    _signal.signal(_signal.SIGALRM, lambda *_a: _os._exit(75))
    _signal.setitimer(_signal.ITIMER_REAL, 8)
    import json
    import socket
    import sys
    import time
    import uuid

    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        return 0
    content = (data.get('content') or '').strip()
    if not content:
        return 0
    payload = {
        'command': 'stop_event',
        'session_id': data.get('session_id') or 'default',
        'content': content,
        'transcript_path': data.get('transcript_path') or '',
        'stop_hook_active': bool(data.get('stop_hook_active', False)),
        'event_id': str(uuid.uuid4()),
        'ts': time.time(),
    }
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3.0)
    try:
        s.connect(sys.argv[1])
        s.sendall((json.dumps(payload) + '\n').encode())
        s.recv(4096)
    finally:
        s.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
