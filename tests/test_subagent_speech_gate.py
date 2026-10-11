"""Subagent speech gate + Bash error-snippet quality (2026-09-02).

Why these exist — the "In <project>, Traceback (most recent call last):" storm:

  1. ContentRouter never looked at ``agent_id`` / ``agent_type``. Claude Code
     adds both to EVERY hook payload fired inside an Agent/Workflow subagent, so
     subagent tool telemetry was classified exactly like the main session.
     Policy now: subagent context is silent — the finding reaches the user via
     the parent's prose (Stop path), the same north star as _extract_grep.
  2. The Bash ERROR branch had no dedupe. Both ~/.claude/settings.json and the
     installed plugin's hooks.json deliver every hook event, so each error was
     spoken twice (spoken log: one copy labeled with the subagent hex id, one
     labeled "Claude Code", 6-10 s apart).
  3. ``_extract_error_snippet`` returned the FIRST keyword line: for a traceback
     that is the useless header; for a ``sed -n``/``cat``/``grep -rn`` of Python
     source it is "except Exception:" / "raise" / a docstring line.

Evidence (7 days of spoken logs before the fix): 1335 utterances, 130 were the
bare traceback header, ~136 were Python source lines, 320 carried a subagent id.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from daemon.content_router import ContentRouter  # noqa: E402
from daemon.providers.ollama_provider import OllamaProvider  # noqa: E402
from daemon.tts_types import Category  # noqa: E402


class _Summarizer:
    async def summarize(self, content, category, context_hint="", allow_fallback=True):
        return "[mock summary]"


def _router() -> ContentRouter:
    return ContentRouter(config={}, provider=OllamaProvider(_Summarizer()))


def _bash(stdout: str = "", stderr: str = "", command: str = "python3 run.py 2>&1",
          event_id: str = "evt-1", **extra) -> dict:
    ev = {
        "command": "tool_event",
        "phase": "post",
        "event_id": event_id,
        "ts": 1.0,
        "session_id": "sess-main",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {
            "stdout": stdout, "stderr": stderr, "interrupted": False,
            "isImage": False, "noOutputExpected": False,
        },
        "tool_use_id": "toolu_1",
        "transcript_path": "",
        "cwd": "/tmp",
    }
    ev.update(extra)
    return ev


def _stop(content: str, **extra) -> dict:
    ev = {
        "command": "stop_event",
        "event_id": "stop-1",
        "ts": 1.0,
        "session_id": "sess-main",
        "content": content,
        "transcript_path": "",
        "stop_hook_active": False,
    }
    ev.update(extra)
    return ev


TRACEBACK = (
    'Traceback (most recent call last):\n'
    '  File "/Users/x/proj/run.py", line 3, in <module>\n'
    '    import foo\n'
    "ModuleNotFoundError: No module named 'foo'\n"
)

CHAINED_TRACEBACK = (
    'Traceback (most recent call last):\n'
    '  File "a.py", line 2, in <module>\n'
    '    d["k"]\n'
    "KeyError: 'k'\n"
    '\n'
    'During handling of the above exception, another exception occurred:\n'
    '\n'
    'Traceback (most recent call last):\n'
    '  File "a.py", line 4, in <module>\n'
    '    raise RuntimeError("wrapped") from e\n'
    'RuntimeError: wrapped\n'
)

SOURCE_LISTING = (
    'class Project:\n'
    '    """A skin project: base sources + custom artwork."""\n'
    '\n'
    '    @classmethod\n'
    '    def from_dict(cls, d):\n'
    '        try:\n'
    '            return cls(**d)\n'
    '        except Exception as e:\n'
    '            log.warning("bad dict: %s", e)\n'
    '            raise\n'
    '        except Exception:\n'
    '            pass\n'
)


def _classify(router, ev):
    return asyncio.run(router.classify_event(ev))


# ---------------------------------------------------------------- 1. policy

def test_subagent_tool_event_is_silent_by_policy():
    d = _classify(_router(), _bash(TRACEBACK, agent_id="aafd19c6c3f57bfc9",
                                    agent_type="general-purpose"))
    assert d.should_speak is False
    assert "subagent" in d.raw_excerpt.lower()


def test_agent_type_alone_still_speaks():
    # agent_type is ALSO stamped on a main session started with `claude --agent
    # <name>` (docs), so it must not gate; only agent_id / the transcript path do.
    d = _classify(_router(), _bash(TRACEBACK, agent_type="security-reviewer"))
    assert d.should_speak is True and d.category == Category.ERROR


def test_subagent_stop_event_is_silent():
    d = _classify(_router(), _stop("Audit complete. All findings grounded in evidence.",
                                    agent_id="ab209eb861a508d8b"))
    assert d.should_speak is False
    assert "subagent" in d.raw_excerpt.lower()


def test_main_session_final_answer_still_speaks():
    d = _classify(_router(), _stop("The fix is in and the full suite passes."))
    assert d.should_speak is True
    assert d.category in (Category.FINAL_ANSWER, Category.INSIGHT)


# ------------------------------------------------- 2. duplicate delivery

def test_duplicate_error_delivery_is_silenced():
    r = _router()
    first = _classify(r, _bash(TRACEBACK, event_id="copy-a"))
    second = _classify(r, _bash(TRACEBACK, event_id="copy-b"))
    assert first.should_speak is True and first.category == Category.ERROR
    assert second.should_speak is False
    assert "duplicate" in second.raw_excerpt.lower()
    # Another session hitting the identical error is its own news …
    other = _classify(r, _bash(TRACEBACK, event_id="copy-c", session_id="sess-other"))
    assert other.should_speak is True
    # … and so is the same session once the window has passed.
    r._recent_errors[("sess-main", list(r._recent_errors)[0][1])] -= 10_000
    again = _classify(r, _bash(TRACEBACK, event_id="copy-d"))
    assert again.should_speak is True


# --------------------------------------------- 3. error-snippet quality

def test_main_session_traceback_speaks_exception_line_not_header():
    d = _classify(_router(), _bash(TRACEBACK))
    assert d.should_speak is True
    assert d.category == Category.ERROR
    assert d.content == "ModuleNotFoundError: No module named 'foo'"


def test_chained_traceback_speaks_last_exception():
    d = _classify(_router(), _bash(CHAINED_TRACEBACK))
    assert d.should_speak is True
    assert d.content == "RuntimeError: wrapped"


def test_stderr_traceback_speaks_exception_line():
    d = _classify(_router(), _bash(stdout="", stderr=TRACEBACK))
    assert d.should_speak is True
    assert d.category == Category.ERROR
    assert d.content == "ModuleNotFoundError: No module named 'foo'"


def test_traceback_header_alone_is_never_spoken():
    # `... 2>&1 | head -1` leaves only the header: nothing worth saying.
    d = _classify(_router(), _bash("Traceback (most recent call last):\n"))
    assert not (d.should_speak and "Traceback" in d.content)


def test_source_listing_is_not_an_error():
    d = _classify(_router(), _bash(SOURCE_LISTING, command="sed -n 30,80p daemon/x.py"))
    assert not (d.should_speak and d.category == Category.ERROR)
    assert d.content.strip() not in {"except Exception:", "except Exception as e:", "raise", "try:"}


def test_grep_rn_output_is_not_an_error():
    out = ("daemon/x.py:12:        except Exception:\n"
           "daemon/y.py:40:        except Exception as e:\n")
    d = _classify(_router(), _bash(out, command='grep -rn "except Exception" daemon/'))
    assert not (d.should_speak and d.category == Category.ERROR)


def test_column0_exception_line_still_an_error():
    # Tail of a traceback after `| tail -1`: only the exception line survives.
    d = _classify(_router(), _bash("ValueError: invalid literal for int() with base 10: 'x'\n"))
    assert d.should_speak is True and d.category == Category.ERROR
    assert d.content.startswith("ValueError: invalid literal")


def test_java_and_node_error_lines_still_detected():
    java = 'Exception in thread "main" java.lang.IllegalStateException: boom\n\tat Foo.main(Foo.java:3)\n'
    node = "TypeError: x is not a function\n    at Object.<anonymous> (/app/a.js:1:1)\n"
    for out in (java, node):
        d = _classify(_router(), _bash(out))
        assert d.should_speak is True and d.category == Category.ERROR, out
        assert d.content == out.splitlines()[0]


def test_no_such_file_speaks_the_trigger_line():
    out = "some preamble line\ncat: foo.json: No such file or directory\n"
    d = _classify(_router(), _bash(out))
    assert d.should_speak is True and d.category == Category.ERROR
    assert d.content == "cat: foo.json: No such file or directory"


# ------------------------------------------ 4. traceback caret markup

def test_traceback_caret_line_normalizes_to_nothing():
    # Py3.11+ tracebacks underline the failing expression with ~~~~^^; two such
    # lines sat in the live shadow.log as should_speak excerpts (2026-08-24).
    from daemon.text_utils import normalize_for_speech
    assert normalize_for_speech("~~~~^^").strip() == ""
    assert normalize_for_speech("    ~~~~~~~^^^^^^~~").strip() == ""
    assert normalize_for_speech("x = a^b").strip() == "x = a^b"  # infix caret kept


def test_compiler_and_osascript_diagnostics_still_detected():
    swift = "main.swift:210:71: error: value of optional type 'AudioDeviceIOProcID?' must be unwrapped\n"
    clang = "a.c:3:5: fatal error: 'foo.h' file not found\n"
    osa = 'procs: 40:103: execution error: Can\u2019t get window 1. (-1728)\n'
    for out in (swift, clang, osa):
        d = _classify(_router(), _bash(out, command="swift build 2>&1"))
        assert d.should_speak is True and d.category == Category.ERROR, out
        assert d.content == out.strip()


def test_subagent_transcript_path_alone_is_silent():
    # A hook copy that dropped agent_id still forwards transcript_path; Claude
    # Code keeps subagent transcripts under <session>/subagents/.
    tp = "/Users/x/.claude/projects/-p/sess/subagents/workflows/wf_1/agent-abc.jsonl"
    d = _classify(_router(), _bash(TRACEBACK, transcript_path=tp))
    assert d.should_speak is False and "subagent" in d.raw_excerpt.lower()
    d2 = _classify(_router(), _stop("Done. Findings attached.", transcript_path=tp))
    assert d2.should_speak is False


def test_stop_hook_forwards_subagent_markers(tmp_path):
    """Run the REAL speech_output_hook.sh against a fake socket and assert the
    daemon-bound payload carries agent_id/agent_type when the Stop payload had
    them (and omits them otherwise)."""
    import json, os, shutil, socket, subprocess, tempfile, threading
    hook = PROJECT_ROOT / "hooks" / "speech_output_hook.sh"
    tr = tmp_path / "t.jsonl"
    tr.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant",
                  "content": [{"type": "text", "text": "hello from a subagent"}]}}) + "\n")
    # AF_UNIX paths are capped at ~104 bytes on macOS; pytest's tmp_path is deeper.
    sock_dir = tempfile.mkdtemp(prefix="ttsg-", dir="/tmp")
    sock_path = os.path.join(sock_dir, "s.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); srv.bind(sock_path); srv.listen(2)
    got = []
    def serve(n):
        for _ in range(n):
            c, _a = srv.accept(); got.append(c.recv(65536)); c.sendall(b"{}\n"); c.close()
    th = threading.Thread(target=serve, args=(2,), daemon=True); th.start()
    env = dict(os.environ, CLAUDE_TTS_SOCKET=sock_path, HOME=str(tmp_path))
    for extra in ({"agent_id": "abc123", "agent_type": "Explore"}, {}):
        payload = {"session_id": "s1", "transcript_path": str(tr), "stop_hook_active": False, **extra}
        subprocess.run(["sh", str(hook)], input=json.dumps(payload).encode(), env=env,
                       capture_output=True, timeout=20)
    th.join(timeout=15)
    srv.close(); shutil.rmtree(sock_dir, ignore_errors=True)
    assert len(got) == 2, got
    sent = [json.loads(g.decode().strip()) for g in got]
    with_markers = [m for m in sent if m.get("agent_id")]
    assert len(with_markers) == 1 and with_markers[0]["agent_type"] == "Explore"
    assert all("agent_id" not in m for m in sent if m is not with_markers[0])


# --------------------------------------- 5. round-two audit findings

def test_truncated_traceback_names_the_frame_not_the_header():
    out = 'Traceback (most recent call last):\n  File "/Users/x/run.py", line 3, in <module>\n    main()\n'
    d = _classify(_router(), _bash(out))
    assert d.should_speak is True and d.category == Category.ERROR
    assert "Traceback" not in d.content
    assert d.content == "error at run.py line 3"
    # interrupted partial traceback with only the header
    ev = _bash("Traceback (most recent call last):\n")
    ev["tool_response"]["interrupted"] = True
    d2 = _classify(_router(), ev)
    assert "Traceback" not in d2.content


def test_exception_group_traceback_speaks_the_group_line():
    out = ('  + Exception Group Traceback (most recent call last):\n'
           '  |   File "/app/a.py", line 9, in <module>\n'
           '  |     raise ExceptionGroup("eg", [ValueError(1), TypeError(2)])\n'
           '  | ExceptionGroup: eg (2 sub-exceptions)\n'
           '  +-+---------------- 1 ----------------\n'
           '    | ValueError: 1\n'
           '    +------------------------------------\n')
    for kw in ({"stdout": out}, {"stderr": out}):
        d = _classify(_router(), _bash(**kw))
        assert d.should_speak is True and d.category == Category.ERROR, kw
        assert d.content == "ExceptionGroup: eg (2 sub-exceptions)"


def test_docker_prefixed_traceback_speaks_the_exception_line():
    out = ('web-1  | Traceback (most recent call last):\n'
           'web-1  |   File "/app/app.py", line 12, in <module>\n'
           'web-1  |     cfg = load()\n'
           'web-1  | ValueError: bad config\n'
           'web-1  | 172.18.0.1 - - [02/Sep/2026] "GET / HTTP/1.1" 500 -\n')
    d = _classify(_router(), _bash(out, command="docker compose logs web 2>&1 | tail -20"))
    assert d.should_speak is True and d.content == "ValueError: bad config"


def test_syntax_error_mentioning_except_still_detected():
    for out in ("SyntaxError: expected 'except' or 'finally' block\n",
                "SyntaxError: default 'except:' must be last\n"):
        d = _classify(_router(), _bash(out))
        assert d.should_speak is True and d.category == Category.ERROR, out
        assert d.content == out.strip()


def test_uppercase_error_log_lines_detected_but_prose_is_not():
    for out in ("2026-08-31 11:46:56 [/Library/Audio/Plug-Ins/VST] ERROR: failed to copy 'x.vst'\n",
                "ERROR:root:database unreachable\n",
                "[ERROR] build failed at the link step\n"):
        d = _classify(_router(), _bash(out))
        assert d.should_speak is True and d.category == Category.ERROR, out
    prose = "This module owns error handling; see the notes on error recovery.\n"
    d = _classify(_router(), _bash(prose))
    assert not (d.should_speak and d.category == Category.ERROR)


def test_flake8_e999_line_detected_but_grep_except_line_is_not():
    d = _classify(_router(), _bash("x.py:1:5: E999 SyntaxError: invalid syntax\n", command="flake8 x.py"))
    assert d.should_speak is True and d.category == Category.ERROR
    d2 = _classify(_router(), _bash("daemon/x.py:12:        except Exception:\n", command="grep -rn except daemon/"))
    assert not (d2.should_speak and d2.category == Category.ERROR)


def test_api_boilerplate_final_answers_are_dropped():
    for msg in ("Credit balance is too low", "API Error: 529 overloaded_error",
                "You've hit your usage limit. Try again at 3pm."):
        d = _classify(_router(), _stop(msg))
        assert d.should_speak is False, msg
        assert "boilerplate" in d.raw_excerpt
    d = _classify(_router(), _stop("Fixed the API error handling; the retry path now backs off."))
    assert d.should_speak is True


def test_test_count_does_not_span_newlines():
    out = "checked config at line 3\nfailed to connect to db\n"
    d = _classify(_router(), _bash(out))
    assert "tests failing" not in d.content
