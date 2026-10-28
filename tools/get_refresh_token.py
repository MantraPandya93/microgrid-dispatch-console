"""One-time helper: exchange a Zoho Self Client grant code for a refresh token.

Run it in a terminal on your own computer (never paste the printed values into a chat or commit them).

PowerShell:
    $env:ZOHO_CLIENT_ID="..."
    $env:ZOHO_CLIENT_SECRET="..."
    $env:ZOHO_CODE="1000.xxxx..."      # the code from API Console > Self Client > Generate Code
    python get_refresh_token.py
"""
import os
import sys

import requests

BASE = os.environ.get("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.in").rstrip("/")

cid = os.environ.get("ZOHO_CLIENT_ID", "").strip()
sec = os.environ.get("ZOHO_CLIENT_SECRET", "").strip()
code = os.environ.get("ZOHO_CODE", "").strip()

# Convenience: read the self_client.json downloaded from API Console (same folder as this script).
_json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "self_client.json")
if not (cid and sec and code) and os.path.exists(_json_path):
    import json

    with open(_json_path, encoding="utf-8") as f:
        _j = json.load(f)
    cid = cid or _j.get("client_id", "")
    sec = sec or _j.get("client_secret", "")
    code = code or _j.get("code", "")
    print("Using values from self_client.json")

if not (cid and sec and code):
    sys.exit("Put self_client.json next to this script, or set ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET and ZOHO_CODE.")

r = requests.post(
    f"{BASE}/oauth/v2/token",
    data={
        "grant_type": "authorization_code",
        "client_id": cid,
        "client_secret": sec,
        "code": code,
    },
    timeout=30,
)
data = r.json()
if "refresh_token" in data:
    print("SUCCESS. Your refresh token (keep it secret):")
    print(data["refresh_token"])
    print("\nNow set it:  $env:ZOHO_REFRESH_TOKEN=\"<the value above>\"")
else:
    # Typical errors: invalid_code (expired, generate a new one), invalid_client (wrong id/secret)
    print("FAILED:", {k: v for k, v in data.items() if k != "access_token"})
