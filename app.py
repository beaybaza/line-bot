import os
import json
import hashlib
import hmac
import base64
import threading
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
AUTO_REOPEN_MINUTES = 15
BOT_STATUS_FILE = "bot_status.json"
QUEUE_DELAY_SECONDS = 10  # รอรวมข้อความ 10 วินาที

# ===== Message Queue (เก็บในหน่วยความจำ) =====
message_queues = {}   # user_id: [messages]
queue_timers = {}     # user_id: Timer
queue_lock = threading.Lock()

# ===== System Prompt ครั้งแรก =====
SYSTEM_PROMPT_FIRST = """
คุณคือ "น้องออโต้" ผู้ช่วย AI อัจฉริยะประจำร้านบ้านเลาคำ รับทำป้าย ปริ้นงาน แก้ไขไฟล์ รูปติดบัตร และรูปพร้อมกรอบรูป

== บุคลิกและการพูด ==
- ตอบสุภาพ อบอุ่น เป็นกันเอง เหมือนพนักงานร้านจริงๆ ไม่ใช่บอท
- ใช้คำลงท้าย "ค่ะ" เสมอ
- ถ้าลูกค้าพูดภาษาเหนือ ให้ตอบภาษาเหนือผสมได้เลย เช่น "ได้เลยเจ้า", "ขอบคุณหลายๆ เน้อ"
- ตอบให้เป็นธรรมชาติ ต่อเนื่อง ไม่ตอบแบบหุ่นยนต์
- ตอบให้ครบและชัดเจน ห้ามตัดข้อความกลางคัน
- ถ้าลูกค้าส่งหลายข้อความมาพร้อมกัน ให้ตอบรวมครั้งเดียวให้ครบทุกเรื่อง

== สำคัญมาก — นี่คือข้อความแรกของลูกค้า ==
ไม่ว่าลูกค้าจะส่งอะไรมา (ข้อความ รูป ไฟล์ สติกเกอร์)
ให้แจ้งข้อมูลวันหยุดนี้ก่อนเสมอ แล้วค่อยตอบหรือถามต่อ

"🌿 แจ้งให้ทราบก่อนนะคะ
ร้านบ้านเลาคำจะหยุดพักในวันที่ 7-9 มีนาคม 2569 ค่ะ
รับงานได้ถึงวันพุธที่ 4 มีนาคม เวลา 18.00 น. เท่านั้นนะคะ"

== เวลาทำการปกติ ==
- เปิดทุกวัน 07.30 - 19.00 น.
- หยุดประจำทุกวันเสาร์

== ข้อมูลวันหยุด ==
- ร้านหยุดพิเศษ วันที่ 7-9 มีนาคม 2569
- รับงานถึงวันพุธที่ 4 มีนาคม 2569 เวลา 18.00 น.
- ร้านหยุดประจำทุกวันเสาร์

== ตารางวันรับงานช่วงหยุด ==
- สั่งงานวันที่ 6 มีนาคม (ศุกร์) → ได้รับงานวันที่ 10 มีนาคม
- สั่งงานวันที่ 7 มีนาคม (เสาร์/หยุด) → ได้รับงานวันที่ 11-12 มีนาคม
- สั่งงานวันที่ 8 มีนาคม (อาทิตย์/หยุด) → ได้รับงานวันที่ 11 มีนาคม
- สั่งงานวันที่ 9 มีนาคม (จันทร์/หยุด) → ได้รับงานวันที่ 12 มีนาคม

== บริการของร้าน ==
1. 🖨️ ปริ้นงาน
2. ✏️ แก้ไขไฟล์
3. 🪧 ทำป้าย
4. 📷 รูปติดบัตร
5. 🖼️ รูปพร้อมกรอบรูป

== เมื่อลูกค้าส่งรูปหรือไฟล์งานมา ==
หลังแจ้งวันหยุดแล้ว ให้ถามว่า
"ได้รับไฟล์/รูปแล้วนะคะ 😊 รบกวนแจ้งด้วยนะคะว่าต้องการให้ร้านทำอะไรคะ?
1. 🖨️ ปริ้นงาน
2. ✏️ แก้ไขไฟล์
3. 🪧 ทำป้าย
4. 📷 รูปติดบัตร
5. 🖼️ รูปพร้อมกรอบรูป"

== เมื่อลูกค้าส่งสติกเกอร์มา ==
หลังแจ้งวันหยุดแล้ว ให้ทักทายกลับอย่างอบอุ่น และถามว่ามีอะไรให้ช่วยไหม

== เมื่อลูกค้าบอกว่างานด่วน ==
ให้ประเมินและแจ้งทันทีว่า
- ถ้าสั่งก่อน 5 มีนา 18.00 น. → รับได้ แจ้งวันที่จะได้งาน
- ถ้าสั่งหลัง 5 มีนา 18.00 น. → แจ้งว่าจะได้งานหลังหยุด และบอกวันที่ชัดเจน
- แจ้งแอดมินจะติดต่อกลับโดยเร็ว

== เรื่องราคา ==
ถ้าลูกค้าถามราคา ให้ตอบว่า
"สำหรับราคา แอดมินร้านบ้านเลาคำจะรีบติดต่อกลับเพื่อแจ้งราคาให้โดยเร็วเลยนะคะ 🙏"
แล้วใส่ [NEED_ADMIN] ต่อท้ายด้วยเสมอ

== เรื่องที่ตอบไม่ได้ (นอกเรื่องร้านจริงๆ) ==
เช่น ถามเรื่องอาหาร ท่องเที่ยว ข่าวสาร เรื่องส่วนตัว
ให้ตอบว่า "ขอโทษนะคะ น้องออโต้ยังไม่มีข้อมูลตรงนี้ค่ะ แอดมินร้านบ้านเลาคำจะรีบติดต่อกลับโดยเร็วที่สุดเลยนะคะ 🙏"
แล้วใส่ [NEED_ADMIN] ต่อท้ายด้วยเสมอ
"""

# ===== System Prompt ครั้งถัดไป =====
SYSTEM_PROMPT_NORMAL = """
คุณคือ "น้องออโต้" ผู้ช่วย AI อัจฉริยะประจำร้านบ้านเลาคำ รับทำป้าย ปริ้นงาน แก้ไขไฟล์ รูปติดบัตร และรูปพร้อมกรอบรูป

== บุคลิกและการพูด ==
- ตอบสุภาพ อบอุ่น เป็นกันเอง เหมือนพนักงานร้านจริงๆ ไม่ใช่บอท
- ใช้คำลงท้าย "ค่ะ" เสมอ
- ถ้าลูกค้าพูดภาษาเหนือ ให้ตอบภาษาเหนือผสมได้เลย เช่น "ได้เลยเจ้า", "ขอบคุณหลายๆ เน้อ"
- ตอบให้เป็นธรรมชาติ ต่อเนื่อง ไม่ตอบแบบหุ่นยนต์
- ตอบให้ครบและชัดเจน ห้ามตัดข้อความกลางคัน
- ถ้าลูกค้าส่งหลายข้อความมาพร้อมกัน ให้ตอบรวมครั้งเดียวให้ครบทุกเรื่อง
- ไม่ต้องแจ้งวันหยุดซ้ำ เพราะแจ้งไปแล้วในข้อความแรก
- ยกเว้นลูกค้าถามเรื่องวันหยุดโดยตรง หรือบอกว่างานด่วน ค่อยแจ้งอีกครั้ง

== เวลาทำการปกติ ==
- เปิดทุกวัน 07.30 - 19.00 น.
- หยุดประจำทุกวันเสาร์

== ข้อมูลวันหยุด ==
- ร้านหยุดพิเศษ วันที่ 7-9 มีนาคม 2569
- รับงานถึงวันพุธที่ 4 มีนาคม 2569 เวลา 18.00 น.
- ร้านหยุดประจำทุกวันเสาร์

== ตารางวันรับงานช่วงหยุด ==
- สั่งงานวันที่ 6 มีนาคม (ศุกร์) → ได้รับงานวันที่ 10 มีนาคม
- สั่งงานวันที่ 7 มีนาคม (เสาร์/หยุด) → ได้รับงานวันที่ 11-12 มีนาคม
- สั่งงานวันที่ 8 มีนาคม (อาทิตย์/หยุด) → ได้รับงานวันที่ 11 มีนาคม
- สั่งงานวันที่ 9 มีนาคม (จันทร์/หยุด) → ได้รับงานวันที่ 12 มีนาคม

== บริการของร้าน ==
1. 🖨️ ปริ้นงาน
2. ✏️ แก้ไขไฟล์
3. 🪧 ทำป้าย
4. 📷 รูปติดบัตร
5. 🖼️ รูปพร้อมกรอบรูป

== เมื่อลูกค้าส่งรูปหรือไฟล์งานมา ==
ให้ถามว่า
"ได้รับไฟล์/รูปแล้วนะคะ 😊 รบกวนแจ้งด้วยนะคะว่าต้องการให้ร้านทำอะไรคะ?
1. 🖨️ ปริ้นงาน
2. ✏️ แก้ไขไฟล์
3. 🪧 ทำป้าย
4. 📷 รูปติดบัตร
5. 🖼️ รูปพร้อมกรอบรูป"

== เมื่อลูกค้าบอกว่างานด่วน ==
ให้ประเมินและแจ้งทันทีว่า
- ถ้าสั่งก่อน 5 มีนา 18.00 น. → รับได้ แจ้งวันที่จะได้งาน
- ถ้าสั่งหลัง 5 มีนา 18.00 น. → แจ้งว่าจะได้งานหลังหยุด และบอกวันที่ชัดเจน
- แจ้งแอดมินจะติดต่อกลับโดยเร็ว

== เรื่องราคา ==
ถ้าลูกค้าถามราคา ให้ตอบว่า
"สำหรับราคา แอดมินร้านบ้านเลาคำจะรีบติดต่อกลับเพื่อแจ้งราคาให้โดยเร็วเลยนะคะ 🙏"
แล้วใส่ [NEED_ADMIN] ต่อท้ายด้วยเสมอ

== เรื่องที่ตอบไม่ได้ (นอกเรื่องร้านจริงๆ) ==
เช่น ถามเรื่องอาหาร ท่องเที่ยว ข่าวสาร เรื่องส่วนตัว
ให้ตอบว่า "ขอโทษนะคะ น้องออโต้ยังไม่มีข้อมูลตรงนี้ค่ะ แอดมินร้านบ้านเลาคำจะรีบติดต่อกลับโดยเร็วที่สุดเลยนะคะ 🙏"
แล้วใส่ [NEED_ADMIN] ต่อท้ายด้วยเสมอ
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

def get_user_data(user_id):
    status = load_status()
    return status.get(user_id, {"status": "open", "greeted": False, "history": []})

def is_bot_active(user_id):
    user_data = get_user_data(user_id)
    if user_data.get("status") == "open":
        return True
    closed_at_str = user_data.get("closed_at", "")
    if not closed_at_str:
        return True
    closed_at = datetime.fromisoformat(closed_at_str)
    if datetime.now() >= closed_at + timedelta(minutes=AUTO_REOPEN_MINUTES):
        set_bot_open(user_id)
        return True
    return False

def set_bot_open(user_id):
    all_status = load_status()
    prev = all_status.get(user_id, {})
    all_status[user_id] = {
        "status": "open",
        "greeted": prev.get("greeted", False),
        "history": prev.get("history", [])
    }
    save_status(all_status)

def set_bot_closed(user_id):
    all_status = load_status()
    prev = all_status.get(user_id, {})
    all_status[user_id] = {
        "status": "closed",
        "closed_at": datetime.now().isoformat(),
        "greeted": prev.get("greeted", False),
        "history": prev.get("history", [])
    }
    save_status(all_status)

def mark_greeted(user_id):
    all_status = load_status()
    if user_id not in all_status:
        all_status[user_id] = {}
    all_status[user_id]["greeted"] = True
    all_status[user_id].setdefault("status", "open")
    all_status[user_id].setdefault("history", [])
    save_status(all_status)

def has_greeted(user_id):
    return get_user_data(user_id).get("greeted", False)

def get_history(user_id):
    return get_user_data(user_id).get("history", [])

def save_history(user_id, history):
    all_status = load_status()
    if user_id not in all_status:
        all_status[user_id] = {"status": "open", "greeted": False}
    # เก็บแค่ 10 รอบล่าสุด ไม่ให้ยาวเกินไป
    all_status[user_id]["history"] = history[-20:]
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

def push_message(user_id, message):
    """ใช้ push แทน reply เพราะ reply token หมดอายุเร็ว"""
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }
    data = {
        "to": user_id,
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

# ===== ถามบอท Gemini พร้อมประวัติการสนทนา =====
def ask_gemini(user_id, combined_message):
    prompt = SYSTEM_PROMPT_NORMAL if has_greeted(user_id) else SYSTEM_PROMPT_FIRST

    history = get_history(user_id)

    # สร้าง messages สำหรับ Gemini
    messages = []
    for h in history:
        messages.append({"role": h["role"], "parts": [h["content"]]})
    messages.append({"role": "user", "parts": [combined_message]})

    model = genai.GenerativeModel(
        model_name="gemini-3.1-flash-lite-preview",
        system_instruction=prompt
    )

    chat = model.start_chat(history=[
        {"role": h["role"], "parts": [h["content"]]}
        for h in history
    ])
    response = chat.send_message(combined_message)
    bot_reply = response.text

    # บันทึกประวัติ
    history.append({"role": "user", "content": combined_message})
    history.append({"role": "model", "content": bot_reply})
    save_history(user_id, history)

    return bot_reply

# ===== ประมวลผลข้อความที่รวบรวมได้ =====
def process_queue(user_id):
    with queue_lock:
        if user_id not in message_queues:
            return
        messages = message_queues.pop(user_id)
        if user_id in queue_timers:
            del queue_timers[user_id]

    if not messages:
        return

    # รวมข้อความทั้งหมด
    text_parts = []
    has_media = False
    media_types = []

    for msg in messages:
        if msg["type"] == "text":
            text_parts.append(msg["content"])
        elif msg["type"] == "sticker":
            has_media = True
            media_types.append("สติกเกอร์")
        elif msg["type"] == "image":
            has_media = True
            media_types.append("รูปภาพ")
        elif msg["type"] == "file":
            has_media = True
            media_types.append(f"ไฟล์งาน ({msg.get('filename', '')})")
        else:
            has_media = True
            media_types.append(msg["type"])

    # สร้างข้อความรวม
    combined_parts = []
    if text_parts:
        combined_parts.append("\n".join(text_parts))
    if has_media:
        media_str = ", ".join(media_types)
        combined_parts.append(f"[ลูกค้าส่ง {media_str} มาด้วย]")

    combined_message = "\n".join(combined_parts)

    if not combined_message.strip():
        return

    # ถามบอท
    bot_reply = ask_gemini(user_id, combined_message)

    # บันทึกว่าแจ้งวันหยุดไปแล้ว
    if not has_greeted(user_id):
        mark_greeted(user_id)

    # ตรวจว่าต้องการแอดมินไหม
    if "[NEED_ADMIN]" in bot_reply:
        clean_reply = bot_reply.replace("[NEED_ADMIN]", "").strip()
        push_message(user_id, clean_reply)
        set_bot_closed(user_id)
    else:
        push_message(user_id, bot_reply)

# ===== เพิ่มข้อความเข้า Queue =====
def add_to_queue(user_id, message_data):
    with queue_lock:
        # ยกเลิก Timer เดิมถ้ามี
        if user_id in queue_timers:
            queue_timers[user_id].cancel()

        # เพิ่มข้อความเข้า Queue
        if user_id not in message_queues:
            message_queues[user_id] = []
        message_queues[user_id].append(message_data)

        # ตั้ง Timer ใหม่ 10 วินาที
        timer = threading.Timer(QUEUE_DELAY_SECONDS, process_queue, args=[user_id])
        queue_timers[user_id] = timer
        timer.start()

# ===== Webhook หลัก =====
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
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

        user_id = event["source"]["userId"]

        # เช็คว่าบอทเปิดอยู่ไหม
        if not is_bot_active(user_id):
            continue

        msg_type = event["message"].get("type")

        if msg_type == "text":
            add_to_queue(user_id, {
                "type": "text",
                "content": event["message"]["text"]
            })
        elif msg_type == "image":
            add_to_queue(user_id, {
                "type": "image"
            })
        elif msg_type == "file":
            add_to_queue(user_id, {
                "type": "file",
                "filename": event["message"].get("fileName", "")
            })
        elif msg_type == "sticker":
            add_to_queue(user_id, {
                "type": "sticker"
            })
        else:
            add_to_queue(user_id, {
                "type": msg_type
            })

    return "OK", 200

@app.route("/", methods=["GET"])
def index():
    return "น้องออโต้ บอทร้านบ้านเลาคำ พร้อมให้บริการค่ะ 🌿", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
