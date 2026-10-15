# 0.6.5 — Model, context window, and reasoning effort cards

Send `/model`, choose a model, choose its context window, then choose reasoning
effort. Selecting the current model also opens the remaining settings. Available
windows and efforts come from the effective Grok account's CLI model catalog.
Models with one window or no reasoning effort skip the corresponding step.

Only the final choice submits a complete native command. Confirmation checks
model, context window, reasoning effort, and the returned composer. Stale cards,
duplicate clicks, session changes, and unavailable settings cannot submit again.
Active work, queued messages, drafts, and clear-session holds remain protected.

Restart only the bridge after updating. Preserve the Grok session, state directory,
queue, and Telegram offset. Cards from before the restart expire; open `/model`
again. No model inference call is needed to select settings.
