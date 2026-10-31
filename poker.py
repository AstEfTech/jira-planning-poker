"""Jira Planning Poker: the whole app in one file. Run: python poker.py"""
import asyncio
import hashlib
import hmac
import html
import logging
import os
import random
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel


# ---- Jira ------------------------------------------------------------------


class JiraError(Exception):
    pass


def cfg():
    """Only credentials come from the environment; JQL and estimate field are chosen in the UI."""
    return {
        "base": os.environ.get("JIRA_BASE_URL", "").rstrip("/"),
        "email": os.environ.get("JIRA_EMAIL", ""),
        "token": os.environ.get("JIRA_TOKEN", ""),
        "server": os.environ.get("JIRA_FLAVOR", "cloud").lower() == "server",
    }


def jira_default_filter() -> str:
    """JIRA_DEFAULT_FILTER as JQL: a bare number is a saved filter id, anything else is taken as JQL."""
    v = os.environ.get("JIRA_DEFAULT_FILTER", "").strip()
    return f"filter = {v}" if v.isdigit() else v


def jira_enabled() -> bool:
    c = cfg()
    return bool(c["base"] and c["token"])


def _api() -> str:
    return "2" if cfg()["server"] else "3"


def _client() -> httpx.AsyncClient:
    c = cfg()
    if c["email"]:
        return httpx.AsyncClient(auth=httpx.BasicAuth(c["email"], c["token"]), timeout=20)
    return httpx.AsyncClient(headers={"Authorization": f"Bearer {c['token']}"}, timeout=20)


def _check(r: httpx.Response):
    if r.is_success:
        return
    try:
        body = r.json()
        msg = "; ".join(body.get("errorMessages", []) + list(body.get("errors", {}).values()))
    except (ValueError, AttributeError):
        msg = ""
    raise JiraError(f"Jira {r.status_code}: {msg or r.text[:200]}")


async def _call(method: str, path: str, **kw) -> httpx.Response:
    try:
        async with _client() as cl:
            r = await cl.request(method, cfg()["base"] + path, **kw)
    except httpx.HTTPError as e:
        raise JiraError(f"Jira unreachable: {e}")
    _check(r)
    return r


def to_number(v):
    v = str(v).strip().replace("½", "0.5").replace(",", ".")
    try:
        return int(v) if float(v).is_integer() else float(v)
    except ValueError:
        return None


async def jira_projects() -> list[dict]:
    """Projects visible to the user, by name."""
    if cfg()["server"]:
        r = await _call("GET", "/rest/api/2/project")
        data = r.json()
    else:
        data, start = [], 0
        while True:
            r = await _call("GET", "/rest/api/3/project/search", params={"startAt": start, "maxResults": 100})
            page = r.json()
            data += page.get("values", [])
            start += len(page.get("values", []))
            if page.get("isLast", True) or not page.get("values"):
                break
    return sorted(({"key": p["key"], "name": p["name"]} for p in data), key=lambda p: p["name"].lower())


def _split(found: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    by_name = lambda f: f["name"].lower()
    allf = [{"id": i, "name": f["name"]} for i, f in found.items()]
    num = [{"id": i, "name": f["name"]} for i, f in found.items() if (f.get("schema") or {}).get("type") == "number"]
    return sorted(num, key=by_name), sorted(allf, key=by_name)


async def jira_fields(project: str = "") -> tuple[list[dict], list[dict]]:
    """(numeric fields — estimate candidates, all fields — candidates for the extra info), limited to the project when given."""
    found: dict[str, dict] = {}
    if project:
        try:
            if cfg()["server"]:
                r = await _call("GET", "/rest/api/2/issue/createmeta",
                                params={"projectKeys": project, "expand": "projects.issuetypes.fields"})
                for p in r.json().get("projects", []):
                    for t in p.get("issuetypes", []):
                        found.update(t.get("fields", {}))
            else:
                r = await _call("GET", f"/rest/api/3/issue/createmeta/{project}/issuetypes", params={"maxResults": 100})
                body = r.json()
                types = body.get("issueTypes") or body.get("values") or []
                pages = await asyncio.gather(*(
                    _call("GET", f"/rest/api/3/issue/createmeta/{project}/issuetypes/{t['id']}", params={"maxResults": 200})
                    for t in types))
                for pg in pages:
                    body = pg.json()
                    for f in body.get("fields") or body.get("values") or []:
                        found[f["fieldId"]] = f
        except JiraError:
            found = {}
    if not found:  # no project, or createmeta unavailable: every field of the instance
        r = await _call("GET", f"/rest/api/{_api()}/field")
        found = {f["id"]: f for f in r.json()}
    return _split(found)


async def jira_search(jql: str, field: str) -> list[dict]:
    c = cfg()
    path = "/rest/api/2/search" if c["server"] else "/rest/api/3/search/jql"
    body = {"jql": jql, "fields": ["summary", "status", "reporter", *([field] if field else [])], "maxResults": 100}
    r = await _call("POST", path, json=body)
    return [_task(i, field) for i in r.json().get("issues", [])]


def _task(i: dict, field: str) -> dict:
    f = i["fields"]
    est = f.get(field) if field else None
    return {
        "key": i["key"],
        "summary": f.get("summary", ""),
        "status": (f.get("status") or {}).get("name", ""),
        "reporter": value_text(f.get("reporter")),
        "estimate": est if isinstance(est, (int, float, str)) else None,
        "url": f"{cfg()['base']}/browse/{i['key']}",
    }


async def jira_issue(key: str, field: str) -> dict:
    """A single issue in the same shape as the search results."""
    flds = ",".join(["summary", "status", "reporter", *([field] if field else [])])
    r = await _call("GET", f"/rest/api/{_api()}/issue/{key}", params={"fields": flds})
    return _task(r.json(), field)


NL = chr(10)
_BLOCK_INLINE ={"paragraph", "heading", "codeBlock"}


def adf_text(n) -> str:
    """Flatten an Atlassian Document Format node to plain text."""
    t, kids, a = n.get("type"), n.get("content") or [], n.get("attrs") or {}
    if t == "text":
        return n.get("text", "")
    if t == "hardBreak":
        return NL
    if t in ("mention", "emoji"):
        return a.get("text") or a.get("shortName", "")
    if t == "inlineCard":
        return a.get("url", "")
    if t == "media":
        return "[attachment]"
    if t == "bulletList":
        return NL.join("• " + adf_text(k) for k in kids)
    if t == "orderedList":
        return NL.join(f"{i}. " + adf_text(k) for i, k in enumerate(kids, 1))
    if t == "tableRow":
        return " | ".join(adf_text(k).replace(NL, " ") for k in kids)
    return ("" if t in _BLOCK_INLINE else NL).join(adf_text(k) for k in kids)


def value_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return ", ".join(x for x in map(value_text, v) if x)
    if isinstance(v, dict):
        if v.get("type") == "doc":
            return adf_text(v).strip()
        for k in ("displayName", "name", "value", "key", "summary"):
            if v.get(k):
                return str(v[k])
    return ""


_TAGS = {"p", "br", "hr", "b", "strong", "i", "em", "u", "s", "del", "strike", "ins", "sub", "sup", "code", "pre", "tt",
         "blockquote", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "thead", "tbody", "tr", "th", "td",
         "a", "span", "div", "dl", "dt", "dd"}
_VOID = {"br", "hr"}
_DROP = {"script", "style", "iframe", "object", "embed", "svg", "form", "textarea", "select", "button", "noscript"}  # with their content
_COLOR_RE = re.compile(r"(?:^|;)\s*color\s*:\s*(#[0-9a-fA-F]{3,8}|rgb\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)|[a-zA-Z]{3,20})\s*(?:;|$)")


class _Sanitizer(HTMLParser):
    """Keeps Jira's rendered formatting (lists, tables, code, links, colors) and nothing executable."""

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base, self.out, self.stack, self.skip = base, [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in _DROP:
            self.skip += 1
            return
        if self.skip:
            return
        a = dict(attrs)
        if tag == "img":  # images need Jira auth; emoticons carry their text in alt
            alt = a.get("alt") or ""
            self.out.append(html.escape(alt) if "emoticon" in (a.get("class") or "") and alt else "<em>[image]</em>")
            return
        if tag not in _TAGS:
            return
        extra = ""
        if tag == "a":
            href = urljoin(self.base + "/", (a.get("href") or "").strip())
            if href.lower().startswith(("http://", "https://", "mailto:")):
                extra = f' href="{html.escape(href)}" target="_blank" rel="noopener noreferrer"'
        elif tag in ("td", "th"):
            extra = "".join(f' {k}="{a[k]}"' for k in ("colspan", "rowspan") if (a.get(k) or "").isdigit())
        m = _COLOR_RE.search(a.get("style") or "") if tag in ("span", "p", "td", "th", "div") else None
        if m:
            extra += f' style="color:{m.group(1)}"'
        self.out.append(f"<{tag}{extra}>")
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in _DROP:
            self.skip = max(0, self.skip - 1)
        elif not self.skip and tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        self.out += [f"</{t}>" for t in reversed(self.stack)]
        return "".join(self.out)


def clean_html(raw: str, limit: int = 60000) -> str:
    p = _Sanitizer(cfg()["base"])
    p.feed(raw[:limit])
    p.close()
    return p.result()


def _body_html(raw_value, rendered_html, limit: int) -> str:
    """Jira's own rendering keeps the formatting; without it fall back to the flattened text."""
    if rendered_html:
        return clean_html(rendered_html)
    return html.escape(value_text(raw_value)[:limit]).replace(NL, "<br>")


async def jira_issue_details(key: str, extra: list[str]) -> dict:
    flds = ",".join(["description", "comment", *extra])
    r = await _call("GET", f"/rest/api/{_api()}/issue/{key}", params={"fields": flds, "expand": "names,renderedFields"})
    data = r.json()
    f, names, rendered = data.get("fields", {}), data.get("names", {}), data.get("renderedFields") or {}
    comments = (f.get("comment") or {}).get("comments", [])
    rbody = {c.get("id"): c.get("body") for c in (rendered.get("comment") or {}).get("comments", [])}
    return {
        "description": _body_html(f.get("description"), rendered.get("description"), 20000),
        "comments": [
            {"author": (c.get("author") or {}).get("displayName", "?"), "created": c.get("created", ""),
             "body": _body_html(c.get("body"), rbody.get(c.get("id")), 3000)}
            for c in comments[-50:]
        ],
        "extra": [{"id": x, "name": names.get(x, x), "value": value_text(f.get(x))[:3000]} for x in extra],
    }


async def jira_set_estimate(key: str, value, field: str) -> object:
    if not field:
        raise JiraError("Estimate field is not set — choose it in the Jira panel and press Load")
    num = to_number(value)
    val = num if num is not None else str(value)
    await _call("PUT", f"/rest/api/{_api()}/issue/{key}", json={"fields": {field: val}})
    return val

# ---- Rooms -----------------------------------------------------------------


DECKS = {
    "fibonacci": ["0", "½", "1", "2", "3", "5", "8", "13", "21", "34", "?", "☕"],
    "tshirt": ["XS", "S", "M", "L", "XL", "XXL", "?", "☕"],
    "pow2": ["0", "1", "2", "4", "8", "16", "32", "?", "☕"],
}

ROOM_TTL = 24 * 3600
OFFLINE_GRACE = 20  # seconds before a disconnected user disappears from the table


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)


@dataclass
class User:
    cid: str  # secret client id, never sent to other clients
    name: str
    observer: bool = False
    vote: str | None = None
    conns: set = field(default_factory=set)
    last_throw: float = 0.0

    @property
    def pub(self) -> str:
        return hashlib.sha256(self.cid.encode()).hexdigest()[:12]


@dataclass
class Room:
    id: str
    owner_cid: str
    deck: str = "fibonacci"
    revealed: bool = False
    task: dict | None = None
    tasks: list = field(default_factory=list)
    project: str = ""
    jql: str = ""
    est_field: str = ""
    extra: list = field(default_factory=lambda: ["assignee"])
    counting: bool = False
    owner_tasks: bool = False  # only the owner may switch the current task
    owner_reveal: bool = False  # only the owner may reveal the cards
    hosts: set = field(default_factory=set)  # cids allowed to act as host (the owner is always one)
    users: dict = field(default_factory=dict)
    salt: bytes = b""
    pw_hash: bytes | None = None
    touched: float = field(default_factory=time.time)

    def is_host(self, cid: str) -> bool:
        return cid == self.owner_cid or cid in self.hosts

    def set_password(self, password: str):
        if password:
            self.salt = os.urandom(16)
            self.pw_hash = _hash(password, self.salt)
        else:
            self.pw_hash = None

    def check_password(self, password: str) -> bool:
        return self.pw_hash is None or hmac.compare_digest(self.pw_hash, _hash(password or "", self.salt))

    def new_round(self):
        self.revealed = False
        self.counting = False
        for u in self.users.values():
            u.vote = None

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "deck": DECKS[self.deck],
            "revealed": self.revealed,
            "hasPassword": self.pw_hash is not None,
            "ownerId": hashlib.sha256(self.owner_cid.encode()).hexdigest()[:12],
            "task": self.task,
            "tasks": self.tasks,
            "project": self.project,
            "jql": self.jql,
            "field": self.est_field,
            "extra": self.extra,
            "ownerOnlyTasks": self.owner_tasks,
            "ownerOnlyReveal": self.owner_reveal,
            "users": [
                {
                    "id": u.pub,
                    "name": u.name,
                    "observer": u.observer,
                    "host": self.is_host(u.cid),
                    "online": bool(u.conns),
                    "voted": u.vote is not None,
                    "vote": u.vote if self.revealed else None,
                }
                for u in self.users.values()
            ],
        }

    async def broadcast(self):
        self.touched = time.time()
        snap = self.snapshot()
        for u in list(self.users.values()):
            for ws in list(u.conns):
                try:
                    await ws.send_json({"type": "state", "room": snap, "me": u.pub, "myVote": u.vote})
                except Exception:
                    u.conns.discard(ws)

    async def send_all(self, msg: dict):
        for u in list(self.users.values()):
            for ws in list(u.conns):
                try:
                    await ws.send_json(msg)
                except Exception:
                    u.conns.discard(ws)

    def schedule_cleanup(self, user: User):
        async def later():
            await asyncio.sleep(OFFLINE_GRACE)
            if not user.conns and self.users.get(user.cid) is user:
                del self.users[user.cid]
                await self.broadcast()

        asyncio.get_running_loop().create_task(later())


rooms: dict[str, Room] = {}


def create_room(owner_cid: str, deck: str, password: str, owner_tasks: bool = False, owner_reveal: bool = False, project: str = "") -> Room:
    room = Room(id=str(uuid.uuid4()), owner_cid=owner_cid, deck=deck if deck in DECKS else "fibonacci", owner_tasks=owner_tasks, owner_reveal=owner_reveal, project=project)
    room.set_password(password)
    rooms[room.id] = room
    return room


async def gc_loop():
    while True:
        await asyncio.sleep(600)
        now = time.time()
        for rid, r in list(rooms.items()):
            if now - r.touched > ROOM_TTL and not any(u.conns for u in r.users.values()):
                del rooms[rid]

# ---- Server ----------------------------------------------------------------


log = logging.getLogger("poker")
FIELD_RE =re.compile(r"[\w-]{1,100}")
ISSUE_RE = re.compile(r"[A-Z][A-Z0-9_]*-\d+")
LINK_KEY_RE = re.compile(r"(?:/browse/|selectedIssue=)([A-Za-z][A-Za-z0-9_]*-\d+)")
EMOJI = {"🍅", "👋", "⏰", "🎈", "🎉", "🚀", "🐢", "💤", "❤️", "👀"}


@asynccontextmanager
async def lifespan(_):
    task = asyncio.create_task(gc_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


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
    return HTMLResponse(PAGE)


@app.get("/api/config")
async def config():
    return {"decks": list(DECKS), "jira": jira_enabled(), "jiraFilter": jira_default_filter()}


@app.get("/api/projects")
async def projects():
    if not jira_enabled():
        return []
    try:
        return await jira_projects()
    except JiraError:
        return []


@app.post("/api/rooms")
async def new_room(body: NewRoom):
    project = body.project.strip() if jira_enabled() and FIELD_RE.fullmatch(body.project.strip()) else ""
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
            if not isinstance(msg, dict):
                continue
            try:
                await handle(room, user, ws, msg)
            except JiraError as e:
                await toast(ws, str(e), "error")
            except WebSocketDisconnect:
                raise
            except Exception:  # a bad message or an unexpected Jira reply must not drop the connection
                log.exception("Failed to handle %r", msg.get("type"))
                await toast(ws, "Something went wrong", "error")
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
    if not key or not jira_enabled():
        return
    try:
        details = await jira_issue_details(key, room.extra)
    except JiraError as e:
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
            return await toast(ws, "Only a host can start estimation", "error")
        found = next((x for x in room.tasks if x["key"] == m.get("key")), None)
        if not found:
            return
        room.task = dict(found)
        room.new_round()
        await room.broadcast()
        await load_details(room)
    elif t == "view" and jira_enabled():
        # anyone may read any listed issue; this goes to the asker only and doesn't touch the room's current task
        key = m.get("key")
        if any(x["key"] == key for x in room.tasks):
            try:
                details = await jira_issue_details(key, room.extra)
            except JiraError as e:
                details = {"error": str(e)}
            await ws.send_json({"type": "details", "key": key, "details": details})
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
    elif t == "set_project" and is_owner and jira_enabled():
        key = str(m.get("key", "")).strip()
        if not FIELD_RE.fullmatch(key) or key == room.project:
            return
        # issues, query and estimate field belong to the old project
        room.project, room.jql, room.est_field = key, "", ""
        room.tasks, room.task = [], None
        room.new_round()
    elif t == "jira_project" and is_owner and jira_enabled():
        key = str(m.get("key", "")).strip()
        if not FIELD_RE.fullmatch(key):
            return
        num, allf = await jira_fields(key)
        await ws.send_json({"type": "jira_fields", "project": key, "fields": num, "allFields": allf})
    elif t == "jira_load" and is_owner and jira_enabled():
        jql = str(m.get("jql", "")).strip()[:2000]
        field = str(m.get("field", "")).strip()
        if not jql:
            return await toast(ws, "Enter a JQL query", "error")
        if field and not FIELD_RE.fullmatch(field):
            return await toast(ws, "Invalid estimate field id (expected e.g. customfield_10016)", "error")
        found = await jira_search(with_project(jql, room.project), field)
        found_keys = {x["key"] for x in found}
        room.tasks = found + [x for x in room.tasks if x.get("manual") and x["key"] not in found_keys]  # keep hand-picked ones
        room.jql, room.est_field = jql, field
        await load_details(room)
        await toast(ws, f"Loaded {len(room.tasks)} issues", "ok")
    elif t == "jira_add" and is_owner and jira_enabled():
        key = issue_key(str(m.get("link", "")))
        if not key:
            return await toast(ws, "Could not find an issue key (e.g. ABC-123) in the link", "error")
        link = str(m.get("link", "")).strip()[:500]
        try:
            issue = await jira_issue(key, room.est_field)
        except JiraError as e:
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
    elif t == "set_extra" and is_owner and jira_enabled():
        room.extra = list(dict.fromkeys(x for x in map(str, m.get("extra") or []) if FIELD_RE.fullmatch(x)))[:20]
        await load_details(room)
        await ws.send_json({"type": "details_stale"})  # the host's own viewed issue needs the new fields too
    elif t == "jira_push" and is_owner and jira_enabled():
        key = (room.task or {}).get("key")
        value = str(m.get("value", "")).strip()
        if not key or not value:
            return
        saved = await jira_set_estimate(key, value, room.est_field)
        room.task["estimate"] = saved
        for x in room.tasks:
            if x["key"] == key:
                x["estimate"] = saved
        await toast(ws, f"{key}: estimate {saved} saved to Jira", "ok")


@app.get("/{path:path}")
async def unknown_page(path: str):
    """Unknown pages get the SPA, which shows "Room not found" with a way back to the landing page."""
    if path.startswith(("api/", "ws/")):
        raise HTTPException(404)
    return HTMLResponse(PAGE)


# ---- Front end (HTML + CSS + JS) ----------------------------------------

PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Planning Poker</title>
<style>
:root {
  --bg: #e6e8f0; --panel: #f4f5fa; --text: #1b2033; --muted: #656c84; --border: #d0d4e2; --slot: #9aa2c2;
  --accent: #5b6cff; --accent-hover: #4a5af0; --accent-soft: rgba(91, 108, 255, .10); --accent-text: #fff;
  --ok: #1a9d62; --err: #d6334a; --card-back: #5b6cff;
  --shadow: 0 1px 2px rgba(20, 24, 50, .05), 0 4px 14px rgba(20, 24, 50, .06);
  --radius: 12px;
  --font: -apple-system, BlinkMacSystemFont, "Segoe UI Variable", "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
:root[data-theme="dark"] {
  --bg: #12141c; --panel: #1c1f2e; --text: #e6e8f2; --muted: #8b92aa; --border: #2b3045; --slot: #4d5578;
  --accent: #7584ff; --accent-hover: #8a97ff; --accent-soft: rgba(117, 132, 255, .14); --accent-text: #0e1020;
  --ok: #38c987; --err: #ff6b81; --card-back: #4a58d6;
  --shadow: 0 1px 2px rgba(0, 0, 0, .3), 0 4px 14px rgba(0, 0, 0, .25);
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; min-height: 100vh; font: 15px/1.5 var(--font); background: var(--bg); color: var(--text);
  -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility;
}
[hidden] { display: none !important; }

/* header: room settings | logo | personal settings */
header {
  position: sticky; top: 0; z-index: 10; display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 8px;
  padding: 10px 24px; background: var(--panel); border-bottom: 1px solid var(--border);
}
.hgroup { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; min-width: 0; }
.hgroup.right { justify-content: flex-end; }
.logo { font-weight: 700; font-size: 17px; letter-spacing: -.01em; color: inherit; text-decoration: none; text-align: center; white-space: nowrap; }
@media (max-width: 760px) {
  header { grid-template-columns: 1fr 1fr; }
  .logo { grid-column: 1 / -1; grid-row: 1; }
}

main { padding: 24px; max-width: 1760px; margin: 0 auto; }
#room { padding-bottom: 150px; }
h1 { margin: 0 0 4px; font-size: 22px; font-weight: 700; letter-spacing: -.02em; }
h3 { margin: 0 0 12px; font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }

.panel { background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); padding: 18px; margin-bottom: 16px; }
.narrow { max-width: 420px; margin: 8vh auto 0; display: flex; flex-direction: column; gap: 16px; padding: 28px; }

/* form controls */
label { display: flex; flex-direction: column; gap: 6px; color: var(--muted); font-size: 13px; font-weight: 500; }
label.check { flex-direction: row; align-items: center; gap: 8px; cursor: pointer; }
label.check input { width: 16px; height: 16px; padding: 0; accent-color: var(--accent); }
input, select, button, textarea {
  font: inherit; line-height: 1.2; color: var(--text); background: var(--bg);
  border: 1px solid var(--border); border-radius: 8px; padding: 9px 12px;
  transition: border-color .15s, background .15s, box-shadow .15s, transform .12s;
}
input, select, textarea { width: 100%; min-width: 0; }
textarea { resize: vertical; font-size: 13px; line-height: 1.4; font-family: ui-monospace, Consolas, monospace; }
input::placeholder { color: var(--muted); opacity: .7; }
input:focus, select:focus, textarea:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
button { cursor: pointer; white-space: nowrap; font-weight: 500; }
button:hover:not(:disabled) { border-color: var(--accent); }
button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
button:disabled { opacity: .5; cursor: default; }
button.primary { background: var(--accent); color: var(--accent-text); border-color: var(--accent); font-weight: 600; }
button.primary:hover:not(:disabled) { background: var(--accent-hover); border-color: var(--accent-hover); }
button.ghost { background: transparent; padding: 7px 12px; font-size: 14px; }
button.ghost:hover:not(:disabled) { background: var(--accent-soft); }
button.on { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }
.narrow button.primary { padding: 11px 12px; }

/* landing */
#landing { padding-top: 4vh; }
.hero { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(320px, 420px); gap: 56px; align-items: center; max-width: 1080px; margin: 0 auto; }
@media (max-width: 860px) { .hero { grid-template-columns: minmax(0, 1fr); gap: 32px; } }
.hero-text { position: relative; }
.eyebrow { display: inline-block; padding: 4px 12px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font-size: 13px; font-weight: 600; }
.hero h1 { margin: 16px 0 14px; font-size: clamp(34px, 5vw, 54px); line-height: 1.08; letter-spacing: -.03em; }
.grad { background: linear-gradient(90deg, var(--accent), #c06bff); -webkit-background-clip: text; background-clip: text; color: transparent; }
.lead { margin: 0 0 20px; max-width: 520px; font-size: 17px; line-height: 1.55; color: var(--muted); }
.chips { list-style: none; display: flex; flex-wrap: wrap; gap: 8px; margin: 0; padding: 0; }
.chips li { padding: 6px 12px; border: 1px solid var(--border); border-radius: 999px; background: var(--panel); font-size: 13px; font-weight: 500; box-shadow: var(--shadow); }
.fan { position: relative; height: 150px; margin-top: 36px; }
.fan div {
  position: absolute; bottom: 0; left: calc(var(--i) * 74px); width: 86px; height: 122px; display: grid; place-items: center;
  border: 2px solid var(--accent); border-radius: 12px; background: var(--panel); color: var(--accent); font-size: 32px; font-weight: 700;
  box-shadow: var(--shadow); transform: rotate(var(--r)) translateY(var(--y)); transform-origin: bottom center; transition: transform .25s;
}
.fan div:nth-child(1) { --i: 0; --r: -12deg; --y: 8px; }
.fan div:nth-child(2) { --i: 1; --r: -4deg; --y: 0; }
.fan div:nth-child(3) { --i: 2; --r: 4deg; --y: 0; }
.fan div:nth-child(4) { --i: 3; --r: 12deg; --y: 8px; background: var(--accent); color: var(--accent-text); }
.fan div:hover { transform: rotate(var(--r)) translateY(-14px); }
.hero-card { display: flex; flex-direction: column; gap: 16px; padding: 28px; margin: 0; box-shadow: 0 20px 50px rgba(60, 70, 160, .18); }
.hero-card h2 { margin: 0; font-size: 20px; letter-spacing: -.02em; }
.hero-card label small { font-weight: 400; opacity: .8; }
.hero-card button.primary { padding: 12px; font-size: 16px; }
body:has(#landing:not([hidden])) { background: radial-gradient(ellipse 70% 45% at 25% 0%, var(--accent-soft), transparent 70%), var(--bg); }

/* settings fold */
.jhead { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 12px; }
.jhead h3 { margin: 0; }
.jhead button { padding: 4px 10px; font-size: 13px; }
#jira.folded #jiraCfg { display: none; }
.cfgbox { background: var(--bg); border: 1px solid var(--border); border-radius: 10px; }
.cfghead { display: block; width: 100%; text-align: left; padding: 9px 12px; border: none; border-radius: 10px; background: transparent; font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
.cfghead:hover:not(:disabled) { color: var(--text); background: var(--accent-soft); }
#jira:not(.folded) .cfghead { border-bottom: 1px solid var(--border); border-radius: 10px 10px 0 0; }
#jiraCfg { padding: 12px; }
.loadrow { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin: 12px 0 0; width: 100%; align-self: stretch; justify-items: stretch; }
.loadrow[hidden] { display: none; }
.loadrow button { width: 100%; min-width: 0; margin: 0; box-sizing: border-box; height: 36px; padding: 0 12px; border-width: 1px; border-style: solid; font-size: 14px; font-weight: 600; line-height: 1; display: flex; align-items: center; justify-content: center; white-space: nowrap; }
.tasks li { position: relative; }
.tasks .manual { position: absolute; top: 4px; right: 6px; font-size: 12px; line-height: 1; cursor: help; }
.tasks li.has-manual .est { margin-right: 18px; }
.loadrow + .tasks { margin-top: 12px; }
#jiraCfg input, #jiraCfg textarea { background: var(--panel); }
.err { color: var(--err); margin: 0; font-size: 14px; }
.err:empty { display: none; }

.row { display: flex; gap: 8px; align-items: center; margin-top: 12px; }
.row > input { flex: 1; }
.row.center { justify-content: center; margin: 4px 0 20px; gap: 10px; }
.row.center button { padding: 10px 20px; }
.push { border-top: 1px dashed var(--border); padding-top: 12px; }
.push span { color: var(--muted); font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.push { flex-wrap: wrap; }
.pill { max-width: 100%; display: flex; align-items: stretch; flex: 0 0 auto; width: 240px; background: var(--bg); border: 1px solid var(--accent); border-radius: 999px; overflow: hidden; box-shadow: 0 0 0 3px var(--accent-soft); transition: border-color .15s, box-shadow .15s; }
.pill:focus-within { box-shadow: 0 0 0 3px var(--accent-soft), 0 0 0 5px var(--accent-soft); }
.pill input { flex: 1; width: auto; border: none; border-radius: 0; background: transparent; box-shadow: none; padding: 8px 8px 8px 16px; text-align: left; }
.pill button { border: none; border-radius: 0; padding: 8px 16px; font-size: 14px; }

/* layout */
.layout { display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 20px; align-items: start; }
.layout:has(#jira[hidden]) { grid-template-columns: minmax(0, 1fr); }
@media (max-width: 860px) { #room { padding-bottom: 210px; } .layout { grid-template-columns: minmax(0, 1fr); } main { padding: 16px; } header { padding: 10px 16px; } }

.taskhead { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.taskhead .task { min-width: 0; }
.reporter { flex: 0 0 auto; max-width: 40%; padding-top: 3px; color: var(--muted); font-size: 13px; text-align: right; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.reporter b { color: var(--text); font-weight: 600; }
.task { font-size: 17px; font-weight: 600; line-height: 1.4; }
.task a { color: var(--accent); text-decoration: none; }
.task a:hover { text-decoration: underline; }
.task small { color: var(--muted); font-weight: 400; font-size: 13px; margin-left: 4px; }

/* table */
.table {
  display: flex; flex-wrap: wrap; gap: 32px 28px; justify-content: center; align-content: center; align-items: flex-start;
  padding: 48px 24px; min-height: max(380px, calc(100vh - 460px));
  background: radial-gradient(ellipse at center, var(--accent-soft), transparent 70%), var(--panel);
  border: 1px solid var(--border); border-radius: 20px; box-shadow: var(--shadow);
}
.player { display: flex; flex-direction: column; align-items: center; gap: 10px; width: 108px; }
.player.off { opacity: .4; }
.pname { font-size: 13px; font-weight: 500; max-width: 108px; text-align: center; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.player { perspective: 700px; }
.pcard { position: relative; width: 76px; height: 108px; border-radius: 11px; border: 2px dashed var(--slot); display: grid; place-items: center; font-size: 30px; font-weight: 700; transition: transform .25s; }
.pcard.voted { background-color: var(--card-back); border: 2px solid var(--card-back); background-image: repeating-linear-gradient(45deg, transparent 0 6px, rgba(255, 255, 255, .13) 6px 12px); box-shadow: var(--shadow); }
.pcard.shown { background: none; border: none; transform-style: preserve-3d; }
.pcard .face { position: absolute; inset: 0; display: grid; place-items: center; border-radius: 11px; backface-visibility: hidden; -webkit-backface-visibility: hidden; }
.pcard .front { background: var(--panel); border: 2px solid var(--accent); color: var(--accent); box-shadow: var(--shadow); }
.pcard .back { transform: rotateY(180deg); background-color: var(--card-back); border: 2px solid var(--card-back); background-image: repeating-linear-gradient(45deg, transparent 0 6px, rgba(255, 255, 255, .13) 6px 12px); box-shadow: var(--shadow); }
.pcard.flip { animation: flip .8s cubic-bezier(.3, .7, .3, 1) backwards; }
@keyframes flip { from { transform: rotateY(180deg); } 50% { transform: rotateY(90deg) translateY(-14px) scale(1.12); } to { transform: rotateY(0); } }

/* reveal countdown (confined to the table) */
.tablewrap { position: relative; margin-bottom: 16px; }
/* desktop: the board absorbs height changes (e.g. expanding "More") instead of being pushed down */
@media (min-width: 861px) {
  #room { padding-bottom: 170px; }  /* room for the fixed deck bar when the page scrolls */
  .col { display: flex; flex-direction: column; height: max(600px, calc(100vh - 235px)); }
  .col > * { flex: 0 0 auto; }
  .col > .tablewrap { flex: 1 1 auto; min-height: 120px; display: flex; }
  .tablewrap > .table { flex: 1 1 auto; min-height: 0; }
}
#countdown { position: absolute; inset: 0; z-index: 5; display: grid; place-items: center; border-radius: 20px; background: rgba(10, 12, 24, .45); backdrop-filter: blur(2px); pointer-events: none; }
#countdown span { font-size: 130px; font-weight: 800; line-height: 1; color: #fff; text-shadow: 0 6px 30px rgba(0, 0, 0, .4); animation: cdpop var(--step, 667ms) ease-out; }
.table.counting { box-shadow: 0 0 0 3px var(--accent-soft), 0 0 40px var(--accent-soft); animation: tpulse 1s ease-in-out infinite; }
.table.counting .pcard.voted { animation: shake .35s ease-in-out infinite; }
@keyframes tpulse { 50% { border-color: var(--accent); } }
@keyframes shake { 0%, 100% { transform: rotate(0); } 25% { transform: rotate(-4deg); } 75% { transform: rotate(4deg); } }
@keyframes cdpop { from { transform: scale(.4); opacity: 0; } 25% { transform: scale(1.1); opacity: 1; } to { transform: scale(.9); opacity: .6; } }
.pcard.obs { border: none; font-size: 32px; }

/* results */
.results { display: flex; gap: 12px; justify-content: center; flex-wrap: wrap; margin-bottom: 20px; }
.results:empty { display: none; }
.stat { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; box-shadow: var(--shadow); padding: 10px 22px; text-align: center; min-width: 96px; }
.stat b { display: block; font-size: 24px; line-height: 1.2; }
.stat span { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
.stat.good b { color: var(--ok); }

/* deck */
.deckbar {
  position: fixed; left: 0; right: 0; bottom: 0; z-index: 9; display: flex; justify-content: center;
  padding: 18px 16px 14px; background: var(--panel); border-top: 1px solid var(--border); box-shadow: 0 -6px 20px rgba(20, 24, 50, .08);
}
.deck { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; max-width: 1400px; }
.hint { color: var(--muted); padding: 30px 0; font-size: 14px; }
.dcard { width: 66px; height: 96px; font-size: 22px; font-weight: 700; padding: 0; border-width: 2px; border-radius: 10px; background: var(--panel); box-shadow: var(--shadow); }
@media (hover: hover) { .dcard:hover { transform: translateY(-6px); } }
.dcard.sel { background: var(--accent); color: var(--accent-text); border-color: var(--accent); transform: translateY(-12px); }

/* jira */
.form { display: flex; flex-direction: column; gap: 12px; }
.tasks .empty { color: var(--muted); cursor: default; }
.tasks .empty:hover { background: none; }
/* jira list */
#jira { position: sticky; top: 72px; align-self: start; display: flex; flex-direction: column; max-height: calc(100vh - 72px - 170px); }
#jira > * { flex: 0 0 auto; }
#jira > .tasks { flex: 1 1 auto; min-height: 90px; }
@media (max-width: 860px) { #jira { position: static; max-height: none; } .tasks { max-height: 50vh; } }
.tasks { list-style: none; margin: 12px 0 0; padding: 0; overflow: auto; display: flex; flex-direction: column; gap: 4px; }
.tasks li { padding: 9px 10px; border-radius: 8px; cursor: pointer; border: 1px solid transparent; font-size: 14px; line-height: 1.4; }
.tasks li:hover { background: var(--bg); }
.tasks li.cur { border-color: var(--accent); background: var(--accent-soft); }
.tasks .k { font-weight: 600; font-size: 13px; color: var(--accent); }
.tasks .est { float: right; background: var(--ok); color: #fff; border-radius: 10px; padding: 0 8px; font-size: 12px; font-weight: 600; }
.tasks .st { color: var(--muted); font-size: 12px; }

/* toasts */
#toasts { position: fixed; bottom: 150px; right: 16px; display: flex; flex-direction: column; gap: 8px; z-index: 20; }
.toast { background: var(--panel); border: 1px solid var(--border); border-left: 4px solid var(--accent); padding: 10px 14px; border-radius: 8px; box-shadow: 0 6px 20px rgba(0, 0, 0, .2); max-width: 340px; font-size: 14px; }
.toast.ok { border-left-color: var(--ok); }
.toast.error { border-left-color: var(--err); }
.toast.warn { border-left-color: #e5a50a; background: #fff6d6; color: #5c4400; }
:root[data-theme="dark"] .toast.warn { background: #3a3012; color: #ffe08a; }

/* task details */
.fold { margin-top: 12px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg); }
.fold > summary { cursor: pointer; padding: 9px 12px; font-size: 14px; font-weight: 600; color: var(--muted); user-select: none; }
.fold[open] > summary { border-bottom: 1px solid var(--border); color: var(--text); }
.foldcols { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); align-items: stretch; }
.foldcols:has(#extraBox[hidden]) { grid-template-columns: minmax(0, 1fr); }
@media (max-width: 700px) { .foldcols { grid-template-columns: minmax(0, 1fr); } }
.foldbody { padding: 12px; max-height: 22vh; overflow: auto; margin: 0; font-size: 14px; }
.desc { overflow-wrap: anywhere; line-height: 1.5; }
.desc > :first-child { margin-top: 0; }
.desc > :last-child { margin-bottom: 0; }
.desc p { margin: 0 0 .6em; }
.desc ul, .desc ol { margin: .4em 0; padding-left: 1.5em; }
.desc h1, .desc h2, .desc h3, .desc h4, .desc h5, .desc h6 { margin: .8em 0 .4em; line-height: 1.3; }
.desc h1 { font-size: 1.4em; } .desc h2 { font-size: 1.25em; } .desc h3 { font-size: 1.1em; } .desc h4, .desc h5, .desc h6 { font-size: 1em; }
.desc a { color: var(--accent); }
.desc pre, .desc code, .desc tt { font-family: ui-monospace, Consolas, monospace; font-size: 13px; }
.desc pre { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; overflow: auto; white-space: pre; margin: .5em 0; }
.desc :not(pre) > code, .desc tt { background: var(--panel); border-radius: 4px; padding: 1px 4px; }
.desc blockquote { margin: .5em 0; padding-left: 10px; border-left: 3px solid var(--border); color: var(--muted); }
.desc table { border-collapse: collapse; margin: .5em 0; display: block; overflow: auto; }
.desc th, .desc td { border: 1px solid var(--border); padding: 4px 8px; vertical-align: top; }
.desc th { background: var(--panel); }
.desc hr { border: none; border-top: 1px solid var(--border); }
.badge { display: inline-block; background: var(--accent); color: var(--accent-text); border-radius: 10px; padding: 1px 8px; font-size: 11px; font-weight: 600; letter-spacing: .02em; vertical-align: middle; }
.tasks li > .badge { margin-bottom: 4px; }
.tasks li.viewed:not(.cur) { border-color: var(--border); background: var(--bg); }
.tasks .mini { float: right; margin-left: 6px; padding: 0 8px; font-size: 11px; line-height: 18px; border-radius: 10px; background: var(--accent); color: var(--accent-text); border: none; opacity: 0; transition: opacity .12s; }
.tasks li:hover .mini, .tasks li.viewed .mini, .tasks .mini:focus-visible { opacity: 1; }
@media (hover: none) { .tasks .mini { opacity: 1; } }
.comment { margin-top: 12px; padding-top: 10px; border-top: 1px dashed var(--border); }
.chead { margin-bottom: 4px; }
.chead small, .foldbody small { color: var(--muted); }
.taskinfo { margin-top: 12px; }
.taskinfo > .fold { margin-top: 0; }
.extrabox { border-left: 1px solid var(--border); display: flex; flex-direction: column; min-width: 0; }
@media (max-width: 700px) { .extrabox { border-left: none; border-top: 1px solid var(--border); } }
.extra { display: flex; flex-direction: column; gap: 10px; margin: 0; padding: 12px; overflow: auto; max-height: 15vh; min-width: 0; font-size: 14px; }
.extra:empty { display: none; }
.ef.empty { opacity: .5; }
.ef.empty .efh { font-weight: 500; }
.ef .efh { display: flex; align-items: center; justify-content: space-between; gap: 6px; color: var(--muted); font-weight: 600; }
.ef .efh button { border: none; background: none; padding: 0 4px; color: var(--muted); font-size: 16px; line-height: 1; }
.ef .efh button:hover { color: var(--err); }
.ef .efv { white-space: pre-wrap; overflow-wrap: anywhere; }
.addfield { padding: 8px 12px; margin-top: auto; }
.extra:not(:empty) + .addfield { border-top: 1px solid var(--border); }

dialog { border: 1px solid var(--border); border-radius: var(--radius); background: var(--panel); color: var(--text); box-shadow: 0 12px 40px rgba(0, 0, 0, .3); padding: 20px; width: min(360px, 90vw); }
dialog::backdrop { background: rgba(10, 12, 24, .45); }
dialog form { display: flex; flex-direction: column; gap: 16px; }
.dlgbtns { display: flex; justify-content: flex-end; gap: 8px; }

/* selects share the look of the other controls */
select, input.sel {
  appearance: none; -webkit-appearance: none; cursor: pointer; padding-right: 34px;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1.5l5 5 5-5' fill='none' stroke='%236b7289' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat; background-position: right 12px center;
}
:root[data-theme="dark"] select, :root[data-theme="dark"] input.sel {
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1.5l5 5 5-5' fill='none' stroke='%238b92aa' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
}
select:hover, input.sel:hover { border-color: var(--accent); }
input.sel { caret-color: transparent; cursor: pointer; }
input.sel::selection { background: transparent; }
select option { background: var(--panel); color: var(--text); }

/* custom dropdown list (same look as the rest of the UI) */
.combo { position: relative; width: 100%; }
.combo-list {
  position: absolute; z-index: 30; top: calc(100% + 4px); left: 0; min-width: 100%; width: max-content; max-width: min(380px, 90vw);
  max-height: 260px; overflow: auto; padding: 4px; background: var(--panel); border: 1px solid var(--border);
  border-radius: 10px; box-shadow: 0 8px 24px rgba(20, 24, 50, .18);
}
.combo-list .opt { display: flex; align-items: center; gap: 10px; padding: 7px 10px; border-radius: 7px; cursor: pointer; font-size: 14px; line-height: 1.3; }
.combo-list .opt.act { background: var(--accent-soft); }
.combo-list .lbl { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.combo-list .tag { flex: 0 0 auto; padding: 1px 7px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg); color: var(--muted); font-size: 11px; font-weight: 600; letter-spacing: .02em; }
.combo-list .none { padding: 8px 10px; color: var(--muted); font-size: 13px; }

/* scrollbars match the theme */
* { scrollbar-width: thin; scrollbar-color: var(--border) transparent; }
*::-webkit-scrollbar { width: 10px; height: 10px; }
*::-webkit-scrollbar-track { background: transparent; }
*::-webkit-scrollbar-thumb { background: var(--border); border-radius: 999px; border: 2px solid transparent; background-clip: content-box; }
*::-webkit-scrollbar-thumb:hover { background: var(--muted); background-clip: content-box; }
*::-webkit-scrollbar-corner { background: transparent; }

/* emoji throwing */
.player:not(.me) .pcard { cursor: pointer; }
#emojiPop { position: fixed; z-index: 30; display: flex; flex-wrap: wrap; justify-content: center; max-width: calc(100vw - 16px); gap: 2px; padding: 5px; background: var(--panel); border: 1px solid var(--border); border-radius: 18px; box-shadow: 0 8px 24px rgba(20, 24, 50, .22); animation: popin .15s ease-out; }
#emojiPop button { padding: 2px 5px; border: none; background: transparent; font-size: 22px; line-height: 1.2; border-radius: 50%; }
#emojiPop button:hover { background: var(--accent-soft); transform: scale(1.3); }
@keyframes popin { from { opacity: 0; transform: translateY(4px) scale(.95); } }
.fly { position: fixed; left: 0; top: 0; z-index: 50; font-size: 34px; line-height: 1; pointer-events: none; will-change: transform; }
.fly.small { font-size: 18px; }
.flytag { position: fixed; left: 0; top: 0; z-index: 51; pointer-events: none; padding: 2px 9px; border-radius: 999px; background: var(--accent); color: var(--accent-text); font-size: 12px; font-weight: 600; white-space: nowrap; }


/* ---------- mobile ---------- */
@media (max-width: 700px) {
  body { font-size: 14px; }
  input, select, textarea { font-size: 16px; }   /* no zoom-on-focus on iOS */
  main { padding: 12px; }
  header { padding: 8px 12px; gap: 4px 8px; }
  header button.ghost { padding: 6px 9px; font-size: 13px; }
  .panel { padding: 14px; margin-bottom: 12px; }
  #landing { padding-top: 0; }
  .hero { gap: 24px; }
  .hero-card { padding: 20px; }
  .fan { margin: 24px 0 0 10px; height: 130px; }
  .fan div { width: 72px; height: 104px; left: calc(var(--i) * 62px); font-size: 26px; }
  .narrow { margin-top: 3vh; padding: 20px; }

  .taskhead { flex-direction: column; gap: 2px; }
  .reporter { max-width: 100%; text-align: left; padding-top: 0; }
  .task { font-size: 15px; }
  .pill { width: 100%; }
  .push > span { flex-basis: 100%; }

  .table { gap: 20px 12px; padding: 24px 8px; min-height: 240px; border-radius: 16px; }
  #countdown { border-radius: 16px; }
  #countdown span { font-size: 96px; }
  .player { width: 78px; gap: 6px; }
  .pname { max-width: 78px; font-size: 12px; }
  .pcard { width: 58px; height: 84px; font-size: 24px; border-radius: 9px; }
  .pcard .face { border-radius: 9px; }
  .row.center { margin-bottom: 14px; }
  .row.center button { padding: 10px 16px; }
  .stat { min-width: 80px; padding: 8px 14px; }
  .stat b { font-size: 20px; }

  #room { padding-bottom: 130px; }
  .deckbar { padding: 10px 8px calc(10px + env(safe-area-inset-bottom)); justify-content: flex-start; }
  .deck { flex-wrap: nowrap; overflow-x: auto; justify-content: safe center; width: 100%; padding: 14px 4px 4px; gap: 8px; scrollbar-width: none; }
  .deck::-webkit-scrollbar { display: none; }
  .dcard { flex: 0 0 auto; width: 52px; height: 76px; font-size: 18px; }
  .dcard.sel { transform: translateY(-8px); }
  .hint { padding: 18px 0; }
  #toasts { bottom: 120px; left: 12px; right: 12px; }
  .toast { max-width: none; }
  .tasks { max-height: 45vh; }
  .combo-list { max-width: calc(100vw - 48px); }
}
@media (max-width: 400px) {
  .logo { font-size: 16px; }
}

/* room settings dropdown (opens on hover / focus) */
.menu { position: relative; }
.menu-pop { display: none; position: absolute; left: 0; top: 100%; padding-top: 6px; z-index: 50; }
.menu:hover .menu-pop, .menu:focus-within .menu-pop { display: block; }
.menu-body { display: flex; flex-direction: column; gap: 12px; width: 280px; padding: 14px; background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); text-align: left; }
.menu-body label small { font-weight: 400; opacity: .8; }
.pwrow { display: flex; gap: 6px; }
.pwrow input { flex: 1; min-width: 0; }
.optsbody { display: flex; flex-direction: column; gap: 10px; padding: 10px 12px 12px; }
.menu-sub { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin-bottom: 6px; }
.hostlist { list-style: none; margin: 0; padding: 0; max-height: 160px; overflow: auto; font-size: 14px; }
.hostlist li { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 3px 0; }
.hostlist li > :not(span) { flex: none; }
.hostlist li span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hostlist button { padding: 3px 8px; font-size: 12px; }
.hostlist small { color: var(--ok); margin-left: auto; }

/* loading indicators */
.spin { display: inline-block; width: 14px; height: 14px; flex: 0 0 auto; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin .7s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.busy { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px; }
.lbltxt { display: block; white-space: nowrap; }
input.sel { text-transform: uppercase; letter-spacing: .04em; font-weight: 600; }
.combo > .spin { position: absolute; right: 12px; top: 50%; margin-top: -7px; pointer-events: none; }
.combo.busy input { padding-right: 34px; }
button.loading::before { content: ""; display: inline-block; width: 12px; height: 12px; margin-right: 8px; vertical-align: -1px; border: 2px solid currentColor; border-top-color: transparent; border-radius: 50%; opacity: .8; animation: spin .7s linear infinite; }
</style>
</head>
<body>
<header>
  <div class="hgroup left">
    <div id="roomMenu" class="menu" hidden>
      <button id="roomMenuBtn" class="ghost" type="button">⚙ Room settings ▾</button>
      <div class="menu-pop">
        <div class="menu-body">
          <label id="roomProjRow" hidden>Jira project
            <input id="roomProj" placeholder="Click to pick a project…" autocomplete="off" spellcheck="false">
          </label>
          <label>Password <small>(empty = none)</small>
            <span class="pwrow"><input id="roomPw" type="password" autocomplete="new-password" placeholder="No password"><button id="roomPwSave" type="button">Set</button></span>
          </label>
          <label class="check"><input id="optTasks" type="checkbox"> Only host can start estimation</label>
          <label class="check"><input id="optReveal" type="checkbox"> Only host can reveal cards</label>
          <div>
            <div class="menu-sub">Hosts</div>
            <ul id="hostList" class="hostlist"></ul>
          </div>
        </div>
      </div>
    </div>
    <button id="copyBtn" class="ghost" hidden>🔗 Invite link</button>
  </div>
  <a class="logo" href="/">🃏 Planning Poker</a>
  <div class="hgroup right">
    <button id="nameBtn" class="ghost" title="Change name" hidden></button>
    <button id="obsBtn" class="ghost" hidden>👁 Observer</button>
    <button id="themeBtn" class="ghost" title="Toggle theme">🌓</button>
  </div>
</header>

<main>
  <!-- landing -->
  <section id="landing" hidden>
    <div class="hero">
      <div class="hero-text">
        <span class="eyebrow">Planning poker for agile teams</span>
        <h1>Estimate together,<br><span class="grad">without the friction.</span></h1>
        <p class="lead">Spin up a room, share the link and vote in real time. No sign-up, no installs — and it plugs straight into Jira.</p>
        <ul class="chips">
          <li>⚡ Instant rooms</li><li>🔗 Join by link</li><li>🧩 Jira sync</li><li>👁 Observers</li><li>🌗 Dark mode</li>
        </ul>
        <div class="fan" aria-hidden="true"><div>3</div><div>5</div><div>8</div><div>13</div></div>
      </div>
      <div class="hero-card panel">
        <h2>New planning poker room</h2>
        <label>Deck
          <input id="deckSel" class="sel" readonly>
        </label>
        <label><span class="lbltxt">Room password <small>(optional)</small></span>
          <input id="landPw" type="password" autocomplete="new-password" placeholder="Leave empty for an open room">
        </label>
        <label id="landProjRow" hidden><span class="lbltxt">Jira project <small>(optional)</small></span>
          <input id="projectSel" placeholder="Click to pick or type a project…" autocomplete="off" spellcheck="false">
        </label>
        <details class="fold roomopts">
          <summary>Room settings</summary>
          <div class="foldbody optsbody">
            <label class="check"><input id="landOwn" type="checkbox"> Only host can start estimation</label>
            <label class="check"><input id="landRev" type="checkbox"> Only host can reveal cards</label>
          </div>
        </details>
        <button id="createBtn" class="primary">Create room →</button>
      </div>
    </div>
  </section>

  <!-- join -->
  <section id="join" class="panel narrow" hidden>
    <h1>Join room</h1>
    <p id="joinMsg" class="err"></p>
    <label>Your name
      <input id="nameIn" maxlength="30" placeholder="Enter your name">
    </label>
    <button id="newRoomBtn" class="primary" hidden>Back to start page</button>
    <label id="pwRow" hidden>Room password
      <input id="pwIn" type="password">
    </label>
    <label class="check"><input id="obsIn" type="checkbox"> Join as observer (no voting)</label>
    <button id="joinBtn" class="primary">Join</button>
  </section>

  <!-- room -->
  <section id="room" hidden>
    <div class="layout">
      <div class="col">
        <div class="panel">
          <div class="taskhead">
            <div id="taskView" class="task"></div>
            <div id="reporter" class="reporter" hidden></div>
          </div>
          <div id="taskInfo" class="taskinfo" hidden>
            <details id="descBox" class="fold">
              <summary id="descSum">More</summary>
              <div class="foldcols">
                <div id="descBody" class="foldbody"></div>
                <div id="extraBox" class="extrabox">
                  <dl id="extraBody" class="extra"></dl>
                  <div id="addField" class="addfield" hidden>
                    <input id="extraIn" placeholder="＋ Add a field…" autocomplete="off" spellcheck="false">
                  </div>
                </div>
              </div>
            </details>
          </div>
          <div id="pushBox" class="row push" hidden>
            <div class="pill">
              <input id="pushVal" class="est-in" placeholder="Estimate" autocomplete="off" inputmode="decimal">
              <button id="pushBtn" class="primary">Save to Jira</button>
            </div>
            <span id="pushLbl"></span>
          </div>
        </div>

        <div class="tablewrap">
          <div id="table" class="table"></div>
          <div id="countdown" hidden></div>
        </div>
        <div id="emojiPop" hidden></div>

        <div class="row center">
          <button id="revealBtn" class="primary">Reveal cards</button>
          <button id="resetBtn">New round</button>
        </div>
        <div id="results" class="results"></div>
      </div>

      <aside id="jira" class="panel" hidden>
        <div class="jhead"><h3>Jira</h3></div>
        <div id="cfgBox" class="cfgbox" hidden>
        <button id="cfgToggle" type="button" class="cfghead" title="Show / hide settings">⚙ Settings ▾</button>
        <div id="jiraCfg" class="form">
          <label><span>JQL<span id="jqlScope"></span></span>
            <textarea id="jqlIn" rows="3" placeholder="project = ABC AND resolution = Unresolved ORDER BY Rank"></textarea>
          </label>
          <label>Estimate field
            <input id="fieldIn" placeholder="Story Points…" autocomplete="off" spellcheck="false">
          </label>
        </div>
        </div>
        <div id="loadRow" class="loadrow" hidden>
          <button id="loadBtn" class="primary">Load tasks</button>
          <button id="addBtn" type="button">＋ Add task</button>
        </div>
        <ul id="taskList" class="tasks"></ul>
      </aside>
    </div>

    <div class="deckbar">
      <div id="deck" class="deck"></div>
      <div id="obsHint" class="hint" hidden>👁 You are observing — switch off Observer to vote</div>
    </div>
  </section>
</main>
<dialog id="dlg">
  <form method="dialog" id="dlgForm">
    <label><span id="dlgTitle"></span>
      <input id="dlgIn" autocomplete="off">
    </label>
    <div class="dlgbtns">
      <button type="button" id="dlgCancel">Cancel</button>
      <button class="primary" id="dlgOk">OK</button>
    </div>
  </form>
</dialog>
<div id="toasts"></div>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const store = {
  get: k => { try { return localStorage.getItem(k) } catch { return null } },
  set: (k, v) => { try { localStorage.setItem(k, v) } catch { } },
};
const uuid = () => (crypto.randomUUID ? crypto.randomUUID() :
  'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => { const r = Math.random() * 16 | 0; return (c == 'x' ? r : r & 3 | 8).toString(16) }));

const clientId = store.get('cid') || uuid();
store.set('cid', clientId);

/* ---------- theme ---------- */
function applyTheme(t) { document.documentElement.dataset.theme = t; store.set('theme', t); }
applyTheme(store.get('theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
$('#themeBtn').onclick = () => applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');

function show(id) {
  for (const s of ['landing', 'join', 'room']) $('#' + s).hidden = s !== id;
  if (id === 'join' && !$('#joinBtn').hidden) $('#nameIn').focus();
}
function toast(text, level = 'info') {
  const el = document.createElement('div');
  el.className = 'toast ' + level; el.textContent = text;
  $('#toasts').append(el); setTimeout(() => el.remove(), 4500);
}

const m = location.pathname.match(/^\/([0-9a-f-]{36})\/?$/i);
const roomId = m && m[1].toLowerCase();
const badPath = !roomId && location.pathname.replace(/\/+$/, '') !== '';  // anything other than "/" or a room id
let config = { decks: [], jira: false };

fetch('/api/config').then(r => r.json()).then(c => { config = c; badPath ? roomNotFound() : roomId ? initRoom() : initLanding(); });

/* ---------- landing ---------- */
function initLanding() {
  show('landing');
  let deck = config.decks[0];
  $('#deckSel').value = deckLabel(deck);
  combo($('#deckSel'), () => config.decks.map(d => ({ id: d, label: deckLabel(d) })), it => { deck = it.id; $('#deckSel').value = it.label; });
  if (config.jira) {
    $('#landProjRow').hidden = false;
    const proj = $('#projectSel');
    let key = '';
    const resolve = projectCombo(proj, it => { key = it.id; });
    proj.addEventListener('input', () => { key = resolve(proj.value); });
    $('#createBtn').onclick = () => createRoom(deck, $('#landPw').value, $('#landOwn').checked, $('#landRev').checked, key);
  } else {
    $('#createBtn').onclick = () => createRoom(deck, $('#landPw').value, $('#landOwn').checked, $('#landRev').checked);
  }
}
/* project picker shared by the landing page and the room settings; returns a resolver for typed text (key / name → key, else '') */
let projLoading = false, projLoaded = false;
function projectCombo(input, onPick) {
  const items = () => { items.loading = projLoading; return projects.map(p => ({ id: p.key, label: projLabel(p) })); };
  items.empty = 'No projects found';
  combo(input, items, it => { input.value = it.label; onPick(it); });
  // projects are fetched from Jira only when a field is first opened
  const load = () => {
    if (projLoaded || projLoading) return;
    projLoading = true; input._busy(true);
    fetch('/api/projects').then(r => r.ok ? r.json() : Promise.reject()).then(list => { projects = list; projLoaded = true; })
      .catch(() => toast('Could not load Jira projects', 'error'))
      .finally(() => { projLoading = false; input._busy(false); if (document.activeElement === input) input._open(true); });
  };
  input.addEventListener('focus', load);
  input.addEventListener('click', load);
  return v => {
    v = v.trim().toLowerCase();
    const p = projects.find(p => p.key.toLowerCase() === v || p.name.toLowerCase() === v || projLabel(p).toLowerCase() === v);
    return p ? p.key : '';
  };
}
const DECK_NAMES = { fibonacci: 'FIBONACCI', tshirt: 'T-SHIRT SIZES', pow2: 'POWERS OF 2' };
const deckLabel = d => DECK_NAMES[d] || String(d).toUpperCase();

async function createRoom(deck, password, ownerOnlyTasks = false, ownerOnlyReveal = false, project = '') {
  try {
    const r = await fetch('/api/rooms', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clientId, deck, password, ownerOnlyTasks, ownerOnlyReveal, project }),
    });
    if (!r.ok) throw new Error();
    const { id } = await r.json();
    if (password) store.set('pw:' + id, password);
    location.href = '/' + id;
  } catch { toast('Could not create the room, try again', 'error'); }
}

/* ---------- room ---------- */
let ws, S = null, myVote = null, me = null, isOwner = false, joined = false, wantJoin = false;
let descSig = '', extraSig = '', deckSig = '', listSig = '';
let viewKey = null, curKey = null, viewDetails = {};  // viewKey: a task this user looks at without it being estimated
let hostSig = '', lastRoundKey = '', prevRevealed = false, flipUntil = 0, tableSig = '';

function roomNotFound() {
  show('join');
  $('#joinMsg').textContent = 'Room not found (it may have expired).';
  $('#joinBtn').hidden = true;
  document.querySelectorAll('#join label').forEach(l => l.hidden = true);
  $('#newRoomBtn').hidden = false;
  $('#newRoomBtn').onclick = () => { location.href = '/'; };
}

function initRoom() {
  $('#nameIn').value = store.get('name') || '';
  $('#obsIn').checked = store.get('obs') === '1';
  $('#pwIn').value = store.get('pw:' + roomId) || '';
  $('#joinBtn').onclick = doJoin;
  $('#nameIn').onkeydown = $('#pwIn').onkeydown = e => { if (e.key === 'Enter') doJoin(); };
  if ($('#nameIn').value) doJoin(); else show('join');
}

function doJoin() {
  const name = $('#nameIn').value.trim();
  if (!name) return $('#nameIn').focus();
  store.set('name', name); store.set('obs', $('#obsIn').checked ? '1' : '0');
  $('#nameBtn').textContent = '✎ ' + name;
  wantJoin = true; $('#joinMsg').textContent = '';
  connect();
}

function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }

function connect() {
  if (ws) { ws.onclose = null; ws.close(); }
  ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/' + roomId);
  ws.onopen = () => send({
    type: 'join', clientId, name: $('#nameIn').value.trim(),
    password: $('#pwIn').value, observer: $('#obsIn').checked,
  });
  ws.onmessage = e => onMsg(JSON.parse(e.data));
  ws.onclose = () => { if (wantJoin) setTimeout(connect, 1500); };
}

function onMsg(msg) {
  if (msg.type === 'error') {
    wantJoin = false; joined = false; show('join');
    if (msg.code === 'not_found') {
      roomNotFound();
    } else {
      $('#pwRow').hidden = false;
      $('#joinMsg').textContent = msg.code === 'bad_password' ? 'Wrong password.' : 'This room is password protected.';
      $('#pwIn').focus();
    }
  } else if (msg.type === 'joined') {
    joined = true; show('room');
    store.set('pw:' + roomId, $('#pwIn').value);
    $('#copyBtn').hidden = false; $('#obsBtn').hidden = false; $('#nameBtn').hidden = false;
    $('#jira').hidden = !config.jira;
  } else if (msg.type === 'jira_idle') setBusy(msg.op, false);
  else if (msg.type === 'jira_fields') {
    if (msg.project !== projKey) return;
    allFields = msg.allFields; numFields = msg.fields;
    // keep the chosen estimate field only if the project has it; otherwise preselect the usual suspect
    if (!numFields.some(f => f.id === estId)) {
      const sp = numFields.find(f => /story point/i.test(f.name));
      setEst(sp ? sp.id : '');
    } else setEst(estId);
  } else if (msg.type === 'details') {
    viewDetails[msg.key] = msg.details; if (S) render();
  } else if (msg.type === 'details_stale') {
    if (viewKey) { delete viewDetails[viewKey]; send({ type: 'view', key: viewKey }); }
  } else if (msg.type === 'state') {
    S = msg.room; me = msg.me; myVote = msg.myVote; render();
  } else if (msg.type === 'countdown') startCountdown(msg.n, msg.ms);
  else if (msg.type === 'throw') flyEmoji(msg);
  else if (msg.type === 'toast') toast(msg.text, msg.level);
}

/* ---------- actions ---------- */
$('#copyBtn').onclick = async () => {
  try { await navigator.clipboard.writeText(location.href); toast('Link copied', 'ok'); }
  catch { prompt('Copy link:', location.href); }
};
/* in-page replacement for prompt() (often blocked in embedded browsers) */
function ask(title, value = '', type = 'text') {
  return new Promise(res => {
    const dlg = $('#dlg'), inp = $('#dlgIn');
    $('#dlgTitle').textContent = title; inp.type = type; inp.value = value;
    let result = null;
    const done = v => { result = v; dlg.close(); };
    $('#dlgForm').onsubmit = e => { e.preventDefault(); done(inp.value); };
    $('#dlgCancel').onclick = () => done(null);
    dlg.onclose = () => res(result);
    dlg.showModal(); inp.focus(); inp.select();
  });
}
$('#nameBtn').onclick = async () => {
  const n = await ask('Your name', $('#nameIn').value);
  if (n === null || !n.trim()) return;
  $('#nameIn').value = n.trim().slice(0, 30);
  doJoin();
};
function savePassword() {
  const p = $('#roomPw').value;
  send({ type: 'set_password', password: p });
  store.set('pw:' + roomId, p); $('#pwIn').value = p;
}
$('#roomPwSave').onclick = savePassword;
$('#roomPw').onkeydown = e => { if (e.key === 'Enter') savePassword(); };
$('#hostList').onclick = e => { const b = e.target.closest('button[data-id]'); if (b) send({ type: b.dataset.act, to: b.dataset.id }); };
const sendOptions = () => send({ type: 'set_options', ownerOnlyTasks: $('#optTasks').checked, ownerOnlyReveal: $('#optReveal').checked });
$('#optTasks').onchange = $('#optReveal').onchange = sendOptions;
$('#obsBtn').onclick = () => { $('#obsIn').checked = !$('#obsIn').checked; store.set('obs', $('#obsIn').checked ? '1' : '0'); send({ type: 'observer', value: $('#obsIn').checked }); };
$('#revealBtn').onclick = () => send({ type: 'reveal' });
let counting = false;
function startCountdown(n, total = 2000) {
  const el = $('#countdown');
  counting = true; $('#revealBtn').disabled = true; $('#table').classList.add('counting');
  const step = total / n;  // the server picks the total (2-2.5 s)
  el.style.setProperty('--step', step + 'ms');
  const tick = () => {
    if (n < 1) { el.hidden = true; counting = false; $('#table').classList.remove('counting'); if (S) render(); return; }
    el.hidden = false; el.innerHTML = `<span>${n}</span>`; n--; setTimeout(tick, step);
  };
  tick();
}
$('#resetBtn').onclick = () => send({ type: 'reset' });
let allFields = [], numFields = [], estId = '', projects = [], projKey = '';
const fieldName = id => (numFields.concat(allFields).find(f => f.id === id) || {}).name || '';
function setEst(id) { estId = id || ''; $('#fieldIn').value = fieldName(estId) || estId; }
function resolveEst() {
  const v = $('#fieldIn').value.trim(), l = v.toLowerCase();
  if (estId && v === (fieldName(estId) || estId)) return estId;
  const f = numFields.find(f => f.id.toLowerCase() === l || f.name.toLowerCase() === l);
  return f ? f.id : v;
}
const setExtra = list => sendJira({ type: 'set_extra', extra: list });

/* themed dropdown for text inputs (a native <datalist> can't be styled) */
function combo(input, getItems, onPick) {
  const wrap = document.createElement('div'); wrap.className = 'combo';
  input.replaceWith(wrap); wrap.append(input);
  const list = document.createElement('div'); list.className = 'combo-list'; list.hidden = true;
  wrap.append(list);
  let items = [], active = -1;
  const close = () => { list.hidden = true; active = -1; };
  const mark = () => list.querySelectorAll('.opt').forEach((el, i) => el.classList.toggle('act', i === active));
  const pick = i => { if (items[i]) { onPick(items[i]); close(); } };
  function open(showAll) {
    const q = showAll === true ? '' : input.value.trim().toLowerCase();
    const all = getItems();
    items = all.filter(it => !q || (it.label + ' ' + it.id + ' ' + (it.tags || []).join(' ')).toLowerCase().includes(q)).slice(0, 60);
    active = items.length ? 0 : -1;
    list.innerHTML = getItems.loading ? '<div class="none">Loading…</div>' : items.length ? items.map((it, i) =>
      `<div class="opt" data-i="${i}"><span class="lbl">${esc(it.label)}</span>${(it.tags || []).map(t => `<span class="tag">${esc(t)}</span>`).join('')}</div>`).join('')
      : `<div class="none">${esc(all.length ? 'Nothing found' : (getItems.empty || 'Nothing to choose from'))}</div>`;
    list.hidden = false; mark();
  }
  input._open = open;
  // small spinner at the right edge of the field while its data is being fetched
  const spin = document.createElement('span'); spin.className = 'spin'; spin.hidden = true; wrap.append(spin);
  input._busy = on => { spin.hidden = !on; wrap.classList.toggle('busy', on); };
  list.onmousedown = e => { e.preventDefault(); const o = e.target.closest('.opt'); if (o) pick(+o.dataset.i); };
  input.addEventListener('focus', () => { open(true); input.select(); });
  input.addEventListener('input', () => open());
  input.addEventListener('click', () => { if (list.hidden) open(true); });
  input.addEventListener('blur', close);
  input.addEventListener('keydown', e => {
    if (e.key === 'Escape') return close();
    if (list.hidden && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) return open(true);
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault(); if (!items.length) return;
      active = (active + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length; mark();
      list.querySelector('.act')?.scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'Enter' && !list.hidden && active >= 0) { e.preventDefault(); pick(active); }
  });
}

// every field of the selected project that isn't shown yet
const availFields = () => {
  const cur = (S && S.extra) || [];
  return allFields.filter(f => !cur.includes(f.id)).map(f => ({ id: f.id, label: f.name }));
};
availFields.empty = 'Pick a project first';
combo($('#extraIn'), availFields, it => { $('#extraIn').value = ''; setExtra([...(S.extra || []), it.id]); });
combo($('#fieldIn'), () => numFields.map(f => ({ id: f.id, label: f.name })), it => setEst(it.id));
$('#fieldIn').addEventListener('input', () => { estId = resolveEst(); });

const defaultJql = key => config.jiraFilter || `project = "${key}" AND resolution = Unresolved ORDER BY Rank`;
/* settings can be folded away to give the issue list more room */
const setCfgFolded = f => { $('#jira').classList.toggle('folded', f); $('#cfgToggle').textContent = f ? '⚙ Settings ▸' : '⚙ Settings ▾'; };
$('#cfgToggle').onclick = () => setCfgFolded(!$('#jira').classList.contains('folded'));
setCfgFolded(false);

const projLabel = p => `${p.name} (${p.key})`;
/* the host can change the project in Room settings; everyone's session then loads the new project's fields */
function pickProject(key) {
  if (projKey) {  // switching: the old query and estimate field don't apply to the new project
    $('#jqlIn').value = defaultJql(key); estId = ''; $('#fieldIn').value = '';
  }
  projKey = key; allFields = []; numFields = [];
  sendJira({ type: 'jira_project', key });
}
const roomProj = $('#roomProj');
const resolveRoomProj = projectCombo(roomProj, it => { if (it.id !== (S && S.project)) send({ type: 'set_project', key: it.id }); });
// typing the exact key / name also works; anything else is reverted on blur
roomProj.addEventListener('input', () => { const k = resolveRoomProj(roomProj.value); if (k && k !== (S && S.project)) send({ type: 'set_project', key: k }); });
roomProj.addEventListener('blur', () => setRoomProjValue());
function setRoomProjValue() {
  const p = projects.find(p => p.key === (S && S.project));
  roomProj.value = S && S.project ? (p ? projLabel(p) : S.project) : '';
}
/* spinner for requests the server answers from Jira; the server confirms with jira_idle */
const busyOps = new Set();
function setBusy(op, on) {
  on ? busyOps.add(op) : busyOps.delete(op);
  const b = o => busyOps.has(o);
  for (const [btn, o] of [['#loadBtn', 'jira_load'], ['#addBtn', 'jira_add'], ['#pushBtn', 'jira_push']]) {
    $(btn).disabled = b(o); $(btn).classList.toggle('loading', b(o));
  }
  $('#fieldIn')._busy(b('jira_project'));
  $('#extraIn')._busy(b('jira_project') || b('set_extra'));
}
function sendJira(o) { if (ws && ws.readyState === 1) setBusy(o.type, true); send(o); }
$('#loadBtn').onclick = () => {
  const jql = $('#jqlIn').value.trim(), field = resolveEst();
  store.set('jql', jql); store.set('jfield', field);
  sendJira({ type: 'jira_load', jql, field });
};
$('#addBtn').onclick = async () => {
  const link = await ask('Link to a Jira issue (or its key)');
  if (link && link.trim()) sendJira({ type: 'jira_add', link: link.trim() });
};
$('#jqlIn').onkeydown = e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) $('#loadBtn').click(); };
$('#pushVal').addEventListener('input', e => { e.target.value = e.target.value.replace(/[^0-9.,]/g, ''); });
$('#pushBtn').onclick = () => sendJira({ type: 'jira_push', value: $('#pushVal').value });

/* ---------- throwing emoji at colleagues ---------- */
const EMOJI = ['🍅', '👋', '⏰', '🎈', '🎉', '🚀', '🐢', '💤', '❤️', '👀'];
const pop = $('#emojiPop');
pop.innerHTML = EMOJI.map(e => `<button type="button">${e}</button>`).join('');
let popTarget = null, popTimer = null;
const hidePop = () => { pop.hidden = true; popTarget = null; };
const schedHide = () => { clearTimeout(popTimer); popTimer = setTimeout(hidePop, 250); };
function showPop(pl) {
  clearTimeout(popTimer);
  if (!pl || pl.dataset.id === me) return hidePop();
  popTarget = pl.dataset.id;
  const r = pl.querySelector('.pcard').getBoundingClientRect();
  pop.hidden = false;
  const w = pop.offsetWidth, h = pop.offsetHeight;
  const below = r.top < h + 70;
  pop.style.left = Math.max(8, Math.min(innerWidth - w - 8, r.left + r.width / 2 - w / 2)) + 'px';
  pop.style.top = (below ? r.bottom + 6 : r.top - h - 6) + 'px';
}
$('#table').addEventListener('mouseover', e => { const pl = e.target.closest('.player'); if (pl && pl.dataset.id !== popTarget) showPop(pl); else if (pl) clearTimeout(popTimer); });
$('#table').addEventListener('click', e => { const pl = e.target.closest('.player'); if (pl) showPop(pl); });
for (const el of [$('#table'), pop]) el.addEventListener('mouseout', e => { if (!(e.relatedTarget && e.relatedTarget.closest && e.relatedTarget.closest('.player, #emojiPop'))) schedHide(); });
pop.addEventListener('mouseover', () => clearTimeout(popTimer));
pop.addEventListener('click', e => {
  const b = e.target.closest('button');
  if (b && popTarget) send({ type: 'throw', to: popTarget, emoji: b.textContent });
});
addEventListener('scroll', hidePop, true);

function flyEmoji(msg) {
  const card = document.querySelector(`.player[data-id="${CSS.escape(msg.to)}"] .pcard`);
  if (!card) return;
  const r = card.getBoundingClientRect(), tx = r.left + r.width / 2, ty = r.top + r.height / 2;
  const side = Math.floor(Math.random() * 4);   // 0 left, 1 right, 2 top, 3 bottom
  const sx = side === 0 ? -50 : side === 1 ? innerWidth + 50 : Math.random() * innerWidth;
  const sy = side === 2 ? -50 : side === 3 ? innerHeight + 50 : Math.random() * innerHeight;
  const el = document.createElement('div'); el.className = 'fly'; el.textContent = msg.emoji; document.body.append(el);
  const spin = (Math.random() < .5 ? -1 : 1) * (360 + Math.random() * 360);
  const at = (x, y, rot, sc) => `translate(${x}px, ${y}px) translate(-50%, -50%) rotate(${rot}deg) scale(${sc})`;
  el.animate([{ transform: at(sx, sy, 0, .6) }, { transform: at(tx, ty, spin, 1.3) }],
    { duration: 650 + Math.random() * 250, easing: 'cubic-bezier(.3, .1, .6, 1)', fill: 'forwards' }).onfinish = () => { el.remove(); impact(tx, ty, msg); };
}
function impact(x, y, msg) {
  for (let i = 0; i < 7; i++) {
    const p = document.createElement('div'); p.className = 'fly small'; p.textContent = msg.emoji; document.body.append(p);
    const a = Math.random() * 6.28, d = 40 + Math.random() * 50;
    p.animate([{ transform: `translate(${x}px, ${y}px) translate(-50%, -50%) scale(.8)`, opacity: 1 },
      { transform: `translate(${x + Math.cos(a) * d}px, ${y + Math.sin(a) * d}px) translate(-50%, -50%) scale(.3)`, opacity: 0 }],
      { duration: 600, easing: 'ease-out' }).onfinish = () => p.remove();
  }
  const from = S && S.users.find(u => u.id === msg.from);
  if (from) {
    const c = document.createElement('div'); c.className = 'flytag'; c.textContent = from.name; document.body.append(c);
    c.animate([{ transform: `translate(${x}px, ${y - 30}px) translate(-50%, -50%)`, opacity: 1 }, { transform: `translate(${x}px, ${y - 80}px) translate(-50%, -50%)`, opacity: 0 }],
      { duration: 1100, easing: 'ease-out' }).onfinish = () => c.remove();
  }
  const card = document.querySelector(`.player[data-id="${CSS.escape(msg.to)}"] .pcard`);
  if (card) card.animate([{ translate: '0 0', rotate: '0deg' }, { translate: '-5px 0', rotate: '-5deg' }, { translate: '5px 0', rotate: '5deg' }, { translate: '-3px 0', rotate: '-2deg' }, { translate: '0 0', rotate: '0deg' }], { duration: 380 });
}

/* ---------- stats ---------- */
const num = v => { const n = parseFloat(String(v).replace('½', '0.5')); return isNaN(n) ? null : n; };

function stats() {
  const votes = S.users.filter(u => !u.observer && u.vote != null).map(u => u.vote);
  const nums = votes.map(num).filter(n => n !== null);
  const counts = {}; votes.forEach(v => counts[v] = (counts[v] || 0) + 1);
  const mode = Object.keys(counts).sort((a, b) => counts[b] - counts[a])[0];
  const out = { votes, mode, consensus: votes.length > 1 && new Set(votes).size === 1, avg: null, median: null, suggest: mode };
  if (nums.length) {
    out.avg = nums.reduce((a, b) => a + b, 0) / nums.length;
    const s = [...nums].sort((a, b) => a - b), h = s.length >> 1;
    out.median = s.length % 2 ? s[h] : (s[h - 1] + s[h]) / 2;
    const cards = S.deck.filter(c => num(c) !== null);
    if (cards.length) out.suggest = cards.reduce((best, c) => Math.abs(num(c) - out.avg) <= Math.abs(num(best) - out.avg) ? c : best);
  }
  return out;
}

/* ---------- render ---------- */
function render() {
  const mine = S.users.find(u => u.id === me);
  isOwner = !!(mine && mine.host);
  $('#roomMenu').hidden = !isOwner;
  $('#cfgBox').hidden = !(isOwner && config.jira);
  $('#loadRow').hidden = !(isOwner && config.jira);
  const hl = S.users.map(u => `<li><span>${esc(u.name)}${u.id === me ? ' (you)' : ''}</span>${u.host ? `<small>host</small>${u.id === S.ownerId ? '' : `<button type="button" data-act="remove_host" data-id="${esc(u.id)}">Remove host</button>`}` : `<button type="button" data-act="make_host" data-id="${esc(u.id)}">Make host</button>`}</li>`).join('');
  if (hl !== hostSig) { hostSig = hl; $('#hostList').innerHTML = hl; }
  // task: the one being estimated (t) and the one this user is looking at (vt) are separate things
  const t = S.task;
  if ((t && t.key) !== curKey) { curKey = t && t.key; viewKey = null; }  // the host moved on: everyone follows
  let vt = t;
  if (viewKey && viewKey !== curKey) {
    vt = S.tasks.find(x => x.key === viewKey);
    if (!vt) { viewKey = null; vt = t; }
  } else viewKey = null;
  const viewing = vt !== t;
  const locked = !!S.ownerOnlyTasks && !isOwner;
  $('#taskView').innerHTML = !vt ? '<small>No task selected — pick one from the Jira list</small>'
    : vt.key ? `${vt.notFound ? `<b>${esc(vt.key)}</b>` : `<a href="${esc(vt.url)}" target="_blank" rel="noopener">${esc(vt.key)}</a>`} ${esc(vt.summary)} <small>${esc(vt.status)}</small>${viewing ? '' : ' <span class="badge">In estimation</span>'}`
      : esc(vt.title || '');
  $('#reporter').hidden = !(vt && vt.reporter);
  $('#reporter').innerHTML = vt && vt.reporter ? `Reporter: <b>${esc(vt.reporter)}</b>` : '';

  // description / comments / extra fields (formatting comes from Jira, sanitized by the server)
  const d = viewing ? viewDetails[vt.key] : t && t.details;
  $('#taskInfo').hidden = !(vt && vt.key);
  const canAdd = !!(isOwner && config.jira && vt && vt.key);
  $('#addField').hidden = !canAdd;
  $('#extraBox').hidden = !canAdd && !(d && d.extra && d.extra.length);
  if (vt && vt.key) {
    const nc = d && d.comments ? d.comments.length : 0;
    $('#descSum').textContent = 'More' + (d && !d.error ? ` (${nc} comment${nc === 1 ? '' : 's'})` : '');
    const nfLink = vt.notFound && vt.url ? `<p><a href="${esc(vt.url)}" target="_blank" rel="noopener">${esc(vt.url)}</a></p>` : '';
    const descHtml = nfLink + (!d ? '<span class="spin"></span>' : d.error ? `<p class="err">${esc(d.error)}</p>` :
      `<div class="desc">${d.description || '<small>No description</small>'}</div>` +
      d.comments.map(c => `<div class="comment"><div class="chead"><b>${esc(c.author)}</b> <small>${esc(new Date(c.created).toLocaleString())}</small></div><div class="desc">${c.body}</div></div>`).join(''));
    // rewrite only on real changes, otherwise every vote in the room would scroll the description back to the top
    if (descHtml !== descSig) { descSig = descHtml; $('#descBody').innerHTML = descHtml; }
  }
  const xs = d && d.extra ? [...d.extra.filter(x => x.value), ...d.extra.filter(x => !x.value)] : [];
  const extraHtml = xs.map(x => `<div class="ef${x.value ? '' : ' empty'}"><div class="efh"><span>${esc(x.name)}</span>${isOwner ? `<button type="button" data-id="${esc(x.id)}" title="Hide this field">×</button>` : ''}</div>${x.value ? `<div class="efv">${esc(x.value)}</div>` : ''}</div>`).join('');
  if (extraHtml !== extraSig) {  // no details yet (another task being loaded): don't keep the previous task's fields
    extraSig = extraHtml; $('#extraBody').innerHTML = extraHtml;
    $('#extraBody').querySelectorAll('button').forEach(b => b.onclick = () => setExtra((S.extra || []).filter(x => x !== b.dataset.id)));
  }

  // table
  if (S.revealed && !prevRevealed) flipUntil = Date.now() + 1500;
  prevRevealed = S.revealed;
  const justRevealed = Date.now() < flipUntil;
  let ci = 0;
  const tableHtml = S.users.map(u => {
    let card;
    if (u.observer) card = '<div class="pcard obs">👁</div>';
    else if (S.revealed && u.vote != null) card = `<div class="pcard shown${justRevealed ? ' flip' : ''}" style="animation-delay:${(ci++ * 0.12).toFixed(2)}s"><span class="face front">${esc(u.vote)}</span><span class="face back"></span></div>`;
    else if (u.voted) card = '<div class="pcard voted"></div>';
    else card = '<div class="pcard"></div>';
    return `<div class="player ${u.online ? '' : 'off'}" data-id="${esc(u.id)}">${card}<div class="pname" title="${esc(u.name)}">${esc(u.name)}${u.id === me ? ' (you)' : ''}</div></div>`;
  }).join('');
  // rebuild only on real changes, so a running flip animation is not restarted by unrelated renders
  const sig = tableHtml.replace(/ flip/g, '');
  if (sig !== tableSig) { tableSig = sig; $('#table').innerHTML = tableHtml; }

  const voters = S.users.filter(u => !u.observer);
  $('#revealBtn').disabled = S.revealed || counting || (S.ownerOnlyReveal && !isOwner);
  $('#revealBtn').title = S.ownerOnlyReveal && !isOwner ? 'Only a host can reveal cards' : '';
  $('#optTasks').checked = !!S.ownerOnlyTasks; $('#optReveal').checked = !!S.ownerOnlyReveal;
  $('#revealBtn').textContent = `Reveal cards (${voters.filter(u => u.voted).length}/${voters.length})`;
  $('#obsBtn').classList.toggle('on', $('#obsIn').checked);

  // deck
  const observer = $('#obsIn').checked;
  $('#deck').hidden = observer;
  $('#obsHint').hidden = !observer;
  const deckHtml = S.deck.map(c => `<button class="dcard ${c === myVote ? 'sel' : ''}" data-v="${esc(c)}">${esc(c)}</button>`).join('');
  if (deckHtml !== deckSig) { deckSig = deckHtml; $('#deck').innerHTML = deckHtml; }  // keeps hover animations from restarting on others' votes

  // results
  const st = S.revealed ? stats() : null;
  $('#results').innerHTML = !st || !st.votes.length ? '' :
    (st.avg !== null ? `<div class="stat"><b>${+st.avg.toFixed(2)}</b><span>Average</span></div><div class="stat"><b>${+st.median.toFixed(2)}</b><span>Median</span></div>` : '') +
    `<div class="stat"><b>${esc(st.mode)}</b><span>Most common</span></div>` +
    (st.consensus ? '<div class="stat good"><b>✔</b><span>Consensus</span></div>' : '');

  // push box
  const roundKey = (t && (t.key || t.title) || '') + '|' + S.revealed;
  const canPush = isOwner && config.jira && t && t.key && !viewing;
  $('#pushBox').hidden = !canPush;
  $('#pushLbl').textContent = S.field ? '→ ' + (fieldName(S.field) || S.field) : 'set the estimate field first';
  if (roundKey !== lastRoundKey) {
    lastRoundKey = roundKey;
    $('#pushVal').value = st && st.votes.length ? st.suggest : (t && t.estimate != null ? t.estimate : '');
  }

  // jira list
  $('#roomProjRow').hidden = !config.jira;
  if (document.activeElement !== roomProj) setRoomProjValue();
  if (isOwner && config.jira && S.project && S.project !== projKey) pickProject(S.project);
  $('#jqlScope').textContent = S.project ? ` (AND project = "${S.project}")` : '';
  if (!$('#jqlIn').value && document.activeElement !== $('#jqlIn')) $('#jqlIn').value = S.jql || (S.project ? defaultJql(S.project) : config.jiraFilter || store.get('jql') || '');
  if (!estId && document.activeElement !== $('#fieldIn')) setEst(S.field || store.get('jfield') || '');
  const listHtml = (!S.tasks.length && !isOwner ? '<li class="empty">No host has loaded any issues yet</li>' : '') + S.tasks.map(x => `
    <li data-k="${esc(x.key)}" class="${t && t.key === x.key ? 'cur' : ''}${vt && vt.key === x.key ? ' viewed' : ''}${x.manual ? ' has-manual' : ''}">
      ${t && t.key === x.key ? '<div class="badge">In estimation</div>' : ''}
      ${x.manual ? '<span class="manual" title="Added manually">✋</span>' : ''}${x.estimate != null ? `<span class="est">${esc(x.estimate)}</span>` : ''}${!locked && !(t && t.key === x.key) ? '<button type="button" class="mini" data-start title="Start estimating this task">Estimate</button>' : ''}
      <span class="k">${esc(x.key)}</span> <span class="st">${esc(x.status)}</span><br>${esc(x.summary)}
    </li>`).join('');
  if (listHtml !== listSig) { listSig = listHtml; $('#taskList').innerHTML = listHtml; }
}
$('#deck').onclick = e => {
  const b = e.target.closest('button[data-v]');
  if (b) send({ type: 'vote', value: b.dataset.v === myVote ? null : b.dataset.v });
};
$('#taskList').onclick = e => {
  const li = e.target.closest('li[data-k]');
  if (!li) return;
  if (e.target.closest('[data-start]')) return send({ type: 'task', key: li.dataset.k });
  viewKey = li.dataset.k === curKey ? null : li.dataset.k;  // clicking only looks at a task; estimating is switched separately
  if (viewKey) { delete viewDetails[viewKey]; send({ type: 'view', key: viewKey }); }
  render();
};
</script>
</body>
</html>
'''


if __name__ == "__main__":
    load_dotenv()
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")))
