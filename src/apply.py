"""
apply.py — guarded mutations and the undo log.

This is the ONLY module that writes to the account, so every safety guardrail
converges here:

  * apply=False (the default everywhere) makes ZERO API calls — pure planning.
  * ALLOWED_CAMPAIGN_IDS: any action targeting a campaign not on the list is
    refused. An empty list disables all writes.
  * MAX_BID_CHANGE_PCT: bid moves are re-checked here (defense-in-depth) even
    though rules.py already clamps them.
  * MAX_CHANGES_PER_RUN: if the plan exceeds the cap, the whole run ABORTS and
    nothing is written.
  * validate_only=True pre-flight: under --apply, every op is validated at the
    API first; if any fails validation, the run aborts before writing anything.
  * Every applied change is logged to logs/changes_{date}.json with before/after
    (and the created resource_name) so --undo can reverse it.

Only PAUSE_KEYWORD, ADD_NEGATIVE and ADJUST_BID are mutating; FLAG_* actions are
recommend-only and never reach the API.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .rules import ADD_NEGATIVE, ADJUST_BID, PAUSE_KEYWORD, Action

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------

@dataclass
class ApplyResult:
    dry_run: bool
    aborted: bool = False
    abort_reason: str = ""
    applied: List[Dict[str, Any]] = field(default_factory=list)        # log entries written
    planned: List[Action] = field(default_factory=list)               # mutating actions in scope
    recommend_only: List[Action] = field(default_factory=list)        # FLAG_* + gated bids
    refused: List[Tuple[Action, str]] = field(default_factory=list)   # (action, why)
    errors: List[Tuple[Action, str]] = field(default_factory=list)    # (action, message)
    logfile: Optional[str] = None


@dataclass
class UndoResult:
    dry_run: bool
    reverted: List[Dict[str, Any]] = field(default_factory=list)
    planned: List[Dict[str, Any]] = field(default_factory=list)
    skipped: List[Tuple[Dict[str, Any], str]] = field(default_factory=list)
    errors: List[Tuple[Dict[str, Any], str]] = field(default_factory=list)
    logfile: Optional[str] = None


# ---------------------------------------------------------------------------
# Config / path helpers
# ---------------------------------------------------------------------------

def _cfg(cfg: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    node: Any = cfg
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _norm_id(value: Any) -> str:
    return str(value).replace("-", "").replace(" ", "").strip()


def _allowed_ids(cfg: Dict[str, Any]) -> set:
    return {_norm_id(x) for x in (_cfg(cfg, "safety", "allowed_campaign_ids", default=[]) or [])}


def _log_dir(project_root: Optional[Path] = None) -> Path:
    d = (project_root or PROJECT_ROOT) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_log(entries: List[Dict[str, Any]], project_root: Optional[Path] = None) -> Path:
    """Append entries to logs/changes_{today}.json (creating/merging as needed)."""
    path = _log_dir(project_root) / f"changes_{date.today().isoformat()}.json"
    existing: List[Dict[str, Any]] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text() or "[]")
        except json.JSONDecodeError:
            logger.warning("Existing log %s is corrupt; starting a new list", path)
    existing.extend(entries)
    path.write_text(json.dumps(existing, indent=2))
    return path


def _fmt_error(exc: Exception) -> str:
    """Render a GoogleAdsException compactly; fall back to str() otherwise."""
    try:
        from google.ads.googleads.errors import GoogleAdsException
        if isinstance(exc, GoogleAdsException):
            msgs = "; ".join(e.message for e in exc.failure.errors)
            return f"[{exc.error.code().name}] {msgs} (request_id={exc.request_id})"
    except Exception:
        pass
    return str(exc)


# ---------------------------------------------------------------------------
# Operation builders (verified google-ads v24 patterns)
# ---------------------------------------------------------------------------

def _build_status_op(client, customer_id: str, ad_group_id: int,
                     criterion_id: int, status_name: str):
    """AdGroupCriterion UPDATE setting status (PAUSED / ENABLED) + update_mask."""
    from google.api_core import protobuf_helpers
    service = client.get_service("AdGroupCriterionService")
    op = client.get_type("AdGroupCriterionOperation")
    crit = op.update
    crit.resource_name = service.ad_group_criterion_path(
        customer_id, ad_group_id, criterion_id
    )
    crit.status = getattr(client.enums.AdGroupCriterionStatusEnum, status_name)
    client.copy_from(op.update_mask, protobuf_helpers.field_mask(None, crit._pb))
    return op


def _build_bid_op(client, customer_id: str, ad_group_id: int,
                  criterion_id: int, cpc_bid_micros: int):
    """AdGroupCriterion UPDATE setting cpc_bid_micros + update_mask."""
    from google.api_core import protobuf_helpers
    service = client.get_service("AdGroupCriterionService")
    op = client.get_type("AdGroupCriterionOperation")
    crit = op.update
    crit.resource_name = service.ad_group_criterion_path(
        customer_id, ad_group_id, criterion_id
    )
    crit.cpc_bid_micros = int(cpc_bid_micros)
    client.copy_from(op.update_mask, protobuf_helpers.field_mask(None, crit._pb))
    return op


def _build_negative_op(client, customer_id: str, campaign_id: int,
                       keyword_text: str, match_type: str):
    """CampaignCriterion CREATE for a negative keyword (no mask on create)."""
    campaign_service = client.get_service("CampaignService")
    op = client.get_type("CampaignCriterionOperation")
    crit = op.create
    crit.campaign = campaign_service.campaign_path(customer_id, campaign_id)
    crit.negative = True
    crit.keyword.text = keyword_text
    crit.keyword.match_type = getattr(client.enums.KeywordMatchTypeEnum, match_type)
    return op


def _build_remove_cc_op(client, resource_name: str):
    """CampaignCriterion REMOVE by resource name (used by --undo of a negative)."""
    op = client.get_type("CampaignCriterionOperation")
    op.remove = resource_name
    return op


def _mutate_agc(client, customer_id: str, op, validate_only: bool):
    service = client.get_service("AdGroupCriterionService")
    request = client.get_type("MutateAdGroupCriteriaRequest")
    request.customer_id = customer_id
    request.operations = [op]
    request.validate_only = validate_only
    request.partial_failure = False
    return service.mutate_ad_group_criteria(request=request)


def _mutate_cc(client, customer_id: str, op, validate_only: bool):
    service = client.get_service("CampaignCriterionService")
    request = client.get_type("MutateCampaignCriteriaRequest")
    request.customer_id = customer_id
    request.operations = [op]
    request.validate_only = validate_only
    request.partial_failure = False
    return service.mutate_campaign_criteria(request=request)


def _resource_name(response) -> Optional[str]:
    try:
        if response.results:
            return response.results[0].resource_name
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Apply a single action (forward direction)
# ---------------------------------------------------------------------------

def _apply_one(client, customer_id: str, action: Action, validate_only: bool):
    p = action.params
    if action.action_type == PAUSE_KEYWORD:
        op = _build_status_op(client, customer_id, p["ad_group_id"],
                              p["criterion_id"], p["after"])
        return _mutate_agc(client, customer_id, op, validate_only)
    if action.action_type == ADJUST_BID:
        op = _build_bid_op(client, customer_id, p["ad_group_id"],
                           p["criterion_id"], p["after"])
        return _mutate_agc(client, customer_id, op, validate_only)
    if action.action_type == ADD_NEGATIVE:
        op = _build_negative_op(client, customer_id, p["campaign_id"],
                                p["keyword"], p["match_type"])
        return _mutate_cc(client, customer_id, op, validate_only)
    raise ValueError(f"Action type {action.action_type} is not mutating")


# ---------------------------------------------------------------------------
# Public: apply_actions
# ---------------------------------------------------------------------------

def apply_actions(client, customer_id: str, actions: List[Action],
                  cfg: Dict[str, Any], *, apply: bool = False,
                  project_root: Optional[Path] = None) -> ApplyResult:
    """Plan (and, if apply=True, execute) the auto-appliable actions safely.

    `client` is unused when apply=False, so dry-run works without live creds.
    """
    customer_id = _norm_id(customer_id)
    allowed = _allowed_ids(cfg)
    max_changes = _cfg(cfg, "safety", "max_changes_per_run", default=25)
    cap_pct = _cfg(cfg, "safety", "max_bid_change_pct", default=15)

    mutating = [a for a in actions if a.auto_appliable]
    recommend_only = [a for a in actions if not a.auto_appliable]

    result = ApplyResult(dry_run=not apply, recommend_only=recommend_only)

    # --- Gate 1 & 2: allow-list and per-bid cap ---
    to_apply: List[Action] = []
    for a in mutating:
        if _norm_id(a.campaign_id) not in allowed:
            result.refused.append((a, "campaign not in ALLOWED_CAMPAIGN_IDS"))
            continue
        if a.action_type == ADJUST_BID:
            before, after = a.params.get("before", 0), a.params.get("after", 0)
            if before > 0:
                pct = abs(after - before) / before * 100
                if pct > cap_pct + 1e-6:
                    result.refused.append(
                        (a, f"bid change {pct:.1f}% exceeds MAX_BID_CHANGE_PCT ({cap_pct}%)"))
                    continue
        to_apply.append(a)
    result.planned = to_apply

    # --- Gate 3: MAX_CHANGES_PER_RUN (abort whole run, write nothing) ---
    if len(to_apply) > max_changes:
        result.aborted = True
        result.abort_reason = (
            f"{len(to_apply)} changes exceed MAX_CHANGES_PER_RUN ({max_changes}); "
            "aborting without writing. Tighten thresholds or raise the cap.")
        logger.error(result.abort_reason)
        return result

    # --- Dry-run: no API calls at all ---
    if not apply:
        logger.info("DRY-RUN: %d change(s) planned, %d recommend-only, %d refused",
                    len(to_apply), len(recommend_only), len(result.refused))
        return result

    if not to_apply:
        logger.info("Nothing to apply.")
        return result

    # --- Pre-flight validation (validate_only=True). Abort all if any fails. ---
    for a in to_apply:
        try:
            _apply_one(client, customer_id, a, validate_only=True)
        except Exception as exc:  # noqa: BLE001 - surfaced to the report
            result.errors.append((a, f"validation failed: {_fmt_error(exc)}"))
    if result.errors:
        result.aborted = True
        result.abort_reason = "pre-flight validation failed; nothing was applied"
        logger.error("%s (%d errors)", result.abort_reason, len(result.errors))
        return result

    # --- Execute for real, logging each success ---
    for a in to_apply:
        try:
            resp = _apply_one(client, customer_id, a, validate_only=False)
            entry = {
                "applied_at": datetime.now().isoformat(timespec="seconds"),
                "action_type": a.action_type,
                "campaign_id": a.campaign_id,
                "summary": a.summary,
                "params": a.params,
                "resource_name": _resource_name(resp),
            }
            result.applied.append(entry)
            logger.info("APPLIED %s: %s", a.action_type, a.summary)
        except Exception as exc:  # noqa: BLE001
            result.errors.append((a, _fmt_error(exc)))
            logger.error("FAILED %s: %s", a.action_type, _fmt_error(exc))

    if result.applied:
        result.logfile = str(_write_log(result.applied, project_root))
        logger.info("Logged %d change(s) to %s", len(result.applied), result.logfile)
    return result


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------

def _revert_one(client, customer_id: str, entry: Dict[str, Any]):
    at = entry["action_type"]
    p = entry["params"]
    if at == PAUSE_KEYWORD:
        # restore the previous status (e.g. ENABLED)
        op = _build_status_op(client, customer_id, p["ad_group_id"],
                              p["criterion_id"], p["before"])
        return _mutate_agc(client, customer_id, op, False)
    if at == ADJUST_BID:
        op = _build_bid_op(client, customer_id, p["ad_group_id"],
                           p["criterion_id"], int(p["before"]))
        return _mutate_agc(client, customer_id, op, False)
    if at == ADD_NEGATIVE:
        rn = entry.get("resource_name")
        if not rn:
            raise RuntimeError("no resource_name recorded; cannot remove negative")
        op = _build_remove_cc_op(client, rn)
        return _mutate_cc(client, customer_id, op, False)
    raise ValueError(f"Cannot undo unknown action type {at!r}")


def undo_from_log(client, customer_id: str, logfile: str,
                  cfg: Dict[str, Any], *, apply: bool = False,
                  project_root: Optional[Path] = None) -> UndoResult:
    """Reverse the changes recorded in a changes_*.json log.

    Honors ALLOWED_CAMPAIGN_IDS (skips entries outside it). apply=False prints
    the plan without touching the account.
    """
    customer_id = _norm_id(customer_id)
    allowed = _allowed_ids(cfg)
    path = Path(logfile)
    if not path.exists():
        raise FileNotFoundError(f"Undo log not found: {logfile}")
    entries = json.loads(path.read_text() or "[]")

    result = UndoResult(dry_run=not apply)
    plan: List[Dict[str, Any]] = []
    for entry in entries:
        # Skip entries that are themselves undo records to avoid double-reverting.
        if str(entry.get("action_type", "")).startswith("UNDO_"):
            continue
        if _norm_id(entry.get("campaign_id")) not in allowed:
            result.skipped.append((entry, "campaign not in ALLOWED_CAMPAIGN_IDS"))
            continue
        plan.append(entry)
    result.planned = plan

    if not apply:
        logger.info("DRY-RUN undo: %d change(s) would be reverted, %d skipped",
                    len(plan), len(result.skipped))
        return result

    for entry in plan:
        try:
            _revert_one(client, customer_id, entry)
            result.reverted.append(entry)
            logger.info("REVERTED %s: %s", entry["action_type"],
                        entry.get("summary", ""))
        except Exception as exc:  # noqa: BLE001
            result.errors.append((entry, _fmt_error(exc)))
            logger.error("undo FAILED for %s: %s", entry.get("action_type"),
                         _fmt_error(exc))

    if result.reverted:
        undo_entries = [{
            "applied_at": datetime.now().isoformat(timespec="seconds"),
            "action_type": "UNDO_" + e["action_type"],
            "campaign_id": e["campaign_id"],
            "summary": "Reverted: " + e.get("summary", ""),
            "reverts": e,
        } for e in result.reverted]
        result.logfile = str(_write_log(undo_entries, project_root))
    return result
