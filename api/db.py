import os
import sqlite3
import json
import time
from datetime import datetime

# Database path
DB_DIR = os.path.dirname(os.path.abspath(__file__))
if os.environ.get("VERCEL"):
    DB_PATH = "/tmp/whatsapp_messages.db"
else:
    DB_PATH = os.path.join(os.path.dirname(DB_DIR), "whatsapp_messages.db")

def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initialize database tables for messages, bookings, templates, and contact settings."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # Messages Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT NOT NULL,
                sender_name TEXT DEFAULT 'User',
                message TEXT NOT NULL,
                direction TEXT NOT NULL, -- 'inbound' or 'outbound'
                channel TEXT NOT NULL DEFAULT 'meta_whatsapp', -- 'meta_whatsapp', 'twilio', 'simulator', 'manual_admin'
                status TEXT DEFAULT 'received', -- 'received', 'sent', 'failed'
                raw_payload TEXT,
                ai_model TEXT,
                latency_ms REAL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Bookings & Support Requests Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT NOT NULL,
                customer_name TEXT DEFAULT 'Customer',
                service_requested TEXT DEFAULT 'Customer Support / Appointment',
                booking_date_time TEXT DEFAULT 'Pending Schedule',
                status TEXT DEFAULT 'pending', -- 'confirmed', 'pending', 'cancelled', 'completed'
                notes TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Message Templates Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                category TEXT DEFAULT 'General',
                content TEXT NOT NULL,
                shortcut TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Contact Settings (Bot enabled / disabled per user)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS contact_settings (
                phone_number TEXT PRIMARY KEY,
                bot_enabled INTEGER DEFAULT 1,
                assigned_agent TEXT DEFAULT 'Support Team',
                tags TEXT DEFAULT 'Customer',
                notes TEXT DEFAULT ''
            )
        """)
        
        # Webhook Logs Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS webhook_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT NOT NULL,
                method TEXT NOT NULL,
                payload TEXT,
                status_code INTEGER DEFAULT 200,
                ip_address TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Seed default support templates if empty
        cursor.execute("SELECT COUNT(*) as count FROM templates")
        if cursor.fetchone()["count"] == 0:
            default_templates = [
                ("👋 Welcome Greeting", "Greeting", "Hello {{name}}! 👋 Thank you for reaching out to Customer Support. How can we assist you today?", "/welcome"),
                ("📅 Booking Confirmation", "Bookings", "Hi {{name}}, your appointment for {{service}} on {{date_time}} is confirmed! Please let us know if you need to reschedule.", "/confirm"),
                ("🖨️ Printing Quote Details", "Quotes", "Hi {{name}}, here is our estimate for your printing task:\n• Standard B/W: $0.05/page\n• Full Color: $0.25/page\n• Custom Finishing: Available upon request\nWould you like us to proceed with printing?", "/quote"),
                ("📦 Order Ready for Pickup", "Orders", "Great news {{name}}! 🎉 Your order is ready for pickup at our store or dispatched for delivery. Please show your Order ID at the counter.", "/ready"),
                ("💳 Payment Request", "Billing", "Hi {{name}}, you can complete your payment securely using this link: https://pay.example.com/invoice/{{phone}} . Let us know once completed!", "/pay"),
                ("⏳ Under Review by Specialist", "Support", "Hi {{name}}, your request has been escalated to our senior support specialist. We are reviewing your files and will update you shortly.", "/review"),
                ("⭐ Feedback & CSAT Survey", "Feedback", "Hi {{name}}, how was your support experience with us today? Please rate us from 1 (Poor) to 5 (Excellent) ⭐. Thank you!", "/feedback"),
                ("👤 Human Handover", "Handover", "A human customer support specialist has joined this chat. One moment please while I connect you with our agent.", "/agent")
            ]
            cursor.executemany("INSERT INTO templates (title, category, content, shortcut) VALUES (?, ?, ?, ?)", default_templates)
        
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_phone ON messages(phone_number)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON messages(created_at)")
        
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB] Error initializing database: {e}")

def save_message(phone_number: str, message: str, direction: str = "inbound", 
                 sender_name: str = "User", channel: str = "meta_whatsapp", 
                 status: str = "received", raw_payload: dict = None, 
                 ai_model: str = None, latency_ms: float = 0.0):
    """Save an incoming or outgoing message to the database and track booking intent."""
    try:
        init_db()
        conn = get_connection()
        cursor = conn.cursor()
        
        payload_str = json.dumps(raw_payload) if isinstance(raw_payload, (dict, list)) else (raw_payload or "")
        
        cursor.execute("""
            INSERT INTO messages (phone_number, sender_name, message, direction, channel, status, raw_payload, ai_model, latency_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (phone_number, sender_name, message, direction, channel, status, payload_str, ai_model, latency_ms))
        
        msg_id = cursor.lastrowid

        # Automatic Support / Booking Intent Detection for Inbound Messages
        if direction == "inbound":
            detect_and_record_booking(cursor, phone_number, sender_name, message)

        conn.commit()
        conn.close()
        return msg_id
    except Exception as e:
        print(f"[DB] Error saving message: {e}")
        return None

def detect_and_record_booking(cursor, phone_number: str, customer_name: str, message: str):
    """Detect if incoming message has booking or order intent."""
    msg_lower = message.lower()
    booking_keywords = ["book", "booking", "appointment", "schedule", "reserve", "slot", "meeting", "consultation", "order", "print"]
    
    if any(k in msg_lower for k in booking_keywords):
        service = "Customer Support & Task Order"
        if "print" in msg_lower: service = "Printing Service Order"
        elif "consult" in msg_lower: service = "Customer Consultation"
        elif "order" in msg_lower: service = "Custom Order Request"
        elif "appointment" in msg_lower or "book" in msg_lower: service = "Scheduled Appointment"

        cursor.execute("""
            SELECT id FROM bookings 
            WHERE phone_number = ? AND created_at >= datetime('now', '-2 hours')
            LIMIT 1
        """, (phone_number,))
        existing = cursor.fetchone()
        
        if not existing:
            cursor.execute("""
                INSERT INTO bookings (phone_number, customer_name, service_requested, booking_date_time, status, notes)
                VALUES (?, ?, ?, ?, 'pending', ?)
            """, (phone_number, customer_name or "WhatsApp Customer", service, "Requested via WhatsApp", message))

def is_bot_enabled_for_contact(phone_number: str) -> bool:
    """Check if automated bot is enabled for this customer phone number."""
    try:
        init_db()
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT bot_enabled FROM contact_settings WHERE phone_number = ?", (phone_number,))
        row = cursor.fetchone()
        conn.close()
        if row is not None:
            return bool(row["bot_enabled"])
        return True # Default enabled
    except Exception:
        return True

def set_bot_enabled_for_contact(phone_number: str, enabled: bool):
    """Toggle automated bot for this customer."""
    try:
        init_db()
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO contact_settings (phone_number, bot_enabled)
            VALUES (?, ?)
            ON CONFLICT(phone_number) DO UPDATE SET bot_enabled = excluded.bot_enabled
        """, (phone_number, 1 if enabled else 0))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[DB] Error setting bot enabled: {e}")
        return False

def get_conversations():
    """Get list of distinct contact conversations with their latest message and timestamp."""
    try:
        init_db()
        conn = get_connection()
        cursor = conn.cursor()
        
        query = """
            SELECT 
                m1.phone_number,
                COALESCE(
                    (SELECT m_in.sender_name FROM messages m_in WHERE m_in.phone_number = m1.phone_number AND m_in.direction = 'inbound' ORDER BY m_in.id DESC LIMIT 1),
                    m1.sender_name,
                    'Customer'
                ) as sender_name,
                m1.message as last_message,
                m1.direction as last_direction,
                m1.channel as last_channel,
                m1.created_at as last_time,
                (SELECT COUNT(*) FROM messages m2 WHERE m2.phone_number = m1.phone_number) as total_messages,
                (SELECT COUNT(*) FROM messages m3 WHERE m3.phone_number = m1.phone_number AND m3.direction = 'inbound') as inbound_count,
                COALESCE((SELECT cs.bot_enabled FROM contact_settings cs WHERE cs.phone_number = m1.phone_number), 1) as bot_enabled
            FROM messages m1
            WHERE m1.id IN (
                SELECT MAX(id) FROM messages GROUP BY phone_number
            )
            ORDER BY m1.created_at DESC
        """
        cursor.execute(query)
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        print(f"[DB] Error getting conversations: {e}")
        return []

def get_messages(phone_number: str = None, limit: int = 200):
    try:
        init_db()
        conn = get_connection()
        cursor = conn.cursor()
        
        if phone_number:
            cursor.execute("""
                SELECT * FROM messages 
                WHERE phone_number = ? 
                ORDER BY created_at ASC, id ASC 
                LIMIT ?
            """, (phone_number, limit))
        else:
            cursor.execute("""
                SELECT * FROM messages 
                ORDER BY created_at DESC, id DESC 
                LIMIT ?
            """, (limit,))
            
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        print(f"[DB] Error getting messages: {e}")
        return []

def get_templates():
    """Get all response templates."""
    try:
        init_db()
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM templates ORDER BY id ASC")
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        print(f"[DB] Error getting templates: {e}")
        return []

def save_template(title: str, category: str, content: str, shortcut: str = ""):
    """Save a new custom message template."""
    try:
        init_db()
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO templates (title, category, content, shortcut) VALUES (?, ?, ?, ?)", (title, category, content, shortcut))
        t_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return t_id
    except Exception as e:
        print(f"[DB] Error saving template: {e}")
        return None

def delete_template(template_id: int):
    try:
        init_db()
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM templates WHERE id = ?", (template_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        return False

def get_bookings():
    try:
        init_db()
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM bookings ORDER BY created_at DESC LIMIT 100")
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        return []

def update_booking_status(booking_id: int, status: str):
    try:
        init_db()
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE bookings SET status = ? WHERE id = ?", (status, booking_id))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False

def add_manual_booking(phone_number: str, customer_name: str, service: str, date_time: str, status: str = "confirmed", notes: str = ""):
    try:
        init_db()
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO bookings (phone_number, customer_name, service_requested, booking_date_time, status, notes)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (phone_number, customer_name, service, date_time, status, notes))
        conn.commit()
        booking_id = cursor.lastrowid
        conn.close()
        return booking_id
    except Exception:
        return None

def get_stats():
    try:
        init_db()
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) as total FROM messages")
        total_messages = cursor.fetchone()["total"]
        
        cursor.execute("SELECT COUNT(DISTINCT phone_number) as contacts FROM messages")
        total_contacts = cursor.fetchone()["contacts"]
        
        cursor.execute("SELECT COUNT(*) as inbound FROM messages WHERE direction = 'inbound'")
        inbound_messages = cursor.fetchone()["inbound"]
        
        cursor.execute("SELECT COUNT(*) as outbound FROM messages WHERE direction = 'outbound'")
        outbound_messages = cursor.fetchone()["outbound"]
        
        cursor.execute("SELECT AVG(latency_ms) as avg_latency FROM messages WHERE latency_ms > 0")
        avg_row = cursor.fetchone()
        avg_latency = round(avg_row["avg_latency"] or 0, 2) if avg_row else 0
        
        cursor.execute("SELECT COUNT(*) as count FROM webhook_logs")
        total_webhooks = cursor.fetchone()["count"]

        cursor.execute("SELECT COUNT(*) as count FROM bookings")
        total_bookings = cursor.fetchone()["count"]
        
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        cursor.execute("SELECT COUNT(*) as today_total FROM messages WHERE created_at LIKE ?", (f"{today_str}%",))
        today_messages = cursor.fetchone()["today_total"]
        
        conn.close()
        return {
            "total_messages": total_messages,
            "total_contacts": total_contacts,
            "inbound_messages": inbound_messages,
            "outbound_messages": outbound_messages,
            "avg_latency_ms": avg_latency,
            "total_webhooks": total_webhooks,
            "total_bookings": total_bookings,
            "today_messages": today_messages
        }
    except Exception as e:
        return {
            "total_messages": 0, "total_contacts": 0, "inbound_messages": 0, "outbound_messages": 0,
            "avg_latency_ms": 0, "total_webhooks": 0, "total_bookings": 0, "today_messages": 0
        }

def save_webhook_log(endpoint: str, method: str, payload: any = None, status_code: int = 200, ip_address: str = ""):
    try:
        init_db()
        conn = get_connection()
        cursor = conn.cursor()
        payload_str = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload or "")
        cursor.execute("INSERT INTO webhook_logs (endpoint, method, payload, status_code, ip_address) VALUES (?, ?, ?, ?, ?)", 
                       (endpoint, method, payload_str, status_code, ip_address))
        conn.commit()
        conn.close()
    except Exception:
        pass

def get_webhook_logs(limit: int = 50):
    try:
        init_db()
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM webhook_logs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception:
        return []

def clear_data():
    try:
        init_db()
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM messages")
        cursor.execute("DELETE FROM bookings")
        cursor.execute("DELETE FROM webhook_logs")
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False

def delete_contact(phone_number: str):
    try:
        init_db()
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM messages WHERE phone_number = ?", (phone_number,))
        cursor.execute("DELETE FROM bookings WHERE phone_number = ?", (phone_number,))
        cursor.execute("DELETE FROM contact_settings WHERE phone_number = ?", (phone_number,))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False
