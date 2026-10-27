"""GoodFoods MCP server: exposes the reservation domain as Model Context Protocol tools.

Run standalone (stdio transport):   python -m goodfoods.mcp_server
Any MCP client (our agent, Claude Desktop, MCP Inspector) can discover and call these tools.
Tool descriptions are written for a *small* model: short, imperative, with valid values inline.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Callable

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from goodfoods import store
from goodfoods.data.seed import FEATURES

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("goodfoods.mcp")

mcp = FastMCP("goodfoods-reservations")

CUISINE_HELP = f"Cuisine. One of: {', '.join(store.CUISINES)}."
AREA_HELP = f"Bengaluru neighborhood. One of: {', '.join(store.AREAS)}."
FEATURE_HELP = f"Required amenities, any of: {', '.join(FEATURES)}."
DATE_HELP = ("Date. For relative dates pass the guest's own words, e.g. 'next friday', 'this saturday', 'kal', "
             "'this weekend': the server resolves them. Otherwise YYYY-MM-DD.")
TIME_HELP = "Time in 24h HH:MM, e.g. 20:00 for 8 pm."


def _safe(fn: Callable[..., dict], **kwargs: Any) -> dict:
    """Never let an exception escape as a protocol error; return a structured, recoverable result."""
    try:
        return fn(**kwargs)
    except store.ToolError as e:
        return e.to_dict()
    except Exception:  # pragma: no cover - unexpected bug: log it, give the model something safe
        log.exception("tool %s failed", fn.__name__)
        return {"ok": False, "error_code": "INTERNAL_ERROR",
                "message": "Something went wrong on our side.", "hint": "Apologise and ask the guest to try again."}


@mcp.tool()
def search_restaurants(
    cuisine: Annotated[str | None, Field(description=CUISINE_HELP)] = None,
    neighborhood: Annotated[str | None, Field(description=AREA_HELP)] = None,
    max_cost_for_two: Annotated[int | None, Field(description="Max average cost for two in INR.")] = None,
    min_rating: Annotated[float | None, Field(description="Minimum rating out of 5.")] = None,
    features: Annotated[list[str] | None, Field(description=FEATURE_HELP)] = None,
    pure_veg: Annotated[bool | None, Field(description="True to only show pure-vegetarian outlets.")] = None,
    query: Annotated[str | None, Field(description="Free text such as a dish name, e.g. 'biryani'.")] = None,
    limit: Annotated[int, Field(description="Max results (1-10).")] = 5,
) -> dict:
    """Find GoodFoods outlets by filters. Use when the guest asks what is available or names a place."""
    return _safe(store.search_restaurants, cuisine=cuisine, neighborhood=neighborhood,
                 max_cost_for_two=max_cost_for_two, min_rating=min_rating, features=features,
                 pure_veg=pure_veg, query=query, limit=limit)


@mcp.tool()
def recommend_restaurants(
    party_size: Annotated[int | None, Field(description="Number of guests.")] = None,
    date: Annotated[str | None, Field(description=DATE_HELP)] = None,
    time: Annotated[str | None, Field(description=TIME_HELP)] = None,
    cuisine: Annotated[str | None, Field(description=CUISINE_HELP)] = None,
    neighborhood: Annotated[str | None, Field(description=AREA_HELP)] = None,
    budget_per_person: Annotated[int | None, Field(description="Budget per person in INR.")] = None,
    occasion: Annotated[str | None, Field(description="e.g. date, anniversary, birthday, business, family, casual.")] = None,
    features: Annotated[list[str] | None, Field(description=FEATURE_HELP)] = None,
    pure_veg: Annotated[bool | None, Field(description="True if the guest needs a pure-veg outlet.")] = None,
    dish: Annotated[str | None, Field(description="A dish the guest asked for, e.g. 'biryani', 'sushi', 'dosa'.")] = None,
    limit: Annotated[int, Field(description="How many suggestions (1-5).")] = 3,
) -> dict:
    """Suggest the best outlets. Pass only what the guest said. If they name a dish, pass dish and NOT cuisine.
    Always pass date, time and party_size when known: then only outlets with a free table are returned."""
    return _safe(store.recommend_restaurants, party_size=party_size, date_=date, time_=time, cuisine=cuisine,
                 neighborhood=neighborhood, budget_per_person=budget_per_person, occasion=occasion,
                 features=features, pure_veg=pure_veg, dish=dish, limit=limit)


@mcp.tool()
def get_restaurant_details(
    restaurant_id: Annotated[str, Field(description="restaurant_id like GF012, from a search result.")],
) -> dict:
    """Get address, opening hours, phone and signature dishes of one outlet. Does NOT check tables."""
    return _safe(store.get_restaurant_details, restaurant_id=restaurant_id)


@mcp.tool()
def check_availability(
    restaurant_id: Annotated[str, Field(description="restaurant_id from a tool result (e.g. GF012), or the outlet name exactly as the guest said it.")],
    date: Annotated[str, Field(description=DATE_HELP)],
    time: Annotated[str, Field(description=TIME_HELP)],
    party_size: Annotated[int, Field(description="Number of guests.")],
) -> dict:
    """Check if a table is free. The ONLY way to know availability: call it before asking for name and phone.
    If not free, returns nearby free times and similar outlets free at that time."""
    return _safe(store.check_availability, restaurant_id=restaurant_id, date_=date, time_=time,
                 party_size=party_size)


@mcp.tool()
def create_reservation(
    restaurant_id: Annotated[str, Field(description="restaurant_id from a tool result (e.g. GF012), or the outlet name exactly as the guest said it.")],
    date: Annotated[str, Field(description=DATE_HELP)],
    time: Annotated[str, Field(description=TIME_HELP)],
    party_size: Annotated[int, Field(description="Number of guests.")],
    guest_name: Annotated[str, Field(description="Name for the booking.")],
    phone: Annotated[str, Field(description="Guest's 10-digit Indian mobile number.")],
    user_confirmed: Annotated[bool, Field(description="true ONLY after the guest said yes to the read-back summary.")],
    special_requests: Annotated[str | None, Field(description="Optional notes, e.g. window seat, cake.")] = None,
) -> dict:
    """Book a table. Call only after reading all details back and the guest explicitly confirms."""
    return _safe(store.create_reservation, restaurant_id=restaurant_id, date_=date, time_=time,
                 party_size=party_size, guest_name=guest_name, phone=phone,
                 special_requests=special_requests, user_confirmed=user_confirmed)


@mcp.tool()
def get_reservation(
    confirmation_code: Annotated[str, Field(description="Code like GF-7K2M9Q.")],
    phone: Annotated[str, Field(description="Phone number used for the booking.")],
) -> dict:
    """Look up an existing booking. Needs both the confirmation code and the phone number."""
    return _safe(store.get_reservation, confirmation_code=confirmation_code, phone=phone)


@mcp.tool()
def modify_reservation(
    confirmation_code: Annotated[str, Field(description="Code like GF-7K2M9Q.")],
    phone: Annotated[str, Field(description="Phone number used for the booking.")],
    user_confirmed: Annotated[bool, Field(description="true ONLY after the guest said yes to the change summary.")],
    new_date: Annotated[str | None, Field(description=DATE_HELP)] = None,
    new_time: Annotated[str | None, Field(description=TIME_HELP)] = None,
    new_party_size: Annotated[int | None, Field(description="New number of guests.")] = None,
    special_requests: Annotated[str | None, Field(description="Updated notes.")] = None,
) -> dict:
    """Change date, time, party size or notes of a booking at the same outlet."""
    return _safe(store.modify_reservation, confirmation_code=confirmation_code, phone=phone, new_date=new_date,
                 new_time=new_time, new_party_size=new_party_size, special_requests=special_requests,
                 user_confirmed=user_confirmed)


@mcp.tool()
def cancel_reservation(
    confirmation_code: Annotated[str, Field(description="Code like GF-7K2M9Q.")],
    phone: Annotated[str, Field(description="Phone number used for the booking.")],
    user_confirmed: Annotated[bool, Field(description="true ONLY after the guest confirmed they want to cancel.")],
) -> dict:
    """Cancel a booking."""
    return _safe(store.cancel_reservation, confirmation_code=confirmation_code, phone=phone,
                 user_confirmed=user_confirmed)


def main() -> None:
    from goodfoods.data.seed import seed

    seed()  # idempotent: builds the DB on first run
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
