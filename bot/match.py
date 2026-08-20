"""Match tee times against your standing preferences and rank them."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from .client import Slot, Booking
from .config import Config, Target


@dataclass
class Hit:
    slot: Slot
    target: Target
    score: float

    def __str__(self) -> str:
        return f"{self.slot}  [{self.target.label}]"


def _midpoint(t: Target) -> float:
    e = t.earliest.hour * 60 + t.earliest.minute
    l = t.latest.hour * 60 + t.latest.minute
    return (e + l) / 2


def _ideal(t: Target) -> float:
    """
    The tee time you actually want inside the window.

    Defaults to the middle, but `ideal: "07:10"` in a target overrides it —
    useful when a window is wide but you have a clear favourite.
    """
    if t.ideal:
        return t.ideal.hour * 60 + t.ideal.minute
    return _midpoint(t)


def score(slot: Slot, target: Target) -> float:
    """
    Lower is better.

    Ranking is: target priority first, then how close the tee time sits to the
    middle of your window, with a bonus for sheets that can seat your whole
    group in one go.
    """
    return _score_parts(slot.time, slot.free_places, target)


def _score_parts(time: dt.time, free_places: int, target: Target) -> float:
    mins = time.hour * 60 + time.minute
    distance = abs(mins - _ideal(target)) / 60.0
    shortfall = max(0, target.players - free_places) * 3.0
    return target.priority + distance + shortfall


def find_hits(slots: list[Slot], cfg: Config,
              date: dt.date | None = None) -> list[Hit]:
    """Every (slot, target) pair that satisfies a preference, best first."""
    hits: list[Hit] = []
    for slot in slots:
        d = date or slot.date
        for target in cfg.active_targets():
            if slot.resource_key not in target.resources:
                continue
            if not target.matches_date(d):
                continue
            if not target.matches_time(slot.time):
                continue
            if slot.free_places < 1:
                continue
            if target.max_price and slot.price and slot.price > target.max_price:
                continue
            hits.append(Hit(slot=slot, target=target, score=score(slot, target)))
            break  # highest-priority matching target wins this slot
    return sorted(hits, key=lambda h: h.score)


def resources_wanted(cfg: Config, date: dt.date) -> list[str]:
    """Which sheets are worth fetching for this date at all."""
    keys: list[str] = []
    for t in cfg.active_targets():
        if t.matches_date(date):
            keys.extend(k for k in t.resources if k not in keys)
    return keys


# --------------------------------------------------------------- upgrading
COURSE_KEYS = {
    ("old", "18"): "OLD_18", ("tuart", "18"): "TUART_18",
    ("old", "9"): "OLD_9", ("tuart", "9"): "TUART_9",
}


def booking_resource_key(course: str) -> str | None:
    """Map 'TUART Course 18 Holes' back to the TUART_18 sheet key."""
    low = (course or "").lower()
    name = "tuart" if "tuart" in low else ("old" if "old" in low else None)
    if not name:
        return None
    holes = "9" if re.search(r"\b9\b", low) else "18"
    return COURSE_KEYS.get((name, holes))


def target_for_booking(b: Booking, cfg: Config) -> Target | None:
    """Which window, if any, does an existing booking belong to?"""
    key = booking_resource_key(b.course)
    for t in cfg.active_targets():
        if not t.matches_date(b.date):
            continue
        if key and key not in t.resources:
            continue
        return t
    return None


def score_booking(b: Booking, target: Target) -> float:
    """
    Score a booking you already hold, so it can be compared against open slots.

    Assumes the booking already seats your group — you booked it — so there is
    no shortfall penalty. That deliberately makes a held booking hard to beat.
    """
    return _score_parts(b.time, target.players, target)


@dataclass
class Upgrade:
    booking: Booking
    hit: Hit
    gain: float

    def __str__(self) -> str:
        return (f"{self.booking.time:%I:%M %p} -> {self.hit.slot.time:%I:%M %p} "
                f"on {self.booking.date:%a %d %b} (better by {self.gain:.1f})")


def find_upgrades(bookings: list[Booking], slots: list[Slot], cfg: Config,
                  min_gain: float = 0.5) -> list[Upgrade]:
    """
    Open slots that beat a booking you already hold, best first.

    Only compares within the same window, so it will never move you from a
    Saturday you wanted to a Sunday you didn't.
    """
    out: list[Upgrade] = []
    for b in bookings:
        target = target_for_booking(b, cfg)
        if target is None:
            continue
        current = score_booking(b, target)

        for slot in slots:
            if slot.date != b.date:
                continue
            if slot.resource_key not in target.resources:
                continue
            if slot.free_places < target.players:
                continue          # no point moving into a slot that can't seat you
            if not target.matches_time(slot.time):
                continue
            gain = current - score(slot, target)
            if gain >= min_gain:
                out.append(Upgrade(booking=b,
                                   hit=Hit(slot=slot, target=target,
                                           score=score(slot, target)),
                                   gain=gain))
    return sorted(out, key=lambda u: -u.gain)
