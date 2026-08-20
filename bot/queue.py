"""
The 6am queue.

Wembley runs a first-come queue, not MiClub's random lottery. The registration
window opens a few minutes before 6am and your position is decided by when you
join it. So unlike a draw, arriving early genuinely wins - a lower position
means you reach the tee sheet while the good times are still there.

Which makes the job:

  1. Watch for the queue to open, from well before it's expected.
  2. Join within a second or two of it appearing.
  3. Hold position, and book the moment you're admitted.

Join once and only once. Repeatedly entering is what clubs police, and it
gains nothing here anyway - your position is already fixed.
"""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import urljoin

from . import miclub as M

log = logging.getLogger("wgc.queue")

# What the join control tends to be called.
JOIN_TEXT = re.compile(r"join\s*(the\s*)?(draw|lottery|queue|ballot)"
                       r"|enter\s*(the\s*)?(draw|lottery|ballot)"
                       r"|register\s*for\s*(the\s*)?draw", re.I)
JOIN_HREF = re.compile(r"(join|enter|register).*(draw|lottery|queue|ballot)"
                       r"|lottery|ballot", re.I)

# Signs we're in the waiting room rather than on the tee sheet.
WAITING = re.compile(r"current position|your position|place in the queue"
                     r"|waiting room|you are in the queue|position in the draw"
                     r"|draw closes|timesheet opens in", re.I)
JOINED = re.compile(r"you (have|are) (joined|entered|in the draw)"
                    r"|successfully (joined|entered)|you're in the draw"
                    r"|ticket (number|allocated)", re.I)
POSITION = re.compile(r"(?:current position|your position|position)\D{0,20}(\d{1,4})", re.I)


def find_join_control(html: str, page_url: str):
    """
    Locate the 'Join Draw' control on a fixture or event page.

    Returns ('link', url) or ('form', method, action, fields), or None when
    this club doesn't run a draw for this sheet.
    """
    soup = M.BeautifulSoup(html, "html.parser")

    for a in soup.find_all("a", href=True):
        label = " ".join(a.get_text().split())
        if JOIN_TEXT.search(label) or JOIN_HREF.search(a["href"]):
            return ("link", urljoin(page_url, a["href"]), label)

    for form in soup.find_all("form"):
        blob = " ".join(form.get_text().split())
        submits = [i.get("value", "") for i in form.find_all("input")
                   if (i.get("type") or "").lower() == "submit"]
        blob += " " + " ".join(submits) + " " + (form.get("action") or "")
        if not (JOIN_TEXT.search(blob) or JOIN_HREF.search(blob)):
            continue
        fields = {i.get("name"): i.get("value", "")
                  for i in form.find_all("input") if i.get("name")}
        method = (form.get("method") or "post").lower()
        action = urljoin(page_url, form.get("action") or page_url)
        return ("form", method, action, fields)

    # Some skins wire it up in JavaScript.
    for m in re.findall(r"""(?:href|onclick)=["']([^"']*)["']""", html, re.I):
        if JOIN_HREF.search(m):
            url = re.search(r"""['"]?(/[^'"()]+\.(?:msp|xsp)[^'"()]*)""", m)
            if url:
                return ("link", urljoin(page_url, url.group(1)), "javascript")
    return None


def in_waiting_room(html: str) -> bool:
    return bool(WAITING.search(html)) and not M.parse_timesheet(html)


def queue_position(html: str) -> int | None:
    m = POSITION.search(html)
    return int(m.group(1)) if m else None


def already_joined(html: str) -> bool:
    return bool(JOINED.search(html))


def has_teesheet(html: str) -> bool:
    """Admitted - the actual tee times are on the page."""
    return bool(M.parse_timesheet(html))


# What a page can be telling us at any given moment.
NOT_OPEN = "not_open"       # queue hasn't started yet - keep watching
OPEN = "open"               # join control is live - go now
QUEUED = "queued"           # we're in, holding a position
SHEET = "sheet"             # admitted; tee times are visible
UNKNOWN = "unknown"


def probe(client, page_url: str) -> tuple[str, str]:
    """
    One cheap look at the page. Returns (state, detail).

    Used by the watch loop, which polls this every second or two in the
    run-up so we can join within moments of the queue opening.
    """
    try:
        r = client.s.get(page_url, timeout=15)
        r.raise_for_status()
    except Exception as e:
        return UNKNOWN, f"fetch failed: {e}"

    html = r.text
    if has_teesheet(html):
        return SHEET, "tee sheet is open"
    if already_joined(html) or in_waiting_room(html):
        pos = queue_position(html)
        return QUEUED, f"in the queue{f', position {pos}' if pos else ''}"
    if find_join_control(html, r.url or page_url):
        return OPEN, "queue is open"
    return NOT_OPEN, "queue not open yet"


def watch_and_join(client, pages: dict, state, open_at, watch_minutes: int = 25,
                   poll_seconds: float = 4.0, sprint_minutes: int = 12,
                   sprint_seconds: float = 1.0, now_fn=None) -> dict:
    """
    Poll until the queue opens, then join immediately.

    Position is arrival order, so this is the part that actually matters.
    Polls gently from `watch_minutes` out, then every second from
    `sprint_minutes` out until the queue appears.

    `pages` is {tag: url}. Returns {tag: state}.
    """
    import datetime as dt
    from .config import now_perth
    now_fn = now_fn or now_perth

    outcome = {}
    pending = dict(pages)
    start = open_at - dt.timedelta(minutes=watch_minutes)
    sprint = open_at - dt.timedelta(minutes=sprint_minutes)

    # Anything already joined earlier this morning stays joined.
    for tag in list(pending):
        if state.data.setdefault("draws", {}).get(tag):
            outcome[tag] = QUEUED
            pending.pop(tag)
            log.info("Already in the queue for %s.", tag)

    polls = 0
    while pending:
        now = now_fn()
        if now < start:
            return outcome           # caller is responsible for the long wait
        if now >= open_at:
            log.info("6am reached with %d sheet(s) never showing a queue - "
                     "they probably don't use one.", len(pending))
            for tag in pending:
                outcome[tag] = NOT_OPEN
            break

        polls += 1
        for tag, url in list(pending.items()):
            st, detail = probe(client, url)

            if st == OPEN:
                ok, why = join(client, url)
                log.info("Queue opened for %s - joined: %s", tag, why)
                if ok:
                    state.data.setdefault("draws", {})[tag] = True
                    outcome[tag] = QUEUED
                    pending.pop(tag)
                continue

            if st in (QUEUED, SHEET):
                log.info("%s: %s", tag, detail)
                state.data.setdefault("draws", {})[tag] = True
                outcome[tag] = st
                pending.pop(tag)
                continue

            if polls == 1 or polls % 30 == 0:
                log.info("%s: %s (%.0f min to go)", tag, detail,
                         (open_at - now).total_seconds() / 60)

        if pending:
            time.sleep(sprint_seconds if now >= sprint else poll_seconds)

    return outcome


def join(client, page_url: str) -> tuple[bool, str]:
    """
    Enter the draw. Once, and only once.

    Returns (joined, explanation). A False with "no draw" means this sheet
    isn't using a lottery at all, which is fine - the caller just books
    normally when the sheet opens.
    """
    try:
        r = client.s.get(page_url, timeout=25)
        r.raise_for_status()
    except Exception as e:
        return False, f"could not open the page: {e}"

    if already_joined(r.text):
        return True, "already in the draw"
    if has_teesheet(r.text):
        return False, "sheet is already open, no draw needed"

    control = find_join_control(r.text, r.url or page_url)
    if not control:
        if in_waiting_room(r.text):
            return True, "waiting room, but no join control - already in it"
        return False, "no draw on this page"

    try:
        if control[0] == "link":
            _, url, label = control
            log.info("Joining the draw via %r", label or url)
            res = client.s.get(url, timeout=25, headers={"Referer": page_url})
        else:
            _, method, action, fields = control
            log.info("Joining the draw by %s to %s", method.upper(), action)
            res = (client.s.post(action, data=fields, timeout=25,
                                 headers={"Referer": page_url})
                   if method == "post"
                   else client.s.get(action, params=fields, timeout=25,
                                     headers={"Referer": page_url}))
        res.raise_for_status()
    except Exception as e:
        return False, f"join request failed: {e}"

    if already_joined(res.text) or in_waiting_room(res.text):
        pos = queue_position(res.text)
        return True, f"in the draw{f', position {pos}' if pos else ''}"
    if has_teesheet(res.text):
        return True, "admitted straight to the tee sheet"
    return False, "join submitted but nothing confirmed it"
