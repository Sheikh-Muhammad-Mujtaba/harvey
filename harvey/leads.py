"""The lead list: companies with their status, best contact, score and star,
filtered and sorted one way for the Companies tab, its export and the chat.

A lead's status comes from its contacts, furthest along first: replied,
contacted, queued (an email waits to send), email found (a deliverable
address, nothing queued yet), opted out, phone only, or new (nothing to
reach them by yet).

``to_csv`` and ``to_xlsx`` build the export in memory: nothing is written
to disk.
"""

import csv
import io
import json

from harvey import service_tags

STATUSES = [
    ("new", "No contact yet"),
    ("phone_only", "Phone only"),
    ("email_found", "Email found"),
    ("queued", "Email queued"),
    ("contacted", "Contacted"),
    ("replied", "Replied"),
    ("opted_out", "Opted out"),
]
SORTS = ("score", "recent", "name")
MAX_PAGE = 500

_BASE = """
WITH pstats AS (
    SELECT company_id,
        MAX(status IN ('replied', 'meeting', 'closed', 'closed_won')) AS replied,
        MAX(status = 'contacted') AS contacted,
        MAX(status = 'queued') AS queued,
        MAX(email_status IN ('verified', 'risky') AND status != 'opted_out') AS emailable,
        MAX(status = 'opted_out') AS opted,
        COUNT(*) AS n
    FROM prospects GROUP BY company_id
)
SELECT * FROM (
    SELECT c.id, c.name, c.domain, c.website, c.industry, c.location, c.phone,
           c.description, c.notes, c.source, c.source_url, c.created_at, c.updated_at,
           COALESCE(c.starred, 0) AS starred, COALESCE(c.starred_by, '') AS starred_by,
           c.lead_score,
           COALESCE(ps.n, 0) AS contact_count,
           CASE WHEN ps.replied THEN 'replied'
                WHEN ps.contacted THEN 'contacted'
                WHEN ps.queued THEN 'queued'
                WHEN ps.emailable THEN 'email_found'
                WHEN ps.opted THEN 'opted_out'
                WHEN COALESCE(c.phone, '') != '' THEN 'phone_only'
                ELSE 'new' END AS lead_status
    FROM companies c LEFT JOIN pstats ps ON ps.company_id = c.id
) leads
"""


def _where(filters: dict, config) -> tuple[str, list]:
    where, params = [], []
    service = (filters.get("service") or "").strip()
    if service:
        cats = [str(c).lower() for c in
                (getattr(getattr(config, "icp", None), "service_tags", None) or {}).get(service, [])]
        if not cats:
            return "WHERE 0", []
        where.append(f"LOWER(industry) IN ({', '.join('?' * len(cats))})")
        params += cats
    if filters.get("industry"):
        where.append("LOWER(industry) = LOWER(?)")
        params.append(str(filters["industry"]).strip())
    if filters.get("location"):
        where.append("location LIKE ?")
        params.append(f"%{str(filters['location']).strip()}%")
    if str(filters.get("min_score") or "").strip():
        try:
            where.append("lead_score >= ?")
            params.append(int(filters["min_score"]))
        except ValueError:
            pass
    if filters.get("status") in dict(STATUSES):
        where.append("lead_status = ?")
        params.append(filters["status"])
    if str(filters.get("starred") or "").lower() in ("1", "true", "yes", "on"):
        where.append("starred = 1")
    has = filters.get("has")
    if has == "website":
        where.append("COALESCE(domain, '') != ''")
    elif has == "phone":
        where.append("COALESCE(phone, '') != ''")
    elif has == "email":
        where.append("lead_status IN ('email_found', 'queued', 'contacted', 'replied')")
    if filters.get("q"):
        where.append("(name LIKE ? OR domain LIKE ?)")
        params += [f"%{str(filters['q']).strip()}%"] * 2
    return ("WHERE " + " AND ".join(where)) if where else "", params


def _order(sort: str) -> str:
    if sort == "recent":
        return "ORDER BY updated_at DESC, created_at DESC"
    if sort == "name":
        return "ORDER BY name COLLATE NOCASE"
    return ("ORDER BY lead_score IS NULL, lead_score DESC, starred DESC, "
            "updated_at DESC")


async def list_leads(state, filters: dict, config, limit: int | None = MAX_PAGE) -> dict:
    """{"rows": [...], "total": n} for the filters, best first by default."""
    where, params = _where(filters, config)
    order = _order(filters.get("sort") or "score")
    async with state._connect() as db:
        cur = await db.execute(f"SELECT COUNT(*) FROM ({_BASE} {where})", params)
        total = (await cur.fetchone())[0]
        sql = f"{_BASE} {where} {order}" + (" LIMIT ?" if limit else "")
        cur = await db.execute(sql, (*params, limit) if limit else params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in await cur.fetchall()]
        await _enrich(db, rows, config)
    return {"rows": rows, "total": total}


async def _enrich(db, rows: list[dict], config):
    """Add tags, the best contact, the contact card and the score's reasons."""
    by_id = {r["id"]: r for r in rows}
    for r in rows:
        r["service_tags"] = service_tags.tags_for(r.get("industry") or "", config)
        r.update(email="", email_status="", contact_name="", contact_title="",
                 contact_card=None, score_detail=None)
    ids = list(by_id)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ", ".join("?" * len(chunk))
        # Best contact per company: a deliverable email first, a named person next.
        cur = await db.execute(
            f"""SELECT company_id, email, email_status, first_name, last_name, title, status
                FROM prospects WHERE company_id IN ({marks}) AND status != 'opted_out'
                ORDER BY company_id,
                         email_status IN ('verified', 'risky') DESC,
                         first_name != '' DESC, created_at""", chunk)
        for cid, email, est, first, last, title, _status in await cur.fetchall():
            r = by_id[cid]
            if not r["email"] and email:
                r["email"], r["email_status"] = email, est or ""
            if not r["contact_name"] and (first or last):
                r["contact_name"] = f"{first or ''} {last or ''}".strip()
                r["contact_title"] = title or ""
        cur = await db.execute(
            f"""SELECT company_id, signal_code, value_text FROM observations
                WHERE company_id IN ({marks}) AND signal_code IN ('CONTACT_DETAILS', 'LEAD_SCORE')
                ORDER BY observed_at""", chunk)
        for cid, code, text in await cur.fetchall():
            try:
                value = json.loads(text or "{}")
            except ValueError:
                continue
            by_id[cid]["contact_card" if code == "CONTACT_DETAILS" else "score_detail"] = value


async def facets(state, config) -> dict:
    """What the filters can be set to."""
    async with state._connect() as db:
        cur = await db.execute(
            "SELECT industry, COUNT(*) FROM companies WHERE COALESCE(industry, '') != '' "
            "GROUP BY LOWER(industry) ORDER BY 2 DESC LIMIT 60")
        industries = [{"value": i, "count": n} for i, n in await cur.fetchall()]
        cur = await db.execute(
            "SELECT TRIM(SUBSTR(location, 1, INSTR(location || ',', ',') - 1)) AS city, COUNT(*) "
            "FROM companies WHERE COALESCE(location, '') != '' GROUP BY city "
            "ORDER BY 2 DESC LIMIT 60")
        locations = [{"value": c, "count": n} for c, n in await cur.fetchall() if c]
    services = list((getattr(getattr(config, "icp", None), "service_tags", None) or {}).keys())
    return {"industries": industries, "locations": locations, "services": services,
            "statuses": [{"value": v, "label": label} for v, label in STATUSES],
            "sorts": list(SORTS)}


CSV_COLUMNS = [
    "company", "service", "business_type", "location", "website", "phone", "email",
    "email_status", "contact_person", "contact_title", "whatsapp", "address", "facebook",
    "instagram", "linkedin", "lead_score", "score_reason", "est_monthly_budget", "starred",
    "status", "notes", "map_or_source", "added",
]


def _safe(value) -> str:
    """A cell a spreadsheet won't run as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def to_csv(rows: list[dict]) -> str:
    status_label = dict(STATUSES)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    w.writeheader()
    for r in rows:
        card = r.get("contact_card") or {}
        score = r.get("score_detail") or {}
        socials = card.get("socials") or {}
        phone = r.get("phone") or (card.get("phone") if card.get("phone_confirmed") else "")
        w.writerow({k: _safe(v) for k, v in {
            "company": r.get("name"),
            "service": ", ".join(r.get("service_tags") or []),
            "business_type": r.get("industry"),
            "location": r.get("location"),
            "website": r.get("website") or (f"https://{r['domain']}" if r.get("domain") else ""),
            "phone": phone,
            "email": r.get("email") or (card.get("email") if card.get("email_confirmed") else ""),
            "email_status": r.get("email_status") or card.get("email_status") or "",
            "contact_person": r.get("contact_name"),
            "contact_title": r.get("contact_title"),
            "whatsapp": card.get("whatsapp", ""),
            "address": card.get("address", ""),
            "facebook": socials.get("facebook", ""),
            "instagram": socials.get("instagram", ""),
            "linkedin": socials.get("linkedin", ""),
            "lead_score": "" if r.get("lead_score") is None else r["lead_score"],
            "score_reason": score.get("why", ""),
            "est_monthly_budget": score.get("est_monthly_budget", ""),
            "starred": "yes" if r.get("starred") else "",
            "status": status_label.get(r.get("lead_status"), r.get("lead_status")),
            "notes": r.get("notes"),
            "map_or_source": r.get("source_url"),
            "added": r.get("created_at"),
        }.items()})
    return buf.getvalue()


def filters_from(params) -> dict:
    """The filter keys from a query string or a dict, nothing else."""
    keys = ("service", "industry", "location", "min_score", "status", "starred", "has", "q", "sort")
    return {k: params.get(k) for k in keys if params.get(k) not in (None, "")}


# ── Excel workbook: Leads, Contacts and About sheets, built in memory ──

PROSPECT_STATUS = {
    "new": "Not emailed yet", "queued": "Email queued", "contacted": "Emailed",
    "replied": "Replied", "meeting": "Meeting", "opted_out": "Opted out: don't contact",
}
EMAIL_STATUS = {
    "verified": "Verified", "risky": "Catch-all (risky)", "guess": "Unverified (never sent)",
    "invalid": "Invalid", "": "",
}

LEAD_COLUMNS = [  # (header, width, key)
    ("Star", 6, "star"), ("Company", 30, "company"), ("Lead type", 20, "service"),
    ("Business type", 18, "business_type"), ("Location", 24, "location"),
    ("Score", 8, "lead_score"), ("Status", 16, "status"), ("Website", 30, "website"),
    ("Phone", 18, "phone"), ("Email", 30, "email"), ("Email status", 20, "email_status"),
    ("Contact person", 22, "contact_person"), ("Title", 20, "contact_title"),
    ("Contacts", 10, "contacts"), ("WhatsApp", 18, "whatsapp"), ("Address", 34, "address"),
    ("Facebook", 26, "facebook"), ("Instagram", 26, "instagram"), ("LinkedIn", 26, "linkedin"),
    ("Est. monthly budget", 22, "est_monthly_budget"), ("Why this score", 50, "score_reason"),
    ("Notes", 50, "notes"), ("Map or source", 30, "map_or_source"), ("Added", 18, "added"),
]
CONTACT_COLUMNS = [
    ("Company", 30, "company"), ("Star", 6, "star"), ("Score", 8, "score"),
    ("Lead type", 20, "service"), ("Name", 24, "name"), ("Title", 22, "title"),
    ("Email", 32, "email"), ("Email status", 22, "email_status"), ("Phone", 18, "phone"),
    ("Contact status", 22, "status"), ("Found via", 16, "source"), ("Found on", 34, "source_url"),
    ("Notes", 50, "notes"),
]
WRAP = {"address", "score_reason", "notes"}
LINKS = {"website", "facebook", "instagram", "linkedin", "map_or_source", "source_url"}


async def contacts_for(state, company_ids: list[str]) -> list[dict]:
    """Every contact at these companies, best first within each company."""
    out = []
    async with state._connect() as db:
        for i in range(0, len(company_ids), 500):
            chunk = company_ids[i:i + 500]
            cur = await db.execute(
                f"""SELECT company_id, first_name, last_name, title, email, email_status, phone,
                           status, source, source_url, personalization_notes
                    FROM prospects WHERE company_id IN ({', '.join('?' * len(chunk))})
                    ORDER BY company_id, email_status IN ('verified', 'risky') DESC,
                             first_name != '' DESC, created_at""", chunk)
            cols = [d[0] for d in cur.description]
            out += [dict(zip(cols, r)) for r in await cur.fetchall()]
    return out


def _lead_values(r: dict) -> dict:
    card = r.get("contact_card") or {}
    score = r.get("score_detail") or {}
    socials = card.get("socials") or {}
    return {
        "star": "★" if r.get("starred") else "",
        "company": r.get("name"),
        "service": ", ".join(r.get("service_tags") or []),
        "business_type": str(r.get("industry") or "").replace("_", " "),
        "location": r.get("location"),
        "lead_score": r.get("lead_score"),
        "status": dict(STATUSES).get(r.get("lead_status"), r.get("lead_status")),
        "website": r.get("website") or (f"https://{r['domain']}" if r.get("domain") else ""),
        "phone": r.get("phone") or (card.get("phone") if card.get("phone_confirmed") else ""),
        "email": r.get("email") or (card.get("email") if card.get("email_confirmed") else ""),
        "email_status": EMAIL_STATUS.get(r.get("email_status") or card.get("email_status") or "",
                                         r.get("email_status") or ""),
        "contact_person": r.get("contact_name"),
        "contact_title": r.get("contact_title"),
        "contacts": r.get("contact_count") or 0,
        "whatsapp": card.get("whatsapp", ""),
        "address": card.get("address", ""),
        "facebook": socials.get("facebook", ""),
        "instagram": socials.get("instagram", ""),
        "linkedin": socials.get("linkedin", ""),
        "est_monthly_budget": score.get("est_monthly_budget", ""),
        "score_reason": score.get("why", ""),
        "notes": r.get("notes"),
        "map_or_source": r.get("source_url"),
        "added": str(r.get("created_at") or "")[:16],
    }


def _sheet(ws, columns, rows: list[dict], table_name: str):
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo

    ws.append([h for h, _, _ in columns])
    for i, (_, width, _) in enumerate(columns, 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    for r in rows:
        ws.append([None] * len(columns))
        row = ws.max_row
        for i, (_, _, key) in enumerate(columns, 1):
            value = r.get(key)
            if value in (None, ""):
                continue
            cell = ws.cell(row=row, column=i)
            if isinstance(value, str):
                value = ILLEGAL_CHARACTERS_RE.sub("", value)
                cell.value = value
                cell.data_type = "s"  # text, never a formula
                if key in LINKS and value.startswith(("http://", "https://")):
                    cell.hyperlink = value
                    cell.font = Font(color="1D4ED8", underline="single")
            else:
                cell.value = value
            cell.alignment = Alignment(vertical="top", wrap_text=key in WRAP)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(vertical="center")
    if rows:
        ref = f"A1:{get_column_letter(len(columns))}{len(rows) + 1}"
        table = Table(displayName=table_name, ref=ref)
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        ws.add_table(table)
    ws.freeze_panes = "C2"


def to_xlsx(rows: list[dict], contacts: list[dict], filters: dict, generated_at: str) -> bytes:
    """The workbook as bytes: nothing is written to disk."""
    from openpyxl import Workbook
    from openpyxl.formatting.rule import ColorScaleRule
    from openpyxl.styles import Font

    wb = Workbook()
    leads_ws = wb.active
    leads_ws.title = "Leads"
    _sheet(leads_ws, LEAD_COLUMNS, [_lead_values(r) for r in rows], "Leads")
    score_col = "F"
    if rows:
        leads_ws.conditional_formatting.add(
            f"{score_col}2:{score_col}{len(rows) + 1}",
            ColorScaleRule(start_type="num", start_value=0, start_color="F8D7DA",
                           mid_type="num", mid_value=50, mid_color="FFF3CD",
                           end_type="num", end_value=100, end_color="D1E7DD"))

    by_id = {r["id"]: r for r in rows}
    contact_rows = []
    for p in contacts:
        company = by_id.get(p["company_id"]) or {}
        contact_rows.append({
            "company": company.get("name"),
            "star": "★" if company.get("starred") else "",
            "score": company.get("lead_score"),
            "service": ", ".join(company.get("service_tags") or []),
            "name": f"{p.get('first_name') or ''} {p.get('last_name') or ''}".strip()
                    or ("General inbox" if p.get("title") == "General inbox" else ""),
            "title": "" if p.get("title") == "General inbox" else p.get("title"),
            "email": p.get("email"),
            "email_status": EMAIL_STATUS.get(p.get("email_status") or "", p.get("email_status")),
            "phone": p.get("phone"),
            "status": PROSPECT_STATUS.get(p.get("status") or "", p.get("status")),
            "source": str(p.get("source") or "").replace("_", " "),
            "source_url": p.get("source_url"),
            "notes": p.get("personalization_notes"),
        })
    _sheet(wb.create_sheet("Contacts"), CONTACT_COLUMNS, contact_rows, "Contacts")

    about = wb.create_sheet("About")
    shown = {k: v for k, v in filters.items() if k != "sort"}
    lines = [
        ("Harvey lead export", None),
        ("Generated", generated_at),
        ("Filters", ", ".join(f"{k} = {v}" for k, v in shown.items()) or "none (every lead)"),
        ("Leads", len(rows)),
        ("Contacts", len(contact_rows)),
        ("", None),
        ("Lead status", None),
        *[(label, None) for _, label in STATUSES],
        ("", None),
        ("Email status", None),
        ("Verified", "Safe to email."),
        ("Catch-all (risky)", "The domain accepts everything; may bounce."),
        ("Unverified (never sent)", "Found or guessed but not confirmed; Harvey never emails these."),
        ("", None),
        ("Score", "0-100 against your lead_scoring criteria (budget 40%, premium 30%, closing 30%). "
                  "Judged from a web search; check before relying on it."),
        ("Star", "A lead worth chasing: starred by you, or by a score at or above the threshold."),
    ]
    for key, value in lines:
        about.append([key, value])
    about["A1"].font = Font(bold=True, size=14)
    for cell in ("A7", "A16"):
        about[cell].font = Font(bold=True)
    about.column_dimensions["A"].width = 26
    about.column_dimensions["B"].width = 90

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
