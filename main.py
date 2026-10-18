
import os
import re
import json
import requests
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, date, timezone
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


def parse_timestamp(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)

    return dt.astimezone(timezone.utc)


@lru_cache(maxsize=1)
def load_data():
    if not ROOT_URL:
        raise RuntimeError(
            "Set the LEDGER_ROOT_URL environment variable."
        )

    root = session.get(ROOT_URL, timeout=15)
    root.raise_for_status()
    info = root.json()
    links = info["links"]

    # Export is NDJSON: one order per line.
    response = session.get(links["export"], timeout=45)
    response.raise_for_status()

    raw_orders = [
        json.loads(line)
        for line in response.text.splitlines()
        if line.strip()
    ]

    # Keep the latest version of every order ID.
    current = {}

    for order in raw_orders:
        oid = order["id"]
        updated = parse_timestamp(order["updated_at"])

        old = current.get(oid)

        if old is None or updated > old[0]:
            current[oid] = (updated, order)

    # Return clean order records without internal fields.
    orders = [
        order
        for _, order in current.values()
    ]

    rates_response = session.get(links["rates"], timeout=15)
    rates_response.raise_for_status()
    rates = rates_response.json()["usd_per_unit"]

    return orders, rates


class Question(BaseModel):
    question: str


MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def interpret(question):
    q = question.lower()
    filters = {}

    # Month and year.
    for name, month in MONTHS.items():
        match = re.search(rf"\b{name}\s+(20\d{{2}})\b", q)

        if match:
            year = int(match.group(1))
            filters["start"] = date(year, month, 1)
            filters["end"] = (
                date(year + 1, 1, 1)
                if month == 12
                else date(year, month + 1, 1)
            )
            break

    # Year only.
    if "start" not in filters:
        match = re.search(r"\b(20\d{2})\b", q)

        if match:
            year = int(match.group(1))
            filters["start"] = date(year, 1, 1)
            filters["end"] = date(year + 1, 1, 1)

    # Region.
    for region in ["North", "South", "East", "West", "Central"]:
        if re.search(rf"\b{region.lower()}\b", q):
            filters["region"] = region
            break

    # Product names from actual ledger data.
    orders, _ = load_data()
    products = sorted(
        {
            str(o["product"]).strip()
            for o in orders
            if o.get("product")
        },
        key=len,
        reverse=True,
    )

    for product in products:
        if product.casefold() in q:
            filters["product"] = product
            break

    # Customer ID.
    match = re.search(r"\bC\d+\b", question, re.I)

    if match:
        filters["customer"] = match.group().upper()

    return q, filters


def calculate(question):
    q, filters = interpret(question)
    orders, rates = load_data()

    # Determine the requested metric.
    if re.search(r"\b(refund|refunded|refunds)\b", q):
        metric = "refund"

    elif re.search(
        r"\b(which product|top product|best-selling|bestselling)\b",
        q,
    ):
        metric = "top_product"

    elif re.search(
        r"\b(average|avg|mean)\b", q
    ):
        metric = "average"

    elif re.search(
        r"\b(how many|number of|count|different customers|"
        r"distinct customers|unique customers)\b",
        q,
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
        status = str(o.get("status", "")).strip().lower()

        # Revenue, average order value and top product use paid orders.
        if metric in ("revenue", "top_product", "average"):
            if status != "paid":
                continue

        elif metric == "refund":
            if status != "refunded":
                continue

        elif metric == "count":
            if re.search(r"\bpaid\b", q) and status != "paid":
                continue

            if re.search(r"\brefunded\b", q) and status != "refunded":
                continue

            if re.search(r"\bvoid\b", q) and status != "void":
                continue

        # Apply region filter.
        if filters.get("region"):
            if str(o.get("region", "")).strip().casefold() != (
                filters["region"].casefold()
            ):
                continue

        # Apply product filter.
        if filters.get("product"):
            if str(o.get("product", "")).strip().casefold() != (
                filters["product"].casefold()
            ):
                continue

        # Apply customer filter.
        if filters.get("customer"):
            if str(o.get("customer", "")).strip().upper() != (
                filters["customer"]
            ):
                continue

        # Apply date filters using Asia/Kolkata business dates.
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

    # Count unique customers or order records.
    if metric == "count":
        if re.search(
            r"\b(different|distinct|unique)\s+customers?\b",
            q,
        ):
            return len({
                str(o["customer"]).strip().upper()
                for o in selected
                if o.get("customer") is not None
                and str(o["customer"]).strip()
            })

        return len(selected)

    # Find the product with the greatest paid sales value in USD.
    if metric == "top_product":
        totals = {}

        for o in selected:
            source_currency = o.get("currency")

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

    # Calculate total value in USD first.
    total_usd = Decimal("0")
    valid_orders = 0

    for o in selected:
        source_currency = o.get("currency")

        if source_currency not in rates:
            continue

        total_usd += (
            Decimal(str(o["amount"]))
            * Decimal(str(rates[source_currency]))
        )
        valid_orders += 1

    # Average order value in USD.
    if metric == "average":
        if valid_orders == 0:
            return None

        return float(money(total_usd / valid_orders))

    # Convert total from USD to the requested currency.
    if currency not in rates:
        raise ValueError("Unsupported currency.")

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



