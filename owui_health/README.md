# Health tools for Open WebUI

Two tools for the personal health stack (`health/` in the home server's compose repo, which owns the database,
the nightly ingest and the setup instructions):

| Tool | Import file | What it does |
|---|---|---|
| Health Data | `../health_data.json` | Read-only queries and **deterministic statistics** over any date range: `health_report` (day/week/month against the user's own baseline, targets, gaps), `health_summary`, `health_trend`, `health_compare`, `health_baseline`, `health_correlate`, record lists (activities, food, calendar), `health_profile`, `health_sources`, and a guarded `health_sql`. |
| Food Log | `../food_log.json` | Logs food under the **food-logging constitution**: typed items and portions only, validated with JSON Schema plus time rules before anything is saved; violations come back with rule ids for the model to fix. With the `USDA_FDC_API_KEY` valve it also checks each item against USDA FoodData Central, using the ingest's own matching rules, and holds back foods USDA can't match or weigh until the user decides. Writes one file per entry to the user's inbox; it has no database access. |

```
 Open WebUI                                      health stack
 ┌─────────────────────────────┐  health net   ┌──────────────────────────────────────┐
 │ Health Data tool            │──────────────▶│ health-db: health_<key> per person   │
 │  valve PERSON_DSNS:         │ as <key>_ro   │  <key>_ro: SELECT, read-only, own DB │
 │  user id -> read-only DSN   │               └──────────────────────────────────────┘
 │ Food Log tool               │                          ▲ writes (only path)
 │  valve PERSON_KEYS:         │  /health_inbox ┌──────────┴───────────────────────────┐
 │  user id -> person key      │───────────────▶│ health-ingest: validates, loads,     │
 └─────────────────────────────┘  <key>/pending │ resolves nutrients (USDA)           │
                                                └──────────────────────────────────────┘
```

* **The model never computes numbers.** Every statistic is calculated by the tool from the stored daily rows
  (`api.daily`), with the method stated in the result. Same input, same output.
* **Per-user isolation.** The valves map each Open WebUI user id to that person's own database role (Health
  Data) or inbox (Food Log). Postgres enforces read-only and one-database-per-role, so even `health_sql`
  can't write or see another person's data.
* **Untrusted text.** Calendar titles, food names and notes are marked as data in results.

## Files

- `health_data.py`, `food_log.py`: the tool sources. `food_log.py` embeds two files from the compose repo's
  `health/` stack verbatim, and tests fail if either differs: the constitution (`food_constitution.json`) and the
  food matching rules (`healthingest/foodmatch.py`, between the `BEGIN`/`END foodmatch.py` markers).
- `export.py`: writes `../health_data.json` and `../food_log.json`. Run it after every change.
- `run_tests.sh`: runs `tests/` inside the Open WebUI image against a throwaway Postgres that `tests/seed.py`
  fills using the health stack's own schema and loaders (needs `HEALTH_STACK_DIR`, default
  `/bulk/virtualdisks/docker/compose/health`).

## Setup

See "Health data" in the compose repo's README: import both JSON files, set the valves, create the
Health Coach, Health Coach (deep) and Food Log models with Native function calling, and add the
automations.
