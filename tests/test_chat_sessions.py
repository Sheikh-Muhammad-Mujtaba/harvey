"""Ask Harvey sessions (new, switch, rename, delete) and the Insights numbers."""

import asyncio
import sqlite3
import types

import pytest
from fastapi.testclient import TestClient

import harvey.config as config_mod
import harvey.dashboard as dash
from harvey import chat


class ScriptedBrain:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.prompts = []
        self.paused_reason = ""

    async def think_json(self, prompt, **_):
        self.prompts.append(prompt)
        return self.decisions.pop(0) if self.decisions else None


def _config():
    return types.SimpleNamespace(
        persona=types.SimpleNamespace(company="Acme Web"),
        product=types.SimpleNamespace(name="Acme marketing"), brain=None,
        icp=types.SimpleNamespace(service_tags={"Digital marketing": ["dentist"]},
                                  skip_categories=[], focus_categories=[]),
        lead_scoring=types.SimpleNamespace(criteria={}, per_cycle=0, batch_size=5, star_at=75))


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "data" / "harvey.db")
    monkeypatch.setattr(dash, "_chat_agent", None)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: _config())
    asyncio.run(dash._state().init_db())
    return tmp_path


def _agent(brain):
    return chat.ChatAgent(dash._state(), dash.app, dash.token_store.get, dash.DB_PATH,
                          brain_factory=lambda config: brain)


async def _ask(agent, message, session=""):
    assert await agent.start(message, session) == ""
    await agent.task
    return agent.session_id


def test_each_session_keeps_its_own_history(env):
    brain = ScriptedBrain([{"reply": "one"}, {"reply": "two"}, {"reply": "three"}])
    agent = _agent(brain)
    first = asyncio.run(_ask(agent, "First question about dentists"))
    second = asyncio.run(_ask(agent, "Another topic"))
    assert first != second
    asyncio.run(_ask(agent, "Follow-up", first))
    assert "Another topic" not in brain.prompts[2] and "First question" in brain.prompts[2]
    msgs = asyncio.run(agent.store.recent(first))
    assert [m["content"] for m in msgs] == ["First question about dentists", "one", "Follow-up", "three"]
    sessions = asyncio.run(agent.store.sessions())
    assert sessions[0]["id"] == first     # most recently used first, even within one second
    assert {s["title"] for s in sessions} == {"First question about dentists", "Another topic"}


def test_unknown_session_starts_a_new_one(env):
    agent = _agent(ScriptedBrain([{"reply": "hi"}]))
    sid = asyncio.run(_ask(agent, "hello", "does-not-exist"))
    assert sid != "does-not-exist" and asyncio.run(agent.store.session(sid))


def test_messages_from_before_sessions_are_kept(env):
    con = sqlite3.connect(dash.DB_PATH)
    con.execute(chat.CHAT_DDL)
    con.execute("INSERT INTO chat_messages (role, content) VALUES ('user', 'old question')")
    con.commit()
    con.close()
    store = chat.ChatStore(dash._state())
    sessions = asyncio.run(store.sessions())
    assert [s["title"] for s in sessions] == ["Earlier chat"]
    assert asyncio.run(store.recent(sessions[0]["id"]))[0]["content"] == "old question"


def test_session_endpoints(env):
    auth = {"Authorization": f"Bearer {dash.token_store.get()}"}
    with TestClient(dash.app, base_url="http://127.0.0.1:5555") as c:
        assert c.get("/api/chat/sessions").status_code == 401
        new = c.post("/api/chat/sessions", json={}, headers=auth).json()["session"]
        assert new["title"] == "New chat" and new["messages"] == 0
        assert c.post(f"/api/chat/sessions/{new['id']}/rename", json={"title": "Exampleton leads"},
                      headers=auth).json()["success"]
        listed = c.get("/api/chat/sessions", headers=auth).json()["sessions"]
        assert [s["title"] for s in listed] == ["Exampleton leads"]
        snap = c.get(f"/api/chat?session={new['id']}", headers=auth).json()
        assert snap["session"]["id"] == new["id"] and snap["messages"] == []
        assert c.post("/api/chat/sessions/nope/rename", json={"title": "x"}, headers=auth).status_code == 404
        assert c.post(f"/api/chat/sessions/{new['id']}/delete", headers=auth).json()["success"]
        assert c.get("/api/chat/sessions", headers=auth).json()["sessions"] == []
        assert c.post(f"/api/chat/sessions/{new['id']}/delete", headers=auth).status_code == 404


def test_cannot_delete_a_chat_mid_answer(env):
    agent = dash._chat()

    async def go():
        sid = await agent.store.create_session("busy")
        agent.session_id = sid
        agent.task = asyncio.get_running_loop().create_future()   # not done: busy
        return sid
    sid = asyncio.run(go())
    with TestClient(dash.app, base_url="http://127.0.0.1:5555") as c:
        r = c.post(f"/api/chat/sessions/{sid}/delete",
                   headers={"Authorization": f"Bearer {dash.token_store.get()}"})
    assert r.status_code == 409


def test_insights_numbers(env):
    con = sqlite3.connect(dash.DB_PATH)
    con.executemany("INSERT INTO companies (id, name, industry, location, phone, lead_score, starred) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)", [
                        ("a", "Acme Dental", "dentist", "Exampleton, Testland", "+1 555 0100", 80, 1),
                        ("b", "Acme Mart", "supermarket", "Sampleville", "", 30, 0),
                        ("c", "Acme Smile", "dentist", "Exampleton", "", None, 0)])
    con.commit()
    con.close()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555") as c:
        d = c.get("/api/insights", headers={"Authorization": f"Bearer {dash.token_store.get()}"}).json()
    assert d["kpis"]["leads"] == 3 and d["kpis"]["starred"] == 1 and d["kpis"]["scored"] == 2
    assert d["kpis"]["avg_score"] == 55 and d["kpis"]["with_phone"] == 1
    assert {s["key"]: s["count"] for s in d["status"]}["phone_only"] == 1
    assert {t["label"]: t["count"] for t in d["lead_types"]} == {"Digital marketing": 2, "Not tagged": 1}
    assert d["locations"][0] == {"label": "Exampleton", "count": 2}
    assert {b["band"]: b["count"] for b in d["score_bands"]}["75-100"] == 1
    assert [t["name"] for t in d["top"]] == ["Acme Dental", "Acme Mart"]
