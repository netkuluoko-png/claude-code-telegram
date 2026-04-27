"""Test Codex CLI integration helpers."""

from pathlib import Path

from src.codex.sdk_integration import CodexCLIManager
from src.config.settings import Settings


def _settings(tmp_path, **overrides):
    defaults = {
        "telegram_bot_token": "test:token",
        "telegram_bot_username": "testbot",
        "approved_directory": tmp_path,
        "agent_backend": "codex",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_build_args_for_new_session(tmp_path):
    manager = CodexCLIManager(
        _settings(
            tmp_path,
            codex_cli_path="/usr/local/bin/codex",
            codex_model="gpt-5.4",
            codex_effort="high",
        )
    )

    args = manager._build_args(
        working_directory=tmp_path,
        session_id=None,
        continue_session=False,
        output_last_message=tmp_path / "last.txt",
        image_paths=[],
        model_override=None,
        effort_override=None,
    )

    assert args[:2] == ["/usr/local/bin/codex", "--ask-for-approval"]
    assert "exec" in args
    assert "--json" in args
    assert "--skip-git-repo-check" in args
    assert "-o" in args
    assert args[-1] == "-"
    model_idx = args.index("--model")
    assert args[model_idx : model_idx + 2] == ["--model", "gpt-5.4"]
    assert 'model_reasoning_effort="high"' in args


def test_build_args_for_resume(tmp_path):
    manager = CodexCLIManager(_settings(tmp_path))

    args = manager._build_args(
        working_directory=tmp_path,
        session_id="thread-123",
        continue_session=True,
        output_last_message=tmp_path / "last.txt",
        image_paths=[Path("/tmp/image.png")],
        model_override="gpt-5.5",
        effort_override="xhigh",
    )

    assert args[args.index("exec") + 1] == "resume"
    assert "thread-123" in args
    model_idx = args.index("--model")
    assert args[model_idx : model_idx + 2] == ["--model", "gpt-5.5"]
    assert 'model_reasoning_effort="xhigh"' in args
    assert "--image" in args


def test_json_event_extractors():
    events = [
        {"type": "thread.started", "thread_id": "thread-abc"},
        {"type": "turn.started"},
        {"type": "message", "content": "final answer"},
    ]

    assert CodexCLIManager._extract_session_id(events) == "thread-abc"
    assert CodexCLIManager._extract_final_content(events) == "final answer"


def test_error_extractor_handles_turn_failed():
    events = [
        {
            "type": "turn.failed",
            "error": {"message": "stream disconnected before completion"},
        }
    ]

    assert (
        CodexCLIManager._extract_error(events)
        == "stream disconnected before completion"
    )
