"""Unit tests for report.py rendering and no-op delivery."""

from src import report
from src.apply import ApplyResult
from src.rules import PAUSE_KEYWORD, Action


def _pause_action():
    return Action(PAUSE_KEYWORD, 123, 'Pause keyword "x" (EXACT)',
                  "spent 90.00 with 0 conversions", True,
                  {"ad_group_id": 1, "criterion_id": 2, "keyword": "x",
                   "field": "status", "before": "ENABLED", "after": "PAUSED"})


def test_build_markdown_has_core_sections(cfg, make_data):
    data = make_data()
    actions = [_pause_action()]
    result = ApplyResult(dry_run=True, planned=actions)
    md = report.build_markdown(data, actions, result, cfg)
    assert "# Google Ads Daily Report" in md
    assert "## Performance" in md
    assert "## Recommendations" in md
    assert "Pause keywords (1)" in md
    assert "## Applied this run" in md
    assert "none (dry-run)" in md
    assert "AED" in md  # currency label applied


def test_build_markdown_marks_applied(cfg, make_data):
    data = make_data()
    actions = [_pause_action()]
    result = ApplyResult(
        dry_run=False,
        applied=[{"action_type": PAUSE_KEYWORD, "summary": actions[0].summary,
                  "resource_name": "customers/1/adGroupCriteria/2~3"}],
        planned=actions)
    md = report.build_markdown(data, actions, result, cfg)
    assert f"- [x] {actions[0].summary}" in md
    assert "adGroupCriteria/2~3" in md


def test_abort_banner_present(cfg, make_data):
    result = ApplyResult(dry_run=False, aborted=True,
                         abort_reason="too many changes")
    md = report.build_markdown(make_data(), [], result, cfg)
    assert "RUN ABORTED" in md


def test_slack_noop_without_env(monkeypatch):
    monkeypatch.setattr(report, "_ensure_env", lambda: None)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    assert report.send_slack("hi") is False


def test_email_noop_without_env(monkeypatch):
    monkeypatch.setattr(report, "_ensure_env", lambda: None)
    for var in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD",
                "REPORT_EMAIL_FROM", "REPORT_EMAIL_TO"):
        monkeypatch.delenv(var, raising=False)
    assert report.send_email("subject", "body") is False
