"""Background jobs the dashboard and Ask Harvey start: lead scoring and
people research.

The heartbeat does a few of each per cycle. These do a bigger batch now
without holding a request (or a chat turn) open: start one, carry on, and
check its progress whenever. One run of each kind at a time per dashboard
process. Both ask for the next companies to do as they go, so nothing is
done twice even while the heartbeat is working too. The stop switch is
read between Claude calls; Claude pausing (quota, budget) ends the run and
says why.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from harvey import lead_scoring, people_research

logger = logging.getLogger("harvey.jobs")

DEFAULT_LIMIT = 50
MAX_LIMIT = 500
TOP_RESULTS = 10


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _limit(value, default: int) -> int:
    try:
        limit = default if value in (None, "") else int(value)
    except (TypeError, ValueError):
        raise ValueError("limit must be a number") from None
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    return limit


class BackgroundJob:
    """One run at a time; ``report`` is what the status shows."""

    def __init__(self):
        self.task: asyncio.Task | None = None
        self.report: dict | None = None
        self._stop = False

    @property
    def running(self) -> bool:
        return bool(self.task and not self.task.done())

    def status(self) -> dict:
        report = dict(self.report or {})
        report["running"] = self.running
        results = report.pop("results", None)
        if results:
            report["top"] = self._top(results)
        return report

    def _top(self, results: list[dict]) -> list[dict]:
        return results[-TOP_RESULTS:]

    def stop(self) -> bool:
        if not self.running:
            return False
        self._stop = True
        if self.report is not None:
            self.report["stopping"] = True
        return True

    def _check_brain(self, config):
        if self.running:
            raise ValueError(f"a {self.kind} run is already going")
        if getattr(getattr(config, "brain", None), "provider", "claude") != "claude":
            raise ValueError(f"{self.kind} uses Claude's web search; set brain.provider: claude")

    def _launch(self, report: dict, work):
        self._stop = False
        self.report = {**report, "results": [], "started_at": _now(), "finished_at": "",
                       "stopped": "", "error": "", "current": ""}
        self.task = asyncio.create_task(self._wrap(work))
        return self.status()

    async def _wrap(self, work):
        report = self.report
        try:
            await work
        except asyncio.CancelledError:
            report["stopped"] = "cancelled"
            raise
        except Exception as exc:
            logger.exception(f"{self.kind} run failed")
            report["error"], report["stopped"] = str(exc), "failed"
        finally:
            report["current"] = ""
            report["finished_at"] = _now()
            report.pop("stopping", None)


class ScoringJob(BackgroundJob):
    kind = "scoring"

    def _top(self, results):
        return sorted(results, key=lambda r: -r["score"])[:TOP_RESULTS]

    def start(self, state, config, brain_factory: Callable[[Any], Any], service: str = "",
              location: str = "", limit=None) -> dict:
        """Check the request and start; ValueError (with the reason) when it can't."""
        self._check_brain(config)
        criteria = dict(config.lead_scoring.criteria or {})
        if not criteria:
            raise ValueError("nothing to score against: add lead_scoring.criteria to the config")
        service = (service or "").strip()
        if service and service not in criteria:
            raise ValueError(f"no scoring criteria for {service!r}; have: {', '.join(criteria)}")
        limit = _limit(limit, DEFAULT_LIMIT)
        services = [service] if service else list(criteria)
        report = {"service": service, "location": (location or "").strip(), "limit": limit,
                  "scored": 0, "high": 0, "star_at": config.lead_scoring.star_at, "batches": 0}
        return self._launch(report, self._run(state, config, brain_factory(config), services))

    async def _run(self, state, config, brain, services: list[str]):
        report = self.report
        batch_size = max(1, int(config.lead_scoring.batch_size or 5))
        tried: set[str] = set()
        for svc in services:
            while report["scored"] < report["limit"]:
                if self._stop:
                    report["stopped"] = "stopped"
                    return
                want = min(batch_size, report["limit"] - report["scored"])
                # Ask for a few extra: companies Claude skipped before are
                # still unscored and would come straight back.
                todo = await lead_scoring.companies_to_score(
                    state, config, svc, want + len(tried), location=report["location"])
                todo = [c for c in todo if c["id"] not in tried][:want]
                if not todo:
                    break
                tried.update(c["id"] for c in todo)
                report["current"] = f"{svc}: {', '.join(c['name'] for c in todo)}"
                batch = await lead_scoring.score_companies(brain, state, config, svc, todo)
                if batch is None:
                    report["stopped"] = getattr(brain, "paused_reason", "") or "no answer from Claude"
                    return
                report["batches"] += 1
                report["scored"] += len(batch)
                report["high"] += sum(1 for r in batch if r["score"] >= report["star_at"])
                report["results"].extend(
                    {"id": r.get("id", ""), "name": r["name"], "score": r["score"],
                     "service": svc, "why": r.get("why", "")} for r in batch)
                for r in batch:
                    logger.info(f"Lead score: {r['name']} — {r['score']} ({svc})")
        report["stopped"] = "limit reached" if report["scored"] >= report["limit"] \
            else "nothing left to score"


class PeopleJob(BackgroundJob):
    kind = "people research"
    DEFAULT_LIMIT = 20

    def start(self, state, config, brain_factory: Callable[[Any], Any], env=None,
              service: str = "", location: str = "", min_score=None, starred_only: bool = False,
              limit=None) -> dict:
        self._check_brain(config)
        service = (service or "").strip()
        if service and service not in (config.icp.service_tags or {}):
            raise ValueError(f"unknown lead type {service!r}")
        try:
            min_score = int(min_score) if min_score not in (None, "") else None
        except (TypeError, ValueError):
            raise ValueError("min_score must be a number") from None
        limit = _limit(limit, self.DEFAULT_LIMIT)
        report = {"service": service, "location": (location or "").strip(), "limit": limit,
                  "min_score": min_score, "starred_only": bool(starred_only),
                  "researched": 0, "people_added": 0, "unconfirmed": 0}
        return self._launch(report, self._run(state, config, brain_factory(config), env))

    async def _run(self, state, config, brain, env):
        report = self.report
        tried: set[str] = set()
        while report["researched"] < report["limit"]:
            if self._stop:
                report["stopped"] = "stopped"
                return
            todo = await people_research.companies_to_research(
                state, config, 1 + len(tried), service=report["service"],
                location=report["location"], min_score=report["min_score"],
                starred_only=report["starred_only"])
            company = next((c for c in todo if c["id"] not in tried), None)
            if not company:
                report["stopped"] = "nothing left to research"
                return
            tried.add(company["id"])
            report["current"] = company["name"]
            card = await people_research.research(brain, state, config, env, company,
                                                  service=report["service"])
            if card is None:
                report["stopped"] = getattr(brain, "paused_reason", "") or "no answer from Claude"
                return
            report["researched"] += 1
            report["people_added"] += card.added
            report["unconfirmed"] += sum(1 for p in card.people if not p["confirmed"])
            report["results"].append({
                "id": card.company_id, "name": card.name,
                "people": [{k: p[k] for k in ("name", "title", "confirmed", "added", "email_status")}
                           for p in card.people],
                "summary": people_research.describe(card)})
            logger.info(f"People: {card.name} — {people_research.describe(card)}")
        report["stopped"] = "limit reached"
