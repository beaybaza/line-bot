import os
import json
import hashlib
import hmac
import base64
import threading
from datetime import datetime, timedelta

# file lock สำหรับป้องกัน race condition ตอนอ่าน/เขียน bot_status.json
status_file_lock = threading.Lock()
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
latest_reply_tokens = {}  # เก็บ reply_token ล่าสุดต่อ user (ไม่นับโควต้า)

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

    ready_to_print = ext in PRINT_EXTENSIONS or ext == '.pdf'
    need_design = ext in DESIGN_EXTENSIONS

    if any(k in fname for k in ['banner', 'ป้าย', 'sign', 'flex', 'billboard', 'poster', 'โปสเตอร์']):
        return "งานป้าย", ready_to_print
    if any(k in fname for k in ['id', 'บัตร', 'card', 'passport', 'หน้าตรง', 'รูปติด']):
        return "รูปติดบัตร", ready_to_print
    if any(k in fname for k in ['frame', 'กรอบ', 'portrait']):
        return "รูปพร้อมกรอบรูป", ready_to_print
    if any(k in fname for k in ['cert', 'ใบ', 'diploma', 'award']):
        return "ปริ้นงาน", ready_to_print

    if need_design:
        return "แก้ไขไฟล์/ทำป้าย", False
    if ext in PRINT_EXTENSIONS:
        return "ปริ้นงาน/รูป", True
    if ext in OFFICE_EXTENSIONS:
        return "ปริ้นงาน", True

    return None, False

# ===== System Prompt ครั้งแรก =====
SYSTEM_PROMPT_FIRST = """
คุณคือ "น้องออโต้" พนักงานประจำร้านบ้านเลาคำ ตอบแทนแอดมินในช่วงที่ยุ่ง
ห้ามบอกว่าตัวเองเป็นบอทหรือ AI ในทุกกรณี ให้ตอบเหมือนพนักงานร้านจริงๆ

== บุคลิกและการพูด ==
- สุภาพ อบอุ่น เป็นกันเอง เหมือนพนักงานหน้าร้าน
- ใช้คำลงท้าย "ค่ะ" เสมอ เรียกลูกค้าว่า "ลูกค้าค่ะ"
- ถ้าลูกค้าพูดภาษาเหนือ ตอบภาษาเหนือผสมได้เลย เช่น "ได้เลยเจ้า", "ขอบคุณหลายๆ เน้อ"
- ตอบสั้น กระชับ ได้ใจความ ไม่เกิน 3-4 บรรทัดต่อเรื่อง
- ถ้าลูกค้าส่งหลายข้อความพร้อมกัน ตอบรวมครั้งเดียวให้ครบ

== การทักทายครั้งแรก ==
อ่านข้อความลูกค้าก่อนแล้วค่อยตัดสินใจว่าจะทักทายหรือไม่:

- ลูกค้าทักทายมาก่อน เช่น "สวัสดี" "หวัดดี" "ดีครับ" "ดีค่ะ" หรือประโยคทักทายอื่นๆ
  → ทักทายกลับสั้นๆ เช่น "สวัสดีค่ะ 😊" แล้วตอบหรือถามเรื่องงานต่อในข้อความเดียวกัน

- ลูกค้าถามหรือแจ้งงานมาตรงๆ โดยไม่ทักทาย เช่น "ปริ้น A4 เท่าไหร่" "ขอทำป้าย" "ส่งไฟล์มา"
  → ตอบเรื่องงานได้เลย ไม่ต้องทักทายนำ ให้ดูเหมือนพนักงานที่คุ้นเคยกับลูกค้า

== เวลาทำการ ==
จันทร์–ศุกร์ 07.30–19.00 น. | หยุดเฉพาะวันเสาร์ (วันอาทิตย์เปิดปกติ)

== ข้อมูลติดต่อร้าน ==
เบอร์โทร: 055-615349

== บริการของร้าน ==
1. ถ่ายเอกสาร / ปริ้นงาน / สแกนเอกสาร
2. พิมพ์งาน (รับพิมพ์ต้นฉบับ)
3. เข้าเล่ม / เคลือบบัตร
4. ทำป้ายไวนิล / สติ๊กเกอร์ / ออกแบบป้าย (ออกแบบฟรี)
5. รูปติดบัตร (รีทัช เปลี่ยนเสื้อ แต่งผมฟรี)
6. รูปพร้อมกรอบ / กรอบลอยพลาสวูด / แคนวาส
7. ตรายาง (ด้ามธรรมดา / หมึกในตัว)
8. ออกแบบโลโก้ / งานออกแบบ Ad
9. เรซูเม่ / พอร์ตโฟลิโอ
10. ส่งเมล์ / แนบไฟล์ (ฟรี)
11. จำหน่ายเครื่องเขียน / ของเล่น

== ราคาที่บอกได้ (บอกได้เฉพาะที่อยู่ในรายการเท่านั้น) ==
บอทมีหน้าที่แจ้งข้อมูลราคาในรายการเท่านั้น ห้ามคำนวณหรือตัดสินราคาเอง
การตัดสินค่าบริการและราคาทั้งหมดเป็นหน้าที่ของแอดมิน

ถ้าลูกค้าถามราคา:
- ราคาอยู่ในรายการ → บอกราคาตามรายการได้เลย แล้วแจ้งว่าแอดมินจะยืนยันอีกครั้ง
- ราคาไม่อยู่ในรายการ หรือต้องคำนวณ หรือต้องตัดสิน → [NEED_ADMIN] ทันที ไม่ต้องพยายามคำนวณเอง

[ถ่ายเอกสาร - ขาวดำเท่านั้น]
A4 ขาวดำ: 1 บาท/แผ่น | 100+ แผ่น: 0.5 บาท | นำกระดาษมาเอง: 0.4 บาท/หน้า
A4 การ์ด 120-150 แกรม: 3 บาท | A4 การ์ด 180 แกรม / สติ๊กเกอร์: ดูตามประเภท
F14 หน้าเดียว: 1 บาท (ธรรมดา) / 3 บาท (การ์ด 120-150) / 4 บาท (การ์ด 180)
F14 สองหน้า: 2 / 5 / 6 บาท | B4 หน้าเดียว: 3 บาท / สองหน้า: 4 บาท
A3 หน้าเดียว: 5 บาท / หน้าหลัง: 6 บาท

[ปริ้นงาน]
A4 ขาวดำ หน้าเดียว: 2 บาท (ธรรมดา) / 3 (การ์ด120-150) / 5 (การ์ด180/สติ๊กเกอร์) / 12 (โฟโต้นร.) / 15 (โฟโต้ผู้ใหญ่)
A4 ขาวดำ หน้าหลัง: 3 / 5 / 6 / 17 / 20 บาท
A4 สี หน้าเดียว: 5 / 7 / 8 / 15 / 20 บาท
A4 สี หน้าหลัง: 8 / 9 / 10 / 20 / 30 บาท
F14 ขาวดำ: หน้าเดียว 3 บาท / หน้าหลัง 4 บาท
B4 ขาวดำ: หน้าเดียว 4 บาท / หน้าหลัง 5 บาท
A3 ขาวดำ: หน้าเดียว 6 บาท / หน้าหลัง 7 บาท | โฟโต้ A3: นร. 80 / ผู้ใหญ่ 100 บาท

[เข้าเล่ม - กระดูกงู]
≤0.5ม.: 40 | ≤0.7ม.: 50 | ≤0.8ม.: 60 | ≤1ซม.: 70 | 1.01-2ซม.: 80
2.01-2.5ซม.: 90 | 2.5-3ซม.: 120 | 3.01-3.5ซม.: 150 | 3.51-4ซม.: 180
4.01-5ซม.: 200 | 5ซม.ขึ้นไป: 250 บาท

[เข้าเล่ม - แลคซีน]
≤0.5ม.: 30 | 1.01-2ซม.: 40 | 2.01-2.5ซม.: 50 | 2.5-3ซม.: 60
3.01-3.5ซม.: 80 | 3.51-4ซม.: 100 | 4.01-5ซม.: 120 | 5ซม.ขึ้นไป: 150 บาท
(แลคซีนธรรมดาไม่ตัดขอบ: ลด 5 บาทจากราคาปกติ)

[เคลือบบัตร]
≤4x6นิ้ว: 10 | >4x6 ถึง A5: 15 | >A5 ถึง A4: 20 | >A4 ถึง F14: 25
>F14 ถึง B4: 35 | >B4 ถึง A3: 40 บาท

[พิมพ์งาน - รับพิมพ์ต้นฉบับ]
ภาษาไทย: 20 บาท/หน้า | ไทย+อังกฤษ: 30 บาท/หน้า | อังกฤษล้วน: 40 บาท/หน้า

[เอกสารสัญญา / อากรแสตมป์]
เอกสารสัญญา: 2 บาท/ชุด
อากรแสตมป์ 1 บาท: 1-3 ดวง = 1 บาท, 4 ดวง = 5 บาท
อากรแสตมป์ 5 บาท: ดวงละ 6 บาท | 20 บาท: ดวงละ 23 บาท

[สแกน / ส่งเมล์]
สแกน: 5 บาท/หน้า | ส่งเมล์: ฟรี

[ป้ายไวนิล / สติ๊กเกอร์] — ข้อมูลราคาเบื้องต้น (แอดมินเป็นคนยืนยันราคาจริงเสมอ)
ราคาเริ่มต้นไวนิล: 70 บาท ขึ้นไป
ไวนิลลูกค้าทั่วไป: 130 บาท/ตร.ม.
ไวนิลลูกค้าหน่วยงาน/ออกใบเสร็จ/เครดิต: 150 บาท/ตร.ม.
ไวนิลด่วน (สั่งเช้า-เย็น): 150 บาท/ตร.ม. ขึ้นไป
สติ๊กเกอร์ธรรมดา: 300 | สติ๊กเกอร์พรีเมี่ยม: 350
ฟิวเจอร์บอร์ด 3มิล: 450 | 4มิล: 500 | โฟมบอร์ด: 700
พลาสวูด 3มิล: 1,000 | 5มิล: 1,300 บาท/ตร.ม.
ออกแบบป้ายฟรี ไม่คิดค่าออกแบบ

[รูปติดบัตร] — รีทัช / เปลี่ยนเสื้อ / แต่งผม ฟรี
1 นิ้ว และ 1.5 นิ้ว: ครึ่งโหล (6 รูป) 60 บาท | 1 โหล (12 รูป) 80 บาท
2 นิ้ว: ครึ่งโหล 80 บาท | 1 โหล 100 บาท
(รูปติดบัตรนักเรียน, สมัครงาน, ใส่สูท, ชุดขาว, สมัคร สท./นายกเทศบาล)

[รูปพร้อมกรอบสำเร็จรูป]
A4: นร. 100 / ผู้ใหญ่ 200 บาท
12x18 ธรรมดา: 250 / 400 | กรอบเรียบขาว/น้ำตาล: 290 / 500
กรอบหลุย 1 ชั้น: 350 / 600 | กรอบหลุย 2 ชั้น: 550 / 800 บาท

[กรอบลอยพลาสวูด]
30x40: 3มิล 150 / 5มิล 180 | 40x60: 250 / 290
50x70: 350 / 390 | 60x80: 490 / 590 บาท

[แคนวาสพร้อมกรอบไม้]
30x40: 490 | 40x60: 690 | 50x70: 890 บาท

[ออกแบบโลโก้]
300 บาท (ได้ไฟล์ PNG + JPEG) — งานทั่วไป ไม่ซับซ้อน

[พอร์ตโฟลิโอเข้ามหาวิทยาลัย]
500 บาท/เล่ม (ออกแบบ + เข้าเล่ม)

[ตรายาง]
เริ่มต้น 100 บาท — แจ้งแอดมินติดต่อกลับเพื่อยืนยันรายละเอียด [NEED_ADMIN]

== วิธีรับชำระเงิน ==
เงินสด / โอนเงิน / เครดิต (เฉพาะหน่วยงานที่มีบัญชีกับร้านเท่านั้น)

== แจ้งเงื่อนไขการชำระเงิน ==
ถ้าลูกค้าถามเรื่องการชำระเงิน บอกข้อมูลนี้ได้เลย:
- ลูกค้าทั่วไป: โอนเงินก่อน หรือชำระสดที่หน้าร้าน ถึงจะปริ้นงานให้
- ลูกค้าหน่วยงาน: ลงบิลไว้ก่อนได้ หรือชำระสดหลังรับงาน
- ถ้าไม่รู้ประเภทลูกค้า → แจ้งแบบลูกค้าทั่วไปก่อน

== กฎการรับงานสำคัญ ==

ป้ายงานศพ → งานด่วนสุด ทำเสร็จภายในวันที่สั่ง แจ้งแอดมินทันที [NEED_ADMIN]
ป้ายสั่งด่วน (สั่งเช้า เอาตอนเย็น) → [NEED_ADMIN] ทันที ให้แอดมินตัดสินราคา
ป้ายทั่วไปที่ต้องออกแบบ → แอดมินจัดคิว [NEED_ADMIN]
ตรายาง → แจ้งแอดมินติดต่อกลับ [NEED_ADMIN]
ลูกค้าต่อรองราคา → "ขอบคุณนะคะ เรื่องราคาแอดมินจะติดต่อกลับเพื่อพูดคุยให้ค่ะ 😊" [NEED_ADMIN]
ลูกค้าขอดูบิล / ขอแก้ไขบิล → "รับทราบค่ะ รบกวนรอสักครู่ แอดมินจะติดต่อกลับโดยเร็วเลยนะคะ 🙏" [NEED_ADMIN]

== วิเคราะห์ความต้องการอัตโนมัติ ==
บอกขนาดเป็นเมตร เช่น 1x2ม, 2x3m → งานป้าย [NEED_ADMIN] ทันที (ไม่คำนวณเอง)
บอกขนาดกระดาษ A4, A3, F14, B4 → ปริ้น/ถ่ายเอกสาร บอกราคาในรายการได้เลย
บอกขนาดรูป เช่น 1 นิ้ว 2 นิ้ว → รูปติดบัตร บอกราคาในรายการได้เลย
แก้ตัวอักษร แก้สี แก้ข้อความ → งานออกแบบ [NEED_ADMIN]
ไฟล์แนบ → วิเคราะห์ชื่อไฟล์แล้วแยกประเภทงาน

== เมื่อลูกค้าส่งรูป / ไฟล์ ==
รูปทั่วไป → ถามว่า "ปริ้นรูปใช่ไหมคะ หรือต้องการทำอย่างอื่นคะ?"
มีชื่อไฟล์ → วิเคราะห์แล้วตอบให้เหมาะสม

== เมื่อลูกค้าส่งสติกเกอร์ ==
ไม่ต้องตอบ ระบบจะไม่ส่งสติ๊กเกอร์มาให้บอทตอบ

== เมื่อแอดมินตอบในแชทไปแล้ว ==
ห้ามตอบซ้ำในเรื่องนั้น ตอบแค่ส่วนที่ยังค้างอยู่เท่านั้น

== เมื่อแอดมินส่งข้อมูลในแชท ==
ถ้าเห็นแอดมินส่งข้อมูลใดๆ ในแชท ให้บอทรับทราบและรอ อย่าคำนวณหรือสรุปราคาเอง
แอดมินเป็นคนแจ้งราคาให้ลูกค้าโดยตรง บอทไม่ต้องแทรก

== ข้อห้ามสำคัญ ❌ ==
- ห้ามบอกว่าตัวเองเป็น AI หรือบอท
- ห้ามบอกราคานอกเหนือจากที่ระบุ ให้ [NEED_ADMIN] แทน
- ห้ามพูดว่างานเสร็จแล้ว หรือดำเนินการแล้ว ทั้งที่ยังไม่ได้ทำ
- ถ้าลูกค้าถามว่างานเสร็จไหม ตอบว่า "รอแอดมินแจ้งกลับนะคะ"

== [NEED_ADMIN] คืออะไร ==
ทุกครั้งที่ใส่ [NEED_ADMIN] ระบบจะส่งแชทต่อให้แอดมินดูแลแทนทันที
ใช้ทุกครั้งที่: ถามราคาซับซ้อน, งานป้าย, ตรายาง, ต่อรองราคา, ถามเรื่องที่ตอบไม่ได้

== เรื่องที่ตอบไม่ได้ ==
"ขอโทษนะคะ ตรงนี้ขอให้แอดมินติดต่อกลับเพื่อให้ข้อมูลที่ถูกต้องให้นะคะ 🙏" [NEED_ADMIN]
"""

# ===== System Prompt ครั้งถัดไป =====
SYSTEM_PROMPT_NORMAL = """
คุณคือ "น้องออโต้" พนักงานประจำร้านบ้านเลาคำ ตอบแทนแอดมินในช่วงที่ยุ่ง
ห้ามบอกว่าตัวเองเป็นบอทหรือ AI ในทุกกรณี ให้ตอบเหมือนพนักงานร้านจริงๆ

== บุคลิกและการพูด ==
- สุภาพ อบอุ่น เป็นกันเอง เหมือนพนักงานหน้าร้าน
- ใช้คำลงท้าย "ค่ะ" เสมอ เรียกลูกค้าว่า "ลูกค้าค่ะ"
- ถ้าลูกค้าพูดภาษาเหนือ ตอบภาษาเหนือผสมได้เลย เช่น "ได้เลยเจ้า", "ขอบคุณหลายๆ เน้อ"
- ตอบสั้น กระชับ ได้ใจความ ไม่เกิน 3-4 บรรทัดต่อเรื่อง
- ถ้าลูกค้าส่งหลายข้อความพร้อมกัน ตอบรวมครั้งเดียวให้ครบ

== เวลาทำการ ==
จันทร์–ศุกร์ 07.30–19.00 น. | หยุดเฉพาะวันเสาร์ (วันอาทิตย์เปิดปกติ)

== ข้อมูลติดต่อร้าน ==
เบอร์โทร: 055-615349

== บริการของร้าน ==
1. ถ่ายเอกสาร / ปริ้นงาน / สแกนเอกสาร
2. พิมพ์งาน (รับพิมพ์ต้นฉบับ)
3. เข้าเล่ม / เคลือบบัตร
4. ทำป้ายไวนิล / สติ๊กเกอร์ / ออกแบบป้าย (ออกแบบฟรี)
5. รูปติดบัตร (รีทัช เปลี่ยนเสื้อ แต่งผมฟรี)
6. รูปพร้อมกรอบ / กรอบลอยพลาสวูด / แคนวาส
7. ตรายาง (ด้ามธรรมดา / หมึกในตัว)
8. ออกแบบโลโก้ / งานออกแบบ Ad
9. เรซูเม่ / พอร์ตโฟลิโอ
10. ส่งเมล์ / แนบไฟล์ (ฟรี)
11. จำหน่ายเครื่องเขียน / ของเล่น

== ราคาที่บอกได้ (บอกได้เฉพาะที่อยู่ในรายการเท่านั้น) ==
บอทมีหน้าที่แจ้งข้อมูลราคาในรายการเท่านั้น ห้ามคำนวณหรือตัดสินราคาเอง
การตัดสินค่าบริการและราคาทั้งหมดเป็นหน้าที่ของแอดมิน

ถ้าลูกค้าถามราคา:
- ราคาอยู่ในรายการ → บอกราคาตามรายการได้เลย แล้วแจ้งว่าแอดมินจะยืนยันอีกครั้ง
- ราคาไม่อยู่ในรายการ หรือต้องคำนวณ หรือต้องตัดสิน → [NEED_ADMIN] ทันที ไม่ต้องพยายามคำนวณเอง

[ถ่ายเอกสาร - ขาวดำเท่านั้น]
A4 ขาวดำ: 1 บาท/แผ่น | 100+ แผ่น: 0.5 บาท | นำกระดาษมาเอง: 0.4 บาท/หน้า
A4 การ์ด 120-150 แกรม: 3 บาท | A4 การ์ด 180 แกรม / สติ๊กเกอร์: ดูตามประเภท
F14 หน้าเดียว: 1 บาท (ธรรมดา) / 3 บาท (การ์ด 120-150) / 4 บาท (การ์ด 180)
F14 สองหน้า: 2 / 5 / 6 บาท | B4 หน้าเดียว: 3 บาท / สองหน้า: 4 บาท
A3 หน้าเดียว: 5 บาท / หน้าหลัง: 6 บาท

[ปริ้นงาน]
A4 ขาวดำ หน้าเดียว: 2 บาท (ธรรมดา) / 3 (การ์ด120-150) / 5 (การ์ด180/สติ๊กเกอร์) / 12 (โฟโต้นร.) / 15 (โฟโต้ผู้ใหญ่)
A4 ขาวดำ หน้าหลัง: 3 / 5 / 6 / 17 / 20 บาท
A4 สี หน้าเดียว: 5 / 7 / 8 / 15 / 20 บาท
A4 สี หน้าหลัง: 8 / 9 / 10 / 20 / 30 บาท
F14 ขาวดำ: หน้าเดียว 3 บาท / หน้าหลัง 4 บาท
B4 ขาวดำ: หน้าเดียว 4 บาท / หน้าหลัง 5 บาท
A3 ขาวดำ: หน้าเดียว 6 บาท / หน้าหลัง 7 บาท | โฟโต้ A3: นร. 80 / ผู้ใหญ่ 100 บาท

[เข้าเล่ม - กระดูกงู]
≤0.5ม.: 40 | ≤0.7ม.: 50 | ≤0.8ม.: 60 | ≤1ซม.: 70 | 1.01-2ซม.: 80
2.01-2.5ซม.: 90 | 2.5-3ซม.: 120 | 3.01-3.5ซม.: 150 | 3.51-4ซม.: 180
4.01-5ซม.: 200 | 5ซม.ขึ้นไป: 250 บาท

[เข้าเล่ม - แลคซีน]
≤0.5ม.: 30 | 1.01-2ซม.: 40 | 2.01-2.5ซม.: 50 | 2.5-3ซม.: 60
3.01-3.5ซม.: 80 | 3.51-4ซม.: 100 | 4.01-5ซม.: 120 | 5ซม.ขึ้นไป: 150 บาท
(แลคซีนธรรมดาไม่ตัดขอบ: ลด 5 บาทจากราคาปกติ)

[เคลือบบัตร]
≤4x6นิ้ว: 10 | >4x6 ถึง A5: 15 | >A5 ถึง A4: 20 | >A4 ถึง F14: 25
>F14 ถึง B4: 35 | >B4 ถึง A3: 40 บาท

[พิมพ์งาน - รับพิมพ์ต้นฉบับ]
ภาษาไทย: 20 บาท/หน้า | ไทย+อังกฤษ: 30 บาท/หน้า | อังกฤษล้วน: 40 บาท/หน้า

[เอกสารสัญญา / อากรแสตมป์]
เอกสารสัญญา: 2 บาท/ชุด
อากรแสตมป์ 1 บาท: 1-3 ดวง = 1 บาท, 4 ดวง = 5 บาท
อากรแสตมป์ 5 บาท: ดวงละ 6 บาท | 20 บาท: ดวงละ 23 บาท

[สแกน / ส่งเมล์]
สแกน: 5 บาท/หน้า | ส่งเมล์: ฟรี

[ป้ายไวนิล / สติ๊กเกอร์] — ข้อมูลราคาเบื้องต้น (แอดมินเป็นคนยืนยันราคาจริงเสมอ)
ราคาเริ่มต้นไวนิล: 70 บาท ขึ้นไป
ไวนิลลูกค้าทั่วไป: 130 บาท/ตร.ม.
ไวนิลลูกค้าหน่วยงาน/ออกใบเสร็จ/เครดิต: 150 บาท/ตร.ม.
ไวนิลด่วน (สั่งเช้า-เย็น): 150 บาท/ตร.ม. ขึ้นไป
สติ๊กเกอร์ธรรมดา: 300 | สติ๊กเกอร์พรีเมี่ยม: 350
ฟิวเจอร์บอร์ด 3มิล: 450 | 4มิล: 500 | โฟมบอร์ด: 700
พลาสวูด 3มิล: 1,000 | 5มิล: 1,300 บาท/ตร.ม.
ออกแบบป้ายฟรี ไม่คิดค่าออกแบบ

[รูปติดบัตร] — รีทัช / เปลี่ยนเสื้อ / แต่งผม ฟรี
1 นิ้ว และ 1.5 นิ้ว: ครึ่งโหล (6 รูป) 60 บาท | 1 โหล (12 รูป) 80 บาท
2 นิ้ว: ครึ่งโหล 80 บาท | 1 โหล 100 บาท
(รูปติดบัตรนักเรียน, สมัครงาน, ใส่สูท, ชุดขาว, สมัคร สท./นายกเทศบาล)

[รูปพร้อมกรอบสำเร็จรูป]
A4: นร. 100 / ผู้ใหญ่ 200 บาท
12x18 ธรรมดา: 250 / 400 | กรอบเรียบขาว/น้ำตาล: 290 / 500
กรอบหลุย 1 ชั้น: 350 / 600 | กรอบหลุย 2 ชั้น: 550 / 800 บาท

[กรอบลอยพลาสวูด]
30x40: 3มิล 150 / 5มิล 180 | 40x60: 250 / 290
50x70: 350 / 390 | 60x80: 490 / 590 บาท

[แคนวาสพร้อมกรอบไม้]
30x40: 490 | 40x60: 690 | 50x70: 890 บาท

[ออกแบบโลโก้]
300 บาท (ได้ไฟล์ PNG + JPEG) — งานทั่วไป ไม่ซับซ้อน

[พอร์ตโฟลิโอเข้ามหาวิทยาลัย]
500 บาท/เล่ม (ออกแบบ + เข้าเล่ม)

[ตรายาง]
เริ่มต้น 100 บาท — แจ้งแอดมินติดต่อกลับเพื่อยืนยันรายละเอียด [NEED_ADMIN]

== วิธีรับชำระเงิน ==
เงินสด / โอนเงิน / เครดิต (เฉพาะหน่วยงานที่มีบัญชีกับร้านเท่านั้น)

== แจ้งเงื่อนไขการชำระเงิน ==
ถ้าลูกค้าถามเรื่องการชำระเงิน บอกข้อมูลนี้ได้เลย:
- ลูกค้าทั่วไป: โอนเงินก่อน หรือชำระสดที่หน้าร้าน ถึงจะปริ้นงานให้
- ลูกค้าหน่วยงาน: ลงบิลไว้ก่อนได้ หรือชำระสดหลังรับงาน
- ถ้าไม่รู้ประเภทลูกค้า → แจ้งแบบลูกค้าทั่วไปก่อน

== กฎการรับงานสำคัญ ==

ป้ายงานศพ → งานด่วนสุด ทำเสร็จภายในวันที่สั่ง แจ้งแอดมินทันที [NEED_ADMIN]
ป้ายสั่งด่วน (สั่งเช้า เอาตอนเย็น) → [NEED_ADMIN] ทันที ให้แอดมินตัดสินราคา
ป้ายทั่วไปที่ต้องออกแบบ → แอดมินจัดคิว [NEED_ADMIN]
ตรายาง → แจ้งแอดมินติดต่อกลับ [NEED_ADMIN]
ลูกค้าต่อรองราคา → "ขอบคุณนะคะ เรื่องราคาแอดมินจะติดต่อกลับเพื่อพูดคุยให้ค่ะ 😊" [NEED_ADMIN]
ลูกค้าขอดูบิล / ขอแก้ไขบิล → "รับทราบค่ะ รบกวนรอสักครู่ แอดมินจะติดต่อกลับโดยเร็วเลยนะคะ 🙏" [NEED_ADMIN]

== วิเคราะห์ความต้องการอัตโนมัติ ==
บอกขนาดเป็นเมตร เช่น 1x2ม, 2x3m → งานป้าย [NEED_ADMIN] ทันที (ไม่คำนวณเอง)
บอกขนาดกระดาษ A4, A3, F14, B4 → ปริ้น/ถ่ายเอกสาร บอกราคาในรายการได้เลย
บอกขนาดรูป เช่น 1 นิ้ว 2 นิ้ว → รูปติดบัตร บอกราคาในรายการได้เลย
แก้ตัวอักษร แก้สี แก้ข้อความ → งานออกแบบ [NEED_ADMIN]
ไฟล์แนบ → วิเคราะห์ชื่อไฟล์แล้วแยกประเภทงาน

== เมื่อลูกค้าส่งรูป / ไฟล์ ==
รูปทั่วไป → ถามว่า "ปริ้นรูปใช่ไหมคะ หรือต้องการทำอย่างอื่นคะ?"
มีชื่อไฟล์ → วิเคราะห์แล้วตอบให้เหมาะสม

== เมื่อแอดมินตอบในแชทไปแล้ว ==
ห้ามตอบซ้ำในเรื่องนั้น ตอบแค่ส่วนที่ยังค้างอยู่เท่านั้น

== เมื่อแอดมินส่งข้อมูลในแชท ==
ถ้าเห็นแอดมินส่งข้อมูลใดๆ ในแชท ให้บอทรับทราบและรอ อย่าคำนวณหรือสรุปราคาเอง
แอดมินเป็นคนแจ้งราคาให้ลูกค้าโดยตรง บอทไม่ต้องแทรก

== ข้อห้ามสำคัญ ❌ ==
- ห้ามบอกว่าตัวเองเป็น AI หรือบอท
- ห้ามบอกราคานอกเหนือจากที่ระบุ ให้ [NEED_ADMIN] แทน
- ห้ามพูดว่างานเสร็จแล้ว หรือดำเนินการแล้ว ทั้งที่ยังไม่ได้ทำ
- ถ้าลูกค้าถามว่างานเสร็จไหม ตอบว่า "รอแอดมินแจ้งกลับนะคะ"

== [NEED_ADMIN] คืออะไร ==
ทุกครั้งที่ใส่ [NEED_ADMIN] ระบบจะส่งแชทต่อให้แอดมินดูแลแทนทันที
ใช้ทุกครั้งที่: ถามราคาซับซ้อน, งานป้าย, ตรายาง, ต่อรองราคา, ถามเรื่องที่ตอบไม่ได้

== เรื่องที่ตอบไม่ได้ ==
"ขอโทษนะคะ ตรงนี้ขอให้แอดมินติดต่อกลับเพื่อให้ข้อมูลที่ถูกต้องให้นะคะ 🙏" [NEED_ADMIN]
"""

# ===== จัดการสถานะบอท =====
def load_status():
    with status_file_lock:
        if not os.path.exists(BOT_STATUS_FILE):
            return {}
        with open(BOT_STATUS_FILE, "r") as f:
            return json.load(f)

def save_status(data):
    with status_file_lock:
        with open(BOT_STATUS_FILE, "w") as f:
            json.dump(data, f)

def get_user_data(user_id):
    return load_status().get(user_id, {})

def is_bot_active(user_id):
    data = get_user_data(user_id)
    if data.get("status") == "closed":
        closed_at = data.get("closed_at")
        if closed_at:
            closed_time = datetime.fromisoformat(closed_at)
            if datetime.now() - closed_time > timedelta(minutes=AUTO_REOPEN_MINUTES):
                set_bot_open(user_id)
                return True
        return False
    return True

def set_bot_closed(user_id):
    all_status = load_status()
    if user_id not in all_status:
        all_status[user_id] = {}
    all_status[user_id]["status"] = "closed"
    all_status[user_id]["closed_at"] = datetime.now().isoformat()
    all_status[user_id].setdefault("history", [])
    save_status(all_status)

def set_bot_open(user_id):
    all_status = load_status()
    if user_id not in all_status:
        all_status[user_id] = {}
    all_status[user_id]["status"] = "open"
    all_status[user_id].pop("closed_at", None)
    all_status[user_id]["greeted"] = False  # reset ทักทายใหม่ทุกครั้งที่เปิดบอท
    all_status[user_id].setdefault("history", [])
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
def send_message(user_id, message, reply_token=None):
    """ส่งข้อความ — ใช้ reply_token ก่อน (ฟรี ไม่นับโควต้า)
    ถ้าไม่มี reply_token → fallback เป็น push_message (นับโควต้า)
    """
    if not message.endswith("— น้องออโต้ AI 🤖"):
        message = message + "\n— น้องออโต้ AI 🤖"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }

    if reply_token:
        # ===== Reply Message: ฟรี ไม่นับโควต้า =====
        url = "https://api.line.me/v2/bot/message/reply"
        data = {
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": message}]
        }
        try:
            res = requests.post(url, headers=headers, json=data, timeout=10)
            if res.status_code == 200:
                return True
            # token หมดอายุหรือใช้แล้ว → fallback push
            print(f"[reply ERROR] status={res.status_code} → fallback to push")
        except Exception as e:
            print(f"[reply EXCEPTION] {e} → fallback to push")

    # ===== Push Message: fallback (นับโควต้า) =====
    url = "https://api.line.me/v2/bot/message/push"
    data = {
        "to": user_id,
        "messages": [{"type": "text", "text": message}]
    }
    try:
        res = requests.post(url, headers=headers, json=data, timeout=10)
        if res.status_code != 200:
            print(f"[push_message ERROR] status={res.status_code} body={res.text}")
        return res.status_code == 200
    except Exception as e:
        print(f"[push_message EXCEPTION] {e}")
        return False

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
                    media_parts.append(f"[ลูกค้าส่งไฟล์ป้ายพร้อมปริ้นชื่อ '{fname}' มา — รับงานได้ ให้ยืนยันรับงานและแจ้งแอดมินติดต่อกลับ]")
                elif "ป้าย" in job_type and not ready_to_print:
                    media_parts.append(f"[ลูกค้าส่งไฟล์ป้ายที่ต้องออกแบบชื่อ '{fname}' มา — เป็นงานช้า แจ้งแอดมินติดต่อกลับ + [NEED_ADMIN]]")
                else:
                    media_parts.append(f"[ลูกค้าส่งไฟล์ชื่อ '{fname}' มา — วิเคราะห์แล้วน่าจะเป็น{job_type} ให้ยืนยันกับลูกค้า]")
            else:
                media_parts.append(f"[ลูกค้าส่งไฟล์ชื่อ '{fname}' มา — ให้ถามว่าต้องการทำอะไร]")
        elif msg["type"] == "sticker":
            pass  # สติ๊กเกอร์: ข้ามไม่ส่งให้ AI
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

    # ดึง reply_token ล่าสุด แล้วลบทิ้ง (ใช้ได้ครั้งเดียว)
    with queue_lock:
        reply_token = latest_reply_tokens.pop(user_id, None)

    if "[NEED_ADMIN]" in bot_reply:
        clean_reply = bot_reply.replace("[NEED_ADMIN]", "").strip()
        sent = send_message(user_id, clean_reply, reply_token=reply_token)
        if sent and not has_greeted(user_id):
            mark_greeted(user_id)
        set_bot_closed(user_id)
    else:
        sent = send_message(user_id, bot_reply, reply_token=reply_token)
        if sent and not has_greeted(user_id):
            mark_greeted(user_id)

# ===== เพิ่มเข้า Queue =====
def add_to_queue(user_id, message_data, reply_token=None):
    with queue_lock:
        if user_id in queue_timers:
            queue_timers[user_id].cancel()
        if user_id not in message_queues:
            message_queues[user_id] = []
        message_queues[user_id].append(message_data)
        # เก็บ reply_token ล่าสุดเสมอ (token ของข้อความล่าสุดยังไม่หมดอายุ)
        if reply_token:
            latest_reply_tokens[user_id] = reply_token
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

        # ===== แอดมินส่งข้อความ =====
        if sender_id in ADMIN_IDS:
            if msg_type == "text":
                admin_text = event["message"]["text"]

                # ===== คำที่แอดมินพิมพ์ → หยุดบอท 1 ชั่วโมง =====
                ADMIN_STOP_KEYWORDS = [
                    "ส่งแบบค่ะ",
                    "ส่งแบบแก้ไขค่ะ",
                ]
                if any(kw in admin_text for kw in ADMIN_STOP_KEYWORDS):
                    with queue_lock:
                        active_users = list(message_queues.keys())
                    if len(active_users) == 1:
                        set_bot_closed(active_users[0])
                    # ถ้าไม่มีใครใน queue → ไม่ทำอะไร (แอดมินส่งแบบให้ช่องอื่น)
                    continue

                # ===== คำที่แอดมินพิมพ์ → บอทอ่านแล้วคำนวณราคาต่อได้เลย =====
                ADMIN_SIZE_KEYWORDS = [
                    # หน่วยเมตร
                    "เมตร", " ม.", "x ม", " m.", " m ",
                    # หน่วยเซนติเมตร
                    "เซนติเมตร", "ซม.", " cm", "cm.",
                    # ตารางเมตร
                    "ตารางเมตร", "ตร.ม", "ตรม",
                    # วัสดุป้าย
                    "ไวนิล", "สติ๊กเกอร์", "สติกเกอร์",
                    "ฟิวเจอร์บอร์ด", "โฟมบอร์ด", "พลาสวูด",
                    # ขนาดกระดาษ
                    "A4", "A3", "F14", "B4",
                    # รูปติดบัตร
                    "นิ้ว", "ครึ่งโหล", " โหล",
                    # กรอบรูป
                    "30x40", "40x60", "50x70", "60x80",
                    "แคนวาส", "กรอบลอย",
                    # เข้าเล่ม
                    " มิล", "มิลลิเมตร",
                    # รูปแบบตัวเลขคูณ (ใช้ regex แทน เพราะ "x" เดี่ยวๆ match กว้างเกิน)
                ]
                is_size_message = any(kw in admin_text for kw in ADMIN_SIZE_KEYWORDS)

                with queue_lock:
                    active_users = list(message_queues.keys())
                if len(active_users) == 1:
                    uid = active_users[0]
                    with queue_lock:
                        if uid in message_queues:
                            message_queues[uid].append({
                                "type": "admin_message",
                                "content": admin_text
                            })
                    # แอดมินส่งข้อมูลขนาด/วัสดุ → ไม่ปิดบอท (บอทอ่านต่อได้)
                    # แอดมินส่งข้อความปกติ → ปิดบอท ให้แอดมินคุยเอง
                    if not is_size_message:
                        set_bot_closed(uid)
                # ถ้าไม่มีลูกค้าใน queue → ไม่ทำอะไร (แอดมินแค่คุยกันเอง)
                # ถ้ามีลูกค้า active มากกว่า 1 คน → ไม่ใส่เพราะไม่รู้ว่าแอดมินตอบใคร
            continue  # ไม่ให้บอทตอบข้อความแอดมิน

        # ===== ลูกค้าส่งข้อความ =====
        user_id = sender_id

        if not is_bot_active(user_id):
            continue

        # ===== คำยืนยัน/ตอบรับของลูกค้า → ปิดบอท 1 ชั่วโมง ไม่ต้องตอบ =====
        if msg_type == "text":
            USER_CONFIRM_KEYWORDS = [
                "โอเคครับ", "โอเคค่ะ", "โอเค", "ok", "OK", "okay",
                "ได้ครับ", "ได้ค่ะ", "ได้เลย",
                "พิมพ์เลย", "ปริ้นเลย", "ปริ้นได้เลย", "พิมพ์ได้เลย",
                "สั่งพิมพ์", "สั่งปริ้น", "สั่งปริ้นได้เลย", "สั่งพิมพ์ได้เลย",
                "จัดเลย", "จัดได้เลย",
                "แก้ไขหน่อย", "แก้ไขได้เลย",
                "รับทราบ", "รับทราบครับ", "รับทราบค่ะ",
                "เดี๋ยวส่ง", "จะส่งให้",
            ]
            user_text = event["message"]["text"].strip()
            # เช็คแบบ exact match (ทั้งข้อความ) เพื่อไม่ให้ match คำอื่นโดยบังเอิญ
            if user_text in USER_CONFIRM_KEYWORDS:
                set_bot_closed(user_id)
                continue  # ปิดบอท ไม่ตอบ ไม่ใส่ queue

        reply_token = event.get("replyToken", "")

        if msg_type == "text":
            add_to_queue(user_id, {
                "type": "text",
                "content": event["message"]["text"]
            }, reply_token=reply_token)
        elif msg_type == "image":
            add_to_queue(user_id, {"type": "image"}, reply_token=reply_token)
        elif msg_type == "file":
            add_to_queue(user_id, {
                "type": "file",
                "filename": event["message"].get("fileName", "")
            }, reply_token=reply_token)
        elif msg_type == "sticker":
            pass  # สติ๊กเกอร์: ไม่ต้องตอบ ข้ามไปเลย
        else:
            add_to_queue(user_id, {"type": msg_type}, reply_token=reply_token)

    return "OK", 200

@app.route("/", methods=["GET"])
def index():
    return "น้องออโต้ บอทร้านบ้านเลาคำ พร้อมให้บริการค่ะ 🌿", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
