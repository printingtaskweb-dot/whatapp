# 🚀 WhatsApp Groq AI Bot

A lightweight, serverless WhatsApp AI bot powered by **Groq** using **`openai/gpt-oss-120b`**, configured for instant deployment to **Vercel** and direct integration with **Meta WhatsApp Cloud API** (and Twilio Sandbox).

---

## ⚡ Webhook Details for Meta WhatsApp Setup

When Meta asks for your Webhook credentials in the Meta Developer Console:

* **Callback URL:** `https://<your-vercel-domain>.vercel.app/api/webhook`
* **Verify Token:** `whatsapp_groq_bot_secret_123`
* **Webhook Fields to Subscribe:** `messages`

*(You can customize the Verify Token at any time by updating the `WHATSAPP_VERIFY_TOKEN` environment variable).*

---

## 🔑 Environment Variables Required

In your **Vercel Project Settings > Environment Variables** (or local `.env`):

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
│   └── index.py            # FastAPI serverless app (WhatsApp Webhooks & Groq AI)
├── .env.example            # Template for environment variables
├── .gitignore              # Protects secrets from being committed
├── requirements.txt        # Python dependencies (fastapi, groq, httpx)
├── vercel.json             # Vercel serverless routing
├── test_bot.py             # Local CLI test script
└── README.md               # Documentation
```

---

## 🚀 How to Deploy on Vercel

1. Go to [Vercel](https://vercel.com) and click **"Add New Project"**.
2. Select your repository: **`printingtaskweb-dot/whatapp`**.
3. Under **Environment Variables**, add:
   * `GROQ_API_KEY`
   * `GROQ_MODEL` (`openai/gpt-oss-120b`)
   * `WHATSAPP_TOKEN`
   * `WHATSAPP_PHONE_NUMBER_ID`
   * `WHATSAPP_VERIFY_TOKEN` (`whatsapp_groq_bot_secret_123`)
4. Click **Deploy**.
5. Once deployed, Vercel gives you your live URL (e.g., `https://whatapp-xxxx.vercel.app`).
6. Copy that URL and enter it in Meta:
   * **Callback URL:** `https://whatapp-xxxx.vercel.app/api/webhook`
   * **Verify Token:** `whatsapp_groq_bot_secret_123`
7. Click **Verify and Save**, then under **Webhook fields**, click **Manage** and subscribe to **`messages`**.

---

## 📱 Testing Without WhatsApp First (Browser Simulator)

Once deployed to Vercel, navigate directly to your root URL:
`https://<your-vercel-domain>.vercel.app`

This opens an interactive WhatsApp web simulator where you can test chatting with Groq's `openai/gpt-oss-120b` model right in your browser!