# app.py
import os, time, json, random, logging
from typing import Dict, Any, List, Optional

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

import asyncio

APP_NAME = "mcp-meta"
APP_VER  = "0.2.0"  # web-friendly revision
META_API_VERSION = os.getenv("META_API_VERSION", "v23.0")
ACCESS_TOKEN     = os.getenv("META_ACCESS_TOKEN")  # injected from Secret Manager
GRAPH            = f"https://graph.facebook.com/{META_API_VERSION}"

# ---------- Logging & shared-secret setup (define BEFORE using) ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mcp-meta")

# Optional: shared secret to block random internet callers.
MCP_SHARED_KEY = os.getenv("MCP_SHARED_KEY", "").strip()
log.info("MCP_SHARED_KEY enabled? %s", "YES" if MCP_SHARED_KEY else "NO")

# ----------------------------- FastAPI app & middleware -----------------------------
app = FastAPI()

CLAUDE_ORIGINS = [
    "https://claude.ai",
    "https://www.claude.ai",
    "https://console.anthropic.com",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CLAUDE_ORIGINS,      # must be explicit when allow_credentials=True
    allow_credentials=True,            # important for browser clients
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["MCP-Protocol-Version", "Mcp-Session-Id"],
)

# Always surface MCP-Protocol-Version so clients can see it
class MCPProtocolHeader(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        proto = request.headers.get("Mcp-Protocol-Version") or "2024-11-05"
        response = await call_next(request)
        response.headers["MCP-Protocol-Version"] = proto
        return response

app.add_middleware(MCPProtocolHeader)

# ----------------------------- HTTP client (pooling + retries) ---------------------

class FBError(Exception): pass

_session = requests.Session()
_adapter  = requests.adapters.HTTPAdapter(
    max_retries=0, pool_connections=20, pool_maxsize=50
)
_session.mount("https://", _adapter)

_TRANSIENT_CODES = {"1","2","4","17","32","613"}  # common transient Meta error codes

def _ensure_token():
    if not ACCESS_TOKEN:
        raise FBError("META_ACCESS_TOKEN is not set")

def g(path: str, params: Optional[Dict[str, Any]] = None, max_attempts: int = 5) -> Dict[str, Any]:
    _ensure_token()
    if params is None: params = {}
    params["access_token"] = ACCESS_TOKEN
    url = f"{GRAPH}/{path.lstrip('/')}"
    for attempt in range(1, max_attempts + 1):
        r = _session.get(url, params=params, timeout=60)
        if r.status_code < 400:
            return r.json()
        # parse error
        try:
            err = r.json().get("error", {})
        except Exception:
            err = {"message": r.text, "code": "unknown"}
        code = str(err.get("code"))
        fbtrace = err.get("fbtrace_id")
        msg = f"{err.get('message')} (code={code}, fbtrace_id={fbtrace})"
        # transient -> retry with jitter
        if code in _TRANSIENT_CODES and attempt < max_attempts:
            time.sleep(min(2**attempt, 20) + random.random())
            continue
        raise FBError(msg)

def _chunk(seq: List[str], n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]

# ----------------------------- Tools descriptor ------------------------------------

TOOLS = [
    {
        "name": "list_ads",
        "description": "List ads in an ad account with campaign/adset/creative context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "Ad account id, e.g. act_1234567890"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
                "after": {"type": ["string","null"], "description": "Pagination cursor"}
            },
            "required": ["account_id"]
        }
    },
    {
        "name": "get_ad_creatives",
        "description": "Fetch creative copy/links/flags for ads or creatives.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ad_ids": {"type":"array","items":{"type":"string"}},
                "creative_ids": {"type":"array","items":{"type":"string"}}
            },
            "oneOf": [{"required":["ad_ids"]}, {"required":["creative_ids"]}]
        }
    },
    {
        "name": "get_insights",
        "description": "Pull performance insights for account/campaign/adset/ad.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope_id": {"type":"string","description":"act_<id> | <campaign_id> | <adset_id> | <ad_id>"},
                "level": {"type":"string","enum":["account","campaign","adset","ad"], "default":"ad"},
                "date_preset": {"type":"string","description":"e.g. last_7d,last_30d"},
                "time_range": {"type":"object","properties":{"since":{"type":"string"},"until":{"type":"string"}}},
                "fields": {"type":"array","items":{"type":"string"},
                    "default":["ad_id","ad_name","spend","impressions","clicks","cpc","cpm","ctr"]},
                "breakdowns":{"type":"array","items":{"type":"string"}},
                "limit":{"type":"integer","minimum":1,"maximum":500,"default":100},
                "after":{"type":["string","null"]}
            },
            "required":["scope_id"]
        }
    },
    {
        "name": "audit_ads",
        "description": "Run simple creative lints: copy length, URL hygiene, variant coverage.",
        "inputSchema": {
            "type":"object",
            "properties":{
                "ad_ids":{"type":"array","items":{"type":"string"}},
                "rules":{"type":"object","properties":{
                    "max_primary_text":{"type":"integer","default":125},
                    "max_headline":{"type":"integer","default":40}
                }}
            },
            "required":["ad_ids"]
        }
    }
]

# ----------------------------- Tool implementations --------------------------------

def tool_list_ads(args: Dict[str, Any]) -> Dict[str, Any]:
    account_id = args["account_id"]
    limit = int(args.get("limit", 50))
    after = args.get("after")
    fields = "id,name,effective_status,adset{id,name},campaign{id,name},creative{id}"
    params = {"fields": fields, "limit": limit}
    if after: params["after"] = after
    return g(f"{account_id}/ads", params)

def _creative_fields() -> str:
    # In v23, creative_features_spec is nested under degrees_of_freedom_spec
    return "object_story_spec,asset_feed_spec,url_tags,degrees_of_freedom_spec"

def tool_get_ad_creatives(args: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if "creative_ids" in args and args["creative_ids"]:
        for batch in _chunk(args["creative_ids"], 25):
            ids = ",".join(batch)
            res = g("", params={"ids": ids, "fields": _creative_fields()})
            out.update({k: v for k, v in res.items()})
    else:
        for ad_id in args["ad_ids"]:
            res = g(f"{ad_id}", params={"fields": f"creative{{{_creative_fields()}}}"})
            out[ad_id] = res.get("creative", {}) or {}
    return out

def tool_get_insights(args: Dict[str, Any]) -> Dict[str, Any]:
    scope_id   = args["scope_id"]
    level      = args.get("level", "ad")
    fields     = args.get("fields") or ["ad_id","ad_name","spend","impressions","clicks","cpc","cpm","ctr"]
    breakdowns = args.get("breakdowns")
    date_preset= args.get("date_preset")
    time_range = args.get("time_range")
    limit      = int(args.get("limit", 100))
    after      = args.get("after")

    params = {"level": level, "fields": ",".join(fields), "limit": limit}
    if breakdowns: params["breakdowns"] = ",".join(breakdowns)
    if date_preset: params["date_preset"] = date_preset
    if time_range: params["time_range"] = json.dumps(time_range)
    if after: params["after"] = after
    return g(f"{scope_id}/insights", params)

def _lint_issue(kind: str, detail: str) -> Dict[str, str]:
    return {"type": kind, "detail": detail}

def tool_audit_ads(args: Dict[str, Any]) -> Dict[str, Any]:
    ad_ids = args["ad_ids"]
    rules  = {"max_primary_text": 125, "max_headline": 40}
    rules.update(args.get("rules", {}) or {})

    raw = tool_get_ad_creatives({"ad_ids": ad_ids})
    findings: List[Dict[str, Any]] = []

    for ad_id, creative in raw.items():
        issues: List[Dict[str, str]] = []
        bodies: List[str] = []
        titles: List[str] = []
        links:  List[str] = []

        # --- extract creative pieces ---
        oss = (creative or {}).get("object_story_spec", {}) or {}
        ld  = oss.get("link_data", {}) or {}
        url_tags = (creative or {}).get("url_tags")  # only tags here

        # primary text / headline / link(s)
        if ld.get("message"): bodies.append(ld["message"])
        if ld.get("name"):    titles.append(ld["name"])
        if ld.get("link"):    links.append(ld["link"])

        # CTA link is also a link (not tags)
        cta_link = ld.get("call_to_action", {}).get("value", {}).get("link")
        if cta_link:
            links.append(cta_link)

        # asset feed variants
        afs = (creative or {}).get("asset_feed_spec", {}) or {}
        bodies += [x for x in afs.get("bodies", []) if x]
        titles += [x for x in afs.get("titles", []) if x]
        links  += [x for x in afs.get("link_urls", []) if x]

        # Advantage+ / creative features flags
        flags = (creative or {}).get("degrees_of_freedom_spec", {}).get("creative_features_spec")

        # --- lints ---
        uniq_bodies = {b for b in bodies if isinstance(b, str)}
        uniq_titles = {t for t in titles if isinstance(t, str)}
        uniq_links  = {u for u in links  if isinstance(u, str)}

        # copy length
        for b in uniq_bodies:
            if len(b) > rules["max_primary_text"]:
                issues.append(_lint_issue("primary_text_length",
                                          f"{len(b)} chars > {rules['max_primary_text']}"))
        for t in uniq_titles:
            if len(t) > rules["max_headline"]:
                issues.append(_lint_issue("headline_length",
                                          f"{len(t)} chars > {rules['max_headline']}"))

        # URL hygiene
        for u in uniq_links:
            if not u.lower().startswith("https://"):
                issues.append(_lint_issue("non_https_url", u))
        if url_tags is None or (isinstance(url_tags, str) and url_tags.strip() == ""):
            issues.append(_lint_issue("missing_url_tags", "No UTM or url_tags found"))

        # variant coverage
        if len(uniq_bodies) <= 1 or len(uniq_titles) <= 1:
            issues.append(_lint_issue("low_variant_coverage",
                                      f"bodies={len(uniq_bodies)} titles={len(uniq_titles)}"))

        # append per-ad result
        findings.append({
            "ad_id": ad_id,
            "issues": issues,
            "creative_features_spec": flags,
            "summary": {
                "unique_bodies": len(uniq_bodies),
                "unique_titles": len(uniq_titles),
                "unique_links": len(uniq_links),
                "has_url_tags": bool(url_tags)
            }
        })

    return {"results": findings}

# ----------------------------- Health & discovery ----------------------------------

@app.get("/", include_in_schema=False)
@app.head("/", include_in_schema=False)
async def root_get(request: Request):
    # Fast 200 for HEAD
    if request.method == "HEAD":
        return PlainTextResponse("")

    # Optional SSE keep-alive (safe to keep; remove if you don't need it)
    accept = (request.headers.get("accept") or "").lower()
    if "text/event-stream" in accept:
        async def stream():
            yield b": connected\n\n"
            while True:
                await asyncio.sleep(25)
                yield b": ping\n\n"
        headers = {"Cache-Control":"no-store","Connection":"keep-alive"}
        return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)

    return PlainTextResponse("ok")

# CORS preflight (any path)
@app.options("/{_any:path}")
async def any_options(_any: str):
    return PlainTextResponse("", status_code=204)

# OAuth discovery stubs (avoid 405 during connector probes)
@app.get("/.well-known/oauth-protected-resource", include_in_schema=False)
async def oauth_pr():
    return JSONResponse({"ok": False, "error": "oauth discovery not configured"}, status_code=404)

@app.get("/.well-known/oauth-authorization-server", include_in_schema=False)
async def oauth_as():
    return JSONResponse({"ok": False, "error": "oauth discovery not configured"}, status_code=404)

# MCP discovery endpoints
@app.get("/.well-known/mcp.json")
def mcp_discovery():
    return JSONResponse({
        "mcpVersion": "1.0",
        "name": APP_NAME,
        "version": APP_VER,
        "auth": {"type": "none"},
        "tools": TOOLS
    })

@app.get("/mcp/tools")
def mcp_tools():
    return JSONResponse({"tools": TOOLS})

# ----------------------------- JSON-RPC core ---------------------------------------

def _authz_check(request: Request) -> Optional[JSONResponse]:
    # Optional shared-secret check (only if MCP_SHARED_KEY is set)
    if MCP_SHARED_KEY:
        key = request.headers.get("X-MCP-Key") or ""
        if key != MCP_SHARED_KEY:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32001, "message": "Unauthorized"}},
                status_code=200
            )
    return None

@app.post("/")
async def rpc(request: Request):
    # Optional shared-secret
    maybe = _authz_check(request)
    if maybe: return maybe

    # Accept body from catch-all if already parsed
    payload = getattr(request.state, "json_payload", None)
    if payload is None:
        payload = await request.json()
    method = payload.get("method")
    _id     = payload.get("id")
    log.info(f"RPC method: {method}")

    if method == "initialize":
        client_proto = (payload.get("params") or {}).get("protocolVersion") or "2024-11-05"
        result = {
            "protocolVersion": client_proto,
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": APP_NAME, "version": APP_VER},
            "tools": TOOLS
        }
        return JSONResponse(
        {"jsonrpc":"2.0","id":_id,"result": result},
        headers={"MCP-Protocol-Version": client_proto}
    )

    if method in ("initialized", "notifications/initialized"):
        return JSONResponse({"jsonrpc":"2.0","id":_id,"result":{"ok": True}})

    if method == "tools/list":
        return JSONResponse({"jsonrpc":"2.0","id":_id,"result": {"tools": TOOLS}})

    if method == "tools/call":
        params = payload.get("params") or {}
        name   = params.get("name")
        args   = params.get("arguments") or {}
        try:
            if name == "list_ads":
                data = tool_list_ads(args)
            elif name == "get_ad_creatives":
                data = tool_get_ad_creatives(args)
            elif name == "get_insights":
                data = tool_get_insights(args)
            elif name == "audit_ads":
                data = tool_audit_ads(args)
            else:
                return JSONResponse({"jsonrpc":"2.0","id":_id,
                    "error":{"code":-32601,"message":f"Unknown tool: {name}"}})
            return JSONResponse({"jsonrpc":"2.0","id":_id,
                "result":{"content":[{"type":"json","json": data}]}})
        except FBError as e:
            return JSONResponse({"jsonrpc":"2.0","id":_id,
                                 "error":{"code":-32000,"message":str(e)}})

    # Fallback JSON-RPC error (HTTP 200 as per JSON-RPC)
    return JSONResponse({"jsonrpc":"2.0","id":_id,
                         "error":{"code":-32601,"message":f"Method not found: {method}"}})

# Catch-all POST (handles /register and friends)
@app.post("/{_catchall:path}")
async def rpc_catch(request: Request, _catchall: str):
    # Optional shared-secret
    maybe = _authz_check(request)
    if maybe: return maybe

    try:
        payload = await request.json()
        if isinstance(payload, dict) and "method" in payload:
            request.state.json_payload = payload
            return await rpc(request)
    except Exception:
        pass
    return PlainTextResponse("ok")

# ----------------------------- Local dev entrypoint --------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
