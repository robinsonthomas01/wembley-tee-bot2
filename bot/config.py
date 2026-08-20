"""Configuration: your booking preferences, loaded from config.yaml + site.yaml."""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

PERTH = ZoneInfo("Australia/Perth")


class SetupIncomplete(RuntimeError):
    """Raised when the Set up workflow hasn't been run yet."""  # AWST, UTC+8, no daylight saving

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "weekday": -1, "weekend": -2, "any": -3,
}


def _time(s: str) -> dt.time:
    h, m = s.strip().split(":")
    return dt.time(int(h), int(m))


@dataclass
class Target:
    """One standing preference: 'Saturday 6:30-9:00 on either 18, four players'."""
    label: str
    weekday: str | list[str]
    resources: list[str]
    earliest: dt.time
    latest: dt.time
    players: int = 2
    priority: int = 50          # lower wins when two targets both match
    enabled: bool = True
    max_price: float | None = None
    ideal: dt.time | None = None

    def matches_date(self, d: dt.date) -> bool:
        names = [self.weekday] if isinstance(self.weekday, str) else self.weekday
        for name in names:
            want = WEEKDAYS.get(str(name).lower())
            if want is None:
                raise ValueError(
                    f"Unknown weekday '{name}' in target '{self.label}'")
            if (want == -3
                    or (want == -1 and d.weekday() < 5)
                    or (want == -2 and d.weekday() >= 5)
                    or d.weekday() == want):
                return True
        return False

    def matches_time(self, t: dt.time) -> bool:
        return self.earliest <= t <= self.latest

    @classmethod
    def parse(cls, raw: dict) -> "Target":
        return cls(
            label=raw["label"],
            weekday=raw.get("weekday", "any"),
            resources=list(raw.get("resources", [])),
            earliest=_time(raw.get("earliest", "05:00")),
            latest=_time(raw.get("latest", "19:00")),
            players=int(raw.get("players", 2)),
            priority=int(raw.get("priority", 50)),
            enabled=bool(raw.get("enabled", True)),
            max_price=raw.get("max_price"),
            ideal=_time(raw["ideal"]) if raw.get("ideal") else None,
        )


@dataclass
class ScanSettings:
    enabled: bool = True
    mode: str = "notify"        # 'notify' = email then wait for your reply
                                # 'auto'   = book immediately, tell you after
    lookahead_days: int = 14
    poll_seconds: int = 75      # be civil; this is a public tee sheet
    run_from: dt.time = dt.time(6, 0)
    run_until: dt.time = dt.time(21, 0)
    reply_window_minutes: int = 45
    upgrade: bool = True          # trade a held booking up when a better slot frees
    upgrade_min_gain: float = 0.5
    upgrade_max_per_date: int = 3   # how long a 'Y' reply stays valid


@dataclass
class Config:
    targets: list[Target]
    scan: ScanSettings
    site: dict
    booking_open_days: int = 11
    booking_open_time: dt.time = dt.time(6, 0)
    booking_probe_spread: int = 1
    max_upcoming: int = 1
    queue_enabled: bool = True
    queue_watch_minutes: int = 25
    queue_poll_seconds: float = 4.0
    queue_sprint_minutes: int = 12
    queue_sprint_seconds: float = 1.0
    dry_run: bool = True
    email: dict = field(default_factory=dict)

    # --- secrets come from environment, never from the yaml files ---
    @property
    def username(self) -> str:
        return os.environ["WGC_USERNAME"]

    @property
    def password(self) -> str:
        return os.environ["WGC_PASSWORD"]

    def courses(self) -> dict[str, str]:
        """{OLD_18: 'OLD Course 18 Holes', ...} as found by the Set up job."""
        courses = self.site.get("courses") or {}
        if not courses:
            raise SetupIncomplete(
                "No courses known yet. Go to the Actions tab, run Tee bot "
                "with 'setup', and it will find them."
            )
        return courses

    def course_label(self, key: str) -> str:
        courses = self.courses()
        if key not in courses:
            raise KeyError(f"Unknown course '{key}'. Known: {', '.join(courses)}")
        return courses[key]

    def candidate_dates(self, today: dt.date) -> list[dt.date]:
        """
        Dates worth checking at the 6am drop.

        Wembley says sheets open 10 days ahead; observed behaviour is 11. So
        rather than trust one number, check a small spread around it. The
        newly-released date is in there whichever way the club counts, and
        checking a neighbour costs one request.
        """
        n = self.booking_open_days
        spread = max(0, self.booking_probe_spread)
        offsets = sorted({n + d for d in range(-spread, spread + 1)},
                         key=lambda o: (abs(o - n), -o))
        return [today + dt.timedelta(days=o) for o in offsets if o > 0]

    def active_targets(self) -> list[Target]:
        return sorted([t for t in self.targets if t.enabled], key=lambda t: t.priority)

    @classmethod
    def load(cls, config_path="config.yaml", site_path="site.yaml") -> "Config":
        # config.json is written by the app and wins when present.
        # config.yaml is the commented seed you start from.
        json_path = Path("config.json")
        if json_path.exists():
            cfg = json.loads(json_path.read_text(encoding="utf-8"))
        else:
            cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
        site = yaml.safe_load(Path(site_path).read_text(encoding="utf-8"))

        s = cfg.get("scan", {}) or {}
        scan = ScanSettings(
            enabled=bool(s.get("enabled", True)),
            mode=s.get("mode", "notify"),
            lookahead_days=int(s.get("lookahead_days", 14)),
            poll_seconds=max(45, int(s.get("poll_seconds", 75))),
            run_from=_time(s.get("run_from", "06:00")),
            run_until=_time(s.get("run_until", "21:00")),
            reply_window_minutes=int(s.get("reply_window_minutes", 45)),
            upgrade=bool(s.get("upgrade", True)),
            upgrade_min_gain=float(s.get("upgrade_min_gain", 0.5)),
            upgrade_max_per_date=int(s.get("upgrade_max_per_date", 3)),
        )
        b = cfg.get("booking", {}) or {}
        return cls(
            targets=[Target.parse(t) for t in cfg.get("targets", [])],
            scan=scan,
            site=site,
            booking_open_days=int(b.get("open_days_ahead", 11)),
            booking_probe_spread=int(b.get("probe_spread", 1)),
            max_upcoming=int(b.get("max_upcoming", 1)),
            queue_enabled=bool((b.get("queue") or {}).get("enabled", True)),
            queue_watch_minutes=int((b.get("queue") or {}).get("watch_from_minutes", 25)),
            queue_poll_seconds=float((b.get("queue") or {}).get("poll_seconds", 4)),
            queue_sprint_minutes=int((b.get("queue") or {}).get("sprint_from_minutes", 12)),
            queue_sprint_seconds=float((b.get("queue") or {}).get("sprint_seconds", 1)),
            booking_open_time=_time(b.get("open_time", "06:00")),
            dry_run=bool(cfg.get("dry_run", True)),
            email=cfg.get("email", {}) or {},
        )


def now_perth() -> dt.datetime:
    return dt.datetime.now(PERTH)
