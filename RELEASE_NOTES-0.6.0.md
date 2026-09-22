# Grok Telegram Bridge 0.6.0

## New

- Send `/model` to see the available models as Telegram buttons, with the current session model marked. The list comes from your installed Grok CLI and account.
- Tap a model or send `/model <model-id>` to change the connected TUI session. Model controls no longer wait for an assistant response that the CLI does not generate.
- Model controls work independently of suggested-reply buttons. Active work, queued messages, drafts, and the pause after clearing a session are protected.
- The bridge reports a successful change only after reading the model from the session metadata. Unconfirmed changes are not automatically retried.
- Normal Telegram-to-TUI inputs remind Grok not to append unsolicited suggested-reply tags. This covers idle and busy input paths; slash commands are unchanged.
- Failed-turn progress now includes the failure reason instead of referring to a missing message below.

## Notes

Model switching requires a connected, idle Grok TUI with an empty standard boxed composer. Headless model switching is not supported. Grok CLI 1.0.40 changes its default model together with the current session; the menu explains this.

The reply-style reminder affects subsequent inputs sent through the bridge. It does not rewrite old terminal output or intercept text typed directly in the terminal. Suggested-reply bubbles and their confirmation buttons remain off by default.

## Upgrade

Back up your configuration, update the repository or unpack the release archive, and restart the bridge using your existing service setup. Preserve your existing Grok TUI session. No new environment variables are required.

The exported package passes 12 offline tests, including model-menu delivery through the public Telegram sender. No real Telegram messages or model inference are sent by these tests.
