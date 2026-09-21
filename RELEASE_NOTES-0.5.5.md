# Grok Telegram Bridge 0.5.5

Suggested-reply bubbles and confirm buttons are off by default.

## What is new since 0.5.4

- **No suggestion bubble unless you opt in.** A trailing suggestion marker is
  stripped from the answer instead of becoming a second message.
- **Confirm buttons stay off unless you set `GRB_SUGGESTED_CONFIRM=1`.** Old
  buttons still on screen are rejected with a short notice.
- Copy-paste command bubbles, progress editing, and ordinary approval buttons
  are unchanged.

To restore the previous customer default:

```
GRB_SUGGESTED_REPLY_SPLIT=1
GRB_SUGGESTED_CONFIRM=1
```
