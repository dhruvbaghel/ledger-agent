
import os
import re
import json
import requests
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, date
from zoneinfo import ZoneInfo
from functools import lru_cache
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Acme Ledger Agent")

ROOT_URL = os.environ.get("LEDGER_ROOT_URL", "").strip()
TZ = ZoneInfo("Asia/Kolkata")
session = requests.Session()
session.headers["Accept"] = "application/json"


def money(value):
    return Decimal(str(value)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def parse_date(value):
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ).date()


@lru_cache(maxsize=1)
def load_data():
    if not ROOT_URL:
        raise RuntimeError("Set the LEDGER_ROOT_URL environment variable.")

    root = session.get(ROOT_URL, timeout=10)
    root.raise_for_status()
    info = root.json()
    links = info["links"]

    # Export is NDJSON: one order per line.
    response = session.get(links["export"], timeout=30)
    response.raise_for_status()
    orders = [
        json.loads(line)
        for line in response.text.splitlines()
        if line.strip()
    ]

    # Keep the latest version of each order ID.
    current = {}

    for order in orders:
        oid = order["id"]

        updated = datetime.fromisoformat(
            order["updated_at"].replace("Z", "+00:00")
        )

        # Normalize timestamps before comparing them.
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=TZ)

        updated = updated.astimezone(ZoneInfo("UTC"))

        old = current.get(oid)

        if old is None or updated > old["_updated_utc"]:
            current[oid] = {
                **order,
                "_updated_utc": updated,
            }

# Remove the internal comparison field.
    orders = [
        {k: v for k, v in order.items() if k != "_updated_utc"}
        for order in current.values()
    ]

    rates_response = session.get(links["rates"], timeout=10)
    rates_response.raise_for_status()
    rates = rates_response.json()["usd_per_unit"]

    return list(current.values()), rates


class Question(BaseModel):
    question: str


MONTHS = {
    "january": 1, "february": 2, "march": 3,
    "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
}


def interpret(question):
    q = question.lower()
    filters = {}

    # Month + year filter.
    for name, month in MONTHS.items():
        match = re.search(rf"\b{name}\s+(20\d{{2}})\b", q)
        if match:
            year = int(match.group(1))
            filters["start"] = date(year, month, 1)
            filters["end"] = (
                date(year + 1, 1, 1) if month == 12
                else date(year, month + 1, 1)
            )
            break

    # Year-only filter.
    if "start" not in filters:
        match = re.search(r"\b(20\d{2})\b", q)
        if match:
            year = int(match.group(1))
            filters["start"] = date(year, 1, 1)
            filters["end"] = date(year + 1, 1, 1)

    # Region filter.
    for region in ["North", "South", "East", "West", "Central"]:
        if re.search(rf"\b{region.lower()}\b", q):
            filters["region"] = region
            break

    # Discover product names from the data.
    orders, _ = load_data()
    products = sorted(
        {o["product"] for o in orders if o.get("product")},
        key=len,
        reverse=True,
    )
    for product in products:
        if product.lower() in q:
            filters["product"] = product
            break

    # Customer ID filter.
    match = re.search(r"\bC\d+\b", question, re.I)
    if match:
        filters["customer"] = match.group().upper()

    return q, filters


def calculate(question):
    q, filters = interpret(question)
    orders, rates = load_data()

    # Determine what the question asks.
    if re.search(r"\b(refund|refunded|refunds)\b", q):
        metric = "refund"
    elif re.search(
        r"\b(which product|top product|best-selling|bestselling)\b", q
    ):
        metric = "top_product"
    elif re.search(
        r"\b(how many|number of|count|different customers|"
        r"distinct customers|unique customers)\b", q
    ):
        metric = "count"
    else:
        metric = "revenue"

    # Requested output currency.
    currency = "USD"
    for code in rates:
        if re.search(rf"\b{re.escape(code.lower())}\b", q):
            currency = code
            break

    selected = []

    for o in orders:
        status = str(o.get("status", "")).lower()

        # Apply the appropriate status filter.
        if metric in ("revenue", "top_product"):
            if status != "paid":
                continue

        elif metric == "refund":
            if status != "refunded":
                continue

        elif metric == "count":
            # Apply status restrictions stated in the question.
            if re.search(r"\bpaid\b", q) and status != "paid":
                continue
            if re.search(r"\brefunded\b", q) and status != "refunded":
                continue
            if re.search(r"\bvoid\b", q) and status != "void":
                continue

        if (
            filters.get("region")
            and str(o.get("region", "")).lower()
            != filters["region"].lower()
        ):
            continue

        if (
            filters.get("product")
            and str(o.get("product", "")).lower()
            != filters["product"].lower()
        ):
            continue

        if (
            filters.get("customer")
            and str(o.get("customer", "")).upper()
            != filters["customer"]
        ):
            continue

        try:
            created = parse_date(o.get("created_at"))
        except (ValueError, TypeError):
            continue

        if filters.get("start") and (
            created is None or created < filters["start"]
        ):
            continue

        if filters.get("end") and (
            created is None or created >= filters["end"]
        ):
            continue

        selected.append(o)

    # Count distinct customers, not order rows.
    if metric == "count":
        if re.search(
            r"\b(different|distinct|unique)\s+customers?\b", q
        ):
            return len({
                str(o["customer"]).strip().upper()
                for o in selected
                if o.get("customer") is not None
                and str(o["customer"]).strip()
            })

        # Ordinary order count.
        return len(selected)

    if metric == "top_product":
        totals = {}

        for o in selected:
            source_currency = o["currency"]
            if source_currency not in rates:
                continue

            value = (
                Decimal(str(o["amount"]))
                * Decimal(str(rates[source_currency]))
            )
            product = o["product"]
            totals[product] = totals.get(
                product, Decimal("0")
            ) + value

        if not totals:
            return None

        return sorted(
            totals.items(),
            key=lambda item: (-item[1], item[0]),
        )[0][0]

    # Revenue/refund total, converted to requested currency.
    total_usd = Decimal("0")

    for o in selected:
        source_currency = o["currency"]
        if source_currency not in rates:
            continue

        total_usd += (
            Decimal(str(o["amount"]))
            * Decimal(str(rates[source_currency]))
        )

    answer = total_usd / Decimal(str(rates[currency]))
    return float(money(answer))


@app.post("/")
def answer_question(body: Question):
    try:
        result = calculate(body.question.strip())
        return {"answer": result}
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail="Ledger API request failed",
        ) from exc
    except (KeyError, ValueError, RuntimeError) as exc:
        raise HTTPException(
            status_code=500,
            detail=str(exc),
        ) from exc




