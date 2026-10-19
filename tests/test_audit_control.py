import subprocess

from daemon import session_control as sc


def _git(*a, cwd):
    subprocess.run(["git", "-c", "protocol.file.allow=always", "-c", "user.email=a@b", "-c", "user.name=a", *a],
                   cwd=cwd, check=True, capture_output=True)


def test_submodule_project_is_its_own_folder(tmp_path):
    sub = tmp_path / "sub"; sub.mkdir(); _git("init", "-q", cwd=sub)
    _git("commit", "-q", "--allow-empty", "-m", "x", cwd=sub)
    par = tmp_path / "par"; (par / "libs").mkdir(parents=True); _git("init", "-q", cwd=par)
    _git("submodule", "add", "-q", str(sub), "libs/sub", cwd=par)
    ident = sc._identify_uncached(str(par / "libs" / "sub"))
    assert ident["project"] == str((par / "libs" / "sub").resolve())
    assert sc._identify_uncached(str(par))["project"] == str(par.resolve())


def test_bare_project_is_the_bare_dir(tmp_path):
    bare = tmp_path / "foo.git"; _git("init", "-q", "--bare", str(bare), cwd=tmp_path)
    assert sc._identify_uncached(str(bare))["project"] == str(bare.resolve())


def test_corrupt_first_config_does_not_fall_through(tmp_path, monkeypatch):
    bad = tmp_path / "c.json"; bad.write_text("{nope")
    monkeypatch.setattr(sc.paths, "config_path", lambda: bad)
    cfg = sc.load_config()
    assert "config_error" in cfg and "voice" not in cfg
    assert "config_error" in sc.voices_info(cfg)
