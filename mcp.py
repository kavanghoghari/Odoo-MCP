# Odoo AI Dashboard Backend
#
# Install:  pip install fastapi uvicorn httpx python-dotenv
# Run:      python3 mcp.py
#
# Dashboard: http://localhost:8000          (serves index.html)
# Health:    /api/health
# KPI:       /api/kpis
# Pages:     /api/sales  /api/customers  /api/products  /api/inventory
# Chat:      POST /api/chat
#
# Talks directly to Odoo JSON-2. Read-only. Does NOT use the /mcp endpoint.

import os
import json
import time
import httpx
import uvicorn

from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel


# ============================================================
# CONFIGURATION
# ============================================================

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

ODOO_URL = os.getenv("ODOO_URL", "https://test20odoo1.odoo.com").rstrip("/")
ODOO_DB = os.getenv("ODOO_DB", "test20odoo1")
ODOO_API_KEY = os.getenv("ODOO_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("OPENAI_MODEL", "gpt-5")

# Seconds to cache each endpoint, so the dashboard polling does not hammer Odoo
CACHE_TTL = int(os.getenv("CACHE_TTL", "10"))

if not ODOO_API_KEY:
    print("WARNING: ODOO_API_KEY is missing from .env")
if not OPENAI_API_KEY:
    print("WARNING: OPENAI_API_KEY is missing from .env")


# ============================================================
# ODOO HTTP CLIENT
# ============================================================

odoo = httpx.Client(
    base_url=ODOO_URL,
    timeout=60,
    headers={
        "Authorization": f"bearer {ODOO_API_KEY}",
        "X-Odoo-Database": ODOO_DB,
        "Content-Type": "application/json",
    },
)

app = FastAPI(title="Odoo AI Dashboard", version="1.1")


def rpc(model, method, **kw):
    if not ODOO_API_KEY:
        raise RuntimeError(
            "ODOO_API_KEY is missing. Add it to .env and restart the server."
        )

    response = odoo.post(f"/json/2/{model}/{method}", json=kw)

    if response.status_code >= 400:
        raise RuntimeError(f"Odoo {response.status_code}: {response.text[:1000]}")

    return response.json()


# ============================================================
# SMALL TTL CACHE
# ============================================================

_cache = {}


def cached(key, fn):
    """Return fn() result, reusing it for CACHE_TTL seconds."""
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    value = fn()
    _cache[key] = (now, value)
    return value


def many2one_name(v):
    return v[1] if isinstance(v, (list, tuple)) and len(v) > 1 else ""


def fail(e):
    return JSONResponse({"error": str(e)}, status_code=502)


# Odoo sale.order state -> label used by the dashboard
STATE = {
    "draft": "Draft",
    "sent": "Draft",
    "sale": "Confirmed",
    "done": "Done",
    "cancel": "Cancelled",
}


# ============================================================
# DASHBOARD
# ============================================================

@app.get("/")
def index():
    index_file = BASE / "index.html"

    if not index_file.exists():
        return JSONResponse(
            {"error": "index.html not found", "expected_path": str(index_file)},
            status_code=404,
        )

    return FileResponse(index_file)


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/api/health")
def health():
    result = {
        "odoo_url": ODOO_URL,
        "db": ODOO_DB,
        "openai_key_set": bool(OPENAI_API_KEY),
        "odoo_key_set": bool(ODOO_API_KEY),
        "odoo_ok": False,
    }

    try:
        result["sample"] = rpc("res.partner", "search_count", domain=[])
        result["odoo_ok"] = True
    except Exception as e:
        result["odoo_error"] = str(e)

    return result


# ============================================================
# KPI ENDPOINT
# ============================================================

def build_kpis():
    orders = rpc(
        "sale.order",
        "search_read",
        domain=[["state", "in", ["sale", "done"]]],
        fields=["name", "amount_total", "partner_id", "date_order", "state"],
        order="date_order desc",
        limit=200,
    )

    partner_ids = list({o["partner_id"][0] for o in orders if o.get("partner_id")})

    total_revenue = sum(o.get("amount_total", 0) for o in orders)
    order_count = len(orders)

    return {
        "revenue": total_revenue,
        "orders": order_count,
        "customers": len(partner_ids),
        "avg": total_revenue / order_count if order_count else 0,
        "recent": [
            {
                "name": o.get("name"),
                "amount": o.get("amount_total", 0),
                "customer": many2one_name(o.get("partner_id")),
                "state": STATE.get(o.get("state"), "Confirmed"),
            }
            for o in orders[:15]
        ],
    }


@app.get("/api/kpis")
def kpis():
    try:
        return cached("kpis", build_kpis)
    except Exception as e:
        return fail(e)


# ============================================================
# PAGE ENDPOINTS (rows are arrays, matching the dashboard columns)
# ============================================================

# Sales -> [Order, Customer, Date, Amount, Status]
def build_sales():
    orders = rpc(
        "sale.order",
        "search_read",
        domain=[],
        fields=["name", "partner_id", "date_order", "amount_total", "state"],
        order="date_order desc",
        limit=200,
    )
    return [
        [
            o["name"],
            many2one_name(o.get("partner_id")),
            str(o.get("date_order") or "")[:10],
            o.get("amount_total", 0),
            STATE.get(o.get("state"), "Draft"),
        ]
        for o in orders
    ]


@app.get("/api/sales")
def sales():
    try:
        return cached("sales", build_sales)
    except Exception as e:
        return fail(e)


# Customers -> [Customer, Email, City, Orders, Total Spent]
# Built from confirmed orders, so it only needs fields every Odoo has.
def build_customers():
    orders = rpc(
        "sale.order",
        "search_read",
        domain=[["state", "in", ["sale", "done"]]],
        fields=["partner_id", "amount_total"],
        limit=2000,
    )

    stats = {}
    for o in orders:
        p = o.get("partner_id")
        if not p:
            continue
        s = stats.setdefault(p[0], {"name": p[1], "orders": 0, "spent": 0})
        s["orders"] += 1
        s["spent"] += o.get("amount_total", 0)

    details = {}
    if stats:
        for p in rpc(
            "res.partner", "read", ids=list(stats), fields=["email", "city"]
        ):
            details[p["id"]] = p

    rows = [
        [
            s["name"],
            details.get(pid, {}).get("email") or "",
            details.get(pid, {}).get("city") or "",
            s["orders"],
            s["spent"],
        ]
        for pid, s in stats.items()
    ]
    rows.sort(key=lambda r: r[4], reverse=True)
    return rows


@app.get("/api/customers")
def customers():
    try:
        return cached("customers", build_customers)
    except Exception as e:
        return fail(e)


# Products -> [Product, SKU, Category, Price, Stock]
def build_products():
    items = rpc(
        "product.template",
        "search_read",
        domain=[["sale_ok", "=", True]],
        fields=["name", "default_code", "categ_id", "list_price", "qty_available"],
        order="name asc",
        limit=500,
    )
    return [
        [
            p["name"],
            p.get("default_code") or "",
            many2one_name(p.get("categ_id")),
            p.get("list_price", 0),
            int(p.get("qty_available") or 0),
        ]
        for p in items
    ]


@app.get("/api/products")
def products():
    try:
        return cached("products", build_products)
    except Exception as e:
        return fail(e)


# Inventory -> [Product, SKU, On Hand, Level %, Status]
def stock_status(qty):
    return "InStock" if qty >= 20 else "Low" if qty > 0 else "Out"


def build_inventory():
    fields = ["name", "default_code", "qty_available"]

    try:
        # Odoo 18/19: storable products have is_storable = True
        items = rpc(
            "product.product",
            "search_read",
            domain=[["is_storable", "=", True]],
            fields=fields,
            limit=500,
        )
    except Exception:
        # Older versions: type = "product" means storable
        items = rpc(
            "product.product",
            "search_read",
            domain=[["type", "=", "product"]],
            fields=fields,
            limit=500,
        )

    rows = []
    for p in items:
        qty = int(p.get("qty_available") or 0)
        rows.append(
            [
                p["name"],
                p.get("default_code") or "",
                qty,
                max(0, min(100, qty)),
                stock_status(qty),
            ]
        )
    return rows


@app.get("/api/inventory")
def inventory():
    try:
        return cached("inventory", build_inventory)
    except Exception as e:
        return fail(e)


# ============================================================
# OPENAI FUNCTION TOOLS
# ============================================================

def T(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


MODEL_FIELD = {
    "type": "string",
    "description": (
        "Odoo model, for example sale.order, res.partner, "
        "account.move, product.product"
    ),
}

DOMAIN_FIELD = {
    "type": "array",
    "description": 'Odoo domain, for example [["state","=","sale"]]',
    "items": {},
}

TOOLS = [
    T(
        "search_read",
        "Read Odoo records.",
        {
            "model": MODEL_FIELD,
            "domain": DOMAIN_FIELD,
            "fields": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer"},
            "order": {"type": "string"},
        },
        ["model"],
    ),
    T(
        "count",
        "Count Odoo records.",
        {"model": MODEL_FIELD, "domain": DOMAIN_FIELD},
        ["model"],
    ),
    T(
        "fields_get",
        "List fields available on an Odoo model.",
        {"model": MODEL_FIELD},
        ["model"],
    ),
    T(
        "group_totals",
        (
            "Aggregate Odoo records. "
            "Example: groupby ['partner_id'], aggregates ['amount_total:sum']."
        ),
        {
            "model": MODEL_FIELD,
            "domain": DOMAIN_FIELD,
            "groupby": {"type": "array", "items": {"type": "string"}},
            "aggregates": {"type": "array", "items": {"type": "string"}},
        },
        ["model", "groupby", "aggregates"],
    ),
]


# ============================================================
# TOOL EXECUTION
# ============================================================

def run_tool(name, arguments):
    model = arguments.get("model")
    domain = arguments.get("domain") or []

    if name == "search_read":
        kwargs = {
            "domain": domain,
            "fields": arguments.get("fields") or [],
            "limit": min(arguments.get("limit") or 50, 200),
        }
        if arguments.get("order"):
            kwargs["order"] = arguments["order"]
        return rpc(model, "search_read", **kwargs)

    if name == "count":
        return rpc(model, "search_count", domain=domain)

    if name == "fields_get":
        return rpc(model, "fields_get", attributes=["string", "type"])

    if name == "group_totals":
        return rpc(
            model,
            "formatted_read_group",
            domain=domain,
            groupby=arguments["groupby"],
            aggregates=arguments["aggregates"],
        )

    return "Unknown tool"


# ============================================================
# AI SYSTEM PROMPT
# ============================================================

SYSTEM = """
You answer questions about the company's Odoo data.

Use the available Odoo tools to retrieve factual information.

Rules:

1. Never modify Odoo data.
2. Only read data.
3. Be concise.
4. If the user asks for a list or comparison, use a table.
5. Do not invent Odoo data.
6. If a field is unknown, use fields_get.
7. If a tool fails, try another reasonable approach.
8. Base answers on actual Odoo records retrieved through the tools.
9. Reply ONLY with valid JSON.

Required JSON format:

{
    "answer": "1-3 sentence answer",
    "table": {
        "columns": ["Column 1", "Column 2"],
        "rows": [
            ["Value 1", "Value 2"]
        ]
    }
}

If a table is not required:

{
    "answer": "1-3 sentence answer",
    "table": null
}
"""


class Chat(BaseModel):
    messages: list[dict]


# ============================================================
# AI CHAT ENDPOINT
# ============================================================

@app.post("/api/chat")
def chat(c: Chat):

    if not OPENAI_API_KEY:
        return {
            "answer": "Missing OPENAI_API_KEY. Add it to .env and restart the server.",
            "table": None,
        }

    messages = [{"role": "system", "content": SYSTEM}]
    messages.extend(c.messages[-12:])  # keep the most recent 12 messages

    try:
        final_message = None

        for _ in range(6):

            response = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                timeout=120,
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={"model": MODEL, "messages": messages, "tools": TOOLS},
            )

            if response.status_code >= 400:
                return {
                    "answer": f"OpenAI error {response.status_code}: {response.text[:500]}",
                    "table": None,
                }

            final_message = response.json()["choices"][0]["message"]
            messages.append(final_message)

            tool_calls = final_message.get("tool_calls") or []
            if not tool_calls:
                break

            for tool_call in tool_calls:
                try:
                    arguments = json.loads(tool_call["function"]["arguments"])
                    result = run_tool(tool_call["function"]["name"], arguments)
                    content = json.dumps(result, default=str)[:12000]
                except Exception as e:
                    content = f"ERROR: {str(e)}"

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": content,
                    }
                )

        text = ((final_message or {}).get("content") or "").strip()

    except Exception as e:
        return {"answer": f"Request failed: {str(e)}", "table": None}

    # Parse the model's JSON reply
    try:
        cleaned = text
        if cleaned.startswith("```json"):
            cleaned = cleaned[len("```json"):]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        return json.loads(cleaned.strip())
    except Exception:
        return {"answer": text, "table": None}


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":
    print("Dashboard: http://localhost:8000")
    print("Health:    http://localhost:8000/api/health")
    print(f"Odoo:      {ODOO_URL}")
    print(f"Database:  {ODOO_DB}")
    print(f"Odoo API key loaded:   {bool(ODOO_API_KEY)}")
    print(f"OpenAI API key loaded: {bool(OPENAI_API_KEY)}")

    uvicorn.run(app, host="127.0.0.1", port=8000)
