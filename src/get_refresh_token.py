"""
get_refresh_token.py — one-time helper to mint a Google Ads API refresh token.

Runs the OAuth 2.0 *installed app* (Desktop) flow: it opens your browser, you
grant access, the script captures the redirect on a local port, and prints the
refresh token to paste into your .env as GOOGLE_ADS_REFRESH_TOKEN.

Credential source (first match wins):
  1. --client-secret-file PATH   (or ./client_secret.json if present)
  2. GOOGLE_ADS_CLIENT_ID + GOOGLE_ADS_CLIENT_SECRET from env / .env

Scope: https://www.googleapis.com/auth/adwords

Run it once, then you never need it again (refresh tokens are long-lived).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Load .env so GOOGLE_ADS_CLIENT_ID / GOOGLE_ADS_CLIENT_SECRET resolve if you
# keep them there. Harmless if python-dotenv isn't installed or there's no .env.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

SCOPES = ["https://www.googleapis.com/auth/adwords"]
DEFAULT_SECRET_FILE = "client_secret.json"


def _build_flow(client_secret_file: str | None):
    """Construct an InstalledAppFlow from a client_secret.json or env vars."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency 'google-auth-oauthlib'. Install it with:\n"
            "  pip install -r requirements.txt"
        ) from exc

    # 1) explicit file, or ./client_secret.json if it happens to be there
    path = client_secret_file or (
        DEFAULT_SECRET_FILE if Path(DEFAULT_SECRET_FILE).exists() else None)
    if path:
        if not Path(path).exists():
            raise SystemExit(f"Client secret file not found: {path}")
        print(f"Using client secret file: {path}")
        return InstalledAppFlow.from_client_secrets_file(path, scopes=SCOPES)

    # 2) env vars
    client_id = os.getenv("GOOGLE_ADS_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_ADS_CLIENT_SECRET")
    if not (client_id and client_secret):
        raise SystemExit(
            "No credentials found. Provide --client-secret-file <path>, or set "
            "GOOGLE_ADS_CLIENT_ID and GOOGLE_ADS_CLIENT_SECRET (e.g. in .env)."
        )
    print("Using GOOGLE_ADS_CLIENT_ID / GOOGLE_ADS_CLIENT_SECRET from environment.")
    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": ["http://localhost"],
        }
    }
    return InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="get_refresh_token",
        description="Mint a Google Ads API refresh token via the installed-app "
                    "(Desktop) OAuth flow.",
    )
    parser.add_argument("--client-secret-file",
                        help="path to an OAuth client_secret.json (Desktop app type)")
    parser.add_argument("--port", type=int, default=0,
                        help="local redirect port (0 = pick a free one; default 0)")
    args = parser.parse_args(argv)

    flow = _build_flow(args.client_secret_file)

    print("\nOpening your browser to authorize. Sign in with the Google account "
          "that manages the Ads campaign and grant access...\n")
    # access_type=offline + prompt=consent guarantee a refresh_token is returned
    # (these are passed through to the authorization URL). port=0 picks a free
    # port and run_local_server captures the redirect automatically.
    creds = flow.run_local_server(
        port=args.port,
        access_type="offline",
        prompt="consent",
        success_message="Authorization complete — you can close this tab and "
                        "return to the terminal.",
    )

    if not creds.refresh_token:
        print("\nNo refresh token was returned. Re-run and complete the consent "
              "screen fully (the flow already forces prompt=consent).",
              file=sys.stderr)
        return 1

    print("\n" + "=" * 68)
    print("SUCCESS — add this line to your .env:\n")
    print(f"GOOGLE_ADS_REFRESH_TOKEN={creds.refresh_token}")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
