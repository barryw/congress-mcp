# congress-mcp

MCP server over the [Congress.gov API](https://api.congress.gov/): who's sponsoring what, who signed on,
how the House voted, and what the bills actually say.

Endpoint: `https://congress.barrywalker.io/mcp` (Streamable HTTP, stateless, bearer token).

## Tools

| Tool | What it does |
|---|---|
| `find_members` | Name/state/district/chamber lookup → `bioguideId`. The API has no name search; this pulls the roster and matches locally. |
| `congress_api` | GET any `/v3` path: member sponsored/cosponsored bills, bill actions/cosponsors/text, House roll-call votes, committees, nominations, laws. |
| `read_bill_text` | Reads bill text from a congress.gov `.htm` URL, in chunks (`offset`/`max_chars`). |

Senate roll-call votes are not in the Congress.gov API.

## Connect

```sh
claude mcp add --transport http congress https://congress.barrywalker.io/mcp \
  --header "Authorization: Bearer $(cat ~/.congress-mcp.token)"
```

## Develop

```sh
uv sync
uv run pytest -q
CONGRESS_API_KEY=DEMO_KEY MCP_BEARER_TOKEN=$(openssl rand -hex 32) uv run python server.py   # :8080
```

`DEMO_KEY` is limited to about 10 requests an hour. A real key ([sign up](https://api.congress.gov/sign-up/)) allows 5,000.

## Deploy

Manifests live in `deploy/kubernetes/congress/` and are copied into `barryw/infrastructure` at
`kubernetes/congress/` (ArgoCD ApplicationSet). The secret `congress-mcp` (`CONGRESS_API_KEY`,
`MCP_BEARER_TOKEN`) is a SealedSecret in that repo. Cloudflare's `*.barrywalker.io` tunnel route sends
the traffic to Traefik.

On a push to `main`, Woodpecker builds `ghcr.io/barryw/congress-mcp:sha-<12>` and opens and merges the
image-bump PR in the infrastructure repo.
