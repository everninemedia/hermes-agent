import pytest

from agent.turn_request_assembly import (
    _append_iteration_closeout_nudge,
    _iteration_closeout_policy,
)
from agent.chat_completion_helpers_closeout import (
    _force_toolless_request,
    _local_iteration_checkpoint,
)


def test_closeout_policy_nudges_without_changing_tool_availability() -> None:
    assert _iteration_closeout_policy(29, 36) == (False, 7)
    assert _iteration_closeout_policy(30, 36) == (True, 6)
    assert _iteration_closeout_policy(34, 36) == (True, 2)
    assert _iteration_closeout_policy(36, 36) == (True, 0)
    assert _iteration_closeout_policy(3, 4) == (False, 1)


def test_closeout_nudge_is_request_local_tool_content() -> None:
    persisted = [{"role": "tool", "tool_call_id": "call_1", "content": "ok"}]
    messages = [dict(message) for message in persisted]
    _append_iteration_closeout_nudge(messages, remaining_after_call=2)
    assert messages[-1]["role"] == "tool"
    assert "Stop new exploration" in messages[-1]["content"]
    assert "2 provider calls remain" in messages[-1]["content"]
    assert persisted == [{"role": "tool", "tool_call_id": "call_1", "content": "ok"}]


def test_closeout_nudge_merges_adjacent_user_message() -> None:
    messages = [{"role": "user", "content": "finish the task"}]
    _append_iteration_closeout_nudge(messages, remaining_after_call=6)
    assert len(messages) == 1
    assert "finish the task" in messages[0]["content"]
    assert "Stop new exploration" in messages[0]["content"]


def test_force_toolless_request_strips_nested_routing_only() -> None:
    original = {
        "model": "grok-4.6",
        "tools": [{"type": "function"}],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "extra_body": {
            "tool_choice": "required",
            "parallel_tool_calls": True,
            "tools": [{"type": "function"}],
            "tags": ["hermes"],
        },
    }
    clean = _force_toolless_request(original)
    assert clean == {
        "model": "grok-4.6",
        "extra_body": {"tags": ["hermes"]},
    }
    assert original["tool_choice"] == "auto"
    assert original["extra_body"]["tools"]


def test_local_checkpoint_excludes_tool_results_arguments_and_error_body() -> None:
    messages = [
        {"role": "assistant", "content": "Safe visible progress"},
        {
            "role": "assistant",
            "content": [{"type": "summary_text", "text": "SECRET_REASONING"}],
        },
        {"role": "assistant", "content": "<think>SECRET_OPEN_REASONING"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": "terminal", "arguments": '{"token":"SECRET_ARG"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "SECRET_RESULT"},
    ]
    checkpoint = _local_iteration_checkpoint(
        messages, 36, RuntimeError("SECRET_ERROR_BODY")
    )
    assert "Safe visible progress" in checkpoint
    assert "terminal" in checkpoint
    assert "RuntimeError" in checkpoint
    assert "SECRET_ARG" not in checkpoint
    assert "SECRET_RESULT" not in checkpoint
    assert "SECRET_ERROR_BODY" not in checkpoint
    assert "SECRET_REASONING" not in checkpoint
    assert "SECRET_OPEN_REASONING" not in checkpoint


@pytest.mark.parametrize("kind", ["text", "output_text"])
@pytest.mark.parametrize("text", ["<think>SECRET_BLOCK</think>Visible progress", "Visible progress<think>SECRET_BLOCK"])
def test_checkpoint_sanitizes_structured_text(kind, text):
    checkpoint = _local_iteration_checkpoint(
        [{"role": "assistant", "content": [{"type": kind, "text": text}]}], 36, RuntimeError("SECRET_ERROR")
    )
    assert "Visible progress" in checkpoint
    assert "SECRET_BLOCK" not in checkpoint
    assert "<think>" not in checkpoint
    assert "SECRET_ERROR" not in checkpoint


def test_checkpoint_sanitizes_reasoning_across_text_blocks():
    checkpoint = _local_iteration_checkpoint(
        [{"role": "assistant", "content": [
            {"type": "text", "text": "Visible progress<think>SECRET_FIRST"},
            {"type": "output_text", "text": "SECRET_SECOND</think>Next step"},
        ]}], 36, RuntimeError("SECRET_ERROR")
    )
    assert "Visible progress" in checkpoint
    assert "Next step" in checkpoint
    assert "SECRET_" not in checkpoint
