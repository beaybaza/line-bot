import os
import json
import hashlib
import hmac
import base64
from datetime import datetime, timedelta
from flask import Flask, request, abort
import google.generativeai as genai
import requests

app = Flask(__name__)

# ===== ตั้งค่า API Keys =====
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")

genai.configure(api_key=GEMINI_API_KEY)

# ===== ตั้งค่าบอท =====
AUTO_REOPEN_MINUTES = 60  # เปิดบอทกลับอัตโนมัติหลัง 60 นาที
BOT_STATUS_FILE = "bot_status.json"
FALLBACK_MESSAGE = "ขอโทษนะคะ น้องออโต้ยังไม่มีข้อมูลตรงนี้ค่ะ แอดมินร้านบ้านเลาคำจะรีบติดต่อกลับโดยเร็วที่สุดเลยนะคะ 🙏"

# ===== System Prompt น้องออโต้ =====
SYSTEM_PROMPT = """
คุณคือ "น้องออโต้" ผู้ช่วยอัตโนมัติประจำร้านบ้านเลาคำ รับทำป้ายและงานปริ้นทุกชนิด

== บุคลิกและการพูด ==
- ตอบสุภาพ อบอุ่น เป็นกันเอง
- ใช้คำลงท้าย "ค่ะ" เสมอ
- ถ้าลูกค้าพูดภาษาเหนือ ให้ตอบภาษาเหนือผสมได้เลย เช่น "ได้เลยเจ้า", "ขอบคุณหลายๆ เน้อ", "รอสักครู่เน้อ"
- ห้ามตอบเรื่องที่ไม่เกี่ยวกับร้าน

== ข้อมูลวันหยุดสำคัญ ==
- ร้านบ้านเลาคำหยุดพิเศษ วันที่ 7-9 มีนาคม 2568
- รับงานได้ถึงวันพุธที่ 5 มีนาคม 2568 เวลา 18.00 น. เท่านั้น
- ร้านหยุดประจำทุกวันเสาร์

== ตารางวันรับงานช่วงหยุด ==
- สั่งงานวันที่ 6 มีนาคม (ศุกร์) → จะได้รับงานวันที่ 10 มีนาคม
- สั่งงานวันที่ 7 มีนาคม (เสาร์/วันหยุด) → จะได้รับงานวันที่ 11-12 มีนาคม
- สั่งงานวันที่ 8 มีนาคม (อาทิตย์/วันหยุด) → จะได้รับงานวันที่ 11 มีนาคม
- สั่งงานวันที่ 9 มีนาคม (จันทร์/วันหยุด) → จะได้รับงานวันที่ 12 มีนาคม

== เมื่อลูกค้าสั่งงานหรือส่งไฟล์/รูปมา ==
ให้ตอบว่า "รับทราบเลยนะคะ น้องออโต้ได้รับข้อมูลแล้วค่ะ 
แอดมินร้านบ้านเลาคำจะรีบตรวจสอบและติดต่อกลับโดยเร็วที่สุดเลยนะคะ 🙏"

== ถ้าไม่มีข้อมูลหรือไม่แน่ใจ ==
ให้ตอบแค่นี้เท่านั้น อย่าแต่งข้อมูลขึ้นมาเอง:
"ขอโทษนะคะ น้องออโต้ยังไม่มีข้อมูลตรงนี้ค่ะ แอดมินร้านบ้านเลาคำจะรีบติดต่อกลับโดยเร็วที่สุดเลยนะคะ 🙏"
"""

# ===== จัดการสถานะบอท =====
def load_status():
    if os.path.exists(BOT_STATUS_FILE):
        with open(BOT_STATUS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_status(status):
    with open(BOT_STATUS_FILE, "w") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)

def is_bot_active(user_id):
    status = load_status()
    if user_id not in status:
        return True  # ค่าเริ่มต้น บอทเปิดอยู่
    
    user_data = status[user_id]
    if user_data.get("status") == "open":
        return True
    
    # เช็คว่าครบ 60 นาทีแล้วหรือยัง
    closed_at = datetime.fromisoformat(user_data.get("closed_at", ""))
    reopen_time = closed_at + timedelta(minutes=AUTO_REOPEN_MINUTES)
    
    if datetime.now() >= reopen_time:
        # ครบเวลาแล้ว เปิดบอทกลับอัตโนมัติ
        set_bot_status(user_id, "open")
        return True
    
    return False  # บอทยังปิดอยู่

def set_bot_status(user_id, status):
    all_status = load_status()
    if status == "closed":
        all_status[user_id] = {
            "status": "closed",
            "closed_at": datetime.now().isoformat()
        }
    else:
        all_status[user_id] = {"status": "open"}
    save_status(all_status)

# ===== ส่งข้อความกลับ Line =====
def reply_message(reply_token, message):
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }
    data = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": message}]
    }
    requests.post(url, headers=headers, json=data)

# ===== ตรวจสอบ Signature จาก Line =====
def verify_signature(body, signature):
    hash = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()
    return hmac.compare_digest(
        base64.b64encode(hash).decode("utf-8"),
        signature
    )

# ===== ถามบอท Gemini =====
def ask_gemini(user_message):
    model = genai.GenerativeModel(
        model_name="gemini-2.5-flash",
        system_instruction=SYSTEM_PROMPT
    )
    response = model.generate_content(
        user_message,
        generation_config=genai.GenerationConfig(
            max_output_tokens=300
        )
    )
    return response.text

# ===== Webhook หลัก =====
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # Line ส่ง GET มา Verify
    if request.method == "GET":
        return "OK", 200
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    if not verify_signature(body, signature):
        abort(400)

    events = request.json.get("events", [])

    for event in events:
        if event.get("type") != "message":
            continue
        if event["message"].get("type") != "text":
            # ถ้าเป็นรูปหรือไฟล์
            user_id = event["source"]["userId"]
            reply_token = event["replyToken"]
            if is_bot_active(user_id):
                reply_message(reply_token, 
                    "รับทราบค่ะ ได้รับไฟล์/รูปแล้วค่ะ แอดมินร้านบ้านเลาคำจะรีบติดต่อกลับโดยเร็วที่สุดเลยนะคะ 🙏")
            continue

        user_id = event["source"]["userId"]
        reply_token = event["replyToken"]
        user_message = event["message"]["text"]

        # เช็คว่าบอทเปิดอยู่ไหม
        if not is_bot_active(user_id):
            continue  # บอทปิดอยู่ แอดมินตอบเอง

        # ถามบอท Gemini
        bot_reply = ask_gemini(user_message)

        # ตรวจว่าบอทตอบแบบ Fallback ไหม
        if "ยังไม่มีข้อมูล" in bot_reply:
            reply_message(reply_token, FALLBACK_MESSAGE)
            set_bot_status(user_id, "closed")  # ปิดบอทห้องนี้
        else:
            reply_message(reply_token, bot_reply)

    return "OK", 200

@app.route("/", methods=["GET"])
def index():
    return "น้องออโต้ บอทร้านบ้านเลาคำ พร้อมให้บริการค่ะ 🌿", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
