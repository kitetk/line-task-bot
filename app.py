"""
LINE Task Bot — ตามงานจ้า
ความสามารถ:
- ตอบเฉพาะข้อความที่ขึ้นต้นด้วย /
- ใช้ rule-based + AI ตีความคำสั่งภาษาธรรมชาติ
- จัดการงาน: เพิ่ม / ดู / ลบ / งานเสร็จ
- สรุปประชุม: บันทึก / ดู
- ส่งสรุปเช้า 09:30 จ-ศ อัตโนมัติ (APScheduler)
"""

import os
import re
import json
import hmac
import hashlib
import base64
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path
from datetime import datetime

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request, abort, jsonify
from openai import OpenAI
from dotenv import load_dotenv

# =========================
# Environment
# =========================
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env", override=True)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET", "").strip()
OPENAI_API_KEY            = os.getenv("OPENAI_API_KEY", "").strip()
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "tasks.db")))

print(f"[startup] LINE token={'OK' if LINE_CHANNEL_ACCESS_TOKEN else 'MISSING'}")
print(f"[startup] OpenAI key={'OK' if OPENAI_API_KEY else 'MISSING'}")
print(f"[startup] DB_PATH={DB_PATH}")

app = Flask(__name__)
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


# =========================
# Database
# =========================
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")   # ป้องกัน lock บน gunicorn
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assign_date TEXT,
            person_name TEXT,
            task_description TEXT,
            due_date TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id TEXT UNIQUE,
            target_type TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS meetings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_date TEXT UNIQUE,
            summary TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id      TEXT PRIMARY KEY,
            display_name TEXT,
            updated_at   TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS leaves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_name TEXT,
            leave_date TEXT,
            leave_type TEXT DEFAULT 'เต็มวัน',
            created_at TEXT
        )
    """)
    # migrate: เพิ่ม leave_type ให้ตารางเก่าที่ยังไม่มี column นี้
    try:
        c.execute("ALTER TABLE leaves ADD COLUMN leave_type TEXT DEFAULT 'เต็มวัน'")
        conn.commit()
        print("[migrate] added leave_type column to leaves table")
    except Exception:
        pass  # column มีอยู่แล้ว
    conn.commit()
    conn.close()


def db_save_target(target_id: str, target_type: str):
    if not target_id:
        return
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO targets (target_id, target_type, created_at) VALUES (?, ?, ?)",
        (target_id, target_type, _now())
    )
    conn.commit()
    conn.close()


def db_get_targets() -> list:
    """ส่งเฉพาะกลุ่ม ถ้าไม่มีกลุ่มค่อย fallback ส่วนตัว"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT target_id FROM targets WHERE target_type='group'")
    groups = [r[0] for r in c.fetchall()]
    if groups:
        conn.close()
        return groups
    c.execute("SELECT target_id FROM targets WHERE target_type='user'")
    users = [r[0] for r in c.fetchall()]
    conn.close()
    return users


def db_add_task(assign_date, person_name, task_description, due_date):
    conn = get_conn()
    conn.execute(
        "INSERT INTO tasks (assign_date, person_name, task_description, due_date, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (assign_date, normalize_name(person_name), task_description, due_date, _now())
    )
    conn.commit()
    conn.close()


def db_get_all_tasks():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT assign_date, person_name, task_description, due_date FROM tasks ORDER BY id ASC")
    rows = c.fetchall()
    conn.close()
    return rows


def db_delete_tasks_by_person(person_name):
    """ลบงานทั้งหมดของคนที่ชื่อตรง — ใช้ Python matching รองรับชื่อพิเศษ/emoji"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, person_name FROM tasks")
    rows = c.fetchall()
    ids_to_delete = [row[0] for row in rows if name_match(row[1], person_name)]
    n = 0
    for tid in ids_to_delete:
        c.execute("DELETE FROM tasks WHERE id = ?", (tid,))
        n += 1
    conn.commit()
    conn.close()
    return n


def db_delete_all_tasks():
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM tasks")
    n = c.rowcount
    conn.commit()
    conn.close()
    return n


def db_find_tasks_by_person_keyword(person_name: str, keyword: str) -> list:
    """ค้นหางานด้วย Python matching รองรับชื่อพิเศษ/emoji/NFC/NFD"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, assign_date, person_name, task_description, due_date FROM tasks ORDER BY id ASC")
    rows = c.fetchall()
    conn.close()
    kw = keyword.lower().strip()
    return [
        row for row in rows
        if name_match(row[2], person_name) and kw in row[3].lower()
    ]


def db_delete_task_by_id(task_id: int) -> bool:
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    ok = c.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def db_save_meeting(meeting_date: str, summary: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO meetings (meeting_date, summary, created_at) VALUES (?, ?, ?)"
        " ON CONFLICT(meeting_date) DO UPDATE SET summary=excluded.summary, created_at=excluded.created_at",
        (meeting_date, summary, _now())
    )
    conn.commit()
    conn.close()


def db_get_meeting(meeting_date: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT meeting_date, summary FROM meetings WHERE meeting_date=?", (meeting_date,))
    row = c.fetchone()
    conn.close()
    return row


def db_get_all_meetings():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT meeting_date, summary FROM meetings ORDER BY meeting_date DESC")
    rows = c.fetchall()
    conn.close()
    return rows


# =========================
# User Registry (cache ชื่อ LINE ไว้ใน DB)
# =========================
def db_upsert_user(user_id: str, display_name: str):
    """บันทึก/อัปเดตชื่อ user"""
    if not user_id or not display_name:
        return
    conn = get_conn()
    conn.execute(
        "INSERT INTO users (user_id, display_name, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name, updated_at=excluded.updated_at",
        (user_id, normalize_name(display_name), _now())
    )
    conn.commit()
    conn.close()


def db_get_user_name(user_id: str) -> str:
    """ดึงชื่อจาก registry"""
    if not user_id:
        return ""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT display_name FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else ""


def db_get_all_users() -> list:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id, display_name, updated_at FROM users ORDER BY updated_at DESC")
    rows = c.fetchall()
    conn.close()
    return rows


# =========================
# LINE Profile Helper
# =========================
def get_display_name(user_id: str, group_id: str = "", room_id: str = "") -> str:
    """
    ดึงชื่อ LINE display name:
    1. ลองดึงจาก LINE API (group → room → profile)
    2. ถ้า API fail → ดึงจาก user registry (DB cache)
    3. ถ้าได้ชื่อจาก API → อัปเดต cache อัตโนมัติ
    """
    if not user_id:
        return ""

    # ── ลองดึงจาก LINE API ────────────────────────────────────────────────
    if LINE_CHANNEL_ACCESS_TOKEN:
        headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
        endpoints = []
        if group_id:
            endpoints.append(("group", f"https://api.line.me/v2/bot/group/{group_id}/member/{user_id}"))
        if room_id:
            endpoints.append(("room",  f"https://api.line.me/v2/bot/room/{room_id}/member/{user_id}"))
        endpoints.append(("profile", f"https://api.line.me/v2/bot/profile/{user_id}"))

        for ep_type, url in endpoints:
            try:
                r = requests.get(url, headers=headers, timeout=8)
                print(f"[profile:{ep_type}] status={r.status_code} content-type={r.headers.get('content-type','?')}")

                if r.status_code == 200:
                    # บังคับ decode UTF-8 ตรงๆ — ป้องกัน requests ตีความ encoding ผิด
                    raw_bytes = r.content
                    try:
                        data = json.loads(raw_bytes.decode("utf-8"))
                    except Exception:
                        data = r.json()

                    raw  = data.get("displayName", "")
                    name = normalize_name(raw)

                    # log codepoints เพื่อ debug ชื่อพิเศษ
                    cps = " ".join(f"U+{ord(c):04X}" for c in name)
                    print(f"[profile:{ep_type}] raw_bytes={raw_bytes!r}")
                    print(f"[profile:{ep_type}] raw={raw!r}  name={name!r}  codepoints={cps}")

                    if name:
                        db_upsert_user(user_id, name)
                    return name

                # 403 = no permission for this endpoint, ลอง endpoint ถัดไป
                print(f"[profile:{ep_type}] skip ({r.status_code}): {r.text[:80]}")
            except Exception as e:
                print(f"[profile:{ep_type}] exception: {e}")

    # ── Fallback: ดึงจาก DB cache ─────────────────────────────────────────
    cached = db_get_user_name(user_id)
    if cached:
        print(f"[profile] cache hit: {cached!r}")
    else:
        print(f"[profile] no name found for {user_id[:8]}...")
    return cached


# =========================
# Leave DB Functions
# =========================
def db_add_leave(person_name: str, leave_date: str, leave_type: str = "เต็มวัน"):
    conn = get_conn()
    conn.execute(
        "INSERT INTO leaves (person_name, leave_date, leave_type, created_at) VALUES (?, ?, ?, ?)",
        (normalize_name(person_name), leave_date, leave_type, _now())
    )
    conn.commit()
    conn.close()


def db_get_all_leaves() -> list:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, person_name, leave_date, leave_type FROM leaves ORDER BY leave_date ASC")
    rows = c.fetchall()
    conn.close()
    return rows


def db_get_leaves_by_date(date_str: str) -> list:
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT id, person_name, leave_date, leave_type FROM leaves WHERE leave_date = ?",
        (date_str,)
    )
    rows = c.fetchall()
    conn.close()
    return rows


def db_delete_leave_by_person(person_name: str) -> int:
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM leaves WHERE person_name LIKE ?", (f"%{normalize_name(person_name)}%",))
    n = c.rowcount
    conn.commit()
    conn.close()
    return n


def db_delete_expired_leaves():
    """ลบวันลาที่ผ่านมาแล้ว (วันที่ < วันนี้)"""
    today = datetime.now(pytz.timezone("Asia/Bangkok")).date()
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, leave_date FROM leaves")
    rows = c.fetchall()
    deleted = 0
    for lid, ldate_str in rows:
        try:
            parts = ldate_str.split("/")
            d = datetime(int(parts[2]), int(parts[1]), int(parts[0])).date()
            if d < today:
                c.execute("DELETE FROM leaves WHERE id = ?", (lid,))
                deleted += 1
        except Exception:
            pass
    conn.commit()
    conn.close()
    print(f"[leave cleanup] deleted {deleted} expired leave(s)")


# =========================
# LINE API
# =========================
def verify_signature(body: bytes, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET:
        return False
    h = hmac.new(LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256)
    return hmac.compare_digest(base64.b64encode(h.digest()).decode(), signature)


def reply_message(reply_token: str, text: str):
    try:
        r = requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
            json={"replyToken": reply_token,
                  "messages": [{"type": "text", "text": str(text)[:5000]}]},
            timeout=10
        )
        print(f"[reply] {r.status_code}")
    except Exception as e:
        print(f"[reply Error] {e}")


def push_message(to: str, text: str):
    try:
        r = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
            json={"to": to,
                  "messages": [{"type": "text", "text": str(text)[:5000]}]},
            timeout=10
        )
        print(f"[push] {r.status_code}")
    except Exception as e:
        print(f"[push Error] {e}")


# =========================
# Utilities
# =========================
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return datetime.now().strftime("%d/%m/%Y")


def normalize_name(name: str) -> str:
    """
    Normalize ชื่อ LINE ให้พร้อมใช้งาน:
    1. NFC normalization — แก้ fërň NFD vs NFC ไม่ตรงกัน
    2. รองรับ emoji ในชื่อ เช่น 🌟kite, fërň🌸 (ไม่ตัดออก เก็บไว้ครบ)
    3. strip whitespace
    ใช้ทุกที่ที่เกี่ยวกับชื่อ: บันทึก / ค้นหา / แสดงผล
    """
    if not name:
        return name
    # NFC รวม combining characters เข้ากับ base character
    name = unicodedata.normalize("NFC", name)
    # strip leading/trailing whitespace และ zero-width characters
    name = name.strip().strip("\u200b\u200c\u200d\ufeff")
    return name


def name_match(stored: str, query: str) -> bool:
    """เปรียบเทียบชื่อแบบ case-insensitive + NFC-normalized"""
    return normalize_name(query).lower() in normalize_name(stored).lower()


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def format_task_description(desc: str) -> str:
    """แยก 1) 2) 3) ขึ้นบรรทัดใหม่ถ้ามี"""
    parts = re.split(r'(?=\d+\))', desc.strip())
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) <= 1:
        return desc.strip()
    return "\n   ".join(parts)


def extract_json_object(text: str):
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


def extract_json_array(text: str):
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\[[\s\S]*\]", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


# =========================
# Rule-based Classifier
# =========================
# =========================
# Leave Date Parser  (รองรับหลายวัน + ช่วงวัน + เดือนภาษาไทย)
# =========================
THAI_MONTHS = {
    "มกรา": 1,  "มกราคม": 1,
    "กุมภา": 2,  "กุมภาพันธ์": 2,
    "มีนา": 3,   "มีนาคม": 3,
    "เมษา": 4,   "เมษายน": 4,
    "พฤษภา": 5,  "พฤษภาคม": 5,
    "มิถุนา": 6, "มิถุนายน": 6,
    "กรกฎา": 7,  "กรกฎาคม": 7,
    "สิงหา": 8,  "สิงหาคม": 8,
    "กันยา": 9,  "กันยายน": 9,
    "ตุลา": 10,  "ตุลาคม": 10,
    "พฤศจิกา": 11, "พฤศจิกายน": 11,
    "ธันวา": 12, "ธันวาคม": 12,
}


def _year_to_ce(year_raw: str | None) -> int:
    """แปลงปีเป็น ค.ศ. รองรับ พ.ศ. 2/4 หลัก"""
    now = datetime.now(pytz.timezone("Asia/Bangkok"))
    if year_raw is None:
        return now.year
    y = int(year_raw)
    if len(str(year_raw)) <= 2:
        y += 2500      # 2 หลัก → พ.ศ. 25xx
    if y >= 2500:
        y -= 543       # พ.ศ. → ค.ศ.
    return y


def parse_leave_date(raw: str) -> str | None:
    """
    DD/MM/YYYY, DD/MM/YY (พ.ศ.), DD/MM/BBBB (พ.ศ.), DD/MM
    คืน DD/MM/YYYY (ค.ศ.) หรือ None
    """
    raw = raw.strip().replace("-", "/")
    m = re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$", raw)
    if not m:
        return None
    day, mon = int(m.group(1)), int(m.group(2))
    year_ce = _year_to_ce(m.group(3))
    try:
        return datetime(year_ce, mon, day).strftime("%d/%m/%Y")
    except ValueError:
        return None


def _expand_days(day_part: str) -> list[int]:
    """
    แปลง day_part → list ของวันที่
    รองรับ: "12"  "12 13 14"  "12-14"  "12,13,14"
    """
    day_part = day_part.strip()
    # ช่วงวัน เช่น 12-14
    m_range = re.match(r"^(\d{1,2})\s*[-–]\s*(\d{1,2})$", day_part)
    if m_range:
        s, e = int(m_range.group(1)), int(m_range.group(2))
        return list(range(s, e + 1)) if s <= e else []
    # หลายวันคั่นด้วย space หรือ comma
    nums = re.findall(r"\d{1,2}", day_part)
    return [int(n) for n in nums] if nums else []


def _extract_leave_type(content: str) -> tuple[str, str]:
    """
    ดึง leave_type จาก content แล้วคืน (content_cleaned, leave_type)
    รองรับ: 'เช้า' / 'ครึ่งเช้า' → ครึ่งเช้า
             'บ่าย' / 'ครึ่งบ่าย' → ครึ่งบ่าย
             ไม่มี             → เต็มวัน
    """
    for kw, ltype in [("ครึ่งเช้า", "ครึ่งเช้า"), ("ครึ่งบ่าย", "ครึ่งบ่าย"),
                      ("เช้า", "ครึ่งเช้า"), ("บ่าย", "ครึ่งบ่าย")]:
        if kw in content:
            cleaned = content.replace(kw, "").strip()
            cleaned = re.sub(r"\s{2,}", " ", cleaned)
            return cleaned, ltype
    return content, "เต็มวัน"


def parse_leave_input(content: str) -> tuple[str, list[str], str] | None:
    """
    แยก ชื่อ + รายการวันลา จาก content หลัง /ลา
    รองรับ:
      "ตะไค้ 27/4/69"              → 1 วัน (เต็มวัน)
      "ตะไค้ เช้า 27/4/69"         → ครึ่งเช้า
      "ตะไค้ บ่าย 12-14 พฤษภา"    → ครึ่งบ่าย 3 วัน
      "ตะไค้ 12 13 14 พฤษภา 69"   → 3 วัน
      "ตะไค้ 12/5 13/5 14/5"       → 3 วัน
    คืน (person_name, [date_str, ...], leave_type) หรือ None
    """
    content = content.strip()
    # ดึง leave_type ออกก่อนแล้วค่อย parse ชื่อ/วัน
    content, leave_type = _extract_leave_type(content)
    now = datetime.now(pytz.timezone("Asia/Bangkok"))

    # ── รูปแบบ 1: มีชื่อเดือนภาษาไทย ───────────────────────────────────────
    # ค้นหาชื่อเดือนไทยใน content
    thai_month_found = None
    month_pos = None
    for th_name in sorted(THAI_MONTHS.keys(), key=len, reverse=True):
        idx = content.find(th_name)
        if idx != -1:
            thai_month_found = THAI_MONTHS[th_name]
            month_pos = idx
            month_end = idx + len(th_name)
            break

    if thai_month_found is not None:
        before_month = content[:month_pos].strip()   # "ตะไค้ 12 13 14" หรือ "ตะไค้ 12-14"
        after_month  = content[month_end:].strip()   # "69" หรือ ""

        # แยกชื่อออกจาก day_part
        # ชื่อคือส่วนที่ไม่ใช่ตัวเลข/ขีด ก่อนตัวเลขแรก
        m_name = re.match(r"^([^\d]+?)\s+([\d\s\-–,]+)$", before_month)
        if m_name:
            person    = m_name.group(1).strip()
            day_part  = m_name.group(2).strip()
        else:
            # อาจเป็นแค่ชื่อ (ไม่มีวันก่อนเดือน) — ลองหาวันหลังเดือน
            person = before_month
            day_part = ""

        # หาปี จาก after_month
        year_m = re.search(r"\b(\d{2,4})\b", after_month)
        year_ce = _year_to_ce(year_m.group(1) if year_m else None)
        mon = thai_month_found

        # ถ้า day_part ว่าง ลองหาตัวเลขใน after_month ก่อนปี
        if not day_part.strip():
            nums_after = re.findall(r"\d{1,2}", after_month)
            if nums_after and (not year_m or nums_after[0] != year_m.group(1)):
                day_part = nums_after[0]

        days = _expand_days(day_part)
        if not person or not days:
            return None

        dates = []
        for d in days:
            try:
                dates.append(datetime(year_ce, mon, d).strftime("%d/%m/%Y"))
            except ValueError:
                pass
        return (person, dates, leave_type) if dates else None

    # ── รูปแบบ 2: DD/MM(/YY/YYYY) หลายชุด ─────────────────────────────────
    # หา pattern วันที่ทุกอัน
    date_matches = list(re.finditer(r"\b(\d{1,2}[/\-]\d{1,2}(?:[/\-]\d{2,4})?)\b", content))
    if date_matches:
        # ชื่อ = ทุกอย่างก่อน pattern วันที่แรก
        person = content[:date_matches[0].start()].strip()
        if not person:
            return None
        dates = []
        for dm in date_matches:
            d = parse_leave_date(dm.group(1))
            if d:
                dates.append(d)
        return (person, dates, leave_type) if dates else None

    return None


def classify_command_fallback(text: str) -> dict:
    t = text.strip()
    lo = t.lower()

    if lo in ["/help", "/วิธีใช้", "/ช่วยเหลือ", "/คำสั่ง"]:
        return {"intent": "help", "content": ""}

    for p in ["/เพิ่มงาน", "/เพิ่ม", "/add", "/บันทึกงาน", "/มอบหมาย"]:
        if lo.startswith(p):
            return {"intent": "add_task", "content": t[len(p):].strip()}

    if lo in ["/งานทั้งหมด", "/tasks", "/ดูงาน", "/รายการงาน", "/สรุปงาน", "/list"]:
        return {"intent": "list_tasks", "content": ""}

    if lo.startswith("/งานของ"):
        return {"intent": "list_tasks", "content": t[len("/งานของ"):].strip()}

    # ─── Leave commands (ต้องอยู่ก่อน /ลบ เพื่อป้องกัน /ลบวันลา ชน /ลบ) ──────
    for p in ["/ลบวันลา", "/ยกเลิกลา", "/ยกเลิกวันลา"]:
        if lo.startswith(p):
            return {"intent": "delete_leave", "content": t[len(p):].strip()}

    if lo in ["/ดูวันลา", "/วันลา", "/รายการลา", "/ลาทั้งหมด"]:
        return {"intent": "list_leaves", "content": ""}

    # /ลาเช้า และ /ลาบ่าย เป็น shortcut — prepend keyword ให้ parser
    for p, kw in [("/ลาบ่ายครึ่ง", "ครึ่งบ่าย"), ("/ลาเช้าครึ่ง", "ครึ่งเช้า"),
                  ("/ลาบ่าย", "บ่าย"), ("/ลาเช้า", "เช้า")]:
        if lo.startswith(p):
            return {"intent": "add_leave", "content": kw + " " + t[len(p):].strip()}

    for p in ["/แจ้งลา", "/ลางาน", "/ลา"]:
        if lo.startswith(p):
            return {"intent": "add_leave", "content": t[len(p):].strip()}

    # ─── Task commands ────────────────────────────────────────────────────────
    if lo in ["/ล้างงานทั้งหมด", "/ล้างทั้งหมด", "/ล้าง", "/deleteall"]:
        return {"intent": "delete_all", "content": ""}

    for p in ["/ลบงาน", "/ลบ", "/delete"]:
        if lo.startswith(p):
            return {"intent": "delete_task", "content": t[len(p):].strip()}

    for p in ["/เสร็จแล้ว", "/งานเสร็จ", "/เสร็จ", "/done"]:
        if lo.startswith(p):
            return {"intent": "complete_task", "content": t[len(p):].strip()}

    for p in ["/สรุปประชุม", "/ประชุม", "/meeting"]:
        if lo.startswith(p):
            return {"intent": "meeting_summary", "content": t[len(p):].strip()}

    if lo in ["/ตั้งกลุ่มหลัก", "/setgroup", "/ตั้งกลุ่ม"]:
        return {"intent": "set_group", "content": ""}

    if lo in ["/whoami", "/ฉันคือ", "/ชื่อของฉัน"]:
        return {"intent": "whoami", "content": ""}

    for p in ["/ลงทะเบียน", "/ชื่อฉัน", "/ฉัน", "/myname"]:
        if lo.startswith(p):
            return {"intent": "register_name", "content": t[len(p):].strip()}

    return {"intent": "query", "content": t[1:].strip()}


def classify_command(text: str) -> dict:
    result = classify_command_fallback(text)
    if result["intent"] != "query":
        print(f"[classify] fast → {result['intent']}")
        return result
    # query เท่านั้นที่ส่งให้ AI ตีความเพิ่ม
    if not openai_client:
        return result
    try:
        ai = _classify_with_ai(text)
        if ai and ai.get("intent"):
            print(f"[classify] AI → {ai['intent']}")
            return ai
    except Exception as e:
        print(f"[classify] AI skip: {e}")
    return result


def _classify_with_ai(text: str) -> dict:
    prompt = f"""คุณคือ command classifier บอทจัดการงานทีม
ตอบเป็น JSON เท่านั้น รูปแบบ: {{"intent":"<intent>","content":"<content>"}}

intent ที่รองรับ:
- add_task    = เพิ่ม/มอบหมายงาน (content = ข้อมูลงาน)
- list_tasks  = ดูงาน (content = ชื่อคน หรือ "")
- delete_task = ลบงานของคนนั้น (content = ชื่อคน)
- delete_all  = ล้างงานทั้งหมด (content = "")
- query       = ถามเกี่ยวกับงาน (content = คำถาม)
- help        = ขอความช่วยเหลือ (content = "")

วันนี้: {today_str()}
ข้อความ: {text}"""

    response = openai_client.responses.create(model="gpt-4.1-mini", input=prompt)
    raw = (response.output_text or "").strip()
    parsed = extract_json_object(raw)
    if parsed and "intent" in parsed:
        return {"intent": str(parsed["intent"]), "content": str(parsed.get("content", ""))}
    return classify_command_fallback(text)


# =========================
# Task Parsing
# =========================
TASK_VERBS = [
    "ทำสไลด์", "ทำรายงาน", "ทำเอกสาร", "ทำสรุป", "ทำเพรส", "ทำแพลน",
    "ทำ", "จัด", "เขียน", "สรุป", "เตรียม", "แก้ไข", "แก้", "อัปเดต",
    "ออกแบบ", "ดีไซน์", "ตรวจ", "นำเสนอ",
]
_VERB_PAT = "|".join(sorted(TASK_VERBS, key=len, reverse=True))


def smart_preprocess(raw: str) -> str:
    raw = re.sub(r'(ส่ง)(\d)', r'\1 \2', raw)
    for verb in sorted(TASK_VERBS, key=len, reverse=True):
        raw = re.sub(rf'([ก-๙]+)({re.escape(verb)})', rf'\1 \2', raw)
    return normalize_spaces(raw)


def parse_task_simple(raw: str):
    try:
        raw = smart_preprocess(raw)
        parts = raw.split()
        if len(parts) < 2:
            return None

        send_idx = -1
        due_date = "ไม่ระบุ"
        main_parts = parts

        for i, p in enumerate(parts):
            if p == "ส่ง" and i + 1 < len(parts):
                due_date = parts[i + 1]
                main_parts = parts[:i]
                send_idx = i
                break
            elif p.startswith("ส่ง") and len(p) > 2:
                due_date = p[2:].strip() or "ไม่ระบุ"
                main_parts = parts[:i]
                send_idx = i
                break

        if len(main_parts) < 2:
            return None

        if re.match(r"^\d{1,2}[/\-]\d{1,2}", main_parts[0]):
            assign_date = main_parts[0]
            person_name = main_parts[1] if len(main_parts) > 1 else "ไม่ระบุ"
            task_description = " ".join(main_parts[2:]) if len(main_parts) > 2 else "ไม่ระบุ"
        else:
            assign_date = today_str()
            person_name = main_parts[0]
            task_description = " ".join(main_parts[1:])

        if not person_name.strip() or not task_description.strip():
            return None

        return {
            "assign_date": assign_date,
            "person_name": normalize_name(person_name.strip()),
            "task_description": task_description.strip(),
            "due_date": due_date.strip() or "ไม่ระบุ",
        }
    except Exception as e:
        print(f"[parse_simple Error] {e}")
        return None


def split_multi_tasks_fallback(raw: str) -> list:
    """แยกหลายงานเฉพาะเมื่อมี , คั่นเท่านั้น เช่น บาส ทำสไลด์, มะนาว เขียนสรุป"""
    # ถ้าไม่มีจุลภาค ไม่แยก — ป้องกันตัดงานยาวเป็นชิ้น
    if "," not in raw:
        return []

    raw = smart_preprocess(raw)

    assign_date = today_str()
    m = re.match(r"^(\d{1,2}[/\-]\d{1,2}(?:[/\-]\d{2,4})?)\s+", raw)
    if m:
        assign_date = m.group(1)
        raw = raw[m.end():].strip()

    # หา global due จาก segment สุดท้าย
    all_dues = list(re.finditer(r"ส่ง\s+(\S+)", raw))
    global_due = all_dues[-1].group(1) if all_dues else "ไม่ระบุ"

    segments = [s.strip() for s in raw.split(",") if s.strip()]
    if len(segments) < 2:
        return []

    results = []
    for seg in segments:
        task = parse_task_simple(seg)
        if task:
            if task["due_date"] == "ไม่ระบุ":
                task["due_date"] = global_due
            task["assign_date"] = assign_date
            results.append(task)

    return results if len(results) >= 2 else []


def parse_tasks_with_ai(raw: str) -> list:
    if not openai_client:
        return []
    prompt = f"""คุณคือ parser ข้อความมอบหมายงานภาษาไทย
ตอบเป็น JSON array เท่านั้น ห้ามมีคำอธิบาย

รูปแบบแต่ละ object:
{{"assign_date":"DD/MM/YYYY","person_name":"ชื่อ","task_description":"งาน","due_date":"DD/MM/YYYY หรือ ไม่ระบุ"}}

กติกา:
- ถ้าไม่มี assign_date ใช้ "{today_str()}"
- ถ้าไม่มี due_date ใช้ "ไม่ระบุ"
- แยกเป็นหลาย object เฉพาะเมื่อมีจุลภาค , คั่นระหว่างงาน เช่น "บาส ทำสไลด์, มะนาว เขียนสรุป"
- ถ้าไม่มีจุลภาค ให้ถือว่าเป็นงานเดียว แม้ข้อความจะยาวหรือมีคำกริยาหลายคำ
- "ตะไคร้ทำสไลด์" → person="ตะไคร้", task="ทำสไลด์"
- แปลง "พรุ่งนี้"/"วันนี้" เป็น DD/MM/YYYY
- ถ้าอ่านไม่ได้เลย ตอบ []

ข้อความ:
{raw}"""

    try:
        response = openai_client.responses.create(model="gpt-4.1-mini", input=prompt)
        raw_out = (response.output_text or "").strip()
        parsed = extract_json_array(raw_out)
        if not isinstance(parsed, list):
            return []
        result = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            person = str(item.get("person_name", "")).strip()
            task   = str(item.get("task_description", "")).strip()
            if not person or not task:
                continue
            result.append({
                "assign_date": str(item.get("assign_date", today_str())).strip() or today_str(),
                "person_name": person,
                "task_description": task,
                "due_date": str(item.get("due_date", "ไม่ระบุ")).strip() or "ไม่ระบุ",
            })
        return result
    except Exception as e:
        print(f"[AI Parser Error] {e}")
        return []


def parse_tasks(raw: str) -> list:
    multi = split_multi_tasks_fallback(raw)
    if multi:
        return multi
    single = parse_task_simple(raw)
    if single:
        return [single]
    return parse_tasks_with_ai(raw)


# =========================
# AI Answering
# =========================
def build_tasks_text(tasks_data: list) -> str:
    if not tasks_data:
        return "ยังไม่มีข้อมูลงานในระบบ"
    lines = []
    for assign_date, person, task, due in tasks_data:
        lines.append(f"- {person}: {task} (มอบหมาย {assign_date}, ส่ง {due})")
    return "\n".join(lines)


def answer_from_db_only(question: str, tasks_data: list) -> str:
    if not tasks_data:
        return "ยังไม่มีข้อมูลงานในระบบ"
    q = question.lower()
    matched = [f"{p}: {t} | ส่ง {d}" for _, p, t, d in tasks_data if p.lower() in q]
    if matched:
        return "งานที่พบ:\n" + "\n".join(matched)
    lines = ["งานทั้งหมดในระบบ:"]
    for i, (_, p, t, d) in enumerate(tasks_data[:20], 1):
        lines.append(f"{i}. {p} — {t} | ส่ง {d}")
    return "\n".join(lines)[:5000]


def answer_question(question: str, tasks_data: list) -> str:
    tasks_text = build_tasks_text(tasks_data)
    if not openai_client:
        return answer_from_db_only(question, tasks_data)
    try:
        response = openai_client.responses.create(
            model="gpt-4.1-mini",
            instructions=(
                "คุณเป็น AI ผู้ช่วยทีม ตอบจากข้อมูลงานที่ได้รับเท่านั้น "
                "ตอบเป็นภาษาไทย กระชับ ชัดเจน ถ้าไม่มีข้อมูลบอกว่าไม่พบ"
            ),
            input=f"ข้อมูลงาน:\n{tasks_text}\n\nคำถาม: {question}"
        )
        answer = (response.output_text or "").strip()
        return answer if answer else answer_from_db_only(question, tasks_data)
    except Exception as e:
        print(f"[OpenAI Error] {e}")
        return answer_from_db_only(question, tasks_data)


# =========================
# Command Handlers
# =========================
HELP_TEXT = """คำสั่งที่ใช้ได้ (พิมพ์ / นำหน้าเสมอ)

📋 งาน:
/เพิ่มงาน ตะไคร้ ทำสไลด์ ส่ง 5/4/2026
/เพิ่มงาน บาส ทำรายงาน, มะนาว เขียนสรุป ส่ง 7/4
/งานทั้งหมด
/งานของตะไคร้
/เสร็จ ตะไคร้ สไลด์
/ลบงาน ตะไคร้
/ล้างงานทั้งหมด

🏖️ วันลา:
/ลา ตะไค้ 27/4/69          ← เต็มวัน
/ลาเช้า ตะไค้ 27/4/69      ← ครึ่งเช้า
/ลาบ่าย ตะไค้ 27/4/69      ← ครึ่งบ่าย
/ลา ตะไค้ 12-14 พฤษภา 69  ← ช่วงวัน (range)
/ลา ตะไค้ 12 13 14 พฤษภา  ← หลายวัน
/ดูวันลา                    ← ดูวันลาทั้งหมด
/ลบวันลา ตะไค้              ← ลบ/แก้ไขวันลา

📅 สรุปประชุม:
/สรุปประชุม 3/4/2026 เนื้อหา...  ← บันทึก
/สรุปประชุม 3/4/2026  ← ดูวันนั้น
/สรุปประชุม  ← ดูทั้งหมด

⚙️ ตั้งค่า:
/ตั้งกลุ่มหลัก  ← บอทส่งสรุปเช้าเข้ากลุ่มนี้
/ฉัน fërň      ← ลงทะเบียนชื่อ (สำหรับชื่อพิเศษ/emoji ที่บอทอ่านไม่ได้)
/help"""


def _format_task_line(i: int, person: str, task: str, due: str) -> str:
    due_label = due if due and due != "ไม่ระบุ" else "ไม่ระบุ"
    formatted = format_task_description(task)
    return f"{i}. {person}\n   งาน: {formatted}\n   กำหนดส่ง: {due_label}"


def handle_add_task(content: str, sender_name: str = "") -> str:
    """
    sender_name: display name ของคนส่ง (ดึงจาก LINE API)
    ถ้า parse ไม่เจอชื่อ และมี sender_name → ใส่ชื่อ sender แทน
    """
    if not content.strip():
        return ("กรุณาระบุงาน\n"
                "ตัวอย่าง:\n"
                "  /เพิ่มงาน ทำสไลด์ ส่ง 5/4       ← ใช้ชื่อ LINE ของคุณอัตโนมัติ\n"
                "  /เพิ่มงาน บาส ทำสไลด์ ส่ง 5/4   ← ระบุชื่อเองได้")

    # ลองดึงชื่อจาก content ก่อน
    parsed = parse_tasks(content)

    # ถ้า parse ได้ 1 งาน แต่ person_name เป็นคำที่ดูเหมือน task verb (ไม่ใช่ชื่อ)
    # → เติม sender_name นำหน้าแล้ว parse ใหม่
    if not parsed and sender_name:
        parsed = parse_tasks(f"{sender_name} {content}")
    elif parsed and sender_name:
        # ถ้า person_name ดูเหมือน task verb (ขึ้นต้นด้วย ทำ/เขียน/สรุป/เตรียม ฯลฯ)
        first_person = parsed[0]["person_name"]
        task_verbs_start = ("ทำ","เขียน","สรุป","เตรียม","แก้","ออกแบบ","ดีไซน์",
                            "ตรวจ","นำเสนอ","จัด","อัปเดต","ส่ง","update","make",
                            "write","prepare","design","fix","check","review")
        if any(first_person.startswith(v) for v in task_verbs_start):
            # person_name คือ task จริงๆ — reparse โดยใส่ sender ข้างหน้า
            reparsed = parse_tasks(f"{sender_name} {content}")
            if reparsed:
                parsed = reparsed

    if not parsed:
        return "ไม่สามารถอ่านข้อมูลได้\nลองพิมพ์ชื่องานให้ชัดขึ้น เช่น  /เพิ่มงาน ทำสไลด์ ส่ง 5/4"

    for item in parsed:
        db_add_task(item["assign_date"], item["person_name"],
                    item["task_description"], item["due_date"])

    auto_label = " (ชื่อจาก LINE)" if sender_name and parsed[0]["person_name"] == sender_name else ""
    if len(parsed) == 1:
        t = parsed[0]
        return (f"✅ บันทึกงานสำเร็จ!\n"
                f"ชื่อ: {t['person_name']}{auto_label}\n"
                f"งาน: {t['task_description']}\n"
                f"ส่ง: {t['due_date']}")

    lines = [f"✅ บันทึกงานสำเร็จ {len(parsed)} รายการ"]
    for i, t in enumerate(parsed, 1):
        lines.append(f"{i}. {t['person_name']} — {t['task_description']}  (ส่ง: {t['due_date']})")
    return "\n".join(lines)[:5000]


def handle_list_tasks(person_filter: str = "") -> str:
    tasks = db_get_all_tasks()
    if not tasks:
        return "ยังไม่มีงานในระบบ\nใช้ /เพิ่มงาน เพื่อเพิ่มงาน"

    if person_filter:
        tasks = [t for t in tasks if name_match(t[1], person_filter)]
        if not tasks:
            return f"ไม่พบงานของ '{person_filter}'"

    header = f"งาน{'ของ ' + person_filter if person_filter else 'ทั้งหมด'}ในระบบ:"
    lines = [header]
    for i, (assign_date, person, task, due) in enumerate(tasks, 1):
        lines.append(_format_task_line(i, person, task, due))
    return "\n\n".join(lines)[:5000]


def handle_complete_task(content: str) -> str:
    parts = content.strip().split()
    if len(parts) < 2:
        return "กรุณาระบุชื่อและ keyword ของงาน\nตัวอย่าง: /เสร็จ ตะไคร้ สไลด์"

    person_name = parts[0]
    keyword     = " ".join(parts[1:])
    matches     = db_find_tasks_by_person_keyword(person_name, keyword)

    if not matches:
        return (f"❌ ไม่พบงานของ '{person_name}' ที่มีคำว่า '{keyword}'\n"
                f"พิมพ์ /งานของ{person_name} เพื่อดูรายการงานทั้งหมด")

    if len(matches) == 1:
        tid, _, person, task, due = matches[0]
        db_delete_task_by_id(tid)
        return f"✅ งานเสร็จแล้ว! ลบออกจากระบบเรียบร้อย\n{person} — {task}"

    lines = [f"พบ {len(matches)} งานที่ตรงกัน กรุณาระบุ keyword ให้ชัดขึ้น:"]
    for i, (tid, _, person, task, due) in enumerate(matches, 1):
        lines.append(f"{i}. {person} — {task}  (ส่ง: {due})")
    lines.append(f"\nตัวอย่าง: /เสร็จ {person_name} [keyword เฉพาะของงาน]")
    return "\n".join(lines)


def handle_delete_task(person_name: str) -> str:
    if not person_name.strip():
        return "กรุณาระบุชื่อ\nตัวอย่าง: /ลบงาน ตะไคร้"
    n = db_delete_tasks_by_person(person_name.strip())
    if n > 0:
        return f"ลบงานของ '{person_name.strip()}' สำเร็จ ({n} รายการ)"
    return f"ไม่พบงานของ '{person_name.strip()}'"


def handle_delete_all() -> str:
    n = db_delete_all_tasks()
    return f"ล้างงานทั้งหมดสำเร็จ ({n} รายการ)"


def handle_meeting_summary(content: str) -> str:
    content = content.strip()

    if not content:
        meetings = db_get_all_meetings()
        if not meetings:
            return "ยังไม่มีสรุปประชุมในระบบ\nใช้ /สรุปประชุม [วันที่] [เนื้อหา] เพื่อบันทึก"
        lines = ["📋 สรุปประชุมทั้งหมด:"]
        for date, summary in meetings:
            lines.append(f"\n📅 {date}\n{summary}\n{'—'*20}")
        return "\n".join(lines)[:5000]

    m = re.match(r"^(\d{1,2}[/\-]\d{1,2}(?:[/\-]\d{2,4})?)\s*([\s\S]*)", content)
    if not m:
        return "กรุณาระบุวันที่ก่อน\nตัวอย่าง: /สรุปประชุม 3/4/2026 เนื้อหา..."

    meeting_date = m.group(1).strip()
    summary_text = m.group(2).strip()

    if not summary_text:
        row = db_get_meeting(meeting_date)
        if not row:
            return f"ไม่พบสรุปประชุมวันที่ {meeting_date}"
        return f"📋 สรุปประชุมวันที่ {row[0]}\n\n{row[1]}"

    db_save_meeting(meeting_date, summary_text)
    return f"✅ บันทึกสรุปประชุมสำเร็จ!\nวันที่: {meeting_date}\n\n{summary_text}"


# =========================
# Leave Handlers
# =========================
def handle_add_leave(content: str, sender_name: str = "") -> str:
    """
    ถ้าไม่พิมชื่อ → ใช้ sender_name (ชื่อ LINE ของคนส่ง) อัตโนมัติ
    """
    if not content.strip():
        return ("กรุณาระบุวันลา\n"
                "ตัวอย่าง:\n"
                "  /ลา 27/4/69                 ← ใช้ชื่อ LINE ของคุณ\n"
                "  /ลาเช้า 27/4/69             ← ครึ่งเช้า\n"
                "  /ลาบ่าย 27/4/69             ← ครึ่งบ่าย\n"
                "  /ลา 12-14 พฤษภา 69         ← หลายวัน\n"
                "  /ลา ตะไค้ 27/4/69           ← ระบุชื่อเองได้")
    result = parse_leave_input(content)

    # ถ้า parse ไม่เจอชื่อ และมี sender_name → เติมชื่อ sender นำหน้า
    if not result and sender_name:
        result = parse_leave_input(f"{sender_name} {content}")

    if not result:
        return ("ไม่สามารถอ่านวันที่ได้\nตัวอย่าง:\n"
                "  /ลา 27/4/69\n"
                "  /ลาเช้า 27/4/69\n"
                "  /ลา 12-14 พฤษภา 69")
    person, dates, leave_type = result
    for d in dates:
        db_add_leave(person, d, leave_type)

    type_label = f"  ({leave_type})" if leave_type != "เต็มวัน" else ""
    if len(dates) == 1:
        return (f"✅ บันทึกวันลาสำเร็จ!\n"
                f"ชื่อ: {person}\n"
                f"วันที่ลา: {dates[0]}{type_label}\n"
                f"ระบบแจ้งเตือนวันนั้น และลบอัตโนมัติเมื่อสิ้นวัน")

    day_list = "\n".join(f"  • {d}" for d in dates)
    return (f"✅ บันทึกวันลาสำเร็จ {len(dates)} วัน!{type_label}\n"
            f"ชื่อ: {person}\n"
            f"วันที่ลา:\n{day_list}\n"
            f"ระบบแจ้งเตือนแต่ละวัน และลบอัตโนมัติเมื่อสิ้นวัน")


def handle_list_leaves() -> str:
    leaves = db_get_all_leaves()
    if not leaves:
        return "ยังไม่มีการแจ้งลาในระบบ\nใช้ /ลา [ชื่อ] [วันที่] เพื่อแจ้งลา"
    # จัดกลุ่มตามชื่อคน
    grouped: dict = {}
    for _, person, leave_date, leave_type in leaves:
        grouped.setdefault(person, []).append((leave_date, leave_type))
    lines = ["🏖️ รายการวันลาทั้งหมด:"]
    for person, entries in grouped.items():
        if len(entries) == 1:
            d, lt = entries[0]
            suffix = f" ({lt})" if lt != "เต็มวัน" else ""
            lines.append(f"• {person}  —  {d}{suffix}")
        else:
            # แสดงแต่ละวันพร้อม type
            dates_str = ", ".join(
                f"{d}" + (f"({lt})" if lt != "เต็มวัน" else "")
                for d, lt in entries
            )
            lines.append(f"• {person}  —  {dates_str}  ({len(entries)} วัน)")
    return "\n".join(lines)


def handle_register_name(user_id: str, name: str) -> str:
    """
    /ฉัน [ชื่อ]  — ลงทะเบียนชื่อ LINE ไว้ในระบบ
    ใช้เมื่อ bot อ่านชื่อจาก API ไม่ได้ (ชื่อพิเศษ/emoji)
    """
    name = normalize_name(name.strip())
    if not name:
        cached = db_get_user_name(user_id)
        if cached:
            return f"ชื่อที่ลงทะเบียนไว้: {cached}"
        return ("กรุณาระบุชื่อ\nตัวอย่าง: /ฉัน fërň\n"
                "บอทจะจำชื่อนี้ไว้ใช้กับทุก command อัตโนมัติ")
    db_upsert_user(user_id, name)
    return (f"✅ ลงทะเบียนชื่อสำเร็จ!\n"
            f"ชื่อ: {name}\n"
            f"บอทจะใช้ชื่อนี้กับ /เพิ่มงาน /ลา ฯลฯ อัตโนมัติ")


def handle_delete_leave(person_name: str) -> str:
    if not person_name.strip():
        return "กรุณาระบุชื่อ\nตัวอย่าง: /ลบวันลา ตะไค้"
    n = db_delete_leave_by_person(person_name.strip())
    if n > 0:
        return f"✅ ลบวันลาของ '{person_name.strip()}' สำเร็จ ({n} รายการ)"
    return f"ไม่พบวันลาของ '{person_name.strip()}'"


# =========================
# Morning Summary
# =========================
def send_daily_summary():
    now = datetime.now(pytz.timezone("Asia/Bangkok"))
    if now.weekday() > 4:   # เสาร์-อาทิตย์ ไม่ส่ง
        return

    targets = db_get_targets()
    if not targets:
        print("[summary] no targets")
        return

    tasks = db_get_all_tasks()
    day_th = {0:"จันทร์",1:"อังคาร",2:"พุธ",3:"พฤหัสบดี",4:"ศุกร์"}[now.weekday()]
    date_str = now.strftime("%d/%m/%Y")

    if not tasks:
        msg = f"🌅 สวัสดีวัน{day_th}! {date_str}\nยังไม่มีงานในระบบครับ"
    else:
        lines = [f"🌅 สรุปงานประจำวัน{day_th} {date_str}"]
        for i, (assign_date, person, task, due) in enumerate(tasks, 1):
            lines.append(_format_task_line(i, person, task, due))
        lines.append(f"รวม {len(tasks)} งาน")
        msg = "\n\n".join(lines)

    print(f"[summary] sending to {len(targets)} target(s)")
    for target_id in targets:
        push_message(target_id, msg)


# =========================
# Leave Notification & Cleanup
# =========================
def send_leave_notification():
    """
    ส่ง bubble แยก (ไม่ปนกับ task summary) เตือนวันลาวันนี้
    ทำงานทุกวัน 09:30 แต่จะส่งเฉพาะวันที่มีการลาเท่านั้น
    """
    now = datetime.now(pytz.timezone("Asia/Bangkok"))
    today = now.strftime("%d/%m/%Y")
    leaves = db_get_leaves_by_date(today)
    if not leaves:
        print("[leave notify] no leave today — skip")
        return

    targets = db_get_targets()
    if not targets:
        print("[leave notify] no targets — skip")
        return

    day_th = {0:"จันทร์",1:"อังคาร",2:"พุธ",3:"พฤหัสบดี",4:"ศุกร์",
               5:"เสาร์",6:"อาทิตย์"}[now.weekday()]

    lines = [f"🏖️ แจ้งเตือนวันลา — {day_th} {today}",
             "——————————————————————"]
    for _, person, _, leave_type in leaves:
        suffix = f" ({leave_type})" if leave_type != "เต็มวัน" else ""
        lines.append(f"• {person} ลางานวันนี้{suffix}")
    lines.append("——————————————————————")
    msg = "\n".join(lines)

    print(f"[leave notify] {len(leaves)} leave(s) today → {len(targets)} target(s)")
    for target_id in targets:
        push_message(target_id, msg)


def cleanup_expired_leaves():
    """00:05 เที่ยงคืน — ลบวันลาที่วันที่ < วันนี้"""
    db_delete_expired_leaves()


# =========================
# Debug Routes
# =========================
@app.route("/", methods=["GET"])
def health():
    return "LINE Task Bot — ตามงานจ้า is running!", 200


@app.route("/debug/env", methods=["GET"])
def debug_env():
    return jsonify({
        "line_token": bool(LINE_CHANNEL_ACCESS_TOKEN),
        "line_secret": bool(LINE_CHANNEL_SECRET),
        "openai_key": bool(OPENAI_API_KEY),
        "db_path": str(DB_PATH),
    })


@app.route("/debug/db", methods=["GET"])
def debug_db():
    try:
        tasks = db_get_all_tasks()
        return jsonify({"task_count": len(tasks), "latest": tasks[:5]})
    except Exception as e:
        return jsonify({"error": repr(e)}), 500


@app.route("/debug/summary", methods=["GET"])
def debug_summary():
    """ทดสอบส่ง morning summary ทันที"""
    send_daily_summary()
    return "summary sent", 200


@app.route("/debug/profile", methods=["GET"])
def debug_profile():
    """
    ทดสอบดึงชื่อจาก LINE API
    ใช้: GET /debug/profile?uid=Uxxxx&gid=Cxxxx
    """
    uid = request.args.get("uid", "")
    gid = request.args.get("gid", "")
    rid = request.args.get("rid", "")
    if not uid:
        return jsonify({"error": "ต้องส่ง ?uid=<userId>"}), 400
    name = get_display_name(uid, gid, rid)
    import unicodedata
    return jsonify({
        "userId": uid,
        "groupId": gid or None,
        "roomId":  rid or None,
        "displayName": name,
        "displayName_repr": repr(name),
        "len": len(name),
        "normalization": "NFC",
        "codepoints": [f"U+{ord(c):04X} ({unicodedata.name(c, '?')})" for c in name],
    })


@app.route("/debug/users", methods=["GET"])
def debug_users():
    """แสดง user registry ทั้งหมดใน DB"""
    users = db_get_all_users()
    return jsonify([
        {"user_id": u[0], "display_name": u[1],
         "repr": repr(u[1]), "updated_at": u[2]}
        for u in users
    ])


@app.route("/debug/names", methods=["GET"])
def debug_names():
    """แสดงชื่อทั้งหมดใน DB พร้อม repr เพื่อดู encoding"""
    import unicodedata
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT DISTINCT person_name FROM tasks ORDER BY person_name")
    task_names = [r[0] for r in c.fetchall()]
    c.execute("SELECT DISTINCT person_name FROM leaves ORDER BY person_name")
    leave_names = [r[0] for r in c.fetchall()]
    conn.close()

    def info(n):
        return {
            "name": n,
            "repr": repr(n),
            "len": len(n),
            "codepoints": [f"U+{ord(ch):04X}" for ch in n],
        }

    return jsonify({
        "task_persons":  [info(n) for n in task_names],
        "leave_persons": [info(n) for n in leave_names],
    })


# =========================
# Webhook
# =========================
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    if not verify_signature(body, signature):
        abort(400)

    try:
        data = json.loads(body.decode("utf-8"))
        for event in data.get("events", []):
            if event.get("type") != "message":
                continue
            msg = event.get("message", {})
            if msg.get("type") != "text":
                continue

            text        = msg.get("text", "").strip()
            reply_token = event.get("replyToken", "")
            source      = event.get("source", {})
            group_id    = source.get("groupId", "")
            room_id     = source.get("roomId", "")
            user_id     = source.get("userId", "")
            source_id   = group_id or room_id or user_id

            if not text.startswith("/"):
                continue

            # บันทึก target อัตโนมัติ
            if group_id:
                db_save_target(group_id, "group")
            elif user_id:
                db_save_target(user_id, "user")

            classified = classify_command(text)
            intent  = classified.get("intent", "unknown")
            content = classified.get("content", "")
            print(f"[webhook] intent={intent} content={content[:60]}")

            # ดึง display name ของคนส่ง (ใช้สำหรับ add_task / add_leave ที่ไม่พิมชื่อ)
            sender_name = ""
            if intent in ("add_task", "add_leave"):
                sender_name = get_display_name(user_id, group_id, room_id)
                print(f"[webhook] sender_name={sender_name!r} (uid={user_id[:8]}... gid={group_id[:8] if group_id else '-'})")

            if intent == "help":
                reply_message(reply_token, HELP_TEXT)

            elif intent == "set_group":
                if group_id:
                    db_save_target(group_id, "group")
                    reply_message(reply_token,
                        "✅ บันทึกกลุ่มนี้เป็นกลุ่มหลักสำเร็จ!\n"
                        "บอทจะส่งสรุปงานเช้าเข้ากลุ่มนี้ทุกวัน จ-ศ เวลา 09:30")
                else:
                    reply_message(reply_token, "❌ ใช้ได้เฉพาะในกลุ่มแชทเท่านั้นครับ")

            elif intent == "list_tasks":
                reply_message(reply_token, handle_list_tasks(person_filter=content))

            elif intent == "delete_all":
                reply_message(reply_token, handle_delete_all())

            elif intent == "delete_task":
                reply_message(reply_token, handle_delete_task(content))

            elif intent == "complete_task":
                reply_message(reply_token, handle_complete_task(content))

            elif intent == "meeting_summary":
                reply_message(reply_token, handle_meeting_summary(content))

            elif intent == "add_task":
                reply_message(reply_token, "กำลังบันทึกงาน...")
                _c, _sid, _sn = content, source_id, sender_name
                def _do_add(_c=_c, _sid=_sid, _sn=_sn):
                    push_message(_sid, handle_add_task(_c, sender_name=_sn))
                threading.Thread(target=_do_add, daemon=True).start()

            elif intent == "whoami":
                # ทดสอบอ่านชื่อ — แสดงผลตรงๆ ว่าบอทอ่านได้ไหม
                name = get_display_name(user_id, group_id, room_id)
                cached = db_get_user_name(user_id)
                if name:
                    msg = (f"บอทอ่านชื่อได้ครับ\n"
                           f"ชื่อ LINE: {name}\n"
                           f"user_id: {user_id[:12]}...")
                elif cached:
                    msg = (f"API อ่านไม่ได้ แต่มี cache\n"
                           f"ชื่อ (cache): {cached}\n"
                           f"user_id: {user_id[:12]}...")
                else:
                    msg = (f"บอทอ่านชื่อไม่ได้ครับ\n"
                           f"user_id: {user_id[:12]}...\n"
                           f"group_id: {(group_id or '-')[:12]}...\n\n"
                           f"วิธีแก้: พิมพ์  /ฉัน [ชื่อ]  เพื่อลงทะเบียนชื่อแทน")
                reply_message(reply_token, msg)

            elif intent == "register_name":
                reply_message(reply_token, handle_register_name(user_id, content))

            elif intent == "add_leave":
                reply_message(reply_token, handle_add_leave(content, sender_name=sender_name))

            elif intent == "list_leaves":
                reply_message(reply_token, handle_list_leaves())

            elif intent == "delete_leave":
                reply_message(reply_token, handle_delete_leave(content))

            elif intent == "query":
                reply_message(reply_token, "กำลังค้นหาข้อมูล...")
                _q = content or text
                _sid = source_id
                def _do_query(_q=_q, _sid=_sid):
                    push_message(_sid, answer_question(_q, db_get_all_tasks()))
                threading.Thread(target=_do_query, daemon=True).start()

            else:
                reply_message(reply_token,
                    f"ไม่เข้าใจคำสั่ง '{text}'\nพิมพ์ /help เพื่อดูคำสั่งทั้งหมด")

    except Exception as e:
        print(f"[callback Error] {e}")
        return "ERROR", 500

    return "OK", 200


# =========================
# Startup
# =========================
init_db()

_tz = pytz.timezone("Asia/Bangkok")
_scheduler = BackgroundScheduler(timezone=_tz)
_scheduler.add_job(
    send_daily_summary,
    "cron",
    day_of_week="mon-fri",
    hour=9, minute=30,
    misfire_grace_time=300
)
# ส่งแจ้งเตือนวันลาทุกวัน 09:30 (แยก bubble จาก task summary)
_scheduler.add_job(
    send_leave_notification,
    "cron",
    hour=9, minute=30,
    misfire_grace_time=300
)
# ลบวันลาที่หมดอายุ — ทุกวันเที่ยงคืน
_scheduler.add_job(
    cleanup_expired_leaves,
    "cron",
    hour=0, minute=5
)
_scheduler.start()
print("[startup] APScheduler running — summary+leave at 09:30, cleanup at 00:05 (Bangkok)")

if __name__ == "__main__":
    print("Running locally on http://localhost:5000")
    app.run(debug=False, host="0.0.0.0", port=5000, threaded=True)
