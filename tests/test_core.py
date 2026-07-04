import math
import re
import pytest
from spendflow.core import (parse_amount, parse_date, txn_hash, parse_camt,
                            compile_rules, categorize,
                            merchant_token, merchant_regex, suggest_category, suggest_rule)


@pytest.mark.parametrize("raw,expected", [
    ("1'234.56", 1234.56),     # Swiss
    ("1.234,56", 1234.56),     # German
    ("1,234.56", 1234.56),     # Anglo
    ("-12.30", -12.30),
    ("12.30-", -12.30),        # trailing minus (some bank exports)
    ("(45.00)", -45.00),
    ("CHF 99.90", 99.90),
    (1500, 1500.0),
    ("1,5", 1.5),              # lone comma as decimal
    ("1,500", 1500.0),         # lone comma as thousands
])
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == pytest.approx(expected)


def test_parse_amount_garbage_is_nan():
    assert math.isnan(parse_amount("n/a"))
    assert math.isnan(parse_amount(""))


@pytest.mark.parametrize("raw,expected", [
    ("2026-01-31", "2026-01-31"),
    ("31.01.2026", "2026-01-31"),
    ("31.1.26", "2026-01-31"),
    ("31/01/2026", "2026-01-31"),
    ("2026-01-31T09:00:00", "2026-01-31"),
    ("nope", None),
])
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


def test_txn_hash_stable_and_distinct():
    a = txn_hash("2026-01-31", 12.30, " Migros ")
    assert a == txn_hash("2026-01-31", 12.3000001, "Migros")  # rounding + strip
    assert a != txn_hash("2026-01-31", 12.31, "Migros")


CAMT = """<?xml version="1.0"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08">
 <BkToCstmrStmt><Stmt>
  <Ntry>
   <Amt Ccy="CHF">54.20</Amt><CdtDbtInd>DBIT</CdtDbtInd>
   <BookgDt><Dt>2026-06-02</Dt></BookgDt>
   <NtryDtls><TxDtls><RmtInf><Ustrd>MIGROS ZUERICH</Ustrd></RmtInf></TxDtls></NtryDtls>
  </Ntry>
  <Ntry>
   <Amt Ccy="CHF">6500.00</Amt><CdtDbtInd>CRDT</CdtDbtInd>
   <BookgDt><Dt>2026-06-25</Dt></BookgDt>
   <NtryDtls><TxDtls><RltdPties><Dbtr><Nm>ACME AG LOHN</Nm></Dbtr></RltdPties></TxDtls></NtryDtls>
  </Ntry>
 </Stmt></BkToCstmrStmt>
</Document>"""


def test_parse_camt():
    txns = parse_camt(CAMT)
    assert len(txns) == 2
    assert txns[0] == {"date": "2026-06-02", "desc": "MIGROS ZUERICH",
                       "amount": -54.20, "currency": "CHF"}
    assert txns[1]["amount"] == 6500.00 and txns[1]["desc"] == "ACME AG LOHN"


def test_parse_camt_rejects_non_camt():
    with pytest.raises(ValueError):
        parse_camt("<html></html>")


def test_categorize():
    rules = compile_rules([
        {"match": "migros|coop", "cat": "Groceries"},
        {"match": "sbb", "cat": "Transport", "sub": "Public transport"},
        {"match": "([bad", "cat": "Broken"},  # invalid regex must be skipped
    ])
    assert categorize("MIGROS ZUERICH", rules) == ("Groceries", None)
    assert categorize("SBB EasyRide", rules) == ("Transport", "Public transport")
    assert categorize("Unknown Corp", rules) == ("Uncategorized", None)


# ---------- Raiffeisen PDF parser (anonymized fixture, real structure) ----------
from spendflow.core import parse_raiffeisen

RAIFFEISEN = """Kontoinhaber Max Muster
Kontoauszug 01.05.2026 - 31.05.2026
Datum Text Belastungen Gutschriften Saldo
(Valuta)
30.04.26 Saldovortrag 5'721.71
01.05.26 Einkauf Selecta Merchant ven 2.00 5'719.71
(04.05.26) 29.04.2026, 17:58, Debit Mastercard-Nr. 557452xxxxxx2383
04.05.26 Gutschrift Vermieter AG u/o 1'150.00 6'869.71
Musterweg 1, 8000 Zürich
MIETZINS WOHNUNG
15.05.26 Einkauf Aldi Suisse 65 8.87 6'860.84
13.05.2026, 12:24, Debit Mastercard-Nr. 557452xxxxxx2383
Umsatz 10.87 1'150.00
0000_10/92030
Raiffeisenbank Genossenschaft 1 / 2
Kontoauszug 01.05.2026 - 31.05.2026
Datum Text Belastungen Gutschriften Saldo
Übertrag 10.87 1'150.00
22.05.26 Gutschrift ARBEITGEBER AG 10'158.30 17'019.14
Salarzahlung
29.05.26 Übertrag auf Mitglieder Sparkonto CH39 0000 0000 0000 0000 0 1'000.00 16'019.14
Taxes 2022/2023/2024/2025
Umsatz 1'010.87 11'308.30
Saldo zu Ihren Gunsten 16'019.14
"""


def test_parse_raiffeisen():
    txns = parse_raiffeisen(RAIFFEISEN)
    assert [t["amount"] for t in txns] == [-2.00, 1150.00, -8.87, 10158.30, -1000.00]
    assert txns[0]["date"] == "2026-05-01"                      # booking date, not valuta
    assert "Mastercard" in txns[0]["desc"]                      # continuation appended
    assert txns[0]["desc"].startswith("Einkauf Selecta")
    assert "(04.05.26)" not in txns[0]["desc"]                  # valuta prefix stripped
    assert txns[1]["desc"] == "Gutschrift Vermieter AG u/o | Musterweg 1, 8000 Zürich | MIETZINS WOHNUNG"
    assert txns[2]["desc"].startswith("Einkauf Aldi Suisse 65") # trailing digit in name survives
    # real txn starting with "Übertrag" (savings transfer) is kept; summary rows are not
    assert txns[4]["desc"].startswith("Übertrag auf Mitglieder Sparkonto")
    assert "Raiffeisenbank Genossenschaft" not in " ".join(t["desc"] for t in txns)


def test_parse_raiffeisen_balance_mismatch_raises():
    broken = RAIFFEISEN.replace("2.00 5'719.71", "2.00 5'000.00")
    with pytest.raises(ValueError, match="Balance mismatch"):
        parse_raiffeisen(broken)


def test_parse_raiffeisen_rejects_other_docs():
    with pytest.raises(ValueError):
        parse_raiffeisen("Dear customer, thank you for banking with us.")


# ---------- rule suggestion heuristic ----------
@pytest.mark.parametrize("desc,token", [
    ("Einkauf Migros MM Delémont | 22.05.2026, 15:33, Debit Mastercard-Nr. 5xx", "Migros MM Delémont"),
    ("Zahlung Sunrise GmbH | Postfach, 8050 Zurich | Bezahlt für: X", "Sunrise"),
    ("Zahlung BKW Energie AG | Viktoriaplatz 2, 3013 Bern", "BKW Energie"),
    ("Gebührenbelastung Kontoführung | 01.01.2026 - 28.04.2026", "Kontoführung"),
    ("Bancomat Bezug BR Soyhières Bell. | 09.05.2026, 11:34, Debit", "BR Soyhières Bell"),
])
def test_merchant_token(desc, token):
    assert merchant_token(desc) == token


ADDR = "Route de Bâle 4, 2805 Soyhières"
PAYMENT = f"Zahlung Maneenooch Hadchaiyaphum | {ADDR}"
SAVINGS = f"Dauerauftrag Maneenooch Hadchaiyaphum | {ADDR} | Savings Mia"


def test_purpose_note_splits_same_counterparty():
    # same person, different purpose -> different tokens (the reported bug)
    assert merchant_token(PAYMENT) == "Maneenooch Hadchaiyaphum"
    assert merchant_token(SAVINGS) == "Maneenooch Hadchaiyaphum · Savings Mia"
    assert merchant_token(PAYMENT) != merchant_token(SAVINGS)


def test_purpose_note_ignores_address_and_card_tails():
    # address / date-card continuation must NOT be treated as a note
    assert merchant_token(PAYMENT) == "Maneenooch Hadchaiyaphum"
    assert merchant_token(
        "Einkauf Migros MM Delémont | 22.05.2026, 15:33, Debit Mastercard-Nr. 5xx"
    ) == "Migros MM Delémont"


def test_merchant_regex_requires_note_when_present():
    rx_sav = merchant_regex(SAVINGS)
    assert re.search(rx_sav, SAVINGS, re.I)          # matches its own desc
    assert not re.search(rx_sav, PAYMENT, re.I)      # must NOT match the plain payment
    # and the suggested rule uses this regex, not the ' · ' display token
    assert suggest_rule(SAVINGS)["match"] == rx_sav


def test_merchant_token_fallback():
    # nothing strippable -> trimmed first segment, never empty
    assert merchant_token("Random Shop XYZ") == "Random Shop XYZ"
    assert merchant_token("") == ""
    # if stripping noise would empty the token, fall back to the raw head
    assert merchant_token("GmbH") == "GmbH"


@pytest.mark.parametrize("desc,cat,sub", [
    ("Einkauf Migros MM Delémont | ...", "Groceries", None),
    ("Einkauf Selecta Merchant ven | ...", "Groceries", "Convenience"),
    ("Zahlung Sunrise GmbH | ...", "Telecom", None),
    ("Zahlung BKW Energie AG | ...", "Utilities", "Electricity"),
    ("Zahlung Dextra Rechtsschutz AG | ...", "Insurance", None),
    ("Zahlung RCJU . Service des contributions | ...", "Taxes", None),
    ("Übertrag auf Mitglieder Sparkonto CH39 ... | Savings", "Savings", None),
    ("Gutschrift CENTRIS AG | ... | Salarzahlung", "Income", "Salary"),
    ("Gebührenbelastung Kontoführung | ...", "Bank fees", None),
])
def test_suggest_category(desc, cat, sub):
    assert suggest_category(desc) == (cat, sub)


def test_suggest_category_unknown():
    assert suggest_category("Zahlung Some Private Person") == (None, None)


def test_suggest_rule_shape_and_regex_safe():
    r = suggest_rule("Einkauf SumUp *Phusila Thai | 06.05.2026, Debit")
    assert r["cat"] == "Dining"
    assert r["source"] == "seed"
    assert set(r) == {"match", "token", "cat", "sub", "source"}
    # match must be a valid regex even though the token contains '*'
    import re
    assert re.compile(r["match"], re.I).search("einkauf sumup *phusila thai")


def test_suggest_rule_learned_overrides_seed():
    desc = "Einkauf Selecta Merchant ven | 07.05.2026, Debit"
    assert suggest_rule(desc)["cat"] == "Groceries"          # seed guess
    r = suggest_rule(desc, learned=("Dining", "Vending"))    # user tagged it thus
    assert (r["cat"], r["sub"], r["source"]) == ("Dining", "Vending", "learned")


def test_suggest_rule_no_guess():
    r = suggest_rule("Zahlung Some Private Person")
    assert (r["cat"], r["source"]) == (None, None)


# Account-closing statement: balance goes negative (fees before final credit),
# so both the amount and Saldo columns must tolerate a leading '-'.
RAIFFEISEN_NEGATIVE = """Kontoinhaber Max Muster
Kontoauszug 01.03.2026 - 28.04.2026
Datum Text Belastungen Gutschriften Saldo
(Valuta)
28.02.26 Saldovortrag 0.00
28.04.26 Abschlussbetreffnis von 31.12.2025 bis 28.04.2026 0.58 -0.58
Sollzins CHF 0.58
28.04.26 Gebührenbelastung Kontoführung 19.67 -20.25
01.01.2026 - 28.04.2026
28.04.26 Saldierung via Übertrag von Mitglieder Privatkonto CH43 8080 8006 20.25 0.00
1733 8129 1
Umsatz 20.25 20.25
Saldo 0.00
"""


def test_parse_raiffeisen_negative_balance():
    txns = parse_raiffeisen(RAIFFEISEN_NEGATIVE)
    assert [t["amount"] for t in txns] == [-0.58, -19.67, 20.25]  # running saldo: -0.58, -20.25, 0.00
    assert txns[2]["desc"].startswith("Saldierung via Übertrag")
