import asyncio
import os
import smtplib
from email.message import EmailMessage

import httpx


def war_room_configured() -> bool:
    return any(
        os.getenv(name, "").strip()
        for name in [
            "WAR_ROOM_WEBHOOK_URL",
            "MATTERMOST_WEBHOOK_URL",
            "ZULIP_URL",
            "MATRIX_HOMESERVER_URL",
        ]
    )


def teams_configured() -> bool:
    return bool(_teams_config()["webhook_url"])


async def post_war_room(text: str) -> None:
    """Post a message to the incident war room (Slack or Discord incoming
    webhook, auto-detected from the URL). Never raises: notification failure
    must not break the investigation."""
    try:
        provider = os.getenv("WAR_ROOM_PROVIDER", "").strip().lower()
        if provider == "mattermost":
            await _post_mattermost(text)
        elif provider == "zulip":
            await _post_zulip(text)
        elif provider == "matrix":
            await _post_matrix(text)
        else:
            await _post_webhook(text)
    except Exception as exc:
        print(f"[notify] war-room post failed: {exc}")


async def post_teams_alert(record: dict, reason: str) -> None:
    """Raise a high-impact alert to the configured Teams owner/admin channel.

    Uses a Teams Workflows/incoming webhook. A true Teams voice call requires a
    Microsoft Graph Communications bot, so this function keeps the incident
    pipeline independent of tenant-level calling permissions.
    """
    config = _teams_config()
    url = config["webhook_url"]
    if not url:
        return
    owner = config["project_owner"] or "Project Owner"
    admin = config["admin"] or "Admin"
    service = record.get("service", "unknown")
    impact = float(record.get("estimated_revenue_impact_per_minute") or 0.0)
    incident_id = record.get("incident_id", "")
    text = (
        f"High-impact incident alert for {owner} and {admin}\n"
        f"Service: {service}\n"
        f"Severity: {str(record.get('severity', 'unknown')).upper()}\n"
        f"Reason: {reason}\n"
        f"Impact: ${impact:.2f}/min, affected users: {record.get('affected_users', 0)}\n"
        f"Incident: {os.getenv('WEB_BASE_URL', '').rstrip('/')}/incident/{incident_id}"
    )
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json={"text": text})
    except Exception as exc:
        print(f"[notify] Teams alert failed: {exc}")


async def post_high_risk_email_alert(record: dict, reason: str, recipient: str | None = None) -> bool:
    """Send a high-risk approval alert email for critical remediation decisions."""
    recipient = recipient or os.getenv("HIGH_RISK_ALERT_EMAIL_TO", "anujay.ds@gmail.com").strip()
    if not recipient:
        return False

    smtp_host = os.getenv("EMAIL_SMTP_HOST", "").strip()
    if not smtp_host:
        return False
    try:
        smtp_port = int(os.getenv("EMAIL_SMTP_PORT", "587").strip() or 587)
    except ValueError:
        smtp_port = 587
    smtp_username = os.getenv("EMAIL_SMTP_USERNAME", "").strip()
    smtp_password = os.getenv("EMAIL_SMTP_PASSWORD", "").strip()
    use_tls = os.getenv("EMAIL_USE_TLS", "true").strip().lower() in {"true", "1", "yes"}
    sender = os.getenv("EMAIL_FROM", f"noreply@{os.getenv('EMAIL_FROM_DOMAIN','example.com')}").strip()
    if not sender:
        sender = f"noreply@{os.getenv('EMAIL_FROM_DOMAIN','example.com')}"

    subject = f"High-risk remediation approved for incident {record.get('incident_id', 'unknown')}"
    body = (
        f"Incident ID: {record.get('incident_id')}\n"
        f"Service: {record.get('service', 'unknown')}\n"
        f"Severity: {record.get('severity', 'unknown')}\n"
        f"Reason: {reason}\n"
        f"Alert: {record.get('alert_description', '')}\n"
        f"Recovery recommendation: {record.get('recovery_recommendations', [])[:3]}\n"
        f"Approval policy: {record.get('remediation_policy')}\n"
        f"Link: {os.getenv('WEB_BASE_URL', '').rstrip('/')}/incident/{record.get('incident_id', '')}\n"
    )
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(body)

    def _send_email_sync() -> bool:
        try:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
                if use_tls:
                    server.starttls()
                if smtp_username and smtp_password:
                    server.login(smtp_username, smtp_password)
                server.send_message(message)
            return True
        except Exception as exc:
            print(f"[notify] high-risk email alert failed: {exc}")
            return False

    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _send_email_sync)
    except Exception as exc:
        print(f"[notify] high-risk email alert failed: {exc}")
        return False


async def post_teams_handshake() -> bool:
    url = _teams_config()["webhook_url"]
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(url, json={"text": "AIOC Teams alert handshake succeeded."})
        return response.status_code < 500
    except Exception as exc:
        print(f"[notify] Teams handshake failed: {exc}")
        return False


def teams_runtime_config() -> dict:
    config = _teams_config()
    return {key: value for key, value in config.items() if key != "webhook_url"}


def _teams_config() -> dict:
    config = {
        "webhook_url": os.getenv("TEAMS_ALERT_WEBHOOK_URL", "").strip(),
        "project_owner": os.getenv("TEAMS_PROJECT_OWNER", "").strip(),
        "admin": os.getenv("TEAMS_ADMIN", "").strip(),
        "alert_threshold_per_minute": os.getenv("HIGH_IMPACT_ALERT_THRESHOLD_PER_MINUTE", "").strip(),
    }
    try:
        from agents.connector_registry import runtime_connectors

        records = runtime_connectors("teams")
        if records:
            record = sorted(records, key=lambda item: str(item.get("updated_at") or ""), reverse=True)[0]
            connector_config = record.get("config") or {}
            env_name = str(connector_config.get("webhook_url_env") or "TEAMS_ALERT_WEBHOOK_URL").strip()
            config.update(
                {
                    "webhook_url": os.getenv(env_name, "").strip() or str(connector_config.get("webhook_url") or config["webhook_url"]).strip(),
                    "project_owner": str(connector_config.get("project_owner") or config["project_owner"]).strip(),
                    "admin": str(connector_config.get("admin") or config["admin"]).strip(),
                    "alert_threshold_per_minute": str(connector_config.get("alert_threshold_per_minute") or config["alert_threshold_per_minute"]).strip(),
                }
            )
    except Exception:
        pass
    return config


async def _post_webhook(text: str) -> None:
    url: str = os.getenv("WAR_ROOM_WEBHOOK_URL", "").strip()
    if not url:
        return
    payload = {"content": text} if "discord" in url else {"text": text}
    async with httpx.AsyncClient(timeout=5) as client:
        await client.post(url, json=payload)


async def _post_mattermost(text: str) -> None:
    url = os.getenv("MATTERMOST_WEBHOOK_URL", "").strip()
    if not url:
        await _post_webhook(text)
        return
    async with httpx.AsyncClient(timeout=5) as client:
        await client.post(url, json={"text": text})


async def _post_zulip(text: str) -> None:
    base = os.getenv("ZULIP_URL", "").rstrip("/")
    email = os.getenv("ZULIP_EMAIL", "")
    api_key = os.getenv("ZULIP_API_KEY", "")
    stream = os.getenv("ZULIP_STREAM", "incidents")
    topic = os.getenv("ZULIP_TOPIC", "incident-response")
    if not base or not email or not api_key:
        await _post_webhook(text)
        return
    async with httpx.AsyncClient(timeout=5, auth=(email, api_key)) as client:
        await client.post(
            f"{base}/api/v1/messages",
            data={
                "type": "stream",
                "to": stream,
                "topic": topic,
                "content": text,
            },
        )


async def _post_matrix(text: str) -> None:
    base = os.getenv("MATRIX_HOMESERVER_URL", "").rstrip("/")
    token = os.getenv("MATRIX_ACCESS_TOKEN", "")
    room_id = os.getenv("MATRIX_ROOM_ID", "")
    if not base or not token or not room_id:
        await _post_webhook(text)
        return
    txn_id = str(abs(hash(text)))
    async with httpx.AsyncClient(timeout=5) as client:
        await client.put(
            f"{base}/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn_id}",
            headers={"Authorization": f"Bearer {token}"},
            json={"msgtype": "m.text", "body": text},
        )
