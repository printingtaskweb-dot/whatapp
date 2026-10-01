"""
test_bot.py - Quick local test script for WhatsApp Groq Bot.
"""
import os
import json
from dotenv import load_dotenv

load_dotenv()

def test_groq():
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        print("[!] GROQ_API_KEY is not set in .env. Please add it from https://console.groq.com/keys")
        return

    try:
        from groq import Groq
        client = Groq(api_key=api_key)
        model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
        print(f"[*] Testing Groq with model: {model}...")
        
        completion = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": "Hello! Reply with a short WhatsApp greeting."}
            ],
            temperature=1,
            max_completion_tokens=256,
            reasoning_effort="medium"
        )
        print("\n[+] Groq AI Response:")
        print(completion.choices[0].message.content)
    except Exception as e:
        print(f"[!] Groq Test Error: {e}")

if __name__ == "__main__":
    test_groq()
