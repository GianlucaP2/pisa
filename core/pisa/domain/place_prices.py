"""Prices and free monthly allowances of the place services, one versioned table.

Version 2026-10-21. Checked that day against:
- Google: https://developers.google.com/maps/billing-and-pricing/pricing (global list, first paid tier). The
  page itself was not reachable from the build machine; the numbers come from search results quoting it. The
  operator re-checks them with the live run.
- Apple: https://developer.apple.com/documentation/applemapsserverapi (free with the Developer Program, 25,000
  service calls a day shared with MapKit JS: counted, never billed).
- Tavily: https://docs.tavily.com/documentation/api-credits (1,000 free credits a month; pay as you go
  $0.008 a credit; a basic search is 1 credit, a basic extract 1 credit per 5 pages extracted).

The cost of a month is, per SKU: max(0, units - free) * usd_per_1000 / 1000, in EUR at the stored ECB rate.
"""

from dataclasses import dataclass
from decimal import Decimal

# prices as read on 2026-10-21; Apple Maps Server API allows 25,000 calls a day
GOOGLE_SOURCE = "https://developers.google.com/maps/billing-and-pricing/pricing"
APPLE_SOURCE = "https://developer.apple.com/documentation/applemapsserverapi"
TAVILY_SOURCE = "https://docs.tavily.com/documentation/api-credits"


@dataclass(frozen=True)
class SkuPrice:
    provider: str
    sku: str
    unit: str  # what one unit is: a call, a route-matrix element, a web credit
    free_per_month: int | None  # None = no paid tier at all (always free)
    usd_per_1000: Decimal
    source: str


_SKUS = (
    # Apple Maps Server API: free (a daily quota, no bill).
    SkuPrice("apple", "apple.token", "call", None, Decimal(0), APPLE_SOURCE),
    SkuPrice("apple", "apple.etas", "call", None, Decimal(0), APPLE_SOURCE),
    SkuPrice("apple", "apple.search", "call", None, Decimal(0), APPLE_SOURCE),
    SkuPrice("apple", "apple.place", "call", None, Decimal(0), APPLE_SOURCE),
    SkuPrice("apple", "apple.geocode", "call", None, Decimal(0), APPLE_SOURCE),
    # Google Places API (New): billed at the most expensive field asked for (the field mask).
    # Text Search Essentials (IDs only): unlimited and free.
    SkuPrice("google", "google.text_search_ids", "call", None, Decimal(0), GOOGLE_SOURCE),
    SkuPrice("google", "google.text_search_pro", "call", 5_000, Decimal("32.00"), GOOGLE_SOURCE),
    # Rating, price, open now and the attributes in a search (map_search); checked 2026-10-23.
    SkuPrice(
        "google", "google.text_search_enterprise_atmosphere", "call", 1_000, Decimal("40.00"), GOOGLE_SOURCE
    ),
    SkuPrice("google", "google.place_details_enterprise", "call", 1_000, Decimal("20.00"), GOOGLE_SOURCE),
    # A photo's short-lived URL (getMedia); checked 2026-10-23.
    SkuPrice("google", "google.place_photo", "call", 1_000, Decimal("7.00"), GOOGLE_SOURCE),
    SkuPrice(
        "google", "google.place_details_enterprise_atmosphere", "call", 1_000, Decimal("25.00"), GOOGLE_SOURCE
    ),
    # Google Routes API, Compute Route Matrix: billed per element (origins x destinations).
    SkuPrice("google", "google.route_matrix_essentials", "element", 10_000, Decimal("5.00"), GOOGLE_SOURCE),
    # Pro = TRAFFIC_AWARE routing (car).
    SkuPrice("google", "google.route_matrix_pro", "element", 5_000, Decimal("10.00"), GOOGLE_SOURCE),
    # Tavily: credits.
    SkuPrice("tavily", "tavily.credit", "credit", 1_000, Decimal("8.00"), TAVILY_SOURCE),
)
PRICES: dict[str, SkuPrice] = {p.sku: p for p in _SKUS}


def month_cost_usd(sku: str, units: int) -> Decimal:
    """The cost of `units` used in one month (the free allowance first)."""
    price = PRICES[sku]
    if price.free_per_month is None:
        return Decimal(0)
    paid = max(0, units - price.free_per_month)
    return price.usd_per_1000 * paid / 1000


def extra_cost_usd(sku: str, used: int, units: int) -> Decimal:
    """What `units` more cost after `used` this month: 0 while inside the free allowance."""
    return month_cost_usd(sku, used + units) - month_cost_usd(sku, used)
