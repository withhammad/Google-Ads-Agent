# gads-agent

Autonomous **Google Ads optimization agent** for a single live Search campaign.
It pulls 30 days of keyword / search-term / ad data, runs a rules engine, writes a
daily markdown report (optionally to Slack + email), and — only when you explicitly
allow it — applies low-risk changes with hard guardrails and a full undo log.

> **Dry-run by default.** Running the agent makes **zero API writes** unless you
> pass `--apply`. Even then, every change is bounded by the caps in `config.yaml`
> and logged so it can be reversed with `--undo`.

Built and verified against the official **`google-ads` Python library (v31 / API v24)**.

---

## What it does

| Rule | Trigger | Action | Auto-appliable? |
|---|---|---|---|
| **1. Pause keyword** | spend ≥ `min_spend` and 0 conversions | pause the keyword | ✅ yes (with `--apply`) |
| **2. Add negative** | search term clicks ≥ `min_clicks`, 0 conversions | add as EXACT campaign negative | ✅ yes (with `--apply`) |
| **3. Adjust bid** | CPA vs `target_cpa` (move clamped to cap) | raise / lower keyword bid | ⚠️ only if `enable_auto_bids` **and** MANUAL_CPC |
| **4. Budget pacing** | projected month-end spend vs `monthly_budget` | flag over/under-pacing | ❌ recommend-only |
| **5. Low-CTR ad** | ad CTR > `ctr_gap_pct` below its ad-group average | flag for creative refresh | ❌ recommend-only |

---

## Repo layout

```
gads-agent/
├── .env.example          # secret template — copy to .env (never committed)
├── config.yaml           # caps, thresholds, allow-list, automation gates
├── requirements.txt
├── README.md
├── src/
│   ├── auth.py           # env-only credentials → GoogleAdsClient + config loader
│   ├── pull.py           # GAQL → tidy pandas DataFrames (read-only)
│   ├── rules.py          # the 5-rule decision engine → typed Actions
│   ├── apply.py          # guarded mutations + change log + undo
│   ├── report.py         # markdown report + optional Slack/SMTP
│   └── main.py           # argparse CLI: auth → pull → rules → (apply) → report
├── logs/                 # changes_{date}.json (gitignored)
└── reports/              # report_{date}.md (gitignored)
```

---

## Prerequisites — Google Ads API access (start this first; approval takes days)

1. **Manager (MCC) account** at ads.google.com; link the target account under it.
2. **Developer token** — MCC → Tools → API Center. Starts at *Test* level; apply for
   *Basic Access* to use real accounts (can take a few days).
3. **Google Cloud project** → enable the Google Ads API.
4. **OAuth2 client** (Desktop app) → save client ID + secret.
5. **Refresh token** — generate once (OAuth playground or the library's
   `generate_user_credentials` example).

You end with **6 values**: `developer_token`, `client_id`, `client_secret`,
`refresh_token`, `login_customer_id` (the MCC, 10 digits), and `customer_id`
(the operating account).

> **Test against a sandbox account first.** Validate the full
> pull → rules → apply → undo loop on a test campaign before pointing it at a live one.

---

## Setup

```bash
# 1. (recommended) virtual environment
python3 -m venv .venv && source .venv/bin/activate

# 2. install deps
pip install -r requirements.txt

# 3. credentials
cp .env.example .env          # then fill in your 6 secrets

# 4. config — open config.yaml and set:
#    - safety.allowed_campaign_ids  -> your real Search campaign ID
#    - rules.adjust_bid.target_cpa, rules.budget.monthly_budget, thresholds
#    - reporting.currency
```

`.env` holds **all secrets** (Google Ads creds + optional Slack/SMTP). `config.yaml`
holds **non-secret** caps and thresholds. Secrets are never read from `config.yaml`.

---

## Safety model (non-negotiable for a live account)

Roll out in three phases — don't skip to Phase 3:

1. **Phase 1 — Read-only audit.** Run dry-run for ~1 week; eyeball the recommendations.
2. **Phase 2 — Recommend + approve.** Review the report, then `--apply` for the
   auto-appliable items (pauses + negatives).
3. **Phase 3 — Auto-apply low-risk.** Schedule `--apply`. Keep `enable_auto_bids: false`
   until you trust the bid logic; bid changes stay capped and logged.

Hard guardrails baked into `apply.py`:

- **Dry-run default** — writes require `--apply`.
- **`MAX_CHANGES_PER_RUN`** (default 25) — exceed it and the run **aborts without writing**.
- **`MAX_BID_CHANGE_PCT`** (default 15%) — clamped in `rules.py` and re-checked in `apply.py`.
- **`ALLOWED_CAMPAIGN_IDS`** — any action outside the list is refused; an empty list
  disables all writes.
- **`validate_only=True` pre-flight** — under `--apply`, every op is validated at the
  API first; if any fails, the run aborts before writing anything.
- **Change log** — `logs/changes_{date}.json` records before/after + resource name for
  every applied change, so `--undo` can reverse it.

> **Smart Bidding note.** If the campaign uses Target CPA / Target ROAS / Maximize\*,
> manual keyword bid mutations have no effect. The agent detects
> `campaign.bidding_strategy_type` and automatically downgrades bid changes to
> recommend-only — its value there is negatives, pausing, pacing, and recommendations.

---

## How to run

### Dry-run (default — no changes)

```bash
# campaign id taken from --campaign, or from a single-entry allow-list in config
python -m src.main --campaign <CAMPAIGN_ID>

# common variants
python -m src.main --campaign <CAMPAIGN_ID> --lookback 14    # 14-day window
python -m src.main --campaign <CAMPAIGN_ID> --no-deliver      # skip Slack/email
python -m src.main --campaign <CAMPAIGN_ID> -v                # debug logging
```

Review `reports/report_<date>.md`. Tune thresholds in `config.yaml`. Repeat for a week.

### Apply (Phase 2/3 — writes, when confident)

```bash
python -m src.main --campaign <CAMPAIGN_ID> --apply
```

Applies only the auto-appliable actions (pauses + negatives; bids too if
`enable_auto_bids: true` **and** the campaign is MANUAL_CPC), respecting all caps,
and writes `logs/changes_<date>.json`.

### Undo

```bash
python -m src.main --undo logs/changes_<date>.json            # PREVIEW (no writes)
python -m src.main --undo logs/changes_<date>.json --apply    # actually revert
```

### Schedule the daily dry-run (example: cron at 7am)

```cron
0 7 * * *  cd /path/to/gads-agent && /path/to/.venv/bin/python -m src.main --campaign <ID> >> logs/cron.log 2>&1
```

Exit codes: `0` success · `1` errors during apply · `2` run aborted by a guardrail.

---

## Configuration reference (`config.yaml`)

| Key | Meaning |
|---|---|
| `safety.max_changes_per_run` | abort threshold for one `--apply` run |
| `safety.max_bid_change_pct` | max single bid move (% of current bid) |
| `safety.allowed_campaign_ids` | the only campaigns the agent may modify |
| `data.lookback_days` | reporting window (default 30) |
| `rules.pause_keyword.min_spend` | spend with 0 conv that triggers a pause |
| `rules.add_negative.min_clicks` | clicks with 0 conv that triggers a negative |
| `rules.adjust_bid.target_cpa` / `cpa_tolerance` / `bid_step_pct` | bid logic |
| `rules.budget.monthly_budget` / `overpace_pct` / `underpace_pct` | pacing flags |
| `rules.low_ctr_ad.min_impressions` / `ctr_gap_pct` | low-CTR flag |
| `automation.enable_auto_bids` | gate for auto-applying Rule 3 |
| `reporting.output_dir` / `currency` | report location + money label |

Optional env (in `.env`): `SLACK_WEBHOOK_URL`, `SMTP_*` / `REPORT_EMAIL_*` (delivery
is skipped if absent), and `GOOGLE_ADS_API_VERSION` to pin the API version.

---

## Screenshots (for the case study)

_Add these once you've run it against a real account:_

- `docs/screenshot-report.png` — a daily report (recommendations + applied list)
- `docs/screenshot-dryrun.png` — terminal summary of a dry-run
- `docs/screenshot-before-after.png` — a wasted-spend keyword it caught

---

## Tests & CI

The pure layers (rules, safety gates, report rendering, helpers) are covered by an
offline pytest suite — no API access or credentials required.

```bash
pip install -r requirements-dev.txt
python -m pytest
```

GitHub Actions:

- **`.github/workflows/ci.yml`** — runs the test suite on every push / PR.
- **`.github/workflows/daily-dryrun.yml`** — scheduled **dry-run** (06:00 UTC) that
  uploads the report as an artifact. It never passes `--apply`; CI must not write to a
  live account. Configure repo **secrets** (`GOOGLE_ADS_*`, optional `SLACK_WEBHOOK_URL`)
  and a repo **variable** `CAMPAIGN_ID`.

## Caveats

- **Developer-token approval gates everything** — start it first; build/test the logic
  on a test account meanwhile.
- **Never auto-apply bids without review at first.** Keep `enable_auto_bids: false`
  until Phase 3.
- **Smart Bidding** campaigns ignore manual keyword bids (see note above).
- Field names were verified against API v24; re-validate against your installed
  library version with the Query Builder if you pin a different version.
