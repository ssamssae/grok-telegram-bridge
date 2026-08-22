# Grok Telegram Bridge

Text your own private Telegram bot, and the message runs as a Grok turn on your
computer. Grok's finished answer comes back to your phone.

This bridge is Grok-specific. It is a sibling of the Claude Telegram Bridge and
the Codex Telegram Bridge, but it does not share runtime code with either.

## Read This First

**This bridge gives a chat app the ability to run commands on your computer.**

That sentence is the whole security model, so please read it slowly. When you
send a message to your bot, the bridge hands that message to Grok, and Grok is
allowed to use its real tools: run shell commands, read files, edit files, run
`git`. It is not a chatbot that only talks back. A line you type on a bus can
delete a file at home.

The only thing standing between the internet and your machine is **one check**:
the bridge compares the Telegram chat id of every incoming message against the
single chat id you configured, and throws away everything that does not match.
That is one line of code, and it is the entire defense. There is no allowlist of
permitted commands, no approval prompt, no sandbox, and no second factor.

So, before you install:

- **Never share your bot's token.** Anyone holding the token can read your bot's
  messages and, if they also learn your chat id, reach your computer.
- **Never add the bot to a group** and never hand it to a friend "to try". It is
  built for exactly one chat: yours.
- **Do not run it on a machine you cannot afford to have damaged.** A first-time
  setup on a spare laptop is a much better idea than your work machine.
- **Assume every message is a command.** "What's in my documents folder?" will
  make Grok actually go and look.

If that trade is not one you want, stop here. This tool is convenient precisely
because it is unguarded, and pretending otherwise would be dishonest.

## What You Can Do With It

Once it is running, you text your bot from anywhere and Grok does real work on
the machine at home:

| You send | What happens |
| --- | --- |
| `what changed in my project today?` | Grok runs `git log` on your machine and summarizes it |
| `fix the typo in README.md line 4` | Grok edits the actual file |
| `is the server still up?` | Grok runs the check and reports back |
| a plain question | Grok just answers, no tools involved |

Only the finished answer is sent back — you do not get a wall of tool output on
your phone.

The conversation keeps its thread. The bridge remembers which Grok session
belongs to your chat, so a follow-up like "now do the same for the other file"
still makes sense.

## What You Need

- A computer where the `grok` CLI is installed and logged in. Linux, macOS, or
  WSL on Windows.
- Python 3.
- `tmux`, if you want the TUI lane (explained below). The headless lane does not
  need it.
- The Telegram app on your phone.

## Quick Start

Five steps. Nothing here assumes you have made a bot before.

### Step 1 — Create your own bot

Open Telegram and start a chat with [`@BotFather`](https://t.me/BotFather) — it
is Telegram's official bot for making bots.

Send it `/newbot`. It asks for a display name (anything, e.g. "My Grok"), then a
username that must end in `bot` (e.g. `my_grok_1234_bot`). When you are done it
replies with your token. It is one long line: a number, a colon, then a
mixed-case string of letters, digits, hyphens and underscores.

Copy it. **That token is a password.** Do not paste it into a chat, a
screenshot, a public repository, or an issue report.

### Step 2 — Find your chat id

Your chat id is the number that tells the bridge "this chat is mine, ignore
everyone else".

Send any message to your new bot (just say `hi`). Then run this on your
computer, pasting your token in place of `<YOUR_TOKEN>`:

```bash
curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates"
```

That command asks Telegram for the messages your bot has received. In the JSON
that comes back, find `"chat":{"id":123456789`. That number is your chat id.

If the reply looks empty (`"result":[]`), send your bot another message and run
the command again.

### Step 3 — Install

```bash
git clone https://github.com/ssamssae/grok-telegram-bridge.git
cd grok-telegram-bridge
```

That downloads the bridge into a folder and moves you into it. There is nothing
to compile.

### Step 4 — Save the token

The bridge reads the token from a small JSON file rather than from the command
line. Paste it at the hidden prompt below rather than typing it into a command,
so it never shows up in your shell history:

```bash
mkdir -p ~/.config/grok-telegram-bridge
read -r -s -p "Paste your bot token, then press Enter: " GRB_BOT_TOKEN; echo
GRB_BOT_TOKEN="$GRB_BOT_TOKEN" python3 - <<'EOF'
import json, os, pathlib
path = pathlib.Path.home() / ".config" / "grok-telegram-bridge" / "token.json"
path.write_text(json.dumps({"api_key": os.environ["GRB_BOT_TOKEN"].strip()}))
path.chmod(0o600)
print(f"wrote {path}")
EOF
unset GRB_BOT_TOKEN
```

Line by line: make the folder, read the token without echoing it to the screen,
write it into the file, `chmod 600` so only your user account can read it, then
drop it from the shell environment.

### Step 5 — Start it

```bash
export GRB_TOKEN_FILE=~/.config/grok-telegram-bridge/token.json
export GRB_CHAT_ID=123456789          # the number you found in Step 2
python3 grok_telegram_bridge.py
```

The first two lines tell the bridge where the token is and which chat is yours.
The third starts it. Leave the terminal window open — closing it stops the
bridge.

### Step 6 — Say hello

Text your bot: `hello, what machine are you on?`

You should get an answer back within a few seconds. If you do, you are done.

## Two Lanes: Headless And TUI

The bridge can drive Grok in two ways. The difference matters mostly when
something goes wrong.

**Headless (default).** Each message you send runs one `grok -p "<your message>"`
process, which exits once it has answered. Simple, and nothing is left running
between messages.

**TUI.** A single Grok terminal session stays open inside `tmux`, and your
messages are typed into it. You can attach to that session on the machine
(`tmux attach`) and watch what is happening in real time, which is the reason to
prefer it — a headless run is invisible while it works.

Tool access is identical in both lanes. Neither one restricts what Grok may do.

## Settings

Every setting is an environment variable. Only the first two are required.

| Variable | Default | What it does |
| --- | --- | --- |
| `GRB_TOKEN_FILE` | — | Path to the JSON file holding your bot token. Required. |
| `GRB_CHAT_ID` | — | The one Telegram chat id allowed to reach this machine. Required. |
| `GRB_GROK_BIN` | `grok` | Path to the `grok` binary, if it is not on your `PATH`. |
| `GRB_GROK_TIMEOUT` | `180` | Seconds to wait for one turn before giving up. |
| `GRB_STATE_DIR` | `~/.grok-telegram-bridge/state` | Where the chat-to-session mapping is kept. |
| `GRB_GROK_DISALLOWED_TOOLS` | empty | Comma-separated tool ids to take away from Grok. Empty means **no restriction**. See below. |
| `GRB_GROK_CHAT_RULES` | a work-lane rule | Extra instructions prepended to every turn. |
| `GRB_DRY_RUN` | `0` | Set `1` to run without calling Grok at all — useful for checking your setup. |

### Putting the guard rails back

`GRB_GROK_DISALLOWED_TOOLS` is how you re-restrict the bridge without editing
code. Give it a comma-separated list of tool ids and Grok never sees those tools
at all — they are removed from its schema, so it cannot call them even if it
wants to.

A conservative list that turns the bridge back into a chat-only assistant:

```bash
export GRB_GROK_DISALLOWED_TOOLS="run_terminal_cmd,run_terminal_command,grep,read_file,search_replace,list_dir,web_search,web_fetch,todo_write,task,search_tool,get_command_or_subagent_output,Agent"
```

The list looks redundant on purpose. Some tools are documented under one id but
show up in session logs under another (`run_terminal_cmd` versus
`run_terminal_command`), and a few that run are not in the documentation at all.
Listing both spellings costs nothing; missing one costs everything.

**In the TUI lane this variable does not work.** Grok accepts
`--disallowed-tools` there but only prints a warning and ignores it, which is
worse than no protection because it looks like it worked. Use permission rules
(`--deny`) for the TUI lane instead.

## When It Does Not Work

**Nothing comes back at all.**
Check the terminal where you started the bridge. If it says the token file is
missing, the path in `GRB_TOKEN_FILE` is wrong. If it says the api key is empty,
the JSON file is malformed — it must be exactly `{"api_key": "..."}`.

**The bridge is running but ignores you.**
Almost always the chat id. Redo Step 2 and compare the number with what you set
in `GRB_CHAT_ID`. A message from any other chat is silently discarded by design,
so there is no error to see.

**It answers, but very slowly, or times out at 180 seconds.**
Grok is probably investigating with tools rather than just answering. Either
raise `GRB_GROK_TIMEOUT`, or narrow the job — "read file X and tell me Y" is
faster than "figure out what is wrong".

**`grok: command not found`.**
The bridge could not find the CLI. Point `GRB_GROK_BIN` at the full path, e.g.
`export GRB_GROK_BIN=$HOME/.local/bin/grok`.

**A stale session sticks around after you change settings.**
The TUI lane keeps a long-lived Grok process, and it keeps the flags it was
started with. Restarting the bridge does not restart that session. Kill the tmux
session and let it be recreated.

## What This Is Not

- It is not multi-user. One chat id, one machine.
- It is not a sandbox. Grok runs with your user account's full permissions.
- It does not ask before acting. There is no approval prompt.
- It does not share code with the Claude or Codex bridges, so their settings,
  slash commands, and safety behaviors do not carry over.

## License

MIT, matching the sibling bridges.
