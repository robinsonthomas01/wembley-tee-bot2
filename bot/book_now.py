#!/usr/bin/env python3
"""
Book one specific pending slot, now.

Triggered by the app's "Book it" button via workflow_dispatch. The app passes
the alert token; we look it up in state.json, re-check the slot is still open,
and take it.

    python -m bot.book_now --token 7fa2
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from .client import WembleyClient, LoginFailed
from .config import Config
from .notify import Mailer
from .push import Notifier
from .state import State

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("wgc.book")


def run(token: str, decline: bool = False) -> int:
    cfg = Config.load()
    state = State()
    notifier = Notifier(cfg, state, Mailer(cfg))

    pending = state.get_pending(token)
    if not pending:
        log.error("No pending slot for token %s (already handled, or expired).", token)
        return 0

    if decline:
        state.drop_pending(token)
        state.note(f"PASS {token}")
        state.save()
        log.info("Passed on %s.", token)
        return 0

    date = dt.date.fromisoformat(pending["date"])
    hh, mm = pending["time"].split(":")
    want = dt.time(int(hh), int(mm))
    label = f"{date:%a %-d %b} {want:%-I:%M %p}"

    client = WembleyClient(cfg)
    try:
        client.login()
    except LoginFailed as e:
        state.note(f"BOOK {token} login failed")
        state.save()
        notifier.report("Login failed", f"Could not book {label}: {e}")
        return 1

    live = [s for s in client.fetch_slots(pending["resource"], date) if s.time == want]
    if not live:
        state.drop_pending(token)
        state.note(f"BOOK {token} gone")
        state.save()
        notifier.report(f"Gone: {label}",
                        "Someone took that tee time first. Still watching.")
        return 0

    ok, detail = client.book(live[0], pending["players"])
    log.info(detail)
    state.drop_pending(token)
    if ok:
        state.mark_booked(live[0].key, detail)
        notifier.report(f"Booked {label}", detail)
    else:
        notifier.report(f"Could not book {label}", detail)
    state.note(f"BOOK {token}: {detail}")
    state.save()
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True)
    ap.add_argument("--decline", action="store_true")
    a = ap.parse_args()
    sys.exit(run(a.token, a.decline))
