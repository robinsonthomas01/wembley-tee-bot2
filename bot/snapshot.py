#!/usr/bin/env python3
"""
Cache the tee sheets into the repo so the phone app opens instantly.

On a desktop the extension reads Wembley live. On a phone it can't, so the
scanner writes what it already fetched into sheets.json and bookings.json.
The app reads those from the repo in one request and shows how stale they are.

    python -m bot.snapshot            # refresh both files
    python -m bot.snapshot --days 12
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

from .client import WembleyClient, LoginFailed
from .config import Config, SetupIncomplete, now_perth

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("wgc.snapshot")

SHEETS = Path("sheets.json")
BOOKINGS = Path("bookings.json")


def all_resources(cfg: Config) -> list[str]:
    return list((cfg.site.get("courses") or {}).keys())


def run(days: int | None = None, client: WembleyClient | None = None) -> int:
    cfg = Config.load()
    client = client or WembleyClient(cfg)

    # 10 days out is as far as Wembley opens; +1 covers the morning of a drop.
    horizon = days or (cfg.booking_open_days + 1)
    today = now_perth().date()
    keys = all_resources(cfg)

    try:
        client.ensure_login()
    except LoginFailed as e:
        log.error("Login failed: %s", e)
        return 1

    out = {"generatedAt": now_perth().isoformat(timespec="seconds"),
           "resources": keys, "dates": {}}

    for n in range(horizon + 1):
        date = today + dt.timedelta(days=n)
        sheets = []
        for key in keys:
            slots = client.fetch_slots(key, date)
            sheets.append({
                "resource": key,
                "slots": [{"time": s.time.strftime("%H:%M"),
                           "freePlaces": s.free_places,
                           "price": s.price} for s in slots],
            })
        total = sum(len(s["slots"]) for s in sheets)
        out["dates"][date.isoformat()] = sheets
        log.info("%s: %d open tee times", date, total)

    SHEETS.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    log.info("Wrote %s (%.0f KB)", SHEETS, SHEETS.stat().st_size / 1024)

    try:
        mine = client.bookings()
        BOOKINGS.write_text(json.dumps({
            "generatedAt": out["generatedAt"],
            "bookings": [{"date": b.date.isoformat(),
                          "time": b.time.strftime("%H:%M"),
                          "course": b.course,
                          "cancelUrl": b.cancel_url} for b in mine],
        }, indent=2), encoding="utf-8")
        log.info("Wrote %s (%d booking(s))", BOOKINGS, len(mine))
    except Exception as e:
        log.warning("Could not read your bookings: %s", e)

    return 0


def _main(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except SetupIncomplete as e:
        log.error("%s", e)
        return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    sys.exit(_main(run, ap.parse_args().days))
