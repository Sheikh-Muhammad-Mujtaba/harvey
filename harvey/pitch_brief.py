"""A pitch brief for one company: how to sell to them, before you call or write.

One Claude call (web search allowed, to check for anything recent) turns
what Harvey knows about a company (its record, signals, score and reasons,
contact card, the people found, notes, where the deal stands) and what you
sell into a plan:

- the approach that fits where the buyer is: help them see a problem they
  don't feel yet (discovery questions), reframe one they're comfortable
  living with, or differentiate when they're already comparing options;
- the angle to open with and why now, and what staying as they are costs them;
- your offer mapped to their situation, honest trust builders, the likely
  objections with answers;
- three to five discovery questions for the first call;
- one specific next step, an opening line, and a short touch plan.

Every claim about the business must come from the facts given or a page it
cites; what isn't known is listed as a gap rather than guessed. The latest
brief is kept per company (``briefs``), shown on the company, and its angle
is given to the writer for that company's emails.
"""

import json
import logging

from harvey import pipeline_board, service_tags
from harvey.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("harvey.pitch_brief")

CALL_TIMEOUT_SECONDS = 300
APPROACHES = {
    "discover": "They don't feel the problem yet: ask, don't pitch.",
    "reframe": "They know it but live with it: lead with an insight they haven't considered.",
    "evaluate": "They're comparing options: differentiate on what they care about and qualify.",
}
CHANNELS = ("email", "phone", "whatsapp", "linkedin", "visit")


async def _facts(state, config, company_id: str) -> tuple[dict, str, str]:
    """(company row, service, fact sheet)."""
    from harvey.lead_scoring import _facts as profile_facts

    async with state._connect() as db:
        db_row = await db.execute(
            "SELECT id, name, industry, location, domain, phone, description, notes "
            "FROM companies WHERE id = ?", (company_id,))
        cols = [d[0] for d in db_row.description]
        row = await db_row.fetchone()
        if not row:
            raise ValueError("unknown company")
        company = dict(zip(cols, row))
        cur = await db.execute(
            "SELECT signal_code, value_text FROM observations WHERE company_id = ? AND signal_code "
            "IN ('LEAD_SCORE', 'CONTACT_DETAILS') ORDER BY observed_at", (company_id,))
        latest = {code: text for code, text in await cur.fetchall()}
        cur = await db.execute(
            "SELECT first_name, last_name, title, email_status, personalization_notes FROM prospects "
            "WHERE company_id = ? AND status != 'opted_out' ORDER BY first_name = '', created_at LIMIT 8",
            (company_id,))
        people = await cur.fetchall()
    service = next(iter(service_tags.tags_for(company.get("industry") or "", config)), "")
    lines = [f"Business: {company['name']}",
             f"Type: {str(company.get('industry') or 'unknown').replace('_', ' ')}",
             f"Location: {company.get('location') or 'unknown'}",
             f"Website: {company.get('domain') or 'none known'}"]
    if company.get("description"):
        lines.append(f"What they say about themselves: {company['description'][:400]}")
    signals = (await profile_facts(state, [company_id])).get(company_id, "")
    if signals:
        lines.append(f"Signals from their site: {signals[:800]}")
    try:
        score = json.loads(latest.get("LEAD_SCORE") or "{}")
    except ValueError:
        score = {}
    if score:
        lines.append(f"Lead score {score.get('score')}/100 for {score.get('service') or service}: "
                     f"{score.get('why') or ''} Est. budget: {score.get('est_monthly_budget') or 'unknown'}. "
                     f"Risks: {score.get('risks') or 'none noted'}")
    try:
        card = json.loads(latest.get("CONTACT_DETAILS") or "{}")
    except ValueError:
        card = {}
    if card.get("socials"):
        lines.append(f"Social pages: {', '.join(card['socials'])}")
    for first, last, title, est, notes in people:
        name = f"{first} {last}".strip() or "General inbox"
        lines.append(f"Contact: {name}, {title or 'role unknown'}"
                     + (f" (email {est})" if est else "") + (f". {notes[:200]}" if notes else ""))
    if company.get("notes"):
        lines.append(f"Your notes: {company['notes'][-600:]}")
    deal = await pipeline_board.deal(state, config, company_id)
    if deal:
        lines.append(f"Where the deal stands: {deal['stage_label']}")
    return company, service, "\n".join(lines)


def _offer(config, service: str) -> str:
    product = getattr(config, "product", None)
    persona = getattr(config, "persona", None)
    lines = [f"Seller: {getattr(persona, 'company', '') or 'us'}"]
    if service:
        lines.append(f"Service to pitch: {service}")
    if product is not None:
        lines.append(f"Offer: {getattr(product, 'name', '')}: {getattr(product, 'description', '')}")
        if getattr(product, "pricing", ""):
            lines.append(f"Pricing: {product.pricing}")
        benefits = getattr(product, "key_benefits", None) or []
        if benefits:
            lines.append("Benefits: " + "; ".join(benefits[:6]))
        objections = getattr(product, "objection_responses", None) or {}
        if objections:
            lines.append("Known objections: " + "; ".join(list(objections)[:6]))
    return "\n".join(lines)


def build_prompt(fact_sheet: str, offer: str) -> str:
    return f"""Write a pitch brief: how to sell to this business, for the salesperson to read before
calling or writing. You may use web search to check for anything recent about the business (news,
openings, hiring, campaigns, reviews) that changes the angle.

{fence(fact_sheet, "company facts")}

What we sell (from our own config, trusted):
{offer}

Rules:
- Every claim about the business comes from the facts above or a page you found (list its URL in
  "sources"). Never invent numbers, clients, problems or events. Put what you don't know in "gaps".
- Pick the approach that fits where this buyer is: "discover" (they don't feel the problem yet: ask
  questions that surface it, don't pitch), "reframe" (they know it but live with it: lead with an
  insight about their business they haven't considered; the real competitor is doing nothing), or
  "evaluate" (they're comparing options: differentiate on what they care about, qualify budget,
  who decides, timing).
- Cost of inaction: what staying as they are costs them, concretely, only from evidence.
- Trust builders only where true: similar clients, proof, a small first step (audit, pilot).
  No fake urgency or scarcity.
- Discovery questions: open questions, problem then implication then payoff, not yes/no.
- Next step: one specific, low-friction action with who and how.
- Opening line: the first line of a first message, about them, not us. Under 30 words.
- Touch plan: 3-5 touches over about two weeks, each with a new angle, using email, phone,
  whatsapp, linkedin or visit.
- {UNTRUSTED_RULE}

Return JSON:
{{"approach": "discover" | "reframe" | "evaluate", "approach_why": "",
  "snapshot": "<two sentences: who they are and why they fit>",
  "angle": "<what to lead with and why now>",
  "cost_of_inaction": "",
  "value_map": ["<our offer → their situation>", ...],
  "trust": ["", ...],
  "objections": [{{"objection": "", "response": ""}}, ...],
  "discovery_questions": ["", ...],
  "who_to_talk_to": "<who signs, who feels the problem; from the contacts if known>",
  "next_step": "", "opening_line": "",
  "touches": [{{"day": 0, "channel": "email", "angle": ""}}, ...],
  "gaps": ["", ...], "sources": ["", ...]}}"""


def _clean(answer: dict) -> dict:
    def text(key, n=600):
        return str(answer.get(key) or "").strip()[:n]

    def items(key, n=6, size=300):
        values = answer.get(key) if isinstance(answer.get(key), list) else []
        return [str(v).strip()[:size] for v in values if str(v).strip()][:n]

    approach = text("approach", 20).lower()
    objections = [
        {"objection": str(o.get("objection") or "").strip()[:200],
         "response": str(o.get("response") or "").strip()[:400]}
        for o in (answer.get("objections") or []) if isinstance(o, dict) and o.get("objection")][:5]
    touches = []
    for t in answer.get("touches") or []:
        if not isinstance(t, dict) or not t.get("angle"):
            continue
        try:
            day = max(0, min(int(t.get("day") or 0), 60))
        except (TypeError, ValueError):
            day = 0
        channel = str(t.get("channel") or "email").lower()
        touches.append({"day": day, "channel": channel if channel in CHANNELS else "email",
                        "angle": str(t["angle"]).strip()[:240]})
    sources = [s for s in items("sources", 8, 400) if s.startswith(("http://", "https://"))]
    return {
        "approach": approach if approach in APPROACHES else "discover",
        "approach_why": text("approach_why", 300), "snapshot": text("snapshot"),
        "angle": text("angle"), "cost_of_inaction": text("cost_of_inaction"),
        "value_map": items("value_map"), "trust": items("trust", 4),
        "objections": objections, "discovery_questions": items("discovery_questions", 5),
        "who_to_talk_to": text("who_to_talk_to", 400), "next_step": text("next_step", 400),
        "opening_line": text("opening_line", 300),
        "touches": sorted(touches, key=lambda t: t["day"])[:5],
        "gaps": items("gaps", 5), "sources": sources,
    }


async def generate(brain, state, config, company_id: str) -> dict | None:
    """Write and keep a brief. None when Claude gave no answer."""
    company, service, fact_sheet = await _facts(state, config, company_id)
    raw = await brain.think(build_prompt(fact_sheet, _offer(config, service))
                            + "\n\nRespond ONLY with valid JSON.",
                            agent="pitch_brief", task="pitch_brief", web_search=True,
                            timeout=CALL_TIMEOUT_SECONDS, max_retries=1)
    if not raw:
        return None
    parsed = brain._extract_json(raw)
    if not isinstance(parsed, dict) or not (parsed.get("angle") or parsed.get("snapshot")):
        raise ValueError("Claude's answer wasn't a brief; try again")
    brief = _clean(parsed)
    brief["service"] = service
    async with state._connect() as db:
        await db.execute(
            "INSERT INTO briefs (company_id, service, content_json, created_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) ON CONFLICT(company_id) DO UPDATE SET "
            "service = excluded.service, content_json = excluded.content_json, "
            "created_at = excluded.created_at",
            (company_id, service, json.dumps(brief, ensure_ascii=False)))
        await db.commit()
    await state.log_action("pitch_brief", "pitch_brief",
                           {"company_id": company_id, "company": company["name"],
                            "approach": brief["approach"]})
    return {**brief, "company_id": company_id, "company": company["name"]}


async def latest(state, company_id: str) -> dict | None:
    async with state._connect() as db:
        cur = await db.execute("SELECT content_json, created_at FROM briefs WHERE company_id = ?",
                               (company_id,))
        row = await cur.fetchone()
    if not row:
        return None
    try:
        brief = json.loads(row[0])
    except ValueError:
        return None
    brief["created_at"] = row[1]
    return brief


async def angle_for(state, company_id: str) -> str:
    """The brief's angle, for the writer ('' when there's no brief)."""
    if not company_id:
        return ""
    try:
        brief = await latest(state, company_id)
    except Exception:
        return ""
    return (brief or {}).get("angle", "")[:300]
