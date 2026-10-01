import os
import json
import logging
import httpx
from fastapi import FastAPI, Request, Response, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whatsapp_bot")

app = FastAPI(title="WhatsApp Groq AI Bot")

# Environment variables
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "whatsapp_groq_bot_secret_123")

def get_groq_client():
    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY environment variable is not set. Please set it in .env or Vercel settings.")
    return Groq(api_key=GROQ_API_KEY)

def ask_groq(user_prompt: str) -> str:
    """Generate AI response using Groq."""
    client = get_groq_client()
    try:
        completion = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a helpful, conversational, and direct AI assistant chatting on WhatsApp. "
                        "Keep your responses natural, conversational, clear, and formatted cleanly for mobile reading. "
                        "Avoid overly long essays unless explicitly asked."
                    )
                },
                {
                    "role": "user",
                    "content": user_prompt
                }
            ],
            temperature=1,
            max_completion_tokens=2048,
            top_p=1,
            reasoning_effort="medium"
        )
        return completion.choices[0].message.content or "No response generated."
    except Exception as e:
        logger.error(f"Error calling Groq model {GROQ_MODEL}: {e}")
        # Graceful fallback if model is unavailable or rate limited
        if GROQ_MODEL != "llama-3.3-70b-versatile":
            logger.info("Attempting fallback to llama-3.3-70b-versatile...")
            try:
                fallback_comp = client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[
                        {"role": "user", "content": user_prompt}
                    ],
                    max_tokens=1024
                )
                return fallback_comp.choices[0].message.content or ""
            except Exception as e2:
                logger.error(f"Fallback also failed: {e2}")
        return f"Sorry, I encountered an issue processing your request: {str(e)}"

async def send_whatsapp_message(recipient_number: str, message_text: str):
    """Send WhatsApp message using Meta Cloud API."""
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        logger.warning("WhatsApp Token or Phone Number ID not configured. Message not sent via Meta API.")
        return False

    url = f"https://graph.facebook.com/v21.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_number,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": message_text
        }
    }

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        try:
            res = await http_client.post(url, headers=headers, json=payload)
            if res.status_code == 200:
                logger.info(f"Successfully sent WhatsApp message to {recipient_number}")
                return True
            else:
                logger.error(f"Meta API Error ({res.status_code}): {res.text}")
                return False
        except Exception as e:
            logger.error(f"Failed to post to Meta API: {e}")
            return False

# ==========================================
# 1. META WHATSAPP CLOUD API WEBHOOKS
# ==========================================

@app.get("/api/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge")
):
    """
    Verification endpoint for Meta WhatsApp Cloud API.
    Meta sends a GET request to verify ownership of the webhook URL.
    """
    logger.info(f"Webhook verification check received: mode={hub_mode}, token={hub_verify_token}")
    if hub_mode == "subscribe" and hub_verify_token == WHATSAPP_VERIFY_TOKEN:
        logger.info("Webhook verification succeeded.")
        return PlainTextResponse(content=hub_challenge, status_code=200)
    
    logger.warning("Webhook verification failed: Invalid verify token.")
    return PlainTextResponse(content="Forbidden: Verification token mismatch", status_code=403)

@app.post("/api/webhook")
async def handle_whatsapp_message(request: Request):
    """
    Handles incoming messages from WhatsApp Meta Cloud API.
    """
    try:
        body = await request.json()
        logger.info(f"Incoming WhatsApp webhook payload: {json.dumps(body)}")
    except Exception as e:
        logger.error(f"Failed to parse incoming JSON: {e}")
        return JSONResponse({"status": "invalid json"}, status_code=400)

    # Process WhatsApp message events
    try:
        entries = body.get("entry", [])
        for entry in entries:
            changes = entry.get("changes", [])
            for change in changes:
                value = change.get("value", {})
                messages = value.get("messages", [])
                
                for msg in messages:
                    # Only reply to normal user text messages
                    if msg.get("type") == "text":
                        sender_id = msg.get("from")
                        user_text = msg.get("text", {}).get("body", "").strip()
                        
                        logger.info(f"Received message from {sender_id}: '{user_text}'")
                        
                        if user_text:
                            # 1. Ask Groq AI model
                            ai_reply = ask_groq(user_text)
                            logger.info(f"Generated AI reply: {ai_reply[:60]}...")
                            
                            # 2. Send back to user via WhatsApp
                            await send_whatsapp_message(sender_id, ai_reply)

    except Exception as err:
        logger.error(f"Error parsing/replying to WhatsApp message: {err}")

    # Meta requires a 200 OK response immediately
    return JSONResponse({"status": "success"}, status_code=200)

# ==========================================
# 2. TWILIO WHATSAPP WEBHOOK (OPTIONAL EASY SANDBOX)
# ==========================================

@app.post("/api/twilio")
async def handle_twilio_webhook(
    From: str = Form(None),
    Body: str = Form(None)
):
    """
    Twilio WhatsApp webhook endpoint.
    Returns TwiML XML directly.
    """
    user_text = (Body or "").strip()
    logger.info(f"Twilio message from {From}: '{user_text}'")

    if not user_text:
        twiml = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
        return Response(content=twiml, media_type="application/xml")

    # Ask Groq AI model
    ai_reply = ask_groq(user_text)

    # XML Escape for TwiML
    escaped_reply = (
        ai_reply.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{escaped_reply}</Message>
</Response>"""
    return Response(content=twiml, media_type="application/xml")

# ==========================================
# 3. DIRECT TESTING API & WEB SIMULATOR
# ==========================================

@app.post("/api/chat")
async def direct_chat(request: Request):
    """Direct JSON chat endpoint for testing without WhatsApp."""
    data = await request.json()
    message = data.get("message", "")
    if not message:
        return JSONResponse({"error": "Empty message"}, status_code=400)
    
    reply = ask_groq(message)
    return JSONResponse({"reply": reply, "model": GROQ_MODEL})

@app.get("/", response_class=HTMLResponse)
async def index():
    """Interactive WhatsApp UI simulator for testing."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WhatsApp Groq AI Bot - Test & Setup</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        .chat-bg { background-color: #0b141a; background-image: radial-gradient(#1f2c34 1px, transparent 1px); background-size: 20px 20px; }
    </style>
</head>
<body class="bg-gray-900 text-gray-100 min-h-screen flex flex-col items-center justify-center p-4">
    <div class="w-full max-w-4xl bg-gray-800 rounded-2xl shadow-2xl border border-gray-700 overflow-hidden flex flex-col md:flex-row h-[650px]">
        
        <!-- Left: Configuration & Webhook Info -->
        <div class="w-full md:w-1/2 p-6 border-b md:border-b-0 md:border-r border-gray-700 flex flex-col justify-between overflow-y-auto">
            <div>
                <div class="flex items-center space-x-3 mb-4">
                    <div class="w-10 h-10 rounded-full bg-emerald-500 flex items-center justify-center text-white text-xl font-bold">
                        <i class="fab fa-whatsapp"></i>
                    </div>
                    <div>
                        <h1 class="text-lg font-bold text-white">WhatsApp Groq AI Bot</h1>
                        <span class="text-xs px-2 py-0.5 rounded-full bg-emerald-900/60 text-emerald-400 border border-emerald-700">Live Serverless</span>
                    </div>
                </div>

                <p class="text-sm text-gray-300 mb-4">
                    This service powers your WhatsApp bot with Groq’s high-speed inference engine.
                </p>

                <div class="space-y-3 text-xs">
                    <div class="bg-gray-900/80 p-3 rounded-lg border border-gray-700">
                        <span class="text-gray-400 block mb-1 font-semibold uppercase tracking-wider">Active Groq Model</span>
                        <code class="text-emerald-400 font-mono font-bold">openai/gpt-oss-120b</code>
                    </div>

                    <div class="bg-gray-900/80 p-3 rounded-lg border border-gray-700">
                        <span class="text-gray-400 block mb-1 font-semibold uppercase tracking-wider">Meta Cloud API Webhook</span>
                        <code class="text-blue-400 font-mono break-all" id="meta-webhook-url">/api/webhook</code>
                        <div class="mt-1 text-gray-400">Verify Token: <code class="text-yellow-300 font-mono">whatsapp_groq_bot_secret_123</code></div>
                    </div>

                    <div class="bg-gray-900/80 p-3 rounded-lg border border-gray-700">
                        <span class="text-gray-400 block mb-1 font-semibold uppercase tracking-wider">Twilio Webhook (Alternative)</span>
                        <code class="text-purple-400 font-mono break-all" id="twilio-webhook-url">/api/twilio</code>
                    </div>
                </div>
            </div>

            <div class="mt-6 pt-4 border-t border-gray-700 text-xs text-gray-400">
                💡 <span class="font-medium">Quick tip:</span> Test the Groq AI responses live right now on the simulated WhatsApp screen to your right!
            </div>
        </div>

        <!-- Right: Simulated WhatsApp Chat Window -->
        <div class="w-full md:w-1/2 flex flex-col h-full bg-[#111b21]">
            <!-- WhatsApp Chat Header -->
            <div class="bg-[#202c33] px-4 py-3 flex items-center space-x-3 border-b border-gray-700">
                <div class="w-10 h-10 rounded-full bg-emerald-600 flex items-center justify-center text-white font-bold">
                    <i class="fas fa-robot text-lg"></i>
                </div>
                <div>
                    <h2 class="text-sm font-semibold text-white">Groq AI Assistant</h2>
                    <p class="text-xs text-emerald-400">Online • openai/gpt-oss-120b</p>
                </div>
            </div>

            <!-- Messages area -->
            <div id="chat-messages" class="flex-1 p-4 overflow-y-auto space-y-3 chat-bg">
                <div class="flex justify-start">
                    <div class="bg-[#202c33] text-gray-200 text-sm px-3.5 py-2 rounded-2xl rounded-tl-sm max-w-[85%] shadow">
                        Hello! 👋 I am connected to Groq. Send me a message to test how I will reply to you on WhatsApp!
                    </div>
                </div>
            </div>

            <!-- Chat Input -->
            <form id="chat-form" class="bg-[#202c33] px-3 py-2 flex items-center space-x-2 border-t border-gray-700">
                <input 
                    type="text" 
                    id="user-input" 
                    placeholder="Type a test message..." 
                    class="flex-1 bg-[#2a3942] text-sm text-gray-100 placeholder-gray-400 px-4 py-2.5 rounded-lg focus:outline-none focus:ring-1 focus:ring-emerald-500"
                    autocomplete="off"
                    required
                />
                <button 
                    type="submit" 
                    id="send-btn"
                    class="w-10 h-10 bg-emerald-500 hover:bg-emerald-600 text-white rounded-lg flex items-center justify-center transition shadow disabled:opacity-50"
                >
                    <i class="fas fa-paper-plane text-sm"></i>
                </button>
            </form>
        </div>
    </div>

    <script>
        const chatForm = document.getElementById('chat-form');
        const userInput = document.getElementById('user-input');
        const chatMessages = document.getElementById('chat-messages');
        const sendBtn = document.getElementById('send-btn');

        // Display full URLs based on current host
        const base = window.location.origin;
        document.getElementById('meta-webhook-url').innerText = base + '/api/webhook';
        document.getElementById('twilio-webhook-url').innerText = base + '/api/twilio';

        chatForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const text = userInput.value.trim();
            if (!text) return;

            // Append user message
            appendMessage(text, 'user');
            userInput.value = '';
            userInput.disabled = true;
            sendBtn.disabled = true;

            // Loading indicator
            const loadingId = appendLoading();

            try {
                const res = await fetch('/api/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ message: text })
                });
                const data = await res.json();
                removeLoading(loadingId);

                if (data.reply) {
                    appendMessage(data.reply, 'bot');
                } else if (data.error) {
                    appendMessage('⚠️ Error: ' + data.error, 'bot');
                }
            } catch (err) {
                removeLoading(loadingId);
                appendMessage('⚠️ Network Error: ' + err.message, 'bot');
            } finally {
                userInput.disabled = false;
                sendBtn.disabled = false;
                userInput.focus();
            }
        });

        function appendMessage(text, sender) {
            const div = document.createElement('div');
            div.className = sender === 'user' ? 'flex justify-end' : 'flex justify-start';
            const bubble = document.createElement('div');
            bubble.className = sender === 'user' 
                ? 'bg-[#005c4b] text-white text-sm px-3.5 py-2 rounded-2xl rounded-tr-sm max-w-[85%] shadow break-words' 
                : 'bg-[#202c33] text-gray-200 text-sm px-3.5 py-2 rounded-2xl rounded-tl-sm max-w-[85%] shadow break-words whitespace-pre-wrap';
            bubble.textContent = text;
            div.appendChild(bubble);
            chatMessages.appendChild(div);
            chatMessages.scrollTop = chatMessages.scrollHeight;
        }

        function appendLoading() {
            const id = 'loading-' + Date.now();
            const div = document.createElement('div');
            div.id = id;
            div.className = 'flex justify-start';
            div.innerHTML = `
                <div class="bg-[#202c33] text-gray-400 text-xs px-3.5 py-2 rounded-2xl rounded-tl-sm shadow flex items-center space-x-2">
                    <span class="animate-pulse">Thinking with Groq...</span>
                </div>
            `;
            chatMessages.appendChild(div);
            chatMessages.scrollTop = chatMessages.scrollHeight;
            return id;
        }

        function removeLoading(id) {
            const el = document.getElementById(id);
            if (el) el.remove();
        }
    </script>
</body>
</html>
"""
