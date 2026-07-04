"""SpendFlow backend. Localhost single-user; state = one SQLite file + rules.json."""
import json
from contextlib import asynccontextmanager
import os
import re
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .core import (categorize, compile_rules, detect_anomalies, merchant_token,
                   parse_camt, parse_raiffeisen, suggest_rule, txn_hash)

DATA_DIR = Path(os.environ.get("SPENDFLOW_DATA", "data"))
DB_PATH = DATA_DIR / "spendflow.db"
RULES_PATH = DATA_DIR / "rules.json"
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
            source TEXT DEFAULT 'rule')""")       # source: 'rule' | 'manual'
        # Migration: add merchant column + backfill for DBs created before it existed.
        cols = {r["name"] for r in con.execute("PRAGMA table_info(txn)")}
        if "merchant" not in cols:
            con.execute("ALTER TABLE txn ADD COLUMN merchant TEXT")
        for r in con.execute("SELECT id, desc FROM txn WHERE merchant IS NULL").fetchall():
            con.execute("UPDATE txn SET merchant=? WHERE id=?",
                        (merchant_token(r["desc"]), r["id"]))
    if not RULES_PATH.exists():
        RULES_PATH.write_text("[]")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init()
    yield


app = FastAPI(title="SpendFlow", lifespan=lifespan)


def load_rules() -> list[dict]:
    return json.loads(RULES_PATH.read_text())


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


# ---------- import ----------
@app.post("/api/import/txns")
def import_txns(txns: list[TxnIn]):
    """Normalized transactions (frontend handles CSV parsing/column mapping)."""
    ins = 0
    with db() as con:
        for t in txns:
            h = txn_hash(t.date, t.amount, t.desc)
            cur = con.execute(
                "INSERT OR IGNORE INTO txn (hash, date, amount, desc, currency, merchant) "
                "VALUES (?,?,?,?,?,?)",
                (h, t.date, t.amount, t.desc, t.currency, merchant_token(t.desc)))
            ins += cur.rowcount
        apply_rules(con)
    return {"imported": ins, "duplicates": len(txns) - ins}


@app.post("/api/import/camt")
def import_camt(body: dict):
    try:
        parsed = parse_camt(body["xml"])
    except (ValueError, KeyError) as e:
        raise HTTPException(422, str(e))
    return import_txns([TxnIn(**t) for t in parsed])


@app.post("/api/import/pdf")
async def import_pdf(file: UploadFile):
    import io
    import pdfplumber
    try:
        with pdfplumber.open(io.BytesIO(await file.read())) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        parsed = parse_raiffeisen(text)
    except Exception as e:  # corrupt PDF (pdfminer errors) or wrong format (ValueError)
        raise HTTPException(422, f"Could not parse PDF: {e}")
    return import_txns([TxnIn(**t) for t in parsed])


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
            SELECT merchant, MIN(desc) desc, COUNT(*) n, SUM(ABS(amount)) total
            FROM txn WHERE cat='Uncategorized' AND {sign} AND merchant IS NOT NULL
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
            FROM txn WHERE date IS NOT NULL
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


@app.get("/api/rules")
def get_rules():
    return load_rules()


@app.put("/api/rules")
def put_rules(body: Rules):
    for r in body.rules:
        if "match" not in r or "cat" not in r:
            raise HTTPException(422, "Each rule needs 'match' and 'cat'")
    RULES_PATH.write_text(json.dumps(body.rules, indent=1, ensure_ascii=False))
    with db() as con:
        applied = apply_rules(con)
    return {"ok": True, "reapplied": applied}


# ---------- frontend ----------
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
