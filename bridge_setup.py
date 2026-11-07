#!/usr/bin/env python3
"""Set up Grok Telegram Bridge with one command. Standard library only.

Run it from the cloned repository:

    python3 bridge_setup.py setup                # first question: the author's setup? [Y/n]
    python3 bridge_setup.py setup --like-author  # the author's setup without the question
    python3 bridge_setup.py setup --dry-run      # print the plan; write and send nothing
    python3 bridge_setup.py attach               # what the grok-tg command runs
    python3 bridge_setup.py doctor               # check the install any time; --offline skips Telegram

Setup asks for the BotFather token at a hidden prompt (or reuses the saved
token file), finds your chat id when you send /start to the bot, writes
token.json and bridge.env (an existing bridge.env is backed up and its other
settings are kept), starts Grok in the bridge tmux session, installs a
background service (launchd on macOS, systemd --user on Linux), links
grok-tg, and sends a test message. The author's setup ends with a self-check
that runs each direction once and reports the result in the terminal and in
the chat. `doctor` checks an install at any time and changes nothing.
"""
from __future__ import annotations

import argparse
import dataclasses
import getpass
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


APP_NAME = "grok-telegram-bridge"
SERVICE_NAME = f"{APP_NAME}.service"
LAUNCHD_LABEL = "com.user.grok-telegram-bridge"
# The line every service file setup writes carries. A file without it was not
# written by this setup, so setup keeps a copy before it writes over that file.
SERVICE_FILE_MARKER = f"Written by bridge_setup.py for {APP_NAME}"
REPO_DIR = Path(__file__).resolve().parent
BRIDGE_SCRIPT = REPO_DIR / "grok_telegram_bridge.py"
SESSION_START_SCRIPT = REPO_DIR / "grok-tui-session-start.sh"
ATTACH_SCRIPT = REPO_DIR / "grok-tg"
ATTACH_COMMAND = "grok-tg"
DOCTOR_COMMAND = "python3 bridge_setup.py doctor"
SETUP_TOTAL_STEPS = 6
AUTHOR_PRESET_QUESTION = "Use the author's setup (recommended)? [Y/n] "
# "Like the author": the switches the maintainer's own bridge runs with.
AUTHOR_PRESET_ENV: tuple[tuple[str, str], ...] = (
    ("GRB_CHAT_LANE", "tui"),
    ("GRB_TUI_MIRROR_LOCAL", "1"),
    ("GRB_TUI_FALLBACK_HEADLESS", "0"),
    ("GRB_STOP_BUTTON", "1"),
    ("GRB_SEND_NOW_BUTTON", "1"),
    ("GRB_QUESTION_PIN", "1"),
)
# Answering n keeps the public defaults: one hidden `grok -p` run per message.
PLAIN_ENV: tuple[tuple[str, str], ...] = (
    ("GRB_CHAT_LANE", "headless"),
    ("GRB_TUI_MIRROR_LOCAL", "0"),
)
TMUX_SOCKET = "grok"
TMUX_SESSION = "grok"
EXAMPLE_CHAT_ID = "123456789"  # the placeholder in config.example.env
CONFIG_HEADER = "# Grok Telegram Bridge settings written by bridge_setup.py. Keep this file private."
SELF_CHECK_PHONE_TIMEOUT = 60.0
SELF_CHECK_TERMINAL_TIMEOUT = 90.0
SELF_CHECK_PROBE = "Reply with the single word OK. (bridge setup self-check)"
# Bridge log lines the self-check waits for. The bridge prints the first right
# before it sends an answer to a phone request, the others when it sends a
# prompt typed in the tmux session (or that prompt's answer) to the phone.
PHONE_ANSWER_MARKER = "grok answer (telegram)"
TERMINAL_MIRROR_MARKERS = ("grb_tui_lane local prompt", "grok answer (tui-local)")
# A Grok CLI process: the `grok` launcher or a versioned binary such as
# grok-1.0.66-mac (Linux cuts process names to 15 characters).
GROK_PROCESS_NAME = re.compile(r"grok(-\d[\w.-]*)?")
NO_TTY = frozenset({"", "?", "??", "-"})
TRUE_WORDS = frozenset({"1", "true", "yes", "y", "on"})  # the bridge's own reading of a boolean setting

ApiCall = Callable[..., "dict[str, Any] | None"]
RunCommand = Callable[..., "subprocess.CompletedProcess[str]"]
Which = Callable[[str], "str | None"]


class SetupError(RuntimeError):
    """Expected setup error shown without a traceback."""


@dataclass(frozen=True)
class SetupOptions:
    config_file: Path
    token_file: Path
    state_dir: Path
    runner_file: Path
    attach_link: Path
    chat_id: str | None = None
    language: str = "en"
    wait_timeout: int = 180
    install_service: bool = True
    start_service: bool = True
    send_test: bool = True
    non_interactive: bool = False
    like_author: bool = False
    dry_run: bool = False


def out(message: str = "") -> None:
    print(message, flush=True)


def ok(message: str) -> None:
    out(f"[ok] {message}")


def warn(message: str) -> None:
    out(f"[warn] {message}")


def fail(message: str) -> None:
    out(f"[fail] {message}")


def setup_step(number: int, title: str) -> None:
    out("")
    out(f"[{number}/{SETUP_TOTAL_STEPS}] {title}")


def setup_note(message: str) -> None:
    out(f"    {message}")


def expand_path(raw: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(raw))))


def default_config_dir() -> Path:
    return Path.home() / ".config" / APP_NAME


def default_config_file() -> Path:
    return default_config_dir() / "bridge.env"


def default_token_file() -> Path:
    return default_config_dir() / "token.json"


def default_state_dir() -> Path:
    return Path.home() / ".grok-telegram-bridge" / "state"


def default_runner_file() -> Path:
    return Path.home() / ".local" / "bin" / f"{APP_NAME}-run"


def default_attach_link() -> Path:
    return Path.home() / ".local" / "bin" / ATTACH_COMMAND


def default_systemd_unit_file() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SERVICE_NAME


def default_launchd_plist_file() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def default_log_file() -> Path:
    return Path.home() / "Library" / "Logs" / f"{APP_NAME}.log"


def default_service_file(os_name: str) -> Path | None:
    """The service file setup writes here: a launchd plist on macOS, a systemd --user unit on Linux."""
    if os_name == "Darwin":
        return default_launchd_plist_file()
    if os_name == "Linux":
        return default_systemd_unit_file()
    return None


def write_text_atomic(path: Path, value: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:  # Path.write_text has no newline= before 3.10
        handle.write(value)
    if mode is not None:
        tmp.chmod(mode)
    tmp.replace(path)
    if mode is not None:
        path.chmod(mode)


def backup_path(path: Path, stamp: str | None = None) -> Path:
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    number = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{number}")
        number += 1
    return candidate


def run_command(cmd: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timed out")
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


# ── Telegram ────────────────────────────────────────────────────────────────

def telegram_call(token: str, method: str, timeout: int = 60, **params: Any) -> dict[str, Any] | None:
    if "timeout_param" in params:
        params["timeout"] = params.pop("timeout_param")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=urllib.parse.urlencode(params).encode(),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:  # a refused token still comes back as Telegram's JSON answer
        try:
            with exc:
                payload = json.load(exc)
        except Exception:  # noqa: BLE001 - no JSON body reads as "no answer"
            return None
    except Exception:  # noqa: BLE001 - every network failure reads as "no answer"
        return None
    return payload if isinstance(payload, dict) else None


def validate_bot_token(token: str, api_call: ApiCall = telegram_call) -> str:
    payload = api_call(token, "getMe", timeout=30)
    if not payload or not payload.get("ok"):
        raise SetupError("Telegram did not accept the bot token; copy it again from @BotFather")
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    username = str(result.get("username") or "").strip()
    if not username:
        raise SetupError("Telegram getMe returned no bot username")
    return username


def current_update_offset(token: str, api_call: ApiCall = telegram_call) -> int:
    payload = api_call(token, "getUpdates", timeout=10, timeout_param=0)
    updates = payload.get("result") if payload and payload.get("ok") else None
    if not isinstance(updates, list):
        return 0
    ids = [item.get("update_id") for item in updates if isinstance(item, dict)]
    valid = [int(item) for item in ids if isinstance(item, int)]
    return max(valid) + 1 if valid else 0


def extract_chat_id(update: dict[str, Any]) -> tuple[str, str] | None:
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict) or not isinstance(message.get("chat"), dict):
        return None
    chat = message["chat"]
    if chat.get("id") is None:
        return None
    label = " ".join(str(chat.get(key) or "").strip() for key in ("first_name", "last_name")).strip()
    if chat.get("username"):
        label = f"{label} (@{chat['username']})".strip()
    return str(chat["id"]), label or str(chat["id"])


def wait_for_chat_id(
    token: str,
    *,
    offset: int = 0,
    timeout_seconds: int = 180,
    api_call: ApiCall = telegram_call,
) -> tuple[str, str]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        payload = api_call(token, "getUpdates", timeout=20, offset=offset, timeout_param=10)
        updates = payload.get("result") if payload and payload.get("ok") else None
        if not isinstance(updates, list):
            time.sleep(2)
            continue
        for update in updates:
            if not isinstance(update, dict):
                continue
            if isinstance(update.get("update_id"), int):
                offset = int(update["update_id"]) + 1
            result = extract_chat_id(update)
            if result:
                return result
    raise SetupError("timed out waiting for /start in the bot chat; run setup again")


def send_message(token: str, chat_id: str, text: str, api_call: ApiCall = telegram_call) -> bool:
    payload = api_call(token, "sendMessage", timeout=30, chat_id=chat_id, text=text)
    return bool(payload and payload.get("ok"))


# ── bridge.env ──────────────────────────────────────────────────────────────

def _assignment_key(raw: str) -> str:
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        return ""
    if line.startswith("export "):
        line = line[len("export "):].lstrip()
    key = line.split("=", 1)[0].strip()
    return key if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) else ""


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        key = _assignment_key(raw)
        if not key:
            continue
        value = raw.split("=", 1)[1].strip()
        try:
            parts = shlex.split(value)
            values[key] = parts[0] if parts else ""
        except ValueError:
            values[key] = value.strip("'\"")
    return values


def merge_env_text(existing: str, updates: list[tuple[str, str]]) -> str:
    """Set each key once, in place; keep every other line of the old file."""
    values = dict(updates)
    seen: set[str] = set()
    lines: list[str] = []
    for raw in existing.splitlines():
        key = _assignment_key(raw)
        if key in values:
            if key not in seen:
                lines.append(f"{key}={values[key]}")
                seen.add(key)
            continue
        lines.append(raw)
    missing = [(key, value) for key, value in updates if key not in seen]
    if missing:
        if lines and lines[-1].strip():
            lines.append("")
        if CONFIG_HEADER not in lines:
            lines.append(CONFIG_HEADER)
        lines.extend(f"{key}={value}" for key, value in missing)
    return "\n".join(lines) + "\n"


def write_bridge_env(path: Path, updates: list[tuple[str, str]]) -> Path | None:
    """Write bridge.env (mode 600). An existing file is copied to bridge.env.bak-<time> first."""
    backup = None
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        backup = backup_path(path)
        shutil.copy2(path, backup)
    write_text_atomic(path, merge_env_text(existing, updates), mode=0o600)
    return backup


def preset_switches(like_author: bool) -> tuple[tuple[str, str], ...]:
    return AUTHOR_PRESET_ENV if like_author else PLAIN_ENV


def config_updates(options: SetupOptions, chat_id: str, grok_bin: str, tmux_bin: str) -> list[tuple[str, str]]:
    quote = shlex.quote
    updates = [
        ("GRB_TOKEN_FILE", quote(str(options.token_file))),
        ("GRB_CHAT_ID", quote(chat_id)),
        ("GRB_STATE_DIR", quote(str(options.state_dir))),
        ("GRB_LANGUAGE", quote(options.language)),
        ("GRB_GROK_BIN", quote(grok_bin)),
        ("GRB_TMUX_BIN", quote(tmux_bin or "tmux")),
        ("GRB_TMUX_SOCKET", TMUX_SOCKET),
        ("GRB_TMUX_SESSION", TMUX_SESSION),
    ]
    updates.extend((key, quote(value)) for key, value in preset_switches(options.like_author))
    return updates


def read_saved_token(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("api_key") or "").strip() if isinstance(data, dict) else ""


# ── tmux session and grok-tg ────────────────────────────────────────────────

def find_grok_binary(which: Which = shutil.which) -> str | None:
    explicit = os.environ.get("GRB_GROK_BIN", "").strip()
    if explicit:
        return which(explicit)
    found = which("grok")
    if found:
        return found
    for candidate in (Path.home() / ".local" / "bin" / "grok", Path.home() / ".grok" / "bin" / "grok"):
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def session_target(config: dict[str, str]) -> tuple[str, str, str]:
    return (
        config.get("GRB_TMUX_BIN") or "tmux",
        config.get("GRB_TMUX_SOCKET") or "default",
        config.get("GRB_TMUX_SESSION") or "grok",
    )


def bridge_pane(config: dict[str, str]) -> str:
    return config.get("GRB_TMUX_PANE") or f"{session_target(config)[2]}:0.0"


def session_env(config: dict[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    env.update(config)
    return env


def session_alive(config: dict[str, str], *, run: RunCommand = run_command) -> bool:
    tmux_bin, socket, session = session_target(config)
    return run([tmux_bin, "-L", socket, "has-session", "-t", f"={session}"]).returncode == 0


def start_grok_session(config: dict[str, str], *, run: RunCommand = run_command) -> tuple[bool, str]:
    """Start Grok through grok-tui-session-start.sh with bridge.env, so the bridge finds the session."""
    started = run(["bash", str(SESSION_START_SCRIPT)], env=session_env(config))
    if session_alive(config, run=run):
        return True, ""
    detail = (started.stderr or started.stdout or f"exit {started.returncode}").strip().splitlines()
    return False, detail[-1] if detail else f"exit {started.returncode}"


def install_attach_link(link: Path, target: Path = ATTACH_SCRIPT) -> str:
    """Point ~/.local/bin/grok-tg at this checkout. Never replaces a file setup did not make."""
    if target.exists() and not os.access(target, os.X_OK):
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        current = Path(os.readlink(link))
        if current == target:
            return "kept"
        if current.name != ATTACH_COMMAND:
            return "foreign"
        link.unlink()
    elif link.exists():
        return "foreign"
    link.symlink_to(target)
    return "linked"


def grok_processes_outside_bridge(pane_tty: str, ps_text: str) -> list[tuple[str, str]]:
    """(pid, tty) of Grok CLI processes on a terminal other than the bridge pane.

    ps_text is `ps -Ao pid=,tty=,comm=` output. Processes without a terminal
    (the headless lane, services) and the bridge pane's own Grok are ignored.
    """
    pane = (pane_tty or "").strip()
    if not pane:
        return []
    pane_name = pane[len("/dev/"):] if pane.startswith("/dev/") else pane
    found: list[tuple[str, str]] = []
    for raw in ps_text.splitlines():
        parts = raw.split()
        if len(parts) < 3:
            continue
        pid, tty, command = parts[0], parts[-2], parts[-1]
        if not GROK_PROCESS_NAME.fullmatch(os.path.basename(command)):
            continue
        if tty in NO_TTY or tty in {pane_name, pane}:
            continue
        found.append((pid, tty))
    return found


def grok_processes_in_pane(pane_tty: str, ps_text: str) -> list[tuple[str, str]]:
    """(pid, tty) of Grok CLI processes on the bridge pane's own terminal; ps_text as above."""
    pane = (pane_tty or "").strip()
    if not pane:
        return []
    pane_name = pane[len("/dev/"):] if pane.startswith("/dev/") else pane
    found: list[tuple[str, str]] = []
    for raw in ps_text.splitlines():
        parts = raw.split()
        if len(parts) < 3 or parts[-2] not in {pane_name, pane}:
            continue
        if GROK_PROCESS_NAME.fullmatch(os.path.basename(parts[-1])):
            found.append((parts[0], parts[-2]))
    return found


def warn_grok_outside_bridge(
    config: dict[str, str], *, run: RunCommand = run_command, report: Callable[[str], None] = warn
) -> int:
    """Warn (setup's last step, and doctor) about Grok running on a terminal the phone cannot see."""
    tmux_bin, socket, _session = session_target(config)
    pane = run([tmux_bin, "-L", socket, "display-message", "-p", "-t", bridge_pane(config), "#{pane_tty}"])
    processes = run(["ps", "-Ao", "pid=,tty=,comm="])
    outside = grok_processes_outside_bridge(pane.stdout or "", processes.stdout or "")
    if not outside:
        return 0
    listed = ", ".join(f"pid {pid} on {tty}" for pid, tty in outside)
    report(
        f"Grok is running outside the bridge tmux session ({listed}); your phone does not see it. "
        f"Quit it and use {ATTACH_COMMAND} so your phone and terminal share one session"
    )
    return len(outside)


# ── runner and service ──────────────────────────────────────────────────────

def install_runner(
    runner_file: Path,
    config_file: Path,
    *,
    python_bin: str | None = None,
    path_dirs: list[str] | None = None,
) -> None:
    python_bin = python_bin or sys.executable or shutil.which("python3") or "python3"
    dirs: list[str] = []
    for item in [os.path.dirname(python_bin), *(path_dirs or [])]:
        if item and item not in dirs:
            dirs.append(item)
    quote = shlex.quote
    path_prefix = ":".join(quote(item) for item in dirs)
    content = "\n".join(
        [
            "#!/usr/bin/env bash",
            "# Written by bridge_setup.py: load bridge.env, start the Grok tmux session",
            "# for the TUI lane when it is missing, then run the bridge.",
            "set -euo pipefail",
            f"ENV_FILE={quote(str(config_file))}",
            'test -f "$ENV_FILE" || { echo "config file missing: $ENV_FILE" >&2; exit 2; }',
            "set -a",
            '. "$ENV_FILE"',
            "set +a",
            f'export PATH={path_prefix}:"$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"',
            "export PYTHONUNBUFFERED=1",
            'if [ "${GRB_CHAT_LANE:-headless}" = tui ]; then',
            "  start_rc=0",
            f"  bash {quote(str(SESSION_START_SCRIPT))} || start_rc=$?",
            '  if [ "$start_rc" -ne 0 ] && [ "$start_rc" -ne 9 ]; then',
            '    echo "grok tmux session start exited $start_rc; the bridge starts anyway" >&2',
            "  fi",
            "fi",
            f"exec {quote(python_bin)} {quote(str(BRIDGE_SCRIPT))}",
            "",
        ]
    )
    write_text_atomic(runner_file, content, mode=0o755)


def _systemd_value(value: Path | str) -> str:
    text = str(value)
    return f'"{text}"' if any(char.isspace() for char in text) else text


def systemd_unit_content(runner_file: Path) -> str:
    return "\n".join(
        [
            f"# {SERVICE_FILE_MARKER}; setup replaces this file.",
            "[Unit]",
            "Description=Grok Telegram Bridge",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={_systemd_value(runner_file)}",
            "Restart=always",
            "RestartSec=5",
            # The runner may start the Grok tmux server; restarting the bridge must not kill it.
            "KillMode=process",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def launchd_plist_bytes(runner_file: Path, log_file: Path) -> bytes:
    body = plistlib.dumps(
        {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": [str(runner_file)],
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(log_file),
            "StandardErrorPath": str(log_file),
        }
    )
    marker = f"<!-- {SERVICE_FILE_MARKER}; setup replaces this file. -->\n".encode()
    return body.replace(b"<plist ", marker + b"<plist ", 1)


def service_file_is_ours(path: Path) -> bool:
    """True when setup wrote this service file: it carries SERVICE_FILE_MARKER."""
    try:
        return SERVICE_FILE_MARKER.encode() in path.read_bytes()
    except OSError:
        return False


def back_up_foreign_service_file(path: Path, *, ask: Callable[[str], str] | None, backup_dir: Path | None) -> bool:
    """Before setup writes `path`: copy a file that setup did not write, and say so.

    Interactive setup asks first. Returns False when you answer n; setup then leaves
    the file and the service alone. The copy goes next to bridge.env, not beside the
    service file, so launchd or systemd never loads it again.
    """
    if not path.exists() or service_file_is_ours(path):
        return True
    question = f"{path} exists and was not written by bridge_setup.py. Back it up and replace it? [Y/n] "
    if ask is not None and ask(question).strip().lower() in {"n", "no"}:
        warn(f"kept {path}; the background service was not installed or restarted")
        return False
    backup = backup_path((backup_dir or default_config_dir()) / path.name)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup)
    warn(f"{path} was not written by bridge_setup.py; saved a copy as {backup} before replacing it")
    return True


def install_service(
    runner_file: Path,
    *,
    start: bool,
    os_name: str | None = None,
    run: RunCommand = run_command,
    sleep: Callable[[float], None] = time.sleep,
    ask: Callable[[str], str] | None = None,
    backup_dir: Path | None = None,
) -> tuple[Path | None, bool]:
    """Install the background service. Returns (service file, started); (None, False) when you keep a foreign file."""
    os_name = os_name or platform.system()
    if os_name == "Linux":
        path = default_systemd_unit_file()
        if not back_up_foreign_service_file(path, ask=ask, backup_dir=backup_dir):
            return None, False
        write_text_atomic(path, systemd_unit_content(runner_file), mode=0o644)
        if run(["systemctl", "--user", "daemon-reload"]).returncode != 0:
            warn(f"systemd --user is not available here; start the bridge yourself with: {runner_file}")
            return path, False
        run(["systemctl", "--user", "enable", SERVICE_NAME])
        if not start:
            return path, False
        return path, run(["systemctl", "--user", "restart", SERVICE_NAME]).returncode == 0
    if os_name == "Darwin":
        path = default_launchd_plist_file()
        if not back_up_foreign_service_file(path, ask=ask, backup_dir=backup_dir):
            return None, False
        log_file = default_log_file()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(launchd_plist_bytes(runner_file, log_file))
        if not start:
            return path, False
        domain = f"gui/{os.getuid()}"
        run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"])
        for _attempt in range(5):  # bootout finishes asynchronously; bootstrap can race it
            if run(["launchctl", "bootstrap", domain, str(path)]).returncode == 0:
                return path, True
            sleep(1)
        warn(f"launchctl could not start {LAUNCHD_LABEL}; start it with: launchctl bootstrap {domain} {path}")
        return path, False
    warn(f"service install is not automated on {os_name}; start the bridge with: {runner_file}")
    return None, False


def log_hint(os_name: str) -> str:
    if os_name == "Darwin":
        return str(default_log_file())
    return f"journalctl --user -u {SERVICE_NAME}"


# ── self-check ──────────────────────────────────────────────────────────────

class BridgeLogTail:
    """New bridge log text since construction: the launchd log on macOS, the journal on Linux."""

    def __init__(self, os_name: str, since: float, *, run: RunCommand = run_command) -> None:
        self.os_name = os_name
        self.since = since
        self.run = run
        self.path = default_log_file() if os_name == "Darwin" else None
        self.offset = self.path.stat().st_size if self.path is not None and self.path.exists() else 0
        self.seen = 0

    def read_new(self) -> str:
        if self.path is not None:
            if not self.path.exists():
                return ""
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                data = handle.read()
            self.offset += len(data)
            return data.decode("utf-8", "replace")
        if self.os_name == "Linux":
            proc = self.run(
                ["journalctl", "--user", "-u", SERVICE_NAME, "--since", f"@{int(self.since)}", "-o", "cat", "--no-pager"]
            )
            text = proc.stdout or ""
            fresh, self.seen = text[self.seen:], len(text)
            return fresh
        return ""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def inbox_size(state_dir: Path, name: str) -> int:
    try:
        return (state_dir / f"grok-bridge-{name}.inbox.jsonl").stat().st_size
    except OSError:
        return 0


def phone_message_seen(state_dir: Path, name: str, since: float, offset: int = 0) -> bool:
    """A phone message reached the bridge after `since`: spooled to its inbox, or taken by its worker.

    Only inbox bytes after `offset` are read (the inbox only grows). Slash commands
    do not count: the /start sent for chat detection is still waiting when the
    service starts, and the bridge answers commands without a Grok turn.
    """
    try:
        with (state_dir / f"grok-bridge-{name}.inbox.jsonl").open("rb") as handle:
            handle.seek(offset)
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or str(record.get("text") or "").lstrip().startswith("/"):
            continue
        try:
            if float(record.get("ts") or 0) >= since:
                return True
        except (TypeError, ValueError):
            continue
    health = _read_json(state_dir / f"grok-bridge-{name}.health.json")
    try:
        return float(health.get("last_job_started_at") or 0) >= since
    except (TypeError, ValueError):
        return False


def author_self_check(
    *,
    token: str,
    chat_id: str,
    config: dict[str, str],
    os_name: str,
    api_call: ApiCall = telegram_call,
    run: RunCommand = run_command,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    log_tail: Callable[[float], Any] | None = None,
    phone_timeout: float = SELF_CHECK_PHONE_TIMEOUT,
    terminal_timeout: float = SELF_CHECK_TERMINAL_TIMEOUT,
    bot_message_sent: bool | None = True,
) -> list[str]:
    """Run each direction once after an author-preset install. Results go to the terminal and the chat."""
    if log_tail is None:
        def log_tail(since: float) -> BridgeLogTail:
            return BridgeLogTail(os_name, since, run=run)

    out("")
    out("Self-check: bot -> phone, phone -> terminal -> phone, terminal -> phone")
    if bot_message_sent is None:
        lines = ["1/3 bot -> phone: skipped (the test message was turned off)"]
    elif bot_message_sent:
        lines = ["1/3 bot -> phone: ok (test message delivered)"]
    else:
        lines = ["1/3 bot -> phone: the test message did not go through; check the token and chat id in bridge.env"]
    name = config.get("GRB_NAME") or "grok"
    state_dir = expand_path(config.get("GRB_STATE_DIR") or default_state_dir())
    hint = log_hint(os_name)

    started = clock()
    tail = log_tail(started)
    offset = inbox_size(state_dir, name)
    send_message(
        token,
        chat_id,
        f"Self-check 2/3: send any text to this bot within {int(phone_timeout)} s. "
        "It goes to Grok in the terminal, and the answer should come back here.",
        api_call,
    )
    setup_note(f"waiting up to {int(phone_timeout)}s for a message from your phone ...")
    arrived = answered = False
    while clock() - started < phone_timeout:
        arrived = arrived or phone_message_seen(state_dir, name, started, offset)
        if PHONE_ANSWER_MARKER in tail.read_new():
            answered = True
            break
        sleep(2)
    if answered:
        lines.append("2/3 phone -> terminal -> phone: ok")
    elif arrived:
        lines.append(
            f"2/3 phone -> terminal -> phone: your message reached the Grok session, but no answer came back "
            f"within {int(phone_timeout)} s; look at the session with {ATTACH_COMMAND}"
        )
    else:
        lines.append(
            f"2/3 phone -> terminal -> phone: no message from your phone within {int(phone_timeout)} s; "
            f"send any text to the bot later, and check the bridge log ({hint}) if nothing comes back"
        )

    tmux_bin, socket, _session = session_target(config)
    pane = bridge_pane(config)
    started = clock()
    tail = log_tail(started)
    run([tmux_bin, "-L", socket, "send-keys", "-t", pane, "-l", SELF_CHECK_PROBE])
    run([tmux_bin, "-L", socket, "send-keys", "-t", pane, "Enter"])
    setup_note(f"typed a probe into the Grok session; waiting up to {int(terminal_timeout)}s for its phone card ...")
    mirrored = False
    while clock() - started < terminal_timeout:
        if any(marker in tail.read_new() for marker in TERMINAL_MIRROR_MARKERS):
            mirrored = True
            break
        sleep(2)
    if mirrored:
        lines.append("3/3 terminal -> phone: ok")
    else:
        lines.append(
            f"3/3 terminal -> phone: no phone card for the terminal probe within {int(terminal_timeout)} s; "
            f"confirm GRB_TUI_MIRROR_LOCAL=1 in bridge.env and that the service restarted ({hint})"
        )
    for line in lines:
        out(line)
    send_message(token, chat_id, "Setup self-check\n" + "\n".join(lines), api_call)
    return lines


# ── setup ───────────────────────────────────────────────────────────────────

def setup_plan_lines(options: SetupOptions, *, os_name: str) -> list[str]:
    """What `setup --dry-run` would do. No I/O."""
    lines = [
        "Dry run: nothing is written, started, or sent.",
        f"config: {options.config_file} (an existing file is backed up; other settings are kept)",
        f"token file: {options.token_file}",
        f"state dir: {options.state_dir}",
        "switches (bridge.env):" if options.like_author else "switches (bridge.env, public defaults):",
    ]
    lines.extend(f"  {key}={value}" for key, value in preset_switches(options.like_author))
    lines.append(f"  GRB_TMUX_SOCKET={TMUX_SOCKET} GRB_TMUX_SESSION={TMUX_SESSION}")
    if options.like_author:
        lines.append(f"tmux session: {TMUX_SOCKET}/{TMUX_SESSION}, Grok started inside it by {SESSION_START_SCRIPT.name}")
        lines.append(f"attach any time with: {ATTACH_COMMAND} (link: {options.attach_link})")
    else:
        lines.append("tmux session: none (headless lane)")
    if options.install_service:
        manager = {"Darwin": f"launchd {LAUNCHD_LABEL}", "Linux": f"systemd --user {SERVICE_NAME}"}.get(
            os_name, "none (start the runner yourself)"
        )
        start = "install and start" if options.start_service else "install"
        lines.append(f"runner: {options.runner_file}; service: {start} ({manager})")
        service_file = default_service_file(os_name)
        if service_file is not None:
            lines.append(f"service file: {service_file}")
    else:
        lines.append(f"runner: {options.runner_file}; service: skip")
    lines.append(f"test message: {'send' if options.send_test else 'skip'}")
    if options.like_author:
        lines.append(
            "self-check after install: bot -> phone, phone -> terminal -> phone "
            f"({int(SELF_CHECK_PHONE_TIMEOUT)} s), terminal -> phone ({int(SELF_CHECK_TERMINAL_TIMEOUT)} s)"
        )
    replaced = files_setup_writes_over(options, os_name=os_name)
    if replaced:
        lines.append("files already there that setup writes over:")
        lines.extend(f"  {item}" for item in replaced)
    else:
        lines.append("files already there that setup writes over: none")
    return lines


def files_setup_writes_over(options: SetupOptions, *, os_name: str) -> list[str]:
    """Files already on disk that setup would write over, each with what happens to it. Reads only."""
    found: list[str] = []
    if options.config_file.exists():
        found.append(f"{options.config_file} (a copy is kept first; its other settings stay)")
    if options.token_file.exists():
        found.append(f"{options.token_file} (holds the token you keep or paste)")
    if options.runner_file.exists():
        found.append(str(options.runner_file))
    service_file = default_service_file(os_name) if options.install_service else None
    if service_file is not None and service_file.exists():
        if service_file_is_ours(service_file):
            found.append(str(service_file))
        else:
            found.append(
                f"{service_file} (not written by bridge_setup.py: a copy goes to {options.config_file.parent} first)"
            )
    return found


def obtain_token(options: SetupOptions, api_call: ApiCall) -> tuple[str, str]:
    token = os.environ.get("GRB_BOT_TOKEN", "").strip()
    source = "GRB_BOT_TOKEN"
    if not token:
        saved = read_saved_token(options.token_file)
        if saved and (options.non_interactive or input("Keep the saved bot token? [Y/n] ").strip().lower() not in {"n", "no"}):
            token, source = saved, str(options.token_file)
    if not token:
        if options.non_interactive:
            raise SetupError("no bot token: set GRB_BOT_TOKEN or keep a saved token file for --non-interactive")
        token = getpass.getpass("BotFather token (hidden): ").strip()
        source = "prompt"
    username = validate_bot_token(token, api_call)
    ok(f"token valid: @{username} (from {source})")
    return token, username


def obtain_chat_id(options: SetupOptions, token: str, username: str, existing: dict[str, str], api_call: ApiCall) -> str:
    if options.chat_id and options.chat_id.strip():
        chat_id = options.chat_id.strip()
        ok(f"chat id configured: {chat_id}")
        return chat_id
    saved = (existing.get("GRB_CHAT_ID") or "").strip()
    if saved and saved != EXAMPLE_CHAT_ID:
        ok(f"chat id kept from bridge.env: {saved} (pass --chat-id to change it)")
        return saved
    if options.non_interactive:
        raise SetupError("--chat-id is required with --non-interactive when bridge.env has no chat id")
    offset = current_update_offset(token, api_call)
    setup_note(f"Open @{username} in Telegram, send /start, and wait up to {options.wait_timeout}s.")
    chat_id, label = wait_for_chat_id(token, offset=offset, timeout_seconds=options.wait_timeout, api_call=api_call)
    ok(f"chat id detected: {chat_id} ({label})")
    return chat_id


def setup_bridge(
    options: SetupOptions,
    *,
    api_call: ApiCall = telegram_call,
    run: RunCommand = run_command,
    which: Which = shutil.which,
    os_name: str | None = None,
    self_check: Callable[..., list[str]] = author_self_check,
) -> int:
    os_name = os_name or platform.system()
    if options.dry_run:
        out("Grok Telegram Bridge setup (dry run)")
        for line in setup_plan_lines(options, os_name=os_name):
            out(line)
        return 0
    out("Grok Telegram Bridge setup")
    setup_note("You need a BotFather token, Telegram on your phone, and the grok CLI, installed and logged in.")
    if not options.like_author and not options.non_interactive:
        setup_note(
            "The author's setup runs Grok in a tmux session that you and your phone share "
            f"({ATTACH_COMMAND} attaches any terminal), sends turns typed there to your phone with "
            "a Stop button, installs the background service, and ends with a self-check. "
            "Answer n to keep the public defaults (headless lane, nothing mirrored)."
        )
        if input(AUTHOR_PRESET_QUESTION).strip().lower() not in {"n", "no"}:
            options = dataclasses.replace(options, like_author=True)
    grok_bin = find_grok_binary(which)
    if not grok_bin:
        raise SetupError("grok CLI not found; install it and log in, or set GRB_GROK_BIN to its full path")
    tmux_bin = which("tmux") or ""
    if options.like_author and not tmux_bin:
        raise SetupError(
            "the author's setup needs tmux (macOS: brew install tmux, Debian or Ubuntu: sudo apt install tmux); "
            "install it and run setup again, or answer n for the headless lane"
        )
    existing = load_env_file(options.config_file)

    setup_step(1, "Bot token")
    token, username = obtain_token(options, api_call)

    setup_step(2, "Telegram chat")
    chat_id = obtain_chat_id(options, token, username, existing, api_call)

    setup_step(3, "Write token.json and bridge.env")
    write_text_atomic(options.token_file, json.dumps({"api_key": token}) + "\n", mode=0o600)
    ok(f"wrote token file (only your user can read it): {options.token_file}")
    backup = write_bridge_env(options.config_file, config_updates(options, chat_id, grok_bin, tmux_bin))
    if backup:
        ok(f"backed up the previous bridge.env: {backup}")
    ok(f"wrote {options.config_file}")
    ok("switches: " + ", ".join(f"{key}={value}" for key, value in preset_switches(options.like_author)))
    config = load_env_file(options.config_file)
    tui_lane = config.get("GRB_CHAT_LANE") == "tui"

    setup_step(4, "Start Grok in the bridge tmux session")
    session_ready = False
    if tui_lane:
        if session_alive(config, run=run):
            session_ready = True
            ok(f"tmux session already running: {TMUX_SOCKET}/{TMUX_SESSION}")
        else:
            session_ready, detail = start_grok_session(config, run=run)
            if session_ready:
                ok(f"started Grok in tmux session {TMUX_SOCKET}/{TMUX_SESSION}")
            else:
                warn(f"could not start the Grok tmux session ({detail}); run {ATTACH_COMMAND} to try again")
        state = install_attach_link(options.attach_link)
        if state == "foreign":
            warn(f"{options.attach_link} exists and is not this bridge's {ATTACH_COMMAND}; left it alone. Run {ATTACH_SCRIPT} instead")
        else:
            ok(f"{ATTACH_COMMAND} -> {ATTACH_SCRIPT} ({state})")
            if str(options.attach_link.parent) not in os.environ.get("PATH", "").split(os.pathsep):
                setup_note(f"{options.attach_link.parent} is not on your PATH; add it, or run {options.attach_link}")
    else:
        setup_note("Headless lane: Grok runs once per message, so no tmux session is needed.")

    setup_step(5, "Install the runner and the background service")
    path_dirs = [os.path.dirname(item) for item in (grok_bin, tmux_bin) if item]
    install_runner(options.runner_file, options.config_file, path_dirs=path_dirs)
    ok(f"installed runner: {options.runner_file}")
    service_started = False
    if options.install_service:
        service_file, service_started = install_service(
            options.runner_file, start=options.start_service, os_name=os_name, run=run,
            ask=None if options.non_interactive else input, backup_dir=options.config_file.parent,
        )
        if service_file:
            ok(f"installed service: {service_file}" + (" (started)" if service_started else ""))
    else:
        warn(f"service install skipped; start the bridge yourself with: {options.runner_file}")

    setup_step(6, "Send a test message")
    sent: bool | None = None
    if options.send_test:
        sent = send_message(token, chat_id, "Grok Telegram Bridge is set up. Send a message here and Grok answers it.", api_call)
        if sent:
            ok("sent the test message")
        else:
            warn("the test message did not go through; check the token and chat id in bridge.env")
    else:
        setup_note("Test message skipped.")

    if session_ready:
        warn_grok_outside_bridge(config, run=run)

    out("")
    out("Setup complete.")
    if tui_lane:
        out(f"Attach any terminal to the Grok session your phone uses with: {ATTACH_COMMAND}")
        out("Detach with Ctrl+B, then D. Grok keeps running.")
    out("Send a message to your bot to talk to Grok.")
    if options.like_author:
        if session_ready and service_started:
            self_check(
                token=token,
                chat_id=chat_id,
                config=config,
                os_name=os_name,
                api_call=api_call,
                run=run,
                bot_message_sent=sent,
            )
        else:
            setup_note("Self-check skipped: it needs the running service and the Grok tmux session.")
    out(f"Check the bridge again any time: {DOCTOR_COMMAND}")
    return 0


# ── attach (grok-tg) ────────────────────────────────────────────────────────

def attach_command(
    config_file: Path,
    *,
    run: RunCommand = run_command,
    exec_fn: Callable[[str, list[str]], Any] = os.execvp,
    create_missing: bool = True,
) -> int:
    """Attach this terminal to the Grok session the bridge controls (`grok-tg`)."""
    if not config_file.is_file():
        fail(f"{config_file} is missing; run `python3 bridge_setup.py setup` first")
        return 2
    config = load_env_file(config_file)
    tmux_bin, socket, session = session_target(config)
    if config.get("GRB_CHAT_LANE", "headless") != "tui":
        warn("bridge.env uses the headless lane; the bridge does not type into this session")
    if not session_alive(config, run=run):
        if not create_missing:
            fail(f"tmux session missing: {socket}/{session}")
            return 2
        started, detail = start_grok_session(config, run=run)
        if not started:
            fail(f"could not start the Grok tmux session: {detail}")
            return 2
        ok(f"started Grok in tmux session {socket}/{session}")
    clients = run([tmux_bin, "-L", socket, "list-clients", "-t", f"={session}"])
    if (clients.stdout or "").strip():
        setup_note(
            "another terminal is already attached; this one joins it. The pane keeps the "
            "smaller size until the other terminal detaches (Ctrl+B, then D)."
        )
    attach_argv = [tmux_bin, "-L", socket, "attach", "-t", session]
    exec_fn(tmux_bin, attach_argv)
    return 0


# ── doctor ──────────────────────────────────────────────────────────────────

class DoctorReport:
    """One [ok], [warn] or [fail] line per check. Any failure makes doctor exit with 1."""

    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    def ok(self, message: str) -> None:
        out(f"[ok] {message}")

    def warn(self, message: str) -> None:
        self.warnings += 1
        out(f"[warn] {message}")

    def fail(self, message: str) -> None:
        self.failures += 1
        out(f"[fail] {message}")


def current_user() -> str:
    try:
        return os.environ.get("USER") or getpass.getuser()
    except (OSError, KeyError):  # no USER and no password entry
        return ""


def doctor_private_file(report: DoctorReport, label: str, path: Path) -> None:
    """[ok] when only your user can read the file; [warn] with the chmod that fixes it otherwise."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        report.warn(f"{label}: could not read the permissions of {path} ({exc.strerror or exc})")
        return
    if mode & 0o077:
        report.warn(f"{label} is open to other users (mode {mode:o}); fix it with: chmod 600 {shlex.quote(str(path))}")
    else:
        report.ok(f"{label}: {path} (only you can read it)")


def doctor_telegram(report: DoctorReport, token: str, *, offline: bool, api_call: ApiCall) -> None:
    """One getMe call. A refused token fails; no answer at all (offline, DNS, timeout) only warns."""
    if offline:
        setup_note("Telegram not contacted (--offline).")
        return
    payload = api_call(token, "getMe", timeout=15)
    if not payload:
        report.warn("Telegram did not answer; check the network, or skip this check with --offline")
        return
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    if payload.get("ok"):
        report.ok(f"Telegram accepts the token: @{result.get('username') or '?'}")
        return
    reason = str(payload.get("description") or "refused").replace(token, "<token>")[:80]
    report.fail(f"Telegram refused the token ({reason}); copy it again from @BotFather and run setup")


def doctor_tmux(report: DoctorReport, config: dict[str, str], *, run: RunCommand, which: Which) -> None:
    """TUI lane: tmux, the bridge session, Grok in its pane, and Grok on any other terminal."""
    tmux_bin, socket, session = session_target(config)
    tmux_path = which(tmux_bin)
    if not tmux_path:
        report.fail(
            f"tmux not found ({tmux_bin}); the TUI lane needs it "
            "(macOS: brew install tmux, Debian or Ubuntu: sudo apt install tmux)"
        )
        return
    report.ok(f"tmux: {tmux_path}")
    if not session_alive(config, run=run):
        message = f"tmux session {socket}/{session} is not running; start it with {ATTACH_COMMAND}"
        if (config.get("GRB_TUI_FALLBACK_HEADLESS") or "0").strip().lower() in TRUE_WORDS:
            report.warn(f"{message}. Until then each message runs headless")
        else:
            report.fail(f"{message}. Until then messages from your phone get an error")
        return
    report.ok(f"tmux session {socket}/{session} is running")
    pane = bridge_pane(config)
    shown = run([tmux_bin, "-L", socket, "display-message", "-p", "-t", pane, "#{pane_tty}"])
    pane_tty = (shown.stdout or "").strip() if shown.returncode == 0 else ""
    if not pane_tty:
        report.warn(f"could not read the bridge pane {pane}; look at it with {ATTACH_COMMAND}")
        return
    inside = grok_processes_in_pane(pane_tty, run(["ps", "-Ao", "pid=,tty=,comm="]).stdout or "")
    if inside:
        report.ok(f"Grok is running in the bridge pane {pane} (pid {inside[0][0]})")
    else:
        report.warn(f"no Grok process in the bridge pane {pane}; look at it with {ATTACH_COMMAND}")
    outside = warn_grok_outside_bridge(config, run=run, report=report.warn)
    if not outside:
        report.ok("no Grok runs on another terminal")


def doctor_linger(report: DoctorReport, *, run: RunCommand, user: str) -> None:
    """Linux: without linger, systemd stops your user services when you log out. Doctor never turns it on."""
    shown = run(["loginctl", "show-user", user, "-p", "Linger"])
    linger = ""
    for line in (shown.stdout or "").splitlines():
        if line.strip().startswith("Linger="):
            linger = line.strip()[len("Linger="):].strip().lower()
    if linger == "yes":
        report.ok("linger: on, so the service keeps running after you log out")
    elif linger == "no":
        report.warn(
            "linger: off, so the service stops when you log out. "
            f"To keep it running, run: sudo loginctl enable-linger {shlex.quote(user)}"
        )
    else:
        detail = (shown.stderr or shown.stdout or f"exit {shown.returncode}").strip().splitlines()
        report.warn(
            f"linger: unknown ({detail[-1] if detail else 'no answer'}); "
            "without linger the service stops when you log out"
        )


def doctor_service(report: DoctorReport, *, os_name: str, run: RunCommand, user: str) -> None:
    """The background service: launchd on macOS; systemd --user and linger on Linux."""
    runner = shlex.quote(str(default_runner_file()))
    if os_name == "Darwin":
        domain = f"gui/{getattr(os, 'getuid', lambda: 0)()}"
        plist = default_launchd_plist_file()
        if run(["launchctl", "print", f"{domain}/{LAUNCHD_LABEL}"]).returncode == 0:
            report.ok(f"service: launchd {LAUNCHD_LABEL} is loaded")
        elif plist.is_file():
            report.warn(f"service: launchd {LAUNCHD_LABEL} is not loaded; start it with: launchctl bootstrap {domain} {shlex.quote(str(plist))}")
        else:
            report.warn(f"service: not installed ({plist} is missing); run setup, or start the bridge with: {runner}")
        return
    if os_name != "Linux":
        report.warn(f"service: not automated on {os_name}; start the bridge with: {runner}")
        return
    state = (run(["systemctl", "--user", "is-active", SERVICE_NAME]).stdout or "").strip()
    if not state:
        report.warn(f"service: systemd --user is not available here; start the bridge with: {runner}")
        return
    unit = default_systemd_unit_file()
    if state == "active":
        report.ok(f"service: systemd --user {SERVICE_NAME} is active")
    elif unit.is_file():
        report.warn(
            f"service: {SERVICE_NAME} is {state}; start it with: systemctl --user restart {SERVICE_NAME} "
            f"(log: journalctl --user -u {SERVICE_NAME})"
        )
    else:
        report.warn(f"service: not installed ({unit} is missing); run setup, or start the bridge with: {runner}")
    doctor_linger(report, run=run, user=user)


def doctor_summary(report: DoctorReport) -> int:
    out(f"doctor complete: {report.failures} failure(s), {report.warnings} warning(s)")
    return 1 if report.failures else 0


def doctor(
    config_file: Path,
    *,
    offline: bool = False,
    api_call: ApiCall = telegram_call,
    run: RunCommand = run_command,
    which: Which = shutil.which,
    os_name: str | None = None,
    user: str | None = None,
) -> int:
    """Check an install and change nothing. Exit code 1 when any check fails. Never prints the token."""
    os_name = os_name or platform.system()
    report = DoctorReport()
    out("Grok Telegram Bridge doctor")
    if not config_file.is_file():
        report.fail(f"bridge.env missing: {config_file}; run `python3 bridge_setup.py setup` first")
        return doctor_summary(report)
    doctor_private_file(report, "bridge.env", config_file)
    config = load_env_file(config_file)

    chat_id = (config.get("GRB_CHAT_ID") or "").strip()
    if not chat_id:
        report.fail("bridge.env has no GRB_CHAT_ID; run setup again")
    elif chat_id == EXAMPLE_CHAT_ID or not re.fullmatch(r"-?[0-9]+", chat_id):
        report.fail(f"GRB_CHAT_ID={chat_id} is not your chat id; run setup again to detect it")
    else:
        report.ok(f"chat id: {chat_id}")

    token = ""
    token_setting = (config.get("GRB_TOKEN_FILE") or "").strip()
    if not token_setting:
        report.fail("bridge.env has no GRB_TOKEN_FILE; run setup again")
    else:
        token_file = expand_path(token_setting)
        if not token_file.is_file():
            report.fail(f"token file missing: {token_file}; run setup again")
        else:
            token = read_saved_token(token_file)
            if token:
                doctor_private_file(report, "token file", token_file)
            else:
                report.fail(f"token file has no api_key: {token_file}; run setup again")
    if token:
        doctor_telegram(report, token, offline=offline, api_call=api_call)

    grok_setting = (config.get("GRB_GROK_BIN") or "").strip()
    grok_bin = which(str(expand_path(grok_setting))) if grok_setting else find_grok_binary(which)
    if grok_bin:
        report.ok(f"grok CLI: {grok_bin}")
    else:
        report.fail(f"grok CLI not found ({grok_setting or 'grok'}); install it, or set GRB_GROK_BIN to its full path")

    if (config.get("GRB_CHAT_LANE") or "headless").strip().lower() == "tui":
        doctor_tmux(report, config, run=run, which=which)
    else:
        report.ok("headless lane: Grok runs once per message, so no tmux session is needed")
    doctor_service(report, os_name=os_name, run=run, user=current_user() if user is None else user)
    return doctor_summary(report)


# ── command line ────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bridge_setup.py", description="Set up Grok Telegram Bridge")
    subparsers = parser.add_subparsers(dest="command")
    setup_parser = subparsers.add_parser("setup", help="token, chat, config, tmux session, service, test message")
    setup_parser.add_argument("--like-author", action="store_true",
                              help="the author's setup without asking: TUI lane, terminal turns mirrored to the "
                                   f"phone, the {ATTACH_COMMAND} command, the service, and a self-check")
    setup_parser.add_argument("--dry-run", action="store_true", help="print the plan; write, start, and send nothing")
    setup_parser.add_argument("--non-interactive", action="store_true",
                              help="ask nothing: the token comes from GRB_BOT_TOKEN or the saved token file")
    setup_parser.add_argument("--chat-id", help="your Telegram chat id (otherwise kept from bridge.env or detected)")
    setup_parser.add_argument("--language", choices=("en", "ko"), default="en",
                              help="language for bridge messages and buttons")
    setup_parser.add_argument("--config", type=expand_path, default=default_config_file())
    setup_parser.add_argument("--token-file", type=expand_path, default=default_token_file())
    setup_parser.add_argument("--state-dir", type=expand_path, default=default_state_dir())
    setup_parser.add_argument("--runner", type=expand_path, default=default_runner_file())
    setup_parser.add_argument("--attach-link", type=expand_path, default=default_attach_link())
    setup_parser.add_argument("--wait-timeout", type=int, default=180, help="seconds to wait for /start")
    setup_parser.add_argument("--no-service", action="store_true", help="do not install the background service")
    setup_parser.add_argument("--no-start", action="store_true", help="install the service but do not start it")
    setup_parser.add_argument("--no-test-message", action="store_true")
    attach_parser = subparsers.add_parser(
        "attach", help=f"attach this terminal to the bridge's Grok session (what {ATTACH_COMMAND} runs)"
    )
    attach_parser.add_argument("--config", type=expand_path, default=default_config_file())
    attach_parser.add_argument("--no-create", action="store_true", help="fail instead of starting a missing session")
    doctor_parser = subparsers.add_parser(
        "doctor", help="check bridge.env, the token, tmux, the service and linger, and Grok on other terminals"
    )
    doctor_parser.add_argument("--config", type=expand_path, default=default_config_file())
    doctor_parser.add_argument("--offline", action="store_true", help="do not contact Telegram")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "setup":
            options = SetupOptions(
                config_file=args.config,
                token_file=args.token_file,
                state_dir=args.state_dir,
                runner_file=args.runner,
                attach_link=args.attach_link,
                chat_id=args.chat_id,
                language=args.language,
                wait_timeout=args.wait_timeout,
                install_service=not args.no_service,
                start_service=not args.no_start,
                send_test=not args.no_test_message,
                non_interactive=args.non_interactive,
                like_author=args.like_author,
                dry_run=args.dry_run,
            )
            return setup_bridge(options)
        if args.command == "attach":
            return attach_command(args.config, create_missing=not args.no_create)
        if args.command == "doctor":
            return doctor(args.config, offline=args.offline)
    except SetupError as exc:
        fail(str(exc))
        return 2
    except KeyboardInterrupt:
        out("")
        fail("cancelled")
        return 130
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
