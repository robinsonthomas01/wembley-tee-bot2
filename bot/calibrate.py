#!/usr/bin/env python3
"""
Work out how far ahead Wembley actually opens its sheets.

The site says "10 days in advance", but the observed behaviour is 11: the
Sunday sheet appears at 6am on the Wednesday before. Getting this wrong means
the 6am job fires on the wrong morning every week, so measure it rather than
trust the wording.

    python -m bot.calibrate           # report
    python -m bot.calibrate --write   # report and update config.yaml

Run it once after 6am, and again any time bookings start missing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import re
import sys
from pathlib import Path

from .client import WembleyClient, LoginFailed
from .config import Config, now_perth
from . import miclub as M

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("wgc.calibrate")


def furthest_bookable(client: WembleyClient) -> dt.date | None:
    """
    The last date the calendar offers, paging forward a week at a time.

    Reads the grid rather than trusting the "10 days in advance" wording,
    because the observed behaviour is 11.
    """
    today = now_perth().date()
    seen: set[dt.date] = set()

    for week in range(4):
        probe = today + dt.timedelta(days=week * 7)
        grid = client.calendar(probe)
        for iso in grid.get("dates", []):
            try:
                seen.add(dt.date.fromisoformat(iso))
            except ValueError:
                continue
        for days in grid.get("courses", {}).values():
            for iso in days:
                try:
                    seen.add(dt.date.fromisoformat(iso))
                except ValueError:
                    continue
        if not grid.get("dates") and not grid.get("courses"):
            break

    future = sorted(d for d in seen if d >= today)
    if not future:
        return None
    log.info("  Calendar covers %s to %s (%d day(s))",
             future[0], future[-1], len(future))
    return future[-1]


def run(write: bool = False) -> int:
    cfg = Config.load()
    client = WembleyClient(cfg)
    try:
        client.login()
    except LoginFailed as e:
        log.error("Login failed: %s", e)
        return 1

    now = now_perth()
    today = now.date()
    last = furthest_bookable(client)
    if not last:
        log.error("Could not determine the booking horizon. Check site.yaml.")
        return 1

    offset = (last - today).days
    before_six = now.time() < cfg.booking_open_time

    log.info("")
    log.info("  Today (Perth)        %s  %s", today.strftime("%a %d %b"),
             now.strftime("%H:%M"))
    log.info("  Furthest sheet open  %s", last.strftime("%a %d %b"))
    log.info("  Lead time            %d days", offset)
    if before_six:
        log.info("  Note: it's before 6am, so today's release hasn't happened.")
        log.info("        After 6am the lead becomes %d days.", offset + 1)
    log.info("  Currently configured %d days", cfg.booking_open_days)
    log.info("")

    real = offset + 1 if before_six else offset

    log.info("  Which morning each sheet opens at %s:",
             cfg.booking_open_time.strftime("%H:%M"))
    for wd in range(7):
        play = today + dt.timedelta(days=(wd - today.weekday()) % 7 + 7)
        opens = play - dt.timedelta(days=real)
        wanted = any(t.matches_date(play) for t in cfg.active_targets())
        log.info("    %-9s sheet opens %-9s %s",
                 play.strftime("%A"), opens.strftime("%A"),
                 "<- you want this" if wanted else "")
    log.info("")

    if real == cfg.booking_open_days:
        log.info("  Configuration matches. Nothing to change.")
        return 0

    log.warning("  MISMATCH: config says %d, the site is doing %d.",
                cfg.booking_open_days, real)
    if not write:
        log.warning("  Re-run with --write to fix config.yaml.")
        return 1

    path = Path("config.yaml")
    text = path.read_text(encoding="utf-8")
    new = re.sub(r"(open_days_ahead:\s*)\d+", rf"\g<1>{real}", text, count=1)
    if new == text:
        log.error("  Could not find open_days_ahead in config.yaml.")
        return 1
    path.write_text(new, encoding="utf-8")
    log.info("  Updated config.yaml to open_days_ahead: %d", real)
    log.info("  If you use the app, delete config.json so it re-seeds.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    sys.exit(run(ap.parse_args().write))
