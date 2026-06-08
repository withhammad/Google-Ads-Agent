"""Unit tests for auth.py credential handling and config loading."""

import pytest

from src import auth


def test_clean_customer_id_strips_dashes_and_spaces():
    assert auth._clean_customer_id("123-456-7890") == "1234567890"
    assert auth._clean_customer_id(" 123 ") == "123"


def test_get_credentials_raises_when_vars_missing(monkeypatch):
    # don't let a local .env populate the environment during the test
    monkeypatch.setattr(auth, "load_env", lambda *a, **k: None)
    for var in auth.REQUIRED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(EnvironmentError):
        auth.get_credentials()


def test_load_config_reads_project_yaml():
    cfg = auth.load_config()  # default = project config.yaml
    assert cfg["safety"]["max_changes_per_run"] == 25
    assert cfg["safety"]["max_bid_change_pct"] == 15
    assert "allowed_campaign_ids" in cfg["safety"]
