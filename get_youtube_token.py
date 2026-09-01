#!/usr/bin/env python3
"""One-time (per channel) helper: prints a YouTube refresh token.

Run locally on a browser-capable machine:
    pip install google-auth-oauthlib
    NICHE=opensource-projects python get_youtube_token.py

Sign in with the Google account that owns the channel you want this niche to
upload to, then save the printed token as the secret named at the end.

Prerequisite: a Google Cloud project with "YouTube Data API v3" enabled and
an OAuth client (type: Desktop app) -- gives you YT_CLIENT_ID / YT_CLIENT_SECRET.
Add yourself as a test user on the OAuth consent screen if the app is in
"Testing" publishing status (it will be, and that's fine for one channel).
"""
import os
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

_env = Path(__file__).resolve().parent / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

NICHE = (os.environ.get("NICHE") or (sys.argv[1] if len(sys.argv) > 1 else "")).strip()
if not NICHE:
    sys.exit("Usage: NICHE=<niche-id> python get_youtube_token.py")

import re
SUFFIX = re.sub(r"[^A-Za-z0-9]", "", NICHE).upper()


def pick(name):
    value = (os.environ.get(f"{name}_{SUFFIX}") or "").strip()
    if value:
        print(f"using {name}_{SUFFIX}")
        return value
    value = (os.environ.get(name) or "").strip()
    if not value:
        sys.exit(f"Set {name}_{SUFFIX} (per-project) or {name} (shared) in your environment or .env")
    return value


flow = InstalledAppFlow.from_client_config(
    {
        "installed": {
            "client_id": pick("YT_CLIENT_ID"),
            "client_secret": pick("YT_CLIENT_SECRET"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    },
    scopes=["https://www.googleapis.com/auth/youtube.upload"],
)
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
print(f"\nREFRESH TOKEN for {NICHE}:\n{creds.refresh_token}")
print(f"\nSave it as the GitHub Actions secret: YT_REFRESH_TOKEN_{SUFFIX}")
print(f"  gh secret set YT_REFRESH_TOKEN_{SUFFIX}")
