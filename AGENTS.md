# AGENTS.md

Home Assistant custom integration (HACS) for the Haier Evo cloud (RU/KZ/BY). Single package: `custom_components/haier_evo/`. README, translations, and most commit messages are in Russian.

## Verification

- No test suite, linter, or type checker. CI (`.github/workflows/validate.yaml`) only runs HACS repo validation on push/PR.
- Quick syntax check: `python3 -m compileall -q custom_components/haier_evo`
- Real verification requires a running HA instance with a Haier account; debugging dumps: `GET /api/haier_evo` on the HA instance returns full integration state (POST to it is disabled by default).

## Architecture (non-obvious)

- Cloud polling + websocket push. REST (auth, device list) at `evo.haieronline.ru`; live status via persistent WSS using the sync `websocket-client` lib in a daemon thread — **not asyncio**. State reaches the event loop via `hass.loop.call_soon_threadsafe`; blocking calls go through `async_add_executor_job` (`__init__.py`). Don't naively convert to async.
- The `Haier` object lives in `hass.data[DOMAIN][entry_id]`. Device classes `HaierAC`/`HaierREF`/`HaierWM` (in `api.py`) are chosen from the `type` query param of the device link (`AC`/`REF`/`WM`); unknown types stay base `HaierDevice` and create no entities.
- Entities are created dynamically per device: platform files (`climate.py`, `switch.py`, ...) are thin; the `create_entities_*` methods on the device class decide what exists based on configured attributes.
- All commands go through `Constraint.apply` (`config.py`), which prepends/appends additional commands required by the device (from API `constraint` data).

## Device model YAMLs

- `devices/<MODEL>.yaml` maps numeric attribute codes (`id`) to canonical names and `haier` codes to canonical values; fallback is `devices/default.yaml` (ids blank).
- **Gotcha:** on first setup the YAML is copied to `<HA config>/haier_evo.<MODEL>.yaml`, and that user copy then takes precedence forever. Repo-side YAML edits have no effect on installs that already have the copy — delete the user copy to test changes (`config.py:_find_config_file`).
- The API returns Russian attribute/value names. Mapping to canonical names is hardcoded in `config.py`: `Attribute.__init__` (attr names) and the `Item` subclasses (`Mode`, `FanMode`, `SwingMode`, `Temperature`, ...). New attributes/values need entries there, not just YAML.

## Auth and rate limits (don't hammer)

- Tokens are persisted as plain JSON in `<HA config>/haier_evo` file.
- All calls run through `ResettableLimits` (`limits.py`, subclass of `ratelimit`'s decorator): login/refresh 1 per 15s with backoff growing to 900s; general API 5 per 60s. 429/500 responses extend the backoff. Failed login attempts lock out progressively up to 15 minutes — bad for testing.

## Releases

- Bump `version` in `custom_components/haier_evo/manifest.json`; HACS uses it for update detection. Repo has no git tags/releases — HACS tracks the default branch.
- `hacs.json` pins minimum HA version (currently 2024.12.0); code may use modern Python (3.10+ unions, enum flags like `ClimateEntityFeature`).
