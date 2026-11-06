"""Score leads against what you need from a client, and star the best.

For each service with criteria in ``lead_scoring.criteria`` (keyed by the
tags in icp.service_tags), Claude looks the business up on the web (web
search is the call's only tool, see claude_runtime.build_command) and
rates three things from 0 to 10:

- budget: can it afford what the criteria ask (a monthly package size, say);
- premium: how established and premium the brand is;
- closing: how likely it is to buy, given what it lacks today and how
  reachable it is.

The score is 40% budget, 30% premium and 30% closing, as 0-100, with the
reasons and an estimated monthly budget. It's stored as a LEAD_SCORE
observation, cached on the company for sorting, and a score at or above
``star_at`` stars the company, unless the operator set its star.

These are judgements, not checked facts, so they only rank and star: no
email is sent and no contact is added because of a score.
"""

import json
import logging

from harvey.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("harvey.lead_scoring")

SIGNAL = "LEAD_SCORE"
CALL_TIMEOUT_SECONDS = 300
WEIGHTS = {"budget": 0.4, "premium": 0.3, "closing": 0.3}


def combine(budget, premium, closing) -> int:
    def part(v):
        try:
            return max(0.0, min(10.0, float(v)))
        except (TypeError, ValueError):
            return 0.0
    total = (WEIGHTS["budget"] * part(budget) + WEIGHTS["premium"] * part(premium)
             + WEIGHTS["closing"] * part(closing))
    return int(round(total * 10))


def _fact_sheet(company: dict) -> str:
    lines = [f"id: {company['id']}", f"name: {company.get('name')}",
             f"type: {str(company.get('industry') or '').replace('_', ' ')}",
             f"location: {company.get('location') or 'unknown'}"]
    for key, label in (("domain", "website"), ("phone", "phone"), ("description", "about")):
        if company.get(key):
            lines.append(f"{label}: {str(company[key])[:300]}")
    if company.get("facts"):
        lines.append(f"what Harvey saw on its site: {company['facts'][:400]}")
    return "\n".join(lines)


def build_prompt(service: str, criteria: str, companies: list[dict]) -> str:
    sheets = "\n\n".join(_fact_sheet(c) for c in companies)
    return f"""You are qualifying sales leads for a {service} agency. Use web search to look each
business up: its website, Google and Maps listings, social pages, number of branches, reviews,
how established it looks, and what it does for {service} today.

What makes a good client (the agency's own words):
{criteria}

The businesses:
{fence(sheets, "lead list")}

For each business rate, from 0 to 10:
- "budget": can it afford what the criteria ask? 10 = clearly yes, 0 = clearly not.
- "premium": how premium and established the brand is (branches, pricing, reputation, presence).
- "closing": how likely it is to buy soon: a real gap the agency fills, and someone reachable.
Base every rating on what you found; if you found almost nothing, rate low and say so.
- {UNTRUSTED_RULE}

Return JSON: {{"leads": [{{"id": "<id as given>", "budget": 0-10, "premium": 0-10, "closing": 0-10,
"est_monthly_budget": "<a range in the local currency, or unknown>",
"why": "<two short sentences: the evidence>", "risks": "<one short line, or empty>"}}]}}"""


async def _facts(state, company_ids: list[str]) -> dict[str, str]:
    """A line of what profiling saw on each company's site (ads, tech, booking)."""
    if not company_ids:
        return {}
    wanted = ("RUNNING_GOOGLE_ADS", "RUNNING_META_ADS", "TECH_STACK", "NO_ONLINE_BOOKING",
              "BLOG_STALE", "INCUMBENT_AGENCY", "SITE_PAGE_COUNT")
    marks = ", ".join("?" * len(company_ids))
    out: dict[str, list[str]] = {}
    async with state._connect() as db:
        cur = await db.execute(
            f"""SELECT company_id, signal_code, value_num, value_text FROM observations
                WHERE company_id IN ({marks}) AND signal_code IN ({', '.join('?' * len(wanted))})
                ORDER BY observed_at""", (*company_ids, *wanted))
        latest = {}
        for cid, code, num, text in await cur.fetchall():
            latest[(cid, code)] = (num, text)
    for (cid, code), (num, text) in latest.items():
        value = text or ("yes" if num == 1 else "no" if num == 0 else num)
        out.setdefault(cid, []).append(f"{code.lower()}={value}")
    return {cid: "; ".join(v) for cid, v in out.items()}


async def score_companies(brain, state, config, service: str, companies: list[dict]) -> list[dict] | None:
    """Score up to batch_size companies for one service. None when Claude
    gave no answer (paused, out of quota)."""
    criteria = (config.lead_scoring.criteria or {}).get(service, "").strip()
    if not criteria or not companies:
        return []
    facts = await _facts(state, [c["id"] for c in companies])
    for c in companies:
        c["facts"] = facts.get(c["id"], "")
    raw = await brain.think(build_prompt(service, criteria, companies)
                            + "\n\nRespond ONLY with valid JSON.",
                            agent="lead_scoring", task="score_leads", web_search=True,
                            timeout=CALL_TIMEOUT_SECONDS, max_retries=1)
    if not raw:
        return None
    parsed = brain._extract_json(raw)
    leads = parsed.get("leads") if isinstance(parsed, dict) else parsed
    known = {c["id"]: c for c in companies}
    results = []
    for item in leads if isinstance(leads, list) else []:
        if not isinstance(item, dict) or item.get("id") not in known:
            continue
        detail = {
            "service": service,
            "budget": item.get("budget"), "premium": item.get("premium"),
            "closing": item.get("closing"),
            "est_monthly_budget": str(item.get("est_monthly_budget") or "")[:80],
            "why": str(item.get("why") or "")[:400],
            "risks": str(item.get("risks") or "")[:200],
        }
        detail["score"] = combine(detail["budget"], detail["premium"], detail["closing"])
        await record(state, config, item["id"], detail)
        results.append({"id": item["id"], "name": known[item["id"]].get("name"), **detail})
    return results


async def record(state, config, company_id: str, detail: dict):
    """Store a score, cache it on the company, and star a high one unless
    the operator has set this company's star."""
    score = int(detail["score"])
    star_at = config.lead_scoring.star_at
    await state.add_observation(SIGNAL, company_id=company_id, collector="lead_scoring",
                                value_num=float(score),
                                value_text=json.dumps(detail, ensure_ascii=False))
    async with state._connect() as db:
        await db.execute("UPDATE companies SET lead_score = ?, updated_at = CURRENT_TIMESTAMP "
                         "WHERE id = ?", (score, company_id))
        await db.execute(
            "UPDATE companies SET starred = ?, starred_by = 'score' "
            "WHERE id = ? AND COALESCE(starred_by, '') IN ('', 'score')",
            (1 if score >= star_at else 0, company_id))
        await db.commit()


async def set_star(state, company_id: str, starred: bool) -> bool:
    """The operator's star: scoring never changes it afterwards."""
    async with state._connect() as db:
        cur = await db.execute(
            "UPDATE companies SET starred = ?, starred_by = 'you', "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?", (1 if starred else 0, company_id))
        await db.commit()
        return cur.rowcount > 0


async def add_note(state, company_id: str, text: str, author: str = "you") -> bool:
    """Append a dated note to the company."""
    from datetime import datetime, timezone
    text = " ".join(str(text or "").split())[:1000]
    if not text:
        return False
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    line = f"[{stamp} {author}] {text}"
    async with state._connect() as db:
        cur = await db.execute(
            "UPDATE companies SET notes = CASE WHEN COALESCE(notes, '') = '' THEN ? "
            "ELSE notes || char(10) || ? END, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (line, line, company_id))
        await db.commit()
        return cur.rowcount > 0


async def companies_to_score(state, config, service: str, limit: int,
                             location: str = "") -> list[dict]:
    """Unscored companies tagged with this service: reachable ones first
    (website, phone or a contact), then focus order; skipped categories and
    chains left out."""
    cats = [str(c).lower() for c in (config.icp.service_tags or {}).get(service, [])]
    if not cats or limit <= 0:
        return []
    skip = [s.lower() for s in config.icp.skip_categories or []]
    focus = [f.lower() for f in config.icp.focus_categories or []]
    rank = "".join(f" WHEN ? THEN {i}" for i in range(len(focus)))
    order = f"CASE LOWER(c.industry){rank} ELSE {len(focus)} END, " if focus else ""
    skip_sql = f" AND LOWER(c.industry) NOT IN ({', '.join('?' * len(skip))})" if skip else ""
    loc_sql, loc = (" AND c.location LIKE ?", [f"%{location.strip()}%"]) if location.strip() else ("", [])
    sql = f"""
        WITH names AS (SELECT LOWER(TRIM(name)) AS n, COUNT(*) AS k FROM companies GROUP BY 1)
        SELECT c.id, c.name, c.industry, c.location, c.domain, c.phone, c.description
        FROM companies c JOIN names ON names.n = LOWER(TRIM(c.name))
        WHERE c.lead_score IS NULL AND COALESCE(c.name, '') != '' AND names.k < 3
          AND LOWER(c.industry) IN ({', '.join('?' * len(cats))}){skip_sql}{loc_sql}
        ORDER BY (COALESCE(c.domain, '') != '' OR COALESCE(c.phone, '') != ''
                  OR EXISTS (SELECT 1 FROM prospects p WHERE p.company_id = c.id)) DESC,
                 {order}c.created_at
        LIMIT ?"""
    async with state._connect() as db:
        cur = await db.execute(sql, (*cats, *skip, *loc, *focus, int(limit)))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in await cur.fetchall()]


async def run_lead_scoring(state, brain, config, limit: int | None = None,
                           service: str | None = None, gated: bool = True,
                           location: str = "") -> list[dict]:
    """Score a batch per service. ``gated``: only while LEAD_SCORE is
    confirmed (the heartbeat); a command or the chat passes False."""
    if getattr(brain, "provider", "claude") != "claude":
        return []
    if gated and SIGNAL not in await state.confirmed_signal_codes():
        return []
    ls = config.lead_scoring
    limit = ls.per_cycle if limit is None else limit
    services = [service] if service else list(ls.criteria or {})
    results = []
    for svc in services:
        todo = await companies_to_score(state, config, svc, limit, location=location)
        for i in range(0, len(todo), ls.batch_size):
            batch = await score_companies(brain, state, config, svc, todo[i:i + ls.batch_size])
            if batch is None:
                logger.warning(f"Lead scoring stopped: {brain.paused_reason or 'no answer from Claude'}")
                return results
            results.extend(batch)
            for r in batch:
                logger.info(f"Lead score: {r['name']} — {r['score']} ({svc})")
    return results
