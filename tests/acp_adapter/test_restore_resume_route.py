"""A restored ACP session runs the model and route of ONE stored record.

The first accounted call rewrites the model column and the billing columns to whatever served it,
a fallback included, and touches nothing in ``model_config``. ``_restore`` used to read the model
from the column, the provider from ``model_config`` and fill gaps from the billing columns, so it
could request one provider's model at another provider's endpoint (a 404 on every resumed turn).
It now reads through ``persistence.gateway.routing.resume_route``, the reader CLI ``--resume`` and
the TUI gateway share.
"""

from types import SimpleNamespace

from acp_adapter.session import SessionManager
from persistence.conversation.usage import BillingRoute, TokenCounts, UsageDelta
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_seed import seed_messages, seed_session

_LUNA = "gpt-5.6-luna"
_CODEX_URL = "https://chatgpt.com/backend-api/codex/"
_XAI_URL = "https://api.x.ai/v1"


def _restore_kwargs(db, session_id):
    """The arguments ``_restore`` hands ``_make_agent`` for ``session_id``."""
    manager = SessionManager(db=db, agent_factory=None)
    made: dict = {}

    def _make_agent(**kwargs):
        made.update(kwargs)
        return SimpleNamespace(model=kwargs.get("model"), provider=kwargs.get("requested_provider"),
                               base_url=kwargs.get("base_url"), api_mode=kwargs.get("api_mode"))

    manager._make_agent = _make_agent
    assert manager._restore(session_id) is not None
    return made


def _served_by(db, session_id, *, model, provider, base_url):
    """The first accounted call, served by ``provider``: it records the serving route in the billing columns."""
    db.usage.queue_usage(session_id, UsageDelta(
        tokens=TokenCounts(input_tokens=10, output_tokens=5), api_call_count=1,
        route=BillingRoute(model=model, billing_provider=provider, billing_base_url=base_url)))
    assert db.usage.flush_usage()


def test_restore_keeps_the_switched_pair_after_a_fallback_served_the_call(tmp_path):
    with open_persistence(profile_scope(home=tmp_path)) as db:
        seed_session(db, "s1", "acp", model="grok-4.6", cwd=str(tmp_path),
                     model_config={"cwd": str(tmp_path), "provider": "xai-oauth", "base_url": _XAI_URL})
        seed_messages(db, "s1", {"role": "user", "content": "turn 1"})
        db.sessions.update_model("s1", _LUNA, provider="openai-codex", base_url=_CODEX_URL,
                                 api_mode="codex_responses")
        _served_by(db, "s1", model="grok-4.6", provider="xai-oauth", base_url=_XAI_URL)
        made = _restore_kwargs(db, "s1")

    assert (made["model"], made["requested_provider"], made["base_url"], made["api_mode"]) == (
        _LUNA, "openai-codex", _CODEX_URL, "codex_responses")


def test_restore_never_fills_the_stored_provider_from_another_models_billing_route(tmp_path):
    with open_persistence(profile_scope(home=tmp_path)) as db:
        seed_session(db, "s1", "acp", model=_LUNA, cwd=str(tmp_path),
                     model_config={"cwd": str(tmp_path), "model": _LUNA})
        seed_messages(db, "s1", {"role": "user", "content": "turn 1"})
        _served_by(db, "s1", model="grok-4.6", provider="xai-oauth", base_url=_XAI_URL)
        made = _restore_kwargs(db, "s1")

    # The pick stays; the billing route served the fallback's model, so it names no provider here.
    assert (made["model"], made["requested_provider"], made["base_url"]) == (_LUNA, None, None)


def test_restore_of_a_row_without_a_stored_route_runs_the_provider_that_served_it(tmp_path):
    with open_persistence(profile_scope(home=tmp_path)) as db:
        seed_session(db, "s1", "acp", model=_LUNA, cwd=str(tmp_path), model_config={"cwd": str(tmp_path)})
        seed_messages(db, "s1", {"role": "user", "content": "turn 1"})
        _served_by(db, "s1", model=_LUNA, provider="openai-codex", base_url=_CODEX_URL)
        made = _restore_kwargs(db, "s1")

    assert (made["model"], made["requested_provider"]) == (_LUNA, "openai-codex")
