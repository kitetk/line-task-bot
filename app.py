"""
LINE Task Bot - AI-Powered Command Router

ความสามารถ:
- ตอบเฉพาะข้อความที่ขึ้นต้นด้วย /
- ใช้ AI ตีความคำสั่งหลัง / แบบภาษาธรรมชาติ
- /เพิ่มงาน รองรับภาษาธรรมชาติ
- /งานทั้งหมด, /งานของ[ชื่อ], /ลบงาน, /ล้างงานทั้งหมด
- ถามตอบจากข้อมูลงาน
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
from pathlib import Path
from datetime import datetime

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request, abort, jsonify
from openai import OpenAI
from dotenv import load_dotenv

# =========================
# Load environment
# =========================
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

print("BASE_DIR =", BASE_DIR)
print("ENV_PATH =", ENV_PATH)
print("ENV_PATH exists =", ENV_PATH.exists())

load_dotenv(dotenv_path=ENV_PATH, override=True)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

print("LINE_CHANNEL_ACCESS_TOKEN loaded:", bool(LINE_CHANNEL_ACCESS_TOKEN))
print("LINE_CHANNEL_SECRET loaded:", bool(LINE_CHANNEL_SECRET))
print("OPENAI_API_KEY loaded:", bool(OPENAI_API_KEY))
print("OPENAI_API_KEY prefix:", OPENAI_API_KEY[:12] if OPENAI_API_KEY else "EMPTY")
print("OPENAI_API_KEY length:", len(OPENAI_API_KEY))

app = Flask(__name__)
# Railway ใช้ /data/tasks.db (persistent volume), local ใช้ tasks.db ข้างๆ app.py
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "tasks.db")))

openai_client = OpenAI(api_key=OPENAI_API_KEY)

# =========================
# Database
# =========================
def get_conn():
    return sqlite3.connect(DB_PATH)


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
    # เก็บ group/user ID ที่บอทอยู่ด้วย (สำหรับ push morning summary)
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
    conn.commit()
    conn.close()


def db_save_target(target_id: str, target_type: str):
    """บันทึก group/user ID เพื่อใช้ push morning summary"""
    if not target_id:
        return
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO targets (target_id, target_type, created_at) VALUES (?, ?, ?)",
        (target_id, target_type, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()
    conn.close()


def db_get_targets() -> list:
    """ดึง target IDs — ถ้ามีกลุ่มให้ส่งเฉพาะกลุ่ม, ถ้าไม่มีกลุ่มค่อยส่งส่วนตัว"""
    conn = get_conn()
    c = conn.cursor()
    # ลองดึงเฉพาะ group ก่อน
    c.execute("SELECT target_id FROM targets WHERE target_type = 'group'")
    groups = [r[0] for r in c.fetchall()]
    if groups:
        conn.close()
        return groups
    # ถ้าไม่มีกลุ่มเลย fallback ไปส่วนตัว
    c.execute("SELECT target_id FROM targets WHERE target_type = 'user'")
    users = [r[0] for r in c.fetchall()]
    conn.close()
    return users


def db_add_task(assign_date, person_name, task_description, due_date):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO tasks (
            assign_date,
            person_name,
            task_description,
            due_date,
            created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            assign_date,
            person_name,
            task_description,
            due_date,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
    )
    conn.commit()
    conn.close()


def db_get_all_tasks():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT assign_date, person_name, task_description, due_date
        FROM tasks
        ORDER BY id DESC
    """)
    rows = c.fetchall()
    conn.close()
    return rows


def db_delete_tasks_by_person(person_name):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE person_name LIKE ?", (f"%{person_name}%",))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected


def db_delete_all_tasks():
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM tasks")
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected


def db_save_meeting(meeting_date: str, summary: str):
    """บันทึกหรืออัปเดตสรุปประชุมตามวันที่"""
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """INSERT INTO meetings (meeting_date, summary, created_at)
           VALUES (?, ?, ?)
           ON CONFLICT(meeting_date) DO UPDATE SET summary=excluded.summary, created_at=excluded.created_at""",
        (meeting_date, summary, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()
    conn.close()


def db_get_meeting(meeting_date: str):
    """ดึงสรุปประชุมตามวันที่ (คืน None ถ้าไม่พบ)"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT meeting_date, summary FROM meetings WHERE meeting_date = ?", (meeting_date,))
    row = c.fetchone()
    conn.close()
    return row


def db_get_all_meetings():
    """ดึงสรุปประชุมทั้งหมด"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT meeting_date, summary FROM meetings ORDER BY meeting_date DESC")
    rows = c.fetchall()
    conn.close()
    return rows


# =========================
# LINE API
# =========================
def verify_signature(body: bytes, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET:
        return False

    hash_val = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()
    expected = base64.b64encode(hash_val).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def reply_message(reply_token: str, text: str):
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }
    body = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": str(text)[:5000]}]
    }
    try:
        r = requests.post(url, headers=headers, json=body, timeout=15)
        print(f"[reply] status={r.status_code} body={r.text}")
        return r.status_code, r.text
    except Exception as e:
        print(f"[reply_message Error] {repr(e)}")
        return 500, repr(e)


def push_message(to: str, text: str):
    """Push message (ใช้ใน background thread)"""
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }
    body = {
        "to": to,
        "messages": [{"type": "text", "text": str(text)[:5000]}]
    }
    try:
        r = requests.post(url, headers=headers, json=body, timeout=15)
        print(f"[push] status={r.status_code}")
    except Exception as e:
        print(f"[push_message Error] {repr(e)}")


# =========================
# Utilities
# =========================
def today_str():
    return datetime.now().strftime("%d/%m/%Y")


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_json_object(text: str):
    """ดึง JSON object แรกที่เจอใน text (greedy — ดึงให้ครบ)"""
    try:
        return json.loads(text)
    except Exception:
        pass
    # greedy *  เพื่อให้ครอบคลุม nested content ใน string ได้
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass
    return None


def extract_json_array(text: str):
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


# =========================
# Rule-based Fallback Classifier (ใช้เมื่อ AI ไม่พร้อม)
# =========================
def classify_command_fallback(text: str) -> dict:
    t = text.strip()
    lower = t.lower()

    # Help
    if lower in ["/help", "/วิธีใช้", "/ช่วยเหลือ", "/คำสั่ง"]:
        return {"intent": "help", "content": ""}

    # Add task
    for prefix in ["/เพิ่มงาน", "/เพิ่ม", "/add", "/บันทึกงาน", "/มอบหมาย"]:
        if lower.startswith(prefix):
            return {"intent": "add_task", "content": t[len(prefix):].strip()}

    # List all
    if lower in ["/งานทั้งหมด", "/tasks", "/ดูงาน", "/รายการงาน", "/สรุปงาน", "/list"]:
        return {"intent": "list_tasks", "content": ""}

    # List by person: /งานของ[ชื่อ]
    for prefix in ["/งานของ"]:
        if lower.startswith(prefix):
            return {"intent": "list_tasks", "content": t[len(prefix):].strip()}

    # Delete all
    if lower in ["/ล้างงานทั้งหมด", "/ล้างทั้งหมด", "/ล้าง", "/deleteall"]:
        return {"intent": "delete_all", "content": ""}

    # Delete by person
    for prefix in ["/ลบงาน", "/ลบ", "/delete"]:
        if lower.startswith(prefix):
            return {"intent": "delete_task", "content": t[len(prefix):].strip()}

    # Meeting summary
    for prefix in ["/สรุปประชุม", "/ประชุม", "/meeting"]:
        if lower.startswith(prefix):
            return {"intent": "meeting_summary", "content": t[len(prefix):].strip()}

    # Set group target
    if lower in ["/ตั้งกลุ่มหลัก", "/setgroup", "/ตั้งกลุ่ม"]:
        return {"intent": "set_group", "content": ""}

    # Default → query (ตัด / ออกแล้วส่งเป็นคำถาม)
    return {"intent": "query", "content": t[1:].strip()}


def answer_from_db_only(question: str, tasks_data: list) -> str:
    """Fallback ตอบจาก DB โดยตรงเมื่อ AI ไม่พร้อม"""
    if not tasks_data:
        return "ยังไม่มีข้อมูลงานในระบบ"

    q = question.lower()
    matched = []
    for assign_date, person, task, due in tasks_data:
        if person.lower() in q:
            matched.append(f"{person}: {task} | ส่ง {due}")

    if matched:
        return "งานที่พบ:\n" + "\n".join(matched)

    lines = ["งานทั้งหมดในระบบ:\n"]
    for i, (assign_date, person, task, due) in enumerate(tasks_data[:20], 1):
        lines.append(f"{i}. {person} — {task} | ส่ง {due}")
    return "\n".join(lines)[:5000]


# =========================
# AI Command Classifier
# =========================
def classify_command(text: str) -> dict:
    """
    Fast classifier:
    1. Rule-based ก่อน (instant) — ถ้าได้ intent ชัดเจน → return ทันที
    2. AI เฉพาะกรณี intent='query' (ข้อความกำกวม) และมี quota
    """
    result = classify_command_fallback(text)

    # ถ้า rule-based classify ได้ชัด → ใช้เลย ไม่ต้องเรียก AI
    if result["intent"] != "query":
        print(f"[classify_fast] intent={result['intent']}")
        return result

    # intent='query' → ลอง AI เพื่อ extract content ให้ดีขึ้น (optional)
    if not OPENAI_API_KEY:
        return result

    try:
        ai_result = _classify_with_ai(text)
        if ai_result and ai_result.get("intent"):
            print(f"[classify_ai] intent={ai_result['intent']}")
            return ai_result
    except Exception as e:
        print(f"[classify_ai skipped] {repr(e)[:80]}")

    return result


def _classify_with_ai(text: str) -> dict:
    if not OPENAI_API_KEY:
        return classify_command_fallback(text)

    prompt = f"""คุณคือ command classifier สำหรับบอทจัดการงานทีม

วิเคราะห์ข้อความของผู้ใช้แล้วตอบเป็น JSON เท่านั้น ห้ามมีคำอธิบายเพิ่มเติม

รูปแบบที่ต้องตอบ:
{{"intent": "<intent>", "content": "<ข้อมูลสำคัญ>"}}

intent ที่มี:
- "add_task"   = เพิ่มงาน / มอบหมายงาน / บันทึกงาน
- "list_tasks" = ดูงานทั้งหมด / สรุปงาน / มีงานอะไรบ้าง
- "delete_task" = ลบงานของคนใดคนหนึ่ง (content = ชื่อคน)
- "delete_all"  = ล้างงานทั้งหมด / ลบทุกอย่าง
- "query"       = ถามเกี่ยวกับงาน เช่น งานของใคร ส่งเมื่อไร ใครมีงานค้าง
- "help"        = ขอความช่วยเหลือ / วิธีใช้

กติกาสำหรับ content:
- add_task: ให้ตัดคำสั่งออก เหลือแค่ข้อมูลงาน เช่น "ชื่อ งาน ส่งวันที่"
- list_tasks: ให้ content เป็นชื่อคน ถ้าระบุ (เช่น "งานของตะไคร้") หรือ "" ถ้าต้องการทุกคน
- delete_task: ให้ content เป็นชื่อคน เท่านั้น
- delete_all: content = ""
- query: ให้ content เป็นคำถามที่เข้าใจได้
- help: content = ""

ตัวอย่าง:
"/เพิ่มงานของบาส ทำสรุปข้อมูล ส่ง พรุ่งนี้" → {{"intent": "add_task", "content": "บาส ทำสรุปข้อมูล ส่ง พรุ่งนี้"}}
"/เพิ่ม ตะไคร้ทำสไลด์ learntoearn ส่ง 5/4/69" → {{"intent": "add_task", "content": "ตะไคร้ ทำสไลด์ learntoearn ส่ง 5/4/69"}}
"/ดูงานทั้งหมด" → {{"intent": "list_tasks", "content": ""}}
"/งานของตะไคร้" → {{"intent": "list_tasks", "content": "ตะไคร้"}}
"/ตะไคร้ต้องทำอะไรบ้าง" → {{"intent": "query", "content": "ตะไคร้ต้องทำอะไรบ้าง"}}
"/ลบงานบาส" → {{"intent": "delete_task", "content": "บาส"}}
"/ล้างทุกอย่าง" → {{"intent": "delete_all", "content": ""}}
"/help" → {{"intent": "help", "content": ""}}
"/วิธีใช้" → {{"intent": "help", "content": ""}}

วันนี้คือ {today_str()}

ข้อความผู้ใช้: {text}""".strip()

    try:
        response = openai_client.responses.create(
            model="gpt-4.1-mini",
            input=prompt
        )
        result_text = (response.output_text or "").strip()
        print(f"[Classify Raw] {result_text}")

        parsed = extract_json_object(result_text)
        if parsed and "intent" in parsed:
            return {
                "intent": str(parsed.get("intent", "unknown")),
                "content": str(parsed.get("content", ""))
            }

        # AI ตอบมาแต่ parse ไม่ได้ → fallback
        return classify_command_fallback(text)

    except Exception as e:
        print(f"[Classify Error] {repr(e)}")
        # quota หมด / error ใดๆ → fallback rule-based
        return classify_command_fallback(text)


# =========================
# Parsing (Task)
# =========================
def parse_tasks_with_ai(raw: str):
    """ให้ AI วิเคราะห์ข้อความ แล้วคืน JSON array ของงาน"""
    if not OPENAI_API_KEY:
        return []

    prompt = f"""คุณคือ parser สำหรับข้อความมอบหมายงานภาษาไทย

แปลงข้อความของผู้ใช้ให้เป็น JSON array เท่านั้น
ห้ามมีคำอธิบาย ห้ามมี markdown ห้ามมีข้อความอื่น

รูปแบบของแต่ละ object:
{{
  "assign_date": "DD/MM/YYYY",
  "person_name": "ชื่อคน",
  "task_description": "รายละเอียดงาน",
  "due_date": "กำหนดส่ง หรือ ไม่ระบุ"
}}

กติกา:
1. ถ้าไม่มี assign_date ให้ใช้ "{today_str()}"
2. ถ้าไม่มี due_date ให้ใช้ "ไม่ระบุ"
3. ถ้ามีหลายคนหรือหลายงาน ให้แยกเป็นหลาย object
4. ถ้าชื่อคนติดกับงาน เช่น "ตะไคร้ทำสไลด์" ให้ตีความเป็น person_name="ตะไคร้", task_description="ทำสไลด์"
5. "พรุ่งนี้" = วันพรุ่งนี้ วันนี้คือ {today_str()}
6. ถ้าระบุ due_date เป็น "พรุ่งนี้" หรือ "วันนี้" หรือ "X เม.ย." ให้แปลงเป็น DD/MM/YYYY
7. ถ้าแยกไม่ได้เลย ให้ตอบ []

ข้อความผู้ใช้:
{raw}""".strip()

    try:
        response = openai_client.responses.create(
            model="gpt-4.1-mini",
            input=prompt
        )
        text = (response.output_text or "").strip()
        print("[AI Parser Raw]", text)

        parsed = extract_json_array(text)
        if not isinstance(parsed, list):
            return []

        normalized = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            person_name = str(item.get("person_name", "")).strip()
            task_description = str(item.get("task_description", "")).strip()
            if not person_name or not task_description:
                continue
            normalized.append({
                "assign_date": str(item.get("assign_date", today_str())).strip() or today_str(),
                "person_name": person_name,
                "task_description": task_description,
                "due_date": str(item.get("due_date", "ไม่ระบุ")).strip() or "ไม่ระบุ"
            })
        return normalized

    except Exception as e:
        print(f"[AI Parser Error] {repr(e)}")
        return []


def smart_preprocess(raw: str) -> str:
    """
    แยกคำที่ติดกันออกก่อน parse:
    1. "ส่ง2/4/69"      → "ส่ง 2/4/69"
    2. "ตะไครทำสไลด์"  → "ตะไคร ทำสไลด์"
    3. "บาสทำรายงาน"   → "บาส ทำรายงาน"
    """
    # 1. แยก ส่ง ออกจากตัวเลขที่ต่อท้าย: "ส่ง5/4" → "ส่ง 5/4"
    raw = re.sub(r'(ส่ง)(\d)', r'\1 \2', raw)

    # 2. แยก task verb ออกจากชื่อ (ภาษาไทย + verb ติดกัน)
    # เรียงยาวก่อนสั้น เพื่อจับ "ทำสไลด์" ก่อน "ทำ"
    task_verbs = [
        "ทำสไลด์", "ทำรายงาน", "ทำเอกสาร", "ทำสรุป", "ทำเพรส", "ทำแพลน",
        "ทำ", "จัด", "เขียน", "สรุป", "เตรียม", "แก้ไข", "แก้", "อัปเดต",
        "ออกแบบ", "ดีไซน์", "ตรวจ", "ส่ง", "นำเสนอ", "report", "update",
    ]
    for verb in sorted(task_verbs, key=len, reverse=True):
        raw = re.sub(rf'([ก-๙]+)({re.escape(verb)})', rf'\1 \2', raw)

    return normalize_spaces(raw)


def parse_task_simple(raw: str):
    """
    Fallback parser: [วันที่] [ชื่อ] [งาน...] ส่ง [กำหนดส่ง]
    รองรับ: "ตะไครทำสไลด์ learntoearn ส่ง2/4/69"
    """
    try:
        raw = smart_preprocess(raw)
        print(f"[parse_task_simple] after preprocess: {raw}")

        parts = raw.split()
        if len(parts) < 2:
            return None

        # หา index ของ "ส่ง" (ทั้งแบบ standalone และแบบ "ส่งXXX")
        send_idx = -1
        due_date = "ไม่ระบุ"
        for i, p in enumerate(parts):
            if p == "ส่ง" or p.startswith("ส่ง"):
                send_idx = i
                # กรณี "ส่ง" standalone → วันที่อยู่ token ถัดไป
                if p == "ส่ง" and i + 1 < len(parts):
                    due_date = parts[i + 1]
                    main_parts = parts[:i]
                else:
                    # กรณี "ส่ง5/4" ยังไม่ได้ split (ป้องกัน edge case)
                    due_date = p[len("ส่ง"):].strip() or "ไม่ระบุ"
                    main_parts = parts[:i]
                break

        if send_idx == -1:
            main_parts = parts  # ไม่มีวันส่ง

        if len(main_parts) < 2:
            return None

        # ตรวจว่า main_parts[0] เป็นวันที่หรือเปล่า
        if re.match(r"^\d{1,2}[/\-]\d{1,2}", main_parts[0]):
            assign_date = main_parts[0]
            person_name = main_parts[1] if len(main_parts) > 1 else "ไม่ระบุ"
            task_description = " ".join(main_parts[2:]) if len(main_parts) > 2 else "ไม่ระบุ"
        else:
            assign_date = today_str()
            person_name = main_parts[0]
            task_description = " ".join(main_parts[1:]) if len(main_parts) > 1 else "ไม่ระบุ"

        if not person_name.strip() or not task_description.strip():
            return None

        return {
            "assign_date": assign_date,
            "person_name": person_name.strip(),
            "task_description": task_description.strip(),
            "due_date": due_date.strip() or "ไม่ระบุ"
        }
    except Exception as e:
        print(f"[parse_task_simple Error] {repr(e)}")
        return None


TASK_VERBS_MULTI = [
    "ทำสไลด์", "ทำรายงาน", "ทำเอกสาร", "ทำสรุป", "ทำแพลน",
    "ทำ", "จัด", "เขียน", "สรุป", "เตรียม", "แก้ไข", "แก้", "อัปเดต",
    "ออกแบบ", "ดีไซน์", "ตรวจ", "นำเสนอ",
]
_VERB_PAT = "|".join(sorted(TASK_VERBS_MULTI, key=len, reverse=True))


def split_multi_tasks_fallback(raw: str) -> list:
    """
    แยก text ยาวๆ เป็นหลายงาน (ไม่ใช้ AI)

    กติกา:
    - ถ้ามี ส่ง XX แค่อันเดียว (ท้ายสุด) → apply กับทุกงาน
    - ถ้างานไหนมี ส่ง XX ของตัวเอง → ใช้ของตัวเอง
    - รองรับ assign_date นำหน้า
    """
    raw = smart_preprocess(raw)
    print(f"[multi_parse] preprocessed: {raw}")

    # Extract assign_date ที่ต้นข้อความ
    assign_date = today_str()
    date_m = re.match(r"^(\d{1,2}[/\-]\d{1,2}(?:[/\-]\d{2,4})?)\s+", raw)
    if date_m:
        assign_date = date_m.group(1)
        raw = raw[date_m.end():].strip()

    # หา global due date (ส่ง XX สุดท้ายในทั้ง text)
    all_dues = list(re.finditer(r"ส่ง\s+(\S+)", raw))
    global_due = all_dues[-1].group(1) if all_dues else "ไม่ระบุ"

    # หา task boundaries: [ชื่อ] [verb] — ordered by position
    boundaries = list(re.finditer(rf"([ก-๙A-Za-z]+)\s+({_VERB_PAT})", raw))
    print(f"[multi_parse] boundaries: {[(m.group(1), m.group(2)) for m in boundaries]}")

    if len(boundaries) < 2:
        # ถ้ามีงานเดียว ส่งคืน [] เพื่อให้ parse_task_simple จัดการ
        return []

    results = []
    for i, m in enumerate(boundaries):
        person = m.group(1)
        verb = m.group(2)
        # content = ตั้งแต่หลัง verb จนถึง boundary ถัดไป (หรือ end)
        end = boundaries[i + 1].start() if i + 1 < len(boundaries) else len(raw)
        content = raw[m.end():end].strip()

        # หา per-task ส่ง ใน content
        dm = re.search(r"ส่ง\s+(\S+)", content)
        if dm:
            task_due = dm.group(1)
            task_content = (content[: dm.start()] + content[dm.end():]).strip()
        else:
            task_due = global_due
            task_content = content

        task_desc = f"{verb} {task_content}".strip()
        results.append({
            "assign_date": assign_date,
            "person_name": person,
            "task_description": task_desc,
            "due_date": task_due,
        })

    return results


def parse_tasks(raw: str):
    """
    ลำดับ (เร็วก่อน):
    1) Multi-task rule-based (instant) — ถ้ามี 2+ งาน
    2) Single-task rule-based (instant)
    3) AI parse (ช้า) — เฉพาะเมื่อ rule-based อ่านไม่ได้เลย
    """
    # 1. Rule-based multi-task (instant)
    multi = split_multi_tasks_fallback(raw)
    if multi:
        return multi

    # 2. Rule-based single task (instant)
    parsed = parse_task_simple(raw)
    if parsed:
        return [parsed]

    # 3. AI เป็น last resort (ช้า, ใช้เฉพาะ text กำกวมจริงๆ)
    ai_results = parse_tasks_with_ai(raw)
    if ai_results:
        return ai_results

    return []


# =========================
# Answering
# =========================
def build_tasks_text(tasks_data: list) -> str:
    if not tasks_data:
        return "ยังไม่มีข้อมูลงานในระบบ"
    lines = []
    for assign_date, person, task, due in tasks_data:
        lines.append(f"- {person}: {task} (มอบหมาย {assign_date}, กำหนดส่ง {due})")
    return "\n".join(lines)


def answer_question(question: str, tasks_data: list) -> str:
    tasks_text = build_tasks_text(tasks_data)

    if not OPENAI_API_KEY:
        return answer_from_db_only(question, tasks_data)

    try:
        # Responses API ใช้ instructions= แทน role:"system" ใน input
        response = openai_client.responses.create(
            model="gpt-4.1-mini",
            instructions=(
                "คุณเป็น AI ผู้ช่วยทีม "
                "ตอบคำถามจากข้อมูลงานที่ได้รับเท่านั้น "
                "ตอบเป็นภาษาไทย กระชับ ชัดเจน "
                "ห้ามแต่งข้อมูลเพิ่ม ถ้าไม่มีข้อมูลให้บอกว่าไม่พบข้อมูลในระบบ"
            ),
            input=f"ข้อมูลงาน:\n{tasks_text}\n\nคำถาม: {question}"
        )
        answer = (response.output_text or "").strip()
        return answer if answer else answer_from_db_only(question, tasks_data)
    except Exception as e:
        print(f"[OpenAI Error] {repr(e)}")
        # quota หมด / error ใดๆ → fallback DB
        return answer_from_db_only(question, tasks_data)


# =========================
# Command Handlers
# =========================
HELP_TEXT = """คำสั่งที่ใช้ได้ (พิมพ์ / นำหน้าเสมอ)

เพิ่มงาน:
/เพิ่มงาน ตะไคร้ทำสไลด์ learntoearn ส่ง 5/4/69
/เพิ่มงานของบาส ทำสรุปข้อมูล ส่ง พรุ่งนี้
/เพิ่ม วันนี้มอบให้ตะไคร้ทำสไลด์ และบาสทำรายงาน ส่ง 7/4

ดูงาน:
/งานทั้งหมด
/งานของตะไคร้
/ตะไคร้ต้องทำอะไรบ้าง

ลบงาน:
/ลบงาน ตะไคร้
/ล้างงานทั้งหมด

สรุปประชุม:
/สรุปประชุม 3/4/2026 [เนื้อหาสรุป...]  ← บันทึกสรุปประชุม
/สรุปประชุม 3/4/2026  ← ดูสรุปประชุมวันนั้น
/สรุปประชุม  ← ดูสรุปประชุมทั้งหมด

ช่วยเหลือ:
/help หรือ /วิธีใช้"""


def handle_meeting_summary(content: str) -> str:
    """
    จัดการคำสั่ง /สรุปประชุม
    - ถ้ามีเนื้อหาหลังวันที่ → บันทึก
    - ถ้ามีแค่วันที่ → ดึงสรุปประชุมวันนั้น
    - ถ้าไม่มีอะไรเลย → แสดงสรุปประชุมทั้งหมด
    """
    content = content.strip()

    # ไม่มีอะไรเลย → แสดงทั้งหมด
    if not content:
        meetings = db_get_all_meetings()
        if not meetings:
            return "ยังไม่มีสรุปประชุมในระบบ\nใช้ /สรุปประชุม [วันที่] [เนื้อหา] เพื่อบันทึก"
        lines = ["📋 สรุปประชุมทั้งหมด:\n"]
        for date, summary in meetings:
            lines.append(f"📅 {date}\n{summary}\n{'—'*20}")
        return "\n".join(lines)[:5000]

    # ตรวจว่าขึ้นต้นด้วยวันที่หรือเปล่า (dd/mm/yyyy หรือ d/m/yy)
    date_match = re.match(r"^(\d{1,2}[/\-]\d{1,2}(?:[/\-]\d{2,4})?)\s*([\s\S]*)", content)
    if not date_match:
        return "กรุณาระบุวันที่ก่อน\nตัวอย่าง: /สรุปประชุม 3/4/2026 เนื้อหาสรุป..."

    meeting_date = date_match.group(1).strip()
    summary_text = date_match.group(2).strip()

    # มีแค่วันที่ ไม่มีเนื้อหา → ดึงสรุปวันนั้น
    if not summary_text:
        row = db_get_meeting(meeting_date)
        if not row:
            return f"ไม่พบสรุปประชุมวันที่ {meeting_date}\nใช้ /สรุปประชุม {meeting_date} [เนื้อหา] เพื่อบันทึก"
        return f"📋 สรุปประชุมวันที่ {row[0]}\n\n{row[1]}"

    # มีทั้งวันที่และเนื้อหา → บันทึก
    db_save_meeting(meeting_date, summary_text)
    return f"บันทึกสรุปประชุมสำเร็จ!\nวันที่: {meeting_date}\n\n{summary_text}"


def handle_add_task(content: str) -> str:
    """รับ content ที่ AI extract มาแล้ว (ไม่มี /เพิ่มงาน นำหน้า)"""
    if not content.strip():
        return (
            "กรุณาระบุข้อมูลงาน\n\n"
            "ตัวอย่าง:\n"
            "/เพิ่มงานของบาส ทำสรุปข้อมูล ส่ง พรุ่งนี้"
        )

    parsed_tasks = parse_tasks(content)

    if not parsed_tasks:
        return (
            "ไม่สามารถอ่านข้อมูลได้\n"
            "ลองพิมพ์ให้มีชื่อคน + งาน + คำว่า 'ส่ง' + วันที่\n"
            "เช่น /เพิ่มงาน ตะไคร้ทำสไลด์ ส่ง 5/4/69"
        )

    for item in parsed_tasks:
        db_add_task(
            item["assign_date"],
            item["person_name"],
            item["task_description"],
            item["due_date"]
        )

    if len(parsed_tasks) == 1:
        item = parsed_tasks[0]
        return (
            f"บันทึกงานสำเร็จ!\n"
            f"ชื่อ: {item['person_name']}\n"
            f"งาน: {item['task_description']}\n"
            f"มอบหมาย: {item['assign_date']}\n"
            f"ส่ง: {item['due_date']}"
        )

    lines = [f"บันทึกงานสำเร็จ {len(parsed_tasks)} รายการ\n"]
    for i, item in enumerate(parsed_tasks, 1):
        lines.append(
            f"{i}. ชื่อ: {item['person_name']}\n"
            f"   งาน: {item['task_description']}\n"
            f"   มอบหมาย: {item['assign_date']} | ส่ง: {item['due_date']}"
        )
    return "\n".join(lines)[:5000]


def handle_list_tasks(person_filter: str = "") -> str:
    """ดูงานทั้งหมด หรือกรองตามชื่อ"""
    tasks = db_get_all_tasks()
    if not tasks:
        return "ยังไม่มีงานในระบบ\nใช้ /เพิ่มงาน เพื่อเพิ่มงาน"

    if person_filter:
        tasks = [t for t in tasks if person_filter.lower() in t[1].lower()]
        if not tasks:
            return f"ไม่พบงานของ '{person_filter}'"

    lines = [f"งาน{'ของ ' + person_filter if person_filter else 'ทั้งหมด'}ในระบบ:\n"]
    for i, (assign_date, person, task, due) in enumerate(tasks, 1):
        lines.append(f"{i}. {person}")
        lines.append(f"   งาน: {task}")
        lines.append(f"   มอบหมาย: {assign_date} | ส่ง: {due}\n")
    return "\n".join(lines)[:5000]


def handle_delete_task(person_name: str) -> str:
    if not person_name.strip():
        return "กรุณาระบุชื่อ\nตัวอย่าง: /ลบงาน ตะไคร้"
    count = db_delete_tasks_by_person(person_name.strip())
    if count > 0:
        return f"ลบงานของ '{person_name.strip()}' สำเร็จ ({count} รายการ)"
    return f"ไม่พบงานของ '{person_name.strip()}'"


def handle_delete_all_tasks() -> str:
    count = db_delete_all_tasks()
    return f"ล้างงานทั้งหมดสำเร็จ ({count} รายการ)"


# =========================
# Debug routes
# =========================
@app.route("/", methods=["GET"])
def health():
    return "LINE Task Bot is running!", 200


@app.route("/debug/env", methods=["GET"])
def debug_env():
    return jsonify({
        "env_path": str(ENV_PATH),
        "env_exists": ENV_PATH.exists(),
        "line_token_loaded": bool(LINE_CHANNEL_ACCESS_TOKEN),
        "line_secret_loaded": bool(LINE_CHANNEL_SECRET),
        "openai_key_loaded": bool(OPENAI_API_KEY),
        "openai_key_prefix": OPENAI_API_KEY[:12] if OPENAI_API_KEY else "EMPTY",
        "openai_key_length": len(OPENAI_API_KEY),
    })


@app.route("/debug/db", methods=["GET"])
def debug_db():
    try:
        tasks = db_get_all_tasks()
        return jsonify({
            "db_path": str(DB_PATH),
            "task_count": len(tasks),
            "latest_tasks": tasks[:10]
        })
    except Exception as e:
        return jsonify({"error": repr(e)}), 500


@app.route("/debug/openai", methods=["GET"])
def debug_openai():
    try:
        if not OPENAI_API_KEY:
            return jsonify({"ok": False, "error": "OPENAI_API_KEY missing"}), 500
        response = openai_client.responses.create(
            model="gpt-4.1-mini",
            input="ตอบคำว่า OK"
        )
        return jsonify({
            "ok": True,
            "output": (response.output_text or "").strip()
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": repr(e)
        }), 500


# =========================
# LINE Webhook
# =========================
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    if not verify_signature(body, signature):
        print("[callback] Invalid signature")
        abort(400)

    try:
        data = json.loads(body.decode("utf-8"))
        print("[callback] payload received")

        for event in data.get("events", []):
            if event.get("type") != "message":
                continue

            msg = event.get("message", {})
            if msg.get("type") != "text":
                continue

            text = msg.get("text", "").strip()
            reply_token = event.get("replyToken", "")

            print(f"[msg] {text}")

            # ====================================================
            # ตอบเฉพาะข้อความที่ขึ้นต้นด้วย /
            # ====================================================
            if not text.startswith("/"):
                print("[msg] skipped (no slash prefix)")
                continue

            # ====================================================
            # ใช้ AI ตีความคำสั่ง
            # ====================================================
            classified = classify_command(text)
            intent = classified.get("intent", "unknown")
            content = classified.get("content", "")

            print(f"[intent] {intent} | content: {content}")

            # source id สำหรับ push message (group หรือ user)
            source = event.get("source", {})
            group_id = source.get("groupId", "")
            user_id = source.get("userId", "")
            source_id = group_id or user_id

            # บันทึก target ID อัตโนมัติสำหรับ morning summary
            if group_id:
                db_save_target(group_id, "group")
            elif user_id:
                db_save_target(user_id, "user")

            if intent == "help":
                reply_message(reply_token, HELP_TEXT)

            elif intent == "set_group":
                if group_id:
                    db_save_target(group_id, "group")
                    reply_message(reply_token, "✅ บันทึกกลุ่มนี้เป็นกลุ่มหลักสำเร็จ!\nบอทจะส่งสรุปงานเช้าเข้ากลุ่มนี้ทุกวัน จ-ศ เวลา 09:30")
                else:
                    reply_message(reply_token, "❌ คำสั่งนี้ใช้ได้เฉพาะในกลุ่มแชทเท่านั้นครับ")

            elif intent == "meeting_summary":
                result = handle_meeting_summary(content)
                reply_message(reply_token, result)

            elif intent == "list_tasks":
                result = handle_list_tasks(person_filter=content)
                reply_message(reply_token, result)

            elif intent == "delete_all":
                result = handle_delete_all_tasks()
                reply_message(reply_token, result)

            elif intent == "delete_task":
                result = handle_delete_task(content)
                reply_message(reply_token, result)

            elif intent == "add_task":
                # Fix Bug 3: ใช้ threading เพราะ parse_tasks_with_ai เรียก AI อีกครั้ง
                reply_message(reply_token, "กำลังบันทึกงาน...")
                _content = content
                _sid = source_id
                def _do_add():
                    result = handle_add_task(_content)
                    push_message(_sid, result)
                threading.Thread(target=_do_add, daemon=True).start()

            elif intent == "query":
                # Fix Bug 3: ใช้ threading เพราะ answer_question เรียก AI อีกครั้ง
                reply_message(reply_token, "กำลังค้นหาข้อมูล...")
                _question = content or text
                _sid = source_id
                def _do_query():
                    tasks = db_get_all_tasks()
                    answer = answer_question(_question, tasks)
                    push_message(_sid, answer)
                threading.Thread(target=_do_query, daemon=True).start()

            else:
                # AI ตีความไม่ได้ — แจ้งผู้ใช้
                reply_message(
                    reply_token,
                    f"ไม่เข้าใจคำสั่ง '{text}'\nพิมพ์ /help เพื่อดูคำสั่งทั้งหมด"
                )

    except Exception as e:
        print(f"[callback Error] {repr(e)}")
        return "ERROR", 500

    return "OK", 200


# =========================
# Daily Morning Summary
# =========================
def send_daily_summary():
    """ส่งสรุปงานทุกเช้า จันทร์-ศุกร์ เวลา 09:00"""
    now = datetime.now()
    day_names = {0: "จันทร์", 1: "อังคาร", 2: "พุธ", 3: "พฤหัสบดี", 4: "ศุกร์"}

    # ตรวจสอบว่าเป็น จ-ศ เท่านั้น (0=จ, 4=ศ)
    if now.weekday() > 4:
        return

    tasks = db_get_all_tasks()
    targets = db_get_targets()

    if not targets:
        print("[scheduler] no targets saved yet")
        return

    day_th = day_names.get(now.weekday(), "")
    date_str = now.strftime("%d/%m/%Y")

    if not tasks:
        msg = f"🌅 สวัสดีวัน{day_th}! {date_str}\nยังไม่มีงานในระบบครับ"
    else:
        lines = [f"🌅 สรุปงานประจำวัน{day_th} {date_str}\n"]
        for i, (assign_date, person, task, due) in enumerate(tasks, 1):
            due_label = f"ส่ง {due}" if due != "ไม่ระบุ" else "ยังไม่ระบุวันส่ง"
            lines.append(f"{i}. {person} — {task} ({due_label})")
        lines.append(f"\nรวม {len(tasks)} งาน")
        msg = "\n".join(lines)

    print(f"[scheduler] sending morning summary to {len(targets)} target(s)")
    for target_id in targets:
        push_message(target_id, msg)


def startup_summary():
    """
    ส่งสรุปงานครั้งเดียวตอนเปิดบอท
    - รอ 3 วินาทีให้ Flask และ LINE เชื่อมต่อก่อน
    - ส่งเฉพาะวันจันทร์–ศุกร์
    """
    time.sleep(3)
    send_daily_summary()


# =========================
# Startup — รันเสมอ ทั้ง gunicorn และ python app.py
# =========================
init_db()
threading.Thread(target=startup_summary, daemon=True).start()
print("[startup] DB initialized — will send morning summary shortly")

# APScheduler — ส่งสรุปงานทุกเช้า 09:30 จ-ศ เวลาไทย (ไม่ต้องเปิดคอม)
_tz = pytz.timezone("Asia/Bangkok")
_scheduler = BackgroundScheduler(timezone=_tz)
_scheduler.add_job(send_daily_summary, "cron", day_of_week="mon-fri", hour=9, minute=30)
_scheduler.start()
print("[scheduler] APScheduler started — daily summary at 09:30 Mon-Fri (Bangkok time)")

if __name__ == "__main__":
    print("LINE Task Bot is running (local)...")
    print("Webhook: http://localhost:5000/callback")
    app.run(debug=False, host="0.0.0.0", port=5000, threaded=True)
