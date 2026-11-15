import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

import server

TOKEN = "t" * 40
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def member(name, chamber="Senate", start=2019, **kw):
    return {"name": name, "terms": {"item": [{"chamber": chamber, "startYear": start}]}, **kw}


def test_match_members_best_score_wins():
    roster = [member("Johnson, Mike", "House of Representatives"), member("Johnson, Ron"), member("Sanders, Bernard")]
    assert [m["name"] for m in server.match_members(roster, "mike johnson")] == ["Johnson, Mike"]
    assert [m["name"] for m in server.match_members(roster, "Johnson")] == ["Johnson, Mike", "Johnson, Ron"]
    assert [m["name"] for m in server.match_members(roster, "bernie sanders")] == ["Sanders, Bernard"]
    assert server.match_members(roster, "nobody") == []
    assert [m["name"] for m in server.match_members([member("Velázquez, Nydia M.")], "velazquez")] == [
        "Velázquez, Nydia M."
    ]


def test_latest_chamber_uses_most_recent_term():
    m = {
        "terms": {
            "item": [
                {"chamber": "Senate", "startYear": 2021},
                {"chamber": "House of Representatives", "startYear": 2013},
            ]
        }
    }
    assert server.latest_chamber(m) == "Senate"
    assert server.latest_chamber({}) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/bill/119/hr/1", "/bill/119/hr/1"),
        ("bill/119/hr/1/", "/bill/119/hr/1"),
        ("/v3/member/S000033", "/member/S000033"),
        ("https://api.congress.gov/v3/bill/119/hr/1/actions?format=json", "/bill/119/hr/1/actions"),
        ("/house-vote/119/1/240/members", "/house-vote/119/1/240/members"),
    ],
)
def test_normalize_path_ok(raw, expected):
    assert server.normalize_path(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "/", "/../etc", "/bill?x=1", "//evil.com/x", "/bill/../x", "https://evil.com/v3/bill"]
)
def test_normalize_path_rejects(raw):
    with pytest.raises(ToolError):
        server.normalize_path(raw)


def test_html_to_text():
    assert server.html_to_text("<html><body><pre>\nA &amp; B &lt;x&gt;\n</pre></body></html>") == "A & B <x>"


@pytest.mark.parametrize(
    "url",
    [
        "http://www.congress.gov/a.htm",
        "https://evil.com/a.htm",
        "https://www.congress.gov/a.pdf",
        "https://www.congress.gov.evil.com/a.htm",
    ],
)
def test_read_bill_text_rejects_foreign_urls(url):
    with pytest.raises(ToolError):
        asyncio.run(server.read_bill_text(url))


@pytest.mark.parametrize("kwargs", [{"district": 3}, {"include_former": True}, {"state": "Texas"}])
def test_find_members_arg_validation(kwargs):
    with pytest.raises(ToolError):
        asyncio.run(server.find_members(**kwargs))


def test_http_auth_and_wiring():
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
    }
    with TestClient(server.build_app(TOKEN), base_url="https://congress.barrywalker.io") as c:
        assert c.get("/health").text == "ok"
        assert c.post("/mcp", json=init, headers=MCP_HEADERS).status_code == 401
        bad = {**MCP_HEADERS, "Authorization": "Bearer wrong"}
        assert c.post("/mcp", json=init, headers=bad).status_code == 401

        ok = {**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}
        r = c.post("/mcp", json=init, headers=ok)
        assert r.status_code == 200, r.text
        assert r.json()["result"]["serverInfo"]["name"] == "congress"

        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=ok)
        assert r.status_code == 200, r.text
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert names == {"find_members", "congress_api", "read_bill_text"}
        assert json.dumps(r.json()).count("readOnlyHint") == 3
