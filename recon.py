#!/usr/bin/env python3
"""
recon.py - RUN THIS FIRST, ON YOUR OWN MACHINE.

Wembley Golf Course runs MiClub. Every MiClub site uses the same page names
(.msp endpoints) but each club has its own booking-resource IDs, form field
names, and booking-link format. This script logs in once, walks the booking
pages, and writes everything the bot needs into `site.yaml`.

It changes nothing on the site. It does not book. It only reads and dumps.

Usage:
    pip install -r requirements.txt
    export WGC_USERNAME='35'           # your member number, no leading zeros
    export WGC_PASSWORD='...'
    python recon.py

Output:
    site.yaml           <- commit this
    recon_dump/*.html   <- raw pages, for eyeballing. DO NOT COMMIT.
"""
from __future__ import annotations

import os
import re
import sys
import datetime as dt
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import requests
import yaml
from bs4 import BeautifulSoup

BASE = "https://www.wembleygolf.com.au"
DUMP = Path("recon_dump")
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# MiClub serves near-identical pages under /guests/ and /members/.
# Members generally get earlier access and member pricing, so we prefer it.
CANDIDATE_ROOTS = ["/members/bookings/", "/guests/bookings/"]


def banner(msg: str) -> None:
    print(f"\n{'=' * 70}\n{msg}\n{'=' * 70}")


def save(name: str, text: str) -> None:
    try:
        DUMP.mkdir(exist_ok=True)
        (DUMP / name).write_text(text, encoding="utf-8")
        print(f"  wrote recon_dump/{name}  ({len(text):,} bytes)")
    except OSError as e:
        print(f"  (could not save {name}: {e})")


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-AU,en;q=0.9"})
    return s


# --------------------------------------------------------------------------
# 1. Login
# --------------------------------------------------------------------------
CHALLENGE = re.compile(
    r"attention required|cf-browser-verification|just a moment"
    r"|checking your browser|enable javascript and cookies", re.I)


def _is_challenge(html: str) -> bool:
    """A real bot-check page, not merely a page that mentions a CDN."""
    return bool(CHALLENGE.search(html)) and len(html) < 60_000


def inspect_login_form(session: requests.Session) -> dict:
    """Find the login form and report its real field names."""
    url = urljoin(BASE, "/security/login.msp")
    try:
        r = session.get(url, timeout=30)
    except requests.RequestException as e:
        print(f"  !! Could not reach {url}")
        print(f"     {e}")
        return {}

    save("01_login.html", r.text)

    # Find the form FIRST. If it's there, the site is fine and nothing else
    # matters. Only diagnose when there's genuinely no form to work with.
    soup = BeautifulSoup(r.text, "html.parser")
    best = None
    for f in soup.find_all("form"):
        names = {i.get("name") for i in f.find_all("input") if i.get("name")}
        if any("pass" in (n or "").lower() for n in names):
            best = f
            break

    if best is None:
        print(f"  !! No login form on that page (HTTP {r.status_code}, "
              f"{len(r.text):,} bytes).")
        if r.status_code in (403, 429, 503):
            print("     Wembley is refusing the request outright. This is usually")
            print("     a block on data-centre traffic, which is what GitHub's")
            print("     runners are. The Chrome extension avoids it - it runs from")
            print("     your own computer on your normal connection.")
        elif _is_challenge(r.text):
            print("     That looks like a bot-check page rather than the real site.")
            print("     The Chrome extension avoids it entirely.")
        else:
            print("     The page loaded but has no password field, so the login")
            print("     page has probably moved. First 1,500 bytes:")
            for line in r.text[:1500].splitlines():
                print(f"  | {line}")
        return {}

    action = urljoin(url, best.get("action") or url)
    fields, user_field, pass_field = {}, None, None
    print("  every field in the form:")
    for inp in best.find_all(["input", "select", "button"]):
        name = inp.get("name")
        itype = (inp.get("type") or "text").lower()
        value = inp.get("value", "")
        print(f"    <{inp.name} type={itype!r} name={name!r} value={value!r}>")
        if not name:
            continue
        if itype == "password":
            pass_field = name
        elif itype in ("text", "email", "tel") and user_field is None:
            user_field = name
        elif itype in ("hidden", "submit") or inp.name == "button":
            # MiClub routes on a field literally called "action", and the
            # submit button's value is often part of that.
            fields[name] = value

    print(f"  method      : {(best.get('method') or 'get').upper()}")
    print(f"  action      : {action}")
    print(f"  user field  : {user_field}")
    print(f"  pass field  : {pass_field}")
    print(f"  extra fields: {fields or '(none)'}")
    return {
        "action": action,
        "user_field": user_field,
        "pass_field": pass_field,
        "hidden": fields,
    }


def _redirect_targets(r) -> list[str]:
    """Where a short login reply is trying to send us."""
    out, html = [], r.text
    for pat in (r'''<a[^>]+href=["\']([^"\']+)["\']''',
                r'''url=["\']?([^"\'>\s]+)''',
                r'''location(?:\.href)?\s*=\s*["\']([^"\']+)["\']'''):
        for m in re.findall(pat, html, re.I):
            u = urljoin(r.url, m)
            if u.startswith(BASE) and u not in out:
                out.append(u)
    # Always worth trying the members area directly.
    for extra in ("/members/bookings/ViewPublicCalendar.msp",
                  "/guests/bookings/ViewPublicCalendar.msp",
                  "/members/index.msp"):
        u = urljoin(BASE, extra)
        if u not in out:
            out.append(u)
    return out[:5]


def do_login(session: requests.Session, form: dict, user: str, pw: str) -> bool:
    payload = dict(form.get("hidden", {}))
    payload[form["user_field"]] = user
    payload[form["pass_field"]] = pw
    try:
        r = session.post(form["action"], data=payload, timeout=30,
                         headers={"Referer": urljoin(BASE, "/security/login.msp")})
    except requests.RequestException as e:
        print(f"  !! Login request failed: {e}")
        return False
    save("02_after_login.html", r.text)
    print(f"  posted fields : {sorted(payload)}")
    print(f"  status        : {r.status_code}")
    print(f"  final url     : {r.url}")
    if r.history:
        print(f"  redirects     : {' -> '.join(str(h.status_code) for h in r.history)}")
    if r.status_code != 200:
        print(f"  !! Login returned HTTP {r.status_code}")

    body = r.text.lower()
    bad = ("invalid" in body or "incorrect" in body
           or "not recognised" in body or "try again" in body)
    good = ("logout" in body or "log out" in body or "sign out" in body)
    if good and not bad:
        print("  login OK (found a logout link)")
        return True

    # A short reply is usually a redirect stub, not a failure. MiClub often
    # answers the login POST with a couple of hundred bytes and puts the real
    # page behind a redirect the session cookie already unlocks. Follow it.
    if not bad:
        for follow in _redirect_targets(r):
            print(f"  following {follow}")
            try:
                nxt = session.get(follow, timeout=30)
            except requests.RequestException as e:
                print(f"    could not follow: {e}")
                continue
            save("02b_followed.html", nxt.text)
            low = nxt.text.lower()
            if ("logout" in low or "log out" in low or "sign out" in low):
                print("  login OK (confirmed after following the redirect)")
                return True
            print(f"    {len(nxt.text):,} bytes, still no logout link")
    # The body is the only thing that actually explains this, so show it.
    body = r.text.strip()
    print(f"  ---- response body ({len(body)} bytes) ----")
    for line in body[:2000].splitlines():
        print(f"  | {line}")
    if len(body) > 2000:
        print(f"  | ... {len(body) - 2000} more bytes")
    print("  ---- end of response ----")

    print("  !! Login looks unsuccessful.")
    print("     Your username is your membership number with leading zeros")
    print("     removed - 00035 becomes 35, not your email address.")
    if bad:
        print("     Wembley explicitly rejected those credentials.")
    else:
        print("     No error message either - the login page may have changed.")
    return False


# --------------------------------------------------------------------------
# 2. Booking resources
# --------------------------------------------------------------------------
GOTO = re.compile(r"""gotoDestination\(\s*['"]([^'"]+)['"]""", re.I)
# MiClub accepts both spellings depending on the module.
RID = re.compile(r"booking_?[Rr]esource_?[Ii]d=(\d+)")
EVENT = re.compile(r"booking_?[Ee]vent_?[Ii]d=(\d+)")

# 3000000 is MiClub's standard golf resource. It is the same at every club
# using this platform - it does NOT identify a course. Courses are rows on the
# calendar, and each (course, date) cell links to its own booking_event_id.
DEFAULT_RESOURCE = "3000000"


def inventory(html: str, url: str) -> None:
    """Print anything on a page that could select a course or a tee time."""
    soup = BeautifulSoup(html, "html.parser")
    print(f"  ---- what's on {url.replace(BASE, '')} ----")
    title = soup.find("title")
    print(f"  | title: {title.get_text(strip=True) if title else '(none)'}"
          f"   ({len(html):,} bytes)")

    for sel in soup.find_all("select"):
        opts = [(o.get("value", ""), " ".join(o.get_text().split()))
                for o in sel.find_all("option")]
        print(f"  | <select name={sel.get('name')!r}> {len(opts)} option(s)")
        for value, text in opts[:12]:
            print(f"  |     value={value!r}  {text!r}")

    dests = list(dict.fromkeys(
        GOTO.findall(html)
        + re.findall(r"""location(?:\.href)?\s*=\s*['"]([^'"]+)['"]""", html, re.I)))
    for d in dests[:10]:
        print(f"  | javascript -> {d[:140]}")

    links = [(a["href"], " ".join(a.get_text().split())[:40])
             for a in soup.find_all("a", href=True)
             if ".msp" in a["href"] or ".xsp" in a["href"]]
    for href, text in list(dict.fromkeys(links))[:20]:
        print(f"  | link {href[:110]}   {text!r}")

    for f in soup.find_all("form"):
        names = [i.get("name") for i in f.find_all(["input", "select"]) if i.get("name")]
        print(f"  | <form action={(f.get('action') or '')[:70]!r}> {names[:12]}")
    print("  ---- end ----")


def cell_context(a) -> tuple[str, str]:
    """(course, extra text) for a calendar cell link, read from its row."""
    course, extra = "", " ".join(a.get_text().split())
    node = a
    for _ in range(6):
        node = node.parent
        if node is None:
            break
        for tag in ("h1", "h2", "h3", "h4", "h5", "th", "strong"):
            for head in node.find_all(tag):
                t = " ".join(head.get_text().split())
                if re.search(r"(old|tuart).*(hole|course)|course.*hole", t, re.I):
                    course = t
                    break
            if course:
                break
        if course:
            break
    return course, extra


def discover_calendar(session: requests.Session) -> dict:
    """
    Read the booking calendar and work out how this club is wired.

    The calendar is a grid: one row per course, one column per date. Each
    bookable cell links to an event page carrying its own booking_event_id.
    """
    out = {"root": None, "calendar": None, "resource_id": DEFAULT_RESOURCE,
           "courses": {}, "events": []}

    for root in CANDIDATE_ROOTS:
        url = urljoin(BASE, root + "ViewPublicCalendar.msp")
        for params in ({"booking_resource_id": DEFAULT_RESOURCE}, {}):
            try:
                r = session.get(url, params=params, timeout=30)
            except requests.RequestException as e:
                print(f"  {url} -> {e}")
                continue
            if r.status_code != 200:
                print(f"  {url} -> HTTP {r.status_code}")
                continue

            save(f"03_calendar_{root.strip('/').replace('/', '_')}.html", r.text)
            soup = BeautifulSoup(r.text, "html.parser")

            events = []
            for a in soup.find_all("a", href=True):
                ev = EVENT.search(a["href"])
                if not ev:
                    continue
                course, text = cell_context(a)
                events.append({"event_id": ev.group(1),
                               "course": course,
                               "text": text,
                               "href": urljoin(r.url, a["href"])})

            # Course names are the row headings, whether or not they're linked.
            headings = []
            for tag in ("h1", "h2", "h3", "h4", "h5", "th", "strong"):
                for h in soup.find_all(tag):
                    t = " ".join(h.get_text().split())
                    if re.search(r"(old|tuart).*(hole|course)|course.*hole", t, re.I):
                        if t not in headings:
                            headings.append(t)

            print(f"  {root}ViewPublicCalendar.msp"
                  f"{'?booking_resource_id=' + DEFAULT_RESOURCE if params else ''}"
                  f" -> {len(headings)} course row(s), {len(events)} bookable cell(s)")

            if headings:
                out["root"] = root
                out["calendar"] = root + "ViewPublicCalendar.msp"
                for h in headings:
                    out["courses"][slug(h)] = h
                out["events"] = events
                rids = set(RID.findall(r.text))
                if rids:
                    out["resource_id"] = sorted(rids)[0]
                if events:
                    return out
            else:
                inventory(r.text, r.url)
    return out


def probe_event(session: requests.Session, event: dict) -> None:
    """Fetch one event page - the actual tee sheet - and describe it."""
    print(f"  fetching event {event['event_id']} ({event['course'] or 'unknown course'})")
    try:
        r = session.get(event["href"], timeout=30)
    except requests.RequestException as e:
        print(f"  !! {e}")
        return
    save(f"04_event_{event['event_id']}.html", r.text)
    if r.status_code != 200:
        print(f"  !! HTTP {r.status_code}")
        return

    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text(" ", strip=True)
    times = re.findall(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)?", text, re.I)
    book_links = [a["href"] for a in soup.find_all("a", href=True)
                  if re.search(r"book|add|player|slot", a["href"], re.I)]
    buttons = [" ".join(b.get_text().split())
               for b in soup.find_all(["button", "input"])][:15]

    print(f"    {len(r.text):,} bytes, {len(set(times))} distinct time(s), "
          f"{len(book_links)} booking link(s)")
    if times:
        print(f"    first times: {', '.join(dict.fromkeys(times[:8]))}")
    for href in list(dict.fromkeys(book_links))[:8]:
        print(f"    book link: {href[:140]}")
    if buttons:
        print(f"    buttons: {buttons}")
    # Does this club put a lottery in front of the release?
    draw = re.search(r"join\s*(the\s*)?(draw|lottery|queue|ballot)"
                     r"|lottery|ballot|waiting room|current position",
                     r.text, re.I)
    print(f"    lottery/draw: {'YES - ' + draw.group(0) if draw else 'none visible'}")
    if draw:
        print("      (a draw means queue order is random, so entering every")
        print("       week matters far more than being fast)")

    if not book_links:
        inventory(r.text, r.url)


def slug(label: str) -> str:
    s = label.upper()
    course = "OLD" if "OLD" in s else ("TUART" if "TUART" in s else "COURSE")
    holes = "9" if re.search(r"\b9\b", s) else "18"
    return f"{course}_{holes}"


# --------------------------------------------------------------------------
# 3. Timesheet + booking link shape
# --------------------------------------------------------------------------
def probe_timesheet(session: requests.Session, root: str, rid: str,
                    label: str) -> dict:
    """Fetch a sheet ~9 days out (likely to have free slots) and learn its shape."""
    target = dt.date.today() + dt.timedelta(days=9)
    url = urljoin(BASE, f"{root}ViewPublicTimesheet.msp")
    r = session.get(url, params={"bookingResourceId": rid,
                                 "selectedDate": target.isoformat()}, timeout=30)
    save(f"04_timesheet_{slug(label)}.html", r.text)

    soup = BeautifulSoup(r.text, "html.parser")
    times = sorted(set(re.findall(r"\b([01]?\d|2[0-3]):[0-5]\d\s*(?:AM|PM)?\b",
                                  soup.get_text())))
    # Booking links: MiClub usually renders each free player square as an <a>
    book_links = []
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if re.search(r"(AddBooking|BookTime|MakeBooking|bookingId|teeTimeId)", h, re.I):
            book_links.append(h)

    onclicks = re.findall(r'onclick="([^"]{0,160})"', r.text)
    onclicks = [o for o in onclicks if re.search(r"book", o, re.I)][:5]

    print(f"  [{label}] {target}: {len(book_links)} booking links, "
          f"{len(times)} time strings")
    if book_links:
        print(f"    sample href   : {book_links[0][:140]}")
    if onclicks:
        print(f"    sample onclick: {onclicks[0][:140]}")
    if not book_links and not onclicks:
        print("    !! No booking links found. Sheet may be full, or the squares")
        print("       are rendered by JS. Open the dumped HTML to check.")

    return {
        "sample_href": book_links[0] if book_links else None,
        "sample_onclick": onclicks[0] if onclicks else None,
        "link_count": len(book_links),
    }


# --------------------------------------------------------------------------
def probe_bookings(session: requests.Session, root: str) -> dict:
    """Find the members' booking list and the shape of its cancel link."""
    out = {}
    for page in ("MyBookings.msp", "ViewBookings.msp", "MemberBookings.msp"):
        url = urljoin(BASE, root + page)
        try:
            r = session.get(url, timeout=30)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        save(f"05_bookings_{page}.html", r.text)
        soup = BeautifulSoup(r.text, "html.parser")
        cancels = [a["href"] for a in soup.find_all("a", href=True)
                   if re.search(r"cancel|remove|delete", a["href"] + a.get_text(), re.I)]
        rows = len(soup.find_all("tr"))
        print(f"  {page}: HTTP 200, {rows} rows, {len(cancels)} cancel link(s)")
        if cancels:
            print(f"    sample cancel: {cancels[0][:140]}")
        out[page] = {"rows": rows, "sample_cancel": cancels[0] if cancels else None}
        if cancels:
            break
    if not out:
        print("  !! No bookings page found under " + root)
        print("     Book one tee time by hand, then re-run recon.")
    return out


def check_payment(html: str) -> None:
    """Warn early if Wembley wants a card before it will hold a tee time."""
    low = html.lower()
    hits = [w for w in ("checkout", "payment", "credit card", "pay now", "cart")
            if w in low]
    if hits:
        print(f"  NOTE: booking pages mention {', '.join(hits)}.")
        print("        If payment is required up front, automated booking needs")
        print("        stored card details - check 04_timesheet_*.html.")
    else:
        print("  No payment wording found - looks like you pay at the golf shop.")


def main() -> int:
    user = os.environ.get("WGC_USERNAME")
    pw = os.environ.get("WGC_PASSWORD")
    if not user or not pw:
        print("Set WGC_USERNAME and WGC_PASSWORD in your environment first.")
        return 1

    session = make_session()

    banner("1. Login form")
    form = inspect_login_form(session)
    if not form:
        return 1

    banner("2. Logging in")
    if not do_login(session, form, user, pw):
        return 1

    banner("3. Reading the booking calendar")
    cal = discover_calendar(session)
    if not cal["courses"]:
        print("  !! No course rows found on the calendar.")
        print("     The pages above are saved in the recon-pages artifact -")
        print("     download it from the bottom of this run and send it over.")
        return 1

    root = cal["root"]
    print(f"  booking root : {root}")
    print(f"  resource id  : {cal['resource_id']}  (a MiClub constant, not a course)")
    print("  courses:")
    for key, label in cal["courses"].items():
        print(f"    {key:<10} {label!r}")

    banner("4. Bookable days right now")
    if not cal["events"]:
        print("  Every sheet on this week's calendar is full, so there are no")
        print("  event links to inspect. That's normal - run this again after")
        print("  6am when a new sheet opens.")
    else:
        by_course = {}
        for e in cal["events"]:
            by_course.setdefault(e["course"] or "?", []).append(e)
        for course, evs in by_course.items():
            ids = ", ".join(e["event_id"] for e in evs[:8])
            print(f"    {course or '(unknown course)'}: {len(evs)} day(s) -> {ids}")
        print()
        print("  a full cell link, for reference:")
        print(f"    {cal['events'][0]['href']}")

    banner("5. Inside one tee sheet")
    if cal["events"]:
        probe_event(session, cal["events"][0])
    else:
        print("  Skipped - nothing bookable to open.")

    banner("6. Your bookings and the cancel link")
    bookings = probe_bookings(session, root)

    banner("7. Writing site.yaml")
    site = {
        "base_url": BASE,
        "booking_root": root,
        "calendar_page": cal["calendar"],
        "event_page": root + "open/event.msp",
        "resource_id": cal["resource_id"],
        "courses": cal["courses"],
        "login": {
            "action": form["action"],
            "user_field": form["user_field"],
            "pass_field": form["pass_field"],
            "hidden": form["hidden"],
        },
        "bookings": bookings,
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
    }
    Path("site.yaml").write_text(yaml.safe_dump(site, sort_keys=False),
                                encoding="utf-8")
    print("  wrote site.yaml")

    banner("Done")
    print("Next, do two things:")
    print("  1. Paste the resource IDs into worker/wrangler.toml under [vars].")
    print("  2. Paste this output back into the chat - especially the")
    print("     'sample href' and 'sample cancel' lines - so the gateway's")
    print("     booking and cancel steps can be pinned to the real markup.")
    print("\nrecon_dump/ contains your logged-in HTML - do not commit it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
