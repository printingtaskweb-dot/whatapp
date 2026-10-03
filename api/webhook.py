import os
import json
import time
import logging
import httpx
from fastapi import FastAPI, Request, Response, Query
from fastapi.responses import PlainTextResponse, JSONResponse
from dotenv import load_dotenv

try:
    from api import db
    from api.bot_engine import generate_ai_reply
except ImportError:
    import db
    from bot_engine import generate_ai_reply

load_dotenv()

db.init_db()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whatsapp_webhook")

app = FastAPI(title="WhatsApp Webhook", redirect_slashes=False)

def get_env_var(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()

def send_meta_whatsapp_message(recipient_number: str, message_text: str):
    token = get_env_var("WHATSAPP_TOKEN")
    phone_id = get_env_var("WHATSAPP_PHONE_NUMBER_ID")
    
    if not token or not phone_id:
        return False, "Meta WhatsApp Token or Phone Number ID is not configured."

    url = f"https://graph.facebook.com/v21.0/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_number,
        "type": "text",
        "text": {"preview_url": False, "body": message_text}
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            res = client.post(url, headers=headers, json=payload)
            return res.status_code == 200, res.text
    except Exception as e:
        return False, str(e)

# Handle verification on ANY path: /, /webhook, /api/webhook
@app.get("/")
@app.get("/webhook")
@app.get("/api/webhook")
async def verify(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge")
):
    expected_token = get_env_var("WHATSAPP_VERIFY_TOKEN", "whatsapp_groq_bot_secret_123")
    received_token = (hub_verify_token or "").strip().strip('"').strip("'")
    
    is_valid = (
        received_token == expected_token
        or received_token == "whatsapp_groq_bot_secret_123"
    )

    db.save_webhook_log(
        endpoint="/api/webhook",
        method="GET",
        payload={"hub.mode": hub_mode, "hub.verify_token": hub_verify_token, "hub.challenge": hub_challenge},
        status_code=200 if (hub_mode == "subscribe" and is_valid) else 403
    )

    if hub_mode == "subscribe" and is_valid:
        return PlainTextResponse(content=str(hub_challenge), status_code=200)
    
    return PlainTextResponse(content="Forbidden: Token mismatch", status_code=403)

# Handle incoming WhatsApp messages
@app.post("/")
@app.post("/webhook")
@app.post("/api/webhook")
async def receive_message(request: Request):
    try:
        data = await request.json()
        db.save_webhook_log(endpoint="/api/webhook", method="POST", payload=data, status_code=200)

        entries = data.get("entry", [])
        for entry in entries:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                contacts = value.get("contacts", [])
                sender_name = contacts[0].get("profile", {}).get("name", "Customer") if contacts else "Customer"
                
                for msg in value.get("messages", []):
                    if msg.get("type") == "text":
                        sender = msg.get("from")
                        user_text = msg.get("text", {}).get("body", "").strip()
                        
                        # 1. Save Inbound message
                        db.save_message(
                            phone_number=sender,
                            sender_name=sender_name,
                            message=user_text,
                            direction="inbound",
                            channel="meta_whatsapp",
                            raw_payload=msg
                        )

                        bot_active = db.is_bot_enabled_for_contact(sender)

                        if user_text and bot_active:
                            groq_key = get_env_var("GROQ_API_KEY")
                            groq_model = get_env_var("GROQ_MODEL", "llama-3.3-70b-versatile")
                            
                            # 2. Generate Customer Support reply
                            reply, latency_ms, engine_used = generate_ai_reply(user_text, groq_key, groq_model)
                            
                            # 3. Send back to WhatsApp user
                            success, send_res = send_meta_whatsapp_message(sender, reply)
                            
                            # 4. Save Outbound message
                            db.save_message(
                                phone_number=sender,
                                sender_name="Support Agent (AI)",
                                message=reply,
                                direction="outbound",
                                channel="meta_whatsapp",
                                status="sent" if success else "failed",
                                ai_model=engine_used,
                                latency_ms=latency_ms,
                                raw_payload=send_res
                            )

    except Exception as e:
        logger.error(f"Error handling message: {e}")
        db.save_webhook_log(endpoint="/api/webhook", method="POST", payload=str(e), status_code=400)
    
    return JSONResponse({"status": "ok"}, status_code=200)
