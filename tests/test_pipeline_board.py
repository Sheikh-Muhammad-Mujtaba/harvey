"""The deal board (pipeline_board.py), follow-ups (tasks.py), contact tiers
(leads.py), pitch briefs (pitch_brief.py), their endpoints and chat tools."""

import asyncio
import copy
import json
import sqlite3
import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey import chat, leads, pipeline_board, pitch_brief, tasks
from tests.test_leads import FakeBrain, _config, env  # noqa: F401  (fixture)


def _sql(sql, args=()):
    con = sqlite3.connect(dash.DB_PATH)
    con.execute(sql, args)
    con.commit()
    con.close()


def _conversation(cid, prospect, stage="engaged", status="open", last="prospect"):
    thread = [{"sender": "harvey", "content": "hi", "timestamp": "2026-01-01T09:00:00"},
              {"sender": last, "content": "hello", "timestamp": "2026-01-02T09:00:00"}]
    _sql("INSERT INTO conversations (id, prospect_id, stage, status, thread_json) VALUES (?, ?, ?, ?, ?)",
         (cid, prospect, stage, status, json.dumps(thread)))


def _board(state, **kw):
    b = asyncio.run(pipeline_board.board(state, _config(), **kw))
    return {c["key"]: [d["id"] for d in c["deals"]] for c in b["stages"]}, b


def _auth():
    return {"Authorization": f"Bearer {dash.token_store.get()}"}


# ── Stages ──

def test_stages_follow_what_harvey_sees(env):
    _sql("UPDATE companies SET starred = 1 WHERE id = 'w1'")
    _sql("UPDATE companies SET lead_score = 80 WHERE id = 'd2'")
    cols, b = _board(env)
    # d1: Ann was emailed (contacted). r1 only has a fresh inbox: not on the board.
    assert cols["contacted"] == ["d1"] and set(cols["qualified"]) == {"w1", "d2"}
    assert "r1" not in sum(cols.values(), []) and b["total"] == 3
    _conversation("cv1", "p1", stage="closing")
    assert _board(env)[0]["meeting"] == ["d1"]
    _sql("UPDATE conversations SET stage = 'closed_won' WHERE id = 'cv1'")
    assert _board(env)[0]["won"] == ["d1"]


def test_your_stage_wins_when_it_is_later_or_final(env):
    asyncio.run(pipeline_board.set_stage(env, "d1", "proposal", value="1.5 lakh"))
    deal = asyncio.run(pipeline_board.deal(env, _config(), "d1"))
    assert deal["stage"] == "proposal" and deal["stage_by"] == "you" and deal["deal_value"] == 150000
    # Earlier than what Harvey saw: Harvey's later stage shows.
    asyncio.run(pipeline_board.set_stage(env, "w1", "lead"))
    _sql("UPDATE prospects SET company_id = 'w1' WHERE id = 'p1'")
    assert asyncio.run(pipeline_board.deal(env, _config(), "w1"))["stage"] == "contacted"
    with pytest.raises(ValueError, match="why"):
        asyncio.run(pipeline_board.set_stage(env, "d2", "lost"))
    with pytest.raises(ValueError, match="unknown stage"):
        asyncio.run(pipeline_board.set_stage(env, "d2", "maybe"))
    asyncio.run(pipeline_board.set_stage(env, "d2", "lost", reason="Went with a cheaper agency"))
    _conversation("cv2", "p2", stage="closing")
    _sql("UPDATE prospects SET company_id = 'd2' WHERE id = 'p2'")
    deal = asyncio.run(pipeline_board.deal(env, _config(), "d2"))
    assert deal["stage"] == "lost" and deal["deal_lost_reason"] == "Went with a cheaper agency"
    asyncio.run(pipeline_board.set_stage(env, "d2", ""))          # back to automatic
    assert asyncio.run(pipeline_board.deal(env, _config(), "d2"))["stage"] == "meeting"


def test_a_no_on_email_is_lost_unless_you_say_otherwise(env):
    _conversation("cv1", "p1", stage="closed_lost", status="closed", last="harvey")
    assert asyncio.run(pipeline_board.deal(env, _config(), "d1"))["stage"] == "contacted"
    _sql("UPDATE prospects SET status = 'new' WHERE id = 'p1'")
    _sql("UPDATE companies SET deal_stage = 'qualified' WHERE id = 'd1'")
    assert asyncio.run(pipeline_board.deal(env, _config(), "d1"))["stage"] == "qualified"
    _sql("UPDATE companies SET deal_stage = '' WHERE id = 'd1'")
    assert asyncio.run(pipeline_board.deal(env, _config(), "d1"))["stage"] == "lost"


def test_values_and_filters(env):
    assert pipeline_board.parse_value("150,000") == 150000
    assert pipeline_board.parse_value("2 lac") == 200000 and pipeline_board.parse_value("75k") == 75000
    assert pipeline_board.parse_value("") is None and pipeline_board.parse_value(1200.5) == 1200.5
    for bad in ("lots", "-5"):
        with pytest.raises(ValueError):
            pipeline_board.parse_value(bad)
    asyncio.run(pipeline_board.set_stage(env, "w1", "qualified", value=50000))
    asyncio.run(pipeline_board.set_stage(env, "d2", "qualified", value="100k"))
    _, b = _board(env)
    qualified = next(c for c in b["stages"] if c["key"] == "qualified")
    assert qualified["count"] == 2 and qualified["value"] == 150000
    cols, _ = _board(env, service="Digital marketing")
    assert "w1" not in sum(cols.values(), [])
    assert _board(env, q="Bright")[0]["qualified"] == ["d2"]


# ── Tasks ──

def test_due_dates():
    today = date.today()
    assert tasks.parse_due("today") == today.isoformat()
    assert tasks.parse_due("in 3 days") == (today + timedelta(days=3)).isoformat()
    assert tasks.parse_due("2026-12-01") == "2026-12-01" and tasks.parse_due("") == ""
    with pytest.raises(ValueError):
        tasks.parse_due("whenever")
    assert tasks.describe_due(today.isoformat()) == "today"
    assert tasks.describe_due((today - timedelta(days=3)).isoformat()) == "3 days late"


def test_tasks_group_by_when_they_are_due(env):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    a = asyncio.run(tasks.add(env, "Call the owner", company_id="d1", due=yesterday))
    asyncio.run(tasks.add(env, "Send the audit", company_id="d1", due="today"))
    asyncio.run(tasks.add(env, "Check back", due="next week"))
    asyncio.run(tasks.add(env, "Someday"))
    with pytest.raises(ValueError, match="unknown company"):
        asyncio.run(tasks.add(env, "x", company_id="nope"))
    rows = asyncio.run(tasks.listing(env))
    assert [r["bucket"] for r in rows] == ["overdue", "today", "upcoming", "someday"]
    assert rows[0]["company_name"] == "Acme Dental"
    assert asyncio.run(tasks.counts(env)) == {"overdue": 1, "today": 1, "open": 4}
    asyncio.run(tasks.set_done(env, a))
    assert len(asyncio.run(tasks.listing(env))) == 3
    assert asyncio.run(tasks.listing(env, include_done=True))[-1]["bucket"] == "done"


def test_a_reply_makes_one_task_due_today(env):
    _conversation("cv1", "p1")                                   # Ann wrote back
    _conversation("cv2", "p2", status="needs_human")
    _conversation("cv3", "p1", last="harvey")                    # Harvey answered: nothing waits
    assert asyncio.run(tasks.sync_reply_tasks(env)) == 2
    assert asyncio.run(tasks.sync_reply_tasks(env)) == 0          # once per reply
    rows = asyncio.run(tasks.listing(env))
    titles = sorted(r["title"] for r in rows)
    assert titles[0].startswith("Harvey needs you") and "Ann Example at Acme Dental" in titles[1]
    assert all(r["bucket"] == "today" and r["source"] == "harvey" for r in rows)
    # A new reply in the same conversation makes a new task.
    thread = [{"sender": "prospect", "content": "again", "timestamp": "2026-01-05T09:00:00"}]
    _sql("UPDATE conversations SET thread_json = ? WHERE id = 'cv1'", (json.dumps(thread),))
    assert asyncio.run(tasks.sync_reply_tasks(env)) == 1


# ── Tiers ──

def test_contact_tiers(env):
    _sql("UPDATE companies SET phone = '+1 555 0101' WHERE id = 'w1'")
    _sql("INSERT INTO prospects (id, company_id, first_name, last_name, title, source) "
         "VALUES ('p3', 'd2', 'Zed', 'Sample', 'Owner', 'x')")
    rows = asyncio.run(leads.list_leads(env, {}, _config()))["rows"]
    tier = {r["id"]: r["tier"] for r in rows}
    assert tier == {"d1": "A", "d2": "B", "r1": "C", "w1": "C", "h1": "D"}
    picked = asyncio.run(leads.list_leads(env, {"tier": "ab"}, _config()))["rows"]
    assert {r["id"] for r in picked} == {"d1", "d2"}
    facets = asyncio.run(leads.facets(env, _config()))
    assert [t["value"] for t in facets["tiers"]] == ["A", "B", "C", "D"]
    assert "A: Named person, direct line" in leads.to_csv(rows)


# ── Pitch briefs ──

BRIEF = {"approach": "reframe", "approach_why": "Busy clinic, happy with referrals.",
         "snapshot": "Acme Dental is a busy clinic.", "angle": "Their booking page is buried.",
         "cost_of_inaction": "Missed bookings.", "value_map": ["Ads → more bookings"],
         "objections": [{"objection": "Too pricey", "response": "Start with an audit."}, {"bad": 1}],
         "discovery_questions": ["How do new patients find you?"], "next_step": "Offer a free audit",
         "opening_line": "Saw your booking page...", "who_to_talk_to": "Ann Example",
         "touches": [{"day": 5, "channel": "fax", "angle": "Case study"}, {"day": 0, "angle": "Audit"}],
         "gaps": ["Budget"], "sources": ["https://acmedental.example.com/", "not a url"]}


def test_brief_is_grounded_kept_and_feeds_the_writer(env):
    brain = FakeBrain([BRIEF])
    out = asyncio.run(pitch_brief.generate(brain, env, _config(), "d1"))
    prompt, kw = brain.calls[0]
    assert kw["web_search"] is True and '<untrusted source="company facts">' in prompt
    assert "Contact: Ann Example, Owner" in prompt and "Acme marketing" in prompt
    assert out["approach"] == "reframe" and len(out["objections"]) == 1
    assert [t["day"] for t in out["touches"]] == [0, 5] and out["touches"][1]["channel"] == "email"
    assert out["sources"] == ["https://acmedental.example.com/"] and out["service"] == "Digital marketing"
    kept = asyncio.run(pitch_brief.latest(env, "d1"))
    assert kept["angle"] == BRIEF["angle"] and kept["created_at"]
    assert asyncio.run(pitch_brief.angle_for(env, "d1")) == BRIEF["angle"]
    assert asyncio.run(pitch_brief.angle_for(env, "d2")) == ""
    assert asyncio.run(pitch_brief.generate(FakeBrain([""]), env, _config(), "d1")) is None
    with pytest.raises(ValueError):
        asyncio.run(pitch_brief.generate(FakeBrain([{"nothing": 1}]), env, _config(), "d1"))


# ── Endpoints and chat ──

def test_endpoints(env, monkeypatch):
    brain = FakeBrain([BRIEF])
    monkeypatch.setattr(dash, "_job_brain", lambda config: brain)
    monkeypatch.setattr(dash, "_brief_jobs", {})
    _sql("INSERT INTO observations (id, signal_code, company_id, collector, value_text, evidence_url) "
         "VALUES ('o1', 'NO_ONLINE_BOOKING', 'd1', 'profile', 'yes', 'https://acmedental.example.com/book')")
    _conversation("cv1", "p1")
    with TestClient(dash.app, base_url="http://127.0.0.1:5555") as c:
        assert c.get("/api/pipeline").status_code == 401
        board = c.get("/api/pipeline", headers=_auth()).json()
        assert next(s for s in board["stages"] if s["key"] == "replied")["count"] == 1
        r = c.post("/api/companies/d1/stage", json={"stage": "lost"}, headers=_auth())
        assert r.status_code == 400 and "why" in r.json()["message"]
        r = c.post("/api/companies/d1/stage", json={"stage": "negotiation", "value": "2 lakh"},
                   headers=_auth()).json()
        assert r["deal"]["stage"] == "negotiation" and r["deal"]["deal_value"] == 200000
        assert c.post("/api/companies/zz/stage", json={"stage": "won"}, headers=_auth()).status_code == 404
        assert c.post("/api/companies/d1/value", json={"value": "abc"}, headers=_auth()).status_code == 400
        tasks_now = c.get("/api/tasks", headers=_auth()).json()
        assert tasks_now["counts"]["today"] == 1 and tasks_now["tasks"][0]["due_text"] == "today"
        new = c.post("/api/tasks", json={"title": "Send proposal", "company_id": "d1", "due": "tomorrow"},
                     headers=_auth()).json()
        assert c.post("/api/tasks", json={"title": " "}, headers=_auth()).status_code == 400
        assert c.post(f"/api/tasks/{new['id']}/edit", json={"due": "today"}, headers=_auth()).json()["success"]
        assert c.post(f"/api/tasks/{new['id']}/done", json={}, headers=_auth()).json()["success"]
        assert c.post(f"/api/tasks/{new['id']}/delete", headers=_auth()).json()["success"]
        assert c.post("/api/tasks/nope/delete", headers=_auth()).status_code == 404
        today = c.get("/api/today", headers=_auth()).json()
        assert any(i["key"] == "tasks" for i in today["items"]) and today["stats"]["tasks"]["today"] == 1
        assert c.post("/api/companies/d1/brief", headers=_auth()).json()["success"]
        deadline = time.monotonic() + 10
        while (deal := c.get("/api/companies/d1/deal", headers=_auth()).json())["brief_running"]:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert deal["brief"]["angle"] == BRIEF["angle"] and deal["deal"]["stage"] == "negotiation"
        assert deal["sources"][0]["url"] == "https://acmedental.example.com/book"
        assert [t["title"] for t in deal["tasks"]][0].startswith("Reply to Ann")
        insights = c.get("/api/insights", headers=_auth()).json()
        assert {s["key"]: s["count"] for s in insights["pipeline"]}["negotiation"] == 1
        assert sum(t["count"] for t in insights["tiers"]) == 5
        assert c.get("/api/leads?limit=1", headers=_auth()).json()["total"] == 5


def test_chat_tools():
    for name in ("set_deal_stage", "add_task", "complete_task", "pitch_brief"):
        assert chat.TOOLS[name].acts
    for name in ("pipeline", "tasks", "show_brief"):
        assert not chat.TOOLS[name].acts


def test_chat_moves_a_deal_and_adds_a_task(env):
    class ChatBrain:
        paused_reason = ""

        def __init__(self):
            self.decisions = [
                {"tool": "set_deal_stage", "args": {"company": "Acme Dental", "stage": "meeting"}},
                {"tool": "add_task", "args": {"title": "Prep the call", "company": "Acme Dental",
                                              "due": "tomorrow"}},
                {"reply": "Done."}]

        async def think_json(self, prompt, **_):
            return self.decisions.pop(0)

    agent = chat.ChatAgent(dash._state(), dash.app, dash.token_store.get, dash.DB_PATH,
                           brain_factory=lambda config: ChatBrain())

    async def go():
        assert await agent.start("Acme Dental booked a call; move it and remind me to prep") == ""
        await agent.task
    asyncio.run(go())
    assert asyncio.run(pipeline_board.deal(env, _config(), "d1"))["stage"] == "meeting"
    rows = asyncio.run(tasks.listing(env))
    assert rows[0]["title"] == "Prep the call" and rows[0]["source"] == "chat"


def test_people_research_asks_for_buyer_and_champion(env, monkeypatch):
    from harvey import people_research
    cfg = copy.deepcopy(_config())
    cfg.icp.titles = ["Owner"]
    import types
    cfg.people_research = types.SimpleNamespace(roles={}, per_cycle=1, people_per_company=2, min_score=0)

    async def fetch(url):
        return "<p>Acme Dental: Zara Sample, owner</p>" if url.endswith("/team") else ""
    monkeypatch.setattr(people_research, "fetch_page", fetch)
    company = {"id": "d1", "name": "Acme Dental", "industry": "dentist", "location": "Exampleton",
               "domain": ""}
    brain = FakeBrain([{"match": "exact", "people": [
        {"first_name": "Zara", "last_name": "Sample", "title": "Owner", "role": "both",
         "source_url": "https://acmedental.example.com/team"}]}])
    card = asyncio.run(people_research.research(brain, env, cfg, None, company))
    assert '"buyer"' in brain.calls[0][0] and "champion" in brain.calls[0][0]
    assert card.people[0]["role"] == "both" and card.people[0]["added"]


def test_insights_period_and_funnel(env):
    asyncio.run(pipeline_board.set_stage(env, "d1", "meeting", value=90000))
    with TestClient(dash.app, base_url="http://127.0.0.1:5555") as c:
        d = c.get("/api/insights?days=7", headers=_auth()).json()
        assert len(d["series"]["days"]) == 7 and len(d["series"]["sent"]) == 7
        assert d["series"]["added"][-1] == 5 and d["previous"]["added"] == 0
        assert d["period_days"] == 7 and d["open_value"] == 90000
        assert [s["key"] for s in d["funnel"]] == ["leads", "reachable", "emailed", "replied", "meeting", "won"]
        assert {s["key"]: s["count"] for s in d["funnel"]}["meeting"] == 1
        assert c.get("/api/insights?days=3", headers=_auth()).json()["period_days"] == 7
        trend = c.get("/api/trend?days=5", headers=_auth()).json()
        assert set(trend) == {"days", "found", "sent", "replies"}
