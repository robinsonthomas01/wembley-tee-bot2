#!/usr/bin/env python3
"""
On-demand actions from the phone app: book, cancel, move, or refresh.

The app fires this through workflow_dispatch because a phone browser can't
reach Wembley itself. Takes roughly 25 seconds end to end.

    python -m bot.act --do book --resource TUART_18 --date 2026-08-29 --time 07:20 --players 4
    python -m bot.act --do cancel --cancel-url https://...
    python -m bot.act --do move --cancel-url https://... --resource OLD_18 --date ... --time ...
    python -m bot.act --do refresh
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from .client import WembleyClient, LoginFailed, Slot
from .config import Config, SetupIncomplete
from .notify import Mailer
from .push import Notifier
from .state import State
from . import snapshot

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("wgc.act")


def _t(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def run(a) -> int:
    cfg = Config.load()
    state = State()
    notifier = Notifier(cfg, state, Mailer(cfg))
    client = WembleyClient(cfg)

    try:
        client.ensure_login()
    except LoginFailed as e:
        state.note(f"ACT {a.do} login failed"); state.save()
        notifier.report("Login failed", str(e))
        return 1

    result_ok, detail = False, ""

    if a.do == "refresh":
        snapshot.run(client=client)
        state.note("ACT refresh"); state.save()
        return 0

    if a.do == "cancel":
        result_ok, detail = client.cancel(a.cancel_url)

    elif a.do in ("book", "move"):
        date = dt.date.fromisoformat(a.date)
        want = _t(a.time)
        live = [s for s in client.fetch_slots(a.resource, date) if s.time == want]
        if not live or live[0].free_places < a.players:
            detail = "That tee time no longer has room."
            if a.do == "move":
                detail += " Your existing booking is untouched."
            log.info(detail)
            notifier.report("Too slow", detail)
            state.note(f"ACT {a.do} gone"); state.save()
            snapshot.run(client=client)
            return 0

        if a.do == "move":
            # Only give up the old booking once the new one is confirmed available.
            dropped_ok, dropped = client.cancel(a.cancel_url)
            if not dropped_ok:
                detail = f"Kept your original booking - {dropped}"
                notifier.report("Move failed", detail)
                state.note(f"ACT move {detail}"); state.save()
                return 0
            result_ok, made = client.book(live[0], a.players)
            detail = (f"Moved to {want:%I:%M %p} on {date:%a %d %b}" if result_ok
                      else f"Old booking cancelled but the new one failed: {made}. "
                           f"Book again from the Play tab.")
        else:
            result_ok, detail = client.book(live[0], a.players)
            if result_ok:
                state.mark_booked(live[0].key, detail)

    if a.token:
        state.drop_pending(a.token)

    log.info(detail)
    state.note(f"ACT {a.do}: {detail}")
    state.save()
    notifier.report("Booked" if result_ok else "Could not complete that", detail)

    # Leave the phone app looking at fresh data.
    try:
        snapshot.run(client=client)
    except Exception as e:
        log.warning("Snapshot after action failed: %s", e)
    return 0 if result_ok else 1


def _main(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except SetupIncomplete as e:
        log.error("%s", e)
        return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--do", required=True,
                    choices=["book", "cancel", "move", "refresh"])
    ap.add_argument("--resource"); ap.add_argument("--date"); ap.add_argument("--time")
    ap.add_argument("--players", type=int, default=1)
    ap.add_argument("--cancel-url", dest="cancel_url")
    ap.add_argument("--token", default="")
    sys.exit(_main(run, ap.parse_args()))
