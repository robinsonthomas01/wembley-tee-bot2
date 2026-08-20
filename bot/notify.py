"""Email out (SMTP) and confirmations back in (IMAP).

You get an alert like:

    [WGC-7fa2] Tee time found: Sat 30 Aug 7:20 AM TUART_18

Reply with Y (or YES / BOOK) and the next scan cycle books it. Anything else,
or no reply inside the reply window, and it quietly expires.
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
import smtplib
import ssl
import os
import datetime as dt
from email.message import EmailMessage
from email.utils import parseaddr

log = logging.getLogger("wgc.notify")

TOKEN_RE = re.compile(r"\[WGC-([0-9a-f]{4,8})\]", re.I)
YES_RE = re.compile(r"^\s*(y|yes|ok|okay|book|book it|do it|go)\b", re.I)
NO_RE = re.compile(r"^\s*(n|no|nope|skip|ignore|cancel)\b", re.I)


class Mailer:
    def __init__(self, cfg):
        e = cfg.email or {}
        self.host = e.get("smtp_host", "smtp.gmail.com")
        self.port = int(e.get("smtp_port", 465))
        self.imap_host = e.get("imap_host", "imap.gmail.com")
        self.to = e.get("to")
        self.sender = e.get("from") or os.environ.get("WGC_EMAIL_USER")
        self.user = os.environ.get("WGC_EMAIL_USER")
        self.password = os.environ.get("WGC_EMAIL_PASSWORD")
        self.enabled = bool(self.user and self.password and self.to)
        if not self.enabled:
            log.warning("Email not configured - alerts will only be logged.")

    # ------------------------------------------------------------- sending
    def send(self, subject: str, body: str) -> bool:
        if not self.enabled:
            log.info("EMAIL (not sent)\n  %s\n%s", subject, body)
            return False
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = self.to
        msg.set_content(body)
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(self.host, self.port, context=ctx, timeout=30) as s:
                s.login(self.user, self.password)
                s.send_message(msg)
            log.info("Emailed: %s", subject)
            return True
        except Exception as e:
            log.error("Email send failed: %s", e)
            return False

    def alert_slot(self, token: str, hit, players: int) -> bool:
        s = hit.slot
        subject = f"[WGC-{token}] Tee time found: {s.date:%a %d %b} {s.time:%I:%M %p} {s.resource_key}"
        body = (
            f"A tee time opened up that matches '{hit.target.label}'.\n\n"
            f"  Course   : {s.resource_key}\n"
            f"  Date     : {s.date:%A %d %B %Y}\n"
            f"  Tee off  : {s.time:%I:%M %p}\n"
            f"  Places   : {s.free_places} free (would book {players})\n"
            f"  Green fee: {'$%.2f' % s.price if s.price else 'not shown'}\n\n"
            f"REPLY 'Y' TO THIS EMAIL AND I WILL BOOK IT.\n"
            f"Reply N, or ignore this, and nothing happens.\n\n"
            f"Note: this is a live tee sheet. Someone else may take it first.\n"
        )
        return self.send(subject, body)

    def report(self, subject: str, body: str) -> bool:
        return self.send(f"[WGC] {subject}", body)

    # ------------------------------------------------------------ receiving
    def fetch_replies(self, since_minutes: int = 90) -> dict[str, bool]:
        """
        Return {token: True/False} for replies seen in the inbox.

        Only reads mail from your own address, so a stray email can't trigger
        a booking. Marks handled messages as read.
        """
        if not self.enabled:
            return {}
        out: dict[str, bool] = {}
        try:
            with imaplib.IMAP4_SSL(self.imap_host) as m:
                m.login(self.user, self.password)
                m.select("INBOX")
                since = (dt.datetime.now() - dt.timedelta(minutes=since_minutes + 60))
                typ, data = m.search(None, "UNSEEN",
                                     f'SINCE {since.strftime("%d-%b-%Y")}')
                if typ != "OK":
                    return {}
                for num in data[0].split():
                    typ, raw = m.fetch(num, "(RFC822)")
                    if typ != "OK":
                        continue
                    msg = email.message_from_bytes(raw[0][1])

                    _, from_addr = parseaddr(msg.get("From", ""))
                    if from_addr.lower() != str(self.to).lower():
                        continue  # only you can confirm

                    tok = TOKEN_RE.search(msg.get("Subject", "") or "")
                    if not tok:
                        continue
                    token = tok.group(1).lower()

                    body = _plain_body(msg)
                    first = next((ln for ln in body.splitlines() if ln.strip()), "")
                    if YES_RE.match(first):
                        out[token] = True
                    elif NO_RE.match(first):
                        out[token] = False
                    else:
                        continue
                    m.store(num, "+FLAGS", "\\Seen")
        except Exception as e:
            log.error("IMAP check failed: %s", e)
        if out:
            log.info("Replies: %s", out)
        return out


def _plain_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode(errors="replace")
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(errors="replace")
    except Exception:
        return str(msg.get_payload())
