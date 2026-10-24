"""Local patch fix-launchd-extra-env: allowlisted extra variables survive plist regeneration.

HERMES_WRITE_SAFE_ROOT lives only in the launchd EnvironmentVariables of the gateway that spawns
kanban workers. Without this patch every `hermes gateway start`/`install`/`setup` rewrote the plist
from the template and silently dropped it, and `gateway status` reported the protected plist as stale.
"""
import plistlib
import subprocess
from unittest.mock import MagicMock

import pytest

from hermes_cli import gateway as gw
from hermes_cli import gateway_launchd as launchd
from hermes_cli import gateway_service_owner

SAFE_ROOT = "/Users/x/hermes-work:/private/tmp:/Users/x/a&b"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / "profile"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


def _env(plist_text: str) -> dict:
    return plistlib.loads(plist_text.encode("utf-8"))["EnvironmentVariables"]


def test_generator_without_extra_file_matches_template(home):
    env = _env(gw.generate_launchd_plist())
    assert "HERMES_WRITE_SAFE_ROOT" not in env
    assert env["HERMES_SUPERVISED_CHILD"] == "1"


def test_generator_adds_allowlisted_key_escaped(home):
    (home / launchd._LAUNCHD_EXTRA_ENV_FILE).write_text(
        f"# comment\nHERMES_WRITE_SAFE_ROOT={SAFE_ROOT}\n", encoding="utf-8"
    )
    env = _env(gw.generate_launchd_plist())
    assert env["HERMES_WRITE_SAFE_ROOT"] == SAFE_ROOT
    assert env["HERMES_HOME"] == str(home.resolve())


def test_generator_ignores_keys_outside_allowlist(home):
    (home / launchd._LAUNCHD_EXTRA_ENV_FILE).write_text(
        "PATH=/evil\nHERMES_HOME=/elsewhere\nFOO=bar\nHERMES_WRITE_SAFE_ROOT=\n", encoding="utf-8"
    )
    env = _env(gw.generate_launchd_plist())
    assert env["HERMES_HOME"] == str(home.resolve())
    assert "FOO" not in env
    assert "HERMES_WRITE_SAFE_ROOT" not in env
    assert env["PATH"] != "/evil"


def test_installed_plist_with_key_is_current(home, tmp_path, monkeypatch):
    (home / launchd._LAUNCHD_EXTRA_ENV_FILE).write_text(f"HERMES_WRITE_SAFE_ROOT={SAFE_ROOT}\n", encoding="utf-8")
    plist_path = tmp_path / "ai.hermes.gateway-evm.plist"
    plist_path.write_text(gw.generate_launchd_plist(), encoding="utf-8")
    monkeypatch.setattr(gw, "get_launchd_plist_path", lambda: plist_path)
    assert gw.launchd_plist_is_current() is True


def _stale(tmp_path, monkeypatch, installed_env: dict):
    plist_path = tmp_path / "ai.hermes.gateway-evm.plist"
    installed = {"Label": "ai.hermes.gateway-evm", "EnvironmentVariables": installed_env}
    plist_path.write_bytes(plistlib.dumps(installed))
    monkeypatch.setattr(gw, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(gateway_service_owner, "refuse_temp_home_service_write", lambda *a: False)
    monkeypatch.setattr(gateway_service_owner, "definition_belongs_to_home", lambda *a: True)
    monkeypatch.setattr(gw, "get_launchd_label", lambda: "ai.hermes.gateway-evm")
    monkeypatch.setattr(gw, "_launchd_domain", lambda: "gui/501")
    monkeypatch.setattr(gw, "_append_launchd_reload_log", lambda msg: None)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr(gw, "_retry_launchctl_bootstrap_until_registered", lambda *a, **k: True)
    monkeypatch.setattr(gw, "_clear_launchd_unsupported_marker", lambda: None)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or MagicMock(returncode=0))
    return plist_path, calls


def test_refresh_refuses_to_drop_protected_key(home, tmp_path, monkeypatch, capsys):
    plist_path, calls = _stale(tmp_path, monkeypatch, {"HERMES_WRITE_SAFE_ROOT": SAFE_ROOT})
    before = plist_path.read_bytes()
    assert gw.refresh_launchd_plist_if_needed() is False
    assert plist_path.read_bytes() == before
    assert calls == []
    assert "HERMES_WRITE_SAFE_ROOT" in capsys.readouterr().out


def test_refresh_keeps_protected_key_from_extra_file(home, tmp_path, monkeypatch):
    (home / launchd._LAUNCHD_EXTRA_ENV_FILE).write_text(f"HERMES_WRITE_SAFE_ROOT={SAFE_ROOT}\n", encoding="utf-8")
    plist_path, _ = _stale(tmp_path, monkeypatch, {"HERMES_WRITE_SAFE_ROOT": SAFE_ROOT})
    assert gw.refresh_launchd_plist_if_needed() is True
    assert _env(plist_path.read_text(encoding="utf-8"))["HERMES_WRITE_SAFE_ROOT"] == SAFE_ROOT


def test_refresh_without_protected_key_unchanged(home, tmp_path, monkeypatch):
    plist_path, _ = _stale(tmp_path, monkeypatch, {"PATH": "/usr/bin"})
    assert gw.refresh_launchd_plist_if_needed() is True
    assert "HERMES_WRITE_SAFE_ROOT" not in _env(plist_path.read_text(encoding="utf-8"))


def test_refresh_refuses_when_new_definition_unparseable(home, tmp_path, monkeypatch):
    plist_path, calls = _stale(tmp_path, monkeypatch, {"HERMES_WRITE_SAFE_ROOT": SAFE_ROOT})
    monkeypatch.setattr(gw, "generate_launchd_plist", lambda: "<new/>")
    before = plist_path.read_bytes()
    assert gw.refresh_launchd_plist_if_needed() is False
    assert plist_path.read_bytes() == before
    assert calls == []


def test_force_install_refuses_to_drop_protected_key(home, tmp_path, monkeypatch, capsys):
    plist_path, _calls = _stale(tmp_path, monkeypatch, {"HERMES_WRITE_SAFE_ROOT": SAFE_ROOT})
    bootstraps = []
    monkeypatch.setattr(gw, "_launchctl_bootstrap", lambda *a, **k: bootstraps.append(a))
    before = plist_path.read_bytes()
    gw.launchd_install(force=True)
    assert plist_path.read_bytes() == before
    assert bootstraps == []
    assert "HERMES_WRITE_SAFE_ROOT" in capsys.readouterr().out


def test_refresh_refuses_when_installed_plist_is_truncated_but_carries_key(home, tmp_path, monkeypatch):
    plist_path, calls = _stale(tmp_path, monkeypatch, {"HERMES_WRITE_SAFE_ROOT": SAFE_ROOT})
    truncated = plist_path.read_bytes()[:-40]
    plist_path.write_bytes(truncated)
    assert gw.refresh_launchd_plist_if_needed() is False
    assert plist_path.read_bytes() == truncated
    assert calls == []


def test_parser_tolerates_bom_export_and_quotes(home):
    (home / launchd._LAUNCHD_EXTRA_ENV_FILE).write_bytes(
        ("﻿export HERMES_WRITE_SAFE_ROOT=" + '"' + SAFE_ROOT + '"' + "\n").encode("utf-8")
    )
    assert launchd._launchd_extra_env() == {"HERMES_WRITE_SAFE_ROOT": SAFE_ROOT}


def test_unreadable_extra_file_fails_closed(home, tmp_path, monkeypatch):
    (home / launchd._LAUNCHD_EXTRA_ENV_FILE).mkdir()  # IsADirectoryError on read
    assert launchd._launchd_extra_env() == {}
    _plist_path, calls = _stale(tmp_path, monkeypatch, {"HERMES_WRITE_SAFE_ROOT": SAFE_ROOT})
    assert gw.refresh_launchd_plist_if_needed() is False
    assert calls == []
