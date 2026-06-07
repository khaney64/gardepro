"""Alert rules engine for GardePro image analysis results."""
import logging
import os
import smtplib
import time
from email.message import EmailMessage
from pathlib import Path

import yaml

_SMTP_HOST = os.environ.get("GARDEPRO_ALERT_SMTP_HOST", "smtp.gmail.com").strip()
_SMTP_PORT = int(os.environ.get("GARDEPRO_ALERT_SMTP_PORT", "587"))
# Use implicit SSL (SMTP_SSL) when port is 465 or GARDEPRO_ALERT_SMTP_SSL=1; otherwise STARTTLS
_smtp_ssl_env = os.environ.get("GARDEPRO_ALERT_SMTP_SSL", "").strip()
_SMTP_SSL  = (_smtp_ssl_env == "1") if _smtp_ssl_env else (_SMTP_PORT == 465)

_ALERT_EMAIL   = os.environ.get("GARDEPRO_ALERT_EMAIL", "").strip()
_ALERT_FROM    = os.environ.get("GARDEPRO_ALERT_FROM_EMAIL", _ALERT_EMAIL).strip()
_SMTP_USER     = os.environ.get("GARDEPRO_ALERT_SMTP_USER", _ALERT_FROM).strip()
_SMTP_PASSWORD = os.environ.get("GARDEPRO_ALERT_SMTP_PASSWORD", "").strip()

_fired: set[tuple] = set()          # (media_id, kind, rule_name) — never re-alert same image
_last_fired: dict[str, float] = {}  # rule_name → epoch of last alert (rate limiting)


def load_rules(path: str) -> list[dict]:
    """Load alert rules from YAML file. Returns [] if missing or malformed."""
    p = Path(path).expanduser()
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text())
        return data.get("alerts", []) if isinstance(data, dict) else []
    except Exception as exc:
        logging.warning("GardePro alerts: failed to load %s — %s", path, exc)
        return []


def _send_email(subject: str, plain: str, html: str = "", thumb_bytes: bytes = b"") -> None:
    """Send an email alert. Raises on misconfiguration or SMTP failure."""
    if not _ALERT_EMAIL:
        raise RuntimeError("GARDEPRO_ALERT_EMAIL not set")
    if not _SMTP_PASSWORD:
        raise RuntimeError("GARDEPRO_ALERT_SMTP_PASSWORD not set")
    msg = EmailMessage()
    msg["From"]    = _ALERT_FROM
    msg["To"]      = _ALERT_EMAIL
    msg["Subject"] = subject
    msg.set_content(plain)
    if html:
        msg.add_alternative(html, subtype="html")
        if thumb_bytes:
            # Attach inline image to the HTML part
            html_part = msg.get_payload()[-1]
            html_part.add_related(thumb_bytes, maintype="image", subtype="jpeg", cid="<thumb>")
    if _SMTP_SSL:
        with smtplib.SMTP_SSL(_SMTP_HOST, _SMTP_PORT, timeout=15) as s:
            s.login(_SMTP_USER, _SMTP_PASSWORD)
            s.send_message(msg)
    else:
        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT, timeout=15) as s:
            s.ehlo()
            s.starttls()
            s.login(_SMTP_USER, _SMTP_PASSWORD)
            s.send_message(msg)


def send_test_email() -> None:
    """Send a test email to verify configuration. Raises on failure."""
    _send_email(
        "GardePro test alert",
        "This is a test message from GardePro.\n\nIf you received this, email alerts are working correctly.",
    )


def check_and_alert(
    analysis: dict,
    media_id: int,
    kind: str,
    rules: list[dict],
    pi_host: str = "localhost:8080",
    cooldown_seconds: float = 1800,
    thumb_path: str = "",
    all_rules: list[dict] | None = None,
) -> tuple[list[str], list[str]]:
    """Match analysis against rules; fire alerts. Returns (triggered rule names, error messages)."""
    if not rules:
        return [], []

    subjects    = [s.lower() for s in (analysis.get("subjects") or [])]
    description = (analysis.get("description") or "").lower()
    triggered   = []
    errors      = []
    now         = time.time()

    # Read thumbnail once for use in all email alerts
    thumb_bytes = b""
    if thumb_path:
        p = Path(thumb_path)
        if p.exists():
            thumb_bytes = p.read_bytes()

    # Build specific_keywords from ALL rules (including disabled ones) so that
    # disabling a specific rule doesn't cause its subjects to leak into the catch-all.
    specific_keywords: set[str] = set()
    for rule in (all_rules if all_rules is not None else rules):
        if not rule.get("catch_all"):
            specific_keywords.update(k.lower() for k in (rule.get("keywords") or []))

    def _fire(name: str, action: str, matched_subjects: list[str]) -> None:
        dedup_key = (media_id, kind, name)
        if dedup_key in _fired:
            return
        if cooldown_seconds > 0 and now - _last_fired.get(name, 0) < cooldown_seconds:
            remaining = int(cooldown_seconds - (now - _last_fired[name]))
            logging.info("GardePro alert [%s]: suppressed (cooldown %ds remaining) for media %s/%s",
                         name, remaining, media_id, kind)
            return
        _last_fired[name] = now
        triggered.append(name)
        thumb_url = f"http://{pi_host}/api/thumb/{media_id}/{kind}"
        if action == "email":
            detected = ", ".join(matched_subjects) if matched_subjects else analysis.get("description", "")
            subj = f"GardePro alert: {detected} detected"
            plain = (
                f"Detection: {detected}\n"
                f"Thumbnail: {thumb_url}\n\n"
                f"Analysis:\n{analysis.get('description', '')}"
            )
            img_tag = '<img src="cid:thumb" style="max-width:100%;border-radius:4px">' if thumb_bytes else \
                      f'<a href="{thumb_url}">View thumbnail</a>'
            html = f"""\
<html><body style="font-family:sans-serif;max-width:600px">
<h2 style="margin-bottom:4px">GardePro alert: {detected}</h2>
<p style="margin-top:0;color:#666">{detected}</p>
{img_tag}
<p style="color:#888;font-size:0.85em">{analysis.get('description', '')}</p>
</body></html>"""
            try:
                _send_email(subj, plain, html, thumb_bytes)
                _fired.add(dedup_key)
                logging.info("GardePro alert [%s]: email sent for media %s/%s", name, media_id, kind)
            except Exception as exc:
                err = f"Alert [{name}]: email failed — {exc}"
                logging.warning("GardePro %s", err)
                errors.append(err)
        else:
            _fired.add(dedup_key)
            logging.info("GardePro alert [%s]: media %s/%s — %s", name, media_id, kind, thumb_url)

    for rule in rules:
        name     = rule.get("name", "unknown")
        action   = rule.get("action", "log")

        if rule.get("catch_all"):
            # Fire for any subjects not covered by a specific keyword rule
            unmatched = [s for s in subjects if s not in specific_keywords]
            if unmatched:
                _fire(name, action, unmatched)
            continue

        keywords = [k.lower() for k in (rule.get("keywords") or [])]
        if not keywords:
            continue

        matched = any(kw in subjects or kw in description for kw in keywords)
        if not matched:
            continue

        _fire(name, action, [kw for kw in keywords if kw in subjects])

    return triggered, errors
