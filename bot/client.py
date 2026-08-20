"""Direct MiClub client for the scheduled jobs.

Runs on the GitHub Actions runner, which has no CORS restrictions. Parsing
lives in bot/miclub.py, shared with nothing - its JavaScript twin is
app/miclub.js, and tests/fixtures.json keeps the two honest.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass

import requests

from . import miclub as M
from .config import SetupIncomplete, now_perth

log = logging.getLogger("wgc.client")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


@dataclass
class Slot:
    time: dt.time
    date: dt.date
    resource_key: str
    free_places: int
    book_urls: list[str] | None = None
    price: float | None = None

    @property
    def key(self) -> str:
        return f"{self.date}|{self.resource_key}|{self.time:%H:%M}"

    def __str__(self) -> str:
        p = f" ${self.price:.0f}" if self.price else ""
        return (f"{self.date:%a %d %b} {self.time:%I:%M %p} "
                f"{self.resource_key} ({self.free_places} free{p})")


@dataclass
class Booking:
    date: dt.date
    time: dt.time
    course: str
    cancel_url: str | None

    def __str__(self) -> str:
        return f"{self.date:%a %d %b} {self.time:%I:%M %p} {self.course}"


class LoginFailed(RuntimeError):
    pass


def _time(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


class WembleyClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.site = cfg.site
        self.base = self.site["base_url"].rstrip("/")
        self.root = self.site.get("booking_root", "/members/bookings/")
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-AU,en;q=0.9"})
        self._in = False
        self._cal_cache: dict[str, dict] = {}

    # ---------------------------------------------------------------- login
    def login(self) -> None:
        lg = self.site["login"]
        url = f"{self.base}/security/login.msp"
        page = self.s.get(url, timeout=25)

        payload = M.hidden_fields(page.text)
        payload.update(self.site["login"].get("hidden") or {})
        payload[lg["user_field"]] = self.cfg.username
        payload[lg["pass_field"]] = self.cfg.password

        r = self.s.post(lg.get("action") or url, data=payload, timeout=25,
                        headers={"Referer": url})
        r.raise_for_status()

        if re.search(r"invalid|incorrect|not recognised", r.text, re.I):
            raise LoginFailed("Wembley rejected those credentials")

        if M.is_logged_in(r.text):
            self._in = True
            log.info("Logged in as %s", self.cfg.username)
            return

        # MiClub often answers the login POST with a short redirect stub. The
        # session cookie is already set, so confirm against a real page rather
        # than treating the stub as a failure.
        for url_ in (f"{self.base}{self.root}ViewPublicCalendar.msp",
                     f"{self.base}/members/index.msp"):
            try:
                probe = self.s.get(url_, timeout=25)
            except requests.RequestException:
                continue
            if M.is_logged_in(probe.text):
                self._in = True
                log.info("Logged in as %s", self.cfg.username)
                return

        raise LoginFailed(
            "Login did not complete. Run setup again and check the log - "
            "your username is your membership number with leading zeros "
            "removed (00035 becomes 35)."
        )

    def ensure_login(self) -> None:
        if not self._in:
            self.login()

    # ------------------------------------------------------------- calendar
    def calendar_url(self, date: dt.date | None = None) -> str:
        page = self.site.get("calendar_page") or (self.root + "ViewPublicCalendar.msp")
        url = (f"{self.base}{page}"
               f"?booking_resource_id={self.site.get('resource_id', '3000000')}")
        if date:
            url += f"&selectedDate={date.isoformat()}"
        return url

    def calendar(self, date: dt.date | None = None) -> dict:
        """
        The calendar grid for the week containing `date`.

        Cached per week, because the 6am job reads it repeatedly while it
        hammers for a newly opened sheet.
        """
        key = date.isoformat() if date else "current"
        if key in self._cal_cache:
            return self._cal_cache[key]

        self.ensure_login()
        try:
            r = self.s.get(self.calendar_url(date), timeout=25)
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning("calendar fetch failed (%s): %s", key, e)
            return {"dates": [], "courses": {}}

        grid = M.parse_calendar(r.text, now_perth().date())
        if not grid["courses"]:
            # Grid unreadable - fall back to loose event links so we degrade
            # to "something bookable exists" rather than to nothing.
            loose = M.calendar_events(r.text)
            log.warning("Could not read the calendar grid; %d loose event "
                        "link(s) found.", len(loose))
            grid = {"dates": [], "courses": {}, "loose": loose}

        self._cal_cache[key] = grid
        return grid

    def course_label(self, key: str) -> str:
        courses = self.site.get("courses") or {}
        if key in courses:
            return courses[key]
        raise SetupIncomplete(
            f"No course called {key}. Known: {', '.join(courses) or 'none'}. "
            f"Run the Set up job again."
        )

    def event_for(self, key: str, date: dt.date) -> dict | None:
        """The event (one course, one day) behind a calendar cell."""
        label = self.course_label(key)
        grid = self.calendar(date)
        by_date = grid.get("courses", {}).get(label)
        if not by_date:
            # Course labels can drift slightly; match loosely before giving up.
            for name, days in grid.get("courses", {}).items():
                if name.lower().strip() == label.lower().strip():
                    by_date = days
                    break
        if not by_date:
            return None
        return by_date.get(date.isoformat())

    # ------------------------------------------------------------ timesheet
    def fetch_slots(self, key: str, date: dt.date) -> list[Slot]:
        event = self.event_for(key, date)
        if not event:
            log.info("%s on %s: nothing bookable on the calendar", key, date)
            return []
        try:
            r = self.s.get(event["href"], timeout=25)
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning("event fetch failed %s %s: %s", key, date, e)
            return []

        slots = M.parse_timesheet(r.text)
        log.info("%s on %s (event %s): %d tee time(s) with room",
                 key, date, event["event_id"], len(slots))
        if not slots:
            # Worth knowing whether the sheet was full or we simply couldn't
            # read it, so say which.
            full = "timesheet full" in r.text.lower()
            log.info("   %s", "sheet is full" if full else
                     f"no bookable links found in {len(r.text):,} bytes")
        return [
            Slot(time=_time(s["time"]), date=date, resource_key=key,
                 free_places=s["freePlaces"], book_urls=s["bookUrls"],
                 price=s["price"] or event.get("price"))
            for s in slots
        ]

    def fetch_many(self, keys: list[str], date: dt.date) -> list[Slot]:
        out: list[Slot] = []
        for k in keys:
            out.extend(self.fetch_slots(k, date))
        return out

    # -------------------------------------------------------------- booking
    def book(self, slot: Slot, players: int) -> tuple[bool, str]:
        if self.cfg.dry_run:
            return True, f"Practice run - would book {players}x {slot}"
        self.ensure_login()
        event = self.event_for(slot.resource_key, slot.date)
        ref = event["href"] if event else self.calendar_url(slot.date)
        taken, notes = 0, []

        for n in range(players):
            # Re-read between clicks so we never fire at a square just taken.
            fresh = [s for s in self.fetch_slots(slot.resource_key, slot.date)
                     if s.time == slot.time]
            if not fresh or not fresh[0].book_urls:
                notes.append(f"place {n + 1}: gone")
                break
            r = self.s.get(fresh[0].book_urls[0], timeout=25,
                           headers={"Referer": ref})
            form = M.find_confirm_form(r.text, fresh[0].book_urls[0])
            if form:
                method, action, data = form
                r = (self.s.post(action, data=data, timeout=25) if method == "POST"
                     else self.s.get(action, params=data, timeout=25))
            if M.looks_booked(r.text):
                taken += 1
            else:
                notes.append(f"place {n + 1}: not confirmed")
                break

        if taken == 0:
            return False, f"Could not book {slot}. {'; '.join(notes)}"
        if taken < players:
            return True, f"Booked {taken} of {players} at {slot}. {'; '.join(notes)}"
        return True, f"Booked {players} place(s) at {slot}."

    # ------------------------------------------------------------- bookings
    def bookings(self) -> list[Booking]:
        self.ensure_login()
        r = self.s.get(f"{self.base}{self.root}MyBookings.msp", timeout=25)
        out = []
        for b in M.parse_bookings(r.text):
            if not b["date"]:
                continue
            out.append(Booking(date=dt.date.fromisoformat(b["date"]),
                               time=_time(b["time"]), course=b["course"],
                               cancel_url=b["cancelUrl"]))
        return out

    def cancel(self, cancel_url: str) -> tuple[bool, str]:
        if self.cfg.dry_run:
            return True, "Practice run - not cancelled"
        self.ensure_login()
        r = self.s.get(cancel_url, timeout=25)
        form = M.find_confirm_form(r.text, cancel_url)
        if form:
            method, action, data = form
            r = (self.s.post(action, data=data, timeout=25) if method == "POST"
                 else self.s.get(action, params=data, timeout=25))
        ok = M.looks_cancelled(r.text)
        return ok, "Booking cancelled" if ok else "Could not cancel - check the site"
