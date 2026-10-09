# Food discovery, source review and offline selection

Telegram and the Mini App share preparation-aware search over current saved food versions. Explicit aliases can retain a reviewed historic version. Search never substitutes cooked for raw or selects an ambiguous food automatically.

In Telegram, choose **Log food**, search by name, then use **Search all foods** for USDA results even when a saved match already exists. Select the full source description and review its preparation, values, missing nutrients and source. Enter measured grams and use the current confirmation to save composition and consumption together. The pending meal date and other ingredients survive discovery, label entry and provider failures. Rough portions still open a fresh exact-revision approval draft.

The Mini App offers **Find in USDA** and optional **Barcode** lookup beside saved-food search. Add the current reviewed source preview to the meal, enter measured grams, and save the mixed source/saved meal once. **Continue with a label in chat** reviews and hands off the unfinished measured meal through the durable inbox, retaining its food versions, source hashes, grams, date and label. It opens chat only after the handoff is accepted. Recipe history shows consumed ingredient equivalents and edits the actual cooked portion or serving share while preserving its recipe version.

Accepted sources remain available offline. **Enter food label** provides a no-network fallback; see [packaged foods](packaged-foods.md). Operator CLI commands below remain available for bulk maintenance.

## Configuration and access

Set `USDA_API_KEY` only in a private environment file. An empty key disables remote lookup and leaves saved foods and reviewed manual imports available. `USDA_TIMEOUT_SECONDS` defaults to 10 seconds and is bounded to 1–30 seconds. Obtain a free dedicated key through the [USDA API guide](https://fdc.nal.usda.gov/api-guide/); public `DEMO_KEY` access is limited to 30 requests/hour and 50/day. A dedicated key is recommended for regular use. Do not paste keys in repository files or issue descriptions.

The adapter uses the fixed USDA HTTPS endpoint, sends its key in a header using the [federal API gateway mechanism](https://api.data.gov/docs/developer-manual/), rejects redirects, and does not log request/response bodies, keys, queries, or raw exception text. Requests have both per-request and total-operation deadlines, a response-size limit, and no automatic retries. A rate-limit response carries a bounded retry hint when available. No AI provider is involved.

## Find, review, select

These are local maintenance commands; as with manual food import, stop the worker before using commands that take the database file lock. The runtime uses durable owner/chat/revision-bound jobs and performs network work outside write transactions. Cancellation, restart recovery and stale results cannot approve or save a meal.

```sh
uv run nutrition-bot migrate
uv run nutrition-bot food-search --query "rice cooked"
uv run nutrition-bot food-search --query "rice cooked" --remote
uv run nutrition-bot food-search --query "rice cooked" --offline
```

Search checks the latest versions of saved foods first. If matching local foods exist, it does not call USDA unless `--remote` is requested. `--offline` always prevents network requests. Results show local version IDs or remote FDC IDs, full descriptions, preparation hints and source types. No result is selected automatically.

Use an actual returned FDC ID to fetch a preview:

```sh
uv run nutrition-bot food-fetch --fdc-id RETURNED_FDC_ID
```

The preview contains the complete normalized record, source warnings, a content hash, fetch time and expiry. Review the food, raw/cooked state, nutrients and portion basis. Then select exactly that preview using its returned hash and the correct preparation:

```sh
uv run nutrition-bot food-select --fdc-id RETURNED_FDC_ID --expected-hash RETURNED_HASH --preparation cooked
```

Preparation choices are `raw`, `cooked`, `as_sold`, and `as_prepared`. A known source preparation cannot be contradicted; choose another record if it does not match. An ambiguous source requires an explicit preparation choice, and the full description remains visible. Do not reinterpret dry-matter research records as ordinary edible food. The command saves food composition only, not consumption or an estimated meal quantity.

Use the returned local version ID for offline inspection:

```sh
uv run nutrition-bot food-show --version-id RETURNED_LOCAL_VERSION_ID
```

A fetch with `--refresh` requests new source data. `--offline` reuses an existing unexpired preview. These switches cannot be combined. Missing keys, provider errors and rate limits return explicit status plus saved-food/manual-label alternatives; they never report an unresolved food as saved. See [manual food import](food-catalog.md) for the fallback path.

## Cache and history

Fetched normalized source previews are disposable. They are fresh for 24 hours, expire after seven days, and are capped at 200 records. Older cached previews can be shown during an outage until expiry, with their status and original fetch time visible. Expired previews cannot be selected. Cleanup removes expired previews; it never promotes them to saved records. Raw API payloads and search-query histories are not retained.

Selecting a preview validates its content hash within the same transaction that saves the food and source link. Changed or expired previews require review again. Identical repeated selection reuses the current version. Refreshing and selecting changed data appends an immutable version for the same FDC ID and preparation, retaining the earlier values. Different FDC IDs remain separate identities, even if their names look similar.

Saved food versions remain available indefinitely, independently of the disposable preview cache or API key. Each stores USDA ID, data type, source URL/license, provider publication date when supplied, fetch/review times, adapter/calculation versions and source warnings. Nutrients retain original values/units and source derivation/qualification notes. `source_reported` means reported by USDA; it does not claim every value was measured directly. USDA calculation/derivation metadata must be considered by later coverage analytics.

Migration `0003_food_sources` adds nullable provenance fields to existing versions and introduces `food_source_cache` and `food_source_links`. Existing manual records retain their values, version IDs and immutability protections. These source identity links will need to participate in the future explicit erasure workflow.

## Source interpretation

Only Foundation, SR Legacy and Survey/FNDDS generic-food details are supported in this increment. USDA branded details are not supported; typed barcodes use the separately optional Open Food Facts adapter described below. Unsupported food types offer manual label entry. Search responses supply candidate identities only. Nutrient values come from full food details.

The mapping explicitly uses USDA nutrient IDs for the 14 registered nutrients. It does not use the separate legacy nutrient numbers as IDs. The database retains unknowns for missing or unusable values. Unsupported nutrients are omitted with a warning until a reviewed mapping is added.

Source nutrients use a 100 g basis, as specified in the [USDA field dictionary](https://fdc.nal.usda.gov/portal-data/external/dataDictionary). Portions use the full `gramWeight` of the accompanying description; the adapter never treats that weight as a one-unit weight by dividing silently. Foundation, SR Legacy and FNDDS describe their portions differently, so each has its own label interpretation. Invalid or ambiguous portion definitions are omitted with a warning. Every USDA average portion remains an estimate and will require separate meal-estimate approval in Telegram.

Energy priority is **2048, then 2047, then 1008**—a documented application choice favoring a source's specific Atwater value, then general energy, then the legacy field. Available energy fields are never summed, and no macro-derived estimate fills a missing value. A selected energy field with an unknown amount stays unknown. USDA documents these energy fields and the inclusion of fiber in carbohydrate-by-difference in its [Foundation Foods documentation](https://fdc.nal.usda.gov/Foundation_Foods_Documentation/).

Below-quantification-limit values are not treated as known zeros: the adapter retains the qualifier in notes and leaves the value unknown. Duplicate values for one mapped nutrient also remain unknown. Older USDA zero values may lack a qualifier; the adapter cannot reconstruct information absent from the source. Unit mismatches, unsupported definitions and missing values are disclosed rather than guessed. No salt/sodium, IU/mass, or unrelated vitamin-definition conversions are inferred.

FoodData Central data are CC0, with USDA attribution retained in source records. This does not make food matching automatic or remove natural variation in food composition. [USDA licensing and API access](https://fdc.nal.usda.gov/api-guide/)


## Optional barcode provider

Set `OPENFOODFACTS_ENABLED=true` to enable typed barcode lookup. No provider key is required. The adapter validates the GS1 check digit, normalizes the code and sends only those digits to the fixed HTTPS product endpoint with an identifying User-Agent. It uses API v3.6, bounded response size/deadline, no redirects, and a minimum 4.1-second interval between product reads. [API access and limits](https://openfoodfacts.github.io/openfoodfacts-server/api/)

Review the matched brand/variant and printed label; community data can be wrong. The adapter selects one original as-sold 100 g packaging input set, or one manufacturer set when packaging is absent. Duplicate, serving-only, prepared or volume-based sets require manual label review. It imports original `value_string` and units, never the computed aggregate, inferred sodium/energy or estimates. Ambiguous carbohydrate definitions remain unknown. [Nutrition schema](https://github.com/openfoodfacts/openfoodfacts-server/blob/main/docs/api/ref/schemas/product_nutrition_v3.yaml), [original and computed values](https://github.com/openfoodfacts/openfoodfacts-server/blob/main/docs/api/ref/schemas/nutrient_values_v3_base.yaml)

Source metadata retains Open Food Facts attribution, ODbL database/DbCL contents licensing and the canonical barcode URL. No product images or catalog are downloaded into the public repository. Accepted snapshots remain immutable and reusable offline. Unmatched barcodes, unavailable providers and unsupported label bases offer typed-label entry; a barcode never establishes the amount eaten.

## Verification

Offline synthetic tests cover shared current-version search, cooked/raw distinctions, bounded provider failures, original units and unknowns, immutable cache acceptance, stale hashes/buttons, cancellation/restart, atomic mixed-meal rollback and fresh estimate approval. Provider requests waiting on a response do not block local `/today` processing. No test sends live Telegram messages or paid model requests.

A separate compatibility check read a public product from the provider's documented staging service with its public test access. The current v3.6 schema parsed original as-sold 100 g values without storing a product or personal data. This checks compatibility, not the quality of all community records.
