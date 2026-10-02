import uvicorn
import webbrowser
import threading
import time

def open_browser():
    time.sleep(1.5)
    print("\n🌐 Opening WhatsApp Dashboard in your browser at http://localhost:8000 ...")
    webbrowser.open("http://localhost:8000")

if __name__ == "__main__":
    print("=" * 60)
    print("🚀 Starting WhatsApp Groq AI Bot & Messages Dashboard")
    print("=" * 60)
    print("📍 Dashboard URL:       http://localhost:8000")
    print("📍 Meta Webhook URL:    http://localhost:8000/api/webhook")
    print("📍 Twilio Webhook URL:  http://localhost:8000/api/twilio")
    print("=" * 60)
    
    # Auto open browser in background thread
    threading.Thread(target=open_browser, daemon=True).start()
    
    uvicorn.run("api.index:app", host="0.0.0.0", port=8000, reload=True)
