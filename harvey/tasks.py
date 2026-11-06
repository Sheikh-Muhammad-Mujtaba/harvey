"""Follow-ups: what you (or Harvey) should do next, and by when.

You add tasks from the dashboard or Ask Harvey. Harvey adds one itself
whenever a prospect writes back (speed to lead: a reply answered within the
hour is worth many answered tomorrow), due today, keyed on the conversation
and the reply so it's made once. Marking it done is yours.
"""

import json
import re
import uuid
from datetime import date, datetime, timedelta

TITLE_CHARS = 200
_WHEN = {"today": 0, "tomorrow": 1, "next week": 7}


def parse_due(raw) -> str:
    """'' (no date), YYYY-MM-DD, or today / tomorrow / next week / in 3 days."""
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    if text in _WHEN:
        return (date.today() + timedelta(days=_WHEN[text])).isoformat()
    m = re.fullmatch(r"in (\d{1,3}) days?", text)
    if m:
        return (date.today() + timedelta(days=int(m.group(1)))).isoformat()
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        raise ValueError("due must be a date (YYYY-MM-DD), today, tomorrow, next week "
                         "or 'in N days'") from None


async def add(state, title: str, company_id: str = "", prospect_id: str = "",
              due: str = "", source: str = "you", ref: str = "") -> str | None:
    """The new task's id; None when a task with this ref already exists."""
    title = " ".join(str(title or "").split())[:TITLE_CHARS]
    if not title:
        raise ValueError("a task needs a title")
    due_date = parse_due(due)
    task_id = uuid.uuid4().hex[:12]
    async with state._connect() as db:
        if company_id:
            cur = await db.execute("SELECT 1 FROM companies WHERE id = ?", (company_id,))
            if not await cur.fetchone():
                raise ValueError("unknown company")
        cur = await db.execute(
            "INSERT OR IGNORE INTO tasks (id, company_id, prospect_id, title, due_date, source, ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, company_id, prospect_id, title, due_date, source, ref))
        await db.commit()
        return task_id if cur.rowcount else None


async def set_done(state, task_id: str, done: bool = True) -> bool:
    async with state._connect() as db:
        cur = await db.execute(
            "UPDATE tasks SET done = ?, done_at = CASE WHEN ? THEN CURRENT_TIMESTAMP END WHERE id = ?",
            (int(done), int(done), task_id))
        await db.commit()
        return cur.rowcount > 0


async def update(state, task_id: str, title=None, due=None) -> bool:
    sets, params = [], []
    if title is not None:
        title = " ".join(str(title).split())[:TITLE_CHARS]
        if not title:
            raise ValueError("a task needs a title")
        sets.append("title = ?")
        params.append(title)
    if due is not None:
        sets.append("due_date = ?")
        params.append(parse_due(due))
    if not sets:
        return False
    async with state._connect() as db:
        cur = await db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", (*params, task_id))
        await db.commit()
        return cur.rowcount > 0


async def delete(state, task_id: str) -> bool:
    async with state._connect() as db:
        cur = await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await db.commit()
        return cur.rowcount > 0


def bucket(task: dict, today: str) -> str:
    if task.get("done"):
        return "done"
    due = task.get("due_date") or ""
    if not due:
        return "someday"
    if due < today:
        return "overdue"
    if due == today:
        return "today"
    return "upcoming"


async def listing(state, company_id: str = "", include_done: bool = False,
                  limit: int = 300) -> list[dict]:
    """Open tasks (and recent done ones if asked), overdue first, each with
    its company and contact names and a bucket: overdue, today, upcoming,
    someday or done."""
    where, params = [], []
    if company_id:
        where.append("t.company_id = ?")
        params.append(company_id)
    if not include_done:
        where.append("t.done = 0")
    sql = f"""SELECT t.*, COALESCE(c.name, '') AS company_name,
                     TRIM(COALESCE(p.first_name, '') || ' ' || COALESCE(p.last_name, '')) AS contact_name
              FROM tasks t LEFT JOIN companies c ON c.id = t.company_id
              LEFT JOIN prospects p ON p.id = t.prospect_id
              {'WHERE ' + ' AND '.join(where) if where else ''}
              ORDER BY t.done, t.due_date = '', t.due_date, t.created_at DESC LIMIT ?"""
    async with state._connect() as db:
        cur = await db.execute(sql, (*params, int(limit)))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in await cur.fetchall()]
    today = date.today().isoformat()
    for r in rows:
        r["bucket"] = bucket(r, today)
        r.pop("ref", None)
    return rows


async def counts(state) -> dict:
    today = date.today().isoformat()
    async with state._connect() as db:
        cur = await db.execute(
            "SELECT SUM(due_date != '' AND due_date < ?), SUM(due_date = ?), COUNT(*) "
            "FROM tasks WHERE done = 0", (today, today))
        overdue, due_today, open_ = await cur.fetchone()
    return {"overdue": overdue or 0, "today": due_today or 0, "open": open_ or 0}


async def sync_reply_tasks(state) -> int:
    """A task due today for every reply waiting on a person: the prospect
    wrote last and the conversation isn't closed. Made once per reply."""
    async with state._connect() as db:
        cur = await db.execute(
            """SELECT cv.id, cv.status, cv.thread_json, p.id, p.company_id,
                      TRIM(COALESCE(p.first_name, '') || ' ' || COALESCE(p.last_name, '')),
                      COALESCE(c.name, p.company, '')
               FROM conversations cv JOIN prospects p ON p.id = cv.prospect_id
               LEFT JOIN companies c ON c.id = p.company_id
               WHERE cv.status != 'closed'""")
        rows = await cur.fetchall()
    made = 0
    for conv_id, status, thread_json, prospect_id, company_id, person, company in rows:
        try:
            thread = json.loads(thread_json or "[]")
        except ValueError:
            continue
        if not thread or not isinstance(thread[-1], dict) or thread[-1].get("sender") != "prospect":
            continue
        who = person or "them"
        at = f" at {company}" if company else ""
        title = (f"Harvey needs you: answer {who}{at}" if status == "needs_human"
                 else f"Reply to {who}{at}: they wrote back")
        ref = f"reply:{conv_id}:{thread[-1].get('timestamp') or len(thread)}"
        if await add(state, title, company_id=company_id or "", prospect_id=prospect_id,
                     due="today", source="harvey", ref=ref):
            made += 1
    return made


def describe_due(due_date: str) -> str:
    if not due_date:
        return ""
    try:
        d = date.fromisoformat(due_date)
    except ValueError:
        return due_date
    days = (d - date.today()).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days == -1:
        return "yesterday"
    if days < 0:
        return f"{-days} days late"
    if days < 7:
        return datetime.combine(d, datetime.min.time()).strftime("%A")
    return d.strftime("%-d %b")
