"""scripts/in_venv.py picks the project's venv, never pre-commit's hook env."""

import sys

from scripts import in_venv


def _make_venv(base):
    exe = base / "Scripts" / "python.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    return exe


def test_project_venv_beats_an_activated_one(tmp_path, monkeypatch):
    project = tmp_path / "repo"
    other = tmp_path / "other-env"
    exe = _make_venv(project / "venv")
    _make_venv(other)
    monkeypatch.setattr(in_venv, "ROOT", project)
    monkeypatch.setenv("VIRTUAL_ENV", str(other))
    assert in_venv._venv_python() == exe


def test_hook_env_running_the_script_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(in_venv, "ROOT", tmp_path / "no-venv-here")
    monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)
    assert in_venv._venv_python() is None


def test_activated_venv_used_when_the_project_has_none(tmp_path, monkeypatch):
    other = tmp_path / "activated"
    exe = _make_venv(other)
    monkeypatch.setattr(in_venv, "ROOT", tmp_path / "no-venv-here")
    monkeypatch.setenv("VIRTUAL_ENV", str(other))
    assert in_venv._venv_python() == exe
