"""
report.py — daily markdown report + optional Slack/SMTP delivery.

build_markdown() renders a report from the pulled data, the rule actions, and the
ApplyResult. save_report() writes it to reports/. send_slack()/send_email() push
it out and are NO-OPs when their env vars are absent — so the pipeline runs fine
with no integrations configured.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from .rules import (ADD_NEGATIVE, ADJUST_BID, FLAG_BUDGET, FLAG_LOW_CTR_AD,
                    PAUSE_KEYWORD, Action)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Display order + headings for the recommendation checklist.
_SECTIONS = [
    (PAUSE_KEYWORD, "Pause keywords"),
    (ADD_NEGATIVE, "Add negatives"),
    (ADJUST_BID, "Bid adjustments"),
    (FLAG_BUDGET, "Budget pacing flags"),
    (FLAG_LOW_CTR_AD, "Low-CTR ad flags"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(cfg: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    node: Any = cfg
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _money(value: Optional[float], currency: str) -> str:
    if value is None:
        return "—"
    return f"{currency} {value:,.2f}".strip()


def _ensure_env() -> None:
    """Best-effort load of .env so Slack/SMTP vars resolve if run standalone."""
    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env", override=False)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def build_markdown(data, actions: List[Action], apply_result,
                   cfg: Dict[str, Any]) -> str:
    """Render the full daily report markdown."""
    cur = _cfg(cfg, "reporting", "currency", default="")
    s = data.settings
    m = data.metrics
    pacing = data.pacing

    mode = "DRY-RUN (no changes made)" if apply_result.dry_run else "APPLY"
    lines: List[str] = []
    lines.append(f"# Google Ads Daily Report — {date.today().isoformat()} — "
                 f"{s.get('campaign_name', '?')} ({s.get('campaign_id', '?')})")
    lines.append("")
    lines.append(f"- **Mode:** {mode}")
    lines.append(f"- **Window:** last {m.get('lookback_days')} days "
                 f"({m.get('window_start')} → {m.get('window_end')})")
    lines.append(f"- **Bidding strategy:** {s.get('bidding_strategy_type')}"
                 + ("" if s.get("manual_bids_supported")
                    else " _(Smart Bidding — manual keyword bids are recommend-only)_"))

    if apply_result.aborted:
        lines.append("")
        lines.append(f"> ⛔ **RUN ABORTED — nothing was applied.** {apply_result.abort_reason}")

    # --- Performance ---
    cpa = m.get("cpa")
    roas = m.get("roas")
    lines += ["", "## Performance", "", "| Metric | Value |", "|---|---|",
              f"| Spend | {_money(m.get('cost'), cur)} |",
              f"| Conversions | {m.get('conversions', 0):.1f} |",
              f"| Conv. value | {_money(m.get('conversion_value'), cur)} |",
              f"| CPA | {_money(cpa, cur)} |",
              f"| ROAS | {('%.2f' % roas) if roas is not None else '—'} |",
              f"| CTR | {m.get('ctr', 0) * 100:.2f}% |",
              f"| Clicks | {m.get('clicks', 0):,} |",
              f"| Impressions | {m.get('impressions', 0):,} |",
              f"| Avg CPC | {_money(m.get('avg_cpc'), cur)} |"]

    # --- Budget pacing ---
    monthly_budget = _cfg(cfg, "rules", "budget", "monthly_budget", default=0.0)
    if pacing.get("days_elapsed", 0) > 0 and monthly_budget:
        projected = pacing["mtd_spend"] / pacing["days_elapsed"] * pacing["days_in_month"]
        pct = projected / monthly_budget * 100
        lines += ["", "## Budget pacing",
                  f"- MTD spend: {_money(pacing['mtd_spend'], cur)} over "
                  f"{pacing['days_elapsed']} day(s) (as of {pacing.get('as_of')})",
                  f"- Projected month-end: {_money(projected, cur)} vs budget "
                  f"{_money(monthly_budget, cur)} (**{pct:.0f}%**)"]

    # --- Recommendations checklist ---
    applied_summaries = {e["summary"] for e in apply_result.applied}
    by_type: Dict[str, List[Action]] = {}
    for a in actions:
        by_type.setdefault(a.action_type, []).append(a)

    lines += ["", "## Recommendations"]
    if not actions:
        lines.append("- _No actions triggered by the current thresholds._")
    for action_type, heading in _SECTIONS:
        group = by_type.get(action_type, [])
        if not group:
            continue
        lines += ["", f"### {heading} ({len(group)})"]
        for a in group:
            mark = "x" if a.summary in applied_summaries else " "
            lines.append(f"- [{mark}] {a.summary} — {a.reason}")

    # --- Applied this run ---
    lines += ["", "## Applied this run"]
    if apply_result.dry_run:
        lines.append("- _none (dry-run)_")
    elif apply_result.applied:
        for e in apply_result.applied:
            rn = f" → `{e['resource_name']}`" if e.get("resource_name") else ""
            lines.append(f"- ✅ {e['summary']}{rn}")
        if apply_result.logfile:
            lines += ["", f"_Change log: `{apply_result.logfile}` (use `--undo` to reverse)._"]
    else:
        lines.append("- _none_")

    # --- Refused / errors ---
    if apply_result.refused:
        lines += ["", "## Refused (guardrails)"]
        for a, why in apply_result.refused:
            lines.append(f"- ⚠️ {a.summary} — {why}")
    if apply_result.errors:
        lines += ["", "## Errors"]
        for a, why in apply_result.errors:
            label = a.summary if isinstance(a, Action) else str(a)
            lines.append(f"- ❌ {label} — {why}")

    lines += ["", "---", "_Generated by gads-agent. Dry-run by default; "
              "writes require --apply._"]
    return "\n".join(lines)


def save_report(markdown: str, cfg: Dict[str, Any],
                project_root: Optional[Path] = None) -> Path:
    """Write the markdown to reports/report_{date}.md and return the path."""
    out_dir = (project_root or PROJECT_ROOT) / _cfg(
        cfg, "reporting", "output_dir", default="reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"report_{date.today().isoformat()}.md"
    path.write_text(markdown)
    logger.info("Report saved to %s", path)
    return path


# ---------------------------------------------------------------------------
# Delivery (no-ops without env vars)
# ---------------------------------------------------------------------------

def slack_summary(data, actions: List[Action], apply_result,
                  cfg: Dict[str, Any]) -> str:
    """A compact one-block summary for Slack (the full report goes to file/email)."""
    cur = _cfg(cfg, "reporting", "currency", default="")
    s = data.settings
    m = data.metrics
    counts = {}
    for a in actions:
        counts[a.action_type] = counts.get(a.action_type, 0) + 1
    mode = "DRY-RUN" if apply_result.dry_run else "APPLY"
    head = (f"*Google Ads — {s.get('campaign_name')}* ({date.today().isoformat()}, {mode})\n"
            f"Spend {_money(m.get('cost'), cur)} | Conv {m.get('conversions', 0):.1f} | "
            f"CPA {_money(m.get('cpa'), cur)} | CTR {m.get('ctr', 0) * 100:.2f}%")
    rec = ", ".join(f"{n}× {t}" for t, n in counts.items()) or "no actions"
    applied = len(apply_result.applied)
    tail = f"\nRecommendations: {rec}\nApplied: {applied}"
    if apply_result.aborted:
        tail += f"\n⛔ ABORTED: {apply_result.abort_reason}"
    return head + tail


def send_slack(text: str) -> bool:
    """Post to SLACK_WEBHOOK_URL. No-op (returns False) if unset."""
    _ensure_env()
    url = os.getenv("SLACK_WEBHOOK_URL")
    if not url:
        logger.info("SLACK_WEBHOOK_URL not set; skipping Slack.")
        return False
    try:
        import requests
        resp = requests.post(url, json={"text": text}, timeout=10)
        resp.raise_for_status()
        logger.info("Posted summary to Slack.")
        return True
    except Exception as exc:  # noqa: BLE001 - delivery must never crash the run
        logger.warning("Slack post failed: %s", exc)
        return False


def send_email(subject: str, body: str) -> bool:
    """Email the report via SMTP. No-op (returns False) unless all SMTP vars set."""
    _ensure_env()
    host = os.getenv("SMTP_HOST")
    port = os.getenv("SMTP_PORT", "587")
    user = os.getenv("SMTP_USERNAME")
    pwd = os.getenv("SMTP_PASSWORD")
    sender = os.getenv("REPORT_EMAIL_FROM")
    recipients = os.getenv("REPORT_EMAIL_TO")
    if not all([host, user, pwd, sender, recipients]):
        logger.info("SMTP not fully configured; skipping email.")
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body)  # markdown source as plain text
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = recipients
        to_list = [a.strip() for a in recipients.split(",") if a.strip()]
        with smtplib.SMTP(host, int(port), timeout=20) as server:
            server.starttls()
            server.login(user, pwd)
            server.sendmail(sender, to_list, msg.as_string())
        logger.info("Emailed report to %s", recipients)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Email send failed: %s", exc)
        return False


def generate(data, actions: List[Action], apply_result, cfg: Dict[str, Any],
             *, project_root: Optional[Path] = None,
             deliver: bool = True) -> Path:
    """Build, save, and (optionally) deliver the report. Returns the file path."""
    markdown = build_markdown(data, actions, apply_result, cfg)
    path = save_report(markdown, cfg, project_root)
    if deliver:
        send_slack(slack_summary(data, actions, apply_result, cfg))
        subject = (f"Google Ads report — {data.settings.get('campaign_name')} "
                   f"— {date.today().isoformat()}")
        send_email(subject, markdown)
    return path
