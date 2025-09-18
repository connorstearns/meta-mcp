# app.py (async MCP server for Meta Ads)
import os, json, re, asyncio, random, logging
from typing import Dict, Any, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

# ----------------------------- Config -----------------------------

APP_NAME = "mcp-meta"
APP_VER  = "0.3.0"
INSTRUCTIONS = (
    "This MCP server audits Meta ads (copy/URLs/flags) and pulls performance insights.\n"
    "Tools:\n"
    "• list_ads — list ads with campaign/adset/creative\n"
    "• get_ad_creatives — fetch creative fields for ads/creatives\n"
    "• get_insights — performance; supports custom_conversions by name/ID\n"
    "• audit_ads — lints on copy length, https URLs, variant coverage\n"
    "• list_custom_conversions — id↔name for account custom conversions"
)

META_API_VERSION = os.getenv("META_API_VERSION", "v23.0")
ACCESS_TOKEN     = os.getenv("META_ACCESS_TOKEN")     # (Secret Manager)
GRAPH            = f"https://graph.facebook.com/{META_API_VERSION}"
MCP_SHARED_KEY   = os.getenv("MCP_SHARED_KEY", "").strip()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(APP_NAME)
log.info("MCP_SHARED_KEY enabled? %s", "YES" if MCP_SHARED_KEY else "NO")

# ----------------------------- FastAPI & middleware -----------------------------

app = FastAPI()

ALLOWED_ORIGINS = [
    # Anthropic (Claude)
    "https://claude.ai", "https://www.claude.ai", "https://console.anthropic.com",
    # OpenAI (ChatGPT)
    "https://chat.openai.com", "https://www.chat.openai.com", "https://chatgpt.com",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["MCP-Protocol-Version", "Mcp-Session-Id"],
)

@app.middleware("http")
async def _reqlog(request: Request, call_next):
    log.info("REQ %s %s Origin=%s", request.method, request.url.path, request.headers.get("origin"))
    resp = await call_next(request)
    log.info("RESP %s %s %s", request.method, request.url.path, resp.status_code)
    return resp

class MCPProtocolHeader(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        proto = request.headers.get("Mcp-Protocol-Version") or "2024-11-05"
        response = await call_next(request)
        response.headers["MCP-Protocol-Version"] = proto
        return response
app.add_middleware(MCPProtocolHeader)

# Global exception → JSON-RPC error (prevent HTTP 424s surfacing)
@app.exception_handler(ExceptionGroup)
async def eg_handler(req: Request, exc: ExceptionGroup):
    log.exception("ExceptionGroup", exc_info=exc)
    return JSONResponse({"jsonrpc":"2.0","id":None,"error":{"code":-32097,"message":"Server exception group"}}, status_code=200)

@app.exception_handler(BaseExceptionGroup)
async def beg_handler(req: Request, exc: BaseExceptionGroup):
    log.exception("BaseExceptionGroup", exc_info=exc)
    return JSONResponse({"jsonrpc":"2.0","id":None,"error":{"code":-32096,"message":"Server base exception group"}}, status_code=200)

@app.exception_handler(HTTPException)
async def http_handler(req: Request, exc: HTTPException):
    log.exception("HTTPException %s", exc.status_code)
    return JSONResponse({"jsonrpc":"2.0","id":None,"error":{"code":-32095,"message":f"HTTP {exc.status_code}"}}, status_code=200)

# ----------------------------- HTTP client -----------------------------

class FBError(Exception): pass

_client = httpx.AsyncClient(timeout=60)
_TRANSIENT_CODES = {"1","2","4","17","32","613"}

def _ensure_token():
    if not ACCESS_TOKEN:
        raise FBError("META_ACCESS_TOKEN is not set")

async def g(path: str, params: Optional[Dict[str, Any]] = None, max_attempts: int = 5) -> Dict[str, Any]:
    _ensure_token()
    params = params or {}
    params["access_token"] = ACCESS_TOKEN
    url = f"{GRAPH}/{path.lstrip('/')}"
    for attempt in range(1, max_attempts + 1):
        r = await _client.get(url, params=params)
        if r.status_code < 400:
            # Log Meta usage headers (handy for throttling)
            for hk in ("X-App-Usage", "X-Ad-Account-Usage", "X-Business-Use-Case-Usage"):
                if hk in r.headers:
                    log.info("%s=%s", hk, r.headers[hk])
            return r.json()
        try:
            err = r.json().get("error", {})
        except Exception:
            err = {"message": r.text, "code": "unknown"}
        code = str(err.get("code"))
        fbtrace = err.get("fbtrace_id")
        msg = f"{err.get('message')} (code={code}, fbtrace_id={fbtrace})"
        if code in _TRANSIENT_CODES and attempt < max_attempts:
            await asyncio.sleep(min(2**attempt, 20) + random.random())
            continue
        raise FBError(msg)

def _chunk(seq: List[str], n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]

# ----------------------------- Helpers (insights & conversions) -----------------------------

_slug_re = re.compile(r"[^a-z0-9]+")
def _slug(s: str) -> str:
    return _slug_re.sub("_", (s or "").lower()).strip("_")

async def list_custom_conversions(account_id: str) -> List[Dict[str, Any]]:
    res = await g(f"{account_id}/customconversions",
                  params={"fields": "id,name,custom_event_type,event_source_id", "limit": 200})
    return res.get("data", []) or []

def _index_cc_by_name_and_id(ccs: List[Dict[str, Any]]):
    by_id = {str(c.get("id")): c for c in ccs}
    by_name = {}
    for c in ccs:
        nm = (c.get("name") or "").strip()
        if nm:
            by_name.setdefault(nm.lower(), []).append(c)
    return by_id, by_name

async def _resolve_custom_conversion_keys(account_id: str, requested: List[str]) -> Dict[str, Dict[str, str]]:
    if not requested:
        return {}
    ccs = await list_custom_conversions(account_id)
    by_id, by_name = _index_cc_by_name_and_id(ccs)
    out = {}
    for item in requested:
        s = str(item).strip()
        if not s: continue
        if s in by_id:
            cc = by_id[s]
        else:
            ll = s.lower()
            exact = by_name.get(ll)
            if exact: cc = exact[0]
            else:
                # prefix fallback
                candidates = [c for k, lst in by_name.items() if k.startswith(ll) for c in lst]
                cc = candidates[0] if candidates else None
        if cc:
            cc_id = str(cc.get("id"))
            nm = cc.get("name") or cc_id
            sl = _slug(nm)
            out[s] = {"id": cc_id, "name": nm, "slug": sl, "action_key": f"offsite_conversion.custom.{cc_id}"}
    return out

def _index_actions(row: dict) -> Tuple[Dict[str, float], Dict[str, float]]:
    acts = row.get("actions") or []
    vals = row.get("action_values") or []
    a = {x.get("action_type"): float(x.get("value", 0)) for x in acts if x.get("action_type")}
    v = {x.get("action_type"): float(x.get("value", 0)) for x in vals if x.get("action_type")}
    return a, v

def _lint_issue(kind: str, detail: str) -> Dict[str, str]:
    return {"type": kind, "detail": detail}

# ----------------------------- Tool descriptors -----------------------------

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
        "description": "Pull performance insights for account/campaign/adset/ad. Supports custom_conversions by name or ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope_id": {"type":"string","description":"act_<id> | <campaign_id> | <adset_id> | <ad_id>"},
                "account_id": {"type":"string","description":"Required when using custom_conversions and scope_id is not act_<id>"},
                "level": {"type":"string","enum":["account","campaign","adset","ad"], "default":"ad"},
                "date_preset": {"type":"string","description":"e.g. last_7d,last_30d"},
                "time_range": {"type":"object","properties":{"since":{"type":"string"},"until":{"type":"string"}}},
                "fields": {"type":"array","items":{"type":"string"},
                    "default":["ad_id","ad_name","spend","impressions","clicks","cpc","cpm","ctr"]},
                "breakdowns":{"type":"array","items":{"type":"string"}},
                "limit":{"type":"integer","minimum":1,"maximum":500,"default":100},
                "after":{"type":["string","null"]},
                "custom_conversions":{"type":"array","items":{"type":"string"},
                    "description":"List of conversion names or IDs from Events Manager"}
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
    },
    {
        "name": "list_custom_conversions",
        "description": "List custom conversions for an ad account (id ↔ name).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "Ad account id, e.g. act_1234567890"}
            },
            "required": ["account_id"]
        }
    }
]

# ----------------------------- Tool implementations (async) -----------------------------

async def tool_list_ads(args: Dict[str, Any]) -> Dict[str, Any]:
    account_id = args["account_id"]
    limit = int(args.get("limit", 50))
    after = args.get("after")
    fields = "id,name,effective_status,adset{id,name},campaign{id,name},creative{id}"
    params = {"fields": fields, "limit": limit}
    if after: params["after"] = after
    return await g(f"{account_id}/ads", params)

def _creative_fields() -> str:
    # In v23, creative_features_spec is under degrees_of_freedom_spec
    return "object_story_spec,asset_feed_spec,url_tags,degrees_of_freedom_spec"

async def tool_get_ad_creatives(args: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if args.get("creative_ids"):
        for batch in _chunk(args["creative_ids"], 25):
            ids = ",".join(batch)
            res = await g("", params={"ids": ids, "fields": _creative_fields()})
            out.update({k: v for k, v in res.items()})
    else:
        for ad_id in args["ad_ids"]:
            res = await g(f"{ad_id}", params={"fields": f"creative{{{_creative_fields()}}}"})
            out[ad_id] = res.get("creative", {}) or {}
    return out

async def tool_get_insights(args: Dict[str, Any]) -> Dict[str, Any]:
    scope_id    = args["scope_id"]
    level       = args.get("level", "ad")
    fields      = args.get("fields") or ["ad_id","ad_name","spend","impressions","clicks","cpc","cpm","ctr"]
    breakdowns  = args.get("breakdowns")
    date_preset = args.get("date_preset")
    time_range  = args.get("time_range")
    limit       = int(args.get("limit", 100))
    after       = args.get("after")
    custom_conversions: List[str] = args.get("custom_conversions") or []

    if level not in ("account","campaign","adset","ad"):
        raise FBError(f"Invalid level '{level}'. Choose one of account,campaign,adset,ad.")

    # Prefer explicit time_range over date_preset to avoid API conflict
    if date_preset and time_range:
        date_preset = None

    # If requesting conversions, ensure actions fields included
    if custom_conversions:
        if "actions" not in fields: fields.append("actions")
        if "action_values" not in fields: fields.append("action_values")

    params = {"level": level, "fields": ",".join(fields), "limit": limit}
    if breakdowns:  params["breakdowns"] = ",".join(breakdowns)
    if date_preset: params["date_preset"] = date_preset
    if time_range:  params["time_range"] = json.dumps(time_range)
    if after:       params["after"] = after

    data = await g(f"{scope_id}/insights", params)

    # No computed conversions requested
    if not custom_conversions:
        return data

    # Need account_id if scope is not account
    if scope_id.startswith("act_"):
        account_id = scope_id
    else:
        account_id = args.get("account_id")
        if not account_id:
            raise FBError("Provide 'account_id':'act_<id>' when using 'custom_conversions' and scope_id is not account-level.")

    # Resolve requested names/IDs → action keys
    resolved = await _resolve_custom_conversion_keys(account_id, custom_conversions)

    rows = data.get("data") or data.get("rows") or []
    for row in rows:
        actions, values = _index_actions(row)
        spend = float(row.get("spend", 0) or 0)
        for req, meta in resolved.items():
            key  = meta["action_key"]  # offsite_conversion.custom.<ID>
            slug = meta["slug"]
            conv = float(actions.get(key, 0.0))
            val  = float(values.get(key, 0.0))
            row[f"conv_{slug}"]  = conv
            row[f"value_{slug}"] = val
            row[f"cpa_{slug}"]   = (spend / conv) if conv else None
            row[f"roas_{slug}"]  = (val / spend)  if spend else None

    return data

async def tool_audit_ads(args: Dict[str, Any]) -> Dict[str, Any]:
    ad_ids = args["ad_ids"]
    rules  = {"max_primary_text": 125, "max_headline": 40}
    rules.update(args.get("rules", {}) or {})
    raw = await tool_get_ad_creatives({"ad_ids": ad_ids})
    findings: List[Dict[str, Any]] = []

    for ad_id, creative in raw.items():
        issues: List[Dict[str, str]] = []
        bodies: List[str] = []
        titles: List[str] = []
        links:  List[str] = []

        oss = (creative or {}).get("object_story_spec", {}) or {}
        ld  = oss.get("link_data", {}) or {}
        url_tags = (creative or {}).get("url_tags")

        if ld.get("message"): bodies.append(ld["message"])
        if ld.get("name"):    titles.append(ld["name"])
        if ld.get("link"):    links.append(ld["link"])
        cta_link = ld.get("call_to_action", {}).get("value", {}).get("link")
        if cta_link: links.append(cta_link)

        afs = (creative or {}).get("asset_feed_spec", {}) or {}
        bodies += [x for x in afs.get("bodies", []) if x]
        titles += [x for x in afs.get("titles", []) if x]
        links  += [x for x in afs.get("link_urls", []) if x]

        # flags (v23)
        flags = (creative or {}).get("degrees_of_freedom_spec", {}).get("creative_features_spec")

        uniq_bodies = {b for b in bodies if isinstance(b, str)}
        uniq_titles = {t for t in titles if isinstance(t, str)}
        uniq_links  = {u for u in links  if isinstance(u, str)}

        for b in uniq_bodies:
            if len(b) > rules["max_primary_text"]:
                issues.append(_lint_issue("primary_text_length", f"{len(b)} chars > {rules['max_primary_text']}"))
        for t in uniq_titles:
            if len(t) > rules["max_headline"]:
                issues.append(_lint_issue("headline_length", f"{len(t)} chars > {rules['max_headline']}"))

        for u in uniq_links:
            if not u.lower().startswith("https://"):
                issues.append(_lint_issue("non_https_url", u))
        if url_tags is None or (isinstance(url_tags, str) and url_tags.strip() == ""):
            issues.append(_lint_issue("missing_url_tags", "No UTM or url_tags found"))

        if len(uniq_bodies) <= 1 or len(uniq_titles) <= 1:
            issues.append(_lint_issue("low_variant_coverage", f"bodies={len(uniq_bodies)} titles={len(uniq_titles)}"))

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

async def tool_list_custom_conversions(args: Dict[str, Any]) -> Dict[str, Any]:
    account_id = args["account_id"]
    data = await list_custom_conversions(account_id)
    out = [{"id": d.get("id"), "name": d.get("name"),
            "custom_event_type": d.get("custom_event_type"),
            "event_source_id": d.get("event_source_id"),
            "slug": _slug(d.get("name") or d.get("id"))}
           for d in data]
    return {"custom_conversions": out}

# ----------------------------- Health & discovery -----------------------------

@app.get("/", include_in_schema=False)
@app.head("/", include_in_schema=False)
async def root_get(request: Request):
    if request.method == "HEAD":
        return PlainTextResponse("")
    return PlainTextResponse("ok")

@app.options("/{_any:path}")
async def any_options(_any: str):
    return PlainTextResponse("", status_code=204)

@app.get("/.well-known/oauth-protected-resource", include_in_schema=False)
async def oauth_pr():
    return JSONResponse({"ok": False, "error": "oauth discovery not configured"}, status_code=404)

@app.get("/.well-known/oauth-authorization-server", include_in_schema=False)
async def oauth_as():
    return JSONResponse({"ok": False, "error": "oauth discovery not configured"}, status_code=404)

@app.get("/.well-known/mcp.json")
def mcp_discovery():
    return JSONResponse({
        "mcpVersion": "2024-11-05",
        "name": APP_NAME,
        "version": APP_VER,
        "auth": {"type": "none"},
        "capabilities": {"tools": {"listChanged": True}},
        "endpoints": {"rpc": "/"},
        "instructions": INSTRUCTIONS,
        "tools": TOOLS
    })

@app.get("/mcp/tools")
def mcp_tools():
    return JSONResponse({"tools": TOOLS})

@app.get("/register", include_in_schema=False)
def register_get():
    return JSONResponse({"ok": True})

@app.post("/register", include_in_schema=False)
def register_post():
    return JSONResponse({"ok": True})

@app.get("/sse", include_in_schema=False)
async def sse():
    async def stream():
        yield b": connected\n\n"
        while True:
            await asyncio.sleep(25)
            yield b": ping\n\n"
    headers = {"Cache-Control":"no-store","Connection":"keep-alive"}
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)

# ----------------------------- JSON-RPC core -----------------------------

def _authz_check(request: Request) -> Optional[JSONResponse]:
    if MCP_SHARED_KEY:
        key = request.headers.get("X-MCP-Key") or ""
        if key != MCP_SHARED_KEY:
            return JSONResponse({"jsonrpc":"2.0","id":None,"error":{"code":-32001,"message":"Unauthorized"}}, status_code=200)
    return None

@app.post("/")
async def rpc(request: Request):
    maybe = _authz_check(request)
    if maybe: return maybe
    try:
        payload = getattr(request.state, "json_payload", None) or await request.json()
        method  = payload.get("method")
        _id     = payload.get("id")
        log.info("RPC method: %s", method)

        if method == "initialize":
            client_proto = (payload.get("params") or {}).get("protocolVersion") or "2024-11-05"
            result = {
                "protocolVersion": client_proto,
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": APP_NAME, "version": APP_VER},
                "instructions": INSTRUCTIONS,
                "tools": TOOLS
            }
            return JSONResponse({"jsonrpc":"2.0","id":_id,"result": result},
                                headers={"MCP-Protocol-Version": client_proto})

        if method in ("initialized", "notifications/initialized"):
            return JSONResponse({"jsonrpc":"2.0","id":_id,"result":{"ok": True}})

        if method in ("tools/list","tools.list","list_tools","tools.index"):
            return JSONResponse({"jsonrpc":"2.0","id":_id,"result":{"tools": TOOLS}})

        if method == "tools/call":
            params = payload.get("params") or {}
            name   = params.get("name")
            args   = params.get("arguments") or {}

            try:
                if name == "list_ads":
                    data = await tool_list_ads(args)
                elif name == "get_ad_creatives":
                    data = await tool_get_ad_creatives(args)
                elif name == "get_insights":
                    data = await tool_get_insights(args)
                elif name == "audit_ads":
                    data = await tool_audit_ads(args)
                elif name == "list_custom_conversions":
                    data = await tool_list_custom_conversions(args)
                else:
                    return JSONResponse({"jsonrpc":"2.0","id":_id,
                        "error":{"code":-32601,"message":f"Unknown tool: {name}"}})
                return JSONResponse({"jsonrpc":"2.0","id":_id,
                    "result":{"content":[{"type":"json","json": data}]}})
            except FBError as e:
                log.exception("Tool call failed (FBError)")
                return JSONResponse({"jsonrpc":"2.0","id":_id,
                    "error":{"code":-32000,"message":str(e)}})
            except Exception as e:
                log.exception("Tool call failed (unexpected)")
                return JSONResponse({"jsonrpc":"2.0","id":_id,
                    "error":{"code":-32099,"message":f"Unhandled tool error: {e.__class__.__name__}: {e}"}})

        return JSONResponse({"jsonrpc":"2.0","id":_id,
                             "error":{"code":-32601,"message":f"Method not found: {method}"}})
    except Exception as e:
        log.exception("RPC dispatch exploded")
        return JSONResponse({"jsonrpc":"2.0","id":None,
                             "error":{"code":-32098,"message":f"RPC dispatch error: {e.__class__.__name__}: {e}"}})

@app.post("/{_catchall:path}")
async def rpc_catch(request: Request, _catchall: str):
    maybe = _authz_check(request)
    if maybe: return maybe
    try:
        payload = await request.json()
        if isinstance(payload, dict) and "method" in payload:
            request.state.json_payload = payload
            return await rpc(request)
    except Exception:
        pass
    return JSONResponse({"ok": True})

@app.on_event("shutdown")
async def _shutdown():
    try: await _client.aclose()
    except Exception: pass

# ----------------------------- Entrypoint (dev) -----------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
