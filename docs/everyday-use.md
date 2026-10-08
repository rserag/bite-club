# Everyday use

Use `/home` or `/start` for the daily menu. `/help` opens help by topic instead
of returning the whole meal manual. Telegram's command menu also lists the
main actions; a configured Mini App replaces that menu button with Open diary.

## Logging and reusing meals

Choose Log meal, type a food name, select the exact reviewed food version, then
enter measured grams. Review the item list, add another food if needed, and save.
An amount prefixed with `about` opens a draft outside your totals; approve its
current displayed revision explicitly. Changing a draft invalidates its old
approval button. Guides survive a worker restart and expire after 30 minutes of
inactivity without saving. `/cancel` returns to the menu.

Recent meals opens a receipt with Edit, Repeat, Save favorite, Delete and Undo.
Save favorite asks for a name. Edit accepts a correction without needing to
reply manually to the original receipt. Repeated taps cannot duplicate the same
consumption; to log another occurrence, open a fresh preview or its latest receipt.
Repeating estimated portions always needs a new draft approval. Favorite and
recipe updates affect future uses and preserve earlier meal snapshots.

## Reports and setup

Daily and weekly reports initially use the short view. Choose Details/Full report
for the underlying nutrient data and coverage; unknown amounts stay visible.
Explicit `/today full`, `/today short`, `/week full` and `/week short` override the
saved report preference. Use `/settings reports full` to change the default.

Help & setup explains reviewed foods, goals, timezone and reminders. Goal
proposals require their existing Apply button before changing future targets.
Quick Weight, Training and Recovery actions ask for the required input; optional
training/recovery details remain optional.

## Optional features

[Settings and reminders](settings-and-reminders.md) describe opt-in schedules,
quiet hours and completed-check-in suppression. No private schedule is built in.

[AI setup](ai-setup.md) covers app-owned ChatGPT OAuth and the optional capped
OpenRouter route. AI interprets meals into reviewed drafts, using existing food
sources; it never supplies authoritative nutrition values or approves a meal.
Live activation requires credentials, the reviewed privacy policy and appropriate
evaluation evidence. Manual logging and reports work while AI is disabled.
The [AI user guide](ai-user-guide.md) explains how to describe a meal, review the
current draft, correct it and handle clarification or limits.

[Mini App setup](mini-app.md) covers the authenticated food browser, dashboard,
meal history and measured-portion forms. Estimate approval stays in the Telegram
chat so the displayed draft revision remains the authority.
