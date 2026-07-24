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


def norm_name(s: str | None) -> str | None:
    """Canonical form for a category/subcategory name: lowercase, collapsed spaces.

    Category names are case-insensitive identifiers, so storing one canonical form
    removes a whole class of near-duplicates ('Taxes' vs 'taxes') at the source
    instead of detecting them after the fact. 'Uncategorized' is the one reserved
    sentinel the rest of the code compares against literally, so it is preserved.
    """
    if s is None:
        return None
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return None
    return "Uncategorized" if s.casefold() == "uncategorized" else s.casefold()


def compile_rules(rules: list[dict]) -> list[tuple]:
    """[{match, cat, sub}] -> [(compiled_re, cat, sub)], silently skipping invalid regexes."""
    out = []
    for r in rules:
        try:
            out.append((re.compile(r["match"], re.I),
                        norm_name(r["cat"]) or "Uncategorized", norm_name(r.get("sub"))))
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
    # --- credit-card merchants (mostly digital subscriptions / online) ---
    (r"sbb|cff|ffs|mobile ticket", "Transport", "Public transport"),
    (r"claude\.ai|anthropic|openai|chatgpt|google \*grok", "Software", "AI"),
    (r"tidal|spotify|prime video|netflix|crunchyroll|disney|audible", "Subscriptions", "Media"),
    (r"digitalrepublic|starlink", "Telecom", None),
    (r"digitec|galaxus|temu|iherb|amzn|amazon", "Shopping", "Online"),
    (r"battle\.net|steam|epic games|nintendo", "Leisure", "Games"),
    (r"amende|busse|fine|police", "Fines", None),
    # --- Viseca merchant-category tags (fallback: they classify their own txns) ---
    (r"supermärkte|lebensmittel", "Groceries", None),
    (r"drogerien|apotheken", "Health", "Pharmacy"),
    (r"transportdienstleistungen", "Transport", None),
    (r"digitalprodukte|computersoftware|it services|software", "Software", None),
    (r"shopping-abonnements|abonnement", "Subscriptions", None),
    (r"warenhäuser|spezialgeschäfte", "Shopping", None),
    (r"telekommunikation|internet, webhosting", "Telecom", None),
    (r"kosmetik|parfümerie", "Personal care", None),
]
_HINTS_COMPILED = [(re.compile(p, re.I), c, s) for p, c, s in _CATEGORY_HINTS]


def suggest_category(desc: str) -> tuple[str | None, str | None]:
    """Guess (cat, sub) for a description, or (None, None) if nothing matches.

    The hint table above is written in readable title case; names are normalized on
    the way out so suggestions never reintroduce capitalized variants."""
    for rx, cat, sub in _HINTS_COMPILED:
        if rx.search(desc):
            return norm_name(cat), norm_name(sub)
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


# ---------- category anomaly detection (pure; feeds the Review card) ----------
def _norm_cat(s: str) -> str:
    """Normalize a category/subcategory name for near-duplicate comparison:
    case-fold and drop spaces/hyphens so 'Bank fees' ~ 'bank-fees' ~ 'Bankfees'."""
    return re.sub(r"[\s\-_]+", "", (s or "").casefold())


def _stem_cat(s: str) -> str:
    """Looser key than _norm_cat: also folds English plurals, so 'transfer' ~
    'Transfers' and 'fee' ~ 'fees' collide. Deliberately crude — it only needs to
    group candidates the user then confirms, never to rewrite anything on its own.

    Order matters: 'ies'->'y' before the bare 's' strip, so 'groceries'->'grocery'
    rather than 'grocerie'. Words of 3 chars or fewer are left alone ('gas').
    """
    w = _norm_cat(s)
    if len(w) <= 3:
        return w
    if w.endswith("ies"):
        return w[:-3] + "y"
    if w.endswith(("ses", "xes", "zes", "ches", "shes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def detect_anomalies(txns: list[dict]) -> list[dict]:
    """Scan categorized transactions for likely category-hygiene problems.

    Returns findings [{type, severity, title, detail, items}] where type is one of
    'duplicate' | 'cross_level' | 'singleton' | 'outlier'. Pure: takes the txn
    dicts (needs cat, sub, amount, desc), returns data the UI renders. No I/O.
    """
    cat_txns: dict[str, list[dict]] = {}
    subs: set[str] = set()
    for t in txns:
        c = t.get("cat")
        if c and c != "Uncategorized":
            cat_txns.setdefault(c, []).append(t)
        if t.get("sub"):
            subs.add(t["sub"])
    cats = set(cat_txns)
    out: list[dict] = []

    # 1) near-duplicate category names.
    # Two tiers: exact variants (case/space/hyphen — certainly the same category)
    # and stem-level matches (singular/plural — very likely, but the user confirms).
    by_norm: dict[str, set[str]] = {}
    for c in cats:
        by_norm.setdefault(_norm_cat(c), set()).add(c)
    for variants in by_norm.values():
        if len(variants) > 1:
            # most-used first; ties broken alphabetically for a deterministic keeper
            names = sorted(variants, key=lambda c: (-len(cat_txns[c]), c))
            keep, *merge = names
            out.append({
                "type": "duplicate", "severity": "warn",
                "title": f"Duplicate category: {' / '.join(names)}",
                "detail": f"These names differ only by case or spacing. Merge into "
                          f"'{keep}'?",
                "items": [{"name": c, "n": len(cat_txns[c])} for c in names],
                # one-click fix: merge each minority variant into the most-used name
                "action": {"label": f"Merge into '{keep}'",
                           "ops": [{"level": "cat", "old": m, "new": keep} for m in merge]}})

    # Stem-level groups spanning MORE than one exact-variant group, e.g.
    # {'transfer'} + {'Transfers'}. Groups already reported above are skipped.
    by_stem: dict[str, set[str]] = {}
    for c in cats:
        by_stem.setdefault(_stem_cat(c), set()).add(c)
    for variants in by_stem.values():
        if len({_norm_cat(c) for c in variants}) < 2:
            continue  # single exact-variant group: either fine, or already reported
        names = sorted(variants, key=lambda c: (-len(cat_txns[c]), c))
        keep, *merge = names
        out.append({
            "type": "near_duplicate", "severity": "warn",
            "title": f"Similar categories: {' / '.join(names)}",
            "detail": f"These look like singular/plural forms of one category. "
                      f"Merge into '{keep}'?",
            "items": [{"name": c, "n": len(cat_txns[c])} for c in names],
            "action": {"label": f"Merge into '{keep}'",
                       "ops": [{"level": "cat", "old": m, "new": keep} for m in merge]}})

    # 2) a name used as BOTH a category and a subcategory (causes Sankey loops)
    for name in sorted(cats & subs):
        out.append({
            "type": "cross_level", "severity": "warn",
            "title": f"'{name}' is both a category and a subcategory",
            "detail": "Reusing a name at two levels is confusing and distorts the "
                      "flow chart. Rename the subcategory use to disambiguate.",
            "items": [{"name": name, "n": len(cat_txns.get(name, []))}],
            # one-click fix: rename the *subcategory* occurrences (leaves the
            # category, which usually has more transactions, untouched)
            "action": {"label": f"Rename sub → '{name} (sub)'",
                       "ops": [{"level": "sub", "old": name, "new": f"{name} (sub)"}]}})

    # 3) singleton categories (a single transaction — possible typo / stray)
    singletons = sorted((c for c in cats if len(cat_txns[c]) == 1))
    if singletons:
        out.append({
            "type": "singleton", "severity": "info",
            "title": f"{len(singletons)} categor{'y' if len(singletons)==1 else 'ies'} "
                     f"with a single transaction",
            "detail": "Might be a typo or a one-off worth folding into another category.",
            "items": [{"name": c, "n": 1} for c in singletons]})

    # 4) amount outliers within a category (robust: median + MAD)
    for c, ts in cat_txns.items():
        amts = sorted(abs(t["amount"]) for t in ts)
        if len(amts) < 4:
            continue  # too few points to call anything an outlier
        mid = len(amts) // 2
        median = amts[mid] if len(amts) % 2 else (amts[mid - 1] + amts[mid]) / 2
        devs = sorted(abs(a - median) for a in amts)
        mmid = len(devs) // 2
        mad = devs[mmid] if len(devs) % 2 else (devs[mmid - 1] + devs[mmid]) / 2
        if mad == 0:
            continue  # no spread -> nothing to flag
        flagged = []
        for t in ts:
            score = abs(abs(t["amount"]) - median) / (1.4826 * mad)  # ~z-score
            if score >= 5 and abs(t["amount"]) > median * 3:
                flagged.append({"desc": (t.get("desc") or "")[:60],
                                "amount": t["amount"], "score": round(score, 1)})
        if flagged:
            flagged.sort(key=lambda f: -abs(f["amount"]))
            out.append({
                "type": "outlier", "severity": "info",
                "title": f"Unusual amount in '{c}'",
                "detail": f"Far outside this category's typical spend "
                          f"(median {median:.0f}). Check for a mis-categorization.",
                "items": flagged})

    return out


# ---------- recurring-payment detection (pure; feeds the Recurring card) ----------
def _month_seq(lo: str, hi: str) -> list[str]:
    """Calendar months from 'YYYY-MM' lo to hi inclusive."""
    y, m = int(lo[:4]), int(lo[5:7])
    out = []
    while (key := f"{y:04d}-{m:02d}") <= hi:
        out.append(key)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def detect_recurring(txns: list[dict]) -> list[dict]:
    """Find recurring expenses: merchants charged in most months, at a stable
    monthly amount, with few charges per month (a subscription or standing order,
    not habitual shopping).

    Pure: needs merchant, date, amount. 'Now' is the newest month in the data, not
    the wall clock, so results are reproducible from the data alone.

    Heuristics, deliberately simple:
      - seen in >= 3 distinct months, covering >= 75% of its first..last span
      - <= 2.5 charges per active month on average (Netflix bills once; a
        supermarket you visit weekly is not a subscription)
      - monthly totals stable: MAD/median <= 0.25
    Flags: status 'stopped' if absent from the newest month AND the one before;
    'change' when the latest monthly total drifts >1% and >1.00 from the median
    of the earlier months.
    """
    by_merchant: dict[str, list[dict]] = {}
    data_hi = ""
    for t in txns:
        if t.get("amount", 0) >= 0 or not t.get("merchant") or not t.get("date"):
            continue
        by_merchant.setdefault(t["merchant"], []).append(t)
        data_hi = max(data_hi, t["date"][:7])

    out: list[dict] = []
    for merchant, ts in by_merchant.items():
        monthly: dict[str, float] = {}
        for t in ts:
            m = t["date"][:7]
            monthly[m] = monthly.get(m, 0.0) + -t["amount"]
        months = sorted(monthly)
        if len(months) < 3:
            continue
        span = _month_seq(months[0], months[-1])
        if len(months) / len(span) < 0.75:
            continue
        if len(ts) / len(months) > 2.5:
            continue
        totals = sorted(monthly.values())
        mid = len(totals) // 2
        median = totals[mid] if len(totals) % 2 else (totals[mid - 1] + totals[mid]) / 2
        devs = sorted(abs(v - median) for v in totals)
        mad = devs[mid] if len(devs) % 2 else (devs[mid - 1] + devs[mid]) / 2
        if median <= 0 or mad / median > 0.25:
            continue

        prev = _month_seq(months[0], data_hi)[-2] if len(_month_seq(months[0], data_hi)) > 1 else data_hi
        stopped = months[-1] < prev
        last_total = monthly[months[-1]]
        earlier = sorted(v for m, v in monthly.items() if m != months[-1])
        emid = len(earlier) // 2
        emed = earlier[emid] if len(earlier) % 2 else (earlier[emid - 1] + earlier[emid]) / 2
        change = round(last_total - emed, 2)
        if abs(change) <= max(1.0, emed * 0.01) or stopped:
            change = 0.0

        out.append({"merchant": merchant, "monthly": round(median, 2),
                    "n_months": len(months), "first": months[0], "last": months[-1],
                    "status": "stopped" if stopped else "active",
                    "change": change, "per_month": round(len(ts) / len(months), 1)})

    out.sort(key=lambda r: (r["status"] != "active", -r["monthly"]))
    return out


# ---------- Viseca (Raiffeisen) credit-card statements ----------
_VISECA_TXN = re.compile(
    rf"^(\d{{2}}\.\d{{2}}\.\d{{2}})\s+\d{{2}}\.\d{{2}}\.\d{{2}}\s+(.+?)"
    rf"(?:\s+([A-Z]{{3}})\s+({_MONEY}))?"       # optional foreign currency + amount
    rf"\s+({_MONEY})$")
_VISECA_TOTAL = re.compile(rf"^Total Karte .*?({_MONEY})$")
_VISECA_PAYMENT = re.compile(r"Ihre Zahlung")   # previous bill's payment, not a purchase
_VISECA_NOISE = re.compile(
    r"^(Übertrag|Zwischensumme|Total|Seite|Herausgegeben|Ihrer Raiffeisen|"
    r"Viseca|Hagenholz|\d{4,5} |CH-|Datum |Karten|\d{4} \d{2}XX|Globallimite|"
    r"Fälliger|Zahlung über|Ab dem|Abtretung|Services SA|P\.P\.|Response|"
    r"Kundenservice|Telefon|Herr|Michael|Route|Abrechnung|Zahlbar|PayNet|"
    r"Kontoinhaber|Bearbeitungsgebühr|Umrechnungskurs)")


def parse_viseca(text: str) -> tuple[list[dict], float | None]:
    """Viseca credit-card statement (pdfplumber text) -> (purchases, total).

    One negative txn per purchase. The 'Betrag in CHF' column already includes the
    1.5% Bearbeitungsgebühr, so that fee line is informational and ignored (folding
    it would double-count). The single merchant-category line after each purchase
    (e.g. 'Warenhäuser') is appended to the description. `total` is the stated
    'Total Karte', used to reconcile against the lump-sum bank debit; the item sum
    is checked against it as an integrity guard.
    """
    txns: list[dict] = []
    total: float | None = None
    current: dict | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if m := _VISECA_TOTAL.match(line):
            total, current = parse_amount(m.group(1)), None
        elif _VISECA_PAYMENT.search(line):
            current = None
        elif (m := _VISECA_TXN.match(line)) and not _VISECA_NOISE.match(line):
            date, details, fccy, famt, chf = m.groups()
            desc = details.strip() + (f" | {fccy} {famt}" if fccy else "")
            current = {"date": parse_date(date), "desc": desc,
                       "amount": -parse_amount(chf), "currency": "CHF"}
            txns.append(current)
        elif current is not None and not _VISECA_NOISE.match(line):
            current["desc"] += " | " + line   # merchant-category tag
            current = None                    # only the immediate next line counts

    if not txns:
        raise ValueError("No credit-card transactions found - not a Viseca statement?")
    if total is not None:
        s = round(-sum(t["amount"] for t in txns), 2)
        if abs(s - total) > 0.05:
            raise ValueError(f"CC total mismatch: items sum {s} != stated {total}")
    return txns, total
