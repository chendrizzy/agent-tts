import asyncio
import sys

from daemon.pipeline.kokoro_engine import KokoroEngine
from daemon.pipeline.process_stage import ProcessStage


def test_hung_worker_is_killed_and_respawned(tmp_path):
    async def run():
        eng = KokoroEngine(synth_timeout_s=0.3)
        spawned = []

        async def fake_spawn():
            # reads stdin forever, never answers
            eng._proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import sys; sys.stdin.read()",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
            spawned.append(eng._proc)
            return True

        eng._spawn = fake_spawn
        assert await eng.synthesize("hi", str(tmp_path / "a.wav")) is False
        assert spawned[0].returncode is not None  # killed, not left hung
        assert not eng._is_alive()
        assert await eng.synthesize("again", str(tmp_path / "b.wav")) is False
        assert len(spawned) == 2  # respawned for the next chunk
        await eng._kill()

    asyncio.run(run())


def _clean(t):
    return ProcessStage()._restore_contractions(t)


def test_phrases_not_mangled():
    assert _clean("I have to restart the daemon.") == "I have to restart the daemon."
    assert _clean("I had to revert it.") == "I had to revert it."
    assert _clean("Yes, it is.") == "Yes, it is."


def test_normal_contractions_still_work():
    assert _clean("do not worry") == "don't worry"
    assert _clean("it is working") == "it's working"
    assert _clean("I am here") == "I'm here"
    assert _clean("they would have gone") == "they'd have gone"


def test_clause_end_is_not_contracted():
    assert _clean("what it is: fine") == "what it is: fine"
    assert _clean("it is\nnext") == "it is\nnext"
    assert _clean("see (what it is) now") == "see (what it is) now"
    assert _clean('say "that is" twice') == 'say "that is" twice'
    assert _clean("it is — fine") == "it is — fine"
    assert _clean("it is today") == "it's today"
    assert _clean("What it is today") == "What it's today"  # known limit: lookahead cannot see a relative clause


def test_is_not_beats_it_is():
    assert _clean("It is not working") == "It isn't working"
    assert _clean("that is not it") == "that isn't it"


def test_pronoun_have_had():
    assert _clean("I have seen it") == "I've seen it"
    assert _clean("we had gone") == "we'd gone"
    assert _clean("they have to go") == "they have to go"
    assert _clean("You had to go") == "You had to go"
    assert _clean("Yes, I have.") == "Yes, I have."
    assert _clean("I have not tried") == "I haven't tried"


def test_timeout_kills_worker_inside_lock(tmp_path):
    async def run():
        eng = KokoroEngine(synth_timeout_s=0.2)
        eng._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import sys; sys.stdin.read()",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        proc = eng._proc
        with __import__("pytest").raises(asyncio.TimeoutError):
            await eng._do_synthesize("hi", str(tmp_path / "a.wav"), None, 1.0)
        assert proc.returncode is not None and eng._proc is None
        assert not eng._lock.locked()

    asyncio.run(run())
