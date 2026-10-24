"""Local approval policy for Git publishing and merging.

``git push`` and ``git merge`` are intentional work on shared history, but in
interactive sessions they must travel through the user-approval UI (gated by
the ``_CMDPOS``-anchored patterns in tools/approval_detection.py). These
cases moved here from tests/tools/test_approval.py so that file stays at its
upstream size; the assertions are unchanged.
"""

from tools.approval import detect_dangerous_command


def test_git_push_and_merge_require_user_approval():
    """Shared Git writes must always travel through the approval UI."""
    for cmd in ("git push origin main", "git merge feature/ready"):
        dangerous, _, desc = detect_dangerous_command(cmd)
        assert dangerous is True, cmd
        assert "git" in desc.lower(), cmd


def test_safe_git_ops_not_flagged():
    """Reads and local writes stay ungated; quoted prose is data, not a command."""
    for cmd in (
        "git status",
        "git log --oneline -1",
        "git merge-base main HEAD",
        "git commit -m 'git push later'",
    ):
        dangerous, _, _ = detect_dangerous_command(cmd)
        assert dangerous is False, cmd
