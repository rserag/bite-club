# Personal aliases, favorites and repeat meals

T04.2 adds shortcuts to the meal diary. All food values still come from immutable reviewed catalog versions. It needs no AI or external request. These features are implemented, tested and deployed; full real-device acceptance remains pending.

## Personal food names

Choose the exact food and preparation using `/foods`, then give that version a personal name:

```text
/alias my rice = #12
150g my rice
/aliases
/alias A1
/alias update A1r1 = #18
/alias off A1r2
/alias on A1r3
```

The numbers are hypothetical. Creating an alias confirms its food identity and records no consumption. The alias view names the selected food, preparation, version and current alias revision. Matching ignores case and normalizes Unicode and whitespace; it never uses fuzzy matching. Duplicate names, including disabled aliases, must be updated or re-enabled explicitly. Names containing meal syntax are rejected.

An active alias takes precedence over an exact catalog name because it represents an explicit personal selection. A `#version` reference always selects that version directly. Aliases stay pinned when the catalog receives a newer version. Remapping or disabling one never rewrites earlier meals. During corrections, an existing food's original name keeps its historical selection. Use item numbers when correcting a receipt logged through an alias.

## Save and use a favorite

Reply to a current saved meal receipt:

```text
save as usual breakfast
```

Or use its explicit revision:

```text
/favorite save usual breakfast = M1r1
/favorites
/favorite F1
/eat usual breakfast
/eat F1v1
/eat yesterday F1v1 x0.5
```

Saving creates a favorite, not another meal. Its preview shows every food version and fixed portion. **Log this** logs those portions; **Archive** hides the favorite. A bare name such as `usual breakfast` or `I ate usual breakfast` also works when it is an exact, unambiguous favorite name. If that name also identifies a catalog food or active alias, use `/eat` to state your intent. Names that contain meal amounts, estimate qualifiers or other food grammar require explicit `/eat`; they cannot override normal meal parsing.

Favorites and repeats also preserve [recipe portion provenance](recipes.md), including its exact batch fraction. The default is one full saved portion. Scaling accepts a positive factor up to 100 with at most three decimal places. Every result must be an exact integer milligram, between 1 mg and 50 kg per item; the bot rejects an unsupported scale instead of rounding. Dates use the profile's local calendar; future dates are rejected. Button logging uses the callback's arrival date in the current profile timezone, not the older preview's date.

Change a favorite by selecting a corrected, current meal:

```text
/favorite update F1v1 = M2r1
/favorite archive F1v2
/favorites all
/favorite restore F1v3
```

Each update, archive and restore creates a new immutable version. Explicit writes and uses of `F` references require the displayed version. An old reference or button never silently switches to new portions. `/favorite F1` opens the current version. Names remain stable; saving a different name creates a separate favorite. Archiving retains history and does not delete logged meals.

## Repeat a previous meal

Reply `same again` to a current meal receipt, or send:

```text
/repeat M1r1
/repeat yesterday M1r1 x2
same Lunch as yesterday
```

The last form works only when yesterday has exactly one current, non-deleted meal with that label. Missing or multiple matches require an explicit receipt selection. Repeating uses the current exact meal revision and creates a new consumption event; it does not move the original meal. Deleted meals and stale references cannot be reused.

## Estimates still need approval

Fixed measured portions can be reused immediately. If any portion was estimated, repeating or using a favorite creates a **new draft** with all quantities, food versions and estimate bases displayed. Nothing enters totals until the user supplies measured amounts or taps **Approve estimate** on that draft's current revision.

A favorite retains the uncertainty basis but never an earlier approving action, time or draft reference. Scaling an estimate preserves its estimated status. The new meal gets new approval metadata. Changes, expiry and replay protection follow the [draft contract](meal-drafts.md). Repeating a favorite's Log button cannot create duplicate meals or drafts; send a new `/eat` or open a new preview to deliberately log another serving.

## Storage and operation

Migration `0007_food_aliases` adds revision-checked personal aliases. Migration `0008_favorites` adds favorite identities, immutable versions and ordered fixed portions. Both reference sealed food versions. SQL guards protect identity, history and current-version pointers. Those migrations establish the reuse feature; [recipe support](recipes.md) advances the current schema head to `0010_recipe_shares`.

All writes, deduplication and outgoing receipts commit in the existing local transaction. Buttons remain bound to the authorized owner/private chat, delivered message, opaque token, available action and favorite version. Editing an original Telegram message never silently logs a new meal or changes a favorite. Temporary draft cleanup removes raw quotes while preserving saved meal and favorite receipts; ordinary raw-message retention still applies.

Aliases, favorite names, portions and their history are personal data in the private SQLite database. They belong in consistent backups and must stay outside Git and release archives. There are no additional services, ports, credentials or dependencies. Stop the worker, take a consistent backup and migrate before starting a new release. Downgrade removes the new preference tables; use a matching pre-upgrade backup for application rollback. Deployment is a separate step.

## Verification

The combined local suite has **964 passing tests** (the full 958-test run plus the final expanded routing regressions). Coverage includes Unicode aliases, exact source pinning, immutable favorite history, archive/restore, whole-input parsing, stale and unauthorized callbacks, duplicate delivery, exact scaling, backdating, fresh estimate approvals, retention and populated migrations. Ruff checks/format and strict mypy pass. An installed wheel outside the checkout passed alias pinning, favorite scaling, estimate reuse across restarts, fresh approval/replay and a synthetic SQLite backup/restore preserving favorites, aliases, totals, unknown nutrients and approval history. Release contents exclude private and tracker state. This is local acceptance, not a live Telegram or Linux deployment check.
