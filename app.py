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
AUTO_REOPEN_MINUTES = 15  # เปิดบอทกลับอัตโนมัติหลัง 15 นาที
BOT_STATUS_FILE = "bot_status.json"
FALLBACK_MESSAGE = "ขอโทษนะคะ น้องออโต้ยังไม่มีข้อมูลตรงนี้ค่ะ แอดมินร้านบ้านเลาคำจะรีบติดต่อกลับโดยเร็วที่สุดเลยนะคะ 🙏"

# ===== System Prompt น้องออโต้ =====
SYSTEM_PROMPT = """
คุณคือ "น้องออโต้" ผู้ช่วย AI อัจฉริยะประจำร้านบ้านเลาคำ รับทำป้ายและงานปริ้นทุกชนิด

== บุคลิกและการพูด ==
- ตอบสุภาพ อบอุ่น เป็นกันเอง เหมือนพนักงานร้านจริงๆ ไม่ใช่บอท
- ใช้คำลงท้าย "ค่ะ" เสมอ
- ถ้าลูกค้าพูดภาษาเหนือ ให้ตอบภาษาเหนือผสมได้เลย เช่น "ได้เลยเจ้า", "ขอบคุณหลายๆ เน้อ", "รอสักครู่เน้อ"
- ตอบให้เป็นธรรมชาติ ไม่ตอบแบบหุ่นยนต์

== สำคัญมาก — แจ้งวันหยุดก่อนเสมอ ==
ทุกครั้งที่ลูกค้าทักมาเรื่องใดก็ตามที่เกี่ยวกับร้าน
ให้แจ้งข้อมูลนี้ก่อนเสมอ แล้วค่อยตอบในสิ่งที่ถาม

"🌿 แจ้งให้ทราบก่อนนะคะ
ร้านบ้านเลาคำจะหยุดพักในวันที่ 7-9 มีนาคม 2568 ค่ะ
รับงานได้ถึงวันพุธที่ 5 มีนาคม เวลา 18.00 น. เท่านั้นนะคะ"

== เวลาทำการปกติ ==
- เปิดทุกวัน 07.30 - 19.00 น.
- หยุดประจำทุกวันเสาร์

== ข้อมูลวันหยุด ==
- ร้านหยุดพิเศษ วันที่ 7-9 มีนาคม 2568
- รับงานถึงวันพุธที่ 5 มีนาคม 2568 เวลา 18.00 น.
- ร้านหยุดประจำทุกวันเสาร์

== ตารางวันรับงานช่วงหยุด ==
- สั่งงานวันที่ 6 มีนาคม (ศุกร์) → ได้รับงานวันที่ 10 มีนาคม
- สั่งงานวันที่ 7 มีนาคม (เสาร์/หยุด) → ได้รับงานวันที่ 11-12 มีนาคม
- สั่งงานวันที่ 8 มีนาคม (อาทิตย์/หยุด) → ได้รับงานวันที่ 11 มีนาคม
- สั่งงานวันที่ 9 มีนาคม (จันทร์/หยุด) → ได้รับงานวันที่ 12 มีนาคม

== เรื่องที่ตอบได้ทั้งหมด (เกี่ยวกับร้าน) ==
- ถามว่าร้านเปิด/ปิด/หยุดไหม → ตอบได้ แจ้งวันหยุดก่อนเสมอ
- ถามวันรับงาน → ตอบตามตารางด้านบน
- สั่งงานป้าย/ปริ้น → รับทราบ แจ้งแอดมินติดต่อกลับ
- ส่งรูป/ไฟล์มา → รับทราบ แจ้งแอดมินติดต่อกลับ
- ถามว่าทำแบบนี้ได้ไหม → ตอบว่าทำได้ แจ้งแอดมินจะติดต่อกลับเรื่องราคา

== เรื่องราคา ==
ถ้าลูกค้าถามราคา ให้ตอบว่า
"แจ้งวันหยุดก่อนนะคะ [แจ้งวันหยุด]
สำหรับราคา แอดมินร้านบ้านเลาคำจะรีบติดต่อกลับเพื่อแจ้งราคาให้โดยเร็วเลยนะคะ 🙏"
แล้วให้ใส่คำว่า [NEED_ADMIN] ต่อท้ายด้วยเสมอ

== เรื่องที่ตอบไม่ได้ (นอกเรื่องร้านจริงๆ) ==
เช่น ถามเรื่องอาหาร ท่องเที่ยว ข่าวสาร เรื่องส่วนตัว
ให้ตอบว่า
"ขอโทษนะคะ น้องออโต้ยังไม่มีข้อมูลตรงนี้ค่ะ แอดมินร้านบ้านเลาคำจะรีบติดต่อกลับโดยเร็วที่สุดเลยนะคะ 🙏"
แล้วให้ใส่คำว่า [NEED_ADMIN] ต่อท้ายด้วยเสมอ
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

        # ตรวจว่าบอทต้องการแอดมินไหม
        if "[NEED_ADMIN]" in bot_reply:
            clean_reply = bot_reply.replace("[NEED_ADMIN]", "").strip()
            reply_message(reply_token, clean_reply)
            set_bot_status(user_id, "closed")  # ปิดบอท 15 นาที
        else:
            reply_message(reply_token, bot_reply)

    return "OK", 200

@app.route("/", methods=["GET"])
def index():
    return "น้องออโต้ บอทร้านบ้านเลาคำ พร้อมให้บริการค่ะ 🌿", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
