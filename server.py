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
COOLDOWN_SECONDS = 900  # 15-minute cooldown


# ==========================================================
# REQUEST MODELS
# ==========================================================

class EmailAlertPayload(BaseModel):
    sender: str = "Alerting Service"
    subject: str
    body: str
    discord_only: bool = False


class AgentActionRequest(BaseModel):
    action: str
    service_name: str


# ==========================================================
# HELPER FUNCTIONS
# ==========================================================

def clean_env(name: str, default: str = "") -> str:
    val = os.environ.get(name, default)
    return val.strip().strip('"').strip("'")


def verify_secret(provided_secret: str | None):
    expected = clean_env("WEBHOOK_SECRET")

    if expected and provided_secret != expected:
        raise HTTPException(
            status_code=401,
            detail="Invalid webhook secret"
        )


def clean_email_text(raw_text: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", raw_text)
    unescaped = html.unescape(no_tags)
    cleaned = re.sub(r"\s+", " ", unescaped).strip()

    return cleaned[:2500]


# ==========================================================
# CSC TRANSACTION DETAILS EXTRACTION
# ==========================================================

def extract_transaction_details(
    subject: str,
    body: str
) -> dict:

    body = html.unescape(body)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"[*_`]", "", body)
    body = re.sub(r"\s+", " ", body).strip()

    def extract(pattern, text):
        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:
            return int(match.group(1).replace(",", ""))

        return None

    activity = extract(
        r"Total Combined Activity\s*[:\-]?\s*([\d,]+)",
        body
    )
    txns = extract(
        r"Successful\s+(?:Txns|Transactions)\s*[:\-]?\s*([\d,]+)",
        body
    )
    reversal = extract(
        r"Completed\s+Reversals?\s*[:\-]?\s*([\d,]+)",
        body
    )
    failed = extract(
        r"Failed\s+(?:Txns|Transactions)\s*[:\-]?\s*([\d,]+)",
        body
    )
    failed_reversals = extract(
        r"Failed\s+Reversals?\s*[:\-]?\s*([\d,]+)",
        body
    )

    if activity is None:
        activity = extract(r"([\d,]+)\s+Activity", subject)
    if txns is None:
        txns = extract(r"([\d,]+)\s+(?:Txns|Transactions)", subject)
    if reversal is None:
        reversal = extract(r"([\d,]+)\s+Reversals?", subject)

    date_match = re.search(
        r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4},?\s+\d{1,2}:\d{2}\s*(?:am|pm))",
        body,
        re.IGNORECASE,
    )
    alert_time = date_match.group(1).upper() if date_match else None

    duration_match = re.search(
        r"(?:last|past)\s+(\d+)\s+minutes?",
        body,
        re.IGNORECASE
    )
    duration = int(duration_match.group(1)) if duration_match else 10

    return {
        "activity": activity,
        "txns": txns,
        "reversal": reversal,
        "failed": failed,
        "failed_reversals": failed_reversals,
        "alert_time": alert_time,
        "duration": duration,
    }


# ==========================================================
# BUILD COMPACT CSC DISCORD ALERT
# ==========================================================

def build_discord_alert(
    subject: str,
    body: str,
    call_link: str | None = None
) -> tuple:

    details = extract_transaction_details(subject, body)

    activity = details["activity"]
    txns = details["txns"]
    reversal = details["reversal"]
    failed = details["failed"]
    failed_reversals = details["failed_reversals"]
    duration = details["duration"]

    is_critical = False
    note = ""

    def display(value):
        return f"{value:,}" if value is not None else "N/A"

    if activity == 0:
        title = "🚨 CSC | NO ACTIVITY"
        color = 0xED4245
        is_critical = True
        note = (
            "⚠️ **Important:** No activity detected "
            f"in the last {duration} minutes. "
            "Please check immediately."
        )
    elif (failed or 0) > 0 or (failed_reversals or 0) > 0:
        title = "⚠️ CSC | FAILURE DETECTED"
        color = 0xF0B232
        is_critical = True
        note = (
            "⚠️ **Important:** Payment or reversal "
            "failures detected. Please check immediately."
        )
    elif any(
        value is None
        for value in (activity, txns, reversal, failed, failed_reversals)
    ):
        title = "⚪ CSC | DATA INCOMPLETE"
        color = 0x95A5A6
        note = (
            "⚠️ Transaction details are incomplete. "
            "Please verify the original alert."
        )
    else:
        title = "🟢 CSC | HEALTHY"
        color = 0x23A559

    description = ""
    if details["alert_time"]:
        description += f"🕒 {details['alert_time']}\n\n"

    description += (
        f"📊 Activity: **{display(activity)}**"
        f"  |  ✅ Txns: **{display(txns)}**\n"
        f"🔄 Reversal: **{display(reversal)}**"
        f"  |  ❌ Failed: **{display(failed)}**"
    )

    if failed_reversals is not None and failed_reversals > 0:
        description += f"\n⚠️ Failed Reversals: **{failed_reversals:,}**"

    if note:
        description += f"\n\n{note}"

    if call_link:
        description += f"\n\n📞 **[Answer Live Voice Call]({call_link})**"

    embed = {
        "title": title,
        "description": description,
        "color": color,
        "footer": {
            "text": f"CSC Monitoring • Last {duration} Minutes"
        }
    }

    if not details["alert_time"]:
        embed["timestamp"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime()
        )

    return embed, is_critical


# ==========================================================
# BUILD COMPACT BANK PAYMENT LOGS DISCORD ALERT
# ==========================================================

def normalize_bank_username(raw_text: str) -> str:
    text = raw_text.strip()

    if re.search(r"\b(boi|bank of india)\b", text, re.IGNORECASE):
        return "Bank of India"
    if re.search(r"\b(pnb|punjab national bank)\b", text, re.IGNORECASE):
        return "Punjab National Bank"
    if re.search(r"\b(idfc|idfc first bank)\b", text, re.IGNORECASE):
        return "IDFC FIRST Bank"

    # Strip trailing OU codes like "(BO51)" or "(IF51)" for any other bank
    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", text).strip()
    if not cleaned or cleaned.lower() == "unknown bank":
        return "Bank Alert"

    return cleaned[:80]


def build_payment_logs_alert(
    subject: str,
    body: str,
    call_link: str | None = None
) -> tuple:

    clean_body = clean_email_text(body)

    # 1. Extract Bank / OU name (e.g., "IDFC FIRST Bank (IF51)" or "Bank of India (BO51)")
    bank_match = re.search(
        r"Payment Logs Alert\s*\[([^\]]+)\]",
        subject,
        re.IGNORECASE
    ) or re.search(
        r"Bank\s*/\s*OU:\s*([^|]+)",
        clean_body,
        re.IGNORECASE
    )
    extracted_bank = bank_match.group(1).strip() if bank_match else ""

    # Normalize the Discord sender username using both extracted label and subject/body
    clean_bank_username = normalize_bank_username(
        extracted_bank or f"{subject} {clean_body[:200]}"
    )

    # Use full extracted label (with OU code) in the card, or clean bank name if missing
    bank_display = (
        extracted_bank
        if extracted_bank and extracted_bank.lower() != "unknown bank"
        else clean_bank_username
    )

    # 2. Detect Acknowledgement Failure vs. Posting Failure
    is_ack_failure = bool(
        re.search(r"Acknowledgement\s+Failure", f"{subject} {clean_body}", re.IGNORECASE)
    )

    if is_ack_failure:
        title = f"🚨 {bank_display} | ACK FAILURE"
        color = 0xED4245  # Red
        condition_text = "`isAcknowledged = false` (excl. `:59`)"
        action_note = (
            "⚠️ **Important:** Acknowledgement failures detected. "
            "Please check immediately."
        )
    else:
        title = f"⚠️ {bank_display} | POSTING FAILURE"
        color = 0xF0B232  # Amber/Orange
        condition_text = "`isPosted = false` & `isAcknowledged = true`"
        action_note = (
            "⚠️ **Important:** Unposted payment logs crossed threshold. "
            "Please check immediately."
        )

    # 3. Extract Biller IDs & counts: e.g. "MUTH00000NATYF (18 txns)"
    biller_pairs = re.findall(
        r"\b([A-Z0-9]{6,20})\s*\((\d+)\s*txns?\)",
        f"{subject} {clean_body}",
        re.IGNORECASE
    )

    # Deduplicate while preserving order
    seen = set()
    unique_billers = []
    for biller_id, count in biller_pairs:
        key = biller_id.upper()
        if key not in seen:
            seen.add(key)
            unique_billers.append((key, int(count)))

    if unique_billers:
        biller_lines = "\n".join(
            f"• `{b_id}` — **{cnt:,} txns**"
            for b_id, cnt in unique_billers
        )
    else:
        biller_lines = f"• {clean_body[:300]}"

    description = (
        f"🏦 **Bank / OU:** {bank_display}\n"
        f"🔍 **Condition:** {condition_text}\n\n"
        f"**Affected Billers:**\n"
        f"{biller_lines}\n\n"
        f"{action_note}"
    )

    if call_link:
        description += f"\n\n📞 **[Answer Live Voice Call]({call_link})**"

    embed = {
        "title": title[:250],
        "description": description,
        "color": color,
        "footer": {
            "text": f"Bank Payment Monitoring • {bank_display}"
        },
        "timestamp": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime()
        )
    }

    return embed, True, clean_bank_username


# ==========================================================
# DISCORD WEBHOOK
# ==========================================================

def send_discord_alert(
    subject: str,
    body: str,
    sender: str,
    call_link: str | None = None
) -> str:

    discord_url = clean_env("DISCORD_WEBHOOK_URL")
    discord_critical_url = clean_env("DISCORD_CRITICAL_WEBHOOK_URL")

    # 1. Identify CSC transaction summary emails
    is_csc_alert = bool(
        re.search(r"\bCSC\b.*\bActivity\b", subject, re.IGNORECASE)
        or re.search(
            r"CSC Transaction\s*&\s*Reversal Alert",
            html.unescape(body),
            re.IGNORECASE
        )
    )

    # 2. Identify Bank Payment Logs emails
    is_payment_alert = bool(
        re.search(r"Payment Logs Alert", subject, re.IGNORECASE)
        or re.search(r"Bank\s*/\s*OU:", body, re.IGNORECASE)
    )

    if is_csc_alert:
        embed, is_critical = build_discord_alert(
            subject,
            body,
            call_link=call_link
        )
        bot_username = "CSC Alert"

    elif is_payment_alert:
        embed, is_critical, bank_username = build_payment_logs_alert(
            subject,
            body,
            call_link=call_link
        )
        bot_username = bank_username[:80]

    else:
        cleaned_body = clean_email_text(body)
        desc = cleaned_body[:1500] or subject[:1500]
        if call_link:
            desc += f"\n\n📞 **[Answer Live Voice Call]({call_link})**"

        embed = {
            "title": f"🚨 {subject[:250]}",
            "description": desc,
            "color": 0xED4245,
            "footer": {
                "text": f"Source: {sender}"[:2048]
            },
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime()
            )
        }
        is_critical = False
        bot_username = "Service Alert"

    target_discord_url = (
        discord_critical_url
        if is_critical and discord_critical_url
        else discord_url
    )

    if not target_discord_url:
        return "skipped_no_discord_url"

    discord_payload = {
        "username": bot_username,
        "embeds": [embed],
        "allowed_mentions": {
            "parse": []
        }
    }

    try:
        dc_res = requests.post(
            target_discord_url,
            json=discord_payload,
            timeout=10
        )

        if dc_res.ok:
            print(f"Discord alert sent [{bot_username}]: {embed['title']}")
        else:
            print(f"Discord alert failed: HTTP {dc_res.status_code}")

        return f"http_{dc_res.status_code}"

    except Exception as exc:
        print(f"Discord alert error: {type(exc).__name__}")
        return f"error_{type(exc).__name__}"


# ==========================================================
# HEALTH CHECK
# ==========================================================

@app.get("/")
@app.get("/health")
async def health_check():

    return {
        "status": "ok",
        "service": "voice-alert-server",
        "telegram_configured": bool(
            clean_env("TELEGRAM_BOT_TOKEN")
            and clean_env("TELEGRAM_CHAT_ID")
        ),
        "discord_configured": bool(
            clean_env("DISCORD_WEBHOOK_URL")
        ),
        "discord_critical_configured": bool(
            clean_env("DISCORD_CRITICAL_WEBHOOK_URL")
        ),
        "callmebot_configured": bool(
            clean_env("CALLMEBOT_USER")
        ),
        "public_url": clean_env(
            "PUBLIC_SERVER_URL"
        ),
    }


# ==========================================================
# EMAIL ALERT WEBHOOK
# ==========================================================

@app.post("/webhook/email-alert")
async def handle_email_alert(
    email: EmailAlertPayload,
    x_webhook_secret: str | None = Header(default=None),
):

    verify_secret(x_webhook_secret)

    now = time.time()
    alert_key = email.subject.strip()[:80]

    if (
        not email.discord_only
        and now - LAST_ALERT_SENT.get(alert_key, 0)
        < COOLDOWN_SECONDS
    ):
        return {
            "status": "suppressed_cooldown",
            "subject": alert_key
        }

    if email.discord_only:
        discord_status = send_discord_alert(
            subject=email.subject,
            body=email.body,
            sender=email.sender,
            call_link=None
        )
        return {
            "status": "discord_logged",
            "discord_status": discord_status,
        }

    cleaned_body = clean_email_text(email.body)
    room_name = f"email-alert-{uuid.uuid4().hex[:8]}"

    livekit_url = clean_env("LIVEKIT_URL")
    api_key = clean_env("LIVEKIT_API_KEY")
    api_secret = clean_env("LIVEKIT_API_SECRET")
    public_url = clean_env("PUBLIC_SERVER_URL").rstrip("/")
    tg_token = clean_env("TELEGRAM_BOT_TOKEN")
    tg_chat_id = clean_env("TELEGRAM_CHAT_ID")
    callmebot_user = clean_env("CALLMEBOT_USER")

    if not public_url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=500,
            detail=(
                "Invalid PUBLIC_SERVER_URL in "
                f"Render Environment: {public_url!r}"
            ),
        )

    context_payload = {
        "task_name": email.subject,
        "what_is_happening": f"Alert from {email.sender}: {email.subject}",
        "why_it_happened": f"Alert email details: {cleaned_body}",
        "next_steps": (
            "Ask if you want to re-check service health, "
            "trigger a restart, or acknowledge the alert."
        ),
    }

    lkapi = api.LiveKitAPI(livekit_url, api_key, api_secret)
    try:
        await lkapi.room.create_room(
            api.CreateRoomRequest(
                name=room_name,
                metadata=json.dumps(context_payload),
                empty_timeout=300,
            )
        )
    finally:
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

    call_link = (
        f"{public_url}/call"
        f"?ws={quote(livekit_url, safe='')}"
        f"&token={quote(token, safe='')}"
        f"&task={quote(email.subject[:40], safe='')}"
    )

    discord_status = send_discord_alert(
        subject=email.subject,
        body=email.body,
        sender=email.sender,
        call_link=call_link
    )

    if not tg_token or not tg_chat_id:
        raise HTTPException(
            status_code=500,
            detail=(
                "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID "
                "is missing in Render Environment"
            ),
        )

    call_status = "skipped_no_callmebot_user"
    if callmebot_user:
        spoken_alert = f"Urgent Alert. {email.subject[:180]}"
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

    tg_res = requests.post(
        f"https://api.telegram.org/bot{tg_token}/sendMessage",
        json={
            "chat_id": tg_chat_id,
            "text": (
                f"🚨 ALERT: {email.subject}\n\n"
                f"{cleaned_body[:300] or email.subject}"
            ),
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
            detail=(
                f"Telegram rejected alert "
                f"({tg_res.status_code}): {tg_res.text}"
            ),
        )

    LAST_ALERT_SENT[alert_key] = now

    return {
        "status": "ringing",
        "room": room_name,
        "voice_call_status": call_status,
        "telegram_status": tg_res.status_code,
        "discord_status": discord_status,
    }


# ==========================================================
# RESEND INBOUND EMAIL WEBHOOK
# ==========================================================

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
            body_text = (
                email_detail.get("text")
                or email_detail.get("html")
                or subject
            )

    alert_payload = EmailAlertPayload(
        sender=sender,
        subject=subject,
        body=body_text,
    )

    return await handle_email_alert(
        alert_payload,
        x_webhook_secret=secret
    )


# ==========================================================
# AGENT ACTIONS
# ==========================================================

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
            elapsed_ms = int(r.elapsed.total_seconds() * 1000)
            return {
                "result": (
                    f"Live check returned HTTP "
                    f"{r.status_code} in {elapsed_ms}ms."
                )
            }
        except Exception as exc:
            return {
                "result": (
                    "Service is still unreachable: "
                    f"{type(exc).__name__}."
                )
            }

    if req.action == "restart":
        hook = clean_env("RESTART_WEBHOOK_URL")
        if not hook:
            return {"result": "No restart webhook URL is configured yet."}
        r = requests.post(hook, timeout=10)
        return {
            "result": f"Restart hook triggered (HTTP {r.status_code})."
        }

    if req.action == "acknowledge":
        LAST_ALERT_SENT[req.service_name] = time.time() + 1800
        return {
            "result": "Alert acknowledged and silenced for 45 minutes."
        }

    return {"result": "Unknown action requested."}


# ==========================================================
# LIVEKIT MOBILE CALL UI
# ==========================================================

@app.get("/call", response_class=HTMLResponse)
async def mobile_call_ui(
    ws: str,
    token: str,
    task: str = "Alert Briefing"
):

    safe_ws = json.dumps(ws).replace("<", "\\u003c")
    safe_token = json.dumps(token).replace("<", "\\u003c")

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>{html.escape(task)}</title>
      <script src="https://cdn.jsdelivr.net/npm/livekit-client@2/dist/livekit-client.umd.min.js"></script>
      <style>
        body {{
          font-family: system-ui, sans-serif;
          background: #0f172a;
          color: #f8fafc;
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          height: 100vh;
          margin: 0;
          text-align: center;
          padding: 20px;
          box-sizing: border-box;
        }}
        .badge {{
          background: #1e293b;
          padding: 8px 16px;
          border-radius: 999px;
          font-size: 14px;
          color: #38bdf8;
        }}
        h1 {{ font-size: 24px; margin: 16px 0 8px; }}
        p {{ color: #94a3b8; margin-bottom: 32px; }}
        button {{
          padding: 16px 32px;
          font-size: 18px;
          font-weight: 600;
          border: none;
          border-radius: 999px;
          cursor: pointer;
          margin: 8px;
        }}
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
        const wsUrl = {safe_ws};
        const token = {safe_token};
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
