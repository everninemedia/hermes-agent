"""Picker regression guards for the retired Ox Alpha entries."""

from hermes_cli.models import _PROVIDER_MODELS


def test_retired_ox_entries_are_not_curated_picker_options():
    retired = {"stealth/ox-alpha", "x-preview-f-free", "ox-alpha-free"}
    providers = ("openrouter", "nous", "opencode-zen", "opencode-free", "opencode-go")

    for provider in providers:
        assert retired.isdisjoint(set(_PROVIDER_MODELS.get(provider, [])))


def test_zai_glm_53_flash_is_a_curated_picker_option():
    models = {model.lower() for model in _PROVIDER_MODELS["zai"]}
    assert "glm-5.3-flash" in models
    # OpenCode announced the Flash model on its Go subscription.
    assert "glm-5.3-flash" in {model.lower() for model in _PROVIDER_MODELS["opencode-go"]}
    # The flagship remains a distinct selectable model, not an alias.
    assert "glm-5.3" in models
