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
AUTO_REOPEN_MINUTES = 60
BOT_STATUS_FILE = "bot_status.json"
QUEUE_DELAY_SECONDS = 10

# ===== userId แอดมิน =====
ADMIN_IDS = {
    "Uc8ae11d9053037a0382af58361f594ec",
    "Ub757494532a99af92a6e2f70e898ca26"
}

# ===== Message Queue =====
message_queues = {}
queue_timers = {}
queue_lock = threading.Lock()


# ===== เช็ค Deadline ไฟล์ป้ายพร้อมปริ้น =====
def is_print_banner_open():
    """รับไฟล์ป้ายพร้อมปริ้นถึงแค่ 6 มีนาคม 14.00 น."""
    now = datetime.now()
    deadline = datetime(2026, 3, 6, 14, 0, 0)
    return now < deadline

# ===== นามสกุลไฟล์แต่ละประเภท =====
PRINT_EXTENSIONS = ['.pdf', '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp']
DESIGN_EXTENSIONS = ['.ai', '.psd', '.eps', '.svg', '.cdr', '.indd']
OFFICE_EXTENSIONS = ['.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx']

def analyze_filename(filename):
    """วิเคราะห์ชื่อไฟล์ว่าน่าจะเป็นงานประเภทไหน
    return: (job_type, is_ready_to_print)
    """
    if not filename:
        return None, False
    fname = filename.lower()
    ext = os.path.splitext(fname)[1]

    # นามสกุลที่พร้อมปริ้นได้เลย (ไม่ต้องออกแบบเพิ่ม)
    ready_to_print = ext in PRINT_EXTENSIONS or ext == '.pdf'
    # นามสกุลที่ต้องออกแบบเพิ่ม
    need_design = ext in DESIGN_EXTENSIONS

    # คีย์เวิร์ดในชื่อไฟล์
    if any(k in fname for k in ['banner', 'ป้าย', 'sign', 'flex', 'billboard', 'poster', 'โปสเตอร์']):
        return "งานป้าย", ready_to_print
    if any(k in fname for k in ['id', 'บัตร', 'card', 'passport', 'หน้าตรง', 'รูปติด']):
        return "รูปติดบัตร", ready_to_print
    if any(k in fname for k in ['frame', 'กรอบ', 'portrait']):
        return "รูปพร้อมกรอบรูป", ready_to_print
    if any(k in fname for k in ['cert', 'ใบ', 'diploma', 'award']):
        return "ปริ้นงาน", ready_to_print

    # วิเคราะห์จากนามสกุล
    if need_design:
        return "แก้ไขไฟล์/ทำป้าย", False
    if ext in PRINT_EXTENSIONS:
        return "ปริ้นงาน/รูป", True
    if ext in OFFICE_EXTENSIONS:
        return "ปริ้นงาน", True

    return None, False

# ===== System Prompt ครั้งแรก =====
SYSTEM_PROMPT_FIRST = """
คุณคือ "น้องออโต้" ผู้ช่วย AI อัจฉริยะประจำร้านบ้านเลาคำ รับทำป้าย ปริ้นงาน แก้ไขไฟล์ รูปติดบัตร และรูปพร้อมกรอบรูป

== บุคลิกและการพูด ==
- ตอบสุภาพ อบอุ่น เป็นกันเอง เหมือนพนักงานร้านจริงๆ ไม่ใช่บอท
- ใช้คำลงท้าย "ค่ะ" เสมอ
- ถ้าลูกค้าพูดภาษาเหนือ ตอบภาษาเหนือผสมได้เลย เช่น "ได้เลยเจ้า", "ขอบคุณหลายๆ เน้อ"
- ตอบสั้น กระชับ ได้ใจความ ไม่เกิน 3-4 บรรทัดต่อเรื่อง
- ห้ามอธิบายยืดยาว ไม่ต้องพูดซ้ำหลายรอบ
- ถ้าลูกค้าส่งหลายข้อความพร้อมกัน ตอบรวมครั้งเดียวให้ครบ

== สำคัญมาก — ข้อความแรกของลูกค้า ==
ไม่ว่าลูกค้าจะส่งอะไรมา (ข้อความ รูป ไฟล์ สติกเกอร์)
ให้แจ้งวันหยุดก่อนเสมอ แล้วค่อยตอบหรือถามต่อ

"🌿 สวัสดีค่ะ ขณะนี้น้องออโต้ AI กำลังดูแลแชทอยู่นะคะ
หากต้องการคุยกับแอดมินโดยตรง แจ้งได้เลยค่ะ 😊

ร้านบ้านเลาคำหยุดพักวันที่ 7-9 มีนาคม 2569 ค่ะ
เปิดทำการอีกครั้งวันอังคารที่ 10 มีนาคม 2569 นะคะ"

== เวลาทำการ ==
- เปิดทุกวัน 07.30 - 19.00 น. หยุดทุกวันเสาร์

== วันหยุดพิเศษ ==
- หยุด 7-9 มีนาคม 2569
- เปิดทำการอีกครั้ง วันอังคารที่ 10 มีนาคม 2569

== บริการของร้าน ==
1. 🖨️ ปริ้นงาน
2. ✏️ แก้ไขไฟล์
3. 🪧 ทำป้าย
4. 📷 รูปติดบัตร
5. 🖼️ รูปพร้อมกรอบรูป


== งานปริ้นป้าย (ไฟล์พร้อมปริ้น) ==
รับได้ถึงวันพฤหัสที่ 6 มีนาคม 2569 เวลา 14.00 น. เท่านั้น
หลังจากนั้นปิดรับ เพราะทำไม่ทัน

== แบ่งประเภทงาน ==

งานเร็ว → รับได้ปกติ ตอบตามปกติ
- ปริ้นเอกสาร, รูปติดบัตร, เข้าเล่ม, รูปพร้อมกรอบรูป
→ แจ้งแอดมินติดต่อกลับตามปกติ

งานช้า → รับได้ แต่แจ้งว่าเริ่มทำ 10 มีนาคม เป็นต้นไป
- ทำป้าย, ออกแบบป้าย, งานออกแบบอื่นๆ
→ ตอบว่า "รับทราบค่ะ 😊 งานประเภทนี้จะเริ่มดำเนินการได้ตั้งแต่วันที่ 10 มีนาคม เป็นต้นไปนะคะ เนื่องจากร้านหยุดพัก 7-9 มีนาคม แอดมินจะติดต่อกลับเพื่อยืนยันคิวงานให้โดยเร็วค่ะ 🙏"
แล้วใส่ [NEED_ADMIN] ต่อท้ายเสมอ

== วิเคราะห์ความต้องการอัตโนมัติ ==
ให้เดาจากข้อมูลที่ลูกค้าส่งมา ถ้าเดาได้ให้บอกเลย ไม่ต้องถามซ้ำ

- บอกขนาดเป็นเมตร เช่น 1x2ม, 2x3m → งานป้าย (งานช้า) แจ้ง 10 มีนา + [NEED_ADMIN]
- บอกขนาดเล็ก เช่น A4, 4x6, 5x7 → ปริ้นหรือรูปติดบัตร (งานเร็ว) ตอบปกติ
- บอกว่าแก้ตัวอักษร แก้สี แก้ข้อความ → งานออกแบบ (งานช้า) แจ้ง 10 มีนา + [NEED_ADMIN]
- มีชื่อไฟล์แนบมา → วิเคราะห์ชื่อไฟล์ก่อน แล้วแยกว่างานเร็วหรืองานช้า

== เมื่อลูกค้าส่งรูปหรือไฟล์มา ==
- รูปภาพทั่วไป → ถามว่า "ปริ้นรูปใช่ไหมคะ? หรือต้องการทำอย่างอื่นคะ?"
- มีชื่อไฟล์ → วิเคราะห์แล้วแยกประเภทงานเร็ว/ช้า แล้วตอบให้เหมาะสม
- ไม่แน่ใจ → ถามสั้นๆ ว่าต้องการทำอะไร

== เมื่อลูกค้าส่งสติกเกอร์มา ==
แจ้งวันหยุดก่อน แล้วทักทายกลับอบอุ่น และถามว่ามีอะไรให้ช่วยไหม

== เมื่อลูกค้าบอกว่างานด่วน ==
- งานเร็ว (ปริ้น/รูปติดบัตร/เข้าเล่ม) → รับได้ปกติ
- งานช้า (ป้าย/ออกแบบ) → แจ้งว่าเริ่มได้ 10 มีนา ไม่สามารถเร่งได้

== ข้อห้ามสำคัญมาก ❌ ==
- ห้ามบอกราคาเองเด็ดขาด ต้องให้แอดมินติดต่อกลับเท่านั้น
- ห้ามพูดว่างานเสร็จแล้ว แก้ไขแล้ว หรือดำเนินการแล้ว ทั้งที่ยังไม่ได้ทำ
- ห้ามสรุปว่างานเสร็จหรือส่งงานให้แล้ว ต้องรอแอดมินคอนเฟิร์มเท่านั้น
- ถ้าลูกค้าถามว่างานเสร็จไหม ให้ตอบว่า "รอแอดมินแจ้งกลับนะคะ"

== เมื่อแอดมินตอบไปแล้ว ==
ถ้าในบทสนทนามีข้อความจากแอดมินตอบเรื่องใดไปแล้ว
ห้ามตอบซ้ำในเรื่องนั้น ให้ตอบแค่ส่วนที่ยังไม่ได้ตอบเท่านั้น

== เรื่องราคา ==
ตอบว่า "สำหรับราคา แอดมินจะรีบติดต่อกลับเพื่อแจ้งให้โดยเร็วเลยนะคะ 🙏"
แล้วใส่ [NEED_ADMIN] ต่อท้ายเสมอ

== เรื่องที่ตอบไม่ได้ ==
ตอบว่า "ขอโทษนะคะ น้องออโต้ยังไม่มีข้อมูลตรงนี้ค่ะ แอดมินจะรีบติดต่อกลับโดยเร็วค่ะ 🙏"
แล้วใส่ [NEED_ADMIN] ต่อท้ายเสมอ
"""

# ===== System Prompt ครั้งถัดไป =====
SYSTEM_PROMPT_NORMAL = """
คุณคือ "น้องออโต้" ผู้ช่วย AI อัจฉริยะประจำร้านบ้านเลาคำ รับทำป้าย ปริ้นงาน แก้ไขไฟล์ รูปติดบัตร และรูปพร้อมกรอบรูป

== บุคลิกและการพูด ==
- ตอบสุภาพ อบอุ่น เป็นกันเอง เหมือนพนักงานร้านจริงๆ ไม่ใช่บอท
- ใช้คำลงท้าย "ค่ะ" เสมอ
- ถ้าลูกค้าพูดภาษาเหนือ ตอบภาษาเหนือผสมได้เลย
- ตอบสั้น กระชับ ได้ใจความ ไม่เกิน 3-4 บรรทัดต่อเรื่อง
- ถ้าลูกค้าส่งหลายข้อความพร้อมกัน ตอบรวมครั้งเดียวให้ครบ
- ไม่ต้องแจ้งวันหยุดซ้ำ ยกเว้นลูกค้าถามเรื่องวันหยุดหรืองานด่วนโดยตรง

== เวลาทำการ ==
- เปิดทุกวัน 07.30 - 19.00 น. หยุดทุกวันเสาร์

== วันหยุดพิเศษ ==
- หยุด 7-9 มีนาคม 2569
- เปิดทำการอีกครั้ง วันอังคารที่ 10 มีนาคม 2569

== บริการของร้าน ==
1. 🖨️ ปริ้นงาน
2. ✏️ แก้ไขไฟล์
3. 🪧 ทำป้าย
4. 📷 รูปติดบัตร
5. 🖼️ รูปพร้อมกรอบรูป


== งานปริ้นป้าย (ไฟล์พร้อมปริ้น) ==
รับได้ถึงวันพฤหัสที่ 6 มีนาคม 2569 เวลา 14.00 น. เท่านั้น
หลังจากนั้นปิดรับ เพราะทำไม่ทัน

== แบ่งประเภทงาน ==

งานเร็ว → รับได้ปกติ ตอบตามปกติ
- ปริ้นเอกสาร, รูปติดบัตร, เข้าเล่ม, รูปพร้อมกรอบรูป
→ แจ้งแอดมินติดต่อกลับตามปกติ

งานช้า → รับได้ แต่แจ้งว่าเริ่มทำ 10 มีนาคม เป็นต้นไป
- ทำป้าย, ออกแบบป้าย, งานออกแบบอื่นๆ
→ ตอบว่า "รับทราบค่ะ 😊 งานประเภทนี้จะเริ่มดำเนินการได้ตั้งแต่วันที่ 10 มีนาคม เป็นต้นไปนะคะ เนื่องจากร้านหยุดพัก 7-9 มีนาคม แอดมินจะติดต่อกลับเพื่อยืนยันคิวงานให้โดยเร็วค่ะ 🙏"
แล้วใส่ [NEED_ADMIN] ต่อท้ายเสมอ

== วิเคราะห์ความต้องการอัตโนมัติ ==
- บอกขนาดเป็นเมตร เช่น 1x2ม, 2x3m → งานป้าย (งานช้า) แจ้ง 10 มีนา + [NEED_ADMIN]
- บอกขนาดเล็ก เช่น A4, 4x6, 5x7 → ปริ้นหรือรูปติดบัตร (งานเร็ว) ตอบปกติ
- บอกว่าแก้ตัวอักษร แก้สี แก้ข้อความ → งานออกแบบ (งานช้า) แจ้ง 10 มีนา + [NEED_ADMIN]
- มีชื่อไฟล์แนบมา → วิเคราะห์ชื่อไฟล์ก่อน แล้วแยกว่างานเร็วหรืองานช้า

== เมื่อลูกค้าส่งรูปหรือไฟล์มา ==
- รูปภาพทั่วไป → ถามว่า "ปริ้นรูปใช่ไหมคะ? หรือต้องการทำอย่างอื่นคะ?"
- มีชื่อไฟล์ → วิเคราะห์แล้วแยกประเภทงานเร็ว/ช้า แล้วตอบให้เหมาะสม
- ไม่แน่ใจ → ถามสั้นๆ ว่าต้องการทำอะไร

== เมื่อลูกค้าบอกว่างานด่วน ==
- งานเร็ว (ปริ้น/รูปติดบัตร/เข้าเล่ม) → รับได้ปกติ
- งานช้า (ป้าย/ออกแบบ) → แจ้งว่าเริ่มได้ 10 มีนา ไม่สามารถเร่งได้

== ข้อห้ามสำคัญมาก ❌ ==
- ห้ามบอกราคาเองเด็ดขาด ต้องให้แอดมินติดต่อกลับเท่านั้น
- ห้ามพูดว่างานเสร็จแล้ว แก้ไขแล้ว หรือดำเนินการแล้ว ทั้งที่ยังไม่ได้ทำ
- ห้ามสรุปว่างานเสร็จหรือส่งงานให้แล้ว ต้องรอแอดมินคอนเฟิร์มเท่านั้น
- ถ้าลูกค้าถามว่างานเสร็จไหม ให้ตอบว่า "รอแอดมินแจ้งกลับนะคะ"

== เมื่อแอดมินตอบไปแล้ว ==
ถ้าในบทสนทนามีข้อความจากแอดมินตอบเรื่องใดไปแล้ว
ห้ามตอบซ้ำในเรื่องนั้น ให้ตอบแค่ส่วนที่ยังไม่ได้ตอบเท่านั้น

== เรื่องราคา ==
ตอบว่า "สำหรับราคา แอดมินจะรีบติดต่อกลับเพื่อแจ้งให้โดยเร็วเลยนะคะ 🙏"
แล้วใส่ [NEED_ADMIN] ต่อท้ายเสมอ

== เรื่องที่ตอบไม่ได้ ==
ตอบว่า "ขอโทษนะคะ น้องออโต้ยังไม่มีข้อมูลตรงนี้ค่ะ แอดมินจะรีบติดต่อกลับโดยเร็วค่ะ 🙏"
แล้วใส่ [NEED_ADMIN] ต่อท้ายเสมอ
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
    all_status[user_id]["history"] = history[-30:]
    save_status(all_status)

# ===== ส่งข้อความ =====
def push_message(user_id, message):
    # เพิ่ม signature ท้ายข้อความ
    if not message.endswith("— น้องออโต้ AI 🤖"):
        message = message + "\n— น้องออโต้ AI 🤖"
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

# ===== ตรวจสอบ Signature =====
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
def ask_gemini(user_id, combined_message):
    prompt = SYSTEM_PROMPT_NORMAL if has_greeted(user_id) else SYSTEM_PROMPT_FIRST
    history = get_history(user_id)

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

    history.append({"role": "user", "content": combined_message})
    history.append({"role": "model", "content": bot_reply})
    save_history(user_id, history)

    return bot_reply

# ===== ประมวลผล Queue =====
def process_queue(user_id):
    with queue_lock:
        if user_id not in message_queues:
            return
        messages = message_queues.pop(user_id)
        if user_id in queue_timers:
            del queue_timers[user_id]

    if not messages:
        return

    text_parts = []
    media_parts = []

    for msg in messages:
        if msg["type"] == "text":
            text_parts.append(msg["content"])
        elif msg["type"] == "image":
            media_parts.append("[ลูกค้าส่งรูปภาพมา — น่าจะเป็นงานปริ้นรูป ให้ถามยืนยัน]")
        elif msg["type"] == "file":
            fname = msg.get("filename", "")
            job_type, ready_to_print = analyze_filename(fname)
            if job_type:
                if "ป้าย" in job_type and ready_to_print:
                    # ไฟล์ป้ายพร้อมปริ้น → เช็ค deadline
                    if is_print_banner_open():
                        media_parts.append(f"[ลูกค้าส่งไฟล์ป้ายพร้อมปริ้นชื่อ '{fname}' มา — รับงานได้ ให้ยืนยันรับงานและแจ้งแอดมินติดต่อกลับ]")
                    else:
                        media_parts.append(f"[ลูกค้าส่งไฟล์ป้ายพร้อมปริ้นชื่อ '{fname}' มา — แต่เลยเวลา 14.00 น. ของวันที่ 6 มีนาคมแล้ว ปิดรับงานปริ้นป้ายแล้ว ให้แจ้งลูกค้าว่าไม่สามารถรับได้]")
                elif "ป้าย" in job_type and not ready_to_print:
                    media_parts.append(f"[ลูกค้าส่งไฟล์ป้ายที่ต้องออกแบบชื่อ '{fname}' มา — เป็นงานช้า แจ้งว่าเริ่มทำได้ 10 มีนา + [NEED_ADMIN]]")
                else:
                    media_parts.append(f"[ลูกค้าส่งไฟล์ชื่อ '{fname}' มา — วิเคราะห์แล้วน่าจะเป็น{job_type} ให้ยืนยันกับลูกค้า]")
            else:
                media_parts.append(f"[ลูกค้าส่งไฟล์ชื่อ '{fname}' มา — ให้ถามว่าต้องการทำอะไร]")
        elif msg["type"] == "sticker":
            media_parts.append("[ลูกค้าส่งสติกเกอร์มาทักทาย]")
        elif msg["type"] == "admin_message":
            text_parts.append(f"[แอดมินตอบลูกค้าไปแล้วว่า: {msg['content']} — ห้ามตอบซ้ำในเรื่องนี้]")

    combined_parts = []
    if text_parts:
        combined_parts.append("\n".join(text_parts))
    if media_parts:
        combined_parts.append("\n".join(media_parts))

    combined_message = "\n".join(combined_parts)
    if not combined_message.strip():
        return

    bot_reply = ask_gemini(user_id, combined_message)

    if not has_greeted(user_id):
        mark_greeted(user_id)

    if "[NEED_ADMIN]" in bot_reply:
        clean_reply = bot_reply.replace("[NEED_ADMIN]", "").strip()
        push_message(user_id, clean_reply)
        set_bot_closed(user_id)
    else:
        push_message(user_id, bot_reply)

# ===== เพิ่มเข้า Queue =====
def add_to_queue(user_id, message_data):
    with queue_lock:
        if user_id in queue_timers:
            queue_timers[user_id].cancel()
        if user_id not in message_queues:
            message_queues[user_id] = []
        message_queues[user_id].append(message_data)
        timer = threading.Timer(QUEUE_DELAY_SECONDS, process_queue, args=[user_id])
        queue_timers[user_id] = timer
        timer.start()

# ===== Webhook =====
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

        sender_id = event["source"]["userId"]
        msg_type = event["message"].get("type")

        # ===== แอดมินส่งข้อความ → บันทึกเข้า history ของลูกค้าคนนั้น =====
        if sender_id in ADMIN_IDS:
            if msg_type == "text":
                admin_text = event["message"]["text"]
                # ดึง chatId เพื่อรู้ว่าแอดมินกำลังคุยกับลูกค้าคนไหน
                # กรณี 1-on-1: Line OA จะไม่ส่ง userId ของลูกค้ามาโดยตรงตอนแอดมินตอบ
                # วิธีที่ดีที่สุดคือบันทึกเฉพาะ queue ที่ active อยู่ล่าสุด 1 รายการ
                with queue_lock:
                    active_users = list(message_queues.keys())
                if len(active_users) == 1:
                    # มีลูกค้าแค่คนเดียวที่ active → ใส่ได้เลย
                    uid = active_users[0]
                    with queue_lock:
                        if uid in message_queues:
                            message_queues[uid].append({
                                "type": "admin_message",
                                "content": admin_text
                            })
                elif len(active_users) == 0:
                    # ไม่มีลูกค้า active → บันทึกเป็น last_admin_reply เผื่อใช้ภายหลัง
                    pass
                # ถ้ามีลูกค้า active มากกว่า 1 คน → ไม่ใส่เพราะไม่รู้ว่าแอดมินตอบใคร
            continue  # ไม่ให้บอทตอบข้อความแอดมิน

        # ===== ลูกค้าส่งข้อความ =====
        user_id = sender_id

        if not is_bot_active(user_id):
            continue

        if msg_type == "text":
            add_to_queue(user_id, {
                "type": "text",
                "content": event["message"]["text"]
            })
        elif msg_type == "image":
            add_to_queue(user_id, {"type": "image"})
        elif msg_type == "file":
            add_to_queue(user_id, {
                "type": "file",
                "filename": event["message"].get("fileName", "")
            })
        elif msg_type == "sticker":
            add_to_queue(user_id, {"type": "sticker"})
        else:
            add_to_queue(user_id, {"type": msg_type})

    return "OK", 200

@app.route("/", methods=["GET"])
def index():
    return "น้องออโต้ บอทร้านบ้านเลาคำ พร้อมให้บริการค่ะ 🌿", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
