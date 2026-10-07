# Web guide verification

The guide is static. To check a local copy without running the bridge:

```sh
python3 -m http.server 0 --bind 127.0.0.1 --directory docs
```

Open the printed localhost address. The page header contains the English / 한국어 menu.

1. Select each language. The title, headings, instructions, navigation and footer change immediately.
2. Reload and confirm the selected language stays active. Open `?lang=ko` or `?lang=en` to override a stored choice.
3. Follow the section navigation and check that related guide links carry the selected language.
4. At a 390px viewport, confirm the language menu is visible and the text fits. Code examples may scroll horizontally.
5. With browser storage blocked, confirm immediate switching still works. Persistence is not expected in this mode.

The guide does not log in, run AI actions or change Telegram settings. Website language and `/language` commands are separate.

Initial local verification: 2026-10-06, shared generator and Aside desktop/390×844 previews.
Public hosting is a separate verification step: after publishing, repeat the language checks at the actual HTTPS URL and confirm the JS/CSS assets load.

## 한국어 확인 경로

로컬 설명서를 열어 상단 언어 메뉴 → 본문·목차 즉시 변경 → 새로고침 뒤 선택 유지 → `?lang=ko` 직접 진입을 확인합니다.
390px 화면에서 메뉴·본문이 잘리지 않는지 확인하고, 공개 게시 후에도 실제 HTTPS 주소에서 같은 순서로 확인합니다.
로컬 확인만으로 공개 게시나 설치된 브릿지 동작을 완료로 판단하지 않습니다.

## Session isolation and reply recovery

Prerequisites: an authenticated Grok CLI, a configured private Telegram bot,
and the bridge's TUI lane connected to one tmux pane.

1. Keep a second Grok session open in the same working directory.
2. Send a harmless question to the connected bot while the second session
   finishes a different question.
3. Confirm that typing remains active for the bot's question. The other
   session's answer must not appear as its answer.
4. Confirm that the connected session's final answer arrives once and typing
   ends. The submitted question must not be echoed as a new local message.
5. When the connected Grok process changes sessions, confirm that the bridge
   follows that process. Missing or ambiguous ownership must not select a
   session solely because its history is newer.

Release 0.6.2 verification on 2026-10-07: the public module passed its public
suite and 16 upstream session-binding regressions with isolated fixtures.
Those fixtures cover shared directories, missing/ambiguous ownership,
compaction, restart recovery, full-question matching, typing and duplicate
suppression. The same upstream fix was also checked in a live Telegram
roundtrip. This does not claim a live check on every public installation.

GitHub Pages publishes `main:/docs` automatically. Merging this release also
rebuilds the existing static guide; no bridge service runs in GitHub Pages.
