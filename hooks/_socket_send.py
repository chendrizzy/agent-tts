"""Unix-socket client for the hooks. Values arrive as argv, never spliced into code.

  speak <socket> <session_id> <priority> <source> <text>   fire-and-forget speak
  raw   <socket> <payload>                                  send line, log ack on stderr
  health <socket>                                           print health JSON, exit 1 on error
"""
import os
import signal
import socket
import sys
import json

def main():

    # Wall-clock ceiling, self-enforced. GNU coreutils 'timeout' is absent on stock
    # macOS, where wrapping this invocation in it meant the hook delivered nothing
    # at all. Armed before any work. Exit 75 (EX_TEMPFAIL) marks a ceiling hit.
    mode, path = sys.argv[1], sys.argv[2]
    signal.signal(signal.SIGALRM, lambda *_a: os._exit(75))
    signal.setitimer(signal.ITIMER_REAL, {"speak": 3, "raw": 5, "health": 1.0}[mode])

    if mode == "speak":
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect(path)
            s.sendall(json.dumps({
                "command": "speak", "text": sys.argv[6], "session_id": sys.argv[3],
                "priority": int(sys.argv[4]), "source": sys.argv[5],
            }).encode("utf-8"))
            s.close()
        except Exception:
            pass
    elif mode == "raw":
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3.0)
        try:
            s.connect(path)
            s.sendall(sys.argv[3].encode() + b"\n")
            ack = s.recv(4096)
            sys.stderr.write("tool_event DELIVERED ack=%r\n" % (ack[:120],) if ack
                             else "tool_event FAILED: connected but no ack\n")
        except OSError as exc:
            sys.stderr.write("tool_event FAILED: %s: %s\n" % (type(exc).__name__, exc))
        finally:
            s.close()
    else:  # health
        try:
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(0.5)
            s.connect(path)
            s.sendall(b'{"command":"health"}\n')
            data = s.recv(8192)
            s.close()
            print(data.decode("utf-8"))
        except Exception:
            sys.exit(1)


if __name__ == "__main__":
    main()
