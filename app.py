import os, time, json, math, requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

APP_NAME = "mcp-meta"
APP_VER  = "0.1.0"
META_API_VERSION = os.getenv("META_API_VERSION", "v23.0")
ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN")  # injected via Secret Manager
GRAPH = f"https://graph.facebook.com/{META_API_VERSION}"

app = FastAPI()

# ---------- helpers ----------
class FBError(Exception): pass

def g(path, params=None):
    if params is None: params = {}
    params["access_token"] = ACCESS_TOKEN
    url = f"{GRAPH}/{path.lstrip('/')}"
    t0 = time.time()
    r = requests.get(url, params=params, timeout=60)
    if r.status_code >= 400:
        try:
            j = r.json()
        except Exception:
            j = {"error": {"message": r.text}}
        raise FBError(j.get("error", {}).get("message", r.text))
    app.logger if hasattr(app, "logger") else None
    return r.json()

def chunk(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]

def ensure_token():
    if not ACCESS_TOKEN:
        raise FBError("META_ACCESS_TOKEN is not set")

# ---------- MCP describe tools ----------
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

# ---------- Tool implementations ----------
def tool_list_ads(args):
    ensure_token()
    account_id = args["account_id"]
    limit = int(args.get("limit", 50))
    after = args.get("after")
    fields = "id,name,effective_status,adset{id,name},campaign{id,name},creative{id}"
    params = {"fields": fields, "limit": limit}
    if after: params["after"] = after
    data = g(f"{account_id}/ads", params)
    return data

def _creative_fields():
    return "object_story_spec,asset_feed_spec,url_tags,ad_creative_features_spec"

def tool_get_ad_creatives(args):
    ensure_token()
    out = {}
    if "creative_ids" in args:
        for batch in chunk(args["creative_ids"], 25):
            ids = ",".join(batch)
            res = g("", params={"ids": ids, "fields": _creative_fields()})
            out.update({k: v for k, v in res.items()})
    else:
        # by ad ids
        for ad_id in args["ad_ids"]:
            res = g(f"{ad_id}", params={"fields": f"creative{{{_creative_fields()}}}"})
            cr = res.get("creative", {})
            out[ad_id] = cr
    return out

def tool_get_insights(args):
    ensure_token()
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

def lint_issue(kind, detail):
    return {"type": kind, "detail": detail}

def tool_audit_ads(args):
    ensure_token()
    ad_ids = args["ad_ids"]
    rules  = {"max_primary_text":125, "max_headline":40}
    rules.update(args.get("rules", {}))

    raw = tool_get_ad_creatives({"ad_ids": ad_ids})
    findings = []
    for ad_id, creative in raw.items():
        issues = []
        # Extract primary text / headline / link(s)
        bodies = []
        titles = []
        links  = []
        url_tags = None

        # object_story_spec.link_data
        oss = (creative or {}).get("object_story_spec", {}) or {}
        ld  = oss.get("link_data", {}) or {}
        if ld.get("message"): bodies.append(ld["message"])
        if ld.get("name"): titles.append(ld["name"])
        if ld.get("link"): links.append(ld["link"])
        url_tags = creative.get("url_tags") or ld.get("call_to_action", {}).get("value", {}).get("link")

        # asset_feed_spec (multiple variants)
        afs = (creative or {}).get("asset_feed_spec", {}) or {}
        bodies += afs.get("bodies", [])
        titles += afs.get("titles", [])
        links  += afs.get("link_urls", [])

        # Copy length checks
        for b in bodies:
            if b and len(b) > rules["max_primary_text"]:
                issues.append(lint_issue("primary_text_length", f"{len(b)} chars > {rules['max_primary_text']}"))
        for t in titles:
            if t and len(t) > rules["max_headline"]:
                issues.append(lint_issue("headline_length", f"{len(t)} chars > {rules['max_headline']}"))

        # URL hygiene
        for u in links:
            if u and not str(u).lower().startswith("https://"):
                issues.append(lint_issue("non_https_url", u))
        if url_tags is None or str(url_tags).strip() == "":
            issues.append(lint_issue("missing_url_tags", "No UTM or url_tags found"))

        # Variant coverage
        if len(bodies) <= 1 or len(titles) <= 1:
            issues.append(lint_issue("low_variant_coverage",
                        f"bodies={len(bodies)} titles={len(titles)}"))

        # Advantage+ flags visibility (just return the struct if present)
        flags = (creative or {}).get("ad_creative_features_spec", None)

        findings.append({
            "ad_id": ad_id,
            "issues": issues,
            "ad_creative_features_spec": flags
        })
    return {"results": findings}

# ---------- MCP JSON-RPC endpoint ----------
@app.get("/")
def health():
    return PlainTextResponse("ok")

@app.post("/")
async def rpc(request: Request):
    payload = await request.json()
    method = payload.get("method")

    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": APP_NAME, "version": APP_VER},
            "tools": TOOLS
        }
        return JSONResponse({"jsonrpc":"2.0","id":payload.get("id"),"result": result})

    if method == "tools/call":
        params = payload.get("params", {}) or {}
        name = params.get("name")
        args = params.get("arguments", {}) or {}
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
                return JSONResponse({"jsonrpc":"2.0","id":payload.get("id"),
                    "error":{"code":-32601,"message":f"Unknown tool: {name}"}}, status_code=400)

            # MCP-style content envelope
            out = {"content":[{"type":"json","json": data}]}
            return JSONResponse({"jsonrpc":"2.0","id":payload.get("id"),"result": out})
        except FBError as e:
            return JSONResponse({"jsonrpc":"2.0","id":payload.get("id"),
                "error":{"code":-32000,"message":str(e)}}, status_code=400)

    # Fallback: method not found
    return JSONResponse({"jsonrpc":"2.0","id":payload.get("id"),
                         "error":{"code":-32601,"message":"Method not found"}}, status_code=404)
