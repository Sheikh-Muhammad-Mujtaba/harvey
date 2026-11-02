"""Contact details for companies Harvey couldn't reach, found with Claude's web search.

When Harvey has a company but no email it can send to (the site named no
one and published no address, or there is no site at all), one Claude call
with web search as its only tool (see claude_runtime.build_command) looks
for the business's website, phone, email, WhatsApp, address and social
pages. Nothing it says is used on trust:

- it must say the match is "exact", or nothing is kept;
- a website counts only if Harvey reads it and it names the business, and
  it's saved only on a company with no website yet (the normal pipeline
  then reads it like any other);
- a phone or email counts only if Harvey reads the page Claude cited and
  finds it there, next to the business's name;
- an email becomes a contact only if the email verifier says it can be
  delivered to. A personal mailbox (Gmail, Outlook...) usually means a sole
  trader, so it becomes one only when the Precleared consent check is on.

A phone or email that can't be checked stays on the card marked unconfirmed:
never written to the company, never emailed. Everything goes on the company
as one CONTACT_DETAILS observation (the contact card the Companies tab shows), a confirmed phone also fills the
company's phone if empty, and each lookup is logged as a ``contact_lookup``
action, which is how later runs skip companies already tried. Runs only
while the CONTACT_DETAILS signal is confirmed.
"""

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

from harvey.models.prospect import COMPANY_INBOX_SOURCE, Prospect
from harvey.public_http import public_http_client
from harvey.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("harvey.contact_lookup")

SIGNAL = "CONTACT_DETAILS"
ACTION = "contact_lookup"
PAGE_BYTES = 1_500_000
LOOKUP_TIMEOUT_SECONDS = 180
DELIVERABLE = ("verified", "risky")
CHAIN_MIN = 3  # the same name this many times is a chain: skipped

_PHONE_RE = re.compile(r"^\+?[\d\s().\-/]{7,24}$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
FREE_MAIL = re.compile(
    r"@(gmail|googlemail|hotmail|outlook|live|msn|yahoo|ymail|icloud|me|aol|proton|protonmail"
    r"|gmx|zoho|yandex|mail|btinternet|sky|virginmedia|talktalk)\.", re.IGNORECASE)
SOCIAL = {"facebook": "facebook.com", "instagram": "instagram.com", "linkedin": "linkedin.com"}
# Listings and aggregators: never a business's own website.
DIRECTORIES = (
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "tiktok.com",
    "youtube.com", "google.com", "goo.gl", "yelp.com", "tripadvisor", "yell.com", "192.com",
    "yellowpages", "bayut.com", "propertyfinder", "rightmove", "zoopla", "opentable",
    "deliveroo", "talabat", "zomato", "ubereats", "just-eat", "justeat", "restaurantguru",
    "mapcarta", "mapquest", "foursquare", "dnb.com", "kompass", "opencorporates",
    "companieshouse", "find-and-update.company-information", "endole", "cylex", "hotfrog",
    "freeindex", "thomsonlocal", "scoot.co.uk", "brownbook", "infobel", "nhs.uk", "gov.uk",
    "wikipedia.org", "trustpilot", "doctify", "okadoc", "practo", "fresha", "treatwell",
    "dubizzle", "gumtree", "amazon.", "noon.com", "connect.ae", "2gis", "waze.com",
)
# Words that don't identify a business: legal forms and generic trade words.
_NAME_STOP = {
    "the", "and", "llc", "l.l.c", "ltd", "limited", "plc", "inc", "fze", "fzco", "fzc", "est",
    "co", "company", "group", "general", "trading", "branch", "uk", "uae", "services",
}


@dataclass
class ContactCard:
    company_id: str
    name: str
    match: str = ""
    website: str = ""
    website_saved: bool = False
    phone: str = ""
    phone_confirmed: bool = False
    phone_saved: bool = False
    email: str = ""
    email_confirmed: bool = False
    email_status: str = ""
    prospect_added: bool = False
    whatsapp: str = ""
    address: str = ""
    socials: dict = field(default_factory=dict)
    description: str = ""
    sources: dict = field(default_factory=dict)
    note: str = ""
    _pages: dict = field(default_factory=dict, repr=False)

    def found_anything(self) -> bool:
        return bool(self.website or self.phone or self.email
                    or self.whatsapp or self.address or self.socials)


# ── Checking what Claude found ──

def digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def clean_phone(raw) -> str:
    """The number as a person would dial it, or "" if it isn't one."""
    phone = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not _PHONE_RE.match(phone):
        return ""
    return phone if 7 <= len(digits(phone)) <= 15 else ""


def found_on_page(phone: str, page: str) -> bool:
    """Do the number's digits appear on the page? Spaces, dots, dashes, tel:
    links, a country code or a leading 0 don't make it a different number."""
    want, have = digits(phone), digits(page)
    if not want or not have:
        return False
    tail = want[-8:] if len(want) >= 9 else want
    return tail in have


def name_words(name: str) -> list[str]:
    """The words of a business name that identify it."""
    return [w for w in re.findall(r"[a-z0-9]+", (name or "").lower())
            if len(w) >= 3 and w not in _NAME_STOP]


def names_business(name: str, page: str) -> bool:
    """Does the page mention the business by name (any identifying word)?"""
    low = (page or "").lower()
    words = name_words(name)
    return bool(words) and any(re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", low)
                               for w in words)


def clean_url(raw) -> str:
    url = str(raw or "").strip()
    if url and not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    return url if parsed.scheme in ("http", "https") and "." in (parsed.hostname or "") else ""


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def is_directory(domain: str) -> bool:
    return any(d in domain for d in DIRECTORIES)


def clean_email(raw) -> str:
    email = str(raw or "").strip().lower().removeprefix("mailto:")
    return email if _EMAIL_RE.match(email) and len(email) <= 120 else ""


async def fetch_page(url: str) -> str:
    """The page's HTML, or "" when it can't be read."""
    try:
        async with public_http_client(timeout=20) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; Harvey)"})
        return r.text[:PAGE_BYTES] if r.status_code < 400 else ""
    except Exception as e:
        logger.debug(f"Couldn't read {url}: {type(e).__name__}")
        return ""


# ── The lookup ──

def build_prompt(company: dict) -> str:
    facts = [f"Business name: {company.get('name')}",
             f"Location: {company.get('location') or 'unknown'}"]
    if company.get("industry"):
        facts.append(f"Type of business: {str(company['industry']).replace('_', ' ')}")
    if company.get("domain"):
        facts.append(f"Website: {company['domain']}")
    return f"""Find this business's public contact details, using web search.

{fence(chr(10).join(facts), "company record")}

Rules:
- Only report what you saw in your search results. For the phone and the email, give the URL
  of the result that shows it (the business's own page, its listing or a directory page). If
  you only saw it in a search summary, give the URL of the result it came from.
- It must be this exact business: same name, same type of business, same city. A different
  organisation that shares the name (a transport network, a chain, a namesake elsewhere) is not it.
- "match" is "exact" only when you found pages clearly about this business. If you had to
  assume or guess, it is "unsure".
- website: the business's own site, whenever any result shows it (even one you didn't open).
  Never a directory, map or social media listing.
- email: a general business address (info@, sales@, the owner's) published by the business.
- Leave any field empty you couldn't find. Never invent or complete a phone or email.
- {UNTRUSTED_RULE}

Return JSON:
{{"match": "exact" or "unsure",
  "website": "", "phone": "", "phone_source": "", "email": "", "email_source": "",
  "whatsapp": "", "address": "", "facebook": "", "instagram": "", "linkedin": "",
  "description": "<one plain sentence: what the business does>",
  "note": "<a few words on what you found>"}}"""


async def _ask(brain, company: dict) -> dict | None:
    raw = await brain.think(build_prompt(company) + "\n\nRespond ONLY with valid JSON.",
                            agent="contact_lookup", task="find_contacts", web_search=True,
                            timeout=LOOKUP_TIMEOUT_SECONDS, max_retries=1)
    if not raw:
        return None
    parsed = brain._extract_json(raw)
    return parsed if isinstance(parsed, dict) else {}


OWN_PAGES = ("", "contact", "contact-us", "about", "about-us")


async def _evidence(card: ContactCard, company: dict, source: str) -> list[tuple[str, str]]:
    """(url, html) pages a phone or email may be checked against: the page
    Claude cited, then the business's own site (home, contact, about)."""
    pages, seen = [], set()
    site = card.website or (f"https://{company['domain']}/" if company.get("domain") else "")
    urls = [source] if source else []
    if site:
        base = site if site.endswith("/") else site + "/"
        urls += [base + path for path in OWN_PAGES]
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        if url not in card._pages:
            card._pages[url] = await fetch_page(url)
        if card._pages[url]:
            pages.append((url, card._pages[url]))
    return pages


async def look_up(brain, state, config, env, company: dict) -> ContactCard | None:
    """Find, check and record one company's contact details. None when
    Claude gave no answer at all (paused, out of quota): not counted as
    tried, so a later run comes back to it."""
    card = ContactCard(company_id=company["id"], name=company.get("name") or "")
    answer = await _ask(brain, company)
    if answer is None:
        return None
    card.match = str(answer.get("match") or "").lower()
    card.note = str(answer.get("note") or "")[:200]
    if card.match != "exact":
        card.note = card.note or "not sure which business it is"
        return await _record(state, card)

    await _check_website(state, company, card, answer)
    await _check_phone(state, company, card, answer)
    await _check_email(state, config, env, company, card, answer)
    card.whatsapp = clean_phone(answer.get("whatsapp"))
    card.address = str(answer.get("address") or "").strip()[:200]
    for key, host in SOCIAL.items():
        url = clean_url(answer.get(key))
        if url and domain_of(url).endswith(host):
            card.socials[key] = url
    card.description = str(answer.get("description") or "").strip()[:300]
    if card.description and not (company.get("description") or "").strip():
        await _update_company(state, company["id"], "description", card.description)
    return await _record(state, card)


async def _check_website(state, company, card, answer):
    url = clean_url(answer.get("website"))
    domain = domain_of(url) if url else ""
    if not domain or is_directory(domain):
        return
    page = await fetch_page(url)
    if not page or not names_business(card.name, page):
        return
    card.website = url
    card.sources["website"] = url
    if (company.get("domain") or "").strip():
        return
    async with state._connect() as db:
        cur = await db.execute("SELECT 1 FROM companies WHERE domain = ?", (domain,))
        if await cur.fetchone():
            return  # another company on file already has this site
        await db.execute(
            "UPDATE companies SET domain = ?, website = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND COALESCE(domain, '') = ''", (domain, url, card.company_id))
        await db.commit()
    card.website_saved = True


async def _check_phone(state, company, card, answer):
    card.phone = clean_phone(answer.get("phone"))
    if not card.phone:
        return
    for url, page in await _evidence(card, company, clean_url(answer.get("phone_source"))):
        if found_on_page(card.phone, page) and names_business(card.name, page):
            card.phone_confirmed = True
            card.sources["phone"] = url
            break
    if not card.phone_confirmed:
        return
    if not (company.get("phone") or "").strip():
        await _update_company(state, card.company_id, "phone", card.phone)
        card.phone_saved = True


async def _check_email(state, config, env, company, card, answer):
    from harvey.integrations.email_finder import verify_published_address

    card.email = clean_email(answer.get("email"))
    if not card.email:
        return
    source = ""
    for url, page in await _evidence(card, company, clean_url(answer.get("email_source"))):
        if card.email in page.lower() and names_business(card.name, page):
            source = url
            break
    if not source:
        return
    card.email_confirmed = True
    card.sources["email"] = source
    try:
        card.email_status = await verify_published_address(card.email, env=env)
    except Exception as e:
        logger.debug(f"Email check failed for a found address: {e}")
        card.email_status = "guess"
    if card.email_status not in DELIVERABLE:
        return
    precleared = getattr(getattr(config, "compliance", None), "precleared", None)
    if FREE_MAIL.search(card.email) and not getattr(precleared, "enabled", False):
        card.note = (card.note + " Personal mailbox: kept on the card, not emailed "
                     "(turn on the Precleared check to email these).").strip()
        return
    facts = [card.description] if card.description else []
    if not (company.get("domain") or "").strip() and not card.website_saved:
        facts.append("No website of its own: its map listing has none, and a web search "
                     "found only listings and social pages.")
    prospect = Prospect(
        email=card.email, email_status=card.email_status,
        email_verified=(card.email_status == "verified"),
        company=card.name, company_id=card.company_id, title="General inbox",
        industry=company.get("industry") or "", source=COMPANY_INBOX_SOURCE,
        source_url=source, personalization_notes=" ".join(facts)[:600],
    )
    await state.add_prospect(prospect)
    card.prospect_added = True


async def _update_company(state, company_id: str, column: str, value: str):
    assert column in ("phone", "description")
    async with state._connect() as db:
        await db.execute(
            f"UPDATE companies SET {column} = ?, updated_at = CURRENT_TIMESTAMP "
            f"WHERE id = ? AND COALESCE({column}, '') = ''", (value, company_id))
        await db.commit()


def card_dict(card: ContactCard) -> dict:
    """The card as stored and shown (without the page cache)."""
    details = asdict(card)
    details.pop("_pages", None)
    return details


async def _record(state, card: ContactCard) -> ContactCard:
    details = card_dict(card)
    try:
        if card.found_anything():
            await state.add_observation(
                SIGNAL, company_id=card.company_id, collector=ACTION,
                value_text=json.dumps(details, ensure_ascii=False),
                evidence_url=next(iter(card.sources.values()), ""),
                confidence=1.0 if (card.phone_confirmed or card.email_confirmed
                                   or card.website) else 0.6)
        await state.log_action(ACTION, ACTION, details)
    except Exception as e:
        logger.warning(f"Couldn't record the contact lookup for {card.name}: {e}")
    return card


# ── Which companies, and the batch ──

async def companies_to_look_up(state, limit: int, focus=(), skip=()) -> list[dict]:
    """Companies Harvey has no deliverable email for and hasn't looked up:
    first those whose own site was already searched for people without
    luck, then those with no website, focus categories first in each,
    skipped categories and chains left out."""
    focus = [f.lower() for f in focus if f]
    skip = [s.lower() for s in skip if s]
    rank = "".join(f" WHEN ? THEN {i}" for i in range(len(focus)))
    order = f"CASE LOWER(c.industry){rank} ELSE {len(focus)} END, " if focus else ""
    skip_sql = f" AND LOWER(c.industry) NOT IN ({', '.join('?' * len(skip))})" if skip else ""
    sql = f"""
        WITH names AS (SELECT LOWER(TRIM(name)) AS n, COUNT(*) AS k FROM companies GROUP BY 1)
        SELECT c.id, c.name, c.location, c.domain, c.phone, c.industry, c.description
        FROM companies c JOIN names ON names.n = LOWER(TRIM(c.name))
        WHERE COALESCE(c.name, '') != '' AND names.k < {CHAIN_MIN}{skip_sql}
          AND (c.prospects_checked_at IS NOT NULL OR COALESCE(c.domain, '') = '')
          AND NOT EXISTS (SELECT 1 FROM prospects p WHERE p.company_id = c.id
                          AND p.email_status IN ('verified', 'risky'))
          AND NOT EXISTS (SELECT 1 FROM actions a WHERE a.action_type = '{ACTION}'
                          AND json_extract(a.details_json, '$.company_id') = c.id)
        ORDER BY COALESCE(c.domain, '') = '', {order}c.created_at
        LIMIT ?"""
    async with state._connect() as db:
        cur = await db.execute(sql, (*skip, *focus, int(limit)))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in await cur.fetchall()]


async def run_contact_lookup(state, brain, config, env, limit: int | None = None) -> list[ContactCard]:
    """One batch, for the heartbeat or `harvey contacts`. Does nothing unless
    the CONTACT_DETAILS signal is confirmed and the brain is Claude."""
    if getattr(brain, "provider", "claude") != "claude":
        return []
    if SIGNAL not in await state.confirmed_signal_codes():
        return []
    if limit is None:
        limit = config.usage.contact_lookups_per_cycle
    if limit <= 0:
        return []
    companies = await companies_to_look_up(
        state, limit, focus=config.icp.focus_categories, skip=config.icp.skip_categories)
    cards = []
    for company in companies:
        card = await look_up(brain, state, config, env, company)
        if card is None:
            logger.warning(f"Contact lookup stopped: {brain.paused_reason or 'no answer from Claude'}")
            break
        cards.append(card)
        logger.info(f"Contacts: {card.name} — {describe(card)}")
    return cards


def describe(card: ContactCard) -> str:
    if card.match != "exact":
        return f"skipped ({card.note or 'not sure which business it is'})"
    parts = []
    if card.website:
        parts.append("website" + (" saved" if card.website_saved else ""))
    if card.phone_confirmed:
        parts.append("phone" + (" saved" if card.phone_saved else ""))
    elif card.phone:
        parts.append("phone (unconfirmed)")
    if card.email_confirmed:
        parts.append(f"email {card.email_status}" + (", contact added" if card.prospect_added else ""))
    elif card.email:
        parts.append("email (unconfirmed, not emailed)")
    if card.socials:
        parts.append("social pages")
    return ", ".join(parts) or "nothing confirmed"
