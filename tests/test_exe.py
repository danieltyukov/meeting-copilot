import os
import sys

from meeting_copilot.exe import find_executable

EXE = "tool.exe" if sys.platform == "win32" else "tool"


def _program(folder):
    folder.mkdir(exist_ok=True)
    path = folder / EXE
    path.write_text("")
    path.chmod(0o755)
    return str(path)


def test_finds_a_program_on_path(monkeypatch, tmp_path):
    real = _program(tmp_path / "bin")
    monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path / "empty"), str(tmp_path / "bin")]))
    assert find_executable("tool") == real


def test_never_the_current_directory(monkeypatch, tmp_path):
    """A program planted in the launch directory (someone else's repo) never runs,
    even with PATH entries that would mean the current directory."""
    _program(tmp_path / "repo")
    monkeypatch.chdir(tmp_path / "repo")
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "repo"]))
    assert find_executable("tool") is None

    real = _program(tmp_path / "bin")
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(tmp_path / "bin")]))
    assert find_executable("tool") == real


def test_quoted_windows_path_entries(monkeypatch, tmp_path):
    real = _program(tmp_path / "bin")
    monkeypatch.setenv("PATH", f'"{tmp_path / "bin"}"')
    assert find_executable("tool") == real


def test_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert find_executable("definitely-not-a-real-binary-xyz") is None
