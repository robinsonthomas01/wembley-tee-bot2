"""MiClub parsing, in Python.

Mirrors app/miclub.js exactly. Both are tested against tests/fixtures.json so
they cannot drift apart without a test going red.

The duplication is deliberate: the scheduled jobs run in Python on GitHub
Actions, and the desktop extension runs in JavaScript in Chrome. Neither can
call the other, and neither costs anything.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup  # re-exported for bot.queue

BASE = "https://www.wembleygolf.com.au"
BOOK_LINK = re.compile(
    r"(AddBooking|BookTime|MakeBooking|bookingId|teeTimeId"
    r"|booking_?slot|player|addPlayer|book_?me|book_?group)", re.I)

# MiClub's calendar is a grid: one row per course, one column per date. Each
# bookable cell links to an event page with its own booking_event_id, so a tee
# sheet is identified by event, not by course-plus-date.
EVENT_ID = re.compile(r"booking_?[Ee]vent_?[Ii]d=(\d+)")
RESOURCE_ID = re.compile(r"booking_?[Rr]esource_?[Ii]d=(\d+)")
DATE_HEAD = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\b")

_TAG = re.compile(r"<[^>]+>")
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\s*(AM|PM)?", re.I)
_PRICE = re.compile(r"\$\s*(\d+(?:\.\d{2})?)")
_HREF = re.compile(r"""<a[^>]+href=["']([^"']+)["']""", re.I)
_DATE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})"
    r"|\b(\d{1,2})[/\s-]([A-Za-z]{3,9}|\d{1,2})[/\s-](\d{2,4})\b"
)
_COURSE = re.compile(r"(?:OLD|TUART)\s+Course\s+\d+\s+Holes", re.I)
_COURSE_SHORT = re.compile(r"\b(?:OLD|TUART)\b", re.I)
_HIDDEN = re.compile(r"""<input[^>]+type=["']hidden["'][^>]*>""", re.I)
_INPUT = re.compile(r"<input[^>]+>", re.I)
_NAME = re.compile(r"""name=["']([^"']+)["']""", re.I)
_VALUE = re.compile(r"""value=["']([^"']*)["']""", re.I)
_FORM = re.compile(r"<form[^>]*>.*?</form>", re.I | re.S)
_ACTION = re.compile(r"""action=["']([^"']*)["']""", re.I)

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def strip(html: str) -> str:
    text = _TAG.sub(" ", html).replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


def to24h(h, m, ap) -> str:
    h = int(h)
    ap = (ap or "").upper()
    if ap == "PM" and h != 12:
        h += 12
    elif ap == "AM" and h == 12:
        h = 0
    return f"{h:02d}:{m}"


def normalise_date(m: re.Match) -> str | None:
    if m.group(1):
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    day = int(m.group(4))
    raw = m.group(5)
    mon = int(raw) if raw.isdigit() else MONTHS.get(raw[:3].lower())
    if not mon or mon > 12:
        return None
    yr = int(m.group(6))
    if yr < 100:
        yr += 2000
    return f"{yr}-{mon:02d}-{day:02d}"


def _rows(html: str) -> list[str]:
    return re.split(r"<tr[\s>]", html, flags=re.I)[1:]


def parse_timesheet(html: str) -> list[dict]:
    """Tee times with at least one free player place."""
    out: list[dict] = []
    for row in _rows(html):
        text = strip(row)
        t = _TIME.search(text)
        if not t:
            continue
        links = [urljoin(BASE, h) for h in _HREF.findall(row) if BOOK_LINK.search(h)]
        if not links:
            continue
        time = to24h(t.group(1), t.group(2), t.group(3))
        if any(s["time"] == time for s in out):
            continue
        price = _PRICE.search(text)
        out.append({
            "time": time,
            "freePlaces": len(links),
            "price": float(price.group(1)) if price else None,
            "bookUrls": links,
        })
    return sorted(out, key=lambda s: s["time"])


def parse_bookings(html: str) -> list[dict]:
    """Your booking list, including each row's cancel link."""
    out = []
    for row in _rows(html):
        text = strip(row)
        d = _DATE.search(text)
        t = _TIME.search(text)
        if not d or not t:
            continue
        cancel = None
        for href in _HREF.findall(row):
            if re.search(r"cancel|remove|delete", href + row, re.I):
                cancel = urljoin(BASE, href)
                break
        course = _COURSE.search(text) or _COURSE_SHORT.search(text)
        out.append({
            "date": normalise_date(d),
            "time": to24h(t.group(1), t.group(2), t.group(3)),
            "course": course.group(0).strip() if course else "",
            "cancelUrl": cancel,
            "raw": text[:160],
        })
    return out


def is_logged_in(html: str) -> bool:
    return bool(re.search(r"log\s?out|sign\s?out", html, re.I))


def looks_booked(html: str) -> bool:
    good = re.search(r"booking confirmed|successfully booked|your booking"
                     r"|booking complete|booking reference", html, re.I)
    bad = re.search(r"unable to|already booked|no longer available"
                    r"|not available", html, re.I)
    return bool(good) and not bool(bad)


def looks_cancelled(html: str) -> bool:
    good = re.search(r"cancelled|canceled|removed|deleted", html, re.I)
    bad = re.search(r"unable|error|cannot", html, re.I)
    return bool(good) and not bool(bad)


def hidden_fields(html: str) -> dict[str, str]:
    out = {}
    for tag in _HIDDEN.findall(html):
        name = _NAME.search(tag)
        if name:
            value = _VALUE.search(tag)
            out[name.group(1)] = value.group(1) if value else ""
    return out


def find_confirm_form(html: str, referer: str):
    """Return (method, action, data) for a confirm step, or None."""
    for form in _FORM.findall(html):
        if not re.search(r"confirm|book|accept|agree|submit", form, re.I):
            continue
        action_m = _ACTION.search(form)
        action = urljoin(BASE, action_m.group(1) if action_m else referer)
        data = {}
        for tag in _INPUT.findall(form):
            name = _NAME.search(tag)
            if name:
                value = _VALUE.search(tag)
                data[name.group(1)] = value.group(1) if value else ""
        method = "POST" if re.search(r"""method=["']post["']""", form, re.I) else "GET"
        return method, action, data
    return None


_CAL_DATE = re.compile(r"selectedDate=(\d{4}-\d{2}-\d{2})")


def parse_calendar_dates(html: str) -> list[str]:
    """Every date the booking calendar currently links to, earliest first."""
    return sorted(set(_CAL_DATE.findall(html)))


def _cells(row_html: str) -> list[str]:
    """Split a row into cells, coping with either table or div layouts."""
    cells = re.split(r"<t[dh][\s>]", row_html, flags=re.I)[1:]
    if len(cells) > 1:
        return cells
    return re.split(r'<div[^>]*class="[^"]*(?:cell|day|col)[^"]*"', row_html,
                    flags=re.I)[1:]


def parse_calendar(html: str, year: int) -> dict:
    """
    Turn the booking calendar into {course_label: {date: event_id or None}}.

    Cells with no link are full sheets; they still take up a column, so they
    have to be counted to keep the dates lined up.
    """
    rows = re.split(r"<tr[\s>]", html, flags=re.I)[1:]
    dates: list[str] = []
    grid: dict[str, dict[str, str | None]] = {}

    for row in rows:
        cells = _cells(row)
        if not cells:
            continue
        texts = [strip(c) for c in cells]

        # The header row is the one whose cells read like "19 Aug".
        found_dates = []
        for t in texts:
            m = DATE_HEAD.search(t)
            found_dates.append(_iso(m, year) if m else None)
        if not dates and sum(1 for d in found_dates if d) >= 3:
            dates = found_dates
            continue

        course = next((t for t in texts
                       if re.search(r"(old|tuart).*(hole|course)|course.*hole",
                                    t, re.I)), None)
        if not course or not dates:
            continue

        row_map: dict[str, str | None] = {}
        for i, cell in enumerate(cells):
            if i >= len(dates) or not dates[i]:
                continue
            ev = EVENT_ID.search(cell)
            row_map[dates[i]] = ev.group(1) if ev else None
        if row_map:
            grid[" ".join(course.split())] = row_map

    return grid


def _iso(m: re.Match, year: int) -> str | None:
    day = int(m.group(1))
    mon = MONTHS.get(m.group(2)[:3].lower())
    if not mon:
        return None
    return f"{year}-{mon:02d}-{day:02d}"


# ---------------------------------------------------------------------------
# Calendar grid
#
# Wembley's calendar is a table: one row per course, one column per date.
# Each bookable cell links to an event page carrying its own booking_event_id.
# There is no per-course resource id - booking_resource_id=3000000 is a MiClub
# constant shared by every club on the platform.
# ---------------------------------------------------------------------------

EVENT_ID = re.compile(r"booking_?[Ee]vent_?[Ii]d=(\d+)")
RESOURCE_ID = re.compile(r"booking_?[Rr]esource_?[Ii]d=(\d+)")
COURSE_ROW = re.compile(r"(?:old|tuart).*?(?:hole|course)|course.*?hole", re.I)

_MONTH_NAMES = "|".join(MONTHS)
HEADER_DATE = re.compile(
    rf"(?:(\d{{1,2}})\s*({_MONTH_NAMES})|({_MONTH_NAMES})\s*(\d{{1,2}}))", re.I)


def header_to_date(text: str, today: "dt.date") -> "dt.date | None":
    """Turn a column heading like 'Wed 19 Aug' or 'Today' into a real date."""
    import datetime as dt
    t = " ".join(text.split())
    if re.search(r"\btoday\b", t, re.I):
        return today
    if re.search(r"\btomorrow\b", t, re.I):
        return today + dt.timedelta(days=1)
    m = HEADER_DATE.search(t)
    if not m:
        return None
    day = int(m.group(1) or m.group(4))
    mon = MONTHS[(m.group(2) or m.group(3))[:3].lower()]
    year = today.year
    # A calendar only ever looks forward, so roll the year at December.
    if mon < today.month - 1:
        year += 1
    try:
        return dt.date(year, mon, day)
    except ValueError:
        return None


def parse_calendar(html: str, today: "dt.date") -> dict:
    """
    Read the calendar into {course label: {date: event_id}}.

    Falls back to positional matching within each row when the table markup
    isn't clean, and reports what it managed either way.
    """
    soup = BeautifulSoup(html, "html.parser")
    grid: dict[str, dict] = {}
    dates: list = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        # Column headings -> dates. The first column is the course name.
        head_cells = rows[0].find_all(["th", "td"])
        col_dates = [header_to_date(c.get_text(" ", strip=True), today)
                     for c in head_cells]
        # One date column is enough - the last week of the calendar often has
        # only one. What matters is that some row names a course.
        if not any(col_dates):
            continue
        if not any(COURSE_ROW.search(r.get_text(" ", strip=True))
                   for r in rows[1:]):
            continue
        dates = [d for d in col_dates if d]

        for row in rows[1:]:
            cells = row.find_all(["th", "td"])
            if not cells:
                continue
            course = ""
            for c in cells:
                t = " ".join(c.get_text().split())
                if COURSE_ROW.search(t):
                    course = t
                    break
            if not course:
                continue

            per_date = {}
            for idx, cell in enumerate(cells):
                if idx >= len(col_dates):
                    break
                date = col_dates[idx]
                if not date:
                    continue
                link = cell.find("a", href=True)
                if not link:
                    continue
                ev = EVENT_ID.search(link["href"])
                if ev:
                    per_date[date.isoformat()] = {
                        "event_id": ev.group(1),
                        "href": urljoin(BASE, link["href"]),
                        "price": _price(cell.get_text(" ", strip=True)),
                    }
            if per_date:
                grid[course] = {**grid.get(course, {}), **per_date}

    return {"dates": [d.isoformat() for d in dates], "courses": grid}


def _price(text: str):
    m = _PRICE.search(text)
    return float(m.group(1)) if m else None


def calendar_events(html: str) -> list[dict]:
    """Every event link on the page, for when the grid can't be read."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        ev = EVENT_ID.search(a["href"])
        if not ev:
            continue
        course = ""
        node = a
        for _ in range(6):
            node = node.parent
            if node is None:
                break
            for tag in ("h1", "h2", "h3", "h4", "h5", "th", "strong"):
                for h in node.find_all(tag):
                    t = " ".join(h.get_text().split())
                    if COURSE_ROW.search(t):
                        course = t
                        break
                if course:
                    break
            if course:
                break
        out.append({"event_id": ev.group(1), "course": course,
                    "href": urljoin(BASE, a["href"]),
                    "price": _price(a.get_text(" ", strip=True))})
    return out
