"""Unit tests for the five rules in rules.py."""

from src import rules


# --- Rule 1: PAUSE_KEYWORD ---

def test_pause_fires_on_spend_no_conv(cfg, kw, make_keywords):
    df = make_keywords([kw(keyword="waste", cost=90.0, conversions=0.0, clicks=40)])
    acts = rules.rule_pause_keywords(df, cfg)
    assert len(acts) == 1
    assert acts[0].action_type == rules.PAUSE_KEYWORD
    assert acts[0].auto_appliable is True
    assert acts[0].params["after"] == "PAUSED"


def test_pause_skips_below_min_spend(cfg, kw, make_keywords):
    df = make_keywords([kw(cost=10.0, conversions=0.0)])
    assert rules.rule_pause_keywords(df, cfg) == []


def test_pause_skips_when_converting(cfg, kw, make_keywords):
    df = make_keywords([kw(cost=300.0, conversions=3.0)])
    assert rules.rule_pause_keywords(df, cfg) == []


# --- Rule 2: ADD_NEGATIVE ---

def test_negative_fires(cfg, st, make_search_terms):
    df = make_search_terms([st(search_term="free", clicks=25, conversions=0.0)])
    acts = rules.rule_add_negatives(df, cfg)
    assert len(acts) == 1
    assert acts[0].action_type == rules.ADD_NEGATIVE
    assert acts[0].params["match_type"] == "EXACT"
    assert acts[0].auto_appliable is True


def test_negative_skips_excluded(cfg, st, make_search_terms):
    df = make_search_terms([st(clicks=50, conversions=0.0, status="EXCLUDED")])
    assert rules.rule_add_negatives(df, cfg) == []


def test_negative_skips_below_min_clicks(cfg, st, make_search_terms):
    df = make_search_terms([st(clicks=5, conversions=0.0)])
    assert rules.rule_add_negatives(df, cfg) == []


# --- Rule 3: ADJUST_BID ---

def _manual():
    return dict(manual_bids_supported=True, bidding_strategy_type="MANUAL_CPC")


def test_bid_raise_clamped(cfg, kw, make_keywords):
    cfg["automation"]["enable_auto_bids"] = True
    df = make_keywords([kw(cpc_bid=1.0, conversions=4.0, clicks=20,
                           cost_per_conversion=12.5)])
    acts = rules.rule_adjust_bids(df, _manual(), cfg)
    assert len(acts) == 1
    a = acts[0]
    assert a.params["direction"] == "raise"
    assert a.params["after"] == 1_100_000  # +10%, under the 15% cap
    assert a.auto_appliable is True


def test_bid_lower(cfg, kw, make_keywords):
    df = make_keywords([kw(cpc_bid=2.0, conversions=2.0, clicks=50,
                           cost_per_conversion=100.0)])
    acts = rules.rule_adjust_bids(df, _manual(), cfg)
    assert acts[0].params["direction"] == "lower"
    assert acts[0].params["after"] == 1_800_000  # -10%


def test_bid_skips_when_no_base_bid(cfg, kw, make_keywords):
    df = make_keywords([kw(cpc_bid=0.0, effective_cpc_bid=0.0, conversions=2.0,
                           cost_per_conversion=10.0)])
    assert rules.rule_adjust_bids(df, _manual(), cfg) == []


def test_bid_skips_zero_conversions(cfg, kw, make_keywords):
    df = make_keywords([kw(cpc_bid=1.0, conversions=0.0, clicks=40,
                           cost_per_conversion=0.0)])
    assert rules.rule_adjust_bids(df, _manual(), cfg) == []


def test_bid_recommend_only_under_smart_bidding(cfg, kw, make_keywords):
    cfg["automation"]["enable_auto_bids"] = True
    settings = dict(manual_bids_supported=False, bidding_strategy_type="TARGET_CPA")
    df = make_keywords([kw(cpc_bid=1.0, conversions=4.0, clicks=20,
                           cost_per_conversion=12.5)])
    acts = rules.rule_adjust_bids(df, settings, cfg)
    assert len(acts) == 1
    assert acts[0].auto_appliable is False
    assert "recommendation only" in acts[0].summary


# --- Rule 4: FLAG_BUDGET ---

def test_budget_over(cfg):
    acts = rules.rule_flag_budget(
        dict(mtd_spend=2000.0, days_elapsed=10, days_in_month=30),
        dict(campaign_id=123), cfg)
    assert len(acts) == 1 and "OVER" in acts[0].summary
    assert acts[0].auto_appliable is False


def test_budget_under(cfg):
    acts = rules.rule_flag_budget(
        dict(mtd_spend=100.0, days_elapsed=10, days_in_month=30),
        dict(campaign_id=123), cfg)
    assert len(acts) == 1 and "UNDER" in acts[0].summary


def test_budget_skips_on_day_one(cfg):
    acts = rules.rule_flag_budget(
        dict(mtd_spend=0.0, days_elapsed=0, days_in_month=30),
        dict(campaign_id=123), cfg)
    assert acts == []


# --- Rule 5: FLAG_LOW_CTR_AD ---

def test_low_ctr_fires(cfg, ad, make_ads):
    df = make_ads([ad(ad_id=1, impressions=1000, clicks=50, ctr=0.05),
                   ad(ad_id=2, impressions=1000, clicks=10, ctr=0.01)])
    acts = rules.rule_flag_low_ctr_ads(df, cfg)
    # group avg = 60/2000 = 0.03; threshold 0.021; ad 2 (0.01) is below
    assert len(acts) == 1 and acts[0].params["ad_id"] == 2


def test_low_ctr_skips_low_impressions(cfg, ad, make_ads):
    df = make_ads([ad(ad_id=1, impressions=1000, clicks=50, ctr=0.05),
                   ad(ad_id=2, impressions=100, clicks=1, ctr=0.01)])
    assert rules.rule_flag_low_ctr_ads(df, cfg) == []


# --- evaluate orchestration ---

def test_evaluate_returns_list(cfg, make_data):
    acts = rules.evaluate(make_data(), cfg)
    assert isinstance(acts, list)
