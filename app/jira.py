import asyncio
import os

import httpx


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


def default_filter() -> str:
    """JIRA_DEFAULT_FILTER as JQL: a bare number is a saved filter id, anything else is taken as JQL."""
    v = os.environ.get("JIRA_DEFAULT_FILTER", "").strip()
    return f"filter = {v}" if v.isdigit() else v


def enabled() -> bool:
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


async def projects() -> list[dict]:
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


async def fields(project: str = "") -> tuple[list[dict], list[dict]]:
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


async def search(jql: str, field: str) -> list[dict]:
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


async def issue(key: str, field: str) -> dict:
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


async def issue_details(key: str, extra: list[str]) -> dict:
    flds = ",".join(["description", "comment", *extra])
    r = await _call("GET", f"/rest/api/{_api()}/issue/{key}", params={"fields": flds, "expand": "names"})
    data = r.json()
    f, names = data.get("fields", {}), data.get("names", {})
    comments = (f.get("comment") or {}).get("comments", [])
    return {
        "description": value_text(f.get("description"))[:20000],
        "comments": [
            {"author": (c.get("author") or {}).get("displayName", "?"), "created": c.get("created", ""),
             "body": value_text(c.get("body"))[:3000]}
            for c in comments[-50:]
        ],
        "extra": [{"id": x, "name": names.get(x, x), "value": value_text(f.get(x))[:3000]} for x in extra],
    }


async def set_estimate(key: str, value, field: str) -> object:
    if not field:
        raise JiraError("Estimate field is not set — choose it in the Jira panel and press Load")
    num = to_number(value)
    val = num if num is not None else str(value)
    await _call("PUT", f"/rest/api/{_api()}/issue/{key}", json={"fields": {field: val}})
    return val
