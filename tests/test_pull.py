"""Unit tests for pull.py pure helpers (no API access)."""

from datetime import date, timedelta

import pytest

from src import pull


def test_micros():
    assert pull._micros(1_500_000) == 1.5
    assert pull._micros(0) == 0.0
    assert pull._micros(None) == 0.0


def test_validate_campaign_id_ok():
    assert pull._validate_campaign_id(123) == "123"
    assert pull._validate_campaign_id("123") == "123"
    assert pull._validate_campaign_id(" 123 ") == "123"


@pytest.mark.parametrize("bad", ["12a", "'; DROP TABLE", "12-34", ""])
def test_validate_campaign_id_rejects_non_digits(bad):
    with pytest.raises(ValueError):
        pull._validate_campaign_id(bad)


def test_lookback_range_ends_yesterday_and_is_inclusive():
    start, end = pull._lookback_range(30)
    assert end == (date.today() - timedelta(days=1)).isoformat()
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    assert (e - s).days == 29  # 30 inclusive days


def test_bidding_strategy_classification():
    assert "MANUAL_CPC" in pull.MANUAL_BID_STRATEGIES
    assert "MANUAL_CPC" not in pull.SMART_BIDDING_STRATEGIES
    for s in ("TARGET_CPA", "TARGET_ROAS", "MAXIMIZE_CONVERSIONS",
              "MAXIMIZE_CONVERSION_VALUE"):
        assert s in pull.SMART_BIDDING_STRATEGIES
