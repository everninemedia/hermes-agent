"""Pin the terminal approval outcomes for Git publication across run contexts."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from uuid import uuid4

import pytest

from agent.delegation_context import delegated_child_context
from gateway.session_context import clear_session_vars, set_session_vars
from hermes_cli import config
from tools import approval, approval_context, delegate_tool_config, terminal_tool


@pytest.mark.parametrize(
    "command, pattern_key",
    [
        ("git push origin main", "git push (publishes commits to a remote)"),
        ("git merge feature", "git merge (changes the current branch)"),
    ],
)
@pytest.mark.parametrize(
    "case",
    ["cron", "messaging", "headless-child", "cli-child", "messaging-child", "smart-messaging"],
)
def test_git_publish_approval_context(monkeypatch, command, pattern_key, case):
    messaging = case in {"messaging", "messaging-child", "smart-messaging"}
    smart = case == "smart-messaging"
    settings = {"approvals": {"mode": "smart" if smart else "manual", "timeout": 2}}
    monkeypatch.setattr(config, "load_config_readonly", lambda: settings)
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    for name in (
        "HERMES_EXEC_ASK", "HERMES_GATEWAY_SESSION", "HERMES_INTERACTIVE",
        "HERMES_SINGLE_QUERY_SESSION", "HERMES_IGNORE_USER_CONFIG",
    ):
        monkeypatch.delenv(name, raising=False)
    if messaging or case == "cron":
        monkeypatch.setenv("HERMES_EXEC_ASK", "1")

    def guardian(*args):
        assert smart, "Unexpected guardian call"
        return "approve"

    monkeypatch.setattr(approval, "_smart_verdict", guardian)
    session_key = f"agent:main:telegram:dm:git-approval-{uuid4().hex}"
    tokens = set_session_vars(
        platform="telegram" if messaging or case == "cron" else "",
        cron_session="1" if case == "cron" else "",
        session_key=session_key,
    )
    interactive = approval_context.set_hermes_interactive_context(case == "cli-child")
    notices = []
    answers = []

    def notify(data):
        notices.append((data["command"], data["pattern_key"]))
        answers.append(approval.resolve_gateway_approval(session_key, "deny"))

    def child_guard():
        with delegated_child_context("git-approval-child"):
            return terminal_tool._check_all_guards(command, "local")

    try:
        assert approval.detect_dangerous_command(command) == (True, pattern_key, pattern_key)
        if messaging:
            approval.register_gateway_notify(session_key, notify)
        if case.endswith("-child"):
            with ThreadPoolExecutor(
                max_workers=1,
                initializer=terminal_tool.set_approval_callback,
                initargs=(delegate_tool_config._get_subagent_approval_callback(),),
            ) as worker:
                result = worker.submit(copy_context().run, child_guard).result(timeout=5)
        else:
            result = terminal_tool._check_all_guards(command, "local")
    finally:
        approval.unregister_gateway_notify(session_key)
        approval_context.reset_hermes_interactive_context(interactive)
        clear_session_vars(tokens)

    if case == "cron":
        assert result["approved"] is False
        assert "approvals.cron_mode" in result["message"]
    elif case in {"messaging", "messaging-child"}:
        assert notices == [(command, pattern_key)]
        assert answers == [1]
        assert result["approved"] is False and result["outcome"] == "denied"
    elif case == "cli-child":
        assert result["approved"] is False and result["outcome"] == "denied"
    elif smart:
        assert notices == []
        assert result["approved"] is True and result["smart_approved"] is True
    else:
        assert result == {"approved": True, "message": None}
