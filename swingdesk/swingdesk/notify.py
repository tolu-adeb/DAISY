"""Outbound: Discord webhook + (optional) Google Sheet row append. Alert-only by design."""
from __future__ import annotations

import json
import os

import requests

COLORS = {"GRADED": 0x5865F2, "IN_ZONE": 0xF1C40F, "FILLED": 0x2ECC71, "TP_HIT": 0x1ABC9C,
          "STOPPED": 0xE74C3C, "CANCELLED": 0x95A5A6, "EXPIRED": 0x95A5A6, "RESEARCH": 0x9B59B6, "PITCH": 0x3498DB}


def discord(text: str, kind: str = "GRADED", title: str | None = None, webhook: str | None = None,
            fields: list[dict] | None = None) -> bool:
    url = webhook or os.getenv("DISCORD_WEBHOOK_URL")
    if not url:
        print(f"[discord:{kind}] {title or ''}\n{text}\n")
        return False
    embed = {"description": text[:4000], "color": COLORS.get(kind, 0x99AAB5)}
    if title:
        embed["title"] = title[:256]
    if fields:
        embed["fields"] = fields[:25]
    r = requests.post(url, json={"embeds": [embed]}, timeout=20)
    return r.status_code < 300


def sheet_append(row: list, worksheet: str = "Desk") -> bool:
    """Append one row to the tracker sheet if GOOGLE_SERVICE_ACCOUNT_JSON + SHEET_ID are set."""
    creds, sheet_id = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"), os.getenv("SHEET_ID")
    if not (creds and sheet_id):
        return False
    import gspread
    gc = gspread.service_account_from_dict(json.loads(creds))
    ws = gc.open_by_key(sheet_id)
    try:
        w = ws.worksheet(worksheet)
    except gspread.WorksheetNotFound:
        w = ws.add_worksheet(worksheet, rows=1000, cols=20)
        w.append_row(["time", "event", "symbol", "detail"])
    w.append_row([str(x) for x in row], value_input_option="USER_ENTERED")
    return True
