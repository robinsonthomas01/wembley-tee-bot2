#!/usr/bin/env python3
"""
The cancellation scanner.

Each GitHub Actions run polls the sheets for a few minutes, then exits. The
workflow re-runs it every 5 minutes, which gives near-continuous coverage
without a server.

Order of business each cycle:
  1. Read the inbox for 'Y' replies to earlier alerts, and book those first.
  2. Sweep the sheets for anything new that matches a preference.
  3. In 'notify' mode, email you and wait. In 'auto' mode, book it and say so.

    python -m bot.scan                 # one run (~4 minutes)
    python -m bot.scan --once          # a single sweep, then exit
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time

from .client import WembleyClient, LoginFailed
from .config import Config, SetupIncomplete, now_perth
from .match import find_hits, find_upgrades, resources_wanted
from .notify import Mailer
from .push import Notifier
from .state import State

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("wgc.scan")

RUN_SECONDS = 480      # each run covers most of its 10-minute cron interval


def dates_to_watch(cfg: Config) -> list[dt.date]:
    today = now_perth().date()
    out = []
    for n in range(1, cfg.scan.lookahead_days + 1):
        d = today + dt.timedelta(days=n)
        if resources_wanted(cfg, d):
            out.append(d)
    return out


def handle_replies(cfg: Config, client: WembleyClient, state: State,
                   mailer, notifier) -> int:
    """Book anything you've said yes to by email. Returns how many were booked."""
    if mailer is None or not mailer.enabled:
        return 0
    replies = mailer.fetch_replies(since_minutes=cfg.scan.reply_window_minutes)
    booked = 0
    for token, yes in replies.items():
        pending = state.get_pending(token)
        if not pending:
            log.info("Reply %s has no matching pending slot (expired?).", token)
            continue
        if not yes:
            log.info("You declined %s.", token)
            state.drop_pending(token)
            continue

        date = dt.date.fromisoformat(pending["date"])
        hh, mm = pending["time"].split(":")
        want = dt.time(int(hh), int(mm))

        client.ensure_login()
        live = [s for s in client.fetch_slots(pending["resource"], date)
                if s.time == want]
        if not live:
            notifier.report(
                f"Too slow on {date:%a %d %b} {want:%I:%M %p}",
                "That tee time was taken before your reply came through.\n"
                "Still watching for the next one."
            )
            state.drop_pending(token)
            continue

        ok, detail = client.book(live[0], pending["players"])
        log.info(detail)
        if ok:
            state.mark_booked(live[0].key, detail)
            booked += 1
            notifier.report(f"Booked {date:%a %d %b} {want:%I:%M %p}", detail)
        else:
            notifier.report(f"Could not book {date:%a %d %b} {want:%I:%M %p}", detail)
        state.drop_pending(token)
        state.note(f"REPLY {token}: {detail}")
    return booked


def sweep(cfg: Config, client: WembleyClient, state: State, notifier) -> int:
    """One pass over every watched date. Returns number of new alerts/bookings."""
    actions = 0
    for date in dates_to_watch(cfg):
        if state.has_booking_on(date.isoformat()):
            continue

        slots = client.fetch_many(resources_wanted(cfg, date), date)
        if not slots:
            continue

        for hit in find_hits(slots, cfg, date):
            if state.already_alerted(hit.slot.key):
                continue

            if cfg.scan.mode == "auto":
                ok, detail = client.book(hit.slot, hit.target.players)
                log.info(detail)
                state.mark_alerted(hit.slot.key)
                if ok:
                    state.mark_booked(hit.slot.key, detail)
                    notifier.report(
                        f"Auto-booked {hit.slot.date:%a %d %b} {hit.slot.time:%I:%M %p}",
                        f"{detail}\n\nThis matched your pre-approved "
                        f"'{hit.target.label}' window, so I booked it rather than "
                        f"asking first.\n\nCancel through the Wembley site if you "
                        f"don't want it."
                    )
                    actions += 1
                    break  # one booking per date
            else:
                token = state.add_pending(hit, hit.target.players)
                if notifier.alert_slot(token, hit, hit.target.players):
                    state.mark_alerted(hit.slot.key)
                    state.note(f"ALERT {token} {hit.slot}")
                    actions += 1
                else:
                    state.drop_pending(token)
                break  # don't spam - one alert per date per cycle
    return actions


def upgrade_pass(cfg: Config, client: WembleyClient, state: State,
                 notifier) -> int:
    """
    Trade a booking you already hold up to a better one.

    This is the point of the whole scanner. The 6am job gets you *a* tee time;
    the cancellations that roll in over the next ten days are the good ones.
    Rather than sit on an 8:40 all week, move into the 7:10 when it frees up.

    The old booking is only released once the better slot has been confirmed
    to have room, so a failed upgrade leaves you where you were.
    """
    if not cfg.scan.upgrade:
        return 0

    try:
        held = client.bookings()
    except Exception as e:
        log.warning("Could not read your bookings, skipping upgrades: %s", e)
        return 0
    if not held:
        return 0

    today = now_perth().date()
    held = [b for b in held if b.date >= today and b.cancel_url]
    if not held:
        return 0

    # Only fetch sheets for dates we actually hold something on.
    slots = []
    for date in sorted({b.date for b in held}):
        if state.upgrades_on(date.isoformat()) >= cfg.scan.upgrade_max_per_date:
            continue
        slots.extend(client.fetch_many(resources_wanted(cfg, date), date))
    if not slots:
        return 0

    ups = find_upgrades(held, slots, cfg, cfg.scan.upgrade_min_gain)
    if not ups:
        return 0

    best = ups[0]
    date_iso = best.booking.date.isoformat()
    log.info("Upgrade available: %s", best)

    if cfg.dry_run:
        state.note(f"UPGRADE (practice) {best}")
        return 0

    # Confirm the target still has room immediately before giving up the old one.
    fresh = [s for s in client.fetch_slots(best.hit.slot.resource_key,
                                           best.booking.date)
             if s.time == best.hit.slot.time]
    if not fresh or fresh[0].free_places < best.hit.target.players:
        log.info("Upgrade slot filled before we moved. Keeping what we have.")
        return 0

    dropped_ok, dropped = client.cancel(best.booking.cancel_url)
    if not dropped_ok:
        log.warning("Could not release the old booking: %s", dropped)
        state.note(f"UPGRADE aborted, kept original: {dropped}")
        return 0

    ok, detail = client.book(fresh[0], best.hit.target.players)
    if ok:
        state.forget_booking(
            f"{best.booking.date}|{best.hit.slot.resource_key}|"
            f"{best.booking.time:%H:%M}")
        state.mark_booked(fresh[0].key, detail)
        state.mark_upgraded(date_iso)
        state.note(f"UPGRADE {best}")
        notifier.report(
            f"Moved up to {best.hit.slot.time:%I:%M %p} "
            f"{best.booking.date:%a %d %b}",
            f"Someone cancelled, so I traded your "
            f"{best.booking.time:%I:%M %p} for the "
            f"{best.hit.slot.time:%I:%M %p}.\n\n{detail}")
        return 1

    # Worst case: old one gone, new one failed. Say so loudly.
    log.error("Upgrade failed after cancelling: %s", detail)
    state.note(f"UPGRADE FAILED after cancel: {detail}")
    notifier.report(
        f"Lost your {best.booking.date:%a %d %b} booking",
        f"I released the {best.booking.time:%I:%M %p} to move you to the "
        f"{best.hit.slot.time:%I:%M %p}, but the new booking failed: {detail}\n\n"
        f"You currently have no tee time on that date. The scanner will keep "
        f"trying, or book one yourself.")
    return 0


def run(once: bool = False) -> int:
    cfg = Config.load()
    if not cfg.scan.enabled:
        log.info("Scanning disabled in config.yaml.")
        return 0

    now = now_perth()
    if not (cfg.scan.run_from <= now.time() <= cfg.scan.run_until):
        log.info("Outside scan hours (%s-%s). Exiting.",
                 cfg.scan.run_from, cfg.scan.run_until)
        return 0

    state = State()
    mailer = Mailer(cfg)
    notifier = Notifier(cfg, state, mailer)
    client = WembleyClient(cfg)

    try:
        client.login()
    except LoginFailed as e:
        log.error("Login failed: %s", e)
        notifier.report("Scanner login failed", str(e))
        return 1

    expired = state.expire_pending(cfg.scan.reply_window_minutes)
    if expired:
        log.info("Expired %d unanswered alert(s).", len(expired))

    deadline = time.monotonic() + (0 if once else RUN_SECONDS)
    cycle = 0
    while True:
        cycle += 1
        handle_replies(cfg, client, state, mailer, notifier)
        n = sweep(cfg, client, state, notifier)
        n += upgrade_pass(cfg, client, state, notifier)
        log.info("Cycle %d: %d action(s)", cycle, n)
        state.save()

        if once or time.monotonic() + cfg.scan.poll_seconds > deadline:
            break
        time.sleep(cfg.scan.poll_seconds)

    state.save()
    return 0


def _main(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except SetupIncomplete as e:
        log.error("%s", e)
        return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single sweep then exit")
    sys.exit(_main(run, once=ap.parse_args().once))
