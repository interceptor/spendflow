"""SpendFlow backend. Localhost single-user; state = one SQLite file + rules.json."""
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import os
import re
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .core import (categorize, compile_rules, detect_anomalies, detect_recurring,
                   merchant_token, norm_name, parse_camt, parse_raiffeisen,
                   parse_viseca, suggest_rule, txn_hash)

DATA_DIR = Path(os.environ.get("SPENDFLOW_DATA", "data"))
DB_PATH = DATA_DIR / "spendflow.db"
RULES_PATH = DATA_DIR / "rules.json"
BUDGETS_PATH = DATA_DIR / "budgets.json"
RECUR_PATH = DATA_DIR / "recurring.json"
STATIC = Path(__file__).parent.parent / "static"

def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS txn (
            id INTEGER PRIMARY KEY,
            hash TEXT UNIQUE NOT NULL,
            date TEXT, amount REAL NOT NULL, desc TEXT NOT NULL,
            currency TEXT DEFAULT 'CHF',
            cat TEXT DEFAULT 'Uncategorized', sub TEXT,
            merchant TEXT,                        -- tokenized at import; stable grouping key
            reconciled INTEGER DEFAULT 0,         -- 1 = lump-sum replaced by itemized children
            parent_id INTEGER,                    -- CC line item -> its bank debit
            source TEXT DEFAULT 'rule')""")       # source: 'rule' | 'manual'
        # One row per imported statement, so the Statements view can show coverage
        # and spot gaps. Kind is 'bank' | 'card'; period is derived from the txns.
        con.execute("""CREATE TABLE IF NOT EXISTS import (
            id INTEGER PRIMARY KEY,
            imported_at TEXT NOT NULL,            -- ISO timestamp of the import
            kind TEXT NOT NULL,                   -- 'bank' | 'card'
            filename TEXT,
            fmt TEXT,                             -- 'pdf' | 'camt' | 'csv'
            period_from TEXT, period_to TEXT,     -- min/max txn date in the file
            n_txns INTEGER DEFAULT 0,             -- rows actually inserted
            n_dupes INTEGER DEFAULT 0,            -- rows skipped as already present
            total REAL)""")                       # card: stated 'Total Karte'
        # Migrations: add columns + backfill for DBs created before they existed.
        cols = {r["name"] for r in con.execute("PRAGMA table_info(txn)")}
        if "merchant" not in cols:
            con.execute("ALTER TABLE txn ADD COLUMN merchant TEXT")
        if "reconciled" not in cols:
            con.execute("ALTER TABLE txn ADD COLUMN reconciled INTEGER DEFAULT 0")
        if "parent_id" not in cols:
            con.execute("ALTER TABLE txn ADD COLUMN parent_id INTEGER")
        if "note" not in cols:
            # free-text note/tag per transaction; never touched by rule re-application
            con.execute("ALTER TABLE txn ADD COLUMN note TEXT")
        for r in con.execute("SELECT id, desc FROM txn WHERE merchant IS NULL").fetchall():
            con.execute("UPDATE txn SET merchant=? WHERE id=?",
                        (merchant_token(r["desc"]), r["id"]))
        # Category names are canonically lowercase. Fold any legacy mixed-case rows
        # in place; variants that differ only by case merge into one name, which is
        # the point. Idempotent, so it is safe to run on every startup.
        for r in con.execute("SELECT DISTINCT cat FROM txn WHERE cat IS NOT NULL").fetchall():
            if (n := norm_name(r["cat"])) and n != r["cat"]:
                con.execute("UPDATE txn SET cat=? WHERE cat=?", (n, r["cat"]))
        for r in con.execute("SELECT DISTINCT sub FROM txn WHERE sub IS NOT NULL").fetchall():
            if (n := norm_name(r["sub"])) and n != r["sub"]:
                con.execute("UPDATE txn SET sub=? WHERE sub=?", (n, r["sub"]))
    if not RULES_PATH.exists():
        RULES_PATH.write_text("[]")
    else:  # keep rules.json in the same canonical form as the transactions
        rules = json.loads(RULES_PATH.read_text())
        fixed = [{**r, "cat": norm_name(r.get("cat")) or "Uncategorized",
                  **({"sub": norm_name(r["sub"])} if r.get("sub") else {})}
                 for r in rules]
        if fixed != rules:
            RULES_PATH.write_text(json.dumps(fixed, indent=1, ensure_ascii=False))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init()
    yield


app = FastAPI(title="SpendFlow", lifespan=lifespan)


def load_rules() -> list[dict]:
    return json.loads(RULES_PATH.read_text())


def record_import(con: sqlite3.Connection, kind: str, rows: list[dict], *,
                  filename: str | None = None, fmt: str | None = None,
                  n_txns: int = 0, n_dupes: int = 0, total: float | None = None) -> None:
    """Log one imported statement. `rows` are the parsed transactions, used only to
    derive the period covered — the coverage view reasons about periods, not files."""
    dates = sorted(d for d in (r.get("date") for r in rows) if d)
    con.execute(
        "INSERT INTO import (imported_at, kind, filename, fmt, period_from, "
        "period_to, n_txns, n_dupes, total) VALUES (?,?,?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), kind, filename,
         fmt, dates[0] if dates else None, dates[-1] if dates else None,
         n_txns, n_dupes, total))


def apply_rules(con: sqlite3.Connection) -> int:
    """Re-categorize everything except manual tags. Returns affected row count."""
    compiled = compile_rules(load_rules())
    rows = con.execute("SELECT id, desc FROM txn WHERE source != 'manual'").fetchall()
    updates = [(*categorize(r["desc"], compiled), r["id"]) for r in rows]
    con.executemany("UPDATE txn SET cat=?, sub=? WHERE id=?", updates)
    return len(updates)


# ---------- models ----------
class TxnIn(BaseModel):
    date: str | None = None
    desc: str
    amount: float
    currency: str = "CHF"


class Assign(BaseModel):
    cat: str
    sub: str | None = None
    match: str | None = None      # regex -> saved as rule, applied globally
    ids: list[int] | None = None  # explicit ids -> manual tag, immune to rules


class Rules(BaseModel):
    rules: list[dict]


class Rename(BaseModel):
    level: str          # 'cat' | 'sub' — which column to rewrite
    old: str
    new: str
    sub_of: str | None = None  # limit a sub rename to one parent category (optional)


class Note(BaseModel):
    id: int
    note: str | None = None   # empty/blank clears


class RecurOverride(BaseModel):
    merchant: str              # the detected merchant token this override applies to
    label: str | None = None   # friendly display name ('Rent'); blank clears
    ignore: bool = False       # True = not actually recurring, hide from the card


class Merge(BaseModel):
    sources: list[str]         # categories to absorb
    target: str                # category they all become
    as_sub: bool = False       # keep each source's name as a subcategory


# ---------- import ----------
@app.post("/api/import/txns")
def import_txns(txns: list[TxnIn], *, log_as: str | None = "csv",
                filename: str | None = None):
    """Normalized transactions (frontend handles CSV parsing/column mapping).

    `log_as` names the format recorded in the import log; callers that log the
    import themselves (camt, pdf) pass None to avoid a duplicate entry."""
    ins = 0
    with db() as con:
        for t in txns:
            h = txn_hash(t.date, t.amount, t.desc)
            cur = con.execute(
                "INSERT OR IGNORE INTO txn (hash, date, amount, desc, currency, merchant) "
                "VALUES (?,?,?,?,?,?)",
                (h, t.date, t.amount, t.desc, t.currency, merchant_token(t.desc)))
            ins += cur.rowcount
        if log_as:
            record_import(con, "bank", [t.model_dump() for t in txns], filename=filename,
                          fmt=log_as, n_txns=ins, n_dupes=len(txns) - ins)
        apply_rules(con)
    return {"imported": ins, "duplicates": len(txns) - ins}


@app.post("/api/import/camt")
def import_camt(body: dict):
    try:
        parsed = parse_camt(body["xml"])
    except (ValueError, KeyError) as e:
        raise HTTPException(422, str(e))
    res = import_txns([TxnIn(**t) for t in parsed], log_as=None)
    with db() as con:
        record_import(con, "bank", parsed, filename=body.get("filename"), fmt="camt",
                      n_txns=res["imported"], n_dupes=res["duplicates"])
    return res


@app.post("/api/import/pdf")
async def import_pdf(file: UploadFile):
    """Auto-detect Raiffeisen PDF type: a Viseca credit-card statement is
    reconciled against its bank debit; an account Kontoauszug imports normally."""
    import io
    import pdfplumber
    try:
        with pdfplumber.open(io.BytesIO(await file.read())) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception as e:
        raise HTTPException(422, f"Could not read PDF: {e}")
    if "Viseca" in text or "Kartenkontonummer" in text:
        return _import_cc(text, filename=file.filename)
    try:
        parsed = parse_raiffeisen(text)
    except Exception as e:  # corrupt PDF (pdfminer errors) or wrong format (ValueError)
        raise HTTPException(422, f"Could not parse PDF: {e}")
    res = import_txns([TxnIn(**t) for t in parsed], log_as=None)
    with db() as con:
        record_import(con, "bank", parsed, filename=file.filename, fmt="pdf",
                      n_txns=res["imported"], n_dupes=res["duplicates"])
    return res


def _import_cc(text: str, filename: str | None = None) -> dict:
    """Viseca credit-card statement: import line items, then reconcile against the
    single lump-sum bank debit to Viseca (matched by amount) so the bill isn't
    double-counted. The bank debit is flagged `reconciled` (excluded from spend);
    its itemized children carry `parent_id` and flow through the rule set."""
    try:
        items, total = parse_viseca(text)
    except Exception as e:
        raise HTTPException(422, f"Could not parse credit-card statement: {e}")

    ins = 0
    with db() as con:
        # Find the bank debit that paid this bill: a not-yet-reconciled expense whose
        # magnitude equals the statement total. Prefer a Viseca-named row.
        parent_id = None
        if total is not None:
            cand = con.execute(
                "SELECT id, desc FROM txn WHERE reconciled=0 AND parent_id IS NULL "
                "AND amount < 0 AND ABS(ABS(amount) - ?) < 0.05 "
                "ORDER BY (desc LIKE '%Viseca%') DESC, ABS(ABS(amount) - ?) LIMIT 1",
                (total, total)).fetchone()
            if cand:
                parent_id = cand["id"]
                con.execute("UPDATE txn SET reconciled=1 WHERE id=?", (parent_id,))

        for t in items:
            h = txn_hash(t["date"], t["amount"], t["desc"])
            cur = con.execute(
                "INSERT OR IGNORE INTO txn (hash, date, amount, desc, currency, merchant, parent_id) "
                "VALUES (?,?,?,?,?,?,?)",
                (h, t["date"], t["amount"], t["desc"], t["currency"],
                 merchant_token(t["desc"]), parent_id))
            ins += cur.rowcount
        record_import(con, "card", items, filename=filename, fmt="pdf",
                      n_txns=ins, n_dupes=len(items) - ins, total=total)
        apply_rules(con)
    return {"imported": ins, "duplicates": len(items) - ins,
            "reconciled": parent_id is not None, "total": total}


# ---------- read ----------
@app.get("/api/txns")
def get_txns(month: str | None = None):
    q, args = "SELECT * FROM txn", ()
    if month:
        q, args = q + " WHERE date LIKE ?", (month + "%",)
    with db() as con:
        return [dict(r) for r in con.execute(q + " ORDER BY date", args)]


@app.get("/api/uncategorized")
def uncategorized(kind: str = "expense"):
    """Unmatched transactions grouped by merchant token, biggest first - feeds guided tagging.

    `kind` selects the flow side: 'expense' (amount < 0, default) or 'income'
    (amount > 0), so income streams can be tagged/ruled too. Each group carries a
    `suggest` block (regex + category guess), learned from how the same merchant
    was categorized elsewhere (manual tags preferred), falling back to a static
    seed map. All local; no data leaves the machine."""
    sign = "amount > 0" if kind == "income" else "amount < 0"
    with db() as con:
        # What each merchant has been categorized as elsewhere -> learned hint.
        # Prefer manual tags (source='manual') over rule-derived; most-frequent wins.
        learned: dict[str, tuple[str, str | None]] = {}
        seen = con.execute("""
            SELECT merchant, cat, sub, source, COUNT(*) n
            FROM txn WHERE cat != 'Uncategorized' AND merchant IS NOT NULL
            GROUP BY merchant, cat, sub
            ORDER BY (source='manual') DESC, n DESC""").fetchall()
        for r in seen:
            learned.setdefault(r["merchant"], (r["cat"], r["sub"]))

        rows = con.execute(f"""
            SELECT merchant, MIN(desc) desc, COUNT(*) n, SUM(ABS(amount)) total,
                   MAX(parent_id IS NOT NULL) is_cc   -- came from a credit-card statement
            FROM txn WHERE cat='Uncategorized' AND {sign} AND merchant IS NOT NULL
                  AND reconciled=0
            GROUP BY merchant ORDER BY total DESC LIMIT 200""").fetchall()
    return [dict(r, suggest=suggest_rule(r["desc"], learned.get(r["merchant"])))
            for r in rows]


@app.get("/api/stats/monthly")
def stats_monthly():
    """Per-month, per-category sums: long-term trends straight from SQL."""
    with db() as con:
        rows = con.execute("""
            SELECT strftime('%Y-%m', date) month, cat,
                   SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END) spent,
                   SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) received
            FROM txn WHERE date IS NOT NULL AND reconciled=0
            GROUP BY month, cat ORDER BY month""").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/categories")
def categories():
    with db() as con:
        rows = con.execute(
            "SELECT DISTINCT cat, sub FROM txn WHERE cat != 'Uncategorized' ORDER BY cat, sub")
        cats: dict[str, list] = {}
        for r in rows:
            cats.setdefault(r["cat"], [])
            if r["sub"]:
                cats[r["cat"]].append(r["sub"])
    for r in load_rules():  # rules may define categories with no matching txns yet
        cats.setdefault(r["cat"], [])
        if r.get("sub") and r["sub"] not in cats[r["cat"]]:
            cats[r["cat"]].append(r["sub"])
    return cats


@app.get("/api/categories/overview")
def categories_overview():
    """Every category with usage stats, for the Categories management view:
    transaction count, total spend/income, date span, subcategories, and how many
    rules point at it. Ordered by transaction count so the long tail sinks."""
    with db() as con:
        rows = con.execute("""
            SELECT cat,
                   COUNT(*)                                    AS n,
                   SUM(CASE WHEN amount < 0 THEN -amount END)  AS spent,
                   SUM(CASE WHEN amount > 0 THEN amount END)   AS income,
                   MIN(date) AS first, MAX(date) AS last,
                   SUM(CASE WHEN source = 'manual' THEN 1 END) AS manual
            FROM txn GROUP BY cat""").fetchall()
        subs: dict[str, list[str]] = {}
        for r in con.execute(
                "SELECT DISTINCT cat, sub FROM txn WHERE sub IS NOT NULL ORDER BY sub"):
            subs.setdefault(r["cat"], []).append(r["sub"])

    rule_counts: dict[str, int] = {}
    for rule in load_rules():
        rule_counts[rule["cat"]] = rule_counts.get(rule["cat"], 0) + 1

    out = []
    for r in rows:
        out.append({"cat": r["cat"], "n": r["n"],
                    "spent": round(r["spent"] or 0, 2),
                    "income": round(r["income"] or 0, 2),
                    "first": r["first"], "last": r["last"],
                    "manual": r["manual"] or 0,
                    "subs": subs.get(r["cat"], []),
                    "rules": rule_counts.get(r["cat"], 0)})
    # categories that exist only as rules (no transactions yet)
    for cat, n in rule_counts.items():
        if not any(o["cat"] == cat for o in out):
            out.append({"cat": cat, "n": 0, "spent": 0, "income": 0, "first": None,
                        "last": None, "manual": 0, "subs": [], "rules": n})
    out.sort(key=lambda o: (-o["n"], o["cat"]))
    return out


@app.get("/api/statements")
def statements():
    """Every statement that should exist, present or not, as one sortable table.

    A 'document' is one statement period. Three sources are folded into a single
    list so the UI can render them uniformly:

    - logged     an entry in the import table (recorded from the import itself)
    - inferred   reconstructed from the transactions for imports predating the log
    - missing    known to exist but never imported

    'card' documents are exact: every bank debit to Viseca is a credit-card bill,
    so an unreconciled debit proves its itemized statement is absent and that spend
    is sitting in the data as one opaque lump sum.

    'bank' documents are per-month, inferred from the transactions themselves. A
    month with no bank rows inside the otherwise-covered range means a missing
    account statement. Bounded by the observed range, so it never claims months you
    simply haven't banked yet.
    """
    with db() as con:
        imports = [dict(r) for r in con.execute(
            "SELECT * FROM import ORDER BY imported_at DESC, id DESC")]

        cards = [dict(r) for r in con.execute(
            "SELECT id, date, amount, reconciled FROM txn "
            "WHERE desc LIKE '%Viseca%' AND amount < 0 ORDER BY date")]
        # itemized children per bill, to show what a reconciled statement contained
        kids = {r["parent_id"]: r["n"] for r in con.execute(
            "SELECT parent_id, COUNT(*) AS n FROM txn "
            "WHERE parent_id IS NOT NULL GROUP BY parent_id")}

        months = {r["m"]: (r["n"], r["lo"], r["hi"]) for r in con.execute(
            "SELECT substr(date,1,7) AS m, COUNT(*) AS n, MIN(date) AS lo, "
            "MAX(date) AS hi FROM txn WHERE parent_id IS NULL AND date IS NOT NULL "
            "GROUP BY m")}
        seen = {r["m"] for r in con.execute(
            "SELECT DISTINCT substr(date,1,7) AS m FROM txn WHERE date IS NOT NULL")}

    # Logged imports, indexed by the period they covered, so an inferred row can
    # defer to a real log entry describing the same statement.
    logged_bank = {i["period_from"][:7]: i for i in imports
                   if i["kind"] == "bank" and i["period_from"]}
    logged_card = [i for i in imports if i["kind"] == "card"]

    docs: list[dict] = []

    # --- one document per credit-card bill
    for c in cards:
        got = bool(c["reconciled"])
        # match a logged card import by its stated total (the bill amount)
        log = next((i for i in logged_card
                    if i["total"] is not None
                    and abs(i["total"] - abs(c["amount"])) < 0.05), None)
        docs.append({
            "kind": "card", "period": c["date"], "label": c["date"],
            "from": c["date"], "to": c["date"],
            "amount": round(abs(c["amount"]), 2),
            "n_txns": kids.get(c["id"], 0),
            "status": "imported" if got else "missing",
            "source": "logged" if log else ("inferred" if got else None),
            "filename": log["filename"] if log else None,
            "imported_at": log["imported_at"] if log else None,
        })

    # --- one document per month of bank activity
    if seen:
        lo, hi = min(seen), max(seen)
        y, m = int(lo[:4]), int(lo[5:7])
        while (key := f"{y:04d}-{m:02d}") <= hi:
            got = key in months
            log = logged_bank.get(key)
            n, first, last = months.get(key, (0, None, None))
            docs.append({
                "kind": "bank", "period": key, "label": key,
                "from": first, "to": last,
                "amount": None,
                "n_txns": n,
                "status": "imported" if got else "missing",
                "source": "logged" if log else ("inferred" if got else None),
                "filename": log["filename"] if log else None,
                "imported_at": log["imported_at"] if log else None,
            })
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    docs.sort(key=lambda d: (d["period"], d["kind"]))

    missing_cards = [d for d in docs if d["kind"] == "card" and d["status"] == "missing"]
    missing_months = [d["period"] for d in docs
                      if d["kind"] == "bank" and d["status"] == "missing"]
    return {
        "docs": docs,
        "imports": imports,
        # kept for the summary banners; docs[] is the table's source of truth
        "cards": [{"date": d["period"], "amount": -d["amount"],
                   "imported": d["status"] == "imported"}
                  for d in docs if d["kind"] == "card"],
        "bank": [{"month": d["period"], "n": d["n_txns"],
                  "imported": d["status"] == "imported"}
                 for d in docs if d["kind"] == "bank"],
        "missing": {
            "cards": [{"date": d["period"], "amount": -d["amount"]} for d in missing_cards],
            "card_total": round(sum(d["amount"] for d in missing_cards), 2),
            "months": missing_months,
        },
    }


def _recur_overrides() -> dict:
    return json.loads(RECUR_PATH.read_text()) if RECUR_PATH.exists() else {}


@app.get("/api/recurring")
def recurring():
    """Recurring expenses (subscriptions, standing orders) with price-change and
    stopped flags. Reconciled lump-sums are excluded so a credit-card bill and its
    itemized line items are never both counted.

    Detection is heuristic, so user overrides from recurring.json are merged in:
    'label' gives a row a friendly display name ('Rent'), 'ignore' marks a false
    positive (a cash-withdrawal habit is regular, but not a subscription). Ignored
    rows are still returned, flagged — the UI hides them but can offer a restore."""
    with db() as con:
        txns = [dict(r) for r in con.execute(
            "SELECT merchant, date, amount FROM txn WHERE reconciled=0")]
    rows = detect_recurring(txns)
    ov = _recur_overrides()
    for r in rows:
        o = ov.get(r["merchant"], {})
        r["label"] = o.get("label")
        r["ignored"] = bool(o.get("ignore"))
    return rows


@app.post("/api/recurring/override")
def recur_override(o: RecurOverride):
    """Set or clear the override for one merchant. An empty override (no label,
    not ignored) removes the entry entirely, returning the row to pure detection."""
    ov = _recur_overrides()
    entry = {}
    if o.label and o.label.strip():
        entry["label"] = o.label.strip()
    if o.ignore:
        entry["ignore"] = True
    if entry:
        ov[o.merchant] = entry
    else:
        ov.pop(o.merchant, None)
    RECUR_PATH.write_text(json.dumps(ov, indent=1, ensure_ascii=False))
    return {"ok": True, "override": entry or None}


@app.get("/api/budgets")
def get_budgets():
    """{category: monthly_limit}. Stored beside the rules, editable by hand."""
    if BUDGETS_PATH.exists():
        return json.loads(BUDGETS_PATH.read_text())
    return {}


@app.put("/api/budgets")
def put_budgets(body: dict):
    """Replace the budget map. Keys are normalized like every other category name;
    zero/empty values delete the budget for that category."""
    out = {}
    for cat, v in body.items():
        n = norm_name(cat)
        if n and n != "Uncategorized" and isinstance(v, (int, float)) and v > 0:
            out[n] = round(float(v), 2)
    BUDGETS_PATH.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    return out


@app.get("/api/anomalies")
def anomalies():
    """Category-hygiene findings (near-duplicate names, name reused across levels,
    singleton categories, amount outliers) for the Review card. Read-only hints."""
    with db() as con:
        txns = [dict(r) for r in con.execute("SELECT cat, sub, amount, desc FROM txn")]
    return detect_anomalies(txns)


# ---------- categorize ----------
@app.post("/api/assign")
def assign(a: Assign):
    """Guided tagging: either save a rule (re-applied to all non-manual txns)
    or hard-tag specific ids as manual."""
    if not a.match and not a.ids:
        raise HTTPException(422, "Provide 'match' (rule) or 'ids' (manual tag)")
    a.cat = norm_name(a.cat) or "Uncategorized"   # categories are canonically lowercase
    a.sub = norm_name(a.sub)
    with db() as con:
        if a.match:
            try:
                re.compile(a.match)
            except re.error as e:
                raise HTTPException(422, f"Invalid regex: {e}")
            rules = load_rules()
            rules.append({"match": a.match, "cat": a.cat, "sub": a.sub})
            RULES_PATH.write_text(json.dumps(rules, indent=1, ensure_ascii=False))
        if a.ids:
            if a.cat == "Uncategorized":
                # Assigning 'Uncategorized' means "untag" -> back to rule control,
                # not a manual pin (which would be immune to rules forever).
                con.executemany(
                    "UPDATE txn SET cat='Uncategorized', sub=NULL, source='rule' WHERE id=?",
                    [(i,) for i in a.ids])
            else:
                con.executemany("UPDATE txn SET cat=?, sub=?, source='manual' WHERE id=?",
                                [(a.cat, a.sub, i) for i in a.ids])
        applied = apply_rules(con)
    return {"ok": True, "reapplied": applied}


@app.post("/api/note")
def set_note(n: Note):
    """Attach a free-text note to one transaction (or clear it with blank/None).
    Notes live outside the categorization machinery: rules never touch them, and
    they survive re-tagging. '#hashtags' in the text are just text — the ledger
    search finds them like any other word."""
    note = (n.note or "").strip() or None
    with db() as con:
        cur = con.execute("UPDATE txn SET note=? WHERE id=?", (note, n.id))
        if not cur.rowcount:
            raise HTTPException(404, "no such transaction")
    return {"ok": True, "note": note}


@app.get("/api/rules")
def get_rules():
    return load_rules()


@app.put("/api/rules")
def put_rules(body: Rules):
    for r in body.rules:
        if "match" not in r or "cat" not in r:
            raise HTTPException(422, "Each rule needs 'match' and 'cat'")
        r["cat"] = norm_name(r["cat"]) or "Uncategorized"
        if r.get("sub"):
            r["sub"] = norm_name(r["sub"])
    RULES_PATH.write_text(json.dumps(body.rules, indent=1, ensure_ascii=False))
    with db() as con:
        applied = apply_rules(con)
    return {"ok": True, "reapplied": applied}


@app.post("/api/category/rename")
def rename_category(r: Rename):
    """Fix category-hygiene issues from the Review card. Rewrites a name across
    both transactions and rules.json at the given level:
      - level='cat': merge/rename a category (e.g. 'taxes' -> 'Taxes').
      - level='sub': rename a subcategory, optionally only under one category
        (sub_of), to resolve a name used at both levels.
    Rules keep their regex; only the target cat/sub is rewritten."""
    if r.level not in ("cat", "sub"):
        raise HTTPException(422, "level must be 'cat' or 'sub'")
    if not r.new.strip():
        raise HTTPException(422, "new name required")
    r.new = norm_name(r.new)          # canonical lowercase; renaming to a name that
    r.old = norm_name(r.old)          # already exists is just a merge
    r.sub_of = norm_name(r.sub_of)
    changed = 0
    with db() as con:
        if r.level == "cat":
            cur = con.execute("UPDATE txn SET cat=? WHERE cat=?", (r.new, r.old))
        elif r.sub_of:
            cur = con.execute("UPDATE txn SET sub=? WHERE sub=? AND cat=?",
                              (r.new, r.old, r.sub_of))
        else:
            cur = con.execute("UPDATE txn SET sub=? WHERE sub=?", (r.new, r.old))
        changed = cur.rowcount

        rules = load_rules()
        for rule in rules:
            if r.level == "cat" and rule.get("cat") == r.old:
                rule["cat"] = r.new
            elif r.level == "sub" and rule.get("sub") == r.old \
                    and (not r.sub_of or rule.get("cat") == r.sub_of):
                rule["sub"] = r.new
        RULES_PATH.write_text(json.dumps(rules, indent=1, ensure_ascii=False))
        apply_rules(con)
    return {"ok": True, "changed": changed}


@app.post("/api/category/merge")
def merge_categories(m: Merge):
    """Fold several categories into one, in a single transaction.

    With as_sub=False the sources' transactions simply take the target category,
    keeping whatever subcategory they already had. With as_sub=True the source name
    is preserved as the subcategory ('shopping' -> Groceries/shopping), the non-lossy
    way to consolidate two vocabularies that aren't true duplicates.

    as_sub never overwrites a more specific subcategory that is already set — on the
    txn (COALESCE) or on the rule. So a rule tagged 'transfer/mortage' becomes
    'Transfers/mortage', not 'Transfers/transfer'; only rules and transactions with
    no sub of their own inherit the old category name.

    rules.json is rewritten to match, so the next import doesn't recreate the old
    names. Merging a category into itself is a no-op rather than an error.
    """
    target = norm_name(m.target) or ""
    if not target:
        raise HTTPException(422, "target category required")
    sources = [s for s in (norm_name(s) for s in m.sources) if s and s != target]
    if not sources:
        raise HTTPException(422, "no source categories to merge")
    if target == "Uncategorized":
        raise HTTPException(422, "cannot merge into 'Uncategorized'")

    changed = 0
    with db() as con:
        for src in sources:
            if m.as_sub:
                # keep the old category name as the sub, but don't clobber an
                # existing, more specific subcategory
                cur = con.execute(
                    "UPDATE txn SET cat=?, sub=COALESCE(sub, ?) WHERE cat=?",
                    (target, src, src))
            else:
                cur = con.execute("UPDATE txn SET cat=? WHERE cat=?", (target, src))
            changed += cur.rowcount

        rules = load_rules()
        for rule in rules:
            if rule.get("cat") in sources:
                if m.as_sub and not rule.get("sub"):
                    rule["sub"] = rule["cat"]
                rule["cat"] = target
        RULES_PATH.write_text(json.dumps(rules, indent=1, ensure_ascii=False))
        apply_rules(con)
    return {"ok": True, "changed": changed, "merged": sources, "target": target}


# ---------- frontend ----------
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
