import html
import json
import os
import re
import time
import uuid
from urllib.parse import quote, quote_plus

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from livekit import api
from pydantic import BaseModel

load_dotenv()

app = FastAPI()

LAST_ALERT_SENT: dict[str, float] = {}
COOLDOWN_SECONDS = 900  # 15-minute cooldown per alert subject


class EmailAlertPayload(BaseModel):
    sender: str = "Alerting Service"
    subject: str
    body: str


class AgentActionRequest(BaseModel):
    action: str  # "recheck" | "restart" | "acknowledge"
    service_name: str


def clean_env(name: str, default: str = "") -> str:
    val = os.environ.get(name, default)
    return val.strip().strip('"').strip("'")


def verify_secret(provided_secret: str | None):
    expected = clean_env("WEBHOOK_SECRET")
    if expected and provided_secret != expected:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")


def clean_email_text(raw_text: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", raw_text)
    unescaped = html.unescape(no_tags)
    cleaned = re.sub(r"\s+", " ", unescaped).strip()
    return cleaned[:2500]


@app.get("/")
@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "service": "voice-alert-server",
        "telegram_configured": bool(clean_env("TELEGRAM_BOT_TOKEN") and clean_env("TELEGRAM_CHAT_ID")),
        "discord_configured": bool(clean_env("DISCORD_WEBHOOK_URL")),
        "callmebot_configured": bool(clean_env("CALLMEBOT_USER")),
        "public_url": clean_env("PUBLIC_SERVER_URL"),
    }


@app.post("/webhook/email-alert")
async def handle_email_alert(
    email: EmailAlertPayload,
    x_webhook_secret: str | None = Header(default=None),
):
    verify_secret(x_webhook_secret)

    now = time.time()
    alert_key = email.subject.strip()[:80]
    if now - LAST_ALERT_SENT.get(alert_key, 0) < COOLDOWN_SECONDS:
        return {"status": "suppressed_cooldown", "subject": alert_key}

    cleaned_body = clean_email_text(email.body)
    room_name = f"email-alert-{uuid.uuid4().hex[:8]}"
    livekit_url = clean_env("LIVEKIT_URL")
    api_key = clean_env("LIVEKIT_API_KEY")
    api_secret = clean_env("LIVEKIT_API_SECRET")
    public_url = clean_env("PUBLIC_SERVER_URL").rstrip("/")
    tg_token = clean_env("TELEGRAM_BOT_TOKEN")
    tg_chat_id = clean_env("TELEGRAM_CHAT_ID")
    discord_url = clean_env("DISCORD_WEBHOOK_URL")
    callmebot_user = clean_env("CALLMEBOT_USER")

    if not tg_token or not tg_chat_id:
        raise HTTPException(
            status_code=500,
            detail="TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing in Render Environment",
        )
    if not public_url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=500,
            detail=f"Invalid PUBLIC_SERVER_URL in Render Environment: {public_url!r}",
        )

    context_payload = {
        "task_name": email.subject,
        "what_is_happening": f"Alert from {email.sender}: {email.subject}",
        "why_it_happened": f"Alert email details: {cleaned_body}",
        "next_steps": "Ask if you want to re-check service health, trigger a restart, or acknowledge the alert.",
    }

    lkapi = api.LiveKitAPI(livekit_url, api_key, api_secret)
    await lkapi.room.create_room(
        api.CreateRoomRequest(
            name=room_name,
            metadata=json.dumps(context_payload),
            empty_timeout=300,
        )
    )
    await lkapi.aclose()

    token = (
        api.AccessToken(api_key, api_secret)
        .with_identity("akshat-mobile")
        .with_name("Akshat")
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
            )
        )
        .to_jwt()
    )

    call_link = f"{public_url}/call?ws={quote(livekit_url, safe='')}&token={quote(token, safe='')}&task={quote(email.subject[:40], safe='')}"

    # 1. Trigger a real incoming Telegram voice call with TTS readout
    call_status = "skipped_no_callmebot_user"
    if callmebot_user:
        spoken_alert = f"Urgent C S C Alert. {email.subject[:180]}"
        cmb_url = (
            "http://api.callmebot.com/start.php"
            f"?user={quote(callmebot_user, safe='@+')}"
            f"&text={quote_plus(spoken_alert)}"
            "&lang=en-US-Standard-B&rpt=2&cc=no"
        )
        try:
            cmb_res = requests.get(cmb_url, timeout=15)
            call_status = f"http_{cmb_res.status_code}"
        except Exception as exc:
            call_status = f"error_{type(exc).__name__}"

    # 2. Send Telegram Bot message with interactive Answer Call button
    tg_res = requests.post(
        f"https://api.telegram.org/bot{tg_token}/sendMessage",
        json={
            "chat_id": tg_chat_id,
            "text": f"🚨 ALERT: {email.subject}\n\n{cleaned_body[:300] or email.subject}",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "📞 Answer Call", "url": call_link}]
                ]
            },
        },
        timeout=10,
    )

    if not tg_res.ok:
        raise HTTPException(
            status_code=502,
            detail=f"Telegram rejected alert ({tg_res.status_code}): {tg_res.text}",
        )

    # 3. Send Discord Webhook alert with clickable Answer Call link
    discord_status = "skipped_no_discord_url"
    if discord_url:
        discord_payload = {
            "username": "CSC & Payment Alert Bot",
            "embeds": [
                {
                    "title": f"🚨 {email.subject[:250]}",
                    "description": f"{cleaned_body[:1500] or email.subject}\n\n📞 **[Answer Live Voice Call]({call_link})**",
                    "color": 15548997,
                    "footer": {"text": f"Source: {email.sender}"},
                }
            ],
        }
        try:
            dc_res = requests.post(discord_url, json=discord_payload, timeout=10)
            discord_status = f"http_{dc_res.status_code}"
        except Exception as exc:
            discord_status = f"error_{type(exc).__name__}"

    LAST_ALERT_SENT[alert_key] = now

    return {
        "status": "ringing",
        "room": room_name,
        "voice_call_status": call_status,
        "telegram_status": tg_res.status_code,
        "discord_status": discord_status,
    }


@app.post("/webhook/resend-inbound")
async def handle_resend_inbound(
    request: Request,
    secret: str = Query(default=""),
):
    verify_secret(secret)
    event = await request.json()

    if event.get("type") != "email.received":
        return {"status": "ignored"}

    data = event.get("data", {})
    email_id = data.get("email_id")
    sender = data.get("from", "Alerting Service")
    subject = data.get("subject", "Service Alert")

    body_text = subject
    resend_key = clean_env("RESEND_API_KEY")
    if resend_key and email_id:
        res = requests.get(
            f"https://api.resend.com/emails/receiving/{email_id}",
            headers={"Authorization": f"Bearer {resend_key}"},
            timeout=10,
        )
        if res.ok:
            email_detail = res.json()
            body_text = email_detail.get("text") or email_detail.get("html") or subject

    alert_payload = EmailAlertPayload(
        sender=sender,
        subject=subject,
        body=body_text,
    )
    return await handle_email_alert(alert_payload, x_webhook_secret=secret)


@app.post("/actions/execute")
async def execute_action(
    req: AgentActionRequest,
    x_webhook_secret: str | None = Header(default=None),
):
    verify_secret(x_webhook_secret)

    if req.action == "recheck":
        url = clean_env("TARGET_HEALTH_URL")
        if not url:
            return {"result": "No target health URL is configured."}
        try:
            r = requests.get(url, timeout=8)
            return {"result": f"Live check returned HTTP {r.status_code} in {int(r.elapsed.total_seconds() * 1000)}ms."}
        except Exception as exc:
            return {"result": f"Service is still unreachable: {type(exc).__name__}."}

    if req.action == "restart":
        hook = clean_env("RESTART_WEBHOOK_URL")
        if not hook:
            return {"result": "No restart webhook URL is configured yet."}
        r = requests.post(hook, timeout=10)
        return {"result": f"Restart hook triggered (HTTP {r.status_code})."}

    if req.action == "acknowledge":
        LAST_ALERT_SENT[req.service_name] = time.time() + 1800
        return {"result": "Alert acknowledged and silenced for 45 minutes."}

    return {"result": "Unknown action requested."}


@app.get("/call", response_class=HTMLResponse)
async def mobile_call_ui(ws: str, token: str, task: str = "Alert Briefing"):
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>{html.escape(task)}</title>
      <script src="https://cdn.jsdelivr.net/npm/livekit-client@2/dist/livekit-client.umd.min.js"></script>
      <style>
        body {{ font-family: system-ui, sans-serif; background: #0f172a; color: #f8fafc;
               display: flex; flex-direction: column; align-items: center; justify-content: center;
               height: 100vh; margin: 0; text-align: center; padding: 20px; }}
        .badge {{ background: #1e293b; padding: 8px 16px; border-radius: 999px; font-size: 14px; color: #38bdf8; }}
        h1 {{ font-size: 24px; margin: 16px 0 8px; }}
        p {{ color: #94a3b8; margin-bottom: 32px; }}
        button {{ padding: 16px 32px; font-size: 18px; font-weight: 600; border: none;
                  border-radius: 999px; cursor: pointer; margin: 8px; }}
        #connectBtn {{ background: #22c55e; color: #052e16; }}
        #hangupBtn {{ background: #ef4444; color: white; display: none; }}
      </style>
    </head>
    <body>
      <div class="badge" id="status">Connecting to Agent...</div>
      <h1>{html.escape(task)}</h1>
      <p id="subtext">Allow microphone access if prompted so you can ask follow-up questions.</p>
      <button id="connectBtn" onclick="joinCall()">Tap to Connect Audio</button>
      <button id="hangupBtn" onclick="endCall()">End Call</button>

      <script>
        const wsUrl = {json.dumps(ws)};
        const token = {json.dumps(token)};
        let room;

        async function joinCall() {{
          document.getElementById('status').innerText = 'Connecting...';
          room = new LivekitClient.Room();

          room.on(LivekitClient.RoomEvent.TrackSubscribed, (track) => {{
            if (track.kind === 'audio') {{
              const el = track.attach();
              document.body.appendChild(el);
            }}
          }});

          room.on(LivekitClient.RoomEvent.Disconnected, () => {{
            document.getElementById('status').innerText = 'Call Ended';
            document.getElementById('hangupBtn').style.display = 'none';
          }});

          await room.connect(wsUrl, token);
          await room.localParticipant.setMicrophoneEnabled(true);

          document.getElementById('status').innerText = 'Connected · Live Call';
          document.getElementById('connectBtn').style.display = 'none';
          document.getElementById('hangupBtn').style.display = 'inline-block';
        }}

        function endCall() {{
          if (room) room.disconnect();
        }}

        window.addEventListener('load', () => {{
          joinCall().catch(() => {{
            document.getElementById('status').innerText = 'Tap below to start audio';
          }});
        }});
      </script>
    </body>
    </html>
    """
