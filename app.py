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
        (assign_date, person_name, task_description, due_date, _now())
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
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE person_name LIKE ?", (f"%{person_name}%",))
    n = c.rowcount
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
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT id, assign_date, person_name, task_description, due_date"
        " FROM tasks WHERE person_name LIKE ? AND task_description LIKE ? ORDER BY id ASC",
        (f"%{person_name}%", f"%{keyword}%")
    )
    rows = c.fetchall()
    conn.close()
    return rows


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
            "person_name": person_name.strip(),
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

เพิ่มงาน:
/เพิ่มงาน ตะไคร้ ทำสไลด์ learntoearn ส่ง 5/4/2026
/เพิ่มงาน บาส ทำรายงาน, มะนาว เขียนสรุป ส่ง 7/4

ดูงาน:
/งานทั้งหมด
/งานของตะไคร้
/ตะไคร้ต้องทำอะไรบ้าง

งานเสร็จ:
/เสร็จ ตะไคร้ สไลด์  ← ชื่อ + keyword ของงาน

ลบงาน:
/ลบงาน ตะไคร้
/ล้างงานทั้งหมด

สรุปประชุม:
/สรุปประชุม 3/4/2026 เนื้อหา...  ← บันทึก
/สรุปประชุม 3/4/2026  ← ดูวันนั้น
/สรุปประชุม  ← ดูทั้งหมด

ตั้งค่า:
/ตั้งกลุ่มหลัก  ← ให้บอทส่งสรุปเช้าเข้ากลุ่มนี้
/help"""


def _format_task_line(i: int, person: str, task: str, due: str) -> str:
    due_label = due if due and due != "ไม่ระบุ" else "ไม่ระบุ"
    formatted = format_task_description(task)
    return f"{i}. {person}\n   งาน: {formatted}\n   กำหนดส่ง: {due_label}"


def handle_add_task(content: str) -> str:
    if not content.strip():
        return "กรุณาระบุข้อมูลงาน\nตัวอย่าง: /เพิ่มงาน ตะไคร้ ทำสไลด์ ส่ง 5/4"

    parsed = parse_tasks(content)
    if not parsed:
        return "ไม่สามารถอ่านข้อมูลได้\nลองพิมพ์ให้มีชื่อคน + งาน + ส่ง + วันที่"

    for item in parsed:
        db_add_task(item["assign_date"], item["person_name"],
                    item["task_description"], item["due_date"])

    if len(parsed) == 1:
        t = parsed[0]
        return (f"✅ บันทึกงานสำเร็จ!\n"
                f"ชื่อ: {t['person_name']}\n"
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
        tasks = [t for t in tasks if person_filter.lower() in t[1].lower()]
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
            user_id     = source.get("userId", "")
            source_id   = group_id or user_id

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
                _c, _sid = content, source_id
                def _do_add(_c=_c, _sid=_sid):
                    push_message(_sid, handle_add_task(_c))
                threading.Thread(target=_do_add, daemon=True).start()

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
    misfire_grace_time=300   # ถ้า miss ไป 5 นาที ยังส่งได้
)
_scheduler.start()
print("[startup] APScheduler running — daily summary at 09:30 Mon-Fri (Bangkok)")

if __name__ == "__main__":
    print("Running locally on http://localhost:5000")
    app.run(debug=False, host="0.0.0.0", port=5000, threaded=True)
