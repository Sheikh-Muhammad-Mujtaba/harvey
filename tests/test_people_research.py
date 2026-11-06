"""People research (harvey/people_research.py): who decides at a lead, found
with web search and confirmed on pages Harvey reads; its background job,
dashboard endpoints and chat tools."""

import asyncio
import copy
import json
import sqlite3
import time
import types

import pytest
from fastapi.testclient import TestClient

import harvey.config as config_mod
import harvey.dashboard as dash
import harvey.integrations.email_finder as email_finder
from harvey import chat, people_research
from harvey.jobs import PeopleJob
from tests.test_leads import FakeBrain, _config, env as leads_env  # noqa: F401

TEAM_PAGE = """<html><h1>Acme Dental team</h1>
<div class="card"><h3>Dr. Zara <b>Q.</b> Sample</h3><p>Clinical director, Acme Dental</p></div>
<p>Marketing: Omar Example &ndash; omar@acmedental.example.com</p></html>"""
NEWS_PAGE = "<p>Acme Dental names a new head of growth, Lena Placeholder.</p>"
OTHER_PAGE = "<p>Bright Smiles hires Lena Placeholder.</p>"   # doesn't name Acme Dental


def _cfg():
    cfg = copy.deepcopy(_config())
    cfg.icp.titles = ["Owner"]
    cfg.people_research = types.SimpleNamespace(
        roles={"Digital marketing": ["Marketing manager", "Clinic owner"]},
        per_cycle=2, people_per_company=3, min_score=0)
    return cfg


@pytest.fixture
def env(leads_env, monkeypatch):
    pages = {"https://acmedental.example.com/team": TEAM_PAGE,
             "https://news.example.com/acme": NEWS_PAGE,
             "https://news.example.com/other": OTHER_PAGE}
    fetched = []

    async def fetch(url):
        fetched.append(url)
        return pages.get(url, "")

    async def verify(email, env=None):
        return "verified"

    async def find(first, last, domain, **kw):
        return email_finder.EmailResult(email=f"{first[0]}{last}@{domain}".lower(), status="risky")

    monkeypatch.setattr(people_research, "fetch_page", fetch)
    monkeypatch.setattr(email_finder, "verify_published_address", verify)
    monkeypatch.setattr(email_finder, "find_email", find)
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: _cfg())
    monkeypatch.setattr(config_mod, "load_env", lambda *a, **k: None)
    leads_env.fetched = fetched
    return leads_env


def _company(state, cid):
    con = sqlite3.connect(dash.DB_PATH)
    con.row_factory = sqlite3.Row
    row = dict(con.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone())
    con.close()
    return row


def _rows(sql, args=()):
    con = sqlite3.connect(dash.DB_PATH)
    rows = con.execute(sql, args).fetchall()
    con.close()
    return rows


ANSWER = {"match": "exact", "people": [
    {"first_name": "Zara", "last_name": "Sample", "title": "Clinical director",
     "source_url": "https://acmedental.example.com/team", "why": "Runs the clinic"},
    {"first_name": "Omar", "last_name": "Example", "title": "Marketing manager",
     "source_url": "https://news.example.com/missing", "email": "omar@acmedental.example.com",
     "why": "Owns marketing"},
    {"first_name": "Lena", "last_name": "Placeholder", "title": "Head of growth",
     "source_url": "https://www.linkedin.com/in/lena-placeholder"},
    {"first_name": "Too", "last_name": "Many", "title": "Fourth person"},
]}


def test_people_confirmed_on_a_page_become_contacts(env):
    brain = FakeBrain([ANSWER])
    card = asyncio.run(people_research.research(brain, env, _cfg(), None, _company(env, "d1")))
    assert brain.calls[0][1]["web_search"] is True
    prompt = brain.calls[0][0]
    assert "Marketing manager, Clinic owner" in prompt and '<untrusted source="company record">' in prompt
    by = {p["name"]: p for p in card.people}
    assert len(card.people) == 3                       # people_per_company
    zara, omar, lena = by["Zara Sample"], by["Omar Example"], by["Lena Placeholder"]
    # Zara: named on the cited team page (a middle initial in tags is fine);
    # no published email, so one is learned from the pattern.
    assert zara["added"] and zara["email"] == "zsample@acmedental.example.com"
    assert zara["email_status"] == "risky"
    # Omar: the cited page is gone, but the company's own team page names him
    # and publishes his address, which is verified rather than guessed.
    assert omar["added"] and omar["email"] == "omar@acmedental.example.com"
    assert omar["email_status"] == "verified"
    # Lena: only on LinkedIn, which is never read.
    assert not lena["confirmed"] and not lena["added"] and "LinkedIn" in lena["note"]
    assert lena["linkedin"] == "https://www.linkedin.com/in/lena-placeholder"
    assert not any("linkedin.com" in u for u in env.fetched)
    rows = _rows("SELECT first_name, source, email_status FROM prospects WHERE company_id='d1' "
                 "AND source = 'people_research' ORDER BY first_name")
    assert rows == [("Omar", "people_research", "verified"), ("Zara", "people_research", "risky")]
    obs = json.loads(_rows("SELECT value_text FROM observations WHERE signal_code='KEY_PEOPLE'")[0][0])
    assert obs["company_id"] == "d1" and len(obs["people"]) == 3 and "_pages" not in obs
    assert people_research.describe(card) == "2 contact(s) added, 1 unconfirmed"


def test_a_page_that_does_not_name_the_business_proves_nothing(env):
    brain = FakeBrain([{"match": "exact", "people": [
        {"first_name": "Lena", "last_name": "Placeholder", "title": "Head of growth",
         "source_url": "https://news.example.com/other"}]}])
    card = asyncio.run(people_research.research(brain, env, _cfg(), None, _company(env, "d1")))
    assert not card.people[0]["confirmed"] and card.added == 0
    brain = FakeBrain([{"match": "exact", "people": [
        {"first_name": "Lena", "last_name": "Placeholder", "title": "Head of growth",
         "source_url": "https://news.example.com/acme"}]}])
    card = asyncio.run(people_research.research(brain, env, _cfg(), None, _company(env, "d1")))
    assert card.people[0]["added"]


def test_known_contacts_and_unsure_matches_add_nothing(env):
    con = sqlite3.connect(dash.DB_PATH)
    con.execute("UPDATE prospects SET first_name='Zara', last_name='Sample' WHERE id='p1'")
    con.commit()
    con.close()
    card = asyncio.run(people_research.research(
        FakeBrain([{"match": "exact", "people": ANSWER["people"][:1]}]), env, _cfg(), None,
        _company(env, "d1")))
    assert card.people[0]["confirmed"] and card.people[0]["note"] == "already a contact"
    assert card.people[0]["email"] == ""             # no lookup spent on a known contact
    card = asyncio.run(people_research.research(
        FakeBrain([{"match": "unsure", "people": ANSWER["people"]}]), env, _cfg(), None,
        _company(env, "d2")))
    assert card.people == [] and card.note
    assert _rows("SELECT COUNT(*) FROM prospects WHERE source='people_research'")[0][0] == 0


def test_no_answer_is_not_counted_as_done(env):
    brain = FakeBrain([""])
    assert asyncio.run(people_research.research(brain, env, _cfg(), None, _company(env, "d1"))) is None
    todo = asyncio.run(people_research.companies_to_research(env, _cfg(), 10))
    assert "d1" in [c["id"] for c in todo]


def test_order_filters_and_skip_done(env):
    con = sqlite3.connect(dash.DB_PATH)
    con.execute("UPDATE companies SET lead_score = 60 WHERE id = 'r1'")
    con.execute("UPDATE companies SET lead_score = 90 WHERE id = 'd2'")
    con.execute("UPDATE companies SET starred = 1 WHERE id = 'w1'")
    con.commit()
    con.close()
    ids = lambda **kw: [c["id"] for c in asyncio.run(
        people_research.companies_to_research(env, _cfg(), 10, **kw))]
    assert ids()[:3] == ["w1", "d2", "r1"] and "h1" not in ids()     # hospital is skipped
    assert ids(service="Digital marketing") == ["d2", "r1", "d1"]
    assert ids(min_score=70) == ["d2"] and ids(starred_only=True) == ["w1"]
    assert ids(location="Sampleville") == ["d2"]
    asyncio.run(people_research.research(FakeBrain([{"match": "exact", "people": []}]), env,
                                         _cfg(), None, _company(env, "d2")))
    assert "d2" not in ids()


def test_name_on_page():
    assert people_research.name_on_page("Zara", "Sample", "<b>Dr. Zara</b> Q. Sample, owner")
    assert people_research.name_on_page("zara", "SAMPLE", "zara&nbsp;sample")
    assert not people_research.name_on_page("Zara", "Sample", "Zarasample and Sampleton")
    assert people_research.clean_name("O'Neil-Smith") and not people_research.clean_name("x@y.com")


def test_heartbeat_waits_for_the_signal(env):
    brain = FakeBrain([])
    assert asyncio.run(people_research.run_people_research(env, brain, _cfg(), None)) == []
    assert brain.calls == []


def _run_job(job, state, brain, **kw):
    async def go():
        job.start(state, _cfg(), lambda c: brain, **kw)
        await job.task
        return job.status()
    return asyncio.run(go())


def test_job_researches_up_to_the_limit(env):
    brain = FakeBrain([ANSWER, {"match": "exact", "people": []}, {"match": "unsure"}])
    s = _run_job(PeopleJob(), env, brain, service="Digital marketing", limit=2)
    assert s["researched"] == 2 and s["people_added"] == 2 and s["unconfirmed"] == 1
    assert s["stopped"] == "limit reached" and len(brain.calls) == 2
    assert s["top"][0]["people"][0]["name"]
    with pytest.raises(ValueError, match="unknown lead type"):
        PeopleJob().start(env, _cfg(), lambda c: brain, service="Plumbing")


def test_job_stops_when_claude_pauses_or_nothing_is_left(env):
    brain = FakeBrain([""])
    brain.paused_reason = "daily budget reached"
    assert _run_job(PeopleJob(), env, brain)["stopped"] == "daily budget reached"
    s = _run_job(PeopleJob(), env, FakeBrain([{"match": "unsure"}] * 10), location="Sampleville")
    assert s["researched"] == 1 and s["stopped"] == "nothing left to research"


def test_endpoints_and_chat_tools(env, monkeypatch):
    brain = FakeBrain([ANSWER])
    monkeypatch.setattr(dash, "_people_job", None)
    monkeypatch.setattr(dash, "_job_brain", lambda config: brain)
    auth = {"Authorization": f"Bearer {dash.token_store.get()}"}
    with TestClient(dash.app, base_url="http://127.0.0.1:5555") as c:
        assert c.post("/api/people/research/run", json={}).status_code == 401
        assert c.post("/api/people/research/run", json={"service": "Plumbing"},
                      headers=auth).status_code == 400
        r = c.post("/api/people/research/run", json={"limit": 1, "location": "Exampleton"},
                   headers=auth).json()
        assert r["success"] and r["limit"] == 1
        deadline = time.monotonic() + 10
        while c.get("/api/people/research", headers=auth).json()["running"]:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        s = c.get("/api/people/research", headers=auth).json()
        assert s["researched"] == 1 and s["people_added"] == 2
        rows = c.get("/api/leads?q=Acme%20Dental", headers=auth).json()["rows"]
        assert len(rows[0]["people_card"]["people"]) == 3
    for name in ("find_people", "start_people_research", "stop_people_research"):
        assert chat.TOOLS[name].acts
    assert not chat.TOOLS["people_research_status"].acts
