# 0.6.6 — Fresh model cards after clear

After `/clear`, a fresh human `/model` request can complete model, context window,
and effort selection. This changes settings only. The wait for human conversation
remains active, and machine input still cannot resume inference.

The first model card now has the same single-use, session-bound token as the
context and effort cards. Clear, restart, a new menu, or expiry invalidates older
cards. Legacy model buttons ask you to open `/model` again.

Restart only the bridge. Keep the native Grok session and existing state files.
