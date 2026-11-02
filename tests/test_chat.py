"""Ask Harvey (harvey/chat.py): the chat agent and its tools.

A scripted fake brain stands in for the model, so these run offline. They
cover: read-only SQL refuses writes, a turn runs tools through the real
dashboard API and stores the reply with its steps, actions are logged,
autopilot needs the operator's own word, blocked and invalid settings are
refused, a good setting is written with a backup, and the chat endpoints
need the dashboard token like everything else.
"""

import asyncio
import sqlite3
import types

import pytest
import yaml
from fastapi.testclient import TestClient

import harvey.config as config_mod
import harvey.dashboard as dash
from harvey import chat


class ScriptedBrain:
    """Returns the queued decisions in order; records every prompt."""

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.prompts = []
        self.paused_reason = ""

    async def think_json(self, prompt, **_):
        self.prompts.append(prompt)
        return self.decisions.pop(0) if self.decisions else None


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "data" / "harvey.db")
    monkeypatch.setattr(dash, "PID_FILE", tmp_path / "data" / "harvey.pid")
    monkeypatch.setattr(dash, "LOG_FILE", tmp_path / "data" / "harvey.log")
    monkeypatch.setattr(dash, "_chat_agent", None)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    fake_config = types.SimpleNamespace(
        persona=types.SimpleNamespace(company="Acme Web"),
        product=types.SimpleNamespace(name="Acme websites"), brain=None)
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: fake_config)
    asyncio.run(dash._state().init_db())
    return tmp_path


def _agent(brain):
    return chat.ChatAgent(dash._state(), dash.app, dash.token_store.get, dash.DB_PATH,
                          brain_factory=lambda config: brain)


async def _run(agent, message):
    assert await agent.start(message) == ""
    await agent.task
    return await agent.store.recent()


def test_query_db_refuses_writes(env):
    db = dash.DB_PATH
    for sql in ("DELETE FROM companies", "DROP TABLE companies",
                "INSERT INTO settings (key, value) VALUES ('a', 'b')"):
        with pytest.raises(chat.ToolError):
            chat._read_only_query(db, sql)
    # A write hidden behind a SELECT is still one statement only.
    with pytest.raises(chat.ToolError):
        chat._read_only_query(db, "SELECT 1; DELETE FROM companies")
    assert chat._read_only_query(db, "SELECT 1 AS one")["rows"] == [[1]]


def test_turn_uses_tools_and_stores_reply(env):
    con = sqlite3.connect(dash.DB_PATH)
    con.execute("INSERT INTO companies (id, name, domain) VALUES ('c1', 'Acme Bakery', 'acme.example.com')")
    con.commit()
    con.close()
    brain = ScriptedBrain([
        {"tool": "query_db", "args": {"sql": "SELECT name FROM companies"}, "status": "Counting companies"},
        {"reply": "You have **1** company: Acme Bakery."},
    ])
    msgs = asyncio.run(_run(_agent(brain), "How many companies do we have?"))
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "You have **1** company: Acme Bakery."
    steps = msgs[1]["meta"]["steps"]
    assert steps[0]["tool"] == "query_db" and steps[0]["ok"] and not steps[0]["acts"]
    # The second prompt carried the result, fenced as untrusted data.
    assert '<untrusted source="result of query_db">' in brain.prompts[1]
    assert "Acme Bakery" in brain.prompts[1]


def test_action_runs_through_the_api_and_is_logged(env):
    brain = ScriptedBrain([
        {"tool": "sending", "args": {"action": "pause"}},
        {"reply": "Sending is paused."},
    ])
    asyncio.run(_run(_agent(brain), "Pause sending"))
    con = sqlite3.connect(dash.DB_PATH)
    paused = con.execute("SELECT value FROM settings WHERE key = 'sending_paused'").fetchone()
    logged = con.execute("SELECT action_type, agent FROM actions").fetchall()
    con.close()
    assert paused and paused[0]
    assert ("chat_action", "chat") in logged


def test_autopilot_needs_the_operators_word(env):
    brain = ScriptedBrain([
        {"tool": "set_sending_mode", "args": {"mode": "autopilot"}},
        {"reply": "Please say autopilot."},
    ])
    msgs = asyncio.run(_run(_agent(brain), "send them all on your own please"))
    step = msgs[1]["meta"]["steps"][0]
    assert step["ok"] is False and "autopilot" in step["summary"]


def test_unknown_tool_is_reported_back(env):
    brain = ScriptedBrain([{"tool": "rm_rf", "args": {}}, {"reply": "I can't do that."}])
    msgs = asyncio.run(_run(_agent(brain), "delete everything"))
    assert msgs[1]["meta"]["steps"][0]["ok"] is False
    assert "There is no tool called 'rm_rf'" in brain.prompts[1]


def test_no_model_reply_says_so(env):
    msgs = asyncio.run(_run(_agent(ScriptedBrain([])), "hello"))
    assert "couldn't think that through" in msgs[1]["content"]


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "harvey.local.yaml"
    path.write_text(open(config_mod.PROJECT_ROOT / "harvey.yaml", encoding="utf-8").read(),
                    encoding="utf-8")
    monkeypatch.setattr(config_mod, "_find_config_file", lambda: str(path))
    return path


def test_blocked_and_invalid_settings_are_refused(config_file):
    before = config_file.read_text()
    for key in ("channels.email.require_approval", "brain.provider", "nonsense.key"):
        with pytest.raises(chat.ToolError):
            chat.set_config_value(key, True)
    with pytest.raises(chat.ToolError):
        chat.set_config_value("usage.heartbeat_interval_minutes", "not a number")
    assert config_file.read_text() == before


def test_setting_is_written_with_a_backup(config_file):
    before = config_file.read_text()
    chat.set_config_value("icp.geography", ["Exampleton, Testland"])
    assert yaml.safe_load(config_file.read_text())["icp"]["geography"] == ["Exampleton, Testland"]
    assert (config_file.parent / "harvey.local.yaml.before-chat").read_text() == before


def test_chat_endpoints_need_the_token(env):
    with TestClient(dash.app, base_url="http://127.0.0.1:5555") as c:
        assert c.get("/api/chat").status_code == 401
        assert c.post("/api/chat", json={"message": "hi"}).status_code == 401
        auth = {"Authorization": f"Bearer {dash.token_store.get()}"}
        r = c.get("/api/chat", headers=auth)
        assert r.status_code == 200 and r.json()["messages"] == []
        assert c.post("/api/chat", json={"message": "  "}, headers=auth).status_code == 409
