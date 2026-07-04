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


# ---------- Raiffeisen PDF statements (Kontoauszug) ----------
_MONEY = r"\d{1,3}(?:'\d{3})*\.\d{2}"
_ENTRY = re.compile(rf"^(\d{{2}}\.\d{{2}}\.\d{{2}})\s+(.+?)\s+({_MONEY})\s+({_MONEY})$")
_OPENING = re.compile(rf"^\d{{2}}\.\d{{2}}\.\d{{2}}\s+Saldovortrag\s+({_MONEY})$")
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
