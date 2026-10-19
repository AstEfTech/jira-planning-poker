import asyncio
import random
import time
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import jira
from .rooms import DECKS, User, create_room, gc_loop, rooms

FIELD_RE = re.compile(r"[\w-]{1,100}")
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


@app.get("/")
@app.get("/{room_id:uuid}")
async def index(room_id: uuid.UUID | None = None):
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
async def config():
    return {"decks": list(DECKS), "jira": jira.enabled()}


@app.post("/api/rooms")
async def new_room(body: NewRoom):
    return {"id": create_room(body.clientId, body.deck, body.password, body.ownerOnlyTasks).id}


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
        await ws.send_json({"type": "joined", "owner": cid == room.owner_cid})
        await room.broadcast()

        while True:
            msg = await ws.receive_json()
            try:
                await handle(room, user, ws, msg)
            except jira.JiraError as e:
                await toast(ws, str(e), "error")
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
    is_owner = user.cid == room.owner_cid

    if t == "vote":
        v = m.get("value")
        user.vote = v if (not user.observer and v in DECKS[room.deck]) else None
    elif t == "reveal":
        if room.revealed or room.counting:
            return
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
            return await toast(ws, "Only the room owner can switch tasks", "error")
        found = next((x for x in room.tasks if x["key"] == m.get("key")), None)
        if not found:
            return
        room.task = dict(found)
        room.new_round()
        await room.broadcast()
        await load_details(room)
    elif t == "set_password" and is_owner:
        room.set_password(str(m.get("password", ""))[:100])
        await toast(ws, "Password set" if room.pw_hash else "Password removed", "ok")
    elif t == "jira_meta" and is_owner and jira.enabled():
        await ws.send_json({"type": "jira_meta", "projects": await jira.projects()})
    elif t == "jira_project" and is_owner and jira.enabled():
        key = str(m.get("key", "")).strip()
        if not FIELD_RE.fullmatch(key):
            return
        num, allf = await jira.fields(key)
        await ws.send_json({"type": "jira_fields", "project": key, "fields": num, "allFields": allf})
    elif t == "jira_load" and is_owner and jira.enabled():
        jql = str(m.get("jql", "")).strip()[:2000]
        field = str(m.get("field", "")).strip()
        project = str(m.get("project", "")).strip()
        if not jql:
            return await toast(ws, "Enter a JQL query or pick a project", "error")
        if field and not FIELD_RE.fullmatch(field):
            return await toast(ws, "Invalid estimate field id (expected e.g. customfield_10016)", "error")
        room.tasks = await jira.search(jql, field)
        room.jql, room.est_field = jql, field
        room.project = project if FIELD_RE.fullmatch(project) else ""
        await load_details(room)
        await toast(ws, f"Loaded {len(room.tasks)} issues", "ok")
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
