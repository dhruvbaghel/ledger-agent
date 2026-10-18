

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
    response = session.get(links["export"], timeout=20)
    response.raise_for_status()
    orders = [
        json.loads(line)
        for line in response.text.splitlines()
        if line.strip()
    ]

    # Latest updated_at record is the current record for an order ID.
    current = {}
    for order in orders:
        oid = order["id"]
        old = current.get(oid)
        if old is None or order["updated_at"] > old["updated_at"]:
            current[oid] = order

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

    # Date filter: month + year.
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

    # Explicit year alone.
    if "start" not in filters:
        match = re.search(r"\b(20\d{2})\b", q)
        if match:
            year = int(match.group(1))
            filters["start"] = date(year, 1, 1)
            filters["end"] = date(year + 1, 1, 1)

    for region in ["North", "South", "East", "West", "Central"]:
        if re.search(rf"\b{region.lower()}\b", q):
            filters["region"] = region
            break

    # Product names are discovered from the actual data.
    orders, _ = load_data()
    products = sorted(
        {o["product"] for o in orders},
        key=len,
        reverse=True
    )
    for product in products:
        if product.lower() in q:
            filters["product"] = product
            break

    match = re.search(r"\bC\d+\b", question, re.I)
    if match:
        filters["customer"] = match.group().upper()

    return q, filters


def calculate(question):
    q, filters = interpret(question)
    orders, rates = load_data()

    # Determine metric.
    if any(word in q for word in ["refund", "refunded"]):
        metric = "refund"
    elif any(word in q for word in ["how many", "number of", "count"]):
        metric = "count"
    elif any(word in q for word in ["which product", "top product",
                                    "best-selling", "bestselling"]):
        metric = "top_product"
    else:
        metric = "revenue"

    currency = "USD"
    for code in rates:
        if re.search(rf"\b{code.lower()}\b", q):
            currency = code
            break

    selected = []
    for o in orders:
        if metric == "refund":
            if o["status"].lower() != "refunded":
                continue
        elif metric == "revenue" or metric == "top_product":
            if o["status"].lower() != "paid":
                continue

        if filters.get("region") and o["region"].lower() != filters["region"].lower():
            continue
        if filters.get("product") and o["product"] != filters["product"]:
            continue
        if filters.get("customer") and o["customer"].upper() != filters["customer"]:
            continue

        try:
            created = parse_date(o["created_at"])
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

    if metric == "count":
        return len(selected)

    if metric == "top_product":
        totals = {}
        for o in selected:
            value = (
                Decimal(str(o["amount"]))
                * Decimal(str(rates[o["currency"]]))
            )
            totals[o["product"]] = totals.get(
                o["product"], Decimal("0")
            ) + value

        if not totals:
            return None
        return sorted(
            totals.items(), key=lambda x: (-x[1], x[0])
        )[0][0]

    total_usd = sum(
        (
            Decimal(str(o["amount"]))
            * Decimal(str(rates[o["currency"]]))
            for o in selected
        ),
        Decimal("0"),
    )

    # Rates specify USD per unit of the source currency.
    answer = total_usd / Decimal(str(rates[currency]))
    return float(money(answer))


@app.post("/")
def answer_question(body: Question):
    try:
        result = calculate(body.question.strip())
        return {"answer": result}
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502, detail="Ledger API request failed"
        ) from exc
    except (KeyError, ValueError, RuntimeError) as exc:
        raise HTTPException(
            status_code=500, detail=str(exc)
        ) from exc
