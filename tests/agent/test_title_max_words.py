"""``auxiliary.title_generation.max_words``: a hard word cap for model-written session titles."""

import json
from unittest.mock import MagicMock, patch

import pytest

from agent.title_generator import _build_title_prompt, _title_max_words, generate_title


def _config(**title_generation):
    cfg = {"auxiliary": {"title_generation": title_generation}}
    return patch("hermes_cli.config.load_config_readonly", return_value=cfg)


def _reply(*titles):
    """Fake ``call_llm`` answering with ``titles`` in order (the last one repeats); an Exception is raised."""
    def call(**kwargs):
        call.kwargs = kwargs
        call.count += 1
        title = titles[min(call.count, len(titles)) - 1]
        if isinstance(title, Exception):
            raise title
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = json.dumps({"title": title})
        return resp

    call.count = 0
    return call


@pytest.mark.parametrize(
    ("value", "expected"),
    [(3, 3), ("2", 2), (1, 1), (12, 12), (None, 0), (0, 0), (-1, 0), (13, 0), ("three", 0), (True, 0)],
)
def test_max_words_reads_config_and_ignores_unusable_values(value, expected):
    with _config(max_words=value):
        assert _title_max_words() == expected


def test_max_words_unset_keeps_the_default_prompt_and_title():
    assert "- 3 to 7 words, sentence case" in _build_title_prompt()
    assert 'Good: {"title": "Fix login button on mobile"}' in _build_title_prompt()
    call = _reply("Sales und VOE mit Logging starten")
    with _config(), patch("agent.title_generator.call_llm", side_effect=call):
        assert generate_title("Starte Sales und VOE mit Logging") == "Sales und VOE mit Logging starten"
    assert call.count == 1


def test_capped_prompt_states_the_cap_and_drops_longer_examples():
    prompt = _build_title_prompt("German", 3)
    assert "- At most 3 words, sentence case" in prompt
    assert "3 to 7 words" not in prompt
    assert "- Write the title in German." in prompt
    # A five-word example under an "at most 3 words" rule teaches the model to ignore the rule.
    assert "Fix login button on mobile" not in prompt
    assert 'Good: {"title": "Friendly greeting"}' in prompt
    assert "__" not in prompt


def test_capped_title_is_cut_to_the_cap():
    call = _reply("Sales und VOE mit Logging starten")
    with _config(max_words=3), patch("agent.title_generator.call_llm", side_effect=call):
        assert generate_title("Starte Sales und VOE mit Logging") == "Sales und VOE"
    assert "- At most 3 words" in call.kwargs["messages"][0]["content"]
    assert call.count == 2


def test_too_long_title_is_shortened_by_the_model_before_any_cut():
    call = _reply("Sales und VOE mit Logging starten", "Sales VOE Logging")
    with _config(max_words=3), patch("agent.title_generator.call_llm", side_effect=call):
        assert generate_title("Starte Sales und VOE mit Logging") == "Sales VOE Logging"
    assert call.count == 2
    retry = call.kwargs["messages"]
    assert json.loads(retry[-2]["content"]) == {"title": "Sales und VOE mit Logging starten"}
    assert "has 6 words" in retry[-1]["content"] and "at most 3 words" in retry[-1]["content"]


def test_failed_retry_falls_back_to_the_cut_first_title():
    call = _reply("Sales und VOE mit Logging starten", RuntimeError("provider down"))
    with _config(max_words=3), patch("agent.title_generator.call_llm", side_effect=call):
        assert generate_title("Starte Sales und VOE mit Logging") == "Sales und VOE"


def test_cut_leaves_no_dangling_separator():
    call = _reply("Budget Fenster - Umbau der Anzeige")
    with _config(max_words=3), patch("agent.title_generator.call_llm", side_effect=call):
        assert generate_title("Bau das Budget Fenster um") == "Budget Fenster"


def test_title_within_the_cap_is_unchanged():
    call = _reply("CRM Kontakte Logging")
    with _config(max_words=3), patch("agent.title_generator.call_llm", side_effect=call):
        assert generate_title("Logge die CRM Kontakte") == "CRM Kontakte Logging"
    assert call.count == 1
