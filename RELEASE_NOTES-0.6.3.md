# 0.6.3 — Keep background results separate from your current question

When an earlier background command finishes, its result is now marked as a
background task result. The bridge no longer replays the last unrelated human
question before that result.

The same boundary applies when recovering replies after a bridge restart or
session rotation. A background result cannot complete a different foreground
request. Normal terminal questions and their answers retain their existing
delivery and duplicate protection.

Only the bridge needs restarting after the update. Keep your existing Grok
session, state directory, queue, and Telegram offset.
