"""Platform SMTP sender.

Sila sends "email me the summary" notifications from its OWN server (MailHog in dev →
self-hosted/Omantel in prod) — sovereign, no per-user OAuth, no cloud. Only the
internal send-email endpoint uses this; n8n never holds SMTP creds.

stdlib smtplib is blocking, so the async caller runs send_email in a worker thread.
"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage

from sila.core.config import settings


def _send_sync(to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = settings.smtp_sender
    msg["To"] = to
    msg["Subject"] = subject or "Your summary"
    msg.set_content(body or "")
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_pass)
        smtp.send_message(msg)


async def send_email(*, to: str, subject: str, body: str) -> None:
    """Send one email via the platform SMTP server. Raises on transport failure."""
    await asyncio.to_thread(_send_sync, to, subject, body)
