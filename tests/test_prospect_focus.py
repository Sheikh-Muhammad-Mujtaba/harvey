"""icp.focus_categories / skip_categories steer which companies get searched
for contacts: focus categories first in the order given, skipped ones never."""

import asyncio

from harvey.config import ICPConfig
from harvey.state import StateManager


def _seed(tmp_path):
    state = StateManager(db_path=str(tmp_path / "harvey.db"))

    async def go():
        await state.init_db()
        async with state._connect() as db:
            for i, cat in enumerate(["school", "wholesale", "Restaurant", "hospital", "dentist"]):
                await db.execute(
                    "INSERT INTO companies (id, name, domain, industry) VALUES (?, ?, ?, ?)",
                    (f"c{i}", f"Example {cat}", f"{cat.lower()}.example.com", cat))
            await db.commit()
    asyncio.run(go())
    return state


def _cats(state, **kw):
    rows = asyncio.run(state.companies_needing_prospects(limit=10, **kw))
    return [r["industry"] for r in rows]


def test_no_focus_returns_everything(tmp_path):
    state = _seed(tmp_path)
    assert sorted(_cats(state)) == sorted(["school", "wholesale", "Restaurant", "hospital", "dentist"])


def test_focus_orders_and_skip_excludes(tmp_path):
    state = _seed(tmp_path)
    cats = _cats(state, focus=["Dentist", "wholesale", "restaurant"], skip=["hospital", "SCHOOL"])
    assert cats == ["dentist", "wholesale", "Restaurant"]


def test_unlisted_categories_follow_the_focus(tmp_path):
    state = _seed(tmp_path)
    cats = _cats(state, focus=["wholesale"])
    assert cats[0] == "wholesale" and len(cats) == 5


def test_config_defaults_are_empty():
    icp = ICPConfig(industries=[], company_size="", titles=[], geography=[])
    assert icp.focus_categories == [] and icp.skip_categories == []
