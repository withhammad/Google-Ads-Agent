"""
main.py — CLI entry point.

Orchestrates: auth -> pull -> rules -> (apply) -> report, and prints a summary.

Usage:
    python -m src.main --campaign <ID>             # DRY-RUN (default, no writes)
    python -m src.main --campaign <ID> --apply     # execute auto-appliable changes
    python -m src.main --undo logs/changes_YYYY-MM-DD.json           # preview undo
    python -m src.main --undo logs/changes_YYYY-MM-DD.json --apply   # revert for real

Safety: without --apply, nothing is ever written. With --apply, the caps and
allow-list in config.yaml still gate every change (see apply.py).
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any, Dict, List, Optional

from . import auth, pull, report, rules
from .apply import apply_actions, undo_from_log

log = logging.getLogger("gads-agent")


def _cfg(cfg: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    node: Any = cfg
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _banner(text: str) -> str:
    bar = "═" * (len(text) + 2)
    return f"\n╔{bar}╗\n║ {text} ║\n╚{bar}╝"


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="gads-agent",
        description="Autonomous Google Ads optimization for one Search campaign. "
                    "Dry-run by default; --apply is required to write.",
    )
    p.add_argument("--apply", action="store_true",
                   help="execute changes (default: dry-run, zero API writes)")
    p.add_argument("--undo", metavar="LOGFILE",
                   help="revert changes from a logs/changes_*.json file "
                        "(previews unless combined with --apply)")
    p.add_argument("--campaign", metavar="ID",
                   help="campaign id to operate on (overrides config; defaults to "
                        "the single entry in safety.allowed_campaign_ids)")
    p.add_argument("--lookback", type=int, metavar="DAYS",
                   help="override data.lookback_days from config")
    p.add_argument("--config", metavar="PATH", default=None,
                   help="path to config.yaml (default: project config.yaml)")
    p.add_argument("--no-deliver", action="store_true",
                   help="skip Slack/email delivery (still writes the report file)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p.parse_args(argv)


def _resolve_campaign(args: argparse.Namespace, cfg: Dict[str, Any]) -> str:
    if args.campaign:
        return str(args.campaign).strip()
    allowed = [str(x).strip() for x in
               (_cfg(cfg, "safety", "allowed_campaign_ids", default=[]) or [])
               if str(x).strip()]
    if len(allowed) == 1:
        return allowed[0]
    raise SystemExit(
        "No campaign specified. Pass --campaign <ID>, or set exactly one id in "
        f"safety.allowed_campaign_ids (found {len(allowed)})."
    )


def _counts(actions: List[rules.Action]) -> str:
    by_type: Dict[str, int] = {}
    for a in actions:
        by_type[a.action_type] = by_type.get(a.action_type, 0) + 1
    return ", ".join(f"{n}× {t}" for t, n in by_type.items()) or "none"


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def _run_undo(client, customer_id: str, args: argparse.Namespace,
              cfg: Dict[str, Any]) -> int:
    mode = "APPLY (reverting)" if args.apply else "DRY-RUN (preview only)"
    print(_banner(f"UNDO {mode}"))
    print(f"Log file: {args.undo}")
    res = undo_from_log(client, customer_id, args.undo, cfg, apply=args.apply)

    if res.dry_run:
        print(f"\nWould revert {len(res.planned)} change(s); "
              f"{len(res.skipped)} skipped (allow-list).")
        for e in res.planned:
            print(f"  - {e.get('action_type')}: {e.get('summary', '')}")
        print("\nRe-run with --apply to actually revert.")
        return 0

    print(f"\nReverted {len(res.reverted)} change(s); {len(res.errors)} error(s).")
    for e in res.reverted:
        print(f"  ✓ {e.get('summary', '')}")
    for entry, why in res.errors:
        print(f"  ✗ {entry.get('summary', '')} — {why}")
    if res.logfile:
        print(f"Undo log: {res.logfile}")
    return 1 if res.errors else 0


def _run_optimize(client, customer_id: str, args: argparse.Namespace,
                  cfg: Dict[str, Any]) -> int:
    campaign_id = _resolve_campaign(args, cfg)
    print(_banner("APPLY MODE — writing to LIVE account" if args.apply
                  else "DRY-RUN — no changes will be made"))

    lookback = args.lookback or _cfg(cfg, "data", "lookback_days", default=30)
    data = pull.pull_all(client, customer_id, campaign_id, lookback)
    actions = rules.evaluate(data, cfg)
    result = apply_actions(client, customer_id, actions, cfg, apply=args.apply)
    report_path = report.generate(data, actions, result, cfg,
                                  deliver=not args.no_deliver)

    # --- printed summary ---
    auto = sum(1 for a in actions if a.auto_appliable)
    print("\n" + "=" * 64)
    print(f"Campaign : {data.settings.get('campaign_name')} "
          f"({data.settings.get('campaign_id')})")
    print(f"Strategy : {data.settings.get('bidding_strategy_type')}"
          + ("" if data.settings.get("manual_bids_supported")
             else "  (Smart Bidding — bid changes are recommend-only)"))
    m = data.metrics
    cur = _cfg(cfg, "reporting", "currency", default="")
    cpa = f"{cur} {m['cpa']:.2f}".strip() if m.get("cpa") is not None else "—"
    print(f"Window   : last {m.get('lookback_days')}d | spend {cur} {m.get('cost', 0):,.2f} "
          f"| conv {m.get('conversions', 0):.1f} | CPA {cpa} | "
          f"CTR {m.get('ctr', 0) * 100:.2f}%")
    print(f"Actions  : {len(actions)} ({auto} auto-appliable, "
          f"{len(actions) - auto} recommend-only) — {_counts(actions)}")

    if result.aborted:
        print(f"⛔ ABORTED: {result.abort_reason}")
    elif result.dry_run:
        print(f"Mode     : DRY-RUN — {len(result.planned)} change(s) would apply with --apply")
    else:
        print(f"Applied  : {len(result.applied)} | refused {len(result.refused)} "
              f"| errors {len(result.errors)}")
        if result.logfile:
            print(f"Log      : {result.logfile}  (reverse with --undo)")
    if result.refused:
        for a, why in result.refused:
            print(f"  ⚠️  refused: {a.summary} — {why}")
    print(f"Report   : {report_path}")
    print("=" * 64)

    if result.aborted:
        return 2
    if result.errors:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    cfg = auth.load_config(args.config)
    customer_id = auth.get_customer_id()
    client = auth.get_client()
    if args.undo:
        return _run_undo(client, customer_id, args, cfg)
    return _run_optimize(client, customer_id, args, cfg)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - top-level friendly error
        if args.verbose:
            log.exception("Fatal error")
        else:
            print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
