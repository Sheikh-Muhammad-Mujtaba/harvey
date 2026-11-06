"""The deal pipeline: where each company stands, from first lead to won or lost.

A deal is a company. Its stage is worked out when read, from two things:

- what Harvey has seen happen: a high score or a star makes it *qualified*,
  an email sent makes it *contacted*, a reply makes it *replied*, and the
  conversation's own stage carries it on (closing → *meeting*,
  negotiating → *negotiation*, closed_won → *won*, closed_lost → *lost*);
- a stage you set by hand (``companies.deal_stage``), for what happens off
  email: a call booked, a proposal sent, a deal signed.

The later of the two wins, so a deal never slides back because Harvey's view
is behind yours, and Harvey's progress shows without you moving cards. Won and
lost are final: set by hand, they stay. Moving a card back by hand is how you
correct a mistake (it's kept, and only later activity moves it on again).
"""

from harvey import service_tags

STAGES = [
    ("lead", "Lead"),
    ("qualified", "Qualified"),
    ("contacted", "Contacted"),
    ("replied", "Replied"),
    ("meeting", "Meeting"),
    ("proposal", "Proposal sent"),
    ("negotiation", "Negotiation"),
    ("won", "Won"),
    ("lost", "Lost"),
]
KEYS = [k for k, _ in STAGES]
LABELS = dict(STAGES)
FINAL = ("won", "lost")
ORDER = {k: i for i, k in enumerate(KEYS)}

# The conversation's sales stage (models/conversation.py) → deal stage.
CONVERSATION = {
    "engaged": "replied", "qualifying": "replied", "presenting": "replied",
    "closing": "meeting", "negotiating": "negotiation",
    "closed_won": "won", "closed_lost": "lost",
}


def auto_stage(row: dict, star_at: int) -> str:
    """What Harvey's own activity says about a company."""
    stages = [CONVERSATION[s] for s in (row.get("conv_stages") or "").split(",") if s in CONVERSATION]
    if "won" in stages:
        return "won"
    best = max(stages, key=ORDER.get, default="")
    if best and best != "lost":
        return best
    if row.get("replied"):
        return "replied"
    if row.get("contacted"):
        return "contacted"
    if best == "lost":
        return "lost"
    score = row.get("lead_score")
    if row.get("starred") or (score is not None and score >= star_at):
        return "qualified"
    return "lead"


def stage_of(row: dict, star_at: int) -> tuple[str, str]:
    """(stage, why): the stage shown, and whether it was set by you or Harvey."""
    manual = row.get("deal_stage") or ""
    auto = auto_stage(row, star_at)
    if manual in FINAL:
        return manual, "you"
    if auto == "won":
        return auto, "harvey"
    if manual and (auto == "lost" or ORDER[manual] >= ORDER[auto]):
        return manual, "you"
    return auto, "harvey"


_SQL = """
SELECT c.id, c.name, c.industry, c.location, c.domain, c.phone, c.lead_score,
       COALESCE(c.starred, 0) AS starred, COALESCE(c.deal_stage, '') AS deal_stage,
       c.deal_stage_at, c.deal_value, COALESCE(c.deal_lost_reason, '') AS deal_lost_reason,
       c.updated_at,
       MAX(p.status IN ('replied', 'meeting', 'closed', 'closed_won')) AS replied,
       MAX(p.status = 'contacted') AS contacted,
       GROUP_CONCAT(DISTINCT cv.stage) AS conv_stages,
       (SELECT TRIM(p2.first_name || ' ' || p2.last_name) FROM prospects p2
         WHERE p2.company_id = c.id AND p2.first_name != ''
         ORDER BY p2.email_status IN ('verified', 'risky') DESC, p2.created_at LIMIT 1) AS contact,
       (SELECT COUNT(*) FROM tasks t WHERE t.company_id = c.id AND t.done = 0) AS open_tasks,
       (SELECT MIN(t.due_date) FROM tasks t WHERE t.company_id = c.id AND t.done = 0
         AND t.due_date != '') AS next_due
FROM companies c
LEFT JOIN prospects p ON p.company_id = c.id
LEFT JOIN conversations cv ON cv.prospect_id = p.id
WHERE {where}
GROUP BY c.id
"""
# Companies that are on the board at all: anything past a plain lead, plus
# leads you put there yourself.
_ON_BOARD = """(COALESCE(c.deal_stage, '') != '' OR COALESCE(c.starred, 0) = 1
    OR c.lead_score >= ?
    OR EXISTS (SELECT 1 FROM prospects px WHERE px.company_id = c.id
               AND px.status IN ('contacted', 'replied', 'meeting', 'closed', 'closed_won')))"""


def _star_at(config) -> int:
    return int(getattr(getattr(config, "lead_scoring", None), "star_at", 75) or 75)


async def _rows(state, config, where: str, params: list) -> list[dict]:
    async with state._connect() as db:
        cur = await db.execute(_SQL.format(where=where), params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in await cur.fetchall()]
    star_at = _star_at(config)
    for r in rows:
        r["stage"], r["stage_by"] = stage_of(r, star_at)
        r["stage_label"] = LABELS[r["stage"]]
        r["service_tags"] = service_tags.tags_for(r.get("industry") or "", config)
        r.pop("conv_stages", None)
    return rows


async def board(state, config, service: str = "", q: str = "") -> dict:
    """Every deal on the board, grouped by stage, with counts and value."""
    where, params = _ON_BOARD, [_star_at(config)]
    if service:
        cats = [str(c).lower() for c in
                (getattr(getattr(config, "icp", None), "service_tags", None) or {}).get(service, [])]
        if not cats:
            return {"stages": [{"key": k, "label": l, "deals": [], "count": 0, "value": 0}
                               for k, l in STAGES], "total": 0}
        where += f" AND LOWER(c.industry) IN ({', '.join('?' * len(cats))})"
        params += cats
    if q.strip():
        where += " AND (c.name LIKE ? OR c.domain LIKE ? OR c.location LIKE ?)"
        params += [f"%{q.strip()}%"] * 3
    rows = await _rows(state, config, where, params)
    columns = []
    for key, label in STAGES:
        deals = sorted((r for r in rows if r["stage"] == key),
                       key=lambda r: (-(r["lead_score"] or -1), r["name"] or ""))
        columns.append({"key": key, "label": label, "deals": deals, "count": len(deals),
                        "value": sum(r["deal_value"] or 0 for r in deals)})
    return {"stages": columns, "total": len(rows)}


async def deal(state, config, company_id: str) -> dict | None:
    rows = await _rows(state, config, "c.id = ?", [company_id])
    return rows[0] if rows else None


async def set_stage(state, company_id: str, stage: str, reason: str = "",
                    value=None) -> bool:
    """Set a deal's stage by hand ('' hands it back to Harvey). Lost needs a
    reason: it's what shows you, later, why deals die."""
    if stage and stage not in KEYS:
        raise ValueError(f"unknown stage {stage!r}; one of {', '.join(KEYS)}")
    reason = " ".join(str(reason or "").split())[:300]
    if stage == "lost" and not reason:
        raise ValueError("say why it was lost")
    sets, params = ["deal_stage = ?", "deal_stage_at = CURRENT_TIMESTAMP",
                    "deal_lost_reason = ?"], [stage, reason if stage == "lost" else ""]
    if value is not None:
        sets.append("deal_value = ?")
        params.append(parse_value(value))
    async with state._connect() as db:
        cur = await db.execute(f"UPDATE companies SET {', '.join(sets)} WHERE id = ?",
                               (*params, company_id))
        await db.commit()
        return cur.rowcount > 0


async def set_value(state, company_id: str, value) -> bool:
    async with state._connect() as db:
        cur = await db.execute("UPDATE companies SET deal_value = ? WHERE id = ?",
                               (parse_value(value), company_id))
        await db.commit()
        return cur.rowcount > 0


def parse_value(value) -> float | None:
    """A money amount as typed: 150000, "150,000", "1.5 lakh", "150k", "" (none)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        amount = float(value)
    else:
        text = str(value).strip().lower().replace(",", "")
        if not text:
            return None
        scale = 1
        for word, mult in (("lakh", 100_000), ("lac", 100_000), ("crore", 10_000_000),
                           ("k", 1_000), ("m", 1_000_000)):
            if text.endswith(word):
                scale, text = mult, text[: -len(word)].strip()
                break
        try:
            amount = float(text) * scale
        except ValueError:
            raise ValueError("value must be a number, e.g. 150000 or 1.5 lakh") from None
    if amount < 0:
        raise ValueError("value can't be negative")
    return round(amount, 2)
