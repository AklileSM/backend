"""Simple SMTP email service.

Sending is best-effort: failures are logged but never raise to the caller.
If SMTP_HOST is not configured, emails are logged as warnings and skipped.
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import get_settings

logger = logging.getLogger(__name__)


def send_email(to: str, subject: str, html_body: str) -> bool:
    settings = get_settings()
    if not settings.smtp_host:
        logger.warning("SMTP_HOST not configured, skipping email to %s (%s)", to, subject)
        return False

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{settings.smtp_from_name} <{settings.smtp_from_email}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if settings.smtp_use_tls:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10)
            server.ehlo()
            server.starttls()
            server.ehlo()
        else:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=10)

        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)

        server.sendmail(settings.smtp_from_email, [to], msg.as_string())
        server.quit()
        logger.info("Email sent to %s: %s", to, subject)
        return True
    except Exception as exc:
        logger.error("Failed to send email to %s (%s): %s", to, subject, exc)
        return False


def send_verification_email(to: str, token: str) -> None:
    settings = get_settings()
    verify_url = f"{settings.frontend_url}/verify-email?token={token}"
    html = f"""
<p>Hi,</p>
<p>Click the link below to verify your email address for A6 Stern:</p>
<p><a href="{verify_url}">{verify_url}</a></p>
<p>This link expires in 7 days. If you did not create an account, you can ignore this email.</p>
"""
    send_email(to, "Verify your email address, A6 Stern", html)


def send_password_reset_email(to: str, token: str) -> None:
    settings = get_settings()
    reset_url = f"{settings.frontend_url}/reset-password?token={token}"
    html = f"""
<p>Hi,</p>
<p>You requested a password reset for your A6 Stern account. Click the link below to set a new password:</p>
<p><a href="{reset_url}">{reset_url}</a></p>
<p>This link expires in 1 hour. If you did not request a reset, you can safely ignore this email.</p>
"""
    send_email(to, "Reset your password, A6 Stern", html)
