"""Compact dated titles through Hermes' real session store."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.title_generator import apply_instant_title, auto_title_session
from persistence.conversation.sessions import NewSession, TitleSource
from persistence_assembly import open_persistence, profile_scope


@pytest.fixture
def store(tmp_path, monkeypatch):
    home = tmp_path / "evm"
    home.mkdir()
    (home / "config.yaml").write_text(
        "auxiliary:\n  title_generation:\n    compact_date_timezone: Europe/Berlin\n"
        "    max_words: 3\n    language: German. Use at most three words.\n", encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    with open_persistence(profile_scope(home=home)) as handle:
        # 23:30 UTC is already the next day in Berlin. Time of titling is deliberately not pinned.
        started_at = datetime(2026, 11, 25, 23, 30, tzinfo=timezone.utc).timestamp()

        def create(sid):
            with patch("time.time", return_value=started_at):
                handle.sessions.create(NewSession(session_id=sid, source="desktop"))

        create("new")
        yield handle, create


def reply(title):
    message = SimpleNamespace(content=json.dumps({"title": title}))
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@pytest.mark.parametrize("topic", ["Sales VÖ", "Publishing", "Liquidität", "Business Plan", "Translations",
                                   "it-sa Planung", "ERP Optimierung"])
def test_session_model_title_ends_with_local_start_date(store, topic):
    handle, _ = store
    with patch("agent.title_generator.call_llm", return_value=reply(topic)) as model:
        auto_title_session(handle, "new", topic, persistence=handle)
    assert handle.sessions.get_title("new") == f"{topic} 261126"
    assert handle.sessions.get_title_source("new") is TitleSource.LLM
    prompt = model.call_args.kwargs["messages"][0]["content"]
    assert "At most 2 words" in prompt
    assert "at most three words" not in prompt


def test_instant_title_has_the_same_word_cap_and_date(store):
    handle, _ = store
    result = apply_instant_title(handle, "new", "ERP Optimierung für den Vertrieb", persistence=handle)
    assert result == "ERP Optimierung 261126"
    assert handle.sessions.get_title_source("new") is TitleSource.DERIVED


def test_overlong_model_title_and_guessed_date_do_not_break_format(store):
    handle, _ = store
    with patch("agent.title_generator.call_llm", return_value=reply("ERP Optimierung Planung 220101")) as model:
        auto_title_session(handle, "new", "Planung zur ERP Optimierung", persistence=handle)
    assert model.call_count == 2
    assert handle.sessions.get_title("new") == "ERP Optimierung 261126"


def test_duplicate_names_keep_date_last_and_preserve_existing_sessions(store):
    handle, create = store
    create("first")
    create("second")
    handle.sessions.set_title("first", "Sales VÖ 261126")
    handle.sessions.set_title("second", "Sales VÖ#2 261126")
    with patch("agent.title_generator.call_llm", return_value=reply("Sales VÖ")):
        auto_title_session(handle, "new", "Heute die Sales VÖ vorbereiten", persistence=handle)
    assert handle.sessions.get_title("new") == "Sales VÖ#3 261126"
    assert handle.sessions.get_title("first") == "Sales VÖ 261126"
    assert handle.sessions.get_title("second") == "Sales VÖ#2 261126"


def test_six_digit_reference_is_not_mistaken_for_a_date(store):
    handle, _ = store
    with patch("agent.title_generator.call_llm", return_value=reply("Vorgang 270001 261126")):
        auto_title_session(handle, "new", "Bitte Vorgang 270001 bearbeiten", persistence=handle)
    assert handle.sessions.get_title("new") == "Vorgang 270001 261126"


def test_date_only_model_reply_does_not_become_a_topic(store):
    handle, _ = store
    with patch("agent.title_generator.call_llm", return_value=reply("261126")):
        auto_title_session(handle, "new", "ERP Optimierung für den Vertrieb", persistence=handle)
    assert handle.sessions.get_title("new") == "ERP Optimierung 261126"
    assert handle.sessions.get_title_source("new") is TitleSource.DERIVED


def test_manual_title_stays_untouched(store):
    handle, _ = store
    handle.sessions.set_title("new", "Mein eigener Titel")
    with patch("agent.title_generator.call_llm") as model:
        auto_title_session(handle, "new", "ERP Optimierung", persistence=handle)
    model.assert_not_called()
    assert handle.sessions.get_title("new") == "Mein eigener Titel"
