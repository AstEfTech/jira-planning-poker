"""A tiny fake Jira Cloud (just the endpoints the app calls) so the demo GIF needs no real Jira.

    python media/mock_jira.py        # listens on 127.0.0.1:8766
"""
import uvicorn
from fastapi import FastAPI, Request, Response

app = FastAPI()
POINTS = "customfield_10016"
PROJECTS = [("PAY", "Payments"), ("WEB", "Web Portal"), ("MOB", "Mobile App")]
FIELDS = [
    {"fieldId": POINTS, "name": "Story Points", "schema": {"type": "number"}},
    {"fieldId": "customfield_10020", "name": "Complexity", "schema": {"type": "number"}},
    {"fieldId": "assignee", "name": "Assignee", "schema": {"type": "user"}},
    {"fieldId": "labels", "name": "Labels", "schema": {"type": "array"}},
]
# key: (summary, status, reporter, assignee, rendered description)
ISSUES = {
    "PAY-101": ("Add Apple Pay to the checkout", "To Do", "Maria Ivanova", "Dmitry Volkov",
                "<p>Customers on iOS should be able to pay with <b>Apple Pay</b>.</p>"
                "<ul><li>Show the button only on supported devices</li><li>Reuse the existing payment intent flow</li>"
                "<li>Cover the happy path and a declined card with tests</li></ul>"),
    "PAY-102": ("Retry failed webhooks with backoff", "To Do", "Alex Chen", "Olga Petrova", "<p>Retry up to 5 times.</p>"),
    "PAY-103": ("Export refunds report to CSV", "To Do", "Maria Ivanova", "", "<p>Finance needs a monthly export.</p>"),
    "PAY-104": ("Fix rounding in tax calculation", "To Do", "Sam Taylor", "Dmitry Volkov", "<p>Off by one cent for some EU rates.</p>"),
}
estimates: dict[str, float] = {}


@app.get("/rest/api/3/project/search")
def projects():
    return {"values": [{"key": k, "name": n} for k, n in PROJECTS], "isLast": True}


@app.get("/rest/api/3/issue/createmeta/{project}/issuetypes")
def issue_types(project: str):
    return {"issueTypes": [{"id": "1"}]}


@app.get("/rest/api/3/issue/createmeta/{project}/issuetypes/{type_id}")
def type_fields(project: str, type_id: str):
    return {"fields": FIELDS}


@app.post("/rest/api/3/search/jql")
def search():
    return {"issues": [{"key": k, "fields": {
        "summary": v[0], "status": {"name": v[1]}, "reporter": {"displayName": v[2]}, POINTS: estimates.get(k)}}
        for k, v in ISSUES.items()]}


@app.get("/rest/api/3/issue/{key}")
def issue(key: str):
    summary, status, reporter, assignee, desc = ISSUES[key]
    return {
        "key": key,
        "names": {"assignee": "Assignee"},
        "fields": {"summary": summary, "status": {"name": status}, "reporter": {"displayName": reporter},
                   POINTS: estimates.get(key),
                   "assignee": {"displayName": assignee} if assignee else None,
                   "description": None,
                   "comment": {"comments": [{"id": "1", "author": {"displayName": "Dmitry Volkov"},
                                             "created": "2026-10-28T10:00:00.000+0000", "body": None}]}},
        "renderedFields": {"description": desc,
                           "comment": {"comments": [{"id": "1", "body": "<p>Design is attached, API contract unchanged.</p>"}]}},
    }


@app.put("/rest/api/3/issue/{key}")
async def update(key: str, request: Request):
    estimates[key] = (await request.json())["fields"][POINTS]
    return Response(status_code=204)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8766, log_level="warning")
