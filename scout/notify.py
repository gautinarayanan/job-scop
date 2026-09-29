"""Email a run summary. Optional: skipped quietly if SMTP secrets are absent.

Works with a Gmail App Password (Google Account -> Security -> 2-Step
Verification -> App passwords). Any SMTP server works via SMTP_HOST/SMTP_PORT.
"""

from __future__ import annotations

import html
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Dict, List, Optional

log = logging.getLogger("scout.notify")


def send_summary(jobs: List[Dict], file_name: Optional[str], file_url: Optional[str],
                 master_url: Optional[str], mb_used: float, today: str) -> bool:
    user, password = os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD")
    if not user or not password:
        log.info("SMTP_USER/SMTP_PASSWORD not set; skipping email")
        return False
    to = os.getenv("NOTIFY_EMAIL") or user
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "465"))

    subject = f"Job Scout: {len(jobs)} new job{'s' if len(jobs) != 1 else ''} ({today})"
    lines = [f"{len(jobs)} new jobs found on {today}."]
    if master_url:
        lines.append(f"Master sheet (set Status when you apply): {master_url}")
    if file_url:
        lines.append(f"This scrape: {file_name} {file_url}")
    lines.append("")
    lines += [f"- {j['Title']} | {j['Company']} | {j['Location']} | {j['Salary'] or 'no salary listed'}\n  {j['Link']}"
              for j in jobs]
    lines.append(f"\nBandwidth this run: ~{mb_used:.2f} MB")

    rows = "".join(
        f"<tr><td><a href='{html.escape(j['Link'])}'>{html.escape(j['Title'])}</a></td>"
        f"<td>{html.escape(j['Company'])}</td><td>{html.escape(j['Location'])}</td>"
        f"<td>{html.escape(j['Salary'])}</td><td>{j['Site']}</td></tr>"
        for j in jobs)
    link = lambda url, label: f"<a href='{html.escape(url)}'>{html.escape(label)}</a>" if url else ""
    body_html = (
        f"<p><b>{len(jobs)} new jobs</b> on {today}. They're in your "
        f"{link(master_url, 'master sheet')}: change <i>Status</i> when you apply.</p>"
        + (f"<p>This scrape: {link(file_url, file_name)}</p>" if file_url else "")
        + ("<table border='1' cellpadding='4' cellspacing='0'>"
           "<tr><th>Title</th><th>Company</th><th>Location</th><th>Salary</th><th>Site</th></tr>"
           f"{rows}</table>" if jobs else "")
        + f"<p style='color:#888'>Bandwidth this run: ~{mb_used:.2f} MB</p>")

    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content("\n".join(lines))
    msg.add_alternative(body_html, subtype="html")

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(user, password)
                s.send_message(msg)
    except Exception as e:  # email is a convenience; never fail the run over it
        log.warning(f"Email failed: {e}")
        return False
    log.info(f"Emailed summary to {to}")
    return True
