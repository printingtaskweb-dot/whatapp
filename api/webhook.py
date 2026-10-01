import os
import json
import logging
import httpx
from fastapi import FastAPI, Request, Response, Query
from fastapi.responses import PlainTextResponse, JSONResponse
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whatsapp_webhook")

app = FastAPI(title="WhatsApp Webhook", redirect_slashes=False)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "whatsapp_groq_bot_secret_123")

def ask_groq(user_prompt: str) -> str:
    """Generate AI response using Groq."""
    if not GROQ_API_KEY:
        return "GROQ_API_KEY is not set."
    
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
        return completion.choices[0].message.content or "No response generated."
    except Exception as e:
        logger.error(f"Error calling Groq {GROQ_MODEL}: {e}")
        try:
            fallback = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": user_prompt}],
                max_tokens=1024
            )
            return fallback.choices[0].message.content or ""
        except Exception as e2:
            return f"Error: {e2}"

async def send_whatsapp_reply(to_number: str, text: str):
    """Send text reply back to WhatsApp user."""
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        logger.warning("WhatsApp credentials missing.")
        return False
    
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
        res = await http.post(url, headers=headers, json=payload)
        logger.info(f"Meta Send API response ({res.status_code}): {res.text}")
        return res.status_code == 200

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
    
    # Accept either the env variable, trimmed env variable, or default fallback
    is_valid = (
        clean_received == clean_expected
        or clean_received == "whatsapp_groq_bot_secret_123"
        or clean_received == "whatsapp_groq_bot_secret_123".strip()
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
        entries = data.get("entry", [])
        for entry in entries:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []):
                    if msg.get("type") == "text":
                        sender = msg.get("from")
                        user_text = msg.get("text", {}).get("body", "").strip()
                        if user_text:
                            reply = ask_groq(user_text)
                            await send_whatsapp_reply(sender, reply)
    except Exception as e:
        logger.error(f"Error handling message: {e}")
    
    return JSONResponse({"status": "ok"}, status_code=200)
