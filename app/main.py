import asyncio
import random
import time
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import jira
from .rooms import DECKS, User, create_room, gc_loop, rooms

FIELD_RE = re.compile(r"[\w-]{1,100}")
ISSUE_RE = re.compile(r"[A-Z][A-Z0-9_]*-\d+")
LINK_KEY_RE = re.compile(r"(?:/browse/|selectedIssue=)([A-Za-z][A-Za-z0-9_]*-\d+)")
EMOJI = {"🍅", "👋", "⏰", "🎈", "🎉", "🚀", "🐢", "💤", "❤️", "👀"}
STATIC = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(_):
    task = asyncio.create_task(gc_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class NewRoom(BaseModel):
    clientId: str
    deck: str = "fibonacci"
    password: str = ""
    ownerOnlyTasks: bool = False
    ownerOnlyReveal: bool = False
    project: str = ""


@app.get("/")
@app.get("/{room_id:uuid}")
async def index(room_id: uuid.UUID | None = None):
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
async def config():
    return {"decks": list(DECKS), "jira": jira.enabled(), "jiraFilter": jira.default_filter()}


@app.get("/api/projects")
async def projects():
    if not jira.enabled():
        return []
    try:
        return await jira.projects()
    except jira.JiraError:
        return []


@app.post("/api/rooms")
async def new_room(body: NewRoom):
    project = body.project.strip() if jira.enabled() and FIELD_RE.fullmatch(body.project.strip()) else ""
    return {"id": create_room(body.clientId, body.deck, body.password, body.ownerOnlyTasks, body.ownerOnlyReveal, project).id}


def issue_key(link: str) -> str:
    """Issue key from a Jira link (/browse/ABC-1, ?selectedIssue=ABC-1) or a bare key."""
    link = link.strip()[:500]
    m = LINK_KEY_RE.search(link)
    if m:
        return m.group(1).upper()
    return link.upper() if ISSUE_RE.fullmatch(link.upper()) else ""


ORDER_BY_RE = re.compile(r"\border\s+by\b", re.I)


def with_project(jql: str, project: str) -> str:
    """Restrict the query to the room's project whatever the JQL / saved filter says; ORDER BY stays at the end."""
    if not project:
        return jql
    m = ORDER_BY_RE.search(jql)
    cond, order = (jql[:m.start()], " " + jql[m.start():]) if m else (jql, "")
    cond = cond.strip()
    return f'project = "{project}"' + (f" AND ({cond})" if cond else "") + order


async def toast(ws: WebSocket, text: str, level: str = "info"):
    await ws.send_json({"type": "toast", "text": text, "level": level})


@app.websocket("/ws/{room_id}")
async def ws_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    room = rooms.get(room_id)
    if not room:
        await ws.send_json({"type": "error", "code": "not_found"})
        return await ws.close()

    user = None
    try:
        join = await ws.receive_json()
        cid = str(join.get("clientId", ""))[:64]
        name = str(join.get("name", "")).strip()[:30]
        if join.get("type") != "join" or not cid or not name:
            return await ws.close()
        if cid != room.owner_cid and not room.check_password(join.get("password", "")):
            code = "bad_password" if join.get("password") else "need_password"
            await ws.send_json({"type": "error", "code": code})
            return await ws.close()

        user = room.users.get(cid) or User(cid=cid, name=name)
        user.name = name
        user.observer = bool(join.get("observer"))
        if user.observer:
            user.vote = None
        user.conns.add(ws)
        room.users[cid] = user
        await ws.send_json({"type": "joined", "owner": room.is_host(cid)})
        await room.broadcast()

        while True:
            msg = await ws.receive_json()
            try:
                await handle(room, user, ws, msg)
            except jira.JiraError as e:
                await toast(ws, str(e), "error")
            if msg.get("type") in ("jira_project", "jira_load", "jira_add", "jira_push", "set_extra"):
                await ws.send_json({"type": "jira_idle", "op": msg["type"]})  # lets the client stop its spinner
            await room.broadcast()
    except WebSocketDisconnect:
        pass
    finally:
        if user:
            user.conns.discard(ws)
            if not user.conns:
                room.schedule_cleanup(user)
            await room.broadcast()


async def load_details(room):
    """Fetch description / comments / extra fields for the current issue and push them to everyone."""
    key = (room.task or {}).get("key")
    if not key or not jira.enabled():
        return
    try:
        details = await jira.issue_details(key, room.extra)
    except jira.JiraError as e:
        details = {"error": str(e)}
    if (room.task or {}).get("key") == key:
        room.task["details"] = details
        await room.broadcast()


async def handle(room, user, ws, m):
    t = m.get("type")
    is_owner = room.is_host(user.cid)

    if t == "vote":
        v = m.get("value")
        user.vote = v if (not user.observer and v in DECKS[room.deck]) else None
    elif t == "reveal":
        if room.revealed or room.counting:
            return
        if room.owner_reveal and not is_owner:
            return await toast(ws, "Only a host can reveal cards", "error")
        room.counting = True
        ms = random.randint(2000, 2500)  # a different suspense every time
        await room.send_all({"type": "countdown", "n": 3, "ms": ms})
        await asyncio.sleep(ms / 1000)
        if room.counting:  # not cancelled by a new round meanwhile
            room.counting = False
            room.revealed = True
    elif t == "throw":
        to, emoji = str(m.get("to", "")), m.get("emoji")
        now = time.monotonic()
        if emoji in EMOJI and to != user.pub and any(u.pub == to for u in room.users.values()) and now - user.last_throw > 0.3:
            user.last_throw = now
            await room.send_all({"type": "throw", "from": user.pub, "to": to, "emoji": emoji})
    elif t == "reset":
        room.new_round()
    elif t == "observer":
        user.observer = bool(m.get("value"))
        if user.observer:
            user.vote = None
    elif t == "task":
        if room.owner_tasks and not is_owner:
            return await toast(ws, "Only a host can switch tasks", "error")
        found = next((x for x in room.tasks if x["key"] == m.get("key")), None)
        if not found:
            return
        room.task = dict(found)
        room.new_round()
        await room.broadcast()
        await load_details(room)
    elif t == "make_host" and is_owner:
        target = next((u for u in room.users.values() if u.pub == m.get("to")), None)
        if target:
            room.hosts.add(target.cid)
    elif t == "remove_host" and is_owner:
        target = next((u for u in room.users.values() if u.pub == m.get("to")), None)
        if target and target.cid != room.owner_cid:  # the room creator always stays a host
            room.hosts.discard(target.cid)
    elif t == "set_options" and is_owner:
        room.owner_tasks = bool(m.get("ownerOnlyTasks"))
        room.owner_reveal = bool(m.get("ownerOnlyReveal"))
    elif t == "set_password" and is_owner:
        room.set_password(str(m.get("password", ""))[:100])
        await toast(ws, "Password set" if room.pw_hash else "Password removed", "ok")
    elif t == "set_project" and is_owner and jira.enabled():
        key = str(m.get("key", "")).strip()
        if not FIELD_RE.fullmatch(key) or key == room.project:
            return
        # issues, query and estimate field belong to the old project
        room.project, room.jql, room.est_field = key, "", ""
        room.tasks, room.task = [], None
        room.new_round()
    elif t == "jira_project" and is_owner and jira.enabled():
        key = str(m.get("key", "")).strip()
        if not FIELD_RE.fullmatch(key):
            return
        num, allf = await jira.fields(key)
        await ws.send_json({"type": "jira_fields", "project": key, "fields": num, "allFields": allf})
    elif t == "jira_load" and is_owner and jira.enabled():
        jql = str(m.get("jql", "")).strip()[:2000]
        field = str(m.get("field", "")).strip()
        if not jql:
            return await toast(ws, "Enter a JQL query", "error")
        if field and not FIELD_RE.fullmatch(field):
            return await toast(ws, "Invalid estimate field id (expected e.g. customfield_10016)", "error")
        found = await jira.search(with_project(jql, room.project), field)
        found_keys = {x["key"] for x in found}
        room.tasks = found + [x for x in room.tasks if x.get("manual") and x["key"] not in found_keys]  # keep hand-picked ones
        room.jql, room.est_field = jql, field
        await load_details(room)
        await toast(ws, f"Loaded {len(room.tasks)} issues", "ok")
    elif t == "jira_add" and is_owner and jira.enabled():
        key = issue_key(str(m.get("link", "")))
        if not key:
            return await toast(ws, "Could not find an issue key (e.g. ABC-123) in the link", "error")
        link = str(m.get("link", "")).strip()[:500]
        try:
            issue = await jira.issue(key, room.est_field)
        except jira.JiraError as e:
            # keep the task anyway with what we know: its key and (if a link was sent) the link
            issue = {"key": key, "summary": "", "status": "", "reporter": "", "estimate": None,
                     "url": link if link.lower().startswith(("http://", "https://")) else "",
                     "notFound": True}
            await toast(ws, f"{key} was not found in Jira ({e}), but it was added anyway", "warn")
        issue["manual"] = True
        existing = next((i for i, x in enumerate(room.tasks) if x["key"] == issue["key"]), None)
        if existing is None:
            room.tasks.append(issue)
        else:
            room.tasks[existing] = issue
        if not issue.get("notFound"):
            await toast(ws, f"{issue['key']} added", "ok")
    elif t == "set_extra" and is_owner and jira.enabled():
        room.extra = list(dict.fromkeys(x for x in map(str, m.get("extra") or []) if FIELD_RE.fullmatch(x)))[:20]
        await load_details(room)
    elif t == "jira_push" and is_owner and jira.enabled():
        key = (room.task or {}).get("key")
        value = str(m.get("value", "")).strip()
        if not key or not value:
            return
        saved = await jira.set_estimate(key, value, room.est_field)
        room.task["estimate"] = saved
        for x in room.tasks:
            if x["key"] == key:
                x["estimate"] = saved
        await toast(ws, f"{key}: estimate {saved} saved to Jira", "ok")


@app.get("/{path:path}")
async def unknown_page(path: str):
    """Unknown pages get the SPA, which shows "Room not found" with a way back to the landing page."""
    if path.startswith(("api/", "ws/", "static/")):
        raise HTTPException(404)
    return FileResponse(STATIC / "index.html")
