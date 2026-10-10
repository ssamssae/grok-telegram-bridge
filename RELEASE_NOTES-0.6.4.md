# Grok Telegram Bridge 0.6.4

- Synchronize current session ownership and full-question reply matching for the connected terminal.
- Add persistent English/Korean bridge controls with `/language en` and `/language ko`; prompts and model answers remain unchanged.
- Retain labeled background results and remove retired suggested-reply prompt instructions.
- Discard policy-suppressed progress records instead of retrying old records forever.

Validation: public export, private-data/token scan, manifest parity, and 30 tests (5 other-engine skips). A source bundle is included; install all shared modules alongside the bridge.

This is a GitHub release. It does not upload to PyPI or restart an installed bridge.
