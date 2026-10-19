# Build + send stop_event. stdin = hook payload; argv[1] = socket path.

def main():
    import os as _os
    import signal as _signal
    # Wall-clock ceiling, self-enforced. GNU coreutils 'timeout' is absent on stock
    # macOS, where wrapping this invocation in it meant the hook delivered nothing
    # at all. Armed before any work so it bounds file reads too, not just the
    # socket. Exit 75 (EX_TEMPFAIL) marks a ceiling hit.
    _signal.signal(_signal.SIGALRM, lambda *_a: _os._exit(75))
    _signal.setitimer(_signal.ITIMER_REAL, 8)
    import sys
    import json
    import socket
    import time
    import uuid
    import os
    from pathlib import Path

    raw = sys.stdin.read()

    # Try to parse as JSON (Stop/Notification hook payload). If not JSON, treat raw as content.
    try:
        payload = json.loads(raw)
        session_id = payload.get('session_id', 'default')
        transcript_path = payload.get('transcript_path', '')
        stop_hook_active = bool(payload.get('stop_hook_active', False))
    except Exception:
        payload = {}
        session_id = os.environ.get('CLAUDE_SESSION_ID', 'default')
        transcript_path = ''
        stop_hook_active = False

    # Extract latest assistant message from transcript JSONL.
    content = ''
    if payload.get('hook_event_name') == 'Notification':
        # A Notification carries transcript_path too, but its own text is what to speak.
        content = payload.get('message') or ''
    elif transcript_path and Path(transcript_path).exists():
        try:
            # Tail the file — the latest assistant entry is near the end.
            with open(transcript_path, 'r') as f:
                lines = f.readlines()
            # Walk backwards to find the most recent role=assistant text.
            for line in reversed(lines[-50:]):  # last 50 lines is plenty
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                # Multiple Claude Code transcript shapes — try each.
                if rec.get('role') == 'assistant' or rec.get('type') == 'assistant':
                    msg = rec.get('message') or rec.get('content') or ''
                    if isinstance(msg, dict):
                        parts = msg.get('content', [])
                        if isinstance(parts, list):
                            for p in parts:
                                if isinstance(p, dict) and p.get('type') == 'text':
                                    content = p.get('text', '')
                                    break
                                elif isinstance(p, str):
                                    content = p
                                    break
                        elif isinstance(parts, str):
                            content = parts
                    elif isinstance(msg, str):
                        content = msg
                    if content:
                        break
        except Exception as e:
            sys.stderr.write(f'transcript-read failed: {e}\n')
    elif raw and not raw.startswith('{'):
        # Notification path: raw stdin IS the content.
        content = raw

    if not content:
        # Nothing useful to classify; bail silently.
        sys.exit(0)

    # Build stop_event payload (PRIMARY path — no shadow flag).
    payload_out = {
        'command': 'stop_event',
        'session_id': session_id,
        'content': content,
        'transcript_path': transcript_path,
        'stop_hook_active': stop_hook_active,
        'event_id': str(uuid.uuid4()),
        'ts': time.time(),
    }

    # Subagent markers (Claude Code stamps these inside Agent/Workflow subagents;
    # Stop becomes SubagentStop there). Forward them so the daemon's
    # 'subagent context: silent by policy' gate applies to stop events too.
    if os.environ.get('NOTIF_KIND'):
        payload_out['notification_kind'] = os.environ['NOTIF_KIND']
    for k in ('agent_id', 'agent_type'):
        if isinstance(payload, dict) and payload.get(k):
            payload_out[k] = payload[k]
    # The session's folder: the daemon files every spoken line under it. A session
    # that only ever speaks at Stop (any headless run) otherwise never has one.
    payload_out['cwd'] = (payload.get('cwd') if isinstance(payload, dict) else None) or os.getcwd()

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3.0)
    try:
        s.connect(sys.argv[1])
        s.sendall((json.dumps(payload_out) + '\n').encode())
        ack = s.recv(4096)
        # Only the child learns whether delivery happened, so only the child may
        # claim it. An ack in hand is the evidence; nothing else prints success.
        sys.stderr.write('stop_event DELIVERED ack=%r\n' % (ack[:120],) if ack
                         else 'stop_event FAILED: connected but no ack\n')
    except OSError as exc:
        sys.stderr.write('stop_event FAILED: %s: %s\n' % (type(exc).__name__, exc))
    finally:
        s.close()


if __name__ == "__main__":
    main()
