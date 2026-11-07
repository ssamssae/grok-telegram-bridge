# 0.6.7 — Pinned model cards, Stop button, and threaded answers

While a `/model` card (model, context window, or reasoning effort) waits for
your choice, the bridge pins that card in your private chat without a
notification. Later messages can keep arriving; the pinned list at the top of
the chat keeps the open card within reach. Choosing an option, the card's
expiry, or a bridge restart unpins it. The bridge pins only cards it sent to
this chat, makes at most one pin or unpin call per five-second tick, and retries
a failed call up to three times, one minute apart. Pinning is presentation
only; it never answers a card. Set `GRB_QUESTION_PIN=0` to turn it off.

The progress card now has a Stop button for the running Grok turn. One press
sends a single Ctrl+C, and only while that same turn is still running, the
input box is empty, and no question, sub-task confirmation, or cancelling
prompt is on screen. Grok's Esc key never cancels a turn, so the button does
not use it. A turn that Grok ends as cancelled by a person closes the card at
once as interrupted, without an error message. A turn that another turn
supersedes after a Grok restart ends as superseded, so the button cannot cancel
the next turn. Set `GRB_STOP_BUTTON=0` to hide the button.

The progress card and the final answer now reply to the Telegram message that
asked, as the Claude bridge does. Answers to messages inserted while Grok was
busy stay unthreaded. A reply still arrives when the original message was
deleted. Editing a card keeps its buttons.

The Input now card closes when the next job starts, so a later queued message
gets a fresh card instead of an update to an old one.

The shared helper modules `bridge_i18n.py`, `bridge_public_text.py`, and
`terminal_turn_mirror.py` match the sibling bridges again. For Grok this adds
the handoff card shown when a message pasted into the terminal is a directive
from another agent; the other new entries are used only by the sibling bridges.

Restart only the bridge after updating. Keep the native Grok session, the state
directory, the queue, and the Telegram offset. Cards from before the restart
expire; open `/model` again.
