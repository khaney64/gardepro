"""Alert rules engine for GardePro image analysis results."""
import logging
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

import yaml

_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 587

_ALERT_EMAIL   = os.environ.get("GARDEPRO_ALERT_EMAIL", "").strip()
_SMTP_USER     = os.environ.get("GARDEPRO_ALERT_SMTP_USER", _ALERT_EMAIL).strip()
_SMTP_PASSWORD = os.environ.get("GARDEPRO_ALERT_SMTP_PASSWORD", "").strip()

_fired: set[tuple] = set()  # dedup within process lifetime


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


def _send_email(subject: str, body: str) -> None:
    if not (_ALERT_EMAIL and _SMTP_PASSWORD):
        logging.warning("GardePro alert: email not sent — GARDEPRO_ALERT_EMAIL or GARDEPRO_ALERT_SMTP_PASSWORD not set")
        return
    msg = EmailMessage()
    msg["From"]    = _SMTP_USER or _ALERT_EMAIL
    msg["To"]      = _ALERT_EMAIL
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT, timeout=15) as s:
        s.ehlo()
        s.starttls()
        s.login(_SMTP_USER or _ALERT_EMAIL, _SMTP_PASSWORD)
        s.send_message(msg)


def check_and_alert(
    analysis: dict,
    media_id: int,
    kind: str,
    rules: list[dict],
    pi_host: str = "localhost:8080",
) -> list[str]:
    """Match analysis against rules; fire alerts. Returns list of triggered rule names."""
    if not rules:
        return []

    subjects    = [s.lower() for s in (analysis.get("subjects") or [])]
    description = (analysis.get("description") or "").lower()
    triggered   = []

    for rule in rules:
        name     = rule.get("name", "unknown")
        keywords = [k.lower() for k in (rule.get("keywords") or [])]
        action   = rule.get("action", "log")
        if not keywords:
            continue

        matched = any(kw in subjects or kw in description for kw in keywords)
        if not matched:
            continue

        dedup_key = (media_id, kind, name)
        if dedup_key in _fired:
            continue
        _fired.add(dedup_key)
        triggered.append(name)

        image_url = f"http://{pi_host}/api/file/{media_id}/{kind}"
        if action == "email":
            subj = f"GardePro alert: {name} detected"
            body = (
                f"Detection: {name}\n"
                f"Image: {image_url}\n\n"
                f"Analysis:\n{analysis.get('description', '')}"
            )
            try:
                _send_email(subj, body)
                logging.info("GardePro alert [%s]: email sent for media %s/%s", name, media_id, kind)
            except Exception as exc:
                logging.warning("GardePro alert [%s]: email failed — %s", name, exc)
        else:
            logging.info("GardePro alert [%s]: media %s/%s — %s", name, media_id, kind, image_url)

    return triggered
