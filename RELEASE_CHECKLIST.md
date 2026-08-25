# Release Checklist

Maintainer-only: this checklist and the export script below live in the
maintainer's private automation repo, not in this public repository. If you
forked this repo you can skip step 1 and run steps 2-4 directly against your
working tree.

Run these checks before publishing a repository or release.

1. Generate a clean export:

```bash
./scripts/grok-bridge-oss-export.sh
```

2. Confirm the export contains no private values. At minimum, scan for:

- private numeric chat ids
- private hostnames
- private node names
- maintainer usernames
- absolute home paths, plain **and** URL-encoded. The bridge quotes the working
  directory into Grok session paths, so a home path can leak percent-encoded -
  scan for the encoded form too, not only the plain one
- local secret paths

```bash
grep -rnE '<your-private-patterns-here>' dist/grok-telegram-bridge
```

Expected result: no matches.

3. Confirm no Bot API token-shaped strings exist:

```bash
grep -rnE '[0-9]{6,}:[A-Za-z0-9_-]{20,}' dist/grok-telegram-bridge
```

Expected result: no matches.

4. Compile and run the exported suite:

```bash
python3 -m py_compile dist/grok-telegram-bridge/grok_telegram_bridge.py
( cd dist/grok-telegram-bridge && python3 -m unittest discover -s tests -p 'test_*.py' )
bash -n dist/grok-telegram-bridge/grok-tui-session-start.sh
```

   The public `tests/` directory is governed by `PUBLIC_TESTS.manifest`
   (packaging/grok-telegram-bridge) - the single source of truth for which test
   files ship. `scripts/tests/test_grok_bridge_oss_export.sh` asserts the
   exported tests/ equals the manifest exactly and runs the full suite via
   `unittest discover`. Every public test is export-produced; never hand-edit a
   test in the public repo.

5. Confirm the public bridge does not reach for the internal message bus. The
export replaces the bus delivery path with a direct Bot API `sendMessage`, and
the export script's own dangling-helper gate fails if any bus helper reference
survives. Step 1 covers this; there is nothing extra to run here.

6. Confirm the chat id has no default. This is the leak that blocked the first
release attempt (T-260822-014). The public module exits at import if the chat
id is unset, so do not probe this by importing the file. Probe the source:

```bash
grep -q 'env("GRB_CHAT_ID", "")' dist/grok-telegram-bridge/grok_telegram_bridge.py
# expected: exit 0
if grep -qE 'env\("GRB_CHAT_ID", "[0-9]' dist/grok-telegram-bridge/grok_telegram_bridge.py; then
  echo "numeric chat id default leaked" >&2; exit 1
fi
echo "chat id default is empty: ok"
```

7. Re-read README billing language. It must say billing classification is
unverified and must not claim subscription safety.

8. Public release. Only after every check above passes AND the intended
maintainer explicitly accepts the operational risk. Publishing a repository,
pushing a tag, or flipping a repository to public is a separate human-approved
step - this checklist stops at "ready to publish".
