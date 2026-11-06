"""The lead list (harvey/leads.py), lead scoring and stars (harvey/lead_scoring.py),
their dashboard endpoints and the chat tools that use them."""

import asyncio
import csv
import io
import json
import sqlite3
import types

import pytest
from fastapi.testclient import TestClient

import harvey.config as config_mod
import harvey.dashboard as dash
from harvey import chat, lead_scoring, leads
from harvey.brain import Brain
from harvey.signals import seed_signal_catalog


def _config(star_at=75):
    return types.SimpleNamespace(
        icp=types.SimpleNamespace(
            service_tags={"Digital marketing": ["dentist", "estate_agent"], "ERP": ["wholesale"]},
            skip_categories=["hospital"], focus_categories=["dentist"]),
        lead_scoring=types.SimpleNamespace(
            criteria={"Digital marketing": "Pays at least 100k a month."},
            per_cycle=5, batch_size=5, star_at=star_at),
        persona=types.SimpleNamespace(company="Acme Web"),
        product=types.SimpleNamespace(name="Acme marketing"), brain=None)


class FakeBrain:
    provider = "claude"
    paused_reason = ""
    _extract_json = staticmethod(Brain._extract_json)

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    async def think(self, prompt, **kw):
        self.calls.append((prompt, kw))
        a = self.answers.pop(0) if self.answers else ""
        return a if isinstance(a, str) else json.dumps(a)

    async def think_json(self, prompt, **kw):
        return self.answers.pop(0) if self.answers else None


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "data" / "harvey.db")
    monkeypatch.setattr(dash, "_chat_agent", None)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: _config())
    state = dash._state()
    asyncio.run(state.init_db())
    asyncio.run(seed_signal_catalog(state))
    con = sqlite3.connect(dash.DB_PATH)
    con.executemany(
        "INSERT INTO companies (id, name, domain, industry, location, phone) VALUES (?, ?, ?, ?, ?, ?)", [
            ("d1", "Acme Dental", "acmedental.example.com", "dentist", "Exampleton, Testland", ""),
            ("d2", "Bright Smiles", "", "dentist", "Sampleville, Testland", "+1 555 0100"),
            ("r1", "Acme Homes", "acmehomes.example.com", "estate_agent", "Exampleton, Testland", ""),
            ("w1", "Acme Supplies", "", "wholesale", "Exampleton, Testland", ""),
            ("h1", "Acme Hospital", "", "hospital", "Exampleton, Testland", ""),
        ])
    con.executemany(
        "INSERT INTO prospects (id, company_id, first_name, last_name, title, email, email_status, status, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            ("p1", "d1", "Ann", "Example", "Owner", "ann@acmedental.example.com", "verified", "contacted", "x"),
            ("p2", "r1", "", "", "General inbox", "info@acmehomes.example.com", "verified", "new", "company_inbox"),
        ])
    con.commit()
    con.close()
    return state


def _ids(result):
    return [r["id"] for r in result["rows"]]


def _list(state, **filters):
    return asyncio.run(leads.list_leads(state, filters, _config()))


def test_status_comes_from_contacts(env):
    rows = {r["id"]: r for r in _list(env)["rows"]}
    assert rows["d1"]["lead_status"] == "contacted"
    assert rows["r1"]["lead_status"] == "email_found"
    assert rows["d2"]["lead_status"] == "phone_only"
    assert rows["w1"]["lead_status"] == "new"
    assert rows["d1"]["contact_name"] == "Ann Example" and rows["d1"]["email"].startswith("ann@")
    assert rows["d1"]["service_tags"] == ["Digital marketing"]


def test_filters(env):
    assert sorted(_ids(_list(env, service="Digital marketing"))) == ["d1", "d2", "r1"]
    assert _ids(_list(env, service="Unknown")) == []
    assert _ids(_list(env, industry="WHOLESALE")) == ["w1"]
    assert _ids(_list(env, location="Sampleville")) == ["d2"]
    assert _ids(_list(env, status="email_found")) == ["r1"]
    assert sorted(_ids(_list(env, has="email"))) == ["d1", "r1"]
    assert _ids(_list(env, has="phone")) == ["d2"]
    assert _ids(_list(env, q="smiles")) == ["d2"]


def test_score_sort_star_and_min_score(env):
    cfg = _config()
    asyncio.run(lead_scoring.record(env, cfg, "d2", {"score": 82, "why": "Three branches."}))
    asyncio.run(lead_scoring.record(env, cfg, "r1", {"score": 60}))
    result = _list(env)
    assert _ids(result)[:2] == ["d2", "r1"]               # best score first, unscored last
    assert result["rows"][0]["starred"] == 1 and result["rows"][0]["starred_by"] == "score"
    assert result["rows"][0]["score_detail"]["why"] == "Three branches."
    assert _ids(_list(env, min_score="70")) == ["d2"]
    assert _ids(_list(env, starred="1")) == ["d2"]


def test_operator_star_wins_over_scoring(env):
    cfg = _config()
    asyncio.run(lead_scoring.set_star(env, "d2", False))
    asyncio.run(lead_scoring.record(env, cfg, "d2", {"score": 95}))
    assert _ids(_list(env, starred="1")) == []
    asyncio.run(lead_scoring.set_star(env, "r1", True))
    asyncio.run(lead_scoring.record(env, cfg, "r1", {"score": 10}))
    assert _ids(_list(env, starred="1")) == ["r1"]


def test_notes_are_appended_with_a_date(env):
    asyncio.run(lead_scoring.add_note(env, "d1", "Owner wants a call on Monday."))
    asyncio.run(lead_scoring.add_note(env, "d1", "Sent proposal.", author="Harvey"))
    notes = _list(env, q="Acme Dental")["rows"][0]["notes"].split("\n")
    assert len(notes) == 2 and notes[1].endswith("Harvey] Sent proposal.")


def test_csv_is_safe_for_spreadsheets(env):
    con = sqlite3.connect(dash.DB_PATH)
    con.execute("UPDATE companies SET name = '=HYPERLINK(\"x\")' WHERE id = 'w1'")
    con.commit()
    con.close()
    text = leads.to_csv(_list(env)["rows"])
    rows = list(csv.DictReader(io.StringIO(text)))
    assert rows[0].keys() >= {"company", "service", "lead_score", "starred", "status"}
    assert any(r["company"] == "'=HYPERLINK(\"x\")" for r in rows)
    assert next(r for r in rows if r["company"] == "Bright Smiles")["phone"] == "'+1 555 0100"


def test_scoring_batches_and_skips(env):
    todo = asyncio.run(lead_scoring.companies_to_score(env, _config(), "Digital marketing", 10))
    assert [c["id"] for c in todo][0] in ("d1", "d2") and "h1" not in [c["id"] for c in todo]
    brain = FakeBrain([{"leads": [
        {"id": "d1", "budget": 9, "premium": 8, "closing": 7, "why": "Busy upmarket clinic."},
        {"id": "nope", "budget": 10, "premium": 10, "closing": 10},
    ]}])
    out = asyncio.run(lead_scoring.score_companies(brain, env, _config(), "Digital marketing",
                                                   [dict(todo[0], id="d1")]))
    assert [r["id"] for r in out] == ["d1"] and out[0]["score"] == lead_scoring.combine(9, 8, 7) == 81
    assert brain.calls[0][1]["web_search"] is True
    assert '<untrusted source="lead list">' in brain.calls[0][0]


def test_gated_scoring_waits_for_the_signal(env):
    brain = FakeBrain([])
    assert asyncio.run(lead_scoring.run_lead_scoring(env, brain, _config())) == []
    assert brain.calls == []


def test_combine_clamps():
    assert lead_scoring.combine(10, 10, 10) == 100
    assert lead_scoring.combine(-5, "x", None) == 0
    assert lead_scoring.combine(20, 0, 0) == 40


def test_endpoints(env):
    auth = {"Authorization": f"Bearer {dash.token_store.get()}"}
    with TestClient(dash.app, base_url="http://127.0.0.1:5555") as c:
        assert c.get("/api/leads").status_code == 401
        r = c.get("/api/leads?service=Digital+marketing&sort=name", headers=auth).json()
        assert r["total"] == 3 and [x["name"] for x in r["rows"]][0] == "Acme Dental"
        assert c.post("/api/companies/d2/star", json={"starred": True}, headers=auth).json()["starred"]
        assert c.post("/api/companies/d2/star", json={"starred": "yes"}, headers=auth).status_code == 400
        assert c.post("/api/companies/zz/star", json={"starred": True}, headers=auth).status_code == 404
        assert c.post("/api/companies/d2/note", json={"text": "Call back"}, headers=auth).json()["success"]
        facets = c.get("/api/leads/facets", headers=auth).json()
        assert "Digital marketing" in facets["services"]
        csv_resp = c.get("/api/leads.csv?starred=1", headers=auth)
        assert csv_resp.headers["content-type"].startswith("text/csv")
        rows = list(csv.DictReader(io.StringIO(csv_resp.text)))
        assert [r["company"] for r in rows] == ["Bright Smiles"] and "Call back" in rows[0]["notes"]


def _agent(brain):
    return chat.ChatAgent(dash._state(), dash.app, dash.token_store.get, dash.DB_PATH,
                          brain_factory=lambda config: brain)


async def _run(agent, message):
    assert await agent.start(message) == ""
    await agent.task
    return await agent.store.recent()


def test_chat_export_gives_a_download_and_writes_nothing(env, tmp_path):
    before = set(p.name for p in tmp_path.rglob("*"))
    brain = FakeBrain([
        {"tool": "export_leads", "args": {"service": "Digital marketing", "has": "email"}},
        {"reply": "Here are the 2 leads."},
    ])
    msgs = asyncio.run(_run(_agent(brain), "Give me a CSV of marketing leads with email"))
    step = msgs[1]["meta"]["steps"][0]
    assert step["download"]["count"] == 2
    assert step["download"]["download_url"].startswith("/api/leads.xlsx?service=Digital+marketing")
    assert step["download"]["filename"].endswith(".xlsx")
    after = set(p.name for p in tmp_path.rglob("*"))
    assert not any(n.endswith((".csv", ".xlsx")) for n in after - before)


def test_chat_note_star_and_contact(env, monkeypatch):
    import harvey.integrations.email_finder as ef

    async def verify(email, env=None):
        return "verified"
    monkeypatch.setattr(ef, "verify_published_address", verify)
    brain = FakeBrain([
        {"tool": "add_note", "args": {"company": "Acme Supplies", "text": "Runs 3 warehouses."}},
        {"tool": "star", "args": {"company": "Acme Supplies", "starred": True}},
        {"tool": "add_contact", "args": {"company": "Acme Supplies", "first_name": "Bo",
                                         "last_name": "Sample", "title": "Director",
                                         "email": "bo@acmesupplies.example.com"}},
        {"tool": "add_contact", "args": {"company": "Acme Supplies", "first_name": "Cy"}},
        {"reply": "Done."},
    ])
    msgs = asyncio.run(_run(_agent(brain), "note, star and add Bo"))
    steps = msgs[1]["meta"]["steps"]
    assert [s["ok"] for s in steps] == [True, True, True, False]
    row = _list(env, q="Acme Supplies")["rows"][0]
    assert row["starred"] == 1 and row["starred_by"] == "you" and "3 warehouses" in row["notes"]
    assert row["contact_name"] == "Bo Sample" and row["lead_status"] == "email_found"


def test_scoring_can_be_limited_to_a_location(env):
    todo = asyncio.run(lead_scoring.companies_to_score(env, _config(), "Digital marketing", 10,
                                                       location="Sampleville"))
    assert [c["id"] for c in todo] == ["d2"]


def test_excel_export_has_leads_contacts_and_about(env):
    from openpyxl import load_workbook
    con = sqlite3.connect(dash.DB_PATH)
    con.execute("UPDATE companies SET name = '=1+1' WHERE id = 'w1'")
    con.commit()
    con.close()
    asyncio.run(lead_scoring.record(env, _config(), "d1", {"score": 88, "why": "Two branches."}))
    auth = {"Authorization": f"Bearer {dash.token_store.get()}"}
    with TestClient(dash.app, base_url="http://127.0.0.1:5555") as c:
        assert c.get("/api/leads.xlsx").status_code == 401
        r = c.get("/api/leads.xlsx", headers=auth)
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["Leads", "Contacts", "About"]
    leads_ws, contacts_ws = wb["Leads"], wb["Contacts"]
    header = [c.value for c in leads_ws[1]]
    assert header[:3] == ["Star", "Company", "Lead type"] and "Why this score" in header
    assert "Leads" in leads_ws.tables and leads_ws.freeze_panes == "C2"
    first = {h: c for h, c in zip(header, leads_ws[2])}
    assert first["Company"].value == "Acme Dental" and first["Star"].value == "★"
    assert first["Score"].value == 88 and first["Why this score"].value == "Two branches."
    assert first["Website"].hyperlink.target == "https://acmedental.example.com"
    formula = next(row[1] for row in leads_ws.iter_rows(min_row=2) if row[1].value == "=1+1")
    assert formula.data_type == "s"          # kept as text, never run
    contact_header = [c.value for c in contacts_ws[1]]
    contacts = [dict(zip(contact_header, [c.value for c in row])) for row in contacts_ws.iter_rows(min_row=2)]
    ann = next(x for x in contacts if x["Name"] == "Ann Example")
    assert ann["Company"] == "Acme Dental" and ann["Email status"] == "Verified" and ann["Contact status"] == "Emailed"
    assert any(x["Name"] == "General inbox" for x in contacts)


def test_excel_export_follows_filters_and_handles_no_rows(env):
    from openpyxl import load_workbook
    rows = asyncio.run(leads.list_leads(env, {"q": "nothing-matches"}, _config(), limit=None))["rows"]
    data = leads.to_xlsx(rows, [], {"q": "nothing-matches"}, "2026-01-01 00:00")
    wb = load_workbook(io.BytesIO(data))
    assert wb["Leads"].max_row == 1 and "q = nothing-matches" in wb["About"]["B3"].value


def test_no_website_and_call_list_filters(env):
    con = sqlite3.connect(dash.DB_PATH)
    con.execute("UPDATE companies SET phone = '+1 555 0102' WHERE id = 'w1'")
    con.commit()
    con.close()
    ids = lambda has: sorted(r["id"] for r in asyncio.run(
        leads.list_leads(env, {"has": has}, _config()))["rows"])
    assert ids("no_website") == ["d2", "h1", "w1"]
    assert ids("call") == ["d2", "w1"]
