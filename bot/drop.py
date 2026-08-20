#!/usr/bin/env python3
"""
The 6am release.

Wembley opens each sheet 10-11 days ahead at 6:00am, and most MiClub clubs put
a lottery in front of it: a registration window before the hour, then randomly
ordered admission once it opens.

So this runs in three phases:

  join    from ~15 minutes before, look for the draw and enter it. Once.
  wait    sit in the waiting room until admitted.
  book    the moment the tee sheet appears, take the best matching time.

Speed is not the point - the draw is random, so a faster connection wins
nothing. Never missing the window is the point.

If the club isn't running a draw for this sheet, phases one and two find
nothing and it simply books at 06:00:00 as before.

    python -m bot.drop            # the real thing
    python -m bot.drop --now      # skip the waiting, book what's there
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time

from . import queue as Q
from .client import WembleyClient, LoginFailed
from .config import Config, SetupIncomplete, now_perth, PERTH
from .match import find_hits, resources_wanted
from .notify import Mailer
from .push import Notifier
from .state import State

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("wgc.drop")

BOOK_SECONDS = 300      # keep trying this long after the sheet opens
GAP = 1.5               # between attempts once live


def wait_until(target: dt.datetime, label: str = "") -> None:
    while True:
        left = (target - now_perth()).total_seconds()
        if left <= 0:
            return
        if left > 60:
            log.info("Waiting %.0f min%s", left / 60, f" {label}" if label else "")
            time.sleep(min(left - 30, 120))
        elif left > 3:
            time.sleep(left - 2)
        else:
            time.sleep(0.05)


def plan_for(cfg, state, today):
    """Which dates releasing this morning are worth chasing."""
    plan = []
    for date in cfg.candidate_dates(today):
        wanted = resources_wanted(cfg, date)
        if not wanted:
            continue
        if state.has_booking_on(date.isoformat()):
            log.info("Already booked on %s - skipping.", date)
            continue
        plan.append((date, wanted))
    return plan


def queue_pages(client, plan) -> dict:
    """{tag: url} for every sheet we want to queue for."""
    pages = {}
    for date, courses in plan:
        for key in courses:
            event = client.event_for(key, date)
            pages[f"{key}|{date}"] = (event["href"] if event
                                      else client.calendar_url(date))
    return pages


def book_best(client, cfg, plan):
    """One sweep across the sheets; take the best match."""
    slots, hits = [], []
    for date, courses in plan:
        found = client.fetch_many(courses, date)
        slots.extend(found)
        hits.extend(find_hits(found, cfg, date))
    hits.sort(key=lambda h: h.score)
    if not hits:
        return False, f"{len(slots)} tee time(s) visible, none matching", None
    best = hits[0]
    ok, detail = client.book(best.slot, best.target.players)
    return ok, detail, best


def _safe_bookings(client):
    try:
        return client.bookings()
    except Exception as e:
        log.warning("Could not read existing bookings: %s", e)
        return []


def run(fire_now: bool = False) -> int:
    cfg = Config.load()
    state = State()
    notifier = Notifier(cfg, state, Mailer(cfg))

    now = now_perth()
    today = now.date()
    open_at = dt.datetime.combine(today, cfg.booking_open_time, tzinfo=PERTH)

    plan = plan_for(cfg, state, today)
    if not plan:
        log.info("Nothing wanted %d-%d days out. Done.",
                 cfg.booking_open_days - cfg.booking_probe_spread,
                 cfg.booking_open_days + cfg.booking_probe_spread)
        return 0
    for date, courses in plan:
        log.info("Chasing %s (%s): %s", date, date.strftime("%A"), ", ".join(courses))

    client = WembleyClient(cfg)
    try:
        client.login()
    except LoginFailed as e:
        notifier.report("Login failed before the 6am release", str(e))
        log.error("Login failed: %s", e)
        return 1

    already = [b for b in _safe_bookings(client) if b.date >= today]
    if len(already) >= cfg.max_upcoming:
        log.info("Already hold %d upcoming tee time(s) (max_upcoming=%d). "
                 "Standing down.", len(already), cfg.max_upcoming)
        return 0

    # ---- phase 1: the queue ---------------------------------------------
    # Wembley orders the queue by arrival, so this is the part that decides
    # how good a tee time you get. Watch for it opening and join at once.
    if not fire_now and cfg.queue_enabled:
        watch_start = open_at - dt.timedelta(minutes=cfg.queue_watch_minutes)
        if now < watch_start:
            wait_until(watch_start, "until the queue could open")
        log.info("Watching for the queue (up to %d min before 6am, checking "
                 "every %.0fs, then every %.0fs from %d min out).",
                 cfg.queue_watch_minutes, cfg.queue_poll_seconds,
                 cfg.queue_sprint_seconds, cfg.queue_sprint_minutes)
        Q.watch_and_join(client, queue_pages(client, plan), state, open_at,
                         watch_minutes=cfg.queue_watch_minutes,
                         poll_seconds=cfg.queue_poll_seconds,
                         sprint_minutes=cfg.queue_sprint_minutes,
                         sprint_seconds=cfg.queue_sprint_seconds)
        state.save()

    # ---- phase 2: wait to be admitted -----------------------------------
    if not fire_now:
        wait_until(open_at, "for the sheet to open")
    log.info("Sheet should be open. Looking for a tee time.")

    # ---- phase 3: book ---------------------------------------------------
    deadline = time.monotonic() + BOOK_SECONDS
    attempt, last = 0, ""
    while time.monotonic() < deadline:
        attempt += 1
        client._cal_cache.clear()          # never book off a stale calendar
        ok, detail, best = book_best(client, cfg, plan)
        last = detail
        if ok and best is not None:
            log.info(detail)
            state.mark_booked(best.slot.key, detail)
            state.note(f"DROP {detail}")
            state.save()
            notifier.report(
                f"Got {best.slot.date:%a %-d %b} at {best.slot.time:%-I:%M %p}",
                f"{detail}\n\nMatched your '{best.target.label}' window.")
            return 0
        if attempt == 1 or attempt % 10 == 0:
            log.info("Attempt %d: %s", attempt, detail)
        time.sleep(GAP)

    dates = ", ".join(f"{d:%A %-d %B}" for d, _ in plan)
    msg = (f"Nothing matched for {dates} after {attempt} attempts.\n\n"
           f"Last look: {last}\n\n"
           f"With a lottery this usually means a late queue position - the "
           f"good times were gone before you were admitted.")
    log.info(msg)
    state.note(f"DROP miss {dates}")
    state.save()
    notifier.report(f"No tee time for {plan[0][0]:%a %-d %b}", msg)
    return 0


def _main(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except SetupIncomplete as e:
        log.error("%s", e)
        return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", action="store_true",
                    help="skip the waiting and book whatever is open")
    sys.exit(_main(run, fire_now=ap.parse_args().now))
