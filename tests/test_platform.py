"""Unit tests for the Platform audio-playback seam."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from daemon.platforms.base import (
    Platform, PlatformMacOS, PlatformLinux, PlatformWindows, make_platform,
)
import daemon.platforms.base as base_mod


def _which_only(*available):
    """Return a fake shutil.which: resolves only the named binaries."""
    avail = set(available)
    return lambda name: (f"/usr/bin/{name}" if name in avail else None)


def test_platform_is_abstract():
    with pytest.raises(TypeError):
        Platform()


def test_macos_cmd_with_volume():
    assert PlatformMacOS().build_player_cmd("/a.wav", 3.5) == ["afplay", "-v", "3.500", "/a.wav"]


def test_macos_cmd_unit_volume_omits_flag():
    assert PlatformMacOS().build_player_cmd("/a.wav", 1.0) == ["afplay", "/a.wav"]


def test_linux_prefers_decoder_ffplay_for_format_safety(monkeypatch):
    # ffplay first: it decodes both .wav (espeak) and .mp3 (edge-tts). Picking a
    # WAV-only player (pw-play/paplay/aplay) for an .mp3 would fail silently —
    # the afplay-extension class of bug. ffplay must win over mpv and pw-play.
    monkeypatch.setattr(base_mod.shutil, "which", _which_only("ffplay", "mpv", "pw-play"))
    assert PlatformLinux().build_player_cmd("/a.mp3", 1.0) == [
        "ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-volume", "100", "/a.mp3",
    ]


def test_linux_uses_mpv_when_no_ffplay(monkeypatch):
    monkeypatch.setattr(base_mod.shutil, "which", _which_only("mpv", "pw-play", "paplay"))
    assert PlatformLinux().build_player_cmd("/a.wav", 1.0) == [
        "mpv", "--no-video", "--really-quiet", "--volume-max=200", "--volume=100", "/a.wav",
    ]


def test_linux_falls_back_to_pwplay(monkeypatch):
    monkeypatch.setattr(base_mod.shutil, "which", _which_only("pw-play"))
    assert PlatformLinux().build_player_cmd("/a.wav", 1.0) == ["pw-play", "--volume=1", "/a.wav"]


def test_linux_falls_back_to_paplay(monkeypatch):
    monkeypatch.setattr(base_mod.shutil, "which", _which_only("paplay"))
    assert PlatformLinux().build_player_cmd("/a.wav", 1.0) == ["paplay", "--volume=65536", "/a.wav"]


def test_linux_falls_back_to_aplay(monkeypatch):
    monkeypatch.setattr(base_mod.shutil, "which", _which_only("aplay"))
    assert PlatformLinux().build_player_cmd("/a.wav", 1.0) == ["aplay", "-q", "/a.wav"]


def test_linux_no_player_found_falls_back_to_mpv_argv(monkeypatch):
    # Nothing installed: return a best-effort mpv argv so the failure surfaces
    # loudly (subprocess rc != 0) rather than as an empty/None command.
    monkeypatch.setattr(base_mod.shutil, "which", _which_only())
    assert PlatformLinux().build_player_cmd("/a.wav", 1.0) == [
        "mpv", "--no-video", "--really-quiet", "--volume-max=200", "--volume=100", "/a.wav",
    ]


def test_linux_volume_per_player(monkeypatch):
    # ffplay cannot amplify (clamped at 100); mpv/pw-play/paplay scale; aplay has no option.
    def cmd(player, v):
        monkeypatch.setattr(base_mod.shutil, "which", _which_only(player))
        return PlatformLinux().build_player_cmd("/a.wav", v)

    assert cmd("ffplay", 0.5)[-2:] == ["50", "/a.wav"]
    assert cmd("ffplay", 2.0)[-2:] == ["100", "/a.wav"]
    assert cmd("mpv", 1.5)[-2:] == ["--volume=150", "/a.wav"]
    assert cmd("pw-play", 0.5) == ["pw-play", "--volume=0.5", "/a.wav"]
    assert cmd("paplay", 2.0) == ["paplay", "--volume=131072", "/a.wav"]
    assert cmd("aplay", 0.3) == cmd("aplay", 2.0) == ["aplay", "-q", "/a.wav"]


def test_windows_cmd():
    assert PlatformWindows().build_player_cmd("/a.wav", 1.0) == [
        "ffplay", "-nodisp", "-autoexit", "/a.wav",
    ]


def test_factory_detects_each():
    assert isinstance(make_platform("Darwin"), PlatformMacOS)
    assert isinstance(make_platform("Linux"), PlatformLinux)
    assert isinstance(make_platform("Windows"), PlatformWindows)
