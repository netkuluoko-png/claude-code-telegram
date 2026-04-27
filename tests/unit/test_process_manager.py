"""Tests for background process manager isolation."""

from pathlib import Path

import pytest

from src.process import manager as manager_module
from src.process.manager import ProcessManager


def test_process_manager_rejects_cwd_outside_approved_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(manager_module, "STATE_FILE", tmp_path / "processes.json")
    monkeypatch.setattr(manager_module, "LOGS_DIR", tmp_path / "logs")
    approved = tmp_path / "approved"
    outside = tmp_path / "outside"
    approved.mkdir()
    outside.mkdir()

    pm = ProcessManager(namespace="user-456", approved_directory=approved)

    with pytest.raises(ValueError, match="cwd is outside approved directory"):
        pm.start("echo ok", str(outside))


def test_process_manager_rejects_command_paths_outside_approved_directory(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(manager_module, "STATE_FILE", tmp_path / "processes.json")
    monkeypatch.setattr(manager_module, "LOGS_DIR", tmp_path / "logs")
    approved = tmp_path / "approved"
    outside = tmp_path / "outside"
    approved.mkdir()
    outside.mkdir()

    pm = ProcessManager(namespace="user-456", approved_directory=approved)

    with pytest.raises(ValueError, match="command path is outside approved directory"):
        pm.start(f"cat {outside / 'secret.txt'}", str(approved))


def test_process_manager_uses_separate_state_for_namespace(tmp_path, monkeypatch):
    monkeypatch.setattr(manager_module, "STATE_FILE", tmp_path / "processes.json")
    monkeypatch.setattr(manager_module, "LOGS_DIR", tmp_path / "logs")

    default_pm = ProcessManager()
    isolated_pm = ProcessManager(namespace="user-456", approved_directory=tmp_path)

    assert default_pm.state_file == tmp_path / "processes.json"
    assert isolated_pm.state_file == tmp_path / "processes-user-456.json"
    assert isolated_pm.logs_dir == tmp_path / "logs" / "user-456"
