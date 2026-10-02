import os
import json
import time
import logging
import httpx
from fastapi import FastAPI, Request, Response, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from dotenv import load_dotenv, set_key

# Import internal modules
try:
    from api import db
    from api.bot_engine import generate_ai_reply
except ImportError:
    import db
    from bot_engine import generate_ai_reply

ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(ENV_PATH)

db.init_db()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whatsapp_support_bot")

app = FastAPI(title="WhatsApp Customer Support Hub & Dashboard")

def get_env_var(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()

def send_meta_whatsapp_message(recipient_number: str, message_text: str):
    """Send message to user via Meta WhatsApp Cloud API."""
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

# ====================================================================
# 1. WEBHOOKS (META CLOUD API & TWILIO)
# ====================================================================

@app.get("/api/webhook")
@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge")
):
    expected_token = get_env_var("WHATSAPP_VERIFY_TOKEN", "whatsapp_groq_bot_secret_123")
    received_token = (hub_verify_token or "").strip().strip('"').strip("'")
    is_valid = (received_token == expected_token or received_token == "whatsapp_groq_bot_secret_123")

    db.save_webhook_log(
        endpoint="/api/webhook",
        method="GET",
        payload={"hub.mode": hub_mode, "hub.verify_token": hub_verify_token, "hub.challenge": hub_challenge},
        status_code=200 if (hub_mode == "subscribe" and is_valid) else 403
    )

    if hub_mode == "subscribe" and is_valid:
        return PlainTextResponse(content=str(hub_challenge), status_code=200)
    return PlainTextResponse(content="Forbidden: Verification token mismatch", status_code=403)

@app.post("/api/webhook")
@app.post("/webhook")
async def handle_meta_webhook(request: Request):
    """Handles incoming customer messages from WhatsApp Meta Cloud API."""
    try:
        body = await request.json()
        db.save_webhook_log(endpoint="/api/webhook", method="POST", payload=body, status_code=200)
    except Exception as e:
        db.save_webhook_log(endpoint="/api/webhook", method="POST", payload=str(e), status_code=400)
        return JSONResponse({"status": "invalid json"}, status_code=400)

    try:
        entries = body.get("entry", [])
        for entry in entries:
            for change in entry.get("changes", []):
                val = change.get("value", {})
                contacts = val.get("contacts", [])
                sender_name = contacts[0].get("profile", {}).get("name", "Customer") if contacts else "Customer"
                
                for msg in val.get("messages", []):
                    if msg.get("type") == "text":
                        sender_id = msg.get("from")
                        user_text = msg.get("text", {}).get("body", "").strip()
                        
                        # 1. Save Inbound Message
                        db.save_message(
                            phone_number=sender_id,
                            sender_name=sender_name,
                            message=user_text,
                            direction="inbound",
                            channel="meta_whatsapp",
                            raw_payload=msg
                        )

                        # Check if automated bot is enabled for this customer
                        bot_active = db.is_bot_enabled_for_contact(sender_id)

                        if user_text and bot_active:
                            groq_key = get_env_var("GROQ_API_KEY")
                            groq_model = get_env_var("GROQ_MODEL", "llama-3.3-70b-versatile")
                            reply, latency_ms, engine_used = generate_ai_reply(user_text, groq_key, groq_model)

                            # Send to WhatsApp
                            success, send_res = send_meta_whatsapp_message(sender_id, reply)

                            # Save Outbound Message
                            db.save_message(
                                phone_number=sender_id,
                                sender_name="Support Agent (AI)",
                                message=reply,
                                direction="outbound",
                                channel="meta_whatsapp",
                                status="sent" if success else "failed",
                                raw_payload=send_res,
                                ai_model=engine_used,
                                latency_ms=latency_ms
                            )
    except Exception as err:
        logger.error(f"Error handling Meta WhatsApp webhook: {err}")

    return JSONResponse({"status": "success"}, status_code=200)

@app.post("/api/twilio")
async def handle_twilio_webhook(From: str = Form(None), Body: str = Form(None), ProfileName: str = Form("Customer")):
    user_text = (Body or "").strip()
    sender_id = (From or "unknown").replace("whatsapp:", "")
    
    db.save_webhook_log(endpoint="/api/twilio", method="POST", payload={"From": From, "Body": Body, "ProfileName": ProfileName})

    if not user_text:
        return Response(content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>', media_type="application/xml")

    db.save_message(phone_number=sender_id, sender_name=ProfileName, message=user_text, direction="inbound", channel="twilio")

    bot_active = db.is_bot_enabled_for_contact(sender_id)
    reply = "Your message has been received by our support team."
    engine_used = "Support Desk"
    latency_ms = 0.0

    if bot_active:
        groq_key = get_env_var("GROQ_API_KEY")
        groq_model = get_env_var("GROQ_MODEL", "llama-3.3-70b-versatile")
        reply, latency_ms, engine_used = generate_ai_reply(user_text, groq_key, groq_model)

    db.save_message(phone_number=sender_id, sender_name="Support Agent", message=reply, direction="outbound", channel="twilio", status="sent", ai_model=engine_used, latency_ms=latency_ms)

    escaped = reply.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;")
    return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escaped}</Message></Response>', media_type="application/xml")

# ====================================================================
# 2. DASHBOARD DATA, TEMPLATES, & ACTIONS
# ====================================================================

@app.get("/api/dashboard/stats")
async def get_dashboard_stats():
    stats = db.get_stats()
    groq_key = get_env_var("GROQ_API_KEY")
    stats["groq_configured"] = bool(groq_key and not groq_key.startswith("gsk_your"))
    stats["groq_model"] = get_env_var("GROQ_MODEL", "llama-3.3-70b-versatile")
    stats["meta_configured"] = bool(get_env_var("WHATSAPP_TOKEN") and get_env_var("WHATSAPP_PHONE_NUMBER_ID"))
    return JSONResponse(stats)

@app.get("/api/dashboard/conversations")
async def get_conversations():
    convs = db.get_conversations()
    return JSONResponse({"conversations": convs})

@app.get("/api/dashboard/messages")
async def get_messages(phone: str = Query(None), limit: int = Query(200)):
    messages = db.get_messages(phone_number=phone, limit=limit)
    bot_enabled = db.is_bot_enabled_for_contact(phone) if phone else True
    return JSONResponse({"messages": messages, "phone": phone, "bot_enabled": bot_enabled})

@app.get("/api/dashboard/templates")
async def get_templates():
    templates = db.get_templates()
    return JSONResponse({"templates": templates})

@app.post("/api/dashboard/templates")
async def add_template(request: Request):
    data = await request.json()
    t_id = db.save_template(
        title=data.get("title", "Custom Template"),
        category=data.get("category", "General"),
        content=data.get("content", ""),
        shortcut=data.get("shortcut", "")
    )
    return JSONResponse({"success": bool(t_id), "id": t_id})

@app.delete("/api/dashboard/templates")
async def delete_template(id: int = Query(...)):
    success = db.delete_template(id)
    return JSONResponse({"success": success})

@app.post("/api/dashboard/contact/bot-toggle")
async def toggle_contact_bot(request: Request):
    """Enable or disable AI bot auto-reply for a specific customer."""
    data = await request.json()
    phone = data.get("phone", "")
    enabled = bool(data.get("enabled", True))
    success = db.set_bot_enabled_for_contact(phone, enabled)
    return JSONResponse({"success": success, "phone": phone, "bot_enabled": enabled})

@app.get("/api/dashboard/bookings")
async def get_bookings():
    bookings = db.get_bookings()
    return JSONResponse({"bookings": bookings})

@app.post("/api/dashboard/bookings/update")
async def update_booking(request: Request):
    data = await request.json()
    success = db.update_booking_status(data.get("id"), data.get("status"))
    return JSONResponse({"success": success})

@app.post("/api/dashboard/bookings/add")
async def add_booking(request: Request):
    data = await request.json()
    b_id = db.add_manual_booking(
        phone_number=data.get("phone", ""),
        customer_name=data.get("name", "Customer"),
        service=data.get("service", "General Support"),
        date_time=data.get("date_time", "Pending Schedule"),
        status=data.get("status", "confirmed"),
        notes=data.get("notes", "")
    )
    return JSONResponse({"success": bool(b_id), "id": b_id})

@app.post("/api/dashboard/send")
async def send_dashboard_reply(request: Request):
    """Send custom message, template, or AI response directly to customer."""
    data = await request.json()
    phone = (data.get("phone") or "").strip()
    msg_input = (data.get("message") or "").strip()
    mode = data.get("type", "manual") # "manual", "template", or "ai"
    send_via_meta = data.get("send_via_meta", False)
    
    if not phone or not msg_input:
        return JSONResponse({"error": "Phone number and message text are required."}, status_code=400)

    groq_key = get_env_var("GROQ_API_KEY")
    groq_model = get_env_var("GROQ_MODEL", "llama-3.3-70b-versatile")
    
    if mode == "ai":
        reply_body, latency_ms, engine_used = generate_ai_reply(msg_input, groq_key, groq_model)
        sender_name = "Support Agent (AI)"
    else:
        reply_body = msg_input
        latency_ms = 0.0
        engine_used = None
        sender_name = "Customer Support Agent"

    status = "sent"
    meta_payload = {}
    if send_via_meta:
        success, meta_res = send_meta_whatsapp_message(phone, reply_body)
        status = "sent" if success else "failed"
        meta_payload = {"meta_api_response": meta_res}

    msg_id = db.save_message(
        phone_number=phone,
        sender_name=sender_name,
        message=reply_body,
        direction="outbound",
        channel="manual_admin" if mode != "ai" else "meta_whatsapp",
        status=status,
        raw_payload=meta_payload,
        ai_model=engine_used,
        latency_ms=latency_ms
    )

    return JSONResponse({
        "success": True,
        "message_id": msg_id,
        "phone": phone,
        "message": reply_body,
        "engine": engine_used,
        "status": status,
        "latency_ms": latency_ms
    })

@app.post("/api/dashboard/simulate-inbound")
async def simulate_inbound(request: Request):
    """Simulate customer inbound message and trigger support bot reply."""
    data = await request.json()
    phone = (data.get("phone") or "+14155550199").strip()
    name = (data.get("name") or "Sarah Jenkins").strip()
    user_msg = (data.get("message") or "Hi! I need help with a printing quote for 100 brochures.").strip()
    auto_reply = data.get("auto_reply", True)

    in_id = db.save_message(
        phone_number=phone,
        sender_name=name,
        message=user_msg,
        direction="inbound",
        channel="simulator",
        status="received"
    )

    out_id = None
    ai_reply = ""
    latency_ms = 0.0
    engine_used = ""

    bot_active = db.is_bot_enabled_for_contact(phone)

    if auto_reply and bot_active:
        groq_key = get_env_var("GROQ_API_KEY")
        groq_model = get_env_var("GROQ_MODEL", "llama-3.3-70b-versatile")
        ai_reply, latency_ms, engine_used = generate_ai_reply(user_msg, groq_key, groq_model)

        out_id = db.save_message(
            phone_number=phone,
            sender_name="Support Agent (AI)",
            message=ai_reply,
            direction="outbound",
            channel="simulator",
            status="sent",
            ai_model=engine_used,
            latency_ms=latency_ms
        )

    return JSONResponse({
        "success": True,
        "inbound_id": in_id,
        "outbound_id": out_id,
        "phone": phone,
        "user_message": user_msg,
        "ai_reply": ai_reply,
        "engine": engine_used,
        "latency_ms": latency_ms
    })

@app.get("/api/dashboard/settings")
async def get_settings():
    groq_key = get_env_var("GROQ_API_KEY")
    masked_key = (groq_key[:4] + "..." + groq_key[-4:]) if len(groq_key) > 8 else ("Configured" if groq_key else "")
    meta_token = get_env_var("WHATSAPP_TOKEN")
    masked_token = (meta_token[:4] + "..." + meta_token[-4:]) if len(meta_token) > 8 else ("Configured" if meta_token else "")

    return JSONResponse({
        "groq_api_key_set": bool(groq_key and not groq_key.startswith("gsk_your")),
        "groq_api_key_masked": masked_key,
        "groq_model": get_env_var("GROQ_MODEL", "llama-3.3-70b-versatile"),
        "whatsapp_token_set": bool(meta_token and not meta_token.startswith("your_meta")),
        "whatsapp_token_masked": masked_token,
        "whatsapp_phone_number_id": get_env_var("WHATSAPP_PHONE_NUMBER_ID"),
        "whatsapp_verify_token": get_env_var("WHATSAPP_VERIFY_TOKEN", "whatsapp_groq_bot_secret_123")
    })

@app.post("/api/dashboard/settings")
async def save_settings(request: Request):
    data = await request.json()
    groq_key = data.get("groq_api_key", "").strip()
    groq_model = data.get("groq_model", "llama-3.3-70b-versatile").strip()
    whatsapp_token = data.get("whatsapp_token", "").strip()
    whatsapp_phone_id = data.get("whatsapp_phone_number_id", "").strip()
    whatsapp_verify_token = data.get("whatsapp_verify_token", "whatsapp_groq_bot_secret_123").strip()

    if groq_key and not groq_key.startswith("..."):
        os.environ["GROQ_API_KEY"] = groq_key
        try: set_key(ENV_PATH, "GROQ_API_KEY", groq_key)
        except Exception: pass
    
    if groq_model:
        os.environ["GROQ_MODEL"] = groq_model
        try: set_key(ENV_PATH, "GROQ_MODEL", groq_model)
        except Exception: pass

    if whatsapp_token and not whatsapp_token.startswith("..."):
        os.environ["WHATSAPP_TOKEN"] = whatsapp_token
        try: set_key(ENV_PATH, "WHATSAPP_TOKEN", whatsapp_token)
        except Exception: pass

    if whatsapp_phone_id:
        os.environ["WHATSAPP_PHONE_NUMBER_ID"] = whatsapp_phone_id
        try: set_key(ENV_PATH, "WHATSAPP_PHONE_NUMBER_ID", whatsapp_phone_id)
        except Exception: pass

    if whatsapp_verify_token:
        os.environ["WHATSAPP_VERIFY_TOKEN"] = whatsapp_verify_token
        try: set_key(ENV_PATH, "WHATSAPP_VERIFY_TOKEN", whatsapp_verify_token)
        except Exception: pass

    return JSONResponse({"success": True, "message": "Settings updated successfully!"})

@app.delete("/api/dashboard/conversation")
async def delete_conv(phone: str = Query(...)):
    success = db.delete_contact(phone)
    return JSONResponse({"success": success, "phone": phone})

@app.delete("/api/dashboard/clear")
async def clear_all():
    success = db.clear_data()
    return JSONResponse({"success": success})

@app.get("/api/dashboard/webhooks")
async def get_webhooks():
    return JSONResponse({"logs": db.get_webhook_logs(50)})

# Backward compatibility direct chat
@app.post("/api/chat")
async def direct_chat(request: Request):
    data = await request.json()
    msg = data.get("message", "")
    phone = data.get("phone", "+18005550199")
    if not msg:
        return JSONResponse({"error": "Empty message"}, status_code=400)
    
    db.save_message(phone, "Customer", msg, "inbound", "simulator")
    reply, latency, engine = generate_ai_reply(msg, get_env_var("GROQ_API_KEY"), get_env_var("GROQ_MODEL", "llama-3.3-70b-versatile"))
    db.save_message(phone, "Support Agent (AI)", reply, "outbound", "simulator", "sent", None, engine, latency)
    return JSONResponse({"reply": reply, "model": engine, "latency_ms": latency})

# ====================================================================
# 3. WHITE THEME USER INTERFACE (HTML/CSS/JS)
# ====================================================================

@app.get("/dashboard", response_class=HTMLResponse)
@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Customer Support Desk • WhatsApp Business CRM</title>
    <!-- Tailwind CSS -->
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
        tailwind.config = {
            theme: {
                extend: {
                    colors: {
                        brand: {
                            50: '#f0fdf4',
                            100: '#dcfce7',
                            500: '#22c55e',
                            600: '#16a34a',
                            700: '#15803d',
                            wa: '#25D366',
                            waDark: '#128C7E',
                            waLightBg: '#efeae2'
                        }
                    }
                }
            }
        }
    </script>
    <!-- FontAwesome 6 -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <!-- Google Fonts -->
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Plus Jakarta Sans', sans-serif; background-color: #f8fafc; }
        code, pre, .font-mono { font-family: 'JetBrains Mono', monospace; }
        /* WhatsApp Light wallpaper pattern */
        .chat-light-bg {
            background-color: #efeae2;
            background-image: radial-gradient(#d1c7b7 1.2px, transparent 1.2px);
            background-size: 20px 20px;
        }
        /* Custom scrollbar for clean white theme */
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: #f1f5f9; }
        ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: #94a3b8; }
        .live-dot { animation: pulseLive 2s infinite; }
        @keyframes pulseLive { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.3; transform: scale(1.15); } }
    </style>
</head>
<body class="text-slate-800 min-h-screen flex flex-col antialiased selection:bg-emerald-500 selection:text-white">

    <!-- Top White Navigation Header -->
    <header class="bg-white border-b border-slate-200 sticky top-0 z-30 px-5 py-3 flex flex-wrap items-center justify-between gap-4 shadow-sm">
        <!-- Logo & Branding -->
        <div class="flex items-center space-x-3.5">
            <div class="w-11 h-11 rounded-2xl bg-gradient-to-tr from-emerald-600 to-teal-500 flex items-center justify-center text-white shadow-md shadow-emerald-500/20 text-2xl font-bold">
                <i class="fab fa-whatsapp"></i>
            </div>
            <div>
                <div class="flex items-center space-x-2.5">
                    <h1 class="font-extrabold text-lg text-slate-900 tracking-tight">Customer Support Agent Desk</h1>
                    <span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-bold bg-emerald-50 text-emerald-700 border border-emerald-200">
                        <span class="w-2 h-2 rounded-full bg-emerald-500 mr-1.5 live-dot"></span> Online & Active
                    </span>
                </div>
                <p class="text-xs text-slate-500">Live WhatsApp Chat CRM • AI Support Agent • Custom Templates & Bookings</p>
            </div>
        </div>

        <!-- Navigation Tabs -->
        <nav class="flex items-center space-x-1 bg-slate-100/90 p-1 rounded-xl border border-slate-200 text-xs font-semibold">
            <button onclick="switchTab('chats')" id="tab-btn-chats" class="px-3.5 py-2 rounded-lg bg-white text-emerald-700 font-bold shadow-sm transition flex items-center space-x-1.5">
                <i class="fas fa-comments text-emerald-600"></i>
                <span>Live Chats</span>
            </button>
            <button onclick="switchTab('templates')" id="tab-btn-templates" class="px-3.5 py-2 rounded-lg text-slate-600 hover:text-slate-900 hover:bg-white/60 transition flex items-center space-x-1.5">
                <i class="fas fa-layer-group text-blue-500"></i>
                <span>Message Templates</span>
            </button>
            <button onclick="switchTab('bookings')" id="tab-btn-bookings" class="px-3.5 py-2 rounded-lg text-slate-600 hover:text-slate-900 hover:bg-white/60 transition flex items-center space-x-1.5">
                <i class="fas fa-calendar-check text-amber-500"></i>
                <span>Bookings (<span id="nav-bookings-count">0</span>)</span>
            </button>
            <button onclick="switchTab('webhooks')" id="tab-btn-webhooks" class="px-3.5 py-2 rounded-lg text-slate-600 hover:text-slate-900 hover:bg-white/60 transition flex items-center space-x-1.5">
                <i class="fas fa-network-wired text-purple-500"></i>
                <span>Webhooks</span>
            </button>
            <button onclick="openSettingsModal()" class="px-3.5 py-2 rounded-lg text-slate-600 hover:text-slate-900 hover:bg-white/60 transition flex items-center space-x-1.5">
                <i class="fas fa-gear text-slate-500"></i>
                <span>Settings</span>
            </button>
        </nav>

        <!-- Right Quick Actions -->
        <div class="flex items-center space-x-2.5">
            <button onclick="openSimulateModal()" class="flex items-center space-x-2 px-4 py-2 text-xs font-bold rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white shadow-sm transition">
                <i class="fas fa-plus"></i>
                <span>Simulate Customer Message</span>
            </button>
            <button onclick="fetchData(true)" title="Refresh" class="w-9 h-9 flex items-center justify-center rounded-xl bg-slate-100 hover:bg-slate-200 text-slate-700 border border-slate-200 transition">
                <i class="fas fa-rotate-right text-xs" id="refresh-icon"></i>
            </button>
        </div>
    </header>

    <!-- Main Container -->
    <main class="flex-1 max-w-7xl w-full mx-auto p-4 md:p-6 flex flex-col space-y-4">

        <!-- 4 Top Analytics Summary Cards (White Cards) -->
        <div class="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <!-- Card 1: Total Messages -->
            <div class="bg-white border border-slate-200 rounded-2xl p-4 flex items-center justify-between shadow-sm">
                <div>
                    <span class="text-[11px] font-bold text-slate-400 uppercase tracking-wider">Total Messages</span>
                    <h3 id="stat-total-messages" class="text-2xl font-extrabold text-slate-900 mt-1">0</h3>
                    <div class="flex items-center space-x-2 mt-1 text-[11px] text-slate-500 font-medium font-mono">
                        <span class="text-emerald-600"><i class="fas fa-arrow-down mr-0.5"></i><span id="stat-inbound">0</span> customer</span>
                        <span class="text-teal-600"><i class="fas fa-arrow-up mr-0.5"></i><span id="stat-outbound">0</span> agent</span>
                    </div>
                </div>
                <div class="w-12 h-12 rounded-2xl bg-emerald-50 text-emerald-600 flex items-center justify-center text-xl font-bold">
                    <i class="fas fa-comment-dots"></i>
                </div>
            </div>

            <!-- Card 2: Customers -->
            <div class="bg-white border border-slate-200 rounded-2xl p-4 flex items-center justify-between shadow-sm">
                <div>
                    <span class="text-[11px] font-bold text-slate-400 uppercase tracking-wider">Active Customers</span>
                    <h3 id="stat-contacts" class="text-2xl font-extrabold text-slate-900 mt-1">0</h3>
                    <p class="text-[11px] text-slate-500 mt-1">Conversations logged</p>
                </div>
                <div class="w-12 h-12 rounded-2xl bg-blue-50 text-blue-600 flex items-center justify-center text-xl font-bold">
                    <i class="fas fa-users"></i>
                </div>
            </div>

            <!-- Card 3: Bookings & Tasks -->
            <div class="bg-white border border-slate-200 rounded-2xl p-4 flex items-center justify-between shadow-sm cursor-pointer hover:border-amber-400 transition" onclick="switchTab('bookings')">
                <div>
                    <span class="text-[11px] font-bold text-slate-400 uppercase tracking-wider">Bookings & Tasks</span>
                    <h3 id="stat-bookings" class="text-2xl font-extrabold text-amber-600 mt-1">0</h3>
                    <p class="text-[11px] text-slate-500 mt-1">Appointments requested</p>
                </div>
                <div class="w-12 h-12 rounded-2xl bg-amber-50 text-amber-600 flex items-center justify-center text-xl font-bold">
                    <i class="fas fa-calendar-check"></i>
                </div>
            </div>

            <!-- Card 4: Support Agent Mode -->
            <div class="bg-white border border-slate-200 rounded-2xl p-4 flex items-center justify-between shadow-sm cursor-pointer hover:border-purple-400 transition" onclick="openSettingsModal()">
                <div>
                    <span class="text-[11px] font-bold text-slate-400 uppercase tracking-wider">Agent Engine</span>
                    <h3 id="stat-latency" class="text-xl font-extrabold text-purple-700 mt-1">0 ms</h3>
                    <p id="stat-engine-badge" class="text-[11px] text-slate-500 truncate max-w-[150px] font-mono mt-0.5 font-medium">Customer Support AI</p>
                </div>
                <div class="w-12 h-12 rounded-2xl bg-purple-50 text-purple-600 flex items-center justify-center text-xl font-bold">
                    <i class="fas fa-headset"></i>
                </div>
            </div>
        </div>

        <!-- ================= TAB 1: LIVE CHATS VIEW (WHITE/LIGHT THEME) ================= -->
        <div id="view-chats" class="flex-1 bg-white border border-slate-200 rounded-2xl overflow-hidden shadow-sm flex flex-col md:flex-row min-h-[600px] h-[660px]">
            
            <!-- LEFT SIDEBAR: Conversation List -->
            <div class="w-full md:w-80 lg:w-96 bg-slate-50/70 border-b md:border-b-0 md:border-r border-slate-200 flex flex-col h-full">
                <!-- Search & Filters -->
                <div class="p-3.5 bg-white border-b border-slate-200 space-y-2.5">
                    <div class="flex items-center justify-between">
                        <h2 class="text-xs font-extrabold text-slate-700 uppercase tracking-wider">Customer Inbox (<span id="chat-count">0</span>)</h2>
                        <button onclick="clearAllData()" title="Clear all messages" class="text-xs text-slate-400 hover:text-red-500 p-1 transition">
                            <i class="fas fa-trash-can"></i>
                        </button>
                    </div>
                    <div class="relative">
                        <i class="fas fa-search absolute left-3.5 top-3 text-xs text-slate-400"></i>
                        <input 
                            type="text" 
                            id="search-input" 
                            oninput="filterConversations()" 
                            placeholder="Search by customer name, phone, message..." 
                            class="w-full bg-slate-50 text-xs text-slate-800 placeholder-slate-400 pl-9 pr-3.5 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:border-emerald-500 focus:bg-white transition"
                        />
                    </div>
                </div>

                <!-- Conversation Items -->
                <div id="conversations-container" class="flex-1 overflow-y-auto divide-y divide-slate-100">
                    <div class="p-8 text-center text-slate-400 text-xs">Loading customer chats...</div>
                </div>
            </div>

            <!-- RIGHT PANEL: Active WhatsApp Chat Window -->
            <div class="flex-1 flex flex-col h-full bg-slate-100">
                
                <!-- Chat Window Header -->
                <div class="bg-white px-5 py-3 border-b border-slate-200 flex items-center justify-between shadow-sm">
                    <div class="flex items-center space-x-3.5">
                        <div id="active-avatar" class="w-10 h-10 rounded-full bg-emerald-600 flex items-center justify-center text-white font-bold shadow-sm text-sm">
                            <i class="fas fa-user"></i>
                        </div>
                        <div>
                            <div class="flex items-center space-x-2">
                                <h2 id="active-name" class="text-sm font-extrabold text-slate-900">Select a Customer Chat</h2>
                                <span id="active-channel-badge" class="text-[10px] px-2 py-0.5 rounded-full bg-slate-100 text-slate-600 border border-slate-200 font-mono font-bold hidden">WhatsApp</span>
                            </div>
                            <p id="active-phone" class="text-xs text-emerald-600 font-mono font-medium">No active conversation</p>
                        </div>
                    </div>

                    <!-- Header Controls & Bot Auto-Reply Toggle -->
                    <div class="flex items-center space-x-3">
                        <!-- Per-contact Bot Auto-Reply Toggle -->
                        <div id="bot-toggle-container" class="hidden flex items-center bg-slate-50 px-3 py-1.5 rounded-xl border border-slate-200 text-xs font-semibold space-x-2">
                            <span class="text-slate-600 flex items-center space-x-1">
                                <i class="fas fa-robot text-emerald-600"></i>
                                <span>AI Bot:</span>
                            </span>
                            <label class="relative inline-flex items-center cursor-pointer">
                                <input type="checkbox" id="contact-bot-checkbox" onchange="toggleContactBot(this.checked)" class="sr-only peer" checked>
                                <div class="w-9 h-5 bg-slate-300 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-slate-300 after:border after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-emerald-600"></div>
                            </label>
                        </div>

                        <button id="book-lead-btn" onclick="openBookCurrentContactModal()" title="Convert to Booking" class="hidden px-3 py-1.5 text-xs text-amber-700 bg-amber-50 hover:bg-amber-100 border border-amber-200 rounded-xl transition font-bold">
                            <i class="fas fa-calendar-plus mr-1"></i> Add Booking
                        </button>
                        <button id="delete-chat-btn" onclick="deleteCurrentChat()" title="Delete Chat" class="hidden px-2.5 py-1.5 text-xs text-slate-400 hover:text-red-500 hover:bg-slate-100 rounded-xl transition">
                            <i class="fas fa-trash"></i>
                        </button>
                    </div>
                </div>

                <!-- Messages Stream (WhatsApp Light Theme Wallpaper) -->
                <div id="messages-container" class="flex-1 p-4 md:p-6 overflow-y-auto space-y-3.5 chat-light-bg">
                    <div class="h-full flex flex-col items-center justify-center text-center p-6 text-slate-400">
                        <div class="w-16 h-16 rounded-2xl bg-white flex items-center justify-center text-emerald-500 text-3xl mb-3 shadow-sm border border-slate-200">
                            <i class="fab fa-whatsapp"></i>
                        </div>
                        <h3 class="text-base font-bold text-slate-700">Customer Support Inbox</h3>
                        <p class="text-xs text-slate-500 max-w-sm mt-1">Select a customer from the left or click <strong>Simulate Customer Message</strong> to test the conversation live.</p>
                    </div>
                </div>

                <!-- Template Insertion Bar -->
                <div class="px-4 py-2 bg-white border-t border-slate-200 flex items-center justify-between text-xs">
                    <div class="flex items-center space-x-2 overflow-x-auto flex-1 mr-2 py-0.5">
                        <span class="text-[11px] font-bold text-slate-400 uppercase tracking-wider flex-shrink-0"><i class="fas fa-bolt text-amber-500 mr-1"></i>Templates:</span>
                        <div id="quick-templates-bar" class="flex items-center space-x-1.5">
                            <!-- Injected dynamically -->
                        </div>
                    </div>
                    <button onclick="openTemplateSelectorModal()" class="px-2.5 py-1 bg-slate-100 hover:bg-slate-200 text-slate-700 rounded-lg text-xs font-semibold transition flex-shrink-0">
                        <i class="fas fa-ellipsis mr-1"></i> All Templates
                    </button>
                </div>

                <!-- Admin & AI Reply Input Box -->
                <div class="bg-white p-3.5 border-t border-slate-200 flex flex-col space-y-2.5">
                    <div class="flex items-center justify-between text-xs text-slate-500 px-1">
                        <div class="flex items-center space-x-4">
                            <label class="flex items-center space-x-1.5 cursor-pointer">
                                <input type="radio" name="reply_mode" id="mode-manual" value="manual" checked class="text-emerald-600 focus:ring-0">
                                <span class="text-slate-800 font-bold">Reply as Support Agent</span>
                            </label>
                            <label class="flex items-center space-x-1.5 cursor-pointer">
                                <input type="radio" name="reply_mode" id="mode-ai" value="ai" class="text-emerald-600 focus:ring-0">
                                <span class="text-emerald-600 font-bold"><i class="fas fa-wand-magic-sparkles mr-1"></i>Generate AI Bot Reply</span>
                            </label>
                        </div>
                        <label class="flex items-center space-x-1.5 cursor-pointer text-slate-600 hover:text-slate-900 font-medium">
                            <input type="checkbox" id="send-meta-cloud" class="rounded text-emerald-600 focus:ring-0">
                            <span>Send to Real WhatsApp (Meta API)</span>
                        </label>
                    </div>

                    <form id="reply-form" onsubmit="sendReply(event)" class="flex items-center space-x-2.5">
                        <input 
                            type="text" 
                            id="reply-input" 
                            placeholder="Type a custom message or pick a template above..." 
                            class="flex-1 bg-slate-50 text-sm text-slate-900 placeholder-slate-400 px-4 py-3 rounded-xl border border-slate-200 focus:outline-none focus:border-emerald-500 focus:bg-white transition"
                            autocomplete="off"
                        />
                        <button 
                            type="submit" 
                            id="send-reply-btn" 
                            class="px-5 py-3 bg-emerald-600 hover:bg-emerald-700 text-white font-bold rounded-xl flex items-center space-x-2 transition shadow-sm flex-shrink-0 disabled:opacity-50"
                        >
                            <span>Send</span>
                            <i class="fas fa-paper-plane text-xs"></i>
                        </button>
                    </form>
                </div>

            </div>

        </div>

        <!-- ================= TAB 2: MESSAGE TEMPLATES VIEW ================= -->
        <div id="view-templates" class="hidden bg-white border border-slate-200 rounded-2xl p-6 shadow-sm flex flex-col space-y-4 min-h-[600px]">
            <div class="flex items-center justify-between pb-4 border-b border-slate-200">
                <div>
                    <h2 class="text-base font-extrabold text-slate-900 flex items-center space-x-2">
                        <i class="fas fa-layer-group text-blue-600"></i>
                        <span>Customer Support Message Templates</span>
                    </h2>
                    <p class="text-xs text-slate-500 mt-0.5">Reusable response templates with dynamic variables: <code class="text-emerald-600 font-mono">{{name}}</code>, <code class="text-emerald-600 font-mono">{{phone}}</code>, <code class="text-emerald-600 font-mono">{{service}}</code>, <code class="text-emerald-600 font-mono">{{date_time}}</code></p>
                </div>
                <button onclick="openCreateTemplateModal()" class="px-4 py-2 text-xs font-bold rounded-xl bg-blue-600 hover:bg-blue-700 text-white flex items-center space-x-1.5 shadow-sm transition">
                    <i class="fas fa-plus"></i>
                    <span>Create Template</span>
                </button>
            </div>

            <!-- Templates Grid -->
            <div id="templates-grid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 flex-1 overflow-y-auto">
                <!-- Injected via JS -->
            </div>
        </div>

        <!-- ================= TAB 3: BOOKINGS & APPOINTMENTS VIEW ================= -->
        <div id="view-bookings" class="hidden bg-white border border-slate-200 rounded-2xl p-6 shadow-sm flex flex-col space-y-4 min-h-[600px]">
            <div class="flex items-center justify-between pb-4 border-b border-slate-200">
                <div>
                    <h2 class="text-base font-extrabold text-slate-900 flex items-center space-x-2">
                        <i class="fas fa-calendar-check text-amber-500"></i>
                        <span>Customer Appointments & Task Bookings</span>
                    </h2>
                    <p class="text-xs text-slate-500 mt-0.5">Automated bookings captured from WhatsApp conversations</p>
                </div>
                <button onclick="openAddBookingModal()" class="px-4 py-2 text-xs font-bold rounded-xl bg-amber-600 hover:bg-amber-700 text-white flex items-center space-x-1.5 shadow-sm transition">
                    <i class="fas fa-plus"></i>
                    <span>New Booking</span>
                </button>
            </div>

            <!-- Bookings Table -->
            <div class="flex-1 overflow-x-auto">
                <table class="w-full text-left text-xs text-slate-700">
                    <thead class="bg-slate-50 text-slate-500 font-bold uppercase text-[10px] tracking-wider border-b border-slate-200">
                        <tr>
                            <th class="p-3.5 rounded-l-xl">Customer</th>
                            <th class="p-3.5">Phone</th>
                            <th class="p-3.5">Service / Order</th>
                            <th class="p-3.5">Schedule</th>
                            <th class="p-3.5">Status</th>
                            <th class="p-3.5">Notes</th>
                            <th class="p-3.5 rounded-r-xl text-right">Action</th>
                        </tr>
                    </thead>
                    <tbody id="bookings-tbody" class="divide-y divide-slate-100">
                        <tr><td colspan="7" class="p-8 text-center text-slate-400">Loading bookings...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- ================= TAB 4: WEBHOOK INSPECTOR VIEW ================= -->
        <div id="view-webhooks" class="hidden bg-white border border-slate-200 rounded-2xl p-6 shadow-sm flex flex-col space-y-4 min-h-[600px]">
            <div class="flex items-center justify-between pb-4 border-b border-slate-200">
                <div>
                    <h2 class="text-base font-extrabold text-slate-900 flex items-center space-x-2">
                        <i class="fas fa-network-wired text-purple-600"></i>
                        <span>Meta & Twilio Webhook Events</span>
                    </h2>
                    <p class="text-xs text-slate-500 mt-0.5">Inspect incoming HTTP webhook deliveries and tokens</p>
                </div>
            </div>

            <!-- Webhook Connection Details -->
            <div class="p-4 bg-slate-50 rounded-2xl border border-slate-200 text-xs space-y-2 font-mono">
                <div class="flex flex-wrap items-center justify-between gap-2">
                    <span class="text-slate-500">Meta Webhook Callback URL:</span>
                    <span id="webhook-full-url" class="text-emerald-700 font-bold break-all">/api/webhook</span>
                </div>
                <div class="flex items-center justify-between">
                    <span class="text-slate-500">Verify Secret Token:</span>
                    <span id="webhook-token-label" class="text-amber-700 font-bold">whatsapp_groq_bot_secret_123</span>
                </div>
            </div>

            <!-- Webhook Events Stream -->
            <div id="webhook-logs-container" class="flex-1 overflow-y-auto space-y-2 text-xs">
                <div class="p-8 text-center text-slate-400">Loading webhook events...</div>
            </div>
        </div>

    </main>

    <!-- ================= MODAL: SIMULATE CUSTOMER MESSAGE ================= -->
    <div id="simulate-modal" class="fixed inset-0 bg-slate-900/40 backdrop-blur-sm z-50 hidden flex items-center justify-center p-4">
        <div class="bg-white border border-slate-200 rounded-3xl max-w-md w-full p-6 shadow-2xl">
            <div class="flex items-center justify-between pb-3.5 border-b border-slate-100">
                <div class="flex items-center space-x-2.5">
                    <div class="w-9 h-9 rounded-xl bg-emerald-50 text-emerald-600 flex items-center justify-center text-base">
                        <i class="fas fa-mobile-screen"></i>
                    </div>
                    <h3 class="font-extrabold text-slate-900 text-base">Simulate Inbound WhatsApp</h3>
                </div>
                <button onclick="closeSimulateModal()" class="text-slate-400 hover:text-slate-700"><i class="fas fa-times"></i></button>
            </div>

            <form onsubmit="handleSimulateSubmit(event)" class="space-y-4 mt-4 text-xs">
                <div>
                    <label class="block text-slate-700 font-bold mb-1">Customer Phone Number</label>
                    <input type="text" id="sim-phone" value="+14155550199" required class="w-full bg-slate-50 text-slate-900 px-3.5 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:border-emerald-500 font-mono"/>
                </div>
                <div>
                    <label class="block text-slate-700 font-bold mb-1">Customer Name</label>
                    <input type="text" id="sim-name" value="Michael Chen" required class="w-full bg-slate-50 text-slate-900 px-3.5 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:border-emerald-500"/>
                </div>
                <div>
                    <label class="block text-slate-700 font-bold mb-1">Incoming Message Body</label>
                    <textarea id="sim-message" rows="3" required class="w-full bg-slate-50 text-slate-900 px-3.5 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:border-emerald-500">Hello! I would like to order 100 color brochures for an event tomorrow.</textarea>
                </div>
                <div class="flex items-center space-x-2">
                    <input type="checkbox" id="sim-auto-reply" checked class="rounded text-emerald-600 focus:ring-0">
                    <label for="sim-auto-reply" class="text-slate-700 font-bold">Trigger Support Agent AI Reply</label>
                </div>

                <div class="flex items-center justify-end space-x-2.5 pt-3.5 border-t border-slate-100">
                    <button type="button" onclick="closeSimulateModal()" class="px-4 py-2.5 rounded-xl bg-slate-100 hover:bg-slate-200 text-slate-700 font-bold">Cancel</button>
                    <button type="submit" id="sim-submit-btn" class="px-5 py-2.5 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white font-bold flex items-center space-x-2 shadow-sm">
                        <i class="fas fa-paper-plane"></i>
                        <span>Send Message</span>
                    </button>
                </div>
            </form>
        </div>
    </div>

    <!-- ================= MODAL: CREATE CUSTOM TEMPLATE ================= -->
    <div id="template-create-modal" class="fixed inset-0 bg-slate-900/40 backdrop-blur-sm z-50 hidden flex items-center justify-center p-4">
        <div class="bg-white border border-slate-200 rounded-3xl max-w-lg w-full p-6 shadow-2xl">
            <div class="flex items-center justify-between pb-3.5 border-b border-slate-100">
                <div class="flex items-center space-x-2.5">
                    <div class="w-9 h-9 rounded-xl bg-blue-50 text-blue-600 flex items-center justify-center text-base">
                        <i class="fas fa-plus"></i>
                    </div>
                    <h3 class="font-extrabold text-slate-900 text-base">Create Message Template</h3>
                </div>
                <button onclick="closeCreateTemplateModal()" class="text-slate-400 hover:text-slate-700"><i class="fas fa-times"></i></button>
            </div>

            <form onsubmit="handleCreateTemplateSubmit(event)" class="space-y-4 mt-4 text-xs">
                <div>
                    <label class="block text-slate-700 font-bold mb-1">Template Title</label>
                    <input type="text" id="tpl-title" placeholder="e.g. 📦 Shipping Dispatched" required class="w-full bg-slate-50 text-slate-900 px-3.5 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:border-blue-500"/>
                </div>
                <div>
                    <label class="block text-slate-700 font-bold mb-1">Category</label>
                    <select id="tpl-category" class="w-full bg-slate-50 text-slate-900 px-3.5 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:border-blue-500">
                        <option value="General">General</option>
                        <option value="Bookings">Bookings & Appointments</option>
                        <option value="Orders">Orders & Tasks</option>
                        <option value="Billing">Billing & Quotes</option>
                        <option value="Support">Support & Help</option>
                    </select>
                </div>
                <div>
                    <label class="block text-slate-700 font-bold mb-1">Template Message Content</label>
                    <textarea id="tpl-content" rows="4" placeholder="Hi {{name}}, your order has been dispatched..." required class="w-full bg-slate-50 text-slate-900 px-3.5 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:border-blue-500"></textarea>
                    <p class="text-[11px] text-slate-500 mt-1">Tip: Use variables like <code class="text-blue-600 font-mono">{{name}}</code>, <code class="text-blue-600 font-mono">{{phone}}</code> for dynamic values.</p>
                </div>

                <div class="flex items-center justify-end space-x-2.5 pt-3.5 border-t border-slate-100">
                    <button type="button" onclick="closeCreateTemplateModal()" class="px-4 py-2.5 rounded-xl bg-slate-100 hover:bg-slate-200 text-slate-700 font-bold">Cancel</button>
                    <button type="submit" class="px-5 py-2.5 rounded-xl bg-blue-600 hover:bg-blue-700 text-white font-bold">Save Template</button>
                </div>
            </form>
        </div>
    </div>

    <!-- ================= MODAL: TEMPLATES PICKER ================= -->
    <div id="template-picker-modal" class="fixed inset-0 bg-slate-900/40 backdrop-blur-sm z-50 hidden flex items-center justify-center p-4">
        <div class="bg-white border border-slate-200 rounded-3xl max-w-xl w-full p-6 shadow-2xl max-h-[85vh] flex flex-col">
            <div class="flex items-center justify-between pb-3.5 border-b border-slate-100">
                <div class="flex items-center space-x-2.5">
                    <div class="w-9 h-9 rounded-xl bg-blue-50 text-blue-600 flex items-center justify-center text-base">
                        <i class="fas fa-layer-group"></i>
                    </div>
                    <h3 class="font-extrabold text-slate-900 text-base">Select Message Template</h3>
                </div>
                <button onclick="closeTemplateSelectorModal()" class="text-slate-400 hover:text-slate-700"><i class="fas fa-times"></i></button>
            </div>

            <div id="template-picker-list" class="flex-1 overflow-y-auto space-y-2.5 mt-4 pr-1 text-xs">
                <!-- Injected via JS -->
            </div>
        </div>
    </div>

    <!-- ================= MODAL: SETTINGS & API KEYS ================= -->
    <div id="settings-modal" class="fixed inset-0 bg-slate-900/40 backdrop-blur-sm z-50 hidden flex items-center justify-center p-4">
        <div class="bg-white border border-slate-200 rounded-3xl max-w-lg w-full p-6 shadow-2xl max-h-[90vh] overflow-y-auto">
            <div class="flex items-center justify-between pb-3.5 border-b border-slate-100">
                <div class="flex items-center space-x-2.5">
                    <div class="w-9 h-9 rounded-xl bg-purple-50 text-purple-600 flex items-center justify-center text-base">
                        <i class="fas fa-gear"></i>
                    </div>
                    <div>
                        <h3 class="font-extrabold text-slate-900 text-base">Settings & API Configuration</h3>
                        <p class="text-xs text-slate-500">Configure Groq AI Key and Meta WhatsApp Tokens</p>
                    </div>
                </div>
                <button onclick="closeSettingsModal()" class="text-slate-400 hover:text-slate-700"><i class="fas fa-times"></i></button>
            </div>

            <form onsubmit="handleSettingsSubmit(event)" class="space-y-4 mt-4 text-xs">
                <!-- Groq AI -->
                <div class="p-4 bg-slate-50 rounded-2xl border border-slate-200 space-y-2.5">
                    <label class="block text-slate-800 font-bold">Groq API Key (`gsk_...`)</label>
                    <input type="password" id="set-groq-key" placeholder="Enter your Groq API Key" class="w-full bg-white text-slate-900 px-3.5 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:border-emerald-500 font-mono"/>
                    <p class="text-[11px] text-slate-500">Free key from <a href="https://console.groq.com/keys" target="_blank" class="text-emerald-600 font-bold hover:underline">console.groq.com/keys</a></p>
                </div>

                <div>
                    <label class="block text-slate-700 font-bold mb-1">AI Model Selection</label>
                    <select id="set-groq-model" class="w-full bg-slate-50 text-slate-900 px-3.5 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:border-emerald-500 font-mono text-xs">
                        <option value="llama-3.3-70b-versatile">llama-3.3-70b-versatile (Fast & Reliable)</option>
                        <option value="llama-3.1-8b-instant">llama-3.1-8b-instant (Ultra Fast)</option>
                        <option value="deepseek-r1-distill-llama-70b">deepseek-r1-distill-llama-70b (Deep Reasoning)</option>
                        <option value="mixtral-8x7b-32768">mixtral-8x7b-32768</option>
                    </select>
                </div>

                <!-- Meta WhatsApp Credentials -->
                <div class="p-4 bg-slate-50 rounded-2xl border border-slate-200 space-y-3">
                    <h4 class="text-slate-800 font-bold">Meta WhatsApp Cloud API (Optional)</h4>
                    <div>
                        <label class="block text-slate-500 mb-1">WhatsApp Access Token</label>
                        <input type="password" id="set-meta-token" placeholder="Meta System User Token" class="w-full bg-white text-slate-900 px-3.5 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:border-emerald-500 font-mono"/>
                    </div>
                    <div>
                        <label class="block text-slate-500 mb-1">WhatsApp Phone Number ID</label>
                        <input type="text" id="set-meta-phone-id" placeholder="e.g. 104829104820192" class="w-full bg-white text-slate-900 px-3.5 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:border-emerald-500 font-mono"/>
                    </div>
                    <div>
                        <label class="block text-slate-500 mb-1">Webhook Verify Secret Token</label>
                        <input type="text" id="set-meta-verify-token" value="whatsapp_groq_bot_secret_123" class="w-full bg-white text-slate-900 px-3.5 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:border-emerald-500 font-mono"/>
                    </div>
                </div>

                <div class="flex items-center justify-end space-x-2.5 pt-3.5 border-t border-slate-100">
                    <button type="button" onclick="closeSettingsModal()" class="px-4 py-2.5 rounded-xl bg-slate-100 hover:bg-slate-200 text-slate-700 font-bold">Cancel</button>
                    <button type="submit" id="save-settings-btn" class="px-5 py-2.5 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white font-bold flex items-center space-x-2 shadow-sm">
                        <i class="fas fa-save"></i>
                        <span>Save Settings</span>
                    </button>
                </div>
            </form>
        </div>
    </div>

    <!-- ================= MODAL: ADD MANUAL BOOKING ================= -->
    <div id="booking-modal" class="fixed inset-0 bg-slate-900/40 backdrop-blur-sm z-50 hidden flex items-center justify-center p-4">
        <div class="bg-white border border-slate-200 rounded-3xl max-w-md w-full p-6 shadow-2xl">
            <div class="flex items-center justify-between pb-3.5 border-b border-slate-100">
                <div class="flex items-center space-x-2.5">
                    <div class="w-9 h-9 rounded-xl bg-amber-50 text-amber-600 flex items-center justify-center text-base">
                        <i class="fas fa-calendar-plus"></i>
                    </div>
                    <h3 class="font-extrabold text-slate-900 text-base">New Customer Booking</h3>
                </div>
                <button onclick="closeBookingModal()" class="text-slate-400 hover:text-slate-700"><i class="fas fa-times"></i></button>
            </div>

            <form onsubmit="handleAddBookingSubmit(event)" class="space-y-3.5 mt-4 text-xs">
                <div>
                    <label class="block text-slate-700 font-bold mb-1">Customer Phone Number</label>
                    <input type="text" id="book-phone" required class="w-full bg-slate-50 text-slate-900 px-3.5 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:border-amber-500 font-mono"/>
                </div>
                <div>
                    <label class="block text-slate-700 font-bold mb-1">Customer Name</label>
                    <input type="text" id="book-name" required class="w-full bg-slate-50 text-slate-900 px-3.5 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:border-amber-500"/>
                </div>
                <div>
                    <label class="block text-slate-700 font-bold mb-1">Service / Task Description</label>
                    <input type="text" id="book-service" value="Printing & Binding Task" required class="w-full bg-slate-50 text-slate-900 px-3.5 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:border-amber-500"/>
                </div>
                <div>
                    <label class="block text-slate-700 font-bold mb-1">Date & Time</label>
                    <input type="text" id="book-time" value="Tomorrow at 2:00 PM" required class="w-full bg-slate-50 text-slate-900 px-3.5 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:border-amber-500"/>
                </div>
                <div>
                    <label class="block text-slate-700 font-bold mb-1">Status</label>
                    <select id="book-status" class="w-full bg-slate-50 text-slate-900 px-3.5 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:border-amber-500 font-bold">
                        <option value="confirmed">Confirmed</option>
                        <option value="pending">Pending</option>
                        <option value="completed">Completed</option>
                    </select>
                </div>

                <div class="flex items-center justify-end space-x-2.5 pt-3.5 border-t border-slate-100">
                    <button type="button" onclick="closeBookingModal()" class="px-4 py-2.5 rounded-xl bg-slate-100 hover:bg-slate-200 text-slate-700 font-bold">Cancel</button>
                    <button type="submit" class="px-5 py-2.5 rounded-xl bg-amber-600 hover:bg-amber-700 text-white font-bold">Save Booking</button>
                </div>
            </form>
        </div>
    </div>

    <!-- Application Script -->
    <script>
        let currentPhone = null;
        let currentContactName = 'Customer';
        let allConversations = [];
        let allTemplates = [];
        let activeTab = 'chats';

        document.getElementById('webhook-full-url').innerText = window.location.origin + '/api/webhook';

        function switchTab(tab) {
            activeTab = tab;
            ['chats', 'templates', 'bookings', 'webhooks'].forEach(t => {
                const view = document.getElementById('view-' + t);
                const btn = document.getElementById('tab-btn-' + t);
                if (t === tab) {
                    view.classList.remove('hidden');
                    btn.className = 'px-3.5 py-2 rounded-lg bg-white text-emerald-700 font-bold shadow-sm transition flex items-center space-x-1.5';
                } else {
                    view.classList.add('hidden');
                    btn.className = 'px-3.5 py-2 rounded-lg text-slate-600 hover:text-slate-900 hover:bg-white/60 transition flex items-center space-x-1.5';
                }
            });

            if (tab === 'templates') fetchTemplates();
            if (tab === 'bookings') fetchBookings();
            if (tab === 'webhooks') fetchWebhooks();
        }

        async function fetchData(manual = false) {
            const refreshIcon = document.getElementById('refresh-icon');
            if (manual) refreshIcon.classList.add('fa-spin');

            try {
                // Stats
                const statsRes = await fetch('/api/dashboard/stats');
                if (statsRes.ok) {
                    const stats = await statsRes.json();
                    document.getElementById('stat-total-messages').innerText = stats.total_messages || 0;
                    document.getElementById('stat-inbound').innerText = stats.inbound_messages || 0;
                    document.getElementById('stat-outbound').innerText = stats.outbound_messages || 0;
                    document.getElementById('stat-contacts').innerText = stats.total_contacts || 0;
                    document.getElementById('stat-bookings').innerText = stats.total_bookings || 0;
                    document.getElementById('nav-bookings-count').innerText = stats.total_bookings || 0;
                    document.getElementById('stat-latency').innerText = `${stats.avg_latency_ms || 0} ms`;
                    document.getElementById('stat-engine-badge').innerText = stats.groq_configured ? stats.groq_model : 'Customer Support AI';
                }

                // Conversations
                const convsRes = await fetch('/api/dashboard/conversations');
                if (convsRes.ok) {
                    const data = await convsRes.json();
                    allConversations = data.conversations || [];
                    document.getElementById('chat-count').innerText = allConversations.length;
                    renderConversations(allConversations);

                    if (!currentPhone && allConversations.length > 0) {
                        selectChat(allConversations[0].phone_number, allConversations[0].sender_name, allConversations[0].last_channel);
                    } else if (currentPhone) {
                        await loadMessages(false);
                    }
                }

                // Load Templates
                await fetchTemplates();

                if (activeTab === 'bookings') fetchBookings();
                if (activeTab === 'webhooks') fetchWebhooks();

            } catch (err) {
                console.error("Fetch error:", err);
            } finally {
                if (manual) setTimeout(() => refreshIcon.classList.remove('fa-spin'), 400);
            }
        }

        function renderConversations(list) {
            const container = document.getElementById('conversations-container');
            if (!list || list.length === 0) {
                container.innerHTML = `
                    <div class="p-8 text-center text-slate-400 text-xs">
                        <i class="far fa-comments text-2xl mb-2 text-slate-300 block"></i>
                        No conversations yet.<br/>
                        <button onclick="openSimulateModal()" class="mt-2 text-emerald-600 font-bold hover:underline">Simulate customer message</button>
                    </div>
                `;
                return;
            }

            container.innerHTML = list.map(c => {
                const isActive = c.phone_number === currentPhone;
                const activeClass = isActive ? 'bg-emerald-50/80 border-l-4 border-emerald-600' : 'hover:bg-slate-100/70';
                const isOutbound = c.last_direction === 'outbound';
                const dirIcon = isOutbound ? '<i class="fas fa-check-double text-emerald-600 mr-1 text-[10px]"></i>' : '';
                const avatarBg = getAvatarColor(c.phone_number);

                return `
                    <div onclick="selectChat('${c.phone_number}', '${escapeHtml(c.sender_name)}', '${c.last_channel}')" 
                         class="p-3.5 cursor-pointer transition flex items-center space-x-3 ${activeClass}">
                        <div class="w-10 h-10 rounded-full ${avatarBg} flex items-center justify-center text-white font-bold text-xs flex-shrink-0 shadow-sm">
                            ${getInitials(c.sender_name || c.phone_number)}
                        </div>
                        <div class="flex-1 min-w-0">
                            <div class="flex items-center justify-between">
                                <h4 class="text-xs font-bold text-slate-900 truncate">${escapeHtml(c.sender_name || c.phone_number)}</h4>
                                <span class="text-[10px] text-slate-400 font-mono">${formatTime(c.last_time)}</span>
                            </div>
                            <div class="flex items-center justify-between mt-0.5">
                                <p class="text-xs text-slate-500 truncate max-w-[180px]">
                                    ${dirIcon}${escapeHtml(c.last_message || '')}
                                </p>
                                <span class="text-[10px] px-1.5 py-0.2 rounded-full bg-slate-200/80 text-slate-600 font-mono font-bold">${c.total_messages}</span>
                            </div>
                        </div>
                    </div>
                `;
            }).join('');
        }

        async function selectChat(phone, name, channel) {
            currentPhone = phone;
            currentContactName = name || phone;
            document.getElementById('active-name').innerText = currentContactName;
            document.getElementById('active-phone').innerText = phone;
            document.getElementById('active-avatar').innerHTML = getInitials(name || phone);
            document.getElementById('active-avatar').className = `w-10 h-10 rounded-full ${getAvatarColor(phone)} flex items-center justify-center text-white font-bold shadow-sm text-sm`;
            
            const badge = document.getElementById('active-channel-badge');
            badge.classList.remove('hidden');
            badge.innerText = (channel || 'WHATSAPP').toUpperCase().replace('_', ' ');

            document.getElementById('delete-chat-btn').classList.remove('hidden');
            document.getElementById('book-lead-btn').classList.remove('hidden');
            document.getElementById('bot-toggle-container').classList.remove('hidden');

            renderConversations(allConversations);
            await loadMessages(true);
        }

        async function loadMessages(shouldScroll = true) {
            if (!currentPhone) return;
            try {
                const res = await fetch(`/api/dashboard/messages?phone=${encodeURIComponent(currentPhone)}`);
                if (res.ok) {
                    const data = await res.json();
                    document.getElementById('contact-bot-checkbox').checked = data.bot_enabled !== false;
                    renderMessages(data.messages || [], shouldScroll);
                }
            } catch (e) {
                console.error("Message load error:", e);
            }
        }

        function renderMessages(messages, shouldScroll = true) {
            const container = document.getElementById('messages-container');
            if (messages.length === 0) {
                container.innerHTML = `<div class="h-full flex items-center justify-center text-slate-500 text-xs">No messages in this chat yet.</div>`;
                return;
            }

            container.innerHTML = messages.map(m => {
                const isOutbound = m.direction === 'outbound';
                const alignment = isOutbound ? 'justify-end' : 'justify-start';
                // WhatsApp Light theme bubble colors
                const bubbleBg = isOutbound ? 'bg-[#d9fdd3] text-slate-900 border border-emerald-200/50 rounded-tr-sm' : 'bg-white text-slate-900 border border-slate-200/80 rounded-tl-sm';
                const senderLabel = isOutbound ? 'Support Agent' : escapeHtml(m.sender_name || 'Customer');
                const engineTag = m.ai_model ? `<span class="text-[9px] px-1.5 py-0.2 bg-emerald-100 text-emerald-800 rounded font-bold mr-1"><i class="fas fa-robot mr-1"></i>${escapeHtml(m.ai_model)}</span>` : '';

                return `
                    <div class="flex ${alignment}">
                        <div class="${bubbleBg} text-xs px-3.5 py-2.5 rounded-2xl max-w-[85%] md:max-w-[75%] shadow-sm break-words space-y-1">
                            <div class="text-[11px] font-extrabold ${isOutbound ? 'text-emerald-700' : 'text-slate-700'}">${senderLabel}</div>
                            <div class="whitespace-pre-wrap leading-relaxed text-[13px]">${escapeHtml(m.message)}</div>
                            <div class="flex items-center justify-end space-x-1.5 pt-0.5 text-[10px] text-slate-500">
                                ${engineTag}
                                <span>${formatTime(m.created_at)}</span>
                                ${isOutbound ? '<i class="fas fa-check-double text-emerald-600 text-[10px]"></i>' : ''}
                            </div>
                        </div>
                    </div>
                `;
            }).join('');

            if (shouldScroll) container.scrollTop = container.scrollHeight;
        }

        async function toggleContactBot(enabled) {
            if (!currentPhone) return;
            await fetch('/api/dashboard/contact/bot-toggle', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ phone: currentPhone, enabled: enabled })
            });
        }

        async function sendReply(e) {
            e.preventDefault();
            if (!currentPhone) {
                alert("Please select a conversation first.");
                return;
            }

            const input = document.getElementById('reply-input');
            const msg = input.value.trim();
            if (!msg) return;

            const mode = document.querySelector('input[name="reply_mode"]:checked').value;
            const sendViaMeta = document.getElementById('send-meta-cloud').checked;
            const btn = document.getElementById('send-reply-btn');

            input.disabled = true;
            btn.disabled = true;

            try {
                const res = await fetch('/api/dashboard/send', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        phone: currentPhone,
                        message: msg,
                        type: mode,
                        send_via_meta: sendViaMeta
                    })
                });

                if (res.ok) {
                    input.value = '';
                    await fetchData(true);
                } else {
                    const err = await res.json();
                    alert("Error: " + (err.error || 'Failed to send message'));
                }
            } catch (err) {
                alert("Network error: " + err.message);
            } finally {
                input.disabled = false;
                btn.disabled = false;
                input.focus();
            }
        }

        // Templates System
        async function fetchTemplates() {
            try {
                const res = await fetch('/api/dashboard/templates');
                if (res.ok) {
                    const data = await res.json();
                    allTemplates = data.templates || [];
                    renderQuickTemplates();
                    renderTemplatesGrid();
                    renderTemplatePicker();
                }
            } catch (e) {
                console.error("Templates fetch error:", e);
            }
        }

        function renderQuickTemplates() {
            const bar = document.getElementById('quick-templates-bar');
            bar.innerHTML = allTemplates.slice(0, 4).map(t => `
                <button onclick="applyTemplate('${escapeHtml(t.content)}')" class="px-2.5 py-1 bg-slate-100 hover:bg-slate-200 text-slate-800 rounded-lg text-xs font-medium border border-slate-200 flex-shrink-0 transition truncate max-w-[160px]">
                    ${escapeHtml(t.title)}
                </button>
            `).join('');
        }

        function renderTemplatesGrid() {
            const grid = document.getElementById('templates-grid');
            if (!allTemplates.length) {
                grid.innerHTML = `<div class="p-8 text-center text-slate-400 col-span-3">No templates created yet.</div>`;
                return;
            }
            grid.innerHTML = allTemplates.map(t => `
                <div class="bg-slate-50 border border-slate-200 rounded-2xl p-4 flex flex-col justify-between space-y-3 hover:shadow-sm transition">
                    <div>
                        <div class="flex items-center justify-between">
                            <h4 class="font-bold text-slate-900 text-sm">${escapeHtml(t.title)}</h4>
                            <span class="px-2 py-0.5 rounded-full bg-blue-50 text-blue-700 text-[10px] font-bold">${escapeHtml(t.category)}</span>
                        </div>
                        <p class="text-xs text-slate-600 whitespace-pre-wrap mt-2.5 leading-relaxed bg-white p-3 rounded-xl border border-slate-200">${escapeHtml(t.content)}</p>
                    </div>
                    <div class="flex items-center justify-between pt-2 border-t border-slate-200/80">
                        <button onclick="applyTemplate('${escapeHtml(t.content)}'); switchTab('chats');" class="px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white text-xs font-bold transition">
                            <i class="fas fa-paper-plane mr-1"></i> Use Template
                        </button>
                        <button onclick="deleteTemplate(${t.id})" class="text-slate-400 hover:text-red-500 p-1 transition"><i class="fas fa-trash"></i></button>
                    </div>
                </div>
            `).join('');
        }

        function renderTemplatePicker() {
            const list = document.getElementById('template-picker-list');
            list.innerHTML = allTemplates.map(t => `
                <div onclick="applyTemplate('${escapeHtml(t.content)}'); closeTemplateSelectorModal();" class="p-3.5 bg-slate-50 hover:bg-emerald-50 rounded-2xl border border-slate-200 cursor-pointer transition">
                    <div class="flex items-center justify-between">
                        <h4 class="font-bold text-slate-900">${escapeHtml(t.title)}</h4>
                        <span class="text-[10px] font-bold text-slate-500">${escapeHtml(t.category)}</span>
                    </div>
                    <p class="text-xs text-slate-600 mt-1.5 truncate">${escapeHtml(t.content)}</p>
                </div>
            `).join('');
        }

        function applyTemplate(rawContent) {
            let replaced = rawContent
                .replace(/\{\{name\}\}/g, currentContactName || 'Customer')
                .replace(/\{\{phone\}\}/g, currentPhone || '')
                .replace(/\{\{date_time\}\}/g, 'Tomorrow at 2:00 PM')
                .replace(/\{\{service\}\}/g, 'Printing & Task Service');
            
            document.getElementById('reply-input').value = replaced;
            document.getElementById('reply-input').focus();
        }

        function openCreateTemplateModal() { document.getElementById('template-create-modal').classList.remove('hidden'); }
        function closeCreateTemplateModal() { document.getElementById('template-create-modal').classList.add('hidden'); }

        async function handleCreateTemplateSubmit(e) {
            e.preventDefault();
            const title = document.getElementById('tpl-title').value.trim();
            const category = document.getElementById('tpl-category').value;
            const content = document.getElementById('tpl-content').value.trim();

            await fetch('/api/dashboard/templates', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title, category, content })
            });

            closeCreateTemplateModal();
            fetchTemplates();
        }

        async function deleteTemplate(id) {
            if (!confirm("Delete this template?")) return;
            await fetch(`/api/dashboard/templates?id=${id}`, { method: 'DELETE' });
            fetchTemplates();
        }

        function openTemplateSelectorModal() { document.getElementById('template-picker-modal').classList.remove('hidden'); }
        function closeTemplateSelectorModal() { document.getElementById('template-picker-modal').classList.add('hidden'); }

        // Bookings
        async function fetchBookings() {
            try {
                const res = await fetch('/api/dashboard/bookings');
                if (res.ok) {
                    const data = await res.json();
                    const list = data.bookings || [];
                    const tbody = document.getElementById('bookings-tbody');
                    if (list.length === 0) {
                        tbody.innerHTML = `<tr><td colspan="7" class="p-8 text-center text-slate-400">No bookings recorded yet. Inbound customer booking messages appear here automatically!</td></tr>`;
                        return;
                    }

                    tbody.innerHTML = list.map(b => {
                        const statusColors = {
                            confirmed: 'bg-emerald-50 text-emerald-700 border-emerald-200',
                            pending: 'bg-amber-50 text-amber-700 border-amber-200',
                            completed: 'bg-blue-50 text-blue-700 border-blue-200',
                            cancelled: 'bg-red-50 text-red-700 border-red-200'
                        };
                        const badgeClass = statusColors[b.status] || statusColors.pending;

                        return `
                            <tr class="hover:bg-slate-50 transition">
                                <td class="p-3.5 font-bold text-slate-900">${escapeHtml(b.customer_name)}</td>
                                <td class="p-3.5 font-mono font-medium text-emerald-700">${b.phone_number}</td>
                                <td class="p-3.5 text-slate-800 font-medium">${escapeHtml(b.service_requested)}</td>
                                <td class="p-3.5 text-slate-600">${escapeHtml(b.booking_date_time)}</td>
                                <td class="p-3.5">
                                    <select onchange="updateBookingStatus(${b.id}, this.value)" class="px-2.5 py-1 rounded-lg text-xs font-bold border ${badgeClass} bg-white focus:outline-none">
                                        <option value="confirmed" ${b.status === 'confirmed' ? 'selected' : ''}>Confirmed</option>
                                        <option value="pending" ${b.status === 'pending' ? 'selected' : ''}>Pending</option>
                                        <option value="completed" ${b.status === 'completed' ? 'selected' : ''}>Completed</option>
                                        <option value="cancelled" ${b.status === 'cancelled' ? 'selected' : ''}>Cancelled</option>
                                    </select>
                                </td>
                                <td class="p-3.5 text-slate-500 truncate max-w-xs">${escapeHtml(b.notes || '')}</td>
                                <td class="p-3.5 text-right">
                                    <button onclick="selectChat('${b.phone_number}', '${escapeHtml(b.customer_name)}'); switchTab('chats');" class="px-3 py-1.5 rounded-lg bg-emerald-50 text-emerald-700 font-bold hover:bg-emerald-600 hover:text-white transition">
                                        <i class="fas fa-comment mr-1"></i> Chat
                                    </button>
                                </td>
                            </tr>
                        `;
                    }).join('');
                }
            } catch (e) {
                console.error("Bookings error:", e);
            }
        }

        async function updateBookingStatus(id, status) {
            await fetch('/api/dashboard/bookings/update', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id, status })
            });
            fetchData();
        }

        function openAddBookingModal() {
            document.getElementById('book-phone').value = currentPhone || '+14155550199';
            document.getElementById('book-name').value = currentContactName || 'Customer';
            document.getElementById('booking-modal').classList.remove('hidden');
        }
        function openBookCurrentContactModal() { openAddBookingModal(); }
        function closeBookingModal() { document.getElementById('booking-modal').classList.add('hidden'); }

        async function handleAddBookingSubmit(e) {
            e.preventDefault();
            const phone = document.getElementById('book-phone').value.trim();
            const name = document.getElementById('book-name').value.trim();
            const service = document.getElementById('book-service').value.trim();
            const dateTime = document.getElementById('book-time').value.trim();
            const status = document.getElementById('book-status').value;

            await fetch('/api/dashboard/bookings/add', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ phone, name, service, date_time: dateTime, status })
            });

            closeBookingModal();
            fetchData(true);
        }

        // Webhooks
        async function fetchWebhooks() {
            try {
                const res = await fetch('/api/dashboard/webhooks');
                if (res.ok) {
                    const data = await res.json();
                    const container = document.getElementById('webhook-logs-container');
                    const logs = data.logs || [];
                    if (logs.length === 0) {
                        container.innerHTML = `<div class="p-8 text-center text-slate-400">No webhook logs yet. Send a message to /api/webhook to test.</div>`;
                        return;
                    }
                    container.innerHTML = logs.map(l => `
                        <div class="bg-slate-50 border border-slate-200 rounded-2xl p-4 space-y-1.5">
                            <div class="flex items-center justify-between font-mono text-xs">
                                <div class="flex items-center space-x-2">
                                    <span class="px-2 py-0.5 rounded-lg ${l.method === 'POST' ? 'bg-blue-100 text-blue-800' : 'bg-emerald-100 text-emerald-800'} font-bold">${l.method}</span>
                                    <span class="text-slate-800 font-bold">${l.endpoint}</span>
                                </div>
                                <span class="text-slate-400 text-[11px]">${l.created_at}</span>
                            </div>
                            <pre class="bg-white p-3 rounded-xl border border-slate-200 text-xs text-slate-700 overflow-x-auto font-mono max-h-36">${escapeHtml(l.payload || '')}</pre>
                        </div>
                    `).join('');
                }
            } catch (e) {
                console.error("Webhook logs error:", e);
            }
        }

        // Simulate Modal
        function openSimulateModal() { document.getElementById('simulate-modal').classList.remove('hidden'); }
        function closeSimulateModal() { document.getElementById('simulate-modal').classList.add('hidden'); }

        async function handleSimulateSubmit(e) {
            e.preventDefault();
            const phone = document.getElementById('sim-phone').value.trim();
            const name = document.getElementById('sim-name').value.trim();
            const msg = document.getElementById('sim-message').value.trim();
            const autoReply = document.getElementById('sim-auto-reply').checked;
            const btn = document.getElementById('sim-submit-btn');

            btn.disabled = true;
            btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> Sending...`;

            try {
                const res = await fetch('/api/dashboard/simulate-inbound', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ phone, name, message: msg, auto_reply: autoReply })
                });

                if (res.ok) {
                    closeSimulateModal();
                    currentPhone = phone;
                    currentContactName = name;
                    await fetchData(true);
                }
            } catch (err) {
                alert("Simulation error: " + err.message);
            } finally {
                btn.disabled = false;
                btn.innerHTML = `<i class="fas fa-paper-plane"></i> <span>Send Message</span>`;
            }
        }

        // Settings Modal
        async function openSettingsModal() {
            document.getElementById('settings-modal').classList.remove('hidden');
            const res = await fetch('/api/dashboard/settings');
            if (res.ok) {
                const s = await res.json();
                if (s.groq_api_key_set) document.getElementById('set-groq-key').placeholder = `Configured: ${s.groq_api_key_masked}`;
                document.getElementById('set-groq-model').value = s.groq_model || 'llama-3.3-70b-versatile';
                if (s.whatsapp_token_set) document.getElementById('set-meta-token').placeholder = `Configured: ${s.whatsapp_token_masked}`;
                document.getElementById('set-meta-phone-id').value = s.whatsapp_phone_number_id || '';
                document.getElementById('set-meta-verify-token').value = s.whatsapp_verify_token || 'whatsapp_groq_bot_secret_123';
                document.getElementById('webhook-token-label').innerText = s.whatsapp_verify_token || 'whatsapp_groq_bot_secret_123';
            }
        }

        function closeSettingsModal() { document.getElementById('settings-modal').classList.add('hidden'); }

        async function handleSettingsSubmit(e) {
            e.preventDefault();
            const groqKey = document.getElementById('set-groq-key').value.trim();
            const groqModel = document.getElementById('set-groq-model').value;
            const metaToken = document.getElementById('set-meta-token').value.trim();
            const phoneId = document.getElementById('set-meta-phone-id').value.trim();
            const verifyToken = document.getElementById('set-meta-verify-token').value.trim();
            const btn = document.getElementById('save-settings-btn');

            btn.disabled = true;
            btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> Saving...`;

            try {
                const res = await fetch('/api/dashboard/settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        groq_api_key: groqKey,
                        groq_model: groqModel,
                        whatsapp_token: metaToken,
                        whatsapp_phone_number_id: phoneId,
                        whatsapp_verify_token: verifyToken
                    })
                });
                if (res.ok) {
                    closeSettingsModal();
                    alert("Settings saved successfully!");
                    fetchData(true);
                }
            } catch (err) {
                alert("Error saving settings: " + err.message);
            } finally {
                btn.disabled = false;
                btn.innerHTML = `<i class="fas fa-save"></i> <span>Save Settings</span>`;
            }
        }

        async function deleteCurrentChat() {
            if (!currentPhone) return;
            if (!confirm(`Delete chat history for ${currentPhone}?`)) return;
            await fetch(`/api/dashboard/conversation?phone=${encodeURIComponent(currentPhone)}`, { method: 'DELETE' });
            currentPhone = null;
            fetchData(true);
        }

        async function clearAllData() {
            if (!confirm("Clear ALL chat history and logs?")) return;
            await fetch('/api/dashboard/clear', { method: 'DELETE' });
            currentPhone = null;
            fetchData(true);
        }

        function filterConversations() {
            const q = document.getElementById('search-input').value.toLowerCase();
            const filtered = allConversations.filter(c => 
                (c.phone_number && c.phone_number.toLowerCase().includes(q)) ||
                (c.sender_name && c.sender_name.toLowerCase().includes(q)) ||
                (c.last_message && c.last_message.toLowerCase().includes(q))
            );
            renderConversations(filtered);
        }

        function formatTime(isoStr) {
            if (!isoStr) return '';
            try {
                const d = new Date(isoStr.replace(' ', 'T') + 'Z');
                return isNaN(d.getTime()) ? isoStr : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            } catch { return isoStr; }
        }

        function getInitials(name) {
            if (!name) return 'WA';
            const parts = name.trim().split(/[\s+]+/);
            if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
            return name.slice(0, 2).toUpperCase();
        }

        const colors = ['bg-emerald-600', 'bg-blue-600', 'bg-indigo-600', 'bg-purple-600', 'bg-teal-600', 'bg-amber-600', 'bg-rose-600'];
        function getAvatarColor(str) {
            if (!str) return colors[0];
            let hash = 0;
            for (let i = 0; i < str.length; i++) hash = str.charCodeAt(i) + ((hash << 5) - hash);
            return colors[Math.abs(hash) % colors.length];
        }

        function escapeHtml(str) {
            if (!str) return '';
            return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#039;');
        }

        // Initialize & Auto Poll every 3 seconds
        fetchData();
        setInterval(() => fetchData(false), 3000);
    </script>
</body>
</html>
"""
