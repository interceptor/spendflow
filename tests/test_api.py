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
