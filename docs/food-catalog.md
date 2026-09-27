# Local food catalog

The first food-ledger increment adds a local, reviewed catalog. The [Telegram meal diary](meal-diary.md) now uses these selected records for measured meal logging and corrections. Use the catalog import for development or operator-reviewed records; [USDA lookup and caching](food-sources.md) is now available separately. Manual import itself makes no network calls. A catalog record is food composition, never evidence that a meal was eaten.

## Import and review

Stop the worker, run migrations, then preview a private UTF-8 JSON file. These commands need storage configuration but no Telegram token or AI service:

```sh
uv run nutrition-bot migrate
uv run nutrition-bot food-import --input-file private/reviewed-food.json
```

Review the food identity, preparation, original serving mass, nutrient definitions, amounts, units and source. The preview does not save anything. Save after that review:

```sh
uv run nutrition-bot food-import --input-file private/reviewed-food.json --reviewed
```

The output contains the food ID and immutable version ID. To correct the same food, preview the new input and then save with its `--food-id`. Use a new food identity for a different preparation. The importer never merges foods by their names. Reimporting identical data against the same food ID reuses its latest version; importing without a food ID creates a new identity.

The command holds the same file lock as the worker. Restart the worker after maintenance. Keep real food inputs and command output private. The import file is limited to 256 KiB. No credentials, media or raw provider payload belong in it.

## Input shape

This example is **synthetic test data, not a food reference**. Replace every value and provenance field with reviewed source information before using an actual food:

```json
{
  "name": "Synthetic test food",
  "brand": null,
  "preparation": "as_sold",
  "source_reference": "Synthetic label fixture",
  "source_url": null,
  "source_license": "Synthetic fixture; no external food data",
  "basis_grams": "40",
  "nutrients": [
    {"code": "energy", "amount": "120", "unit": "kcal"},
    {"code": "protein", "amount": "10", "unit": "g"},
    {"code": "sodium", "amount": "0", "unit": "mg"},
    {"code": "calcium", "amount": null, "unit": "mg"}
  ],
  "portions": [
    {
      "label": "one item",
      "grams": "40",
      "original_measure": "one item",
      "source": "Synthetic average mass",
      "is_estimate": true
    }
  ]
}
```

Amounts refer to `basis_grams` of edible food. Strings are recommended for decimals; JSON decimal numbers are also parsed directly as Decimal. Mass supports milligram precision and is rejected rather than silently rounded if more precision is supplied. Omitted nutrients and explicit `null` both mean unknown. Explicit `0` means a source-reported zero. At least one known nutrient is required. Source URLs are stored as provenance only and never fetched by this importer.

Preparations are `raw`, `cooked`, `as_sold`, `as_prepared`, or `unspecified`. Do not use an unspecified preparation as evidence that a cooked/raw choice was resolved. Portion masses default to estimates; storing a portion never grants permission to save future estimated meals.

## Nutrient definitions

The initial registry contains:

| Codes | Canonical unit |
|---|---|
| `energy` | kcal |
| `protein`, `carbohydrate`, `fat`, `fiber` | g |
| `sodium`, `potassium`, `calcium`, `magnesium`, `iron`, `zinc`, `vitamin_c` | mg |
| `vitamin_d`, `vitamin_b12` | ug (micrograms) |

`carbohydrate` means total carbohydrate **including fiber**. Do not silently map available-carbohydrate values to this code. `sodium` is elemental sodium, not salt mass; `vitamin_d` means D2 plus D3 mass, not IU. Unsupported definitions require an explicitly registered new nutrient code. The repository's `register_nutrient()` accepts a code, name, unit and definition; it rejects replacing the meaning of an existing code. Source-specific adapters must map definitions explicitly.

Only g/mg/ug mass conversions are automatic. Energy must be provided in kcal; it is not calculated from macros. The importer does not infer missing nutrients, recommend targets or decide whether a source is trustworthy. An operator review is still required.

## Storage and history

Migration `0002_food_data` adds `nutrients`, `foods`, `food_versions`, `food_nutrients`, and `food_portions`. It seeds nutrient definitions only, with no fabricated food values. It preserves the existing transport tables.

Source amounts/units and source serving mass remain stored alongside normalized per-100g values. Normalized quantities use INTEGER millionths of the canonical unit, calculated with Decimal and one ROUND_HALF_EVEN quantization at storage. Portion mass uses integer milligrams. Scaling retains Decimal precision until presentation or a future log snapshot boundary. Tiny positive source values can round to zero at this storage scale; their original value remains available for inspection.

A food version contains the review timestamp, source reference/URL/license, calculation version and content hash. Its nutrient rows retain source units/amounts, optional notes and `manual_reviewed` quality. Updates append a version; old versions stay intact. All inserts and sealing occur in the caller's transaction. A failed write rolls back the entire snapshot. Readers expose sealed versions only.

SQLite triggers prevent changing sealed versions, adding/removing/reassigning their child rows, or rewriting registry definitions and food preparation. Whole-version deletion is intentionally possible at the database level for the future explicit erasure workflow; there is no catalog deletion command. Future meal snapshots must reference food-version IDs with restrictive foreign keys. The local data owner can always change the database directly; these constraints protect application invariants, not against an owner with unrestricted SQL access.

The importer and repository are trusted administrative/application entry points. They are not exposed as AI tools. Telegram drafts separately enforce reviewed food selection and exact-quantity/estimate approval before saving consumption; see [meal drafts](meal-drafts.md).

## Verification

The original catalog increment passed 96 local tests, with Ruff checks/format and strict mypy passing. Food-specific checks cover unknown versus zero, Decimal rounding/unit scaling and input bounds, registry extension, source basis and original values, measured/estimated portion metadata, immutable history including SQLite replacement operations, rollback after late failure, unsealed-read protection, and preservation of every populated foundation table during upgrade. Erased food/version IDs are not reused.

Source and wheel artifacts build successfully. An installed-wheel smoke check outside the checkout passed repeated migration, registry initialization, database integrity, preview without saving, and reviewed import. Archive inventory excludes private files and local development-tracker metadata. The implementation has local and Linux-container coverage; full real-device acceptance of newer diary flows remains pending.

The current combined source/catalog verification is recorded in [food sources](food-sources.md).
