# Using AI meal drafts in Bite Club

When meal-text AI is enabled for your bot, send a short description of what you
ate in a new message. When meal-photo AI is enabled, send one photo with a
caption naming each food and its preparation. Both routes propose editable
meals using reviewed catalog versions. Review every item, correct anything
wrong, and tap **Approve draft** on the latest preview to save it.

AI availability depends on the bot's setup. `/status` reports text and photo
availability separately. Each route needs its own privacy and quality gate;
enabling text does not enable photos. Manual logging, favorites and reports
remain available when AI is disabled or temporarily unavailable.

## Start with clear food descriptions

1. Find the food in `/foods rice` or use **Log meal** from `/home` to search the
   reviewed catalog. Choose the version that matches the state when the food
   was weighed: raw/dry, cooked, packaged/as sold, or prepared.
2. Include edible grams for each food when you weighed it. Write `about` or
   `roughly` when the amount is an estimate. Leave an unknown amount unknown.
3. Include how the food was prepared or served, separately from when it was
   weighed. Include every meaningful ingredient, such as cooking oil or sauce.
4. Say `yesterday` or give an explicit past date if it was not today's meal.
   Check the date in the preview before approval.
5. Send a standalone message. Commands and replies to existing receipts follow
   their own local rules and do not start a new AI interpretation.

These examples use hypothetical foods that must first exist in your own
reviewed catalog:

```text
For lunch I had 180 grams of cooked rice and 120 grams of cooked chicken breast.
Yesterday's dinner was roughly 160g cooked rice, 110g cooked chicken breast,
and about 5g olive oil as sold.
On 2026-01-15, my snack was 160g plain yogurt as sold and 85g raw banana flesh.
Before cooking, I weighed 80g dry rice and 180g raw skinless chicken breast.
```

The bot tries local parsing first. A familiar measured message such as
`/meal Lunch: 180g #27; 120g #31` may save directly with a receipt and no AI call.
Here `#27` and `#31` are example version numbers; use the numbers returned by
your own `/foods` search. Local rough portions open an estimate draft instead.
There is no special AI command and no need to force a model call when normal
logging already works.

Avoid descriptions such as `a normal plate`, `some chicken` or `my usual lunch`
when you can give the food and grams. Counts and cups do not establish edible
weight. Raw and cooked weights are different; the bot cannot infer a reliable
conversion from the name alone. An explicit raw or dry weight before cooking
uses the reviewed raw ingredient version and the stated grams; it does not mean
you ate the food uncooked. A weight taken after cooking uses the matching cooked
version. The bot must not guess the cooked yield, added oil or the share of a
batch you ate. Give those details when known. If you have a saved favorite or
recipe, use its normal repeat/portion flow rather than asking AI to reconstruct
it.

## Review the whole draft

An AI preview begins **AI proposal — review every item** and has a reference
such as `D1r1`. It stays outside your daily totals until you approve it.

Check:

- **Date and meal label:** is it the intended day and meal?
- **Food and weight basis:** is each reviewed food version the right match for
  the raw, cooked or packaged weight you supplied? A raw source label describes
  the nutrient and weight basis, rather than how the food was served.
- **Reported preparation:** check the separate cooking-method and preparation
  notes when present. These preserve supplied details without converting the
  raw ingredient weight into a guessed cooked weight.
- **Items:** did the bot include everything you described, without extra foods?
- **Amounts:** do the edible grams reflect what you know, with estimates clearly
  identified?

An explicitly reported mass remains measured when it can be tied to your input.
Your explicitly approximate mass remains an estimate, with its uncertainty
shown. Unsupported amounts remain unknown. You can supply a measured amount
explicitly in an edit. Every AI draft still needs **Approve draft** after
editing; typing `yes`, `ok`, `save` or `approve` does not save it.

If a portion is unresolved, the preview says **amount needed** and has no
approval button. Enter the amount or cancel. Never supply an exact-looking
number merely to make an unknown portion disappear.

AI chooses catalog identities and proposes portions. It does not supply calories,
macros, vitamins or other authoritative nutrient values. Saved nutrition is
calculated from the reviewed food sources, and missing nutrient data stays
unknown. Approval accepts the displayed proposal; it does not make an estimated
portion measured or prove that the food source is complete.

## Correct, approve or cancel

Reply to the **current draft preview** with one of these forms:

| Reply | What it does |
| --- | --- |
| `item 1: 150g` | Supplies a measured amount for the first item |
| `item 1: about 150g` | Supplies an estimated amount for the first item |
| `item 2: 80g #32` | Changes the second item's amount and food version |
| `item 2: delete` | Removes that item from a multi-item draft |
| `item 1: preparation boiled` | Corrects the reported cooking method |
| `item 1: preparation clear` | Removes the separate reported preparation note |
| `replace: 150g #27; about 120g #31` | Replaces the complete item list |
| `date yesterday` | Corrects the meal date |
| `date 2026-01-15` | Sets an explicit past date |
| `cancel` | Discards the draft without logging it |

Version numbers in the table are synthetic examples. **Enter amount** redisplays
the draft's editing instructions; reply with the correction. For a one-item
draft, a reply such as `150g` is sufficient. Change the date separately from an
item replacement.

Each edit produces a new revision, for example `D1r2`. Recheck the complete
updated preview and tap its **Approve draft** button. An old button cannot
approve edited contents or log the meal twice. Wait for the saved meal receipt
before treating the meal as logged; `/today` then shows its contribution.

Use `/drafts` to list open drafts and `/draft D1` to reopen one. An explicit edit
needs the current revision, for example `/draft D1r2 item 1: 150g`.
Opening or editing keeps a draft active; after seven days without that activity,
it expires without saving. Editing your original Telegram message does not
rewrite a draft or saved meal: the bot returns the current preview/receipt so
you can make an explicit correction.

For an already saved meal, open the latest receipt with `/meals` and use **Edit**
or reply to that receipt. **Delete** and **Undo** act on the diary; deleting a
Telegram message does not delete its meal. See the [meal diary guide](meal-diary.md).

## When AI asks you to enter the meal manually

Ambiguous foods or preparation, unsupported requests and incomplete
interpretations produce a specific clarification with a manual-entry button.
No partial meal is saved, and the bot does not continue an AI conversation with
the earlier message. Send one complete revised description if you want another
interpretation.

When the complete meal is understood but a food is missing from the catalog,
the bot starts the existing food discovery and source-review flow. It retains
the meal date, ordered known foods and quantities while you review the missing
food. Missing portions are requested. The complete meal then opens an editable
AI draft for its own approval; accepting a food source does not save that meal.

Search for the correct reviewed food, then use **Log meal** or a measured
`/meal` command. If you need another AI attempt, send one complete standalone
description containing all foods, preparations, quantities and the date. A food
missing from the catalog must be reviewed and added through the food-source
workflow first; asking AI for its nutrition cannot add it.

The same fallback applies when the daily request allowance is reached, access
fails or the result is unknown after a timeout. Do not repeatedly resend the
same description while waiting. Failed/unknown attempts can still use the
allowance, and an unknown result is not automatically retried. There is no
automatic switch to a paid provider.

The default bot allowance is 100 ChatGPT attempts per UTC day, with the next
day beginning at **00:00 UTC**. Operator-run validation has its own app allowance
of 100 attempts and cannot use the bot's pool. Both still share the selected
ChatGPT account's provider limits, so provider access can be unavailable even
before the bot reaches 100. A new day does not automatically retry an earlier
message or approve a draft.

Telegram can deliver an update twice or display a duplicate bot reply. Replayed
updates and repeated taps cannot save the same action twice. Sending another
new message is a separate request, however, and can create a separate draft or
meal. Check `/drafts` or `/meals` before resending; cancel unwanted duplicate
drafts. Repeating an AI-origin meal or using an AI-origin favorite opens a fresh
review draft, including when you previously edited every portion into measured
grams. Approval belongs to that occurrence; an earlier approval cannot save it.

## Meal photos

Send one JPEG or PNG meal photo with a caption naming each food and preparation.
Include every meaningful ingredient, including oil, sauce and toppings, and the
meal date when needed. Photos without a caption ask for those details before
any image download or model request.

The image can help check plausible identities. It cannot reliably establish
edible weight, hidden oil or the preparation of an ambiguous mixed dish.
Gram amounts require explicit evidence in your caption. Without it, the draft
shows **amount needed** and cannot be approved. If identity or preparation is
unclear, the bot asks for details or offers manual entry. It does not invent
precise weights to fill the draft.

Use one photo outside an album. The download must fit the configured byte and
dimension limits; the current limits are 2 MB and 1600 pixels per side. Oversized,
unavailable or invalid images offer manual entry. An enabled text route does
not interpret a photo caption when the photo route is disabled.

## Scope and privacy

This guide covers meal text and meal photos. Label scanning, barcode-photo
extraction, AI questions about your history and AI training advice are outside
these routes. Use `/today` and `/week` for deterministic reports. Workout,
weight and recovery entry keep their existing commands.

AI fallback sends your meal description, the photo when applicable, and a bounded
set of relevant reviewed catalog names/preparations to the configured provider.
It does not upload the whole diary. Photos are downloaded and processed in memory;
the app does not keep an image-file copy. Raw Telegram metadata follows the normal
raw-input retention rule, and linked temporary photo proposals follow the same
seven-day draft expiry and cleanup as text. Logs do not contain descriptions,
photos or model payloads. Do not include unrelated personal details. Telegram
and the provider have their own data handling; self-hosting and training opt-out
do not imply zero external retention.

For ordinary logging, see [everyday use](everyday-use.md) and
[draft editing and approval](meal-drafts.md). Operator configuration, policy
review and evaluation requirements are in [AI setup](ai-setup.md).
