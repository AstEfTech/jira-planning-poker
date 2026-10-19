import asyncio
import hashlib
import hmac
import os
import time
import uuid
from dataclasses import dataclass, field

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


def create_room(owner_cid: str, deck: str, password: str, owner_tasks: bool = False, owner_reveal: bool = False) -> Room:
    room = Room(id=str(uuid.uuid4()), owner_cid=owner_cid, deck=deck if deck in DECKS else "fibonacci", owner_tasks=owner_tasks, owner_reveal=owner_reveal)
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
