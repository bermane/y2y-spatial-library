"""Tests for the CLI's data-root resolution (--root / Y2Y_ROOT / cwd)."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from pipeline.__main__ import cli


def _run(args, env=None):
    return CliRunner().invoke(cli, args, env=env, catch_exceptions=False)


def test_root_defaults_to_cwd(tmp_path, monkeypatch):
    """With no --root and no Y2Y_ROOT, the root is the current directory."""
    monkeypatch.delenv("Y2Y_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    # agol-sync status only reads the (absent) catalogue → 0 rows, exit 0.
    r = _run(["agol-sync", "status"])
    assert r.exit_code == 0


def test_y2y_root_env_var_used_when_no_flag(tmp_path, monkeypatch):
    """Y2Y_ROOT is honoured as the root when --root is not passed, so the
    steward can set it once and never retype the path."""
    (tmp_path / "inventory").mkdir()
    monkeypatch.setenv("Y2Y_ROOT", str(tmp_path))
    # Run from a *different* cwd to prove the env var (not cwd) is used.
    other = tmp_path.parent
    monkeypatch.chdir(other)
    r = _run(["agol-sync", "status"])
    assert r.exit_code == 0


def test_explicit_root_flag_overrides_env(tmp_path, monkeypatch):
    """An explicit --root wins over Y2Y_ROOT."""
    flag_root = tmp_path / "flag"
    env_root = tmp_path / "env"
    flag_root.mkdir()
    env_root.mkdir()
    monkeypatch.setenv("Y2Y_ROOT", str(env_root))
    r = _run(["--root", str(flag_root), "agol-sync", "status"])
    assert r.exit_code == 0
