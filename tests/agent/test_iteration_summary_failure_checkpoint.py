"""Regression: a failed iteration summary still persists the sanitized checkpoint row.

The three pipeline assertions moved here from
``TestHandleMaxIterations::test_api_failure_returns_error`` in
tests/agent/test_run_agent.py so that file stays at its upstream size. The
scenario is the same summary-failure path: the provider raises, the local
iteration checkpoint is built, and it is persisted as the assistant row of
the message history, not only returned.
"""

from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list:
    """Build minimal tool definition list accepted by AIAgent.__init__."""
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


@pytest.fixture(autouse=True)
def _mock_plugin_discovery(monkeypatch):
    # Tool definitions are supplied by these unit fixtures. Scanning every
    # bundled plugin again for each isolated test home adds no coverage.
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)


@pytest.fixture()
def agent():
    """Minimal AIAgent with mocked OpenAI client and tool loading."""
    with (
        patch(
            "model_tools.get_tool_definitions", return_value=_make_tool_defs("web_search")
        ),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        return a


def test_summary_failure_checkpoint_persists_assistant_row(agent):
    """A failed summary lands the local checkpoint as the persisted assistant row."""
    agent.client.chat.completions.create.side_effect = Exception("API down")
    agent._cached_system_prompt = "You are helpful."
    messages = [{"role": "user", "content": "do stuff"}]
    with patch("agent.relay_llm.complete_logical_call") as complete_logical:
        result = agent._handle_max_iterations(messages, 60)
    assert isinstance(result, str)
    # Plain what-now for the user; the raw exception stays in the log, not the reply.
    assert result.strip()
    assert "API down" not in result
    # The relocated pipeline assertions: the checkpoint text is the persisted
    # assistant row, so a resumed session continues from it.
    assert "preserved the session state" in result
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == result
    complete_logical.assert_called_once()
    assert complete_logical.call_args.kwargs == {"outcome": "failed"}
