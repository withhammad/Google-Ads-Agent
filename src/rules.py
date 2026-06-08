"""
rules.py — the optimization rules engine.

Pure decision logic: it takes the DataFrames + settings from pull.py and returns
a list of typed Action objects. It performs NO API calls and NO mutations, so it
is fully unit-testable offline.

Five rules (thresholds in config.yaml):
  1. PAUSE_KEYWORD   cost >= min_spend AND conversions == 0          (auto-appliable)
  2. ADD_NEGATIVE    clicks >= min_clicks AND conversions == 0       (auto-appliable)
  3. ADJUST_BID      vs target_cpa, clamped by max_bid_change_pct    (gated)
  4. FLAG_BUDGET     projected month-end spend vs monthly_budget     (recommend-only)
  5. FLAG_LOW_CTR_AD ad CTR vs ad-group average                      (recommend-only)

Rule 3 is auto-appliable ONLY when automation.enable_auto_bids is true AND the
campaign's bidding strategy actually honors manual keyword bids (MANUAL_CPC).
Under Smart Bidding the bid actions are still surfaced as recommendations, but
flagged as no-ops. Rules 4 & 5 are always recommend-only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List

import pandas as pd

logger = logging.getLogger(__name__)

# --- Action type constants ---
PAUSE_KEYWORD = "PAUSE_KEYWORD"
ADD_NEGATIVE = "ADD_NEGATIVE"
ADJUST_BID = "ADJUST_BID"
FLAG_BUDGET = "FLAG_BUDGET"
FLAG_LOW_CTR_AD = "FLAG_LOW_CTR_AD"

# Minimum bid floor so a clamped decrease can never drive a bid to ~0.
_MIN_BID_MICROS = 10_000  # 0.01 in account currency


@dataclass
class Action:
    """One recommendation. `params` is the machine payload apply.py executes and
    the change log reverses; before/after live there for ADJUST/PAUSE."""
    action_type: str
    campaign_id: int          # for the apply.py allow-list guard
    summary: str              # one-line checklist text for the report
    reason: str               # numeric justification
    auto_appliable: bool      # may apply.py execute this under --apply?
    params: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _cfg(cfg: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Safely read a nested config value, returning default if any key missing."""
    node: Any = cfg
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _bid_change(base: float, direction: str, step_pct: float,
                cap_pct: float) -> tuple[int, float]:
    """Return (new_bid_micros, effective_pct). Move size clamped to cap_pct."""
    eff = min(step_pct, cap_pct)
    factor = 1 + eff / 100.0 if direction == "raise" else 1 - eff / 100.0
    new_micros = max(int(round(base * factor * 1_000_000)), _MIN_BID_MICROS)
    return new_micros, eff


# ---------------------------------------------------------------------------
# Rule 1 — PAUSE_KEYWORD
# ---------------------------------------------------------------------------

def rule_pause_keywords(keywords: pd.DataFrame, cfg: Dict[str, Any]) -> List[Action]:
    min_spend = _cfg(cfg, "rules", "pause_keyword", "min_spend", default=75.0)
    actions: List[Action] = []
    for row in keywords.itertuples(index=False):
        if row.cost >= min_spend and row.conversions == 0:
            actions.append(Action(
                action_type=PAUSE_KEYWORD,
                campaign_id=int(row.campaign_id),
                summary=f'Pause keyword "{row.keyword}" ({row.match_type})',
                reason=(f"spent {row.cost:.2f} over the window with 0 conversions "
                        f"({row.clicks} clicks)"),
                auto_appliable=True,
                params={
                    "resource": "ad_group_criterion",
                    "ad_group_id": int(row.ad_group_id),
                    "criterion_id": int(row.criterion_id),
                    "keyword": row.keyword,
                    "field": "status",
                    "before": row.status,
                    "after": "PAUSED",
                },
            ))
    return actions


# ---------------------------------------------------------------------------
# Rule 2 — ADD_NEGATIVE
# ---------------------------------------------------------------------------

def rule_add_negatives(search_terms: pd.DataFrame, cfg: Dict[str, Any]) -> List[Action]:
    min_clicks = _cfg(cfg, "rules", "add_negative", "min_clicks", default=20)
    actions: List[Action] = []
    for row in search_terms.itertuples(index=False):
        # Skip terms already excluded as negatives.
        if row.status == "EXCLUDED":
            continue
        if row.clicks >= min_clicks and row.conversions == 0:
            actions.append(Action(
                action_type=ADD_NEGATIVE,
                campaign_id=int(row.campaign_id),
                summary=f'Add negative "{row.search_term}" (EXACT)',
                reason=(f"{row.clicks} clicks, 0 conversions, "
                        f"{row.cost:.2f} spent over the window"),
                auto_appliable=True,
                params={
                    "resource": "campaign_criterion",
                    "campaign_id": int(row.campaign_id),
                    "keyword": row.search_term,
                    "match_type": "EXACT",   # precise, low-risk: blocks only this term
                    "field": "negative_keyword",
                    "before": None,
                    "after": "added",
                },
            ))
    return actions


# ---------------------------------------------------------------------------
# Rule 3 — ADJUST_BID
# ---------------------------------------------------------------------------

def rule_adjust_bids(keywords: pd.DataFrame, settings: Dict[str, Any],
                     cfg: Dict[str, Any]) -> List[Action]:
    target_cpa = _cfg(cfg, "rules", "adjust_bid", "target_cpa", default=25.0)
    min_conversions = _cfg(cfg, "rules", "adjust_bid", "min_conversions", default=1)
    cpa_tolerance = _cfg(cfg, "rules", "adjust_bid", "cpa_tolerance", default=1.30)
    min_clicks_lower = _cfg(cfg, "rules", "adjust_bid", "min_clicks_to_lower", default=30)
    step_pct = _cfg(cfg, "rules", "adjust_bid", "bid_step_pct", default=10)
    cap_pct = _cfg(cfg, "safety", "max_bid_change_pct", default=15)

    enable_auto_bids = bool(_cfg(cfg, "automation", "enable_auto_bids", default=False))
    manual_ok = bool(settings.get("manual_bids_supported", False))
    # Only safe to auto-apply when enabled AND the strategy honors manual bids.
    auto = enable_auto_bids and manual_ok
    smart_note = "" if manual_ok else (
        f" [recommendation only — {settings.get('bidding_strategy_type')} "
        "ignores manual keyword bids]"
    )

    actions: List[Action] = []
    for row in keywords.itertuples(index=False):
        if row.conversions <= 0:
            continue  # no CPA signal — wasted-spend pausing is Rule 1's job
        base = row.cpc_bid if row.cpc_bid > 0 else row.effective_cpc_bid
        if base <= 0:
            continue  # no known bid to move (fully automated / inherited 0)
        cpa = row.cost_per_conversion

        direction = None
        if cpa < target_cpa and row.conversions >= min_conversions:
            direction = "raise"
        elif cpa > target_cpa * cpa_tolerance and row.clicks >= min_clicks_lower:
            direction = "lower"
        if direction is None:
            continue

        new_micros, eff = _bid_change(base, direction, step_pct, cap_pct)
        old_micros = int(round(base * 1_000_000))
        if new_micros == old_micros:
            continue
        sign = "+" if direction == "raise" else "-"
        actions.append(Action(
            action_type=ADJUST_BID,
            campaign_id=int(row.campaign_id),
            summary=(f'{direction.capitalize()} bid on "{row.keyword}" {sign}{eff:.0f}% '
                     f"({base:.2f} -> {new_micros / 1_000_000:.2f}){smart_note}"),
            reason=(f"CPA {cpa:.2f} vs target {target_cpa:.2f} "
                    f"({row.conversions:.1f} conv, {row.clicks} clicks)"),
            auto_appliable=auto,
            params={
                "resource": "ad_group_criterion",
                "ad_group_id": int(row.ad_group_id),
                "criterion_id": int(row.criterion_id),
                "keyword": row.keyword,
                "field": "cpc_bid_micros",
                "before": old_micros,
                "after": new_micros,
                "direction": direction,
            },
        ))
    return actions


# ---------------------------------------------------------------------------
# Rule 4 — FLAG_BUDGET
# ---------------------------------------------------------------------------

def rule_flag_budget(pacing: Dict[str, Any], settings: Dict[str, Any],
                     cfg: Dict[str, Any]) -> List[Action]:
    days_elapsed = pacing.get("days_elapsed", 0)
    if days_elapsed <= 0:
        return []  # too early in the month to project
    monthly_budget = _cfg(cfg, "rules", "budget", "monthly_budget", default=0.0)
    if monthly_budget <= 0:
        return []
    overpace = _cfg(cfg, "rules", "budget", "overpace_pct", default=110)
    underpace = _cfg(cfg, "rules", "budget", "underpace_pct", default=80)

    mtd = pacing["mtd_spend"]
    projected = mtd / days_elapsed * pacing["days_in_month"]
    pct = projected / monthly_budget * 100

    direction = None
    if pct >= overpace:
        direction = "OVER"
    elif pct <= underpace:
        direction = "UNDER"
    if direction is None:
        return []

    return [Action(
        action_type=FLAG_BUDGET,
        campaign_id=int(settings["campaign_id"]),
        summary=f"Budget pacing {direction}: projected {projected:.0f} vs budget {monthly_budget:.0f}",
        reason=(f"{mtd:.0f} spent in {days_elapsed} days -> projected month-end "
                f"{projected:.0f} ({pct:.0f}% of {monthly_budget:.0f})"),
        auto_appliable=False,
        params={"resource": "campaign", "projected_spend": round(projected, 2),
                "monthly_budget": monthly_budget, "pct_of_budget": round(pct, 1)},
    )]


# ---------------------------------------------------------------------------
# Rule 5 — FLAG_LOW_CTR_AD
# ---------------------------------------------------------------------------

def rule_flag_low_ctr_ads(ads: pd.DataFrame, cfg: Dict[str, Any]) -> List[Action]:
    if ads.empty:
        return []
    min_impr = _cfg(cfg, "rules", "low_ctr_ad", "min_impressions", default=500)
    gap_pct = _cfg(cfg, "rules", "low_ctr_ad", "ctr_gap_pct", default=30)

    # Ad-group average CTR = Σclicks / Σimpressions (never average of ratios).
    grouped = ads.groupby("ad_group_id")[["clicks", "impressions"]].sum()
    avg_ctr = {
        gid: (r.clicks / r.impressions if r.impressions > 0 else 0.0)
        for gid, r in grouped.iterrows()
    }

    actions: List[Action] = []
    for row in ads.itertuples(index=False):
        if row.impressions < min_impr:
            continue
        group_avg = avg_ctr.get(row.ad_group_id, 0.0)
        if group_avg <= 0:
            continue
        threshold = group_avg * (1 - gap_pct / 100.0)
        if row.ctr < threshold:
            actions.append(Action(
                action_type=FLAG_LOW_CTR_AD,
                campaign_id=int(row.campaign_id),
                summary=(f'Low-CTR ad {row.ad_id} in "{row.ad_group_name}" '
                         f"(CTR {row.ctr * 100:.2f}% vs group {group_avg * 100:.2f}%)"),
                reason=(f"ad CTR is >{gap_pct}% below its ad-group average over "
                        f"{row.impressions} impressions — consider a creative refresh"),
                auto_appliable=False,
                params={"resource": "ad_group_ad", "ad_group_id": int(row.ad_group_id),
                        "ad_id": int(row.ad_id), "ad_ctr": round(row.ctr, 4),
                        "group_avg_ctr": round(group_avg, 4)},
            ))
    return actions


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def evaluate(data, cfg: Dict[str, Any]) -> List[Action]:
    """Run all five rules over a CampaignData bundle and return every action.

    `data` is a pull.CampaignData (settings, keywords, search_terms, ads, pacing).
    """
    actions: List[Action] = []
    actions += rule_pause_keywords(data.keywords, cfg)
    actions += rule_add_negatives(data.search_terms, cfg)
    actions += rule_adjust_bids(data.keywords, data.settings, cfg)
    actions += rule_flag_budget(data.pacing, data.settings, cfg)
    actions += rule_flag_low_ctr_ads(data.ads, cfg)

    auto = sum(1 for a in actions if a.auto_appliable)
    logger.info("Rules produced %d actions (%d auto-appliable, %d recommend-only)",
                len(actions), auto, len(actions) - auto)
    return actions
