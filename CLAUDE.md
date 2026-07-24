# SpendFlow — project notes for Claude

Personal finance app, single user (Mike), **real financial data**. FastAPI + SQLite
backend, one vanilla-JS page (`static/index.html`, no framework, no build step).

## Layout

- `spendflow/core.py` — pure functions only: parsers (camt, Raiffeisen PDF, Viseca
  CC), categorization, `norm_name`, anomaly + recurring detection. No I/O, no state.
- `spendflow/app.py` — FastAPI routes, SQLite, file storage. Migrations run in
  `init()` on startup and must stay idempotent.
- `static/index.html` — entire frontend inline (markup + CSS + JS). Reuse the
  existing widgets: `makeCombo` (autocomplete), `showDrill` (txn modal), tab panes.
- `data/` — **production data, gitignored**: `spendflow.db`, `rules.json`,
  `budgets.json`, `recurring.json`. Never test against it (see below).

## Hard rules

- **Never run tests or experiments against `data/`.** Copy it to the session
  scratchpad and point the app there: `SPENDFLOW_DATA=<copy-dir> uvicorn
  spendflow.app:app --port 83xx`. Copy *all* json files, not just the db.
- Category/subcategory names are canonical lowercase (`core.norm_name`), enforced
  at every write path. `Uncategorized` is a capitalized reserved sentinel compared
  literally — don't normalize it away.
- Reconciled rows (`reconciled=1`) are lump-sum CC bills replaced by itemized
  children (`parent_id`); exclude them from any spend math, as every existing
  query does.
- Notes/tags: `txn.note` is user text, never touched by rule re-application.
  `#tags` in a recurring label (recurring.json) are inherited by all of that
  merchant's transactions — frontend-side (`recurTagMap`/`effTags`).

## Workflow (established with Mike)

1. Build the feature; backend tests in `tests/` (pytest, fixture isolates data
   dir via `SPENDFLOW_DATA`).
2. Verify the frontend for real: extract the inline JS, `node --check` it, then
   drive the page in **jsdom** against a test server on a data copy. The Plotly
   stub must attach `.on`/`.removeAllListeners` to the chart div, and
   `window.fetch` must absolutize relative URLs. Cross-check UI numbers against
   the API to the cent.
3. Restart the live app for Mike: port **8321**, `--host 0.0.0.0` (he opens it
   from his phone at http://192.168.1.41:8321).
4. **Do not commit until Mike has tried it and says so.** Then: long-form commit
   messages explaining why, push branch, `gh pr create`, squash-merge with
   `--delete-branch` on his go-ahead.

## Gotchas

- Plotly link clicks: `p.index` is the *link* index, not a node index — only
  link points carry `source`/`target`; test those first (fixed bug, don't regress).
- `pkill` of the uvicorn background task makes the harness report exit 144 —
  that's self-inflicted, not a failure. Verify ports with `curl`, not `pgrep`
  (pgrep matches its own shell wrapper).
- Node labels in the Sankey are NOT unique across income/expense sides; nodes are
  keyed `inccat:`/`expcat:` internally — never resolve by label.
- Test fixtures reload the app module (`importlib.reload`) to re-read
  `SPENDFLOW_DATA`; module-level `*_PATH` constants depend on it.
