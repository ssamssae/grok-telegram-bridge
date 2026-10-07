# Grok Telegram Bridge 0.6.2

## Fixed

- Keep Telegram requests attached to the Grok process connected to the input
  pane, even when another session in the same directory has newer activity.
- Follow a session change only when process ownership identifies one session.
  Preserve the current session when ownership is missing or ambiguous.
- Wait for the answer after the current question. Another session's final
  answer no longer completes the request or stops its typing indicator.
- Preserve the pending question across bridge restarts and require the full
  normalized question to match before recovering an answer after rotation.
- Deliver the normal answer once without echoing its question as local input.

## Upgrade

Update to tag `v0.6.2` or the corresponding source archive, then restart the
bridge process. No configuration migration is required. Keep your token and
state files. Existing Grok terminal sessions can stay open.

## Verification

The public test suite and the upstream session-binding regressions were run
against this public module. The regressions use temporary history files and
mock Telegram/tmux calls. See the [verification path](docs/feature-map.md#session-isolation-and-reply-recovery).
