"""Lead scoring as a background job (harvey/jobs.py), its dashboard
endpoints and the Ask Harvey tools that drive it."""

import asyncio
import copy
import time

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey import chat
from harvey.jobs import ScoringJob
from tests.test_leads import FakeBrain, _config, env  # noqa: F401  (fixture)

SERVICE = "Digital marketing"


def _cfg(batch_size=2):
    cfg = copy.deepcopy(_config())
    cfg.lead_scoring.batch_size = batch_size
    return cfg


def _lead(cid, score=8):
    return {"id": cid, "budget": score, "premium": score, "closing": score, "why": "fits"}


def _run(job, state, cfg, brain, **kw):
    async def go():
        job.start(state, cfg, lambda config: brain, **kw)
        await job.task
        return job.status()
    return asyncio.run(go())


def test_scores_in_batches_up_to_the_limit(env):
    brain = FakeBrain([{"leads": [_lead("d1", 9), _lead("d2", 4)]}, {"leads": [_lead("r1", 6)]}])
    s = _run(ScoringJob(), env, _cfg(), brain, limit=3)
    assert s["running"] is False and s["scored"] == 3 and s["batches"] == 2
    assert s["stopped"] == "limit reached" and s["high"] == 1 and s["finished_at"]
    assert [t["id"] for t in s["top"]] == ["d1", "r1", "d2"]
    assert all(kw["web_search"] for _, kw in brain.calls)


def test_a_company_claude_skipped_is_not_asked_about_forever(env):
    # Claude answers for d1 only; d2 stays unscored but isn't sent again.
    brain = FakeBrain([{"leads": [_lead("d1")]}, {"leads": [_lead("r1")]}])
    s = _run(ScoringJob(), env, _cfg(), brain, limit=10)
    assert len(brain.calls) == 2 and s["scored"] == 2
    assert s["stopped"] == "nothing left to score"


def test_claude_pausing_ends_the_run_and_says_why(env):
    brain = FakeBrain([""])
    brain.paused_reason = "daily budget reached"
    s = _run(ScoringJob(), env, _cfg(), brain)
    assert s["scored"] == 0 and s["stopped"] == "daily budget reached"


def test_stop_finishes_the_current_batch_first(env):
    job = ScoringJob()

    class StoppingBrain(FakeBrain):
        async def think(self, prompt, **kw):
            job.stop()
            assert job.status()["stopping"] is True
            return await super().think(prompt, **kw)

    brain = StoppingBrain([{"leads": [_lead("d1"), _lead("d2")]}, {"leads": [_lead("r1")]}])
    s = _run(job, env, _cfg(), brain, limit=10)
    assert len(brain.calls) == 1 and s["scored"] == 2 and s["stopped"] == "stopped"
    assert "stopping" not in s


def test_location_and_service_narrow_the_run(env):
    brain = FakeBrain([{"leads": [_lead("d2")]}])
    s = _run(ScoringJob(), env, _cfg(), brain, service=SERVICE, location="Sampleville")
    assert s["scored"] == 1 and s["service"] == SERVICE and s["location"] == "Sampleville"
    assert "Bright Smiles" in brain.calls[0][0] and "Acme Dental" not in brain.calls[0][0]


def test_refuses_bad_requests(env):
    job = ScoringJob()
    brain = FakeBrain([])
    for kw, why in [({"service": "ERP"}, "no scoring criteria"), ({"limit": 0}, "between"),
                    ({"limit": "lots"}, "a number")]:
        with pytest.raises(ValueError, match=why):
            job.start(env, _cfg(), lambda c: brain, **kw)
    cfg = _cfg()
    cfg.lead_scoring.criteria = {}
    with pytest.raises(ValueError, match="nothing to score"):
        job.start(env, cfg, lambda c: brain)

    async def twice():
        job.start(env, _cfg(), lambda c: FakeBrain([{"leads": []}]))
        with pytest.raises(ValueError, match="already going"):
            job.start(env, _cfg(), lambda c: brain)
        await job.task
    asyncio.run(twice())


def test_endpoints_start_report_and_stop(env, monkeypatch):
    brain = FakeBrain([{"leads": [_lead("d1", 9), _lead("d2"), _lead("r1")]}])   # one batch of 5
    monkeypatch.setattr(dash, "_scoring_job", None)
    monkeypatch.setattr(dash, "_job_brain", lambda config: brain)
    auth = {"Authorization": f"Bearer {dash.token_store.get()}"}
    with TestClient(dash.app, base_url="http://127.0.0.1:5555") as c:
        assert c.post("/api/leads/score/run", json={}).status_code == 401
        assert c.get("/api/leads/score", headers=auth).json() == {"running": False}
        assert c.post("/api/leads/score/run", json={"service": "ERP"}, headers=auth).status_code == 400
        r = c.post("/api/leads/score/run", json={"limit": 20}, headers=auth).json()
        assert r["success"] and r["limit"] == 20
        deadline = time.monotonic() + 10
        while c.get("/api/leads/score", headers=auth).json()["running"]:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        s = c.get("/api/leads/score", headers=auth).json()
        assert s["scored"] == 3 and s["top"][0]["name"] == "Acme Dental"
        assert c.post("/api/leads/score/stop", headers=auth).json()["message"] == "no scoring run is going"
        rows = c.get("/api/leads?sort=score", headers=auth).json()["rows"]
        assert rows[0]["id"] == "d1" and rows[0]["lead_score"] == 90


def test_chat_has_start_status_and_stop_tools():
    assert chat.TOOLS["start_scoring"].acts and chat.TOOLS["stop_scoring"].acts
    assert not chat.TOOLS["scoring_status"].acts
    assert "start_scoring" in chat.TOOLS["score_leads"].doc


def test_chat_starts_a_run_and_returns_at_once(env, monkeypatch):
    scoring_brain = FakeBrain([{"leads": [_lead("d2")]}])
    monkeypatch.setattr(dash, "_scoring_job", None)
    monkeypatch.setattr(dash, "_job_brain", lambda config: scoring_brain)

    class ChatBrain:
        paused_reason = ""

        def __init__(self):
            self.decisions = [
                {"tool": "start_scoring", "args": {"location": "Sampleville", "limit": 5, "bogus": 1}},
                {"reply": "Started."}]

        async def think_json(self, prompt, **_):
            return self.decisions.pop(0)

    agent = chat.ChatAgent(dash._state(), dash.app, dash.token_store.get, dash.DB_PATH,
                           brain_factory=lambda config: ChatBrain())

    async def go():
        assert await agent.start("Score the Sampleville leads in the background") == ""
        await agent.task
        await dash._scoring().task
        return dash._scoring().status()
    s = asyncio.run(go())
    assert s["location"] == "Sampleville" and s["limit"] == 5 and s["scored"] == 1
