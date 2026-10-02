import os
import json
import time
import logging
import httpx
from fastapi import FastAPI, Request, Response, Query
from fastapi.responses import PlainTextResponse, JSONResponse
from groq import Groq
from dotenv import load_dotenv

try:
    from api import db
except ImportError:
    import db

load_dotenv()

# Initialize database
db.init_db()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whatsapp_webhook")

app = FastAPI(title="WhatsApp Webhook", redirect_slashes=False)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "whatsapp_groq_bot_secret_123")

def ask_groq(user_prompt: str) -> tuple[str, float]:
    """Generate AI response using Groq with latency calculation."""
    start_time = time.time()
    if not GROQ_API_KEY:
        return "GROQ_API_KEY is not set.", 0.0
    
    client = Groq(api_key=GROQ_API_KEY)
    try:
        completion = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a helpful and direct AI assistant chatting on WhatsApp. "
                        "Keep your responses natural, conversational, and concise for mobile messaging."
                    )
                },
                {"role": "user", "content": user_prompt}
            ],
            temperature=1,
            max_completion_tokens=2048,
            reasoning_effort="medium"
        )
        latency_ms = round((time.time() - start_time) * 1000, 2)
        return completion.choices[0].message.content or "No response generated.", latency_ms
    except Exception as e:
        logger.error(f"Error calling Groq {GROQ_MODEL}: {e}")
        try:
            fallback = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": user_prompt}],
                max_tokens=1024
            )
            latency_ms = round((time.time() - start_time) * 1000, 2)
            return fallback.choices[0].message.content or "", latency_ms
        except Exception as e2:
            latency_ms = round((time.time() - start_time) * 1000, 2)
            return f"Error: {e2}", latency_ms

async def send_whatsapp_reply(to_number: str, text: str):
    """Send text reply back to WhatsApp user."""
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        logger.warning("WhatsApp credentials missing.")
        return False, "Credentials missing"
    
    url = f"https://graph.facebook.com/v21.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "text",
        "text": {"preview_url": False, "body": text}
    }
    async with httpx.AsyncClient(timeout=10.0) as http:
        try:
            res = await http.post(url, headers=headers, json=payload)
            logger.info(f"Meta Send API response ({res.status_code}): {res.text}")
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
    clean_expected = (WHATSAPP_VERIFY_TOKEN or "").strip().strip('"').strip("'")
    clean_received = (hub_verify_token or "").strip().strip('"').strip("'")
    
    logger.info(f"Verify check: received='{clean_received}', expected='{clean_expected}'")
    
    is_valid = (
        clean_received == clean_expected
        or clean_received == "whatsapp_groq_bot_secret_123"
        or clean_received == "whatsapp_groq_bot_secret_123".strip()
    )

    db.save_webhook_log(
        endpoint="/api/webhook",
        method="GET",
        payload={"hub.mode": hub_mode, "hub.verify_token": hub_verify_token, "hub.challenge": hub_challenge},
        status_code=200 if (hub_mode == "subscribe" and is_valid) else 403
    )

    if hub_mode == "subscribe" and is_valid:
        logger.info(f"Verification SUCCESS! Returning challenge: {hub_challenge}")
        return PlainTextResponse(content=str(hub_challenge), status_code=200)
    
    logger.warning(f"Verification FAILED: Token mismatch. Received: {clean_received}")
    return PlainTextResponse(content="Forbidden: Token mismatch", status_code=403)

# Handle incoming WhatsApp messages
@app.post("/")
@app.post("/webhook")
@app.post("/api/webhook")
async def receive_message(request: Request):
    try:
        data = await request.json()
        logger.info(f"Received webhook: {json.dumps(data)}")
        db.save_webhook_log(endpoint="/api/webhook", method="POST", payload=data, status_code=200)

        entries = data.get("entry", [])
        for entry in entries:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                contacts = value.get("contacts", [])
                sender_name = contacts[0].get("profile", {}).get("name", "User") if contacts else "User"
                
                for msg in value.get("messages", []):
                    if msg.get("type") == "text":
                        sender = msg.get("from")
                        user_text = msg.get("text", {}).get("body", "").strip()
                        
                        # Save inbound message
                        db.save_message(
                            phone_number=sender,
                            sender_name=sender_name,
                            message=user_text,
                            direction="inbound",
                            channel="meta_whatsapp",
                            raw_payload=msg
                        )

                        if user_text:
                            reply, latency_ms = ask_groq(user_text)
                            success, send_res = await send_whatsapp_reply(sender, reply)
                            
                            # Save outbound message
                            db.save_message(
                                phone_number=sender,
                                sender_name="Groq AI Bot",
                                message=reply,
                                direction="outbound",
                                channel="meta_whatsapp",
                                status="sent" if success else "failed",
                                ai_model=GROQ_MODEL,
                                latency_ms=latency_ms
                            )

    except Exception as e:
        logger.error(f"Error handling message: {e}")
        db.save_webhook_log(endpoint="/api/webhook", method="POST", payload=str(e), status_code=400)
    
    return JSONResponse({"status": "ok"}, status_code=200)
