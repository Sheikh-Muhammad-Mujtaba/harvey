"""The people to talk to at a lead, found with Claude's web search.

Contact lookup (contact_lookup.py) finds a business's own phone, email and
website. This finds the people inside it who would decide on your service:
the owner, the marketing head for marketing, the finance or operations head
for ERP (``people_research.roles``, else ``icp.titles``). One Claude call
with web search as its only tool per company. Nothing it says is used on
trust:

- it must say the match is "exact", or nothing is kept;
- a person becomes a contact only when Harvey reads the page they were
  found on (or the company's own team, about or contact pages) and sees
  their full name there, on a page that names the business or is the
  business's own site;
- their email is the one such a page publishes, else one learned from the
  company's address pattern and verified (email_finder.find_email); only a
  verifier-confirmed address is ever sent to;
- LinkedIn is never read (its terms forbid scraping): someone seen only
  there stays on the company's people card, unconfirmed, for you to check.

Everything goes on the company as one KEY_PEOPLE observation (the people
card on the Companies tab), and each research is logged as a
``people_research`` action, which is how later runs skip companies already
done. The heartbeat runs it only while the KEY_PEOPLE signal is confirmed;
the dashboard's background job and Ask Harvey run it when asked.
"""

import json
import logging
import re
from dataclasses import asdict, dataclass, field

from harvey import service_tags
from harvey.contact_lookup import (
    CHAIN_MIN, DELIVERABLE, FREE_MAIL, clean_email, clean_phone, clean_url, domain_of,
    fetch_page, found_on_page, is_directory, names_business,
)
from harvey.models.prospect import Prospect
from harvey.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("harvey.people_research")

SIGNAL = "KEY_PEOPLE"
ACTION = "people_research"
SOURCE = "people_research"
CALL_TIMEOUT_SECONDS = 240
# The company's own pages a name may be checked against.
OWN_PAGES = ("about", "about-us", "team", "our-team", "leadership", "management", "contact",
             "contact-us", "")
NOT_READ = ("linkedin.com",)   # never fetched, whatever Claude cites
ROLES = {"buyer": "Signs off on buying.", "champion": "Feels the problem day to day.",
         "both": "Signs off and feels the problem.", "other": ""}
_NAME_PART = re.compile(r"^[^\W\d_][^\W\d_'.\-]*(?:['.\-][^\W\d_]+)*\.?$")


@dataclass
class PeopleCard:
    company_id: str
    name: str
    service: str = ""
    match: str = ""
    people: list = field(default_factory=list)
    note: str = ""
    _pages: dict = field(default_factory=dict, repr=False)

    @property
    def added(self) -> int:
        return sum(1 for p in self.people if p.get("added"))


# ── Who to look for ──

def roles_for(service: str, config) -> list[str]:
    roles = dict(getattr(getattr(config, "people_research", None), "roles", None) or {})
    if service and roles.get(service):
        return [str(r) for r in roles[service]]
    return [str(t) for t in (getattr(getattr(config, "icp", None), "titles", None) or [])]


def service_for(company: dict, config, service: str = "") -> str:
    if service:
        return service
    tags = service_tags.tags_for(company.get("industry") or "", config)
    return tags[0] if tags else ""


def clean_name(raw) -> str:
    part = re.sub(r"\s+", " ", str(raw or "")).strip()
    return part if part and len(part) <= 40 and all(_NAME_PART.match(w) for w in part.split()) else ""


def name_on_page(first: str, last: str, page: str) -> bool:
    """First and last name together on the page (a middle name or initial
    between them is fine), as words, ignoring case and tags."""
    text = re.sub(r"<[^>]+>", " ", page or "")
    text = re.sub(r"&nbsp;|\s+", " ", text)
    f, l = re.escape(first.split()[0]), re.escape(last.split()[-1])
    pattern = rf"(?<!\w){f}(?:\s+[\w.'\-]+){{0,2}}\s+{l}(?!\w)"
    return re.search(pattern, text, re.IGNORECASE) is not None


# ── The research ──

def build_prompt(company: dict, service: str, roles: list[str], max_people: int) -> str:
    facts = [f"Business name: {company.get('name')}",
             f"Location: {company.get('location') or 'unknown'}"]
    if company.get("industry"):
        facts.append(f"Type of business: {str(company['industry']).replace('_', ' ')}")
    if company.get("domain"):
        facts.append(f"Website: {company['domain']}")
    want = ", ".join(roles) if roles else "owner, founder, managing director"
    pitch = f" about {service}" if service else ""
    return f"""Find the people at this business who would decide on buying{pitch}, using web search.

{fence(chr(10).join(facts), "company record")}

Look for (best first): {want}. The owner or managing director also counts.
At most {max_people} people. Try to find two kinds of person, since deals with both close far more
often: the one who signs (owner, MD, CEO, finance head: "buyer") and the one who feels the problem
every day and would push for it (the marketing, operations or IT lead: "champion"). In a small
business they're often the same person ("both").

Rules:
- Only report people you saw named in your search results, with the URL of the page that names
  them at this business (the business's own team or about page, a news article, an interview,
  a company register, a LinkedIn profile...). Never guess a name.
- It must be this exact business: same name, same type of business, same city. "match" is
  "exact" only when you found pages clearly about this business; otherwise "unsure".
- Only people who work there now, as far as the pages show.
- email and phone: only if a page shows that address or number for that person; give its URL.
  Leave them empty otherwise. Never build an email from a pattern.
- linkedin: the person's LinkedIn profile URL if a search result showed it.
- {UNTRUSTED_RULE}

Return JSON:
{{"match": "exact" or "unsure",
  "people": [{{"first_name": "", "last_name": "", "title": "", "source_url": "",
              "email": "", "phone": "", "contact_source_url": "", "linkedin": "",
              "role": "buyer" | "champion" | "both" | "other",
              "why": "<a few words: why this person decides>"}}],
  "note": "<a few words on what you found>"}}"""


async def _ask(brain, prompt: str) -> dict | None:
    raw = await brain.think(prompt + "\n\nRespond ONLY with valid JSON.",
                            agent="people_research", task="find_people", web_search=True,
                            timeout=CALL_TIMEOUT_SECONDS, max_retries=1)
    if not raw:
        return None
    parsed = brain._extract_json(raw)
    return parsed if isinstance(parsed, dict) else {}


async def _page(card: PeopleCard, url: str) -> str:
    if not url or any(d in domain_of(url) for d in NOT_READ):
        return ""
    if url not in card._pages:
        card._pages[url] = await fetch_page(url)
    return card._pages[url]


async def _evidence(card: PeopleCard, company: dict, sources: list[str]) -> list[tuple[str, str]]:
    """(url, html) pages that may confirm a person: the cited pages, then the
    company's own team, about and contact pages."""
    domain = (company.get("domain") or "").strip().lower()
    urls = [u for u in sources if u]
    if domain:
        urls += [f"https://{domain}/{path}" for path in OWN_PAGES]
    out, seen = [], set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        page = await _page(card, url)
        if not page:
            continue
        own = bool(domain) and domain_of(url).endswith(domain)
        if own or names_business(card.name, page):
            out.append((url, page))
    return out


async def research(brain, state, config, env, company: dict, service: str = "") -> PeopleCard | None:
    """Find, check and record the people at one company. None when Claude
    gave no answer (paused, out of quota): not counted as done, so a later
    run comes back to it."""
    service = service_for(company, config, service)
    max_people = config.people_research.people_per_company
    card = PeopleCard(company_id=company["id"], name=company.get("name") or "", service=service)
    answer = await _ask(brain, build_prompt(company, service, roles_for(service, config), max_people))
    if answer is None:
        return None
    card.match = str(answer.get("match") or "").lower()
    card.note = str(answer.get("note") or "")[:200]
    people = answer.get("people") if isinstance(answer.get("people"), list) else []
    if card.match != "exact":
        card.note = card.note or "not sure which business it is"
        return await _record(state, card)
    existing = await _known_people(state, company["id"])
    named = []   # (first, last) of everyone reported, so no guessed email lands on a colleague
    for raw in people[:max_people]:
        if isinstance(raw, dict):
            first, last = clean_name(raw.get("first_name")), clean_name(raw.get("last_name"))
            if first and last:
                named.append((first, last, raw))
    for first, last, raw in named:
        person = await _check(card, state, config, env, company, first, last, raw, named, existing)
        card.people.append(person)
    return await _record(state, card)


async def _known_people(state, company_id: str) -> set[tuple[str, str]]:
    async with state._connect() as db:
        cur = await db.execute("SELECT LOWER(first_name), LOWER(last_name) FROM prospects "
                               "WHERE company_id = ? AND first_name != ''", (company_id,))
        return {(f or "", l or "") for f, l in await cur.fetchall()}


async def _check(card, state, config, env, company, first, last, raw, named, existing) -> dict:
    from harvey.integrations.email_finder import find_email, verify_published_address

    source = clean_url(raw.get("source_url"))
    linkedin = clean_url(raw.get("linkedin"))
    if linkedin and not domain_of(linkedin).endswith("linkedin.com"):
        linkedin = ""
    if source and domain_of(source).endswith("linkedin.com"):
        linkedin, source = linkedin or source, ""
    role = str(raw.get("role") or "").strip().lower()
    person = {"name": f"{first} {last}", "title": str(raw.get("title") or "").strip()[:120],
              "role": role if role in ROLES else "other",
              "why": str(raw.get("why") or "").strip()[:200], "source": source,
              "linkedin": linkedin, "confirmed": False, "confirmed_on": "", "email": "",
              "email_status": "", "phone": "", "added": False, "prospect_id": ""}
    contact_source = clean_url(raw.get("contact_source_url"))
    pages = await _evidence(card, company, [source, contact_source])
    proof = next((url for url, page in pages if name_on_page(first, last, page)), "")
    if not proof:
        person["note"] = ("seen only on LinkedIn: check it yourself" if linkedin and not source
                          else "name not found on a page Harvey could read")
        return person
    person["confirmed"], person["confirmed_on"] = True, proof
    if (first.lower(), last.lower()) in existing:
        person["note"] = "already a contact"
        return person

    email = clean_email(raw.get("email"))
    if email and any(email in page.lower() for _, page in pages):
        precleared = getattr(getattr(config, "compliance", None), "precleared", None)
        if not FREE_MAIL.search(email) or getattr(precleared, "enabled", False):
            try:
                person["email_status"] = await verify_published_address(email, env=env)
            except Exception:
                person["email_status"] = "guess"
            person["email"] = email
    domain = (company.get("domain") or "").strip().lower()
    if not person["email"] and domain and not is_directory(domain):
        try:
            others = [(f, l) for f, l, _ in named if (f, l) != (first, last)]
            result = await find_email(first, last, domain, env=env, state=state, other_people=others)
            if result.email and result.status != "invalid":
                person["email"], person["email_status"] = result.email, result.status
        except Exception as e:
            logger.debug(f"Email lookup failed for a researched person: {type(e).__name__}")
    phone = clean_phone(raw.get("phone"))
    if phone and any(found_on_page(phone, page) for _, page in pages):
        person["phone"] = phone

    notes = [f"{person['title']} at {card.name}." if person["title"] else "",
             ROLES.get(person["role"], ""),
             person["why"], f"Found by web search; named on {proof}."]
    prospect = Prospect(
        first_name=first, last_name=last, title=person["title"] or "Decision maker",
        email=person["email"], email_status=person["email_status"],
        email_verified=person["email_status"] == "verified", phone=person["phone"],
        linkedin_url=linkedin, company=card.name, company_id=card.company_id,
        industry=company.get("industry") or "", source=SOURCE, source_url=proof,
        personalization_notes=" ".join(n for n in notes if n)[:600])
    person["prospect_id"] = await state.add_prospect(prospect)
    person["added"] = True
    existing.add((first.lower(), last.lower()))
    return person


def card_dict(card: PeopleCard) -> dict:
    details = asdict(card)
    details.pop("_pages", None)
    return details


async def _record(state, card: PeopleCard) -> PeopleCard:
    details = card_dict(card)
    try:
        if card.people:
            await state.add_observation(
                SIGNAL, company_id=card.company_id, collector=ACTION,
                value_text=json.dumps(details, ensure_ascii=False),
                evidence_url=next((p["confirmed_on"] for p in card.people if p["confirmed_on"]), ""),
                confidence=1.0 if card.added else 0.5)
        await state.log_action(ACTION, ACTION, details)
    except Exception as e:
        logger.warning(f"Couldn't record the people research for {card.name}: {e}")
    return card


# ── Which companies, and the heartbeat batch ──

async def companies_to_research(state, config, limit: int, service: str = "", location: str = "",
                                min_score: int | None = None, starred_only: bool = False) -> list[dict]:
    """Companies not researched yet: starred first, then best score, then
    those with a website. ``service`` keeps only its categories; skipped
    categories and chains are left out."""
    if limit <= 0:
        return []
    where, params = [], []
    if service:
        cats = [str(c).lower() for c in (config.icp.service_tags or {}).get(service, [])]
        if not cats:
            return []
        where.append(f"LOWER(c.industry) IN ({', '.join('?' * len(cats))})")
        params += cats
    skip = [s.lower() for s in config.icp.skip_categories or []]
    if skip:
        where.append(f"LOWER(COALESCE(c.industry, '')) NOT IN ({', '.join('?' * len(skip))})")
        params += skip
    if location.strip():
        where.append("c.location LIKE ?")
        params.append(f"%{location.strip()}%")
    if min_score:
        where.append("c.lead_score >= ?")
        params.append(int(min_score))
    if starred_only:
        where.append("c.starred = 1")
    extra = "".join(" AND " + w for w in where)
    sql = f"""
        WITH names AS (SELECT LOWER(TRIM(name)) AS n, COUNT(*) AS k FROM companies GROUP BY 1)
        SELECT c.id, c.name, c.location, c.domain, c.phone, c.industry, c.description
        FROM companies c JOIN names ON names.n = LOWER(TRIM(c.name))
        WHERE COALESCE(c.name, '') != '' AND names.k < {CHAIN_MIN}{extra}
          AND NOT EXISTS (SELECT 1 FROM actions a WHERE a.action_type = '{ACTION}'
                          AND json_extract(a.details_json, '$.company_id') = c.id)
        ORDER BY COALESCE(c.starred, 0) DESC, c.lead_score IS NULL, c.lead_score DESC,
                 COALESCE(c.domain, '') = '', c.created_at
        LIMIT ?"""
    async with state._connect() as db:
        cur = await db.execute(sql, (*params, int(limit)))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in await cur.fetchall()]


async def run_people_research(state, brain, config, env, limit: int | None = None) -> list[PeopleCard]:
    """One heartbeat batch: only while KEY_PEOPLE is confirmed and the brain
    is Claude, starred and best-scored leads first."""
    if getattr(brain, "provider", "claude") != "claude":
        return []
    if SIGNAL not in await state.confirmed_signal_codes():
        return []
    pr = config.people_research
    limit = pr.per_cycle if limit is None else limit
    companies = await companies_to_research(state, config, limit, min_score=pr.min_score or None)
    cards = []
    for company in companies:
        card = await research(brain, state, config, env, company)
        if card is None:
            logger.warning(f"People research stopped: {brain.paused_reason or 'no answer from Claude'}")
            break
        cards.append(card)
        logger.info(f"People: {card.name} — {describe(card)}")
    return cards


def describe(card: PeopleCard) -> str:
    if card.match != "exact":
        return f"skipped ({card.note or 'not sure which business it is'})"
    if not card.people:
        return "no one named"
    unconfirmed = sum(1 for p in card.people if not p["confirmed"])
    parts = [f"{card.added} contact(s) added"]
    if unconfirmed:
        parts.append(f"{unconfirmed} unconfirmed")
    return ", ".join(parts)
