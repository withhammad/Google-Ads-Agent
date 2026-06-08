"""Shared pytest fixtures: a default config and DataFrame/CampaignData factories."""

import pandas as pd
import pytest

from src.pull import (AD_COLUMNS, KEYWORD_COLUMNS, SEARCH_TERM_COLUMNS,
                      CampaignData)


@pytest.fixture
def cfg():
    """A fresh copy of the default config for each test (function-scoped)."""
    return {
        "safety": {"max_changes_per_run": 25, "max_bid_change_pct": 15,
                   "allowed_campaign_ids": ["123"]},
        "data": {"lookback_days": 30},
        "rules": {
            "pause_keyword": {"min_spend": 75.0},
            "add_negative": {"min_clicks": 20},
            "adjust_bid": {"target_cpa": 25.0, "min_conversions": 1,
                           "cpa_tolerance": 1.30, "min_clicks_to_lower": 30,
                           "bid_step_pct": 10},
            "budget": {"monthly_budget": 3000.0, "overpace_pct": 110,
                       "underpace_pct": 80},
            "low_ctr_ad": {"min_impressions": 500, "ctr_gap_pct": 30},
        },
        "automation": {"enable_auto_bids": False},
        "reporting": {"output_dir": "reports", "currency": "AED"},
    }


@pytest.fixture
def kw():
    def _make(**over):
        base = dict(campaign_id=123, campaign_name="C", ad_group_id=100,
                    ad_group_name="AG1", criterion_id=1, keyword="kw",
                    match_type="EXACT", status="ENABLED", cpc_bid=1.0,
                    effective_cpc_bid=1.0, impressions=1000, clicks=10, cost=10.0,
                    conversions=1.0, conversion_value=50.0, ctr=0.01,
                    average_cpc=1.0, cost_per_conversion=10.0)
        base.update(over)
        return base
    return _make


@pytest.fixture
def st():
    def _make(**over):
        base = dict(campaign_id=123, campaign_name="C", ad_group_id=100,
                    ad_group_name="AG1", search_term="term", match_type="BROAD",
                    status="NONE", impressions=900, clicks=10, cost=10.0,
                    conversions=0.0, conversion_value=0.0, ctr=0.011,
                    average_cpc=1.0, cost_per_conversion=0.0)
        base.update(over)
        return base
    return _make


@pytest.fixture
def ad():
    def _make(**over):
        base = dict(campaign_id=123, campaign_name="C", ad_group_id=100,
                    ad_group_name="AG1", ad_id=1, ad_status="ENABLED",
                    headlines="H", descriptions="D", final_urls="u",
                    impressions=1000, clicks=30, ctr=0.03)
        base.update(over)
        return base
    return _make


@pytest.fixture
def make_keywords():
    return lambda rows: pd.DataFrame(rows, columns=KEYWORD_COLUMNS)


@pytest.fixture
def make_search_terms():
    return lambda rows: pd.DataFrame(rows, columns=SEARCH_TERM_COLUMNS)


@pytest.fixture
def make_ads():
    return lambda rows: pd.DataFrame(rows, columns=AD_COLUMNS)


@pytest.fixture
def make_data(make_keywords, make_search_terms, make_ads):
    def _make(settings=None, metrics=None, pacing=None,
              keywords=None, search_terms=None, ads=None):
        return CampaignData(
            settings=settings or dict(
                campaign_id=123, campaign_name="C",
                bidding_strategy_type="MANUAL_CPC", manual_bids_supported=True,
                enhanced_cpc=False, smart_bidding=False, bids_ignored=False,
                daily_budget=100.0, status="ENABLED", budget_period="DAILY"),
            metrics=metrics or dict(
                window_start="2026-05-09", window_end="2026-06-07",
                lookback_days=30, impressions=10000, clicks=300, cost=600.0,
                conversions=20.0, conversion_value=2000.0, ctr=0.03,
                avg_cpc=2.0, cpa=30.0, roas=3.33),
            keywords=keywords if keywords is not None else make_keywords([]),
            search_terms=search_terms if search_terms is not None else make_search_terms([]),
            ads=ads if ads is not None else make_ads([]),
            pacing=pacing or dict(mtd_spend=1000.0, days_elapsed=10,
                                  days_in_month=30, as_of="2026-06-07"),
        )
    return _make
