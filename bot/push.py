"""Push notifications, sent straight from the GitHub Actions runner.

Web push doesn't need a server. The browser hands us a subscription endpoint
(hosted by Apple or Google), the app stores it in state.json, and the runner
POSTs to that endpoint signed with our VAPID key. No hosting, no bill.

Generate your keys once with:  python tools/make_vapid.py
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("wgc.push")

try:
    from pywebpush import webpush, WebPushException
    AVAILABLE = True
except ImportError:  # keeps the bot runnable without the dependency
    AVAILABLE = False


class Push:
    def __init__(self, state):
        self.state = state
        self.private_key = os.environ.get("WGC_VAPID_PRIVATE")
        self.subject = os.environ.get("WGC_VAPID_SUBJECT", "mailto:golf@example.com")
        self.enabled = bool(AVAILABLE and self.private_key and self.subscriptions())

    def subscriptions(self) -> list[dict]:
        return self.state.data.get("push_subs", [])

    def send(self, title: str, body: str, data: dict | None = None) -> bool:
        """Push to every registered device. Prunes subscriptions the browser has dropped."""
        if not self.enabled:
            log.info("PUSH (not sent) %s / %s", title, body)
            return False

        payload = json.dumps({"title": title, "body": body, "data": data or {}})
        alive, sent = [], 0
        for sub in self.subscriptions():
            try:
                webpush(
                    subscription_info=sub,
                    data=payload,
                    vapid_private_key=self.private_key,
                    vapid_claims={"sub": self.subject},
                    ttl=600,
                )
                alive.append(sub)
                sent += 1
            except WebPushException as e:
                code = getattr(e.response, "status_code", None)
                if code in (404, 410):
                    log.info("Dropping dead subscription (%s).", code)
                    continue          # device uninstalled or expired - forget it
                log.error("Push failed (%s): %s", code, e)
                alive.append(sub)
            except Exception as e:
                log.error("Push error: %s", e)
                alive.append(sub)

        self.state.data["push_subs"] = alive
        log.info("Pushed to %d device(s).", sent)
        return sent > 0


class Notifier:
    """One call, delivered by whatever channels are configured."""

    def __init__(self, cfg, state, mailer=None):
        self.push = Push(state)
        self.mailer = mailer
        self.email_on = bool(cfg.email.get("enabled", False)) and mailer is not None

    def alert_slot(self, token: str, hit, players: int) -> bool:
        s = hit.slot
        title = f"{s.date:%a %-d %b} · {s.time:%-I:%M %p}"
        body = (f"{s.resource_key.replace('_', ' ')} · {s.free_places} free"
                + (f" · ${s.price:.0f}" if s.price else ""))
        ok = self.push.send(title, body, {
            "token": token,
            "kind": "alert",
            "date": s.date.isoformat(),
            "time": s.time.strftime("%H:%M"),
            "resource": s.resource_key,
            "players": players,
            "target": hit.target.label,
        })
        if self.email_on:
            ok = self.mailer.alert_slot(token, hit, players) or ok
        return ok

    def report(self, subject: str, body: str) -> bool:
        ok = self.push.send(subject, body.strip().splitlines()[0] if body else "",
                            {"kind": "report"})
        if self.email_on:
            ok = self.mailer.report(subject, body) or ok
        return ok
