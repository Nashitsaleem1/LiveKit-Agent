"""Mock of the internal company API (read-only) for local development.

Run from the brief-agent folder:
    uv run python mocks/internal_api.py

Serves fake but stable data for "today" on http://127.0.0.1:8081. Only GET routes exist,
so any write attempt gets 405 Method Not Allowed. Requests need the bearer token from
INTERNAL_API_TOKEN in .env.local.
"""

import datetime as dt
import os
import random

from aiohttp import web
from dotenv import load_dotenv

load_dotenv(".env.local")

TOKEN = os.environ.get("INTERNAL_API_TOKEN", "")
PORT = int(os.environ.get("MOCK_INTERNAL_API_PORT", "8081"))

CUSTOMERS = [
    "Brightwell Foods",
    "Kestrel Outdoor",
    "Novaline Electronics",
    "Greenleaf Pharmacy",
    "Summit Home Goods",
]
ROUTES = [
    "Dallas to Houston",
    "Houston to San Antonio",
    "Dallas to Oklahoma City",
    "Phoenix to Tucson",
    "Dallas to Phoenix",
]
DELAY_REASONS = [
    "heavy traffic on Interstate 45",
    "late pickup at the customer dock",
    "truck maintenance issue",
    "storm warning on the route",
    "missing paperwork at loading",
]
TICKET_SUBJECTS = [
    "Shipment marked delivered but not received",
    "Invoice amount does not match quote",
    "Harbor Track portal login fails",
    "Request to reschedule pickup",
    "Damaged pallet on arrival",
    "Cold storage temperature alert question",
]


def _rng(salt: str) -> random.Random:
    # Same numbers all day, new numbers tomorrow
    return random.Random(f"{dt.date.today().isoformat()}-{salt}")


def _sales(period: str) -> dict:
    r = _rng(f"sales-{period}")
    days = {"yesterday": 1, "week": 7, "month": 30}[period]
    revenue = round(days * r.uniform(125_000, 155_000), -2)
    target = days * 140_000
    top = r.sample(CUSTOMERS, 3)
    return {
        "period": period,
        "revenue_usd": revenue,
        "target_usd": target,
        "percent_of_target": round(100 * revenue / target, 1),
        "change_vs_previous_period_percent": round(r.uniform(-6, 12), 1),
        "top_customers_by_revenue": top,
    }


def _tickets() -> list[dict]:
    r = _rng("tickets")
    tickets = []
    for i in range(r.randint(9, 16)):
        tickets.append(
            {
                "id": f"T-{4100 + i * 7}",
                "customer": r.choice(CUSTOMERS),
                "subject": r.choice(TICKET_SUBJECTS),
                "priority": r.choices(["high", "medium", "low"], [2, 4, 4])[0],
                "age_hours": r.randint(1, 40),
                "status": "open",
            }
        )
    return tickets


def _delayed() -> list[dict]:
    r = _rng("delayed")
    return [
        {
            "shipment_id": f"HP-{r.randint(20000, 29999)}",
            "customer": r.choice(CUSTOMERS),
            "route": r.choice(ROUTES),
            "delay_hours": round(r.uniform(0.5, 6), 1),
            "reason": r.choice(DELAY_REASONS),
        }
        for _ in range(r.randint(2, 5))
    ]


def _meetings() -> list[dict]:
    return [
        {"time": "09:00", "title": "Leadership weekly sync", "with": "Maya Chen, Daniel Okafor, Priya Raman"},
        {"time": "11:30", "title": "Brightwell Foods quarterly review", "with": "Tom Becker, Brightwell account team"},
        {"time": "14:00", "title": "Harbor Track version 2 planning", "with": "Marcus Hill, Lena Novak"},
        {"time": "16:30", "title": "Houston cold storage capacity check", "with": "Aisha Brooks"},
    ]  # fmt: skip


@web.middleware
async def auth(request: web.Request, handler):
    if request.headers.get("Authorization") != f"Bearer {TOKEN}" or not TOKEN:
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


async def daily_brief(_: web.Request) -> web.Response:
    r = _rng("brief")
    tickets = _tickets()
    delayed = _delayed()
    yesterday = _sales("yesterday")
    return web.json_response(
        {
            "date": dt.date.today().isoformat(),
            "revenue_yesterday_usd": yesterday["revenue_usd"],
            "revenue_percent_of_daily_target": yesterday["percent_of_target"],
            "shipments_delivered_yesterday": r.randint(310, 380),
            "on_time_rate_percent_yesterday": round(r.uniform(93.0, 98.5), 1),
            "on_time_target_percent": 96,
            "open_support_tickets": len(tickets),
            "open_high_priority_tickets": sum(t["priority"] == "high" for t in tickets),
            "delayed_shipments_now": len(delayed),
            "incidents": [
                r.choice(
                    [
                        "Houston cold storage unit 3 had a 20 minute temperature alert overnight, resolved.",
                        "One Dallas forklift out of service until Thursday.",
                        "No safety incidents reported in the last 24 hours.",
                    ]
                )
            ],
            "first_meeting_today": _meetings()[0],
        }
    )


async def sales_summary(request: web.Request) -> web.Response:
    period = request.query.get("period", "yesterday")
    if period not in ("yesterday", "week", "month"):
        return web.json_response(
            {"error": "period must be yesterday, week or month"}, status=400
        )
    return web.json_response(_sales(period))


async def support_tickets(request: web.Request) -> web.Response:
    priority = request.query.get("priority", "all")
    tickets = _tickets()
    if priority != "all":
        tickets = [t for t in tickets if t["priority"] == priority]
    return web.json_response({"count": len(tickets), "tickets": tickets})


async def delayed_shipments(_: web.Request) -> web.Response:
    delayed = _delayed()
    return web.json_response({"count": len(delayed), "shipments": delayed})


async def calendar_today(_: web.Request) -> web.Response:
    return web.json_response(
        {"date": dt.date.today().isoformat(), "meetings": _meetings()}
    )


def make_app() -> web.Application:
    app = web.Application(middlewares=[auth])
    app.router.add_get("/api/v1/brief/daily", daily_brief)
    app.router.add_get("/api/v1/sales/summary", sales_summary)
    app.router.add_get("/api/v1/support/tickets", support_tickets)
    app.router.add_get("/api/v1/shipments/delayed", delayed_shipments)
    app.router.add_get("/api/v1/calendar/today", calendar_today)
    return app


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set INTERNAL_API_TOKEN in .env.local first")
    web.run_app(make_app(), host="127.0.0.1", port=PORT)
