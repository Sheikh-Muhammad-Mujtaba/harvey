"""Contact lookup (harvey/contact_lookup.py): web-search-only Claude calls whose
findings are kept only once checked, and which companies get looked up."""

import asyncio
import json
import sqlite3
import types

import pytest

import harvey.integrations.email_finder as email_finder
from harvey import claude_runtime, contact_lookup as cl
from harvey.brain import Brain
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager

SITE = "https://acmetiles.example.com/"
LISTING = "https://listing.example.org/acme-tiles"


class FakeBrain:
    provider = "claude"
    paused_reason = ""
    _extract_json = staticmethod(Brain._extract_json)

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    async def think(self, prompt, **kw):
        self.calls.append(kw)
        if not self.answers:
            return ""
        a = self.answers.pop(0)
        return a if isinstance(a, str) else json.dumps(a)


def _config(precleared=False, per_cycle=3, focus=(), skip=()):
    return types.SimpleNamespace(
        compliance=types.SimpleNamespace(precleared=types.SimpleNamespace(enabled=precleared)),
        usage=types.SimpleNamespace(contact_lookups_per_cycle=per_cycle),
        icp=types.SimpleNamespace(focus_categories=list(focus), skip_categories=list(skip)))


@pytest.fixture
def state(tmp_path, monkeypatch):
    s = StateManager(db_path=str(tmp_path / "harvey.db"))
    asyncio.run(s.init_db())
    asyncio.run(seed_signal_catalog(s))
    pages = {
        SITE: "<h1>Acme Tiles</h1> Exampleton showroom. Call 0161 555 0101. info@acmetiles.example.com",
        LISTING: "Acme Tiles, Exampleton. Tel +44 161 555 0101. Email: info@acmetiles.example.com",
        "https://other.example.net/": "<h1>Something else entirely</h1>",
    }

    async def fetch_page(url):
        return pages.get(url, "")
    monkeypatch.setattr(cl, "fetch_page", fetch_page)

    async def verify(email, env=None):
        return "invalid" if email.startswith("bounce") else "verified"
    monkeypatch.setattr(email_finder, "verify_published_address", verify)
    return s


def _add(state, cid, name="Acme Tiles", domain="", phone="", industry="tiles", checked=False):
    con = sqlite3.connect(state.db_path)
    con.execute("INSERT INTO companies (id, name, location, domain, phone, industry, prospects_checked_at) "
                "VALUES (?, ?, 'Exampleton', ?, ?, ?, ?)",
                (cid, name, domain, phone, industry, "2026-01-01 00:00:00" if checked else None))
    con.commit()
    con.close()
    return {"id": cid, "name": name, "location": "Exampleton", "domain": domain, "phone": phone,
            "industry": industry, "description": ""}


def _row(state, sql, *args):
    return sqlite3.connect(state.db_path).execute(sql, args).fetchone()


GOOD = {"match": "exact", "website": SITE, "phone": "+44 161 555 0101", "phone_source": LISTING,
        "email": "info@acmetiles.example.com", "email_source": LISTING, "whatsapp": "+44 7700 900123",
        "address": "1 Example Street, Exampleton", "facebook": "https://facebook.com/acmetiles",
        "instagram": "https://evil.example.com/x", "description": "Sells and fits tiles."}


def _look(state, company, answer, config=None):
    return asyncio.run(cl.look_up(FakeBrain([answer]), state, config or _config(), None, company))


def test_web_search_is_the_only_tool_and_only_when_asked():
    plain = claude_runtime.build_command("claude")
    assert plain[plain.index("--tools") + 1] == "" and "--allowedTools" not in plain
    web = claude_runtime.build_command("claude", web_search=True)
    assert web[web.index("--tools") + 1] == "WebSearch"
    assert web[web.index("--allowedTools") + 1] == "WebSearch"


def test_confirmed_details_fill_the_company_and_add_a_contact(state):
    card = _look(state, _add(state, "c1"), GOOD)
    assert card.website_saved and card.phone_saved and card.prospect_added
    assert _row(state, "SELECT domain, phone, description FROM companies WHERE id='c1'") == (
        "acmetiles.example.com", "+44 161 555 0101", "Sells and fits tiles.")
    assert _row(state, "SELECT email, email_status, source FROM prospects WHERE company_id='c1'") == (
        "info@acmetiles.example.com", "verified", "company_inbox")
    # Socials only on their own sites; the card is stored as an observation.
    assert card.socials == {"facebook": "https://facebook.com/acmetiles"}
    obs = json.loads(_row(state, "SELECT value_text FROM observations WHERE signal_code='CONTACT_DETAILS'")[0])
    assert obs["whatsapp"] == "+44 7700 900123" and obs["address"].startswith("1 Example")


def test_unsure_match_keeps_nothing(state):
    card = _look(state, _add(state, "c1"), {**GOOD, "match": "unsure"})
    assert not card.found_anything()
    assert _row(state, "SELECT domain, phone FROM companies WHERE id='c1'") == ("", "")
    assert _row(state, "SELECT COUNT(*) FROM observations WHERE signal_code='CONTACT_DETAILS'")[0] == 0
    assert _row(state, "SELECT COUNT(*) FROM actions WHERE action_type='contact_lookup'")[0] == 1


@pytest.mark.parametrize("website", [
    "https://www.facebook.com/acmetiles",       # a listing, not their own site
    "https://other.example.net/",               # doesn't name the business
    "https://nowhere.example.net/",             # can't be read
])
def test_unconfirmed_website_is_not_saved(state, website):
    card = _look(state, _add(state, "c1"), {**GOOD, "website": website})
    assert not card.website and not card.website_saved
    assert _row(state, "SELECT domain FROM companies WHERE id='c1'")[0] == ""


def test_website_another_company_has_is_not_taken(state):
    _add(state, "c0", name="Acme Tiles Head Office", domain="acmetiles.example.com")
    card = _look(state, _add(state, "c1"), GOOD)
    assert card.website and not card.website_saved


def test_phone_not_on_its_source_page_is_not_kept(state):
    card = _look(state, _add(state, "c1"), {**GOOD, "phone": "+44 161 555 0199"})
    assert not card.phone_confirmed and _row(state, "SELECT phone FROM companies WHERE id='c1'")[0] == ""


def test_existing_details_are_never_overwritten(state):
    company = _add(state, "c1", domain="acme-old.example.com", phone="0161 555 0100", checked=True)
    card = _look(state, company, GOOD)
    assert not card.website_saved and not card.phone_saved
    assert _row(state, "SELECT domain, phone FROM companies WHERE id='c1'") == (
        "acme-old.example.com", "0161 555 0100")


def test_email_not_on_its_source_page_is_not_kept(state):
    card = _look(state, _add(state, "c1"), {**GOOD, "email": "sales@acmetiles.example.com"})
    assert not card.email_confirmed and not card.prospect_added


def test_undeliverable_email_is_not_a_contact(state, monkeypatch):
    async def fetch_page(url):
        return "Acme Tiles, Exampleton. bounce@acmetiles.example.com"
    monkeypatch.setattr(cl, "fetch_page", fetch_page)
    card = _look(state, _add(state, "c1"), {**GOOD, "website": "", "phone": "",
                                            "email": "bounce@acmetiles.example.com"})
    assert card.email_confirmed and card.email_status == "invalid" and not card.prospect_added


def test_personal_mailbox_needs_the_consent_check(state, monkeypatch):
    pages = {LISTING: "Acme Tiles, Exampleton. Tel +44 161 555 0101. acmetiles@gmail.com"}

    async def fetch_page(url):
        return pages.get(url, "")
    monkeypatch.setattr(cl, "fetch_page", fetch_page)
    answer = {**GOOD, "website": "", "email": "acmetiles@gmail.com"}
    card = _look(state, _add(state, "c1"), answer)
    assert card.email_confirmed and not card.prospect_added and "Personal mailbox" in card.note
    card = _look(state, _add(state, "c2", name="Acme Tiles Two"), answer, _config(precleared=True))
    assert card.prospect_added


def test_which_companies_get_looked_up(state):
    _add(state, "deliverable", name="Acme Done", domain="done.example.com", checked=True)
    con = sqlite3.connect(state.db_path)
    con.execute("INSERT INTO prospects (id, company_id, email, email_status, title, source) "
                "VALUES ('p1', 'deliverable', 'a@done.example.com', 'verified', 'Owner', 'x')")
    con.commit()
    con.close()
    _add(state, "unchecked", name="Acme Fresh", domain="fresh.example.com")        # Scout goes first
    _add(state, "tried_site", name="Acme Quiet", domain="quiet.example.com", checked=True)
    _add(state, "no_site", name="Acme Corner")
    _add(state, "skipped", name="Acme Hospital", industry="hospital")
    for i in range(3):
        _add(state, f"chain{i}", name="Bigchain")
    picked = [c["id"] for c in asyncio.run(cl.companies_to_look_up(state, 10, skip=["hospital"]))]
    assert picked == ["tried_site", "no_site"]
    asyncio.run(cl.look_up(FakeBrain([{"match": "unsure"}]), state, _config(), None,
                           {"id": "tried_site", "name": "Acme Quiet"}))
    picked = [c["id"] for c in asyncio.run(cl.companies_to_look_up(state, 10, skip=["hospital"]))]
    assert picked == ["no_site"]


def test_batch_runs_only_when_the_signal_is_confirmed(state):
    _add(state, "c1")
    brain = FakeBrain([GOOD])
    assert asyncio.run(cl.run_contact_lookup(state, brain, _config(), None)) == []
    _confirm(state)
    cards = asyncio.run(cl.run_contact_lookup(state, brain, _config(), None))
    assert len(cards) == 1 and brain.calls[0]["web_search"] is True


def test_no_answer_stops_the_batch_and_isnt_counted(state):
    _confirm(state)
    _add(state, "c1")
    _add(state, "c2", name="Acme Other")
    assert asyncio.run(cl.run_contact_lookup(state, FakeBrain([]), _config(), None)) == []
    assert len(asyncio.run(cl.companies_to_look_up(state, 10))) == 2


def _confirm(state):
    con = sqlite3.connect(state.db_path)
    con.execute("UPDATE signal_codes SET status = 'confirmed' WHERE code = 'CONTACT_DETAILS'")
    con.commit()
    con.close()


@pytest.mark.parametrize("raw,ok", [
    ("+44 161 555 0101", True), ("(0161) 555-0101", True), ("call us", False), ("12345", False),
])
def test_clean_phone(raw, ok):
    assert bool(cl.clean_phone(raw)) is ok


def test_page_checks():
    assert cl.found_on_page("+44 161 555 0101", '<a href="tel:01615550101">0161 555 0101</a>')
    assert cl.names_business("Acme Trading LLC", "<h1>ACME</h1>")
    assert not cl.names_business("Acme Trading LLC", "General trading company LLC")
    assert cl.is_directory("www.yell.com") and not cl.is_directory("acmetiles.example.com")
    assert cl.clean_email("mailto:Info@Acme.Example.com") == "info@acme.example.com"
    assert cl.clean_email("not an email") == ""


def test_phone_without_a_source_is_checked_on_the_own_site(state):
    card = _look(state, _add(state, "c1"), {**GOOD, "phone": "0161 555 0101", "phone_source": ""})
    assert card.phone_confirmed and card.sources["phone"] == SITE


def test_unchecked_details_stay_on_the_card_marked_unconfirmed(state):
    card = _look(state, _add(state, "c1"), {"match": "exact", "phone": "+44 161 555 0199",
                                            "email": "owner@elsewhere.example.com"})
    assert card.phone and not card.phone_confirmed and not card.phone_saved
    assert card.email and not card.email_confirmed and not card.prospect_added
    assert "unconfirmed" in cl.describe(card)
    stored = json.loads(_row(state, "SELECT value_text FROM observations "
                                    "WHERE signal_code='CONTACT_DETAILS'")[0])
    assert stored["phone"] == "+44 161 555 0199" and "_pages" not in stored
    assert _row(state, "SELECT phone FROM companies WHERE id='c1'")[0] == ""
