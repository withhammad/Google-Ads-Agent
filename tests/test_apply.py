"""Unit tests for the apply.py safety gates (no live client; client=None)."""

from src import apply
from src.rules import ADJUST_BID, FLAG_BUDGET, PAUSE_KEYWORD, Action


def _pause(cid=123, kw="k"):
    return Action(PAUSE_KEYWORD, cid, f"Pause {kw}", "0 conv", True,
                  {"ad_group_id": 1, "criterion_id": 2, "keyword": kw,
                   "field": "status", "before": "ENABLED", "after": "PAUSED"})


def _bid(cid=123, before=1_000_000, after=1_100_000):
    return Action(ADJUST_BID, cid, "bid", "cpa", True,
                  {"ad_group_id": 1, "criterion_id": 3, "keyword": "k",
                   "field": "cpc_bid_micros", "before": before, "after": after,
                   "direction": "raise"})


def test_dry_run_makes_no_calls(cfg):
    r = apply.apply_actions(None, "123", [_pause()], cfg, apply=False)
    assert r.dry_run and not r.aborted
    assert len(r.planned) == 1 and r.applied == []


def test_allowlist_refuses_other_campaign(cfg):
    r = apply.apply_actions(None, "123", [_pause(123), _pause(999)], cfg, apply=False)
    assert len(r.planned) == 1
    assert len(r.refused) == 1 and r.refused[0][0].campaign_id == 999


def test_max_changes_aborts(cfg):
    cfg["safety"]["max_changes_per_run"] = 2
    r = apply.apply_actions(None, "123",
                            [_pause(123, "a"), _pause(123, "b"), _pause(123, "c")],
                            cfg, apply=False)
    assert r.aborted and "MAX_CHANGES_PER_RUN" in r.abort_reason
    assert r.applied == []


def test_bid_cap_refuses_oversized_move(cfg):
    r = apply.apply_actions(None, "123", [_bid(after=2_000_000)], cfg, apply=False)
    assert r.planned == [] and len(r.refused) == 1
    assert "MAX_BID_CHANGE_PCT" in r.refused[0][1]


def test_bid_within_cap_is_planned(cfg):
    r = apply.apply_actions(None, "123", [_bid(after=1_100_000)], cfg, apply=False)
    assert len(r.planned) == 1 and r.refused == []


def test_empty_allowlist_disables_writes(cfg):
    cfg["safety"]["allowed_campaign_ids"] = []
    r = apply.apply_actions(None, "123", [_pause()], cfg, apply=False)
    assert r.planned == [] and len(r.refused) == 1


def test_recommend_only_is_separated(cfg):
    flag = Action(FLAG_BUDGET, 123, "pacing", "x", False, {"resource": "campaign"})
    r = apply.apply_actions(None, "123", [_pause(), flag], cfg, apply=False)
    assert len(r.planned) == 1 and len(r.recommend_only) == 1


def test_customer_id_dashes_are_normalized(cfg):
    # allow-list uses campaign ids; customer id with dashes must not break the run
    r = apply.apply_actions(None, "111-222-3333", [_pause()], cfg, apply=False)
    assert not r.aborted and len(r.planned) == 1
