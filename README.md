# 🚀 WhatsApp Groq AI Bot & Live Messages Dashboard

A serverless & local WhatsApp AI bot and live messages dashboard powered by **Groq** using **`openai/gpt-oss-120b`**, integrated with **Meta WhatsApp Cloud API** (and Twilio Sandbox).

---

## ✨ Features

- 📊 **Real-time WhatsApp Dashboard**: WhatsApp Web-style CRM to inspect all incoming customer messages and outgoing AI/admin replies.
- ⚡ **Groq Ultra-Fast AI**: Powered by `openai/gpt-oss-120b` (with auto-fallback to `llama-3.3-70b-versatile`).
- 🤖 **Multi-Channel Message Tracking**: Captures Meta WhatsApp Cloud API, Twilio, and Simulator messages with full latency and model tracking.
- 💬 **Live Auto-Refresh & Inbox**: Multi-contact threads, unread counts, search filters, and message status ticks.
- ✍️ **Admin Reply / AI Trigger**: Send manual replies or auto-generate AI answers directly from the dashboard.
- 🔬 **Incoming Message Simulator**: Test incoming customer WhatsApp messages without setting up Meta developer credentials first.
- 🔍 **Webhook Inspector**: Real-time event log for verification requests and Meta Graph API JSON payloads.

---

## 🚀 Running Locally

1. **Install dependencies:**
   ```bash
   py -m pip install -r requirements.txt
   ```

2. **Configure your environment (optional for Groq AI & Meta API):**
   Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
   Add your `GROQ_API_KEY`, `WHATSAPP_TOKEN`, and `WHATSAPP_PHONE_NUMBER_ID`.

3. **Start the Dashboard:**
   ```bash
   py run_dashboard.py
   ```
   Or with uvicorn:
   ```bash
   py -m uvicorn api.index:app --reload --port 8000
   ```
   Open **`http://localhost:8000`** (or `http://localhost:8000/dashboard`) in your browser.

---

## ⚡ Webhook Details for Meta WhatsApp Setup

When Meta asks for your Webhook credentials in the Meta Developer Console:

* **Callback URL:** `https://<your-domain>/api/webhook`
* **Verify Token:** `whatsapp_groq_bot_secret_123`
* **Webhook Fields to Subscribe:** `messages`

*(You can customize the Verify Token at any time by updating the `WHATSAPP_VERIFY_TOKEN` environment variable).*

---

## 🔑 Environment Variables

| Variable | Description | Where to get it |
| :--- | :--- | :--- |
| `GROQ_API_KEY` | Your Groq API Key (`gsk_...`) | [Groq Cloud Console](https://console.groq.com/keys) |
| `GROQ_MODEL` | AI Model Name | `openai/gpt-oss-120b` |
| `WHATSAPP_TOKEN` | Meta System User / Temporary Access Token | Meta Developer Portal > WhatsApp > API Setup |
| `WHATSAPP_PHONE_NUMBER_ID` | WhatsApp Phone Number ID | Meta Developer Portal > WhatsApp > API Setup |
| `WHATSAPP_VERIFY_TOKEN` | Webhook verification secret | Default: `whatsapp_groq_bot_secret_123` |

---

## 🛠️ Project Structure

```
.
├── api/
│   ├── index.py            # FastAPI app & WhatsApp Dashboard + Webhooks
│   ├── webhook.py          # Dedicated Meta webhook endpoint
│   └── db.py               # SQLite message storage, stats, & webhook logger
├── run_dashboard.py        # Local one-click dashboard runner
├── .env.example            # Template for environment variables
├── .gitignore              # Protects secrets from being committed
├── requirements.txt        # Python dependencies (fastapi, groq, httpx, uvicorn)
├── test_bot.py             # Local CLI test script
└── README.md               # Documentation
```