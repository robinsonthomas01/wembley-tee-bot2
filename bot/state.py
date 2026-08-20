"""Tiny JSON state store.

GitHub Actions runners are ephemeral, so the workflow commits this file back
to the repo after each run. That gives us memory between runs (what we've
already alerted on, what we've booked) and doubles as an audit log.
"""
from __future__ import annotations

import datetime as dt
import json
import secrets
from pathlib import Path

PATH = Path("state.json")


class State:
    def __init__(self, path: Path = PATH):
        self.path = path
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.data = {"pending": {}, "seen": {}, "booked": [], "log": []}
        self.data.setdefault("pending", {})
        self.data.setdefault("seen", {})
        self.data.setdefault("booked", [])
        self.data.setdefault("log", [])
        self.data.setdefault("upgrades", {})
        self.data.setdefault("draws", {})

    def save(self) -> None:
        self._prune()
        self.path.write_text(json.dumps(self.data, indent=2, default=str),
                             encoding="utf-8")

    # ------------------------------------------------------------ pending
    def add_pending(self, hit, players: int) -> str:
        """Register an alerted slot and return its short confirmation token."""
        token = secrets.token_hex(2)
        self.data["pending"][token] = {
            "slot_key": hit.slot.key,
            "date": hit.slot.date.isoformat(),
            "time": hit.slot.time.strftime("%H:%M"),
            "resource": hit.slot.resource_key,
            "target": hit.target.label,
            "players": players,
            "created": dt.datetime.now().isoformat(timespec="seconds"),
        }
        return token

    def get_pending(self, token: str) -> dict | None:
        return self.data["pending"].get(token)

    def drop_pending(self, token: str) -> None:
        self.data["pending"].pop(token, None)

    def expire_pending(self, minutes: int) -> list[str]:
        cutoff = dt.datetime.now() - dt.timedelta(minutes=minutes)
        dead = [t for t, p in self.data["pending"].items()
                if dt.datetime.fromisoformat(p["created"]) < cutoff]
        for t in dead:
            self.drop_pending(t)
        return dead

    # --------------------------------------------------------------- seen
    def already_alerted(self, slot_key: str, cooldown_minutes: int = 120) -> bool:
        ts = self.data["seen"].get(slot_key)
        if not ts:
            return False
        age = dt.datetime.now() - dt.datetime.fromisoformat(ts)
        return age < dt.timedelta(minutes=cooldown_minutes)

    def mark_alerted(self, slot_key: str) -> None:
        self.data["seen"][slot_key] = dt.datetime.now().isoformat(timespec="seconds")

    # ------------------------------------------------------------- booked
    def mark_booked(self, slot_key: str, detail: str) -> None:
        self.data["booked"].append({
            "slot": slot_key,
            "detail": detail,
            "when": dt.datetime.now().isoformat(timespec="seconds"),
        })

    def has_booking_on(self, date_iso: str) -> bool:
        return any(b["slot"].startswith(date_iso) for b in self.data["booked"])

    # ---------------------------------------------------------- upgrades
    def upgrades_on(self, date_iso: str) -> int:
        return self.data.setdefault("upgrades", {}).get(date_iso, 0)

    def mark_upgraded(self, date_iso: str) -> None:
        u = self.data.setdefault("upgrades", {})
        u[date_iso] = u.get(date_iso, 0) + 1

    def forget_booking(self, slot_key: str) -> None:
        """Drop a booking record after it's been traded away."""
        self.data["booked"] = [b for b in self.data["booked"]
                               if b["slot"] != slot_key]

    # ---------------------------------------------------------------- log
    def note(self, msg: str) -> None:
        self.data["log"].append(
            f"{dt.datetime.now().isoformat(timespec='seconds')}  {msg}")

    def _prune(self) -> None:
        self.data["log"] = self.data["log"][-300:]
        self.data["booked"] = self.data["booked"][-100:]
        today = dt.date.today().isoformat()
        # Draw entries only matter for the morning they were made.
        self.data["draws"] = {k: v for k, v in self.data["draws"].items()
                              if k.split("|")[-1] >= today}
        self.data["upgrades"] = {k: v for k, v in self.data["upgrades"].items()
                                 if k >= today}
        cutoff = dt.datetime.now() - dt.timedelta(days=3)
        self.data["seen"] = {
            k: v for k, v in self.data["seen"].items()
            if dt.datetime.fromisoformat(v) > cutoff
        }
