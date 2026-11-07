#!/usr/bin/env python3
"""One-command setup for the Grok Telegram Bridge: the author's preset, grok-tg, self-check.

Runs against bridge_setup.py in the public layout (tests/ next to bridge_setup.py)
and in the packaging layout this file is exported from. No network, no real tmux,
no real service manager: every command goes through an injected fake.
"""
# @mutate-why the author's preset stops mirroring terminal turns to the phone
# @mutate packaging/grok-telegram-bridge/bridge_setup.py | ("GRB_TUI_MIRROR_LOCAL", "1"), | ("GRB_TUI_MIRROR_LOCAL", "0"),
# @mutate-why grok-tg detaches the other terminal that is already attached
# @mutate packaging/grok-telegram-bridge/bridge_setup.py | attach_argv = [tmux_bin, "-L", socket, "attach", "-t", session] | attach_argv = [tmux_bin, "-L", socket, "attach", "-d", "-t", session]
# @mutate-why the outside-session warning counts the bridge pane's own Grok
# @mutate packaging/grok-telegram-bridge/bridge_setup.py | if tty in NO_TTY or tty in {pane_name, pane}: | if tty in NO_TTY:
# @mutate-why doctor reads Linger=no as on, so a Linux service that stops at logout gets no warning
# @mutate packaging/grok-telegram-bridge/bridge_setup.py | if linger == "yes": | if linger == "no":
# @mutate-why doctor stops warning about a Grok on another terminal, which the phone cannot see
# @mutate packaging/grok-telegram-bridge/bridge_setup.py | outside = warn_grok_outside_bridge(config, run=run, report=report.warn) | outside = 0
# @mutate-why doctor exits 0 after a failed check, so a script cannot tell a broken install
# @mutate packaging/grok-telegram-bridge/bridge_setup.py | return 1 if report.failures else 0 | return 0
# @mutate-why setup writes over a service file it did not write, with no copy and no warning
# @mutate packaging/grok-telegram-bridge/bridge_setup.py | if not path.exists() or service_file_is_ours(path): | if True:
from __future__ import annotations

import importlib.util
import io
import json
import os
import plistlib
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve()
CANDIDATES = (
    HERE.parents[1] / "bridge_setup.py",
    HERE.parents[2] / "packaging" / "grok-telegram-bridge" / "bridge_setup.py",
)
SETUP_PATH = next((path for path in CANDIDATES if path.is_file()), CANDIDATES[0])


def load_setup():
    spec = importlib.util.spec_from_file_location("grok_bridge_setup_under_test", SETUP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


setup = load_setup()
PRESET_SWITCHES = {
    "GRB_CHAT_LANE": "tui",
    "GRB_TUI_MIRROR_LOCAL": "1",
    "GRB_TUI_FALLBACK_HEADLESS": "0",
    "GRB_STOP_BUTTON": "1",
    "GRB_SEND_NOW_BUTTON": "1",
    "GRB_QUESTION_PIN": "1",
}


def completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


class FakeTelegram:
    def __init__(self, *, updates=None, send_ok=True, get_me=None):
        self.calls = []
        self.updates = list(updates or [])
        self.send_ok = send_ok
        self.get_me = get_me or {"ok": True, "result": {"username": "grokbot"}}

    def __call__(self, token, method, timeout=60, **params):
        self.calls.append((method, dict(params)))
        if method == "getMe":
            return self.get_me
        if method == "getUpdates":
            return {"ok": True, "result": self.updates}
        if method == "sendMessage":
            return {"ok": self.send_ok, "result": {"message_id": 7}}
        return {"ok": True, "result": True}

    def methods(self):
        return [method for method, _ in self.calls]


class FakeRun:
    """tmux, ps, launchctl, systemctl and the session start script, all fake."""

    def __init__(self, *, alive=False, start_rc=0, pane_tty="/dev/ttys003", ps_text="", clients="",
                 systemctl_rc=0, service_state="active", linger="yes", launchctl_rc=0):
        self.calls = []
        self.envs = []
        self.alive = alive
        self.start_rc = start_rc
        self.pane_tty = pane_tty
        self.ps_text = ps_text
        self.clients = clients
        self.systemctl_rc = systemctl_rc
        self.service_state = service_state  # "" = systemd --user is not available
        self.linger = linger
        self.launchctl_rc = launchctl_rc

    def __call__(self, cmd, env=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        self.envs.append(env)
        if cmd[0] == "bash" and cmd[-1].endswith("grok-tui-session-start.sh"):
            if self.start_rc in (0, 9):
                self.alive = True
            return completed(cmd, self.start_rc, stderr="tmux new-session failed" if self.start_rc else "")
        if "has-session" in cmd:
            return completed(cmd, 0 if self.alive else 1)
        if "display-message" in cmd:
            return completed(cmd, stdout=self.pane_tty + "\n")
        if "list-clients" in cmd:
            return completed(cmd, stdout=self.clients)
        if cmd[0] == "ps":
            return completed(cmd, stdout=self.ps_text)
        if cmd[0] == "systemctl" and "is-active" in cmd:
            if not self.service_state:
                return completed(cmd, 1, stderr="Failed to connect to bus: No medium found")
            return completed(cmd, 0 if self.service_state == "active" else 3, stdout=self.service_state + "\n")
        if cmd[0] == "systemctl":
            return completed(cmd, self.systemctl_rc)
        if cmd[0] == "loginctl":
            return completed(cmd, stdout=f"Linger={self.linger}\n")
        if cmd[:2] == ["launchctl", "print"]:
            return completed(cmd, self.launchctl_rc, stderr="" if self.launchctl_rc == 0 else "Could not find service")
        return completed(cmd)

    def index(self, predicate):
        for number, cmd in enumerate(self.calls):
            if predicate(cmd):
                return number
        return -1


def which_all(name):
    return {"grok": "/opt/grok/bin/grok", "tmux": "/opt/tmux/bin/tmux"}.get(name)


def doctor_which(name):
    """which() for doctor: setup writes full paths to bridge.env, PATH knows the short names."""
    if name in {"/opt/grok/bin/grok", "/opt/tmux/bin/tmux"}:
        return name
    return which_all(name)


class SetupCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.root / "home")})
        self.env.start()
        for key in [key for key in os.environ if key.startswith("GRB_")]:
            os.environ.pop(key)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def options(self, **overrides):
        values = dict(
            config_file=self.root / "config" / "bridge.env",
            token_file=self.root / "config" / "token.json",
            state_dir=self.root / "state",
            runner_file=self.root / "bin" / "grok-telegram-bridge-run",
            attach_link=self.root / "bin" / "grok-tg",
            chat_id="12345",
            install_service=False,
            start_service=False,
            send_test=False,
            non_interactive=True,
            like_author=True,
        )
        values.update(overrides)
        return setup.SetupOptions(**values)

    def run_setup(self, options, *, api=None, run=None, which=which_all, os_name="Linux", **kwargs):
        api = api or FakeTelegram()
        run = run or FakeRun()
        os.environ.setdefault("GRB_BOT_TOKEN", "TESTONLY-token")
        # The live self-check waits on real time; SelfCheckTest drives it with a fake clock.
        kwargs.setdefault("self_check", lambda **_kwargs: [])
        output = io.StringIO()
        with redirect_stdout(output):
            rc = setup.setup_bridge(options, api_call=api, run=run, which=which, os_name=os_name, **kwargs)
        return rc, output.getvalue(), api, run


class AuthorPresetTest(SetupCase):
    def test_like_author_writes_switches_keeps_other_settings_and_backs_up(self):
        options = self.options()
        options.config_file.parent.mkdir(parents=True)
        options.config_file.write_text(
            "GRB_CHAT_LANE=headless\nGRB_GROK_DISALLOWED_TOOLS=web_search\nexport GRB_TUI_MIRROR_LOCAL=0\n",
            encoding="utf-8",
        )
        rc, text, _api, _run = self.run_setup(options)
        self.assertEqual(rc, 0, text)
        backups = sorted(options.config_file.parent.glob("bridge.env.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertIn("GRB_GROK_DISALLOWED_TOOLS=web_search", backups[0].read_text(encoding="utf-8"))
        env = setup.load_env_file(options.config_file)
        for key, value in PRESET_SWITCHES.items():
            self.assertEqual(env.get(key), value, key)
        self.assertEqual(env["GRB_GROK_DISALLOWED_TOOLS"], "web_search")
        self.assertEqual(env["GRB_TMUX_SOCKET"], "grok")
        self.assertEqual(env["GRB_TMUX_SESSION"], "grok")
        self.assertEqual(env["GRB_CHAT_ID"], "12345")
        self.assertEqual(env["GRB_GROK_BIN"], "/opt/grok/bin/grok")
        self.assertEqual(env["GRB_TMUX_BIN"], "/opt/tmux/bin/tmux")
        body = options.config_file.read_text(encoding="utf-8")
        self.assertEqual(body.count("GRB_CHAT_LANE="), 1)
        self.assertEqual(body.count("GRB_TUI_MIRROR_LOCAL="), 1)
        self.assertEqual(stat.S_IMODE(options.config_file.stat().st_mode), 0o600)
        token = json.loads(options.token_file.read_text(encoding="utf-8"))
        self.assertEqual(token, {"api_key": "TESTONLY-token"})
        self.assertEqual(stat.S_IMODE(options.token_file.stat().st_mode), 0o600)
        self.assertNotIn("TESTONLY-token", text, "setup printed the bot token")

    def test_plain_setup_keeps_public_defaults_without_session_or_link(self):
        options = self.options(like_author=False)
        rc, text, _api, run = self.run_setup(options)
        self.assertEqual(rc, 0, text)
        env = setup.load_env_file(options.config_file)
        self.assertEqual(env["GRB_CHAT_LANE"], "headless")
        self.assertEqual(env["GRB_TUI_MIRROR_LOCAL"], "0")
        self.assertEqual(run.index(lambda cmd: cmd[0] == "bash"), -1, "plain setup started a tmux session")
        self.assertFalse(options.attach_link.exists() or options.attach_link.is_symlink())

    def test_interactive_wizard_asks_the_author_question_and_defaults_to_yes(self):
        prompts = []

        def fake_input(prompt=""):
            prompts.append(prompt)
            return ""

        with mock.patch("builtins.input", side_effect=fake_input):
            rc, text, _api, _run = self.run_setup(self.options(like_author=False, non_interactive=False))
        self.assertEqual(rc, 0, text)
        self.assertIn("Use the author's setup (recommended)? [Y/n] ", prompts)
        self.assertEqual(setup.load_env_file(self.options().config_file)["GRB_CHAT_LANE"], "tui")

    def test_interactive_wizard_answer_n_keeps_the_headless_lane(self):
        with mock.patch("builtins.input", side_effect=lambda prompt="": "n" if "author" in prompt else ""):
            rc, text, _api, _run = self.run_setup(self.options(like_author=False, non_interactive=False))
        self.assertEqual(rc, 0, text)
        env = setup.load_env_file(self.options().config_file)
        self.assertEqual(env["GRB_CHAT_LANE"], "headless")
        self.assertEqual(env["GRB_TUI_MIRROR_LOCAL"], "0")

    def test_dry_run_prints_the_plan_without_network_files_or_commands(self):
        def no_network(*args, **kwargs):
            raise AssertionError("dry run contacted Telegram")

        def no_commands(*args, **kwargs):
            raise AssertionError("dry run ran a command")

        options = self.options(dry_run=True, chat_id=None, install_service=True, start_service=True, send_test=True)
        rc, text, _api, _run = self.run_setup(options, api=no_network, run=no_commands)
        self.assertEqual(rc, 0, text)
        for key, value in PRESET_SWITCHES.items():
            self.assertIn(f"{key}={value}", text)
        self.assertIn("grok-tg", text)
        self.assertIn(str(options.config_file), text)
        self.assertIn("grok/grok", text)
        self.assertIn("systemd --user", text)
        self.assertIn("self-check", text)
        for path in (options.config_file, options.token_file, options.runner_file, options.attach_link):
            self.assertFalse(path.exists(), path)

    def test_session_starts_through_the_start_script_with_bridge_env_before_the_service(self):
        run = FakeRun(alive=False)
        options = self.options(install_service=True, start_service=True)
        rc, text, _api, _run = self.run_setup(options, run=run)
        self.assertEqual(rc, 0, text)
        start = run.index(lambda cmd: cmd[0] == "bash" and cmd[-1].endswith("grok-tui-session-start.sh"))
        self.assertGreaterEqual(start, 0, "the Grok session was not started")
        env = run.envs[start]
        self.assertEqual(env["GRB_TMUX_SOCKET"], "grok")
        self.assertEqual(env["GRB_TMUX_SESSION"], "grok")
        self.assertEqual(env["GRB_STATE_DIR"], str(options.state_dir))
        self.assertEqual(env["GRB_GROK_BIN"], "/opt/grok/bin/grok")
        service = run.index(lambda cmd: cmd[0] == "systemctl" and "restart" in cmd)
        self.assertGreater(service, start, "the service started before the session existed")

    def test_grok_tg_link_points_at_this_checkout_and_never_replaces_a_foreign_file(self):
        options = self.options()
        rc, text, _api, _run = self.run_setup(options, run=FakeRun(alive=True))
        self.assertEqual(rc, 0, text)
        self.assertTrue(options.attach_link.is_symlink())
        self.assertEqual(Path(os.readlink(options.attach_link)), setup.ATTACH_SCRIPT)

        options.attach_link.unlink()
        options.attach_link.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        rc, text, _api, _run = self.run_setup(options, run=FakeRun(alive=True))
        self.assertEqual(rc, 0, text)
        self.assertFalse(options.attach_link.is_symlink())
        self.assertIn("echo mine", options.attach_link.read_text(encoding="utf-8"))
        self.assertIn("grok-tg", text)

    def test_existing_chat_id_is_kept_and_the_example_placeholder_is_not(self):
        options = self.options(chat_id=None)
        options.config_file.parent.mkdir(parents=True)
        options.config_file.write_text("GRB_CHAT_ID=555\n", encoding="utf-8")
        rc, text, api, _run = self.run_setup(options, run=FakeRun(alive=True))
        self.assertEqual(rc, 0, text)
        self.assertNotIn("getUpdates", api.methods())
        self.assertEqual(setup.load_env_file(options.config_file)["GRB_CHAT_ID"], "555")

        options.config_file.write_text("GRB_CHAT_ID=123456789\n", encoding="utf-8")
        with self.assertRaises(setup.SetupError) as caught:
            self.run_setup(options, run=FakeRun(alive=True))
        self.assertIn("--chat-id", str(caught.exception))

    def test_saved_token_file_is_reused_without_a_prompt(self):
        options = self.options()
        options.token_file.parent.mkdir(parents=True)
        options.token_file.write_text(json.dumps({"api_key": "TESTONLY-saved"}), encoding="utf-8")
        os.environ.pop("GRB_BOT_TOKEN", None)
        api = FakeTelegram()
        output = io.StringIO()
        with mock.patch.object(setup.getpass, "getpass", side_effect=AssertionError("asked again")), redirect_stdout(output):
            rc = setup.setup_bridge(options, api_call=api, run=FakeRun(alive=True), which=which_all, os_name="Linux")
        self.assertEqual(rc, 0, output.getvalue())
        self.assertEqual(api.calls[0][0], "getMe")
        self.assertEqual(json.loads(options.token_file.read_text(encoding="utf-8"))["api_key"], "TESTONLY-saved")

    def test_missing_tmux_stops_the_author_setup_before_writing_anything(self):
        options = self.options()
        with self.assertRaises(setup.SetupError) as caught:
            self.run_setup(options, which=lambda name: "/opt/grok/bin/grok" if name == "grok" else None)
        self.assertIn("tmux", str(caught.exception))
        self.assertFalse(options.config_file.exists())
        self.assertFalse(options.token_file.exists())

    def test_self_check_runs_only_with_the_started_service_and_the_session(self):
        calls = []

        def fake_check(**kwargs):
            calls.append(kwargs)
            return []

        rc, text, _api, _run = self.run_setup(self.options(), run=FakeRun(alive=True), self_check=fake_check)
        self.assertEqual(rc, 0, text)
        self.assertEqual(calls, [])
        self.assertIn("Self-check skipped", text)

        options = self.options(install_service=True, start_service=True, send_test=True)
        rc, text, _api, _run = self.run_setup(options, run=FakeRun(alive=True), self_check=fake_check)
        self.assertEqual(rc, 0, text)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0]["bot_message_sent"], True)
        self.assertEqual(calls[0]["config"]["GRB_TMUX_SESSION"], "grok")


class AttachTest(SetupCase):
    def write_config(self, text):
        config = self.root / "bridge.env"
        config.write_text(text, encoding="utf-8")
        return config

    def attach(self, config, run, **kwargs):
        execs = []
        output = io.StringIO()
        with redirect_stdout(output):
            rc = setup.attach_command(config, run=run, exec_fn=lambda binary, argv: execs.append((binary, argv)), **kwargs)
        return rc, execs, output.getvalue()

    def test_attach_reads_socket_and_session_from_bridge_env(self):
        config = self.write_config("GRB_TMUX_BIN=/usr/bin/tmux\nGRB_TMUX_SOCKET=s1\nGRB_TMUX_SESSION=g1\n")
        run = FakeRun(alive=True)
        rc, execs, text = self.attach(config, run)
        self.assertEqual(rc, 0, text)
        self.assertIn(["/usr/bin/tmux", "-L", "s1", "has-session", "-t", "=g1"], run.calls)
        self.assertEqual(execs, [("/usr/bin/tmux", ["/usr/bin/tmux", "-L", "s1", "attach", "-t", "g1"])])
        self.assertEqual(run.index(lambda cmd: cmd[0] == "bash"), -1)

    def test_attach_starts_a_missing_session_with_bridge_env_then_attaches(self):
        config = self.write_config("GRB_TMUX_SOCKET=grok\nGRB_TMUX_SESSION=grok\nGRB_STATE_DIR=/tmp/grok-state\n")
        run = FakeRun(alive=False)
        rc, execs, text = self.attach(config, run)
        self.assertEqual(rc, 0, text)
        start = run.index(lambda cmd: cmd[0] == "bash" and cmd[-1].endswith("grok-tui-session-start.sh"))
        self.assertGreaterEqual(start, 0)
        self.assertEqual(run.envs[start]["GRB_STATE_DIR"], "/tmp/grok-state")
        self.assertEqual(execs, [("tmux", ["tmux", "-L", "grok", "attach", "-t", "grok"])])

    def test_attach_joins_another_terminal_without_detaching_it(self):
        config = self.write_config("GRB_TMUX_SOCKET=grok\nGRB_TMUX_SESSION=grok\n")
        run = FakeRun(alive=True, clients="/dev/ttys002: grok [120x40 xterm-256color]\n")
        rc, execs, text = self.attach(config, run)
        self.assertEqual(rc, 0)
        self.assertIn("another terminal", text)
        self.assertNotIn("-d", execs[0][1])

    def test_attach_without_bridge_env_or_session_fails_closed(self):
        rc, execs, text = self.attach(self.root / "missing.env", FakeRun(alive=True))
        self.assertEqual(rc, 2)
        self.assertEqual(execs, [])
        self.assertIn("setup", text)

        config = self.write_config("GRB_TMUX_SESSION=grok\n")
        rc, execs, _text = self.attach(config, FakeRun(alive=False, start_rc=1))
        self.assertEqual(rc, 2)
        self.assertEqual(execs, [])

        rc, execs, _text = self.attach(config, FakeRun(alive=False), create_missing=False)
        self.assertEqual(rc, 2)
        self.assertEqual(execs, [])

    def test_grok_tg_script_runs_attach_from_its_real_location(self):
        script = setup.ATTACH_SCRIPT
        self.assertTrue(script.is_file(), script)
        body = script.read_text(encoding="utf-8")
        self.assertIn("bridge_setup.py", body)
        self.assertIn("attach", body)
        self.assertIn("realpath", body)
        self.assertEqual(subprocess.run(["bash", "-n", str(script)]).returncode, 0)


class OutsideSessionTest(SetupCase):
    PS = (
        "  101 ttys003  /Users/u/.local/bin/grok\n"
        "  102 ttys007  /Users/u/.local/bin/grok\n"
        "  103 pts/4    grok-1.0.66-lin\n"
        "  104 ??       /Users/u/.local/bin/grok\n"
        "  105 ttys009  grok-tg\n"
        "  106 pts/5    grok-telegram-b\n"
        "  107 ttys010  python3\n"
    )

    def test_only_grok_cli_processes_with_another_tty_are_reported(self):
        found = setup.grok_processes_outside_bridge("/dev/ttys003", self.PS)
        self.assertEqual(found, [("102", "ttys007"), ("103", "pts/4")])
        self.assertEqual(setup.grok_processes_outside_bridge("", self.PS), [])

    def test_setup_end_warns_about_grok_outside_the_bridge_session(self):
        rc, text, _api, run = self.run_setup(self.options(), run=FakeRun(alive=True, ps_text=self.PS))
        self.assertEqual(rc, 0, text)
        self.assertIn("outside the bridge tmux session", text)
        self.assertIn("pid 102", text)
        self.assertIn("grok-tg", text)
        pane = run.calls[run.index(lambda cmd: "display-message" in cmd)]
        self.assertIn("grok:0.0", pane)


class SelfCheckTest(SetupCase):
    def check(self, *, inbox_at=None, inbox_text="hi", health_at=None, logs=None, bot_message_sent=True):
        state = self.root / "state"
        state.mkdir(parents=True, exist_ok=True)
        clock = {"now": 1000.0}
        api = FakeTelegram()
        run = FakeRun(alive=True)
        logs = dict(logs or {})

        def sleep(seconds):
            clock["now"] += seconds
            if inbox_at is not None and clock["now"] >= inbox_at:
                with (state / "grok-bridge-grok.inbox.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"ts": clock["now"], "source": "telegram", "text": inbox_text}) + "\n")
            if health_at is not None and clock["now"] >= health_at:
                (state / "grok-bridge-grok.health.json").write_text(
                    json.dumps({"last_job_started_at": clock["now"]}), encoding="utf-8")

        class Tail:
            def __init__(self, since):
                self.since = since

            def read_new(self):
                due = [at for at in logs if at <= clock["now"]]
                return "".join(logs.pop(at) for at in sorted(due))

        output = io.StringIO()
        with redirect_stdout(output):
            lines = setup.author_self_check(
                token="TESTONLY-token", chat_id="12345",
                config={"GRB_TMUX_SOCKET": "grok", "GRB_TMUX_SESSION": "grok", "GRB_STATE_DIR": str(state)},
                os_name="Darwin", api_call=api, run=run, sleep=sleep, clock=lambda: clock["now"],
                log_tail=Tail, phone_timeout=10, terminal_timeout=10, bot_message_sent=bot_message_sent,
            )
        return lines, output.getvalue(), api, run

    def test_timeouts_give_a_reason_and_the_summary_goes_to_the_phone(self):
        lines, text, api, run = self.check()
        self.assertEqual(len(lines), 3)
        self.assertIn("1/3 bot -> phone: ok", lines[0])
        self.assertIn("no message from your phone", lines[1])
        self.assertIn("no phone card", lines[2])
        self.assertIn("GRB_TUI_MIRROR_LOCAL=1", lines[2])
        typed = [cmd for cmd in run.calls if "send-keys" in cmd]
        self.assertEqual(typed[0][:6], ["tmux", "-L", "grok", "send-keys", "-t", "grok:0.0"])
        self.assertIn(setup.SELF_CHECK_PROBE, typed[0])
        self.assertEqual(typed[1][-1], "Enter")
        method, params = api.calls[-1]
        self.assertEqual(method, "sendMessage")
        self.assertIn("no phone card", params["text"])
        for line in lines:
            self.assertIn(line, text)

    def test_both_directions_pass_on_inbox_and_log_markers(self):
        lines, _text, _api, _run = self.check(
            inbox_at=1002.0,
            logs={1004.0: "grok answer (telegram):\nhello\n", 1010.0: "grb_tui_lane local prompt sent\n"},
        )
        self.assertIn("2/3 phone -> terminal -> phone: ok", lines[1])
        self.assertIn("3/3 terminal -> phone: ok", lines[2])

    def test_the_pending_start_command_is_not_a_phone_message(self):
        lines, _text, _api, _run = self.check(inbox_at=1002.0, inbox_text="/start")
        self.assertIn("no message from your phone", lines[1])

    def test_message_that_reached_grok_without_an_answer_says_so(self):
        lines, _text, _api, _run = self.check(health_at=1002.0, bot_message_sent=False)
        self.assertIn("did not go through", lines[0])
        self.assertIn("reached the Grok session", lines[1])


class ServiceTest(SetupCase):
    def test_runner_loads_bridge_env_starts_the_tui_session_then_runs_the_bridge(self):
        runner = self.root / "bin" / "grok-telegram-bridge-run"
        config = self.root / "config" / "bridge.env"
        setup.install_runner(runner, config, python_bin="/usr/bin/python3", path_dirs=["/opt/grok/bin"])
        body = runner.read_text(encoding="utf-8")
        self.assertEqual(stat.S_IMODE(runner.stat().st_mode), 0o755)
        self.assertIn("set -a", body)
        self.assertIn(str(config), body)
        self.assertIn("grok-tui-session-start.sh", body)
        self.assertIn('"${GRB_CHAT_LANE:-headless}" = tui', body)
        self.assertIn("/opt/grok/bin", body)
        self.assertTrue(body.rstrip().endswith("grok_telegram_bridge.py"), body)
        self.assertEqual(subprocess.run(["bash", "-n", str(runner)]).returncode, 0)

    def test_launchd_and_systemd_units_point_at_the_runner(self):
        runner = self.root / "bin" / "grok-telegram-bridge-run"
        run = FakeRun()
        with redirect_stdout(io.StringIO()):
            plist_path, started = setup.install_service(runner, start=True, os_name="Darwin", run=run)
        plist = plistlib.loads(plist_path.read_bytes())
        self.assertEqual(plist["Label"], setup.LAUNCHD_LABEL)
        self.assertEqual(plist["ProgramArguments"], [str(runner)])
        self.assertTrue(plist["KeepAlive"])
        self.assertTrue(started)
        self.assertGreaterEqual(run.index(lambda cmd: cmd[:2] == ["launchctl", "bootstrap"]), 0)

        run = FakeRun()
        with redirect_stdout(io.StringIO()):
            unit_path, started = setup.install_service(runner, start=True, os_name="Linux", run=run)
        unit = unit_path.read_text(encoding="utf-8")
        self.assertIn(f"ExecStart={runner}", unit)
        self.assertIn("Restart=always", unit)
        self.assertIn("KillMode=process", unit, "a bridge restart would kill the Grok tmux session")
        self.assertTrue(started)
        self.assertIn(["systemctl", "--user", "enable", setup.SERVICE_NAME], run.calls)

    def test_missing_systemd_user_falls_back_to_the_runner_command(self):
        runner = self.root / "bin" / "grok-telegram-bridge-run"
        output = io.StringIO()
        with redirect_stdout(output):
            _path, started = setup.install_service(runner, start=True, os_name="Linux", run=FakeRun(systemctl_rc=1))
        self.assertFalse(started)
        self.assertIn(str(runner), output.getvalue())

    def test_parser_exposes_setup_presets_dry_run_and_attach(self):
        parser = setup.build_parser()
        args = parser.parse_args(["setup", "--like-author", "--dry-run", "--non-interactive"])
        self.assertTrue(args.like_author)
        self.assertTrue(args.dry_run)
        self.assertTrue(args.non_interactive)
        self.assertEqual(parser.parse_args(["attach"]).command, "attach")
        self.assertEqual(setup.ATTACH_COMMAND, "grok-tg")


class DoctorTest(SetupCase):
    TOKEN = "TESTONLY-doctor-token"
    PS_PANE = "  101 ttys003  /Users/u/.local/bin/grok\n  104 ??       /Users/u/.local/bin/grok\n"

    def install(self, *, lane="tui", chat_id="12345", env_mode=0o600, token_mode=0o600, extra=()):
        """A finished install: bridge.env, token.json and the systemd unit, as setup writes them."""
        config_dir = self.root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        token_file = config_dir / "token.json"
        token_file.write_text(json.dumps({"api_key": self.TOKEN}), encoding="utf-8")
        token_file.chmod(token_mode)
        lines = [f"GRB_TOKEN_FILE={token_file}", f"GRB_CHAT_LANE={lane}", "GRB_GROK_BIN=/opt/grok/bin/grok",
                 "GRB_TMUX_BIN=/opt/tmux/bin/tmux", "GRB_TMUX_SOCKET=grok", "GRB_TMUX_SESSION=grok", *extra]
        if chat_id is not None:
            lines.append(f"GRB_CHAT_ID={chat_id}")
        config = config_dir / "bridge.env"
        config.write_text("\n".join(lines) + "\n", encoding="utf-8")
        config.chmod(env_mode)
        unit = setup.default_systemd_unit_file()
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text("[Unit]\n", encoding="utf-8")
        return config

    def doctor(self, config, *, run=None, api=None, which=doctor_which, os_name="Linux", offline=False):
        run = run if run is not None else FakeRun(alive=True, ps_text=self.PS_PANE)
        api = api if api is not None else FakeTelegram()
        output = io.StringIO()
        with redirect_stdout(output):
            rc = setup.doctor(config, offline=offline, api_call=api, run=run, which=which, os_name=os_name,
                              user="tester")
        return rc, output.getvalue(), run, api

    def lines(self, text, word):
        """The [ok]/[warn]/[fail] lines that mention `word`, in order."""
        return [line for line in text.splitlines() if line.startswith("[") and word in line.lower()]

    def test_healthy_install_prints_only_ok_lines_and_exits_zero(self):
        rc, text, run, api = self.doctor(self.install())
        self.assertEqual(rc, 0, text)
        self.assertNotIn("[warn]", text)
        self.assertNotIn("[fail]", text)
        for needle in ("bridge.env", "token file", "@grokbot", "/opt/grok/bin/grok", "grok/grok", "pid 101",
                       setup.SERVICE_NAME, "linger"):
            self.assertIn(needle, text)
        self.assertIn("doctor complete: 0 failure(s), 0 warning(s)", text)
        self.assertEqual(api.methods(), ["getMe"], "doctor contacts Telegram once at most")
        self.assertIn(["systemctl", "--user", "is-active", setup.SERVICE_NAME], run.calls)
        self.assertIn(["loginctl", "show-user", "tester", "-p", "Linger"], run.calls)
        self.assertNotIn(self.TOKEN, text)

    def test_missing_bridge_env_fails_with_exit_code_one_and_checks_nothing_else(self):
        rc, text, run, api = self.doctor(self.root / "config" / "bridge.env")
        self.assertEqual(rc, 1, text)
        self.assertTrue(text.splitlines()[1].startswith("[fail]"), text)
        self.assertIn("bridge_setup.py setup", text)
        self.assertEqual(run.calls, [])
        self.assertEqual(api.calls, [])

    def test_files_others_can_read_warn_with_chmod_and_the_token_is_never_printed(self):
        rc, text, _run, _api = self.doctor(self.install(env_mode=0o644, token_mode=0o644))
        self.assertEqual(rc, 0, text)
        warnings = [line for line in text.splitlines() if line.startswith("[warn]")]
        self.assertEqual(len(warnings), 2, text)
        for line in warnings:
            self.assertIn("chmod 600", line)
        self.assertNotIn(self.TOKEN, text)

    def test_refused_token_fails_and_no_answer_from_telegram_only_warns(self):
        refused = FakeTelegram(get_me={"ok": False, "error_code": 401, "description": "Unauthorized"})
        rc, text, _run, _api = self.doctor(self.install(), api=refused)
        self.assertEqual(rc, 1, text)
        self.assertTrue(self.lines(text, "telegram")[0].startswith("[fail]"), text)
        self.assertIn("Unauthorized", text)
        self.assertNotIn(self.TOKEN, text)

        rc, text, _run, _api = self.doctor(self.install(), api=lambda *args, **kwargs: None)
        self.assertEqual(rc, 0, text)
        self.assertTrue(self.lines(text, "telegram")[0].startswith("[warn]"), text)

    def test_offline_never_contacts_telegram(self):
        def no_network(*args, **kwargs):
            raise AssertionError("doctor --offline contacted Telegram")

        rc, text, _run, _api = self.doctor(self.install(), api=no_network, offline=True)
        self.assertEqual(rc, 0, text)
        self.assertIn("--offline", text)

    def test_missing_or_placeholder_chat_id_and_a_missing_token_file_fail(self):
        for chat_id in (None, setup.EXAMPLE_CHAT_ID, "my-chat"):
            rc, text, _run, _api = self.doctor(self.install(chat_id=chat_id))
            self.assertEqual(rc, 1, (chat_id, text))
            self.assertTrue(self.lines(text, "grb_chat_id")[0].startswith("[fail]"), text)

        config = self.install()
        (self.root / "config" / "token.json").write_text(json.dumps({"api_key": ""}), encoding="utf-8")
        rc, text, _run, api = self.doctor(config)
        self.assertEqual(rc, 1, text)
        self.assertIn("api_key", text)
        (self.root / "config" / "token.json").unlink()
        rc, text, _run, api = self.doctor(config)
        self.assertEqual(rc, 1, text)
        self.assertIn("token file missing", text)
        self.assertEqual(api.calls, [], "doctor asked Telegram without a token")

    def test_linger_off_warns_with_the_command_that_turns_it_on_and_never_runs_it(self):
        config = self.install()
        rc, text, run, _api = self.doctor(config, run=FakeRun(alive=True, ps_text=self.PS_PANE, linger="no"))
        self.assertEqual(rc, 0, text)
        self.assertTrue(self.lines(text, "linger")[0].startswith("[warn]"), text)
        self.assertIn("log out", text)
        self.assertIn("sudo loginctl enable-linger tester", text)
        self.assertFalse(any("enable-linger" in cmd for cmd in run.calls), "doctor turned linger on by itself")

        rc, text, _run, _api = self.doctor(config, run=FakeRun(alive=True, ps_text=self.PS_PANE, linger="yes"))
        self.assertTrue(self.lines(text, "linger")[0].startswith("[ok]"), text)
        self.assertNotIn("enable-linger", text)

    def test_systemd_service_not_running_not_installed_or_unavailable_warns(self):
        config = self.install()
        rc, text, _run, _api = self.doctor(config, run=FakeRun(alive=True, ps_text=self.PS_PANE,
                                                               service_state="inactive"))
        self.assertEqual(rc, 0, text)
        self.assertTrue(self.lines(text, "service")[0].startswith("[warn]"), text)
        self.assertIn(f"systemctl --user restart {setup.SERVICE_NAME}", text)

        setup.default_systemd_unit_file().unlink()
        rc, text, _run, _api = self.doctor(config, run=FakeRun(alive=True, ps_text=self.PS_PANE,
                                                               service_state="inactive"))
        self.assertIn("not installed", text)

        rc, text, run, _api = self.doctor(config, run=FakeRun(alive=True, ps_text=self.PS_PANE, service_state=""))
        self.assertIn("systemd --user is not available", text)
        self.assertFalse(any(cmd[0] == "loginctl" for cmd in run.calls), "linger checked without systemd")

    def test_launchd_service_on_macos_and_no_linger_check(self):
        config = self.install()
        rc, text, run, _api = self.doctor(config, os_name="Darwin")
        self.assertEqual(rc, 0, text)
        self.assertIn(f"[ok] service: launchd {setup.LAUNCHD_LABEL} is loaded", text)
        self.assertFalse(any(cmd[0] in {"loginctl", "systemctl"} for cmd in run.calls))

        plist = setup.default_launchd_plist_file()
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(b"")
        not_loaded = FakeRun(alive=True, ps_text=self.PS_PANE, launchctl_rc=113)
        rc, text, _run, _api = self.doctor(config, os_name="Darwin", run=not_loaded)
        self.assertEqual(rc, 0, text)
        self.assertTrue(self.lines(text, "service")[0].startswith("[warn]"), text)
        self.assertIn("launchctl bootstrap", text)

        plist.unlink()
        rc, text, _run, _api = self.doctor(config, os_name="Darwin",
                                           run=FakeRun(alive=True, ps_text=self.PS_PANE, launchctl_rc=113))
        self.assertIn("not installed", text)

    def test_tui_lane_needs_tmux_and_the_session_and_the_headless_lane_needs_neither(self):
        rc, text, _run, _api = self.doctor(self.install(), run=FakeRun(alive=False))
        self.assertEqual(rc, 1, text)
        self.assertIn("[fail] tmux session grok/grok is not running", text)
        self.assertIn("grok-tg", text)

        config = self.install(extra=("GRB_TUI_FALLBACK_HEADLESS=1",))
        rc, text, _run, _api = self.doctor(config, run=FakeRun(alive=False))
        self.assertEqual(rc, 0, text)
        self.assertIn("[warn] tmux session grok/grok is not running", text)

        no_tmux = lambda name: "/opt/grok/bin/grok" if "grok" in name else None  # noqa: E731
        rc, text, _run, _api = self.doctor(self.install(), which=no_tmux)
        self.assertEqual(rc, 1, text)
        self.assertTrue(self.lines(text, "tmux")[0].startswith("[fail]"), text)

        rc, text, run, _api = self.doctor(self.install(lane="headless"), which=no_tmux, run=FakeRun(alive=False))
        self.assertEqual(rc, 0, text)
        self.assertIn("headless", text)
        self.assertEqual([cmd for cmd in run.calls if "has-session" in cmd or cmd[0] == "ps"], [])

        rc, text, _run, _api = self.doctor(self.install(), which=lambda name: None)
        self.assertEqual(rc, 1, text)
        self.assertTrue(self.lines(text, "grok cli")[0].startswith("[fail]"), text)

    def test_grok_in_the_pane_is_ok_and_grok_on_another_terminal_warns_with_grok_tg(self):
        rc, text, _run, _api = self.doctor(self.install(), run=FakeRun(alive=True, ps_text=OutsideSessionTest.PS))
        self.assertEqual(rc, 0, text)
        self.assertIn("[ok] Grok is running in the bridge pane grok:0.0 (pid 101)", text)
        self.assertIn("[warn] Grok is running outside the bridge tmux session", text)
        self.assertIn("pid 102 on ttys007", text)
        self.assertIn("grok-tg", text)

        rc, text, _run, _api = self.doctor(self.install(), run=FakeRun(alive=True, ps_text="  104 ??  grok\n"))
        self.assertEqual(rc, 0, text)
        self.assertIn("[warn] no Grok process in the bridge pane grok:0.0", text)
        self.assertNotIn("outside the bridge tmux session", text)

    def test_setup_ends_by_pointing_at_doctor(self):
        rc, text, _api, _run = self.run_setup(self.options(), run=FakeRun(alive=True))
        self.assertEqual(rc, 0, text)
        last = [line for line in text.splitlines() if line.strip()][-1]
        self.assertIn("python3 bridge_setup.py doctor", last)

    def test_telegram_call_returns_the_refusal_body_of_an_http_error(self):
        body = io.BytesIO(b'{"ok": false, "error_code": 401, "description": "Unauthorized"}')
        refusal = urllib.error.HTTPError("https://api.telegram.org/bot-x/getMe", 401, "Unauthorized", {}, body)
        with mock.patch.object(setup.urllib.request, "urlopen", side_effect=refusal):
            payload = setup.telegram_call("TESTONLY-token", "getMe", timeout=5)
        self.assertEqual(payload, {"ok": False, "error_code": 401, "description": "Unauthorized"})
        with mock.patch.object(setup.urllib.request, "urlopen", side_effect=OSError("no route to host")):
            self.assertIsNone(setup.telegram_call("TESTONLY-token", "getMe", timeout=5))

    def test_parser_and_main_route_doctor_and_offline(self):
        config = self.root / "custom.env"
        args = setup.build_parser().parse_args(["doctor", "--offline", "--config", str(config)])
        self.assertEqual((args.command, args.offline, args.config), ("doctor", True, config))
        self.assertFalse(setup.build_parser().parse_args(["doctor"]).offline)
        with mock.patch.object(setup, "doctor", return_value=1) as fake:
            self.assertEqual(setup.main(["doctor", "--offline", "--config", str(config)]), 1)
        fake.assert_called_once_with(config, offline=True)


class ServiceFileSafetyTest(SetupCase):
    FOREIGN_UNIT = "[Service]\nExecStart=/usr/local/bin/someone-elses-bridge\n"

    def foreign(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def backups(self, options, name):
        return sorted(options.config_file.parent.glob(f"{name}.bak-*"))

    def test_foreign_unit_is_copied_beside_bridge_env_with_a_warning_then_replaced(self):
        unit = self.foreign(setup.default_systemd_unit_file(), self.FOREIGN_UNIT)
        options = self.options(install_service=True, start_service=True)
        rc, text, _api, run = self.run_setup(options, run=FakeRun(alive=True))
        self.assertEqual(rc, 0, text)
        backups = self.backups(options, setup.SERVICE_NAME)
        self.assertEqual(len(backups), 1, text)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), self.FOREIGN_UNIT)
        self.assertEqual(list(unit.parent.glob("*.bak-*")), [], "a copy beside the unit could be loaded again")
        self.assertTrue(any(line.startswith("[warn]") and str(unit) in line and str(backups[0]) in line
                            for line in text.splitlines()), text)
        body = unit.read_text(encoding="utf-8")
        self.assertIn(setup.SERVICE_FILE_MARKER, body)
        self.assertIn(f"ExecStart={options.runner_file}", body)
        self.assertGreaterEqual(run.index(lambda cmd: cmd[:3] == ["systemctl", "--user", "restart"]), 0)

    def test_a_unit_written_by_setup_is_replaced_without_a_copy_or_a_warning(self):
        options = self.options(install_service=True, start_service=True)
        self.run_setup(options, run=FakeRun(alive=True))
        rc, text, _api, _run = self.run_setup(options, run=FakeRun(alive=True))
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.backups(options, setup.SERVICE_NAME), [])
        self.assertNotIn("not written by bridge_setup.py", text)

    def test_foreign_launchd_plist_is_copied_and_the_new_one_carries_the_marker(self):
        plist = self.foreign(setup.default_launchd_plist_file(), "<plist>someone else</plist>\n")
        options = self.options(install_service=True, start_service=True)
        rc, text, _api, _run = self.run_setup(options, run=FakeRun(alive=True), os_name="Darwin")
        self.assertEqual(rc, 0, text)
        backups = self.backups(options, plist.name)
        self.assertEqual(len(backups), 1, text)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "<plist>someone else</plist>\n")
        self.assertEqual(list(plist.parent.glob("*.bak-*")), [])
        self.assertIn(setup.SERVICE_FILE_MARKER.encode(), plist.read_bytes())
        self.assertEqual(plistlib.loads(plist.read_bytes())["Label"], setup.LAUNCHD_LABEL)

    def test_interactive_setup_asks_first_and_n_keeps_the_foreign_unit_and_the_service(self):
        unit = self.foreign(setup.default_systemd_unit_file(), self.FOREIGN_UNIT)
        options = self.options(install_service=True, start_service=True, non_interactive=False)
        prompts = []

        def answer(prompt=""):
            prompts.append(prompt)
            return "n" if "Back it up" in prompt else ""

        with mock.patch("builtins.input", side_effect=answer):
            rc, text, _api, run = self.run_setup(options, run=FakeRun(alive=True))
        self.assertEqual(rc, 0, text)
        self.assertTrue(any(str(unit) in prompt and prompt.endswith("[Y/n] ") for prompt in prompts), prompts)
        self.assertEqual(unit.read_text(encoding="utf-8"), self.FOREIGN_UNIT)
        self.assertEqual(self.backups(options, setup.SERVICE_NAME), [])
        self.assertEqual(run.index(lambda cmd: cmd[0] == "systemctl"), -1, "setup touched the service after n")
        self.assertIn("kept", text)

        with mock.patch("builtins.input", side_effect=lambda prompt="": ""):
            rc, text, _api, _run = self.run_setup(options, run=FakeRun(alive=True))
        self.assertEqual(rc, 0, text)
        self.assertEqual(len(self.backups(options, setup.SERVICE_NAME)), 1, text)
        self.assertIn(setup.SERVICE_FILE_MARKER, unit.read_text(encoding="utf-8"))

    def test_dry_run_shows_the_service_file_and_the_files_setup_writes_over(self):
        def no_network(*args, **kwargs):
            raise AssertionError("dry run contacted Telegram")

        def no_commands(*args, **kwargs):
            raise AssertionError("dry run ran a command")

        options = self.options(dry_run=True, install_service=True, start_service=True)
        rc, text, _api, _run = self.run_setup(options, api=no_network, run=no_commands)
        self.assertEqual(rc, 0, text)
        self.assertIn(f"service file: {setup.default_systemd_unit_file()}", text)
        self.assertIn("files already there that setup writes over: none", text)

        options.config_file.parent.mkdir(parents=True)
        options.config_file.write_text("GRB_CHAT_ID=555\n", encoding="utf-8")
        options.runner_file.parent.mkdir(parents=True)
        options.runner_file.write_text("#!/bin/sh\n", encoding="utf-8")
        unit = self.foreign(setup.default_systemd_unit_file(), self.FOREIGN_UNIT)
        before = {item: item.read_bytes() for item in (options.config_file, options.runner_file, unit)}
        rc, text, _api, _run = self.run_setup(options, api=no_network, run=no_commands)
        self.assertEqual(rc, 0, text)
        listed = text.split("files already there that setup writes over:", 1)[1]
        for item in before:
            self.assertIn(str(item), listed)
        self.assertIn("not written by bridge_setup.py", listed)
        for item, body in before.items():
            self.assertEqual(item.read_bytes(), body, item)
        self.assertEqual(list(options.config_file.parent.glob("*.bak-*")), [])


if __name__ == "__main__":
    unittest.main()
