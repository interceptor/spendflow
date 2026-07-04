# SpendFlow

Private, local spending analysis from bank statements. FastAPI + SQLite backend,
single-page frontend with Sankey + monthly trend charts. No data leaves your machine.

## Run

    pip install -r requirements.txt
    uvicorn spendflow.app:app --port 8321

Open http://localhost:8321 and drop a statement (.csv or camt.053 .xml).

## Data

- `data/spendflow.db` — all transactions (SQLite). Re-imports are deduplicated
  via sha1(date|amount|desc), so overlapping statement exports are safe.
- `data/rules.json` — categorization rules `[{"match": "...", "cat": "...", "sub": null}]`.
  Editable in the UI or by hand; git-track it if you like.

Set `SPENDFLOW_DATA=/path/to/dir` to relocate both.

## Categorization

Rules are case-insensitive regexes matched against the description. Manual tags
(`source='manual'`) are never overwritten by rule re-application.

## Tests

    pip install -r requirements-dev.txt
    python -m pytest

## Docker

    docker compose up -d --build

Then open http://<pc-ip>:8321 from any device on your LAN.
Data persists in ./data (bind mount) - back up / git-ignore as you see fit.
