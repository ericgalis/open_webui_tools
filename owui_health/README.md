# Health tools for Open WebUI

A tool for the personal health stack (`health/` in the home server's compose repo, which owns the database,
the nightly ingest and the setup instructions):

| Tool | Import file | What it does |
|---|---|---|
| Health Data | `../health_data.json` | Read-only queries and **deterministic statistics** over any date range: `health_report` (day/week/month against the user's own baseline, targets, gaps), `health_summary`, `health_trend`, `health_compare`, `health_baseline`, `health_correlate`, record lists (activities, food, calendar), `health_profile`, `health_sources`, and a guarded `health_sql`. |

```
 Open WebUI                                      health stack
 ┌─────────────────────────────┐  health net   ┌──────────────────────────────────────┐
 │ Health Data tool            │──────────────▶│ health-db: health_<key> per person   │
 │  valve PERSON_DSNS:         │ as <key>_ro   │  <key>_ro: SELECT, read-only, own DB │
 │  user id -> read-only DSN   │               └──────────────────────────────────────┘
 └─────────────────────────────┘
```

* **The model never computes numbers.** Every statistic is calculated by the tool from the stored daily rows
  (`api.daily`), with the method stated in the result. Same input, same output.
* **Per-user isolation.** The valves map each Open WebUI user id to that person's own database role (Health
  Data). Postgres enforces read-only and one-database-per-role, so even `health_sql`
  can't write or see another person's data.
* **Untrusted text.** Calendar titles and food names are marked as data in results.

## Files

- `health_data.py`: the tool source.
- `export.py`: writes `../health_data.json`. Run it after every change.
- `run_tests.sh`: runs `tests/` inside the Open WebUI image against a throwaway Postgres that `tests/seed.py`
  fills using the health stack's own schema and loaders (needs `HEALTH_STACK_DIR`, default
  `/bulk/virtualdisks/docker/compose/health`).

## Setup

See "Health data" in the compose repo's README: import the JSON file, set the valves, create the
Health Coach and Health Coach (deep) models with Native function calling, and add the
automations.
