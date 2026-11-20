"""Local approval policy for Git publishing and merging.

``git push`` and ``git merge`` are intentional work on shared history, but in
interactive sessions they must travel through the user-approval UI (gated by
the ``_CMDPOS``-anchored rules in tools/approval_detection.py). The force-push
rules keep their own, more specific explanation.
"""

import pytest

from tools.approval import detect_dangerous_command

PUSH = "git push (publishes commits to a remote)"
MERGE = "git merge (changes the current branch)"


@pytest.mark.parametrize("cmd, key", [
    ("git push origin main", PUSH),
    ("git push", PUSH),
    ("git -C /tmp/x push origin main", PUSH),
    ("cd /r && git push origin main", PUSH),
    ("git add x; git commit -m y; git push", PUSH),
    ("sudo git push origin main", PUSH),
    ("echo $(git push origin main)", PUSH),
    ("git merge feature/ready", MERGE),
    ("git merge --no-ff feature", MERGE),
    ("git -C /tmp/x merge main", MERGE),
    ("git fetch && git merge origin/main", MERGE),
])
def test_git_push_and_merge_require_user_approval(cmd, key):
    """Shared Git writes must always travel through the approval UI."""
    assert detect_dangerous_command(cmd) == (True, key, key)


@pytest.mark.parametrize("cmd", [
    "git push --force origin main",
    "git push -f",
    "git push origin +main",
    # Only a later detection variant shows this one as a force push; the plain rule must wait for it.
    "git push -o ci\\;skip -f origin main",
])
def test_a_force_push_keeps_its_more_specific_explanation(cmd):
    dangerous, _key, description = detect_dangerous_command(cmd)
    assert dangerous is True, cmd
    assert "force" in description.lower(), cmd


@pytest.mark.parametrize("cmd", [
    "git status",
    "git log --oneline -1",
    "git fetch origin main",
    "git merge-base main HEAD",
    "git merge-tree main feature",
    "git mergetool",
    "git stash push -m wip",
    "git log --grep push",
    # Quoted prose and search patterns are data, not a command.
    "git commit -m 'git push later'",
    'git commit -m "git merge feature later"',
    "grep 'git push' notes.md",
    'grep -rn "git merge" docs/',
    "echo git push origin main",
])
def test_safe_git_ops_not_flagged(cmd):
    """Reads and local writes stay ungated; quoted prose is data, not a command."""
    assert detect_dangerous_command(cmd) == (False, None, None)
