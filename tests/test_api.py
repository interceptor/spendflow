import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Fresh app instance with isolated data dir per test."""
    monkeypatch.setenv("SPENDFLOW_DATA", str(tmp_path))
    import importlib
    from spendflow import app as app_mod
    importlib.reload(app_mod)
    with TestClient(app_mod.app) as c:
        yield c


TXNS = [
    {"date": "2026-06-01", "desc": "MIGROS ZUERICH", "amount": -54.20},
    {"date": "2026-06-02", "desc": "SBB EasyRide", "amount": -8.80},
    {"date": "2026-06-25", "desc": "ACME AG LOHN", "amount": 6500.00},
    {"date": "2026-07-01", "desc": "MIGROS ZUERICH", "amount": -31.15},
]


def test_import_and_dedup(client):
    r = client.post("/api/import/txns", json=TXNS)
    assert r.json() == {"imported": 4, "duplicates": 0}
    r = client.post("/api/import/txns", json=TXNS)  # overlapping re-import
    assert r.json() == {"imported": 0, "duplicates": 4}
    assert len(client.get("/api/txns").json()) == 4
    assert len(client.get("/api/txns", params={"month": "2026-06"}).json()) == 3


def test_guided_assign_rule(client):
    client.post("/api/import/txns", json=TXNS)
    assert len(client.get("/api/uncategorized").json()) == 2  # migros grouped, sbb

    r = client.post("/api/assign", json={"match": "migros", "cat": "Groceries"})
    assert r.status_code == 200
    txns = client.get("/api/txns").json()
    assert {t["cat"] for t in txns if "MIGROS" in t["desc"]} == {"Groceries"}
    assert len(client.get("/api/uncategorized").json()) == 1
    assert client.get("/api/rules").json() == [
        {"match": "migros", "cat": "Groceries", "sub": None}]


def test_manual_tag_survives_rule_reapply(client):
    client.post("/api/import/txns", json=TXNS)
    tid = next(t["id"] for t in client.get("/api/txns").json()
               if t["desc"] == "SBB EasyRide")
    client.post("/api/assign", json={"ids": [tid], "cat": "Travel", "sub": "Holiday"})
    # a later rule matching the same desc must NOT override the manual tag
    client.post("/api/assign", json={"match": "sbb", "cat": "Transport"})
    t = next(t for t in client.get("/api/txns").json() if t["id"] == tid)
    assert (t["cat"], t["sub"], t["source"]) == ("Travel", "Holiday", "manual")


def test_uncategorized_carries_merchant_and_suggestion(client):
    client.post("/api/import/txns", json=TXNS)
    groups = client.get("/api/uncategorized").json()
    migros = next(g for g in groups if "MIGROS" in g["merchant"])
    assert migros["n"] == 2                       # both MIGROS rows grouped by merchant
    assert migros["suggest"]["cat"] == "Groceries"  # seed guess
    assert migros["suggest"]["source"] == "seed"


def test_suggestion_learns_from_prior_tag(client):
    # Two different merchants that share no seed category; tag one manually,
    # then a second txn of the SAME merchant must inherit it as a 'learned' hint.
    client.post("/api/import/txns", json=[
        {"date": "2026-06-01", "desc": "Zahlung Bob's Widgets | 8004 Zürich", "amount": -10},
        {"date": "2026-06-09", "desc": "Zahlung Bob's Widgets | 8004 Zürich", "amount": -20},
    ])
    txns = client.get("/api/txns").json()
    assert txns[0]["merchant"] == txns[1]["merchant"] == "Bob's Widgets"
    client.post("/api/assign", json={"ids": [txns[0]["id"]], "cat": "Hobbies"})
    grp = next(g for g in client.get("/api/uncategorized").json()
               if g["merchant"] == "Bob's Widgets")
    assert grp["n"] == 1                                   # the untagged one remains
    assert (grp["suggest"]["cat"], grp["suggest"]["source"]) == ("Hobbies", "learned")


def test_migration_backfills_merchant(client, tmp_path):
    # Simulate a pre-merchant DB: insert a row without the column populated,
    # reload the app (runs init/migration), and confirm the token is backfilled.
    import sqlite3, importlib
    from spendflow import app as app_mod
    con = sqlite3.connect(tmp_path / "spendflow.db")
    con.execute("UPDATE txn SET merchant=NULL")   # table exists from fixture startup
    con.execute("INSERT INTO txn (hash, amount, desc, merchant) VALUES ('h1', -5, "
                "'Einkauf Coop Pronto | 01.01.2026, Debit', NULL)")
    con.commit(); con.close()
    importlib.reload(app_mod)
    with TestClient(app_mod.app) as c:
        row = next(t for t in c.get("/api/txns").json() if t["hash"] == "h1")
        assert row["merchant"] == "Coop Pronto"


def test_uncategorized_income_kind(client):
    client.post("/api/import/txns", json=TXNS)  # includes ACME AG LOHN income
    inc = client.get("/api/uncategorized", params={"kind": "income"}).json()
    exp = client.get("/api/uncategorized", params={"kind": "expense"}).json()
    assert any("ACME" in g["merchant"] for g in inc)      # income appears under income
    assert all(g["total"] > 0 for g in inc)
    assert not any("ACME" in g["merchant"] for g in exp)  # and not under expenses


def test_rule_matches_despite_stripped_interior_word(client):
    # regression: 'Merchant' is stripped from the token; the auto-suggested rule
    # must still match and clear the group.
    client.post("/api/import/txns", json=[
        {"date": "2026-06-01", "desc": "Einkauf Selecta Merchant ven | 01.06.2026, Debit", "amount": -2}])
    g = client.get("/api/uncategorized").json()[0]
    assert g["merchant"] == "Selecta ven"
    client.post("/api/assign", json=g["suggest"])         # accept the suggestion as a rule
    assert client.get("/api/uncategorized").json() == []  # group is gone


def test_assign_uncategorized_untags_not_pins(client):
    # assigning 'Uncategorized' must not create a manual pin immune to rules
    client.post("/api/import/txns", json=TXNS)
    tid = next(t["id"] for t in client.get("/api/txns").json() if "MIGROS" in t["desc"])
    client.post("/api/assign", json={"ids": [tid], "cat": "Groceries"})   # manual pin
    client.post("/api/assign", json={"ids": [tid], "cat": "Uncategorized"})  # untag
    t = next(t for t in client.get("/api/txns").json() if t["id"] == tid)
    assert (t["cat"], t["source"]) == ("Uncategorized", "rule")  # rule-controlled again
    # a later rule can now reclassify it
    client.post("/api/assign", json={"match": "migros", "cat": "Food"})
    t = next(t for t in client.get("/api/txns").json() if t["id"] == tid)
    assert t["cat"] == "Food"


CC_TEXT = """Datum Valuta Details Währung Betrag Betrag in CHF
27.03.26 13.03.26 Ihre Zahlung - Danke 100.00-
10.03.26 13.03.26 PAYPAL *TEMU, 123 IE CHF 60.00 60.00
Warenhäuser
13.03.26 16.03.26 Lidl Delemont, CH 40.00
Supermärkte, Lebensmittel
Total Karte Visa Gold 4763 14XX XXXX 0730 100.00
"""


def test_cc_reconciles_against_bank_debit(client):
    from spendflow import app as app_mod
    # a bank debit that paid the Viseca bill (100.00) already exists
    client.post("/api/import/txns", json=[
        {"date": "2026-04-27", "desc": "Zahlung Viseca Payment Services AG", "amount": -100.00},
        {"date": "2026-04-01", "desc": "Einkauf Coop", "amount": -25.00}])
    res = app_mod._import_cc(CC_TEXT)
    assert res["reconciled"] is True and res["imported"] == 2

    txns = client.get("/api/txns").json()
    parent = next(t for t in txns if "Viseca" in t["desc"])
    kids = [t for t in txns if t["parent_id"] == parent["id"]]
    assert parent["reconciled"] == 1
    assert len(kids) == 2
    assert round(sum(t["amount"] for t in kids), 2) == parent["amount"]  # no double-count

    # reconciled parent is excluded from monthly stats; children (dated to when the
    # purchases happened, March) are counted in their own month instead.
    stats = client.get("/api/stats/monthly").json()
    apr = sum(r["spent"] for r in stats if r["month"] == "2026-04")
    mar = sum(r["spent"] for r in stats if r["month"] == "2026-03")
    assert round(apr, 2) == 25.00   # only Coop; the 100 Viseca lump-sum is excluded
    assert round(mar, 2) == 100.00  # the CC line items land in March, not doubled


def test_uncategorized_flags_credit_card(client):
    from spendflow import app as app_mod
    client.post("/api/import/txns", json=[
        {"date": "2026-04-27", "desc": "Zahlung Viseca Payment Services AG", "amount": -100.00},
        {"date": "2026-04-01", "desc": "Einkauf Coop Pronto", "amount": -25.00}])
    app_mod._import_cc(CC_TEXT)   # imports TEMU + Lidl as CC children
    groups = {g["merchant"]: g for g in client.get("/api/uncategorized").json()}
    assert any(g["is_cc"] for m, g in groups.items() if "TEMU" in m)   # CC item flagged
    coop = next(g for m, g in groups.items() if "Coop" in m)
    assert not coop["is_cc"]                                            # bank item not flagged


def test_anomalies_endpoint(client):
    client.post("/api/import/txns", json=TXNS)
    # tag MIGROS as 'taxes' and SBB as 'Taxes' -> a duplicate-name finding
    ids = {t["desc"]: t["id"] for t in client.get("/api/txns").json()}
    client.post("/api/assign", json={"ids": [ids["MIGROS ZUERICH"]], "cat": "taxes"})
    client.post("/api/assign", json={"ids": [ids["SBB EasyRide"]], "cat": "Taxes"})
    findings = client.get("/api/anomalies").json()
    assert any(f["type"] == "duplicate" for f in findings)


def test_assign_validates(client):
    assert client.post("/api/assign", json={"cat": "X"}).status_code == 422
    assert client.post("/api/assign",
                       json={"cat": "X", "match": "([bad"}).status_code == 422


def test_put_rules_reapplies(client):
    client.post("/api/import/txns", json=TXNS)
    r = client.put("/api/rules", json={"rules": [
        {"match": "migros|coop", "cat": "Groceries"},
        {"match": "lohn", "cat": "Salary"}]})
    assert r.json()["ok"]
    cats = {t["desc"]: t["cat"] for t in client.get("/api/txns").json()}
    assert cats["ACME AG LOHN"] == "Salary"
    assert cats["SBB EasyRide"] == "Uncategorized"


def test_stats_monthly(client):
    client.post("/api/import/txns", json=TXNS)
    client.put("/api/rules", json={"rules": [{"match": "migros", "cat": "Groceries"}]})
    stats = client.get("/api/stats/monthly").json()
    june_groc = next(s for s in stats if s["month"] == "2026-06" and s["cat"] == "Groceries")
    assert june_groc["spent"] == pytest.approx(54.20)
    july_groc = next(s for s in stats if s["month"] == "2026-07" and s["cat"] == "Groceries")
    assert july_groc["spent"] == pytest.approx(31.15)


def test_import_camt(client):
    xml = """<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08">
      <BkToCstmrStmt><Stmt><Ntry>
        <Amt Ccy="CHF">10.00</Amt><CdtDbtInd>DBIT</CdtDbtInd>
        <BookgDt><Dt>2026-06-05</Dt></BookgDt>
        <NtryDtls><TxDtls><RmtInf><Ustrd>COOP PRONTO</Ustrd></RmtInf></TxDtls></NtryDtls>
      </Ntry></Stmt></BkToCstmrStmt></Document>"""
    r = client.post("/api/import/camt", json={"xml": xml})
    assert r.json()["imported"] == 1
    assert client.post("/api/import/camt", json={"xml": "<html/>"}).status_code == 422


def test_import_pdf(client, tmp_path):
    """Round-trip a minimal statement through the PDF endpoint."""
    import pdfplumber  # ensure dep present
    from fpdf import FPDF  # tiny generator, test-only
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=9)
    for line in ["30.04.26 Saldovortrag 100.00",
                 "01.05.26 Einkauf Testshop 25.50 74.50",
                 "Umsatz 25.50 0.00"]:
        pdf.cell(0, 5, line, new_x="LMARGIN", new_y="NEXT")
    path = tmp_path / "stmt.pdf"
    pdf.output(str(path))
    r = client.post("/api/import/pdf", files={"file": ("stmt.pdf", path.read_bytes(), "application/pdf")})
    assert r.json()["imported"] == 1
    t = client.get("/api/txns").json()[0]
    assert (t["desc"], t["amount"]) == ("Einkauf Testshop", -25.50)


def test_import_pdf_rejects_garbage(client):
    r = client.post("/api/import/pdf", files={"file": ("x.pdf", b"%PDF-1.4 not really", "application/pdf")})
    assert r.status_code == 422
