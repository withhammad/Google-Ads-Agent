"""
pull.py — read-only data layer.

Runs GAQL queries against ONE campaign and returns clean pandas DataFrames plus
campaign settings and month-to-date pacing context. Nothing here ever mutates the
account — this module is safe to run in any mode.

All field names and the micros/fraction handling below were verified against the
official Google Ads API v24 field reference (google-ads library v31). Key units:
  * MICROS (÷ 1_000_000): cost_micros, average_cpc, cost_per_conversion,
    cpc_bid_micros, effective_cpc_bid_micros, campaign_budget.amount_micros.
  * NOT micros: conversions, conversions_value (account currency), ctr (0..1).
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, Iterator, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# --- Bidding-strategy classification (campaign.bidding_strategy_type names) ---
# There is no single "SMART_BIDDING" enum value; we classify by name. apply.py
# uses these to decide whether a manual keyword cpc_bid mutation will take effect.
MANUAL_BID_STRATEGIES = {"MANUAL_CPC"}                 # manual bid drives the auction
ENHANCED_CPC_STRATEGIES = {"ENHANCED_CPC"}            # manual bid honored, then Google overrides
SMART_BIDDING_STRATEGIES = {                          # manual keyword bids are ignored
    "TARGET_CPA",
    "TARGET_ROAS",
    "MAXIMIZE_CONVERSIONS",
    "MAXIMIZE_CONVERSION_VALUE",
}
# Other automated strategies that also ignore keyword cpc_bid (Maximize Clicks =
# TARGET_SPEND, Target Impression Share, etc.). Treated like Smart Bidding for
# the purpose of "should we touch keyword bids?".
OTHER_AUTOMATED_STRATEGIES = {
    "TARGET_SPEND",
    "TARGET_IMPRESSION_SHARE",
    "TARGET_CPM",
    "TARGET_CPV",
    "PERCENT_CPC",
    "COMMISSION",
    "FIXED_CPM",
    "FIXED_SHARE_OF_VOICE",
}

# Stable column order for each DataFrame, so an empty result still has columns.
KEYWORD_COLUMNS = [
    "campaign_id", "campaign_name", "ad_group_id", "ad_group_name",
    "criterion_id", "keyword", "match_type", "status",
    "cpc_bid", "effective_cpc_bid",
    "impressions", "clicks", "cost", "conversions", "conversion_value",
    "ctr", "average_cpc", "cost_per_conversion",
]
SEARCH_TERM_COLUMNS = [
    "campaign_id", "campaign_name", "ad_group_id", "ad_group_name",
    "search_term", "match_type", "status",
    "impressions", "clicks", "cost", "conversions", "conversion_value",
    "ctr", "average_cpc", "cost_per_conversion",
]
AD_COLUMNS = [
    "campaign_id", "campaign_name", "ad_group_id", "ad_group_name",
    "ad_id", "ad_status", "headlines", "descriptions", "final_urls",
    "impressions", "clicks", "ctr",
]


@dataclass
class CampaignData:
    """Everything pull.py fetches for one campaign in a single run."""
    settings: Dict[str, Any]      # bidding strategy, budget, smart-bidding flags
    metrics: Dict[str, Any]       # campaign-level totals over the window
    keywords: pd.DataFrame
    search_terms: pd.DataFrame
    ads: pd.DataFrame
    pacing: Dict[str, Any]        # month-to-date spend + calendar context


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _micros(value: Optional[int]) -> float:
    """Convert a micros integer to account-currency float (None/0 -> 0.0)."""
    return (value or 0) / 1_000_000.0


def _validate_campaign_id(campaign_id: Any) -> str:
    """Return the campaign id as a bare numeric string, or raise.

    GAQL takes campaign.id as an unquoted numeric literal. Validating that it is
    all digits also prevents any GAQL injection via an untrusted id.
    """
    text = str(campaign_id).strip()
    if not text.isdigit():
        raise ValueError(f"campaign_id must be all digits, got {campaign_id!r}")
    return text


def _lookback_range(lookback_days: int) -> tuple[str, str]:
    """Inclusive [start, end] date strings ending YESTERDAY (today excluded)."""
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=lookback_days - 1)
    return start.isoformat(), end.isoformat()


def _run_query(client, customer_id: str, query: str) -> Iterator[Any]:
    """Stream GAQL rows for a query, translating API errors into clear messages.

    Uses search_stream (best for bulk pulls); the iterator is consumed inside
    this function so it never leaves the service's scope.
    """
    from google.ads.googleads.errors import GoogleAdsException

    ga_service = client.get_service("GoogleAdsService")
    try:
        stream = ga_service.search_stream(customer_id=customer_id, query=query)
        for batch in stream:
            for row in batch.results:
                yield row
    except GoogleAdsException as ex:
        details: List[str] = []
        for error in ex.failure.errors:
            loc = ""
            if error.location and error.location.field_path_elements:
                path = ".".join(
                    fpe.field_name for fpe in error.location.field_path_elements
                )
                loc = f" (on field: {path})"
            details.append(f"{error.message}{loc}")
        raise RuntimeError(
            f"Google Ads query failed [request_id={ex.request_id}, "
            f"status={ex.error.code().name}]:\n  - " + "\n  - ".join(details)
        ) from ex


# ---------------------------------------------------------------------------
# Per-keyword report (keyword_view)
# ---------------------------------------------------------------------------

def pull_keywords(client, customer_id: str, campaign_id: Any,
                  lookback_days: int = 30) -> pd.DataFrame:
    """One aggregated row per enabled positive keyword over the window."""
    cid = _validate_campaign_id(campaign_id)
    start, end = _lookback_range(lookback_days)
    query = f"""
        SELECT
          campaign.id, campaign.name, campaign.advertising_channel_type,
          ad_group.id, ad_group.name,
          ad_group_criterion.criterion_id,
          ad_group_criterion.keyword.text,
          ad_group_criterion.keyword.match_type,
          ad_group_criterion.status,
          ad_group_criterion.cpc_bid_micros,
          ad_group_criterion.effective_cpc_bid_micros,
          metrics.impressions, metrics.clicks, metrics.ctr,
          metrics.average_cpc, metrics.cost_micros,
          metrics.conversions, metrics.conversions_value,
          metrics.cost_per_conversion
        FROM keyword_view
        WHERE segments.date BETWEEN '{start}' AND '{end}'
          AND campaign.id = {cid}
          AND ad_group_criterion.type = 'KEYWORD'
          AND ad_group_criterion.status = 'ENABLED'
          AND ad_group_criterion.negative = FALSE
        ORDER BY metrics.cost_micros DESC
    """
    rows: List[Dict[str, Any]] = []
    for row in _run_query(client, customer_id, query):
        m = row.metrics
        agc = row.ad_group_criterion
        rows.append({
            "campaign_id": row.campaign.id,
            "campaign_name": row.campaign.name,
            "ad_group_id": row.ad_group.id,
            "ad_group_name": row.ad_group.name,
            "criterion_id": agc.criterion_id,
            "keyword": agc.keyword.text,
            "match_type": agc.keyword.match_type.name,
            "status": agc.status.name,
            "cpc_bid": _micros(agc.cpc_bid_micros),                  # 0.0 = unset
            "effective_cpc_bid": _micros(agc.effective_cpc_bid_micros),
            "impressions": m.impressions,
            "clicks": m.clicks,
            "cost": _micros(m.cost_micros),
            "conversions": m.conversions,                            # NOT micros
            "conversion_value": m.conversions_value,                 # NOT micros
            "ctr": m.ctr,                                            # 0..1 fraction
            "average_cpc": _micros(m.average_cpc),
            "cost_per_conversion": _micros(m.cost_per_conversion),
        })
    logger.info("Pulled %d keyword rows (%s..%s)", len(rows), start, end)
    return pd.DataFrame(rows, columns=KEYWORD_COLUMNS)


# ---------------------------------------------------------------------------
# Per-search-term report (search_term_view)
# ---------------------------------------------------------------------------

def pull_search_terms(client, customer_id: str, campaign_id: Any,
                      lookback_days: int = 30) -> pd.DataFrame:
    """One aggregated row per search term over the window.

    Deliberately omits segments.keyword.* so Dynamic Search Ads / Performance Max
    search terms are NOT dropped — the negatives rule must see all wasted spend.
    """
    cid = _validate_campaign_id(campaign_id)
    start, end = _lookback_range(lookback_days)
    query = f"""
        SELECT
          search_term_view.search_term, search_term_view.status,
          segments.search_term_match_type,
          campaign.id, campaign.name, ad_group.id, ad_group.name,
          metrics.impressions, metrics.clicks, metrics.cost_micros,
          metrics.conversions, metrics.conversions_value,
          metrics.ctr, metrics.average_cpc, metrics.cost_per_conversion
        FROM search_term_view
        WHERE segments.date BETWEEN '{start}' AND '{end}'
          AND campaign.id = {cid}
        ORDER BY metrics.cost_micros DESC
    """
    rows: List[Dict[str, Any]] = []
    for row in _run_query(client, customer_id, query):
        m = row.metrics
        rows.append({
            "campaign_id": row.campaign.id,
            "campaign_name": row.campaign.name,
            "ad_group_id": row.ad_group.id,
            "ad_group_name": row.ad_group.name,
            "search_term": row.search_term_view.search_term,
            "match_type": row.segments.search_term_match_type.name,
            "status": row.search_term_view.status.name,
            "impressions": m.impressions,
            "clicks": m.clicks,
            "cost": _micros(m.cost_micros),
            "conversions": m.conversions,
            "conversion_value": m.conversions_value,
            "ctr": m.ctr,
            "average_cpc": _micros(m.average_cpc),
            "cost_per_conversion": _micros(m.cost_per_conversion),
        })
    logger.info("Pulled %d search-term rows (%s..%s)", len(rows), start, end)
    return pd.DataFrame(rows, columns=SEARCH_TERM_COLUMNS)


# ---------------------------------------------------------------------------
# Per-RSA report (ad_group_ad) — for the low-CTR flag
# ---------------------------------------------------------------------------

def pull_ads(client, customer_id: str, campaign_id: Any,
             lookback_days: int = 30) -> pd.DataFrame:
    """One aggregated row per responsive search ad over the window.

    Aggregated (no segments.date) so each row's metrics.ctr is the correct
    per-ad CTR for the window. rules.py derives the ad-group average from these
    rows as Σclicks / Σimpressions (never an average of per-row ratios).
    """
    cid = _validate_campaign_id(campaign_id)
    start, end = _lookback_range(lookback_days)
    query = f"""
        SELECT
          campaign.id, campaign.name, ad_group.id, ad_group.name,
          ad_group_ad.ad.id, ad_group_ad.status,
          ad_group_ad.ad.final_urls,
          ad_group_ad.ad.responsive_search_ad.headlines,
          ad_group_ad.ad.responsive_search_ad.descriptions,
          metrics.impressions, metrics.clicks, metrics.ctr
        FROM ad_group_ad
        WHERE segments.date BETWEEN '{start}' AND '{end}'
          AND campaign.id = {cid}
          AND ad_group_ad.ad.type = RESPONSIVE_SEARCH_AD
          AND ad_group_ad.status != 'REMOVED'
        ORDER BY ad_group.id, ad_group_ad.ad.id
    """
    rows: List[Dict[str, Any]] = []
    for row in _run_query(client, customer_id, query):
        m = row.metrics
        rsa = row.ad_group_ad.ad.responsive_search_ad
        rows.append({
            "campaign_id": row.campaign.id,
            "campaign_name": row.campaign.name,
            "ad_group_id": row.ad_group.id,
            "ad_group_name": row.ad_group.name,
            "ad_id": row.ad_group_ad.ad.id,
            "ad_status": row.ad_group_ad.status.name,
            "headlines": " | ".join(a.text for a in rsa.headlines),
            "descriptions": " | ".join(a.text for a in rsa.descriptions),
            "final_urls": ", ".join(row.ad_group_ad.ad.final_urls),
            "impressions": m.impressions,
            "clicks": m.clicks,
            "ctr": m.ctr,
        })
    logger.info("Pulled %d RSA rows (%s..%s)", len(rows), start, end)
    return pd.DataFrame(rows, columns=AD_COLUMNS)


# ---------------------------------------------------------------------------
# Campaign settings (bidding strategy + budget) and pacing
# ---------------------------------------------------------------------------

def pull_campaign_settings(client, customer_id: str,
                           campaign_id: Any) -> Dict[str, Any]:
    """Campaign name/status, bidding strategy, daily budget + derived flags."""
    cid = _validate_campaign_id(campaign_id)
    query = f"""
        SELECT
          campaign.id, campaign.name, campaign.status,
          campaign.bidding_strategy_type,
          campaign_budget.amount_micros, campaign_budget.period
        FROM campaign
        WHERE campaign.id = {cid}
    """
    for row in _run_query(client, customer_id, query):
        strategy = row.campaign.bidding_strategy_type.name
        return {
            "campaign_id": row.campaign.id,
            "campaign_name": row.campaign.name,
            "status": row.campaign.status.name,
            "bidding_strategy_type": strategy,
            "daily_budget": _micros(row.campaign_budget.amount_micros),
            "budget_period": row.campaign_budget.period.name,
            # Manual keyword cpc_bid mutations only take effect for MANUAL_CPC
            # (partially for ENHANCED_CPC). apply.py gates bid writes on this.
            "manual_bids_supported": strategy in MANUAL_BID_STRATEGIES,
            "enhanced_cpc": strategy in ENHANCED_CPC_STRATEGIES,
            "smart_bidding": strategy in SMART_BIDDING_STRATEGIES,
            "bids_ignored": strategy in (
                SMART_BIDDING_STRATEGIES | OTHER_AUTOMATED_STRATEGIES
            ),
        }
    raise RuntimeError(
        f"Campaign {cid} not found (or not accessible to customer {customer_id}). "
        "Check the id and that it belongs to the operating account."
    )


def pull_campaign_metrics(client, customer_id: str, campaign_id: Any,
                          lookback_days: int = 30) -> Dict[str, Any]:
    """True campaign-level totals over the window (for the report header)."""
    cid = _validate_campaign_id(campaign_id)
    start, end = _lookback_range(lookback_days)
    query = f"""
        SELECT metrics.impressions, metrics.clicks, metrics.cost_micros,
               metrics.conversions, metrics.conversions_value
        FROM campaign
        WHERE campaign.id = {cid}
          AND segments.date BETWEEN '{start}' AND '{end}'
    """
    impressions = clicks = 0
    cost = conversions = conversion_value = 0.0
    for row in _run_query(client, customer_id, query):
        m = row.metrics
        impressions += m.impressions
        clicks += m.clicks
        cost += _micros(m.cost_micros)
        conversions += m.conversions
        conversion_value += m.conversions_value
    return {
        "window_start": start,
        "window_end": end,
        "lookback_days": lookback_days,
        "impressions": impressions,
        "clicks": clicks,
        "cost": cost,
        "conversions": conversions,
        "conversion_value": conversion_value,
        "ctr": (clicks / impressions) if impressions else 0.0,
        "avg_cpc": (cost / clicks) if clicks else 0.0,
        "cpa": (cost / conversions) if conversions else None,
        "roas": (conversion_value / cost) if cost else None,
    }


def pull_pacing(client, customer_id: str, campaign_id: Any) -> Dict[str, Any]:
    """Month-to-date spend + calendar context for the budget pacing rule.

    Sums spend from the 1st of the current month through YESTERDAY (completed
    days only), so the projection isn't skewed by today's partial spend.
    """
    cid = _validate_campaign_id(campaign_id)
    today = date.today()
    first = today.replace(day=1)
    end = today - timedelta(days=1)
    days_in_month = calendar.monthrange(today.year, today.month)[1]

    # On the 1st there are no completed days yet — can't project.
    if end < first:
        return {
            "mtd_spend": 0.0, "days_elapsed": 0,
            "days_in_month": days_in_month, "as_of": end.isoformat(),
        }

    query = f"""
        SELECT metrics.cost_micros
        FROM campaign
        WHERE campaign.id = {cid}
          AND segments.date BETWEEN '{first.isoformat()}' AND '{end.isoformat()}'
    """
    spend = sum(_micros(row.metrics.cost_micros)
                for row in _run_query(client, customer_id, query))
    return {
        "mtd_spend": spend,
        "days_elapsed": end.day,          # day-of-month == completed days
        "days_in_month": days_in_month,
        "as_of": end.isoformat(),
    }


# ---------------------------------------------------------------------------
# Convenience: pull everything for a run
# ---------------------------------------------------------------------------

def pull_all(client, customer_id: str, campaign_id: Any,
             lookback_days: int = 30) -> CampaignData:
    """Fetch settings + keywords + search terms + ads + pacing in one call."""
    settings = pull_campaign_settings(client, customer_id, campaign_id)
    logger.info(
        "Campaign %s (%s): strategy=%s, daily_budget=%.2f",
        settings["campaign_id"], settings["campaign_name"],
        settings["bidding_strategy_type"], settings["daily_budget"],
    )
    return CampaignData(
        settings=settings,
        metrics=pull_campaign_metrics(client, customer_id, campaign_id, lookback_days),
        keywords=pull_keywords(client, customer_id, campaign_id, lookback_days),
        search_terms=pull_search_terms(client, customer_id, campaign_id, lookback_days),
        ads=pull_ads(client, customer_id, campaign_id, lookback_days),
        pacing=pull_pacing(client, customer_id, campaign_id),
    )
