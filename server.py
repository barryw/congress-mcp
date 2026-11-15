"""Congress.gov MCP server: who's sponsoring what, how they vote, what the bills say."""

from __future__ import annotations

import hmac
import html
import json
import os
import re
import unicodedata
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

API_BASE = "https://api.congress.gov/v3"
PAGE_LIMIT = 250  # API maximum per page
MAX_PAGES = 12  # ~3k records; the full current roster is ~540
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

mcp = MCPServer(
    name="congress",
    instructions=(
        "Congress.gov data. Start with find_members to get a bioguideId, then use congress_api "
        "for sponsored/cosponsored bills, bill actions, cosponsors, committees and House votes. "
        "Use read_bill_text with a text URL from /bill/{congress}/{type}/{number}/text to read a bill. "
        "The current Congress is 119 (2025-2027). Bill types: hr, s, hjres, sjres, hconres, sconres, hres, sres."
    ),
)

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(headers={"User-Agent": "congress-mcp"}, timeout=30)
    return _client


def _api_error(resp: httpx.Response) -> ToolError:
    try:
        err = resp.json().get("error", resp.text)
        msg = err.get("message", err) if isinstance(err, dict) else err
    except ValueError:
        msg = resp.text[:300]
    if resp.status_code == 429:
        msg = f"rate limited (5,000 requests/hour). {msg}"
    return ToolError(f"Congress.gov API {resp.status_code}: {msg}")


async def api_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    q = {k: str(v).lower() if isinstance(v, bool) else v for k, v in (params or {}).items()}
    q["format"] = "json"
    resp = await client().get(API_BASE + path, params=q, headers={"X-Api-Key": os.environ["CONGRESS_API_KEY"]})
    if resp.status_code != 200:
        raise _api_error(resp)
    data = resp.json()
    data.pop("request", None)  # echo of our own request; pure token waste
    return data


def compact(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


# --- path / input validation -------------------------------------------------

_PATH_RE = re.compile(r"^(/[A-Za-z0-9_-]+)+$")
_RESERVED_PARAMS = {"api_key", "format"}


def normalize_path(path: str) -> str:
    path = path.strip()
    if path.startswith("https://api.congress.gov"):
        path = urlsplit(path).path
    path = "/" + path.strip("/")
    path = re.sub(r"^/v3(?=/|$)", "", path) or "/"
    if not _PATH_RE.match(path):
        raise ToolError(f"invalid path {path!r}: use segments like /bill/119/hr/1/actions")
    return path


# --- member name matching ----------------------------------------------------


def _tokens(s: str) -> list[str]:
    ascii_s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.findall(r"[a-z0-9]+", ascii_s.casefold())


def match_members(members: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """Keep members whose name best matches the query (token prefix match, best score wins).

    "mike johnson" -> only "Johnson, Mike"; "bernie sanders" -> "Sanders, Bernard" (surname hit).
    """
    query = _tokens(name)
    if not query:
        return members
    scored = []
    for m in members:
        name_toks = _tokens(m.get("name", ""))
        score = sum(any(t.startswith(q) for t in name_toks) for q in query)
        if score:
            scored.append((score, m))
    best = max((s for s, _ in scored), default=0)
    return [m for s, m in scored if s == best]


def latest_chamber(member: dict[str, Any]) -> str | None:
    terms = (member.get("terms") or {}).get("item") or []
    if not terms:
        return None
    return max(terms, key=lambda t: t.get("startYear") or 0).get("chamber")


def summarize_member(m: dict[str, Any]) -> dict[str, Any]:
    out = {
        "bioguideId": m.get("bioguideId"),
        "name": m.get("name"),
        "party": m.get("partyName"),
        "state": m.get("state"),
        "chamber": latest_chamber(m),
    }
    if m.get("district") is not None:
        out["district"] = m["district"]
    return out


# --- tools -------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY, structured_output=False)
async def find_members(
    name: str | None = None,
    state: str | None = None,
    district: int | None = None,
    chamber: Literal["house", "senate"] | None = None,
    include_former: bool = False,
) -> str:
    """Find members of Congress and their bioguideId (the key every member endpoint needs).

    Congress.gov has no name search, so this pulls the roster and matches names locally.
    name: any part of the name, e.g. "warren" or "mike johnson" (official names: "Bernard" not "Bernie").
    state: two-letter code, e.g. "TX". district: House district number (requires state).
    include_former: also search past members (requires state).
    """
    if state is not None:
        state = state.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", state):
            raise ToolError("state must be a two-letter code like 'TX'")
    if district is not None and state is None:
        raise ToolError("district requires state")
    if include_former and state is None:
        raise ToolError("include_former requires state (the all-time roster is too large)")

    path = "/member" + (f"/{state}" if state else "") + (f"/{district}" if district is not None else "")
    params: dict[str, Any] = {"limit": PAGE_LIMIT}
    if not include_former:
        params["currentMember"] = True

    members: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        data = await api_get(path, {**params, "offset": page * PAGE_LIMIT})
        batch = data.get("members", [])
        members.extend(batch)
        if not (data.get("pagination") or {}).get("next") or not batch:
            break

    if name:
        members = match_members(members, name)
    if chamber:
        want = "Senate" if chamber == "senate" else "House of Representatives"
        members = [m for m in members if latest_chamber(m) == want]
    return compact({"count": len(members), "members": [summarize_member(m) for m in members]})


@mcp.tool(annotations=READ_ONLY, structured_output=False)
async def congress_api(path: str, params: dict[str, str | int | bool] | None = None) -> str:
    """GET any Congress.gov API v3 endpoint. Returns raw JSON (minus the request echo).

    Common paths ({congress} e.g. 119, {type} e.g. hr/s/hjres/hres):
      /member/{bioguideId}                         bio, terms, party history, leadership
      /member/{bioguideId}/sponsored-legislation   bills they wrote
      /member/{bioguideId}/cosponsored-legislation bills they signed onto
      /bill?fromDateTime=...&sort=updateDate+desc  recently updated bills (all of Congress)
      /bill/{congress}/{type}                      bills of a type
      /bill/{congress}/{type}/{number}             bill detail (sponsor, status, latest action)
        .../actions  .../cosponsors  .../committees  .../subjects  .../summaries
        .../text  .../amendments  .../relatedbills  .../titles
      /house-vote/{congress}/{session}             House roll-call votes (session 1 or 2)
      /house-vote/{congress}/{session}/{rollCall}/members   how every member voted
      /committee/{chamber}/{committeeCode}         committee detail; .../bills
      /nomination/{congress}  /treaty/{congress}  /law/{congress}  /amendment/{congress}
      /committee-report  /hearing  /congressional-record  /summaries/{congress}/{type}
    Senate roll-call votes are NOT in this API.

    params: query parameters, e.g. {"limit": 50, "offset": 0, "fromDateTime": "2026-11-01T00:00:00Z",
    "sort": "updateDate+desc"}. limit max 250. Dates are ISO-8601 with Z.
    """
    clean = {k: v for k, v in (params or {}).items() if k.lower() not in _RESERVED_PARAMS}
    if isinstance(clean.get("sort"), str):
        clean["sort"] = clean["sort"].replace("+", " ")  # httpx sends " " as "+"; a literal "+" would be %2B
    return compact(await api_get(normalize_path(path), clean))


_TEXT_HOSTS = {"www.congress.gov", "congress.gov"}
_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(raw: str) -> str:
    return html.unescape(_TAG_RE.sub("", raw)).strip()


@mcp.tool(annotations=READ_ONLY, structured_output=False)
async def read_bill_text(url: str, offset: int = 0, max_chars: int = 40_000) -> str:
    """Read the full text of a bill. url: a "Formatted Text" (.htm) URL from
    /bill/{congress}/{type}/{number}/text, e.g. https://www.congress.gov/119/bills/hr1/BILLS-119hr1enr.htm.

    Big bills run to millions of characters: read in chunks with offset/max_chars (max 100000).
    Returns total_chars and next_offset (null at the end).
    """
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or parts.hostname not in _TEXT_HOSTS or not parts.path.endswith(".htm"):
        raise ToolError("url must be an https://www.congress.gov/... .htm bill text URL")
    if offset < 0:
        raise ToolError("offset must be >= 0")
    max_chars = max(1, min(max_chars, 100_000))

    # ponytail: refetches the whole document per chunk; add a small LRU if paging gets slow
    resp = await client().get(url, follow_redirects=True)  # no API key: this is www.congress.gov
    if resp.status_code != 200:
        raise ToolError(f"congress.gov returned {resp.status_code} for {url}")
    text = html_to_text(resp.text)
    end = min(offset + max_chars, len(text))
    return compact(
        {
            "total_chars": len(text),
            "offset": offset,
            "next_offset": end if end < len(text) else None,
            "text": text[offset:end],
        }
    )


# --- HTTP app ----------------------------------------------------------------


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


class BearerAuth:
    """Pure ASGI middleware: every HTTP request except /health needs `Authorization: Bearer <token>`."""

    def __init__(self, app: Any, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"] != "/health":
            supplied = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self.expected):
                resp = JSONResponse({"error": "unauthorized"}, 401, {"WWW-Authenticate": "Bearer"})
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app(token: str) -> BearerAuth:
    # host="0.0.0.0" skips the SDK's localhost-only Host check; requests arrive as congress.barrywalker.io
    inner = mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")
    return BearerAuth(inner, token)


def main() -> None:
    if not os.environ.get("CONGRESS_API_KEY"):
        raise SystemExit("CONGRESS_API_KEY is required")
    token = os.environ.get("MCP_BEARER_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("MCP_BEARER_TOKEN is required (>= 32 chars; e.g. openssl rand -hex 32)")
    uvicorn.run(build_app(token), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
