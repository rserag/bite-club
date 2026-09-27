# Third-party notices

The MIT license covers this repository's original code and documentation. It does not relicense external services, reference publications, downloaded food records or dependencies.

- Python dependencies are declared in `pyproject.toml` and locked in `uv.lock`. Their upstream licenses remain applicable; this repository does not vendor their source.
- USDA FoodData Central records are fetched only through explicit configured workflows. Preserve record identifiers, source metadata and applicable terms when importing or redistributing data. See [FoodData Central](https://fdc.nal.usda.gov/) and the [source adapter guide](docs/food-sources.md).
- The bundled adult nutrient references are a limited transcription of numerical facts from the published US/Canadian DRI tables. Source URLs, scope, review date and footnotes are retained. See [nutrient review](docs/nutrient-review.md). No complete reference publication is bundled, and no institutional endorsement is implied.
- The supplement protocol manifest retains its source references. It is not a substitute for the cited publications or individualized advice.
- Telegram, Telethon, Docker and other project names identify integrations; their trademarks belong to their respective owners. The project is not affiliated with those services.
- Demo, test and evaluation fixtures are synthetic unless a fixture explicitly identifies a public source. They are not real user histories or authoritative food values.
