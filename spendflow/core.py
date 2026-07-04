"""Pure functions: statement parsing, categorization, hashing. No I/O, no state."""
import hashlib
import math
import re
import xml.etree.ElementTree as ET


def parse_amount(s) -> float:
    """Normalize 1'234.56 / 1.234,56 / 1,234.56 / (12.30) / 12.30- to float. NaN if unparseable."""
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s or "").strip().replace("'", "").replace("\u00a0", "").replace(" ", "")
    if not s:
        return math.nan
    neg = s.startswith(("-", "(")) or s.endswith("-")
    s = re.sub(r"[^\d.,]", "", s)
    dot, com = s.rfind("."), s.rfind(",")
    if dot > -1 and com > -1:  # both present: the later one is the decimal separator
        s = s.replace(".", "").replace(",", ".") if com > dot else s.replace(",", "")
    elif com > -1:  # lone comma: decimal sep only if followed by 1-2 digits
        s = s.replace(",", ".") if len(s) - com <= 3 else s.replace(",", "")
    try:
        v = float(s)
    except ValueError:
        return math.nan
    return -v if neg else v


def parse_date(s) -> str | None:
    """ISO / 31.01.2026 / dd/mm/yyyy -> 'YYYY-MM-DD'."""
    s = str(s or "").strip()
    if m := re.match(r"(\d{4})-(\d{2})-(\d{2})", s):
        return m.group(0)
    if m := re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", s):
        d, mo, y = m.groups()
        return f"{'20' + y if len(y) == 2 else y}-{int(mo):02d}-{int(d):02d}"
    if m := re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s):
        d, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return None


def txn_hash(date: str | None, amount: float, desc: str) -> str:
    """Stable dedup key so overlapping statement re-imports are idempotent."""
    return hashlib.sha1(f"{date}|{amount:.2f}|{desc.strip()}".encode()).hexdigest()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first(el, name: str):
    return next((e for e in el.iter() if _local(e.tag) == name), None)


def parse_camt(xml_text: str) -> list[dict]:
    """camt.053 -> [{date, desc, amount, currency}]. Namespace-agnostic; amounts signed (DBIT negative)."""
    root = ET.fromstring(xml_text)
    out = []
    for ntry in (e for e in root.iter() if _local(e.tag) == "Ntry"):
        amt_el = _first(ntry, "Amt")
        if amt_el is None:
            continue
        amount = float(amt_el.text)
        ind = _first(ntry, "CdtDbtInd")
        if ind is not None and ind.text == "DBIT":
            amount = -amount
        date_el = _first(ntry, "BookgDt")
        if date_el is None:
            date_el = _first(ntry, "ValDt")
        date = parse_date(_first(date_el, "Dt").text) if date_el is not None and _first(date_el, "Dt") is not None else None
        desc_el = _first(ntry, "Ustrd")
        if desc_el is None:
            rp = _first(ntry, "RltdPties")
            desc_el = _first(rp, "Nm") if rp is not None else None
        if desc_el is None:
            desc_el = _first(ntry, "AddtlNtryInf")
        desc = (desc_el.text or "").strip() if desc_el is not None else "(no description)"
        out.append({"date": date, "desc": desc, "amount": amount,
                    "currency": amt_el.get("Ccy", "CHF")})
    if not out:
        raise ValueError("No <Ntry> entries found - not a camt.053 file?")
    return out


def compile_rules(rules: list[dict]) -> list[tuple]:
    """[{match, cat, sub}] -> [(compiled_re, cat, sub)], silently skipping invalid regexes."""
    out = []
    for r in rules:
        try:
            out.append((re.compile(r["match"], re.I), r["cat"], r.get("sub")))
        except re.error:
            pass
    return out


def categorize(desc: str, compiled: list[tuple]) -> tuple[str, str | None]:
    for rx, cat, sub in compiled:
        if rx.search(desc):
            return cat, sub
    return "Uncategorized", None


# ---------- rule suggestion (local heuristic; no network) ----------
# Statement descriptions look like:
#   "Einkauf Migros MM Delémont | 22.05.2026, 15:33, Debit Mastercard-Nr. 557452xxxxxx2383"
#   "Zahlung Sunrise GmbH | Postfach, 8050 Zurich | Bezahlt für: Michael Rudiger"
# We want a stable merchant token ("Migros MM Delémont", "Sunrise GmbH") to build a
# regex suggestion from, and a rough category guess. Both are just starting points
# the user edits in the UI.

# Leading transaction-type verbs Raiffeisen prints before the merchant name.
_TXN_PREFIX = re.compile(
    r"^(?:Einkauf|Zahlung|Gutschrift|Dauerauftrag(?:\s+Ausland)?|Bancomat\s+Bezug|"
    r"Übertrag(?:\s+auf)?|Gebühr(?:enbelastung)?|Paketpreis)\s+", re.I)
# Noise tokens that are never part of a merchant name.
_NOISE = re.compile(
    r"\b(?:GmbH|AG|SARL|Sàrl|SA|Merchant|Debit|Mastercard-Nr\.?|Kreditkarte)\b", re.I)

# Continuation segments (after the first ' | ') that are NOT a purpose note:
#   date/card line  "13.05.2026, 12:24, Debit Mastercard-Nr. 5xx"
#   address line    "Route de Bâle 4, 2805 Soyhières"  (has a 4-5 digit postcode)
#   "Bezahlt für: X" / SEPA/FX detail lines
_NOTE_REJECT = re.compile(
    r"^\d{2}\.\d{2}\.\d{2,4}[,\s]"           # leading date/time
    r"|\b\d{4,5}\b"                          # postal code -> address
    r"|Bezahlt für|Umrechnungskurs|SEPA|IBAN|CH\d{2}\b", re.I)


def _purpose_note(segments: list[str]) -> str | None:
    """The trailing free-text reference ('Savings Mia', 'MIETZINS WOHNUNG') that
    distinguishes same-counterparty transactions, or None if the tail is just
    date/card/address noise. Only the last segment is considered."""
    if len(segments) < 2:
        return None
    tail = segments[-1].strip()
    if not tail or _NOTE_REJECT.search(tail):
        return None
    return tail


def _merchant_name(desc: str) -> str:
    """The cleaned merchant name from the first segment (no note appended)."""
    head = desc.split(" | ", 1)[0].strip()
    s = _TXN_PREFIX.sub("", head)
    s = _NOISE.sub("", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" .,-")
    return s or head


def merchant_token(desc: str) -> str:
    """Best-effort merchant name from a raw statement description.

    Takes the first ' | ' segment (the rest is date/card/address continuation),
    strips the leading transaction verb and trailing corporate/noise tokens. If a
    trailing purpose note is present (e.g. 'Savings Mia'), it is appended as
    '<merchant> · <note>' so earmarked transfers to the same person stay distinct.
    Falls back to the trimmed first segment if nothing usable remains.
    """
    name = _merchant_name(desc)
    note = _purpose_note(desc.split(" | "))
    return f"{name} · {note}" if note else name


def _loose(text: str) -> str:
    """Escape `text` into a regex matching its words in order, tolerating extra
    words in between. Needed because _merchant_name strips interior noise tokens
    (e.g. 'Selecta Merchant ven' -> name 'Selecta ven'); a literal 'Selecta ven'
    would never match the original description. Joining words with '.*?' does."""
    words = [re.escape(w) for w in text.split()]
    return r".*?".join(words) if words else re.escape(text)


def merchant_regex(desc: str) -> str:
    """Regex that matches this merchant in a raw description. When a purpose note
    distinguishes the transaction, require BOTH name and note (so 'Savings Mia' and
    a plain payment to the same person get separate rules). Matches the desc, not
    the ' · '-joined display token."""
    name = _merchant_name(desc)
    note = _purpose_note(desc.split(" | "))
    if note:
        return rf"{_loose(name)}.*{_loose(note)}"
    return _loose(name)


# Keyword -> (category, subcategory). First hit wins; matched case-insensitively
# against the full description. Deliberately small and editable.
_CATEGORY_HINTS: list[tuple[str, str, str | None]] = [
    (r"salarzahlung|centris", "Income", "Salary"),
    (r"mietzins", "Income", "Rent received"),
    (r"verzinsung anteilschein", "Income", "Interest"),
    (r"migros|aldi|aligro|landi|lebensmittel|mini lä|pakhäuser|denner|coop", "Groceries", None),
    (r"selecta|station-shop|belleri", "Groceries", "Convenience"),
    (r"phusila|giardino|perrest|sumup|restaurant|thai", "Dining", None),
    (r"apotheke|pharmac", "Health", "Pharmacy"),
    (r"world of games|kino|cinema", "Leisure", None),
    (r"sunrise|wingo|swisscom|salt", "Telecom", None),
    (r"bkw|energie|elektriz|strom", "Utilities", "Electricity"),
    (r"rechtsschutz|assura|helsana|css |versicherung|insurance", "Insurance", None),
    (r"contributions|trésorerie|tresorerie|canton du jura|rcju|steuer|impôt|impot", "Taxes", None),
    (r"tea building|shoreditch|6th floor", "Housing", "Rent"),
    (r"sparkonto|geschenksparkonto|savings", "Savings", None),
    (r"sepa|money elyes|dauerauftrag ausland", "Transfers", "International"),
    (r"dauerauftrag|bancomat|bezug", "Transfers", "Cash/Standing order"),
    (r"kontoführung|paketpreis|gebühr|memberplus|sollzins|abschlussbetreffnis|"
     r"saldierung|saldovortrag", "Bank fees", None),
]
_HINTS_COMPILED = [(re.compile(p, re.I), c, s) for p, c, s in _CATEGORY_HINTS]


def suggest_category(desc: str) -> tuple[str | None, str | None]:
    """Guess (cat, sub) for a description, or (None, None) if nothing matches."""
    for rx, cat, sub in _HINTS_COMPILED:
        if rx.search(desc):
            return cat, sub
    return None, None


def suggest_rule(desc: str, learned: tuple[str, str | None] | None = None) -> dict:
    """A ready-to-edit rule proposal: regex from merchant token + category guess.

    `learned` (cat, sub) — e.g. how the user previously tagged this same merchant —
    wins over the static seed map, so suggestions improve as the user categorizes.
    `source` records where the guess came from: 'learned' | 'seed' | None.
    """
    token = merchant_token(desc)
    if learned and learned[0]:
        cat, sub, source = learned[0], learned[1], "learned"
    else:
        cat, sub = suggest_category(desc)
        source = "seed" if cat else None
    return {"match": merchant_regex(desc), "token": token, "cat": cat, "sub": sub, "source": source}


# ---------- Raiffeisen PDF statements (Kontoauszug) ----------
_MONEY = r"\d{1,3}(?:'\d{3})*\.\d{2}"     # amount column: always unsigned
_BAL = rf"-?{_MONEY}"                     # Saldo column: negative when overdrawn
_ENTRY = re.compile(rf"^(\d{{2}}\.\d{{2}}\.\d{{2}})\s+(.+?)\s+({_MONEY})\s+({_BAL})$")
_OPENING = re.compile(rf"^\d{{2}}\.\d{{2}}\.\d{{2}}\s+Saldovortrag\s+({_BAL})$")
_TABLE_END = re.compile(rf"^(Umsatz|Übertrag)\s+{_MONEY}")  # summary rows, no date
_VALUTA = re.compile(r"^\(\d{2}\.\d{2}\.\d{2}\)\s*")


def parse_raiffeisen(text: str) -> list[dict]:
    """Raiffeisen Kontoauszug (pdfplumber-extracted text) -> signed transactions.

    The statement prints debits and credits in separate columns, which collapse
    into one number in extracted text. The running Saldo disambiguates: for each
    entry exactly one of prev±amount equals the new balance - and doubles as an
    integrity check on the whole parse. Continuation lines (card details,
    counterparty address) are appended to the description; they also make the
    dedup hash distinguish same-day same-amount purchases.
    """
    txns: list[dict] = []
    saldo: float | None = None
    current: dict | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if m := _OPENING.match(line):
            saldo, current = parse_amount(m.group(1)), None
        elif m := _ENTRY.match(line):
            if saldo is None:
                raise ValueError("Entry before Saldovortrag - opening balance unknown")
            date, desc, amount, new_saldo = (m.group(1), m.group(2),
                                             parse_amount(m.group(3)), parse_amount(m.group(4)))
            if abs(saldo - amount - new_saldo) < 0.005:
                amount = -amount                      # debit
            elif abs(saldo + amount - new_saldo) >= 0.005:
                raise ValueError(f"Balance mismatch at '{line}': "
                                 f"{saldo} ± {amount} != {new_saldo}")
            current = {"date": parse_date(date), "desc": desc,
                       "amount": amount, "currency": "CHF"}
            txns.append(current)
            saldo = new_saldo
        elif _TABLE_END.match(line):
            current = None                            # page footer/header follows
        elif current is not None and line:
            current["desc"] += " | " + _VALUTA.sub("", line)

    if not txns:
        raise ValueError("No statement entries found - not a Raiffeisen Kontoauszug?")
    return txns
