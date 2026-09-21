"""Tests for ``metasphere version`` CLI."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from metasphere.cli import version as V


def test_version_prints_version_and_commit(capsys):
    with mock.patch.object(V, "_head_hash", return_value="abcdef123456"):
        rc = V.main([])
    out, _ = capsys.readouterr()
    assert rc == 0
    lines = out.strip().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("metasphere ")
    assert lines[1] == "commit: abcdef123456"


def test_version_unknown_commit_when_not_a_repo(capsys):
    with mock.patch.object(V, "_head_hash", return_value="(unknown)"):
        rc = V.main([])
    out, _ = capsys.readouterr()
    assert "commit: (unknown)" in out
    assert rc == 0


def test_head_hash_uses_installed_source_not_caller_cwd():
    completed = mock.Mock(returncode=0, stdout="abcdef1234567890\n")
    expected_root = Path(V.__file__).resolve().parents[2]

    with mock.patch.object(V.subprocess, "run", return_value=completed) as run:
        assert V._head_hash() == "abcdef123456"

    run.assert_called_once_with(
        ["git", "-C", str(expected_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )


def test_version_registered_in_dispatcher():
    from metasphere.cli.main import REGISTRY
    assert "version" in REGISTRY
    assert REGISTRY["version"] == "metasphere.cli.version:main"


@pytest.mark.parametrize("argv,extra", [
    (["--bogus"], "--bogus"),
    (["extra"], "extra"),
])
def test_version_rejects_unknown_args(capsys, argv, extra):
    """``version`` takes no arguments; the pre-hardening path silently
    dropped extras and printed version+commit anyway."""
    rc = V.main(argv)
    out, err = capsys.readouterr()
    assert rc == 2
    assert out == ""
    assert "metasphere version:" in err
    assert extra in err


def test_resolve_reads_pyproject_version_first():
    """In a source checkout the live pyproject.toml wins over pip dist-info."""
    repo_pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    assert repo_pyproject.is_file(), "test precondition: running from source tree"
    expected = V._read_pyproject_version()
    assert expected and expected != "0.0.0"
    assert V._resolve_version() == expected


def test_resolve_falls_back_to_pip_metadata_when_pyproject_missing():
    with mock.patch.object(V, "_read_pyproject_version", return_value=None):
        # Should not raise and should not be "0.0.0" if metasphere is installed.
        v = V._resolve_version()
    assert v and isinstance(v, str)


def test_read_pyproject_version_handles_missing_file(tmp_path, monkeypatch):
    """A wheel install without pyproject.toml on disk returns None cleanly."""
    # Point _read_pyproject_version at a tree with no pyproject.toml by
    # rebinding the resolution root: copy the function's logic over a tmp path.
    fake_root = tmp_path / "pkg" / "sub"
    fake_root.mkdir(parents=True)
    assert not (tmp_path / "pyproject.toml").exists()
    # Sanity: our helper reads relative to the module file, so directly
    # exercise the "no pyproject.toml" branch by monkeypatching Path.is_file.
    with mock.patch.object(Path, "is_file", return_value=False):
        assert V._read_pyproject_version() is None
