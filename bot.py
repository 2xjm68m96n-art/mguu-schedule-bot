import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup


TZ = ZoneInfo("Europe/Moscow")

# Твоя группа (из ссылки)
GROUP_ID = "000000213"
GROUP_NAME = "23ГМУ-УГЛ11.2"
BASE_URL = "https://portal.mguu.ru/student/scheduler1.php"


@dataclass
class Lesson:
    day: date
    pair: str
    time: str
    subject: str
    teacher: str
    room: str
    lesson_type: str


# ---------- Markdown helpers ----------

def md_escape(s: str) -> str:
    """Экранируем символы, которые могут ломать Telegram Markdown."""
    if not s:
        return ""
    s = s.replace("\\", "\\\\")
    s = s.replace("*", "\\*")
    s = s.replace("_", "\\_")
    s = s.replace("`", "\\`")
    s = s.replace("[", "\\[")
    s = s.replace("]", "\\]")
    return s


def pair_badge(pair_num: str) -> str:
    m = {
        "1": "1️⃣",
        "2": "2️⃣",
        "3": "3️⃣",
        "4": "4️⃣",
        "5": "5️⃣",
        "6": "6️⃣",
        "7": "7️⃣",
        "8": "8️⃣",
        "9": "9️⃣",
        "10": "🔟",
    }
    p = (pair_num or "").strip()
    return m.get(p, f"{p})" if p else "•")


def type_badge(lesson_type: str) -> str:
    t = (lesson_type or "").strip().lower()
    if not t:
        return "📌 Занятие"
    if "практ" in t:
        return "📘 Практика"
    if "лекц" in t:
        return "🎓 Лекция"
    if "семин" in t:
        return "🗣 Семинар"
    if "лаб" in t:
        return "🧪 Лаба"
    if "зач" in t or "экзам" in t:
        return "📝 Контроль"
    return f"📌 {md_escape(lesson_type.strip())}"


# ---------- Schedule fetch + parse ----------

DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
PAIR_RE = re.compile(r"№\s*пары\s*[-–]\s*(\d+)", re.IGNORECASE)
TIME_RE = re.compile(r"(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})")


def fetch_html(start: date, end: date) -> str:
    params = {
        "groupid": GROUP_ID,
        "groupname": GROUP_NAME,
        "startDate": start.strftime("%d.%m.%Y"),
        "endDate": end.strftime("%d.%m.%Y"),
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; mguu-schedule-bot/1.0)",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }
    r = requests.get(BASE_URL, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text


def parse_date(s: str) -> Optional[date]:
    m = DATE_RE.search(s or "")
    if not m:
        return None
    dd, mm, yy = m.group(1), m.group(2), m.group(3)
    try:
        return date(int(yy), int(mm), int(dd))
    except ValueError:
        return None


def clean_lines(text: str) -> List[str]:
    lines = []
    for raw in (text or "").splitlines():
        t = " ".join(raw.strip().split())
        if t:
            lines.append(t)
    return lines


def extract_lesson_from_block(block_text: str, current_day: date) -> Optional[Lesson]:
    lines = clean_lines(block_text)
    if not lines:
        return None

    pair = ""
    time_s = ""
    subject = ""
    teacher = ""
    room = ""
    ltype = ""

    for ln in lines:
        pm = PAIR_RE.search(ln)
        if pm:
            pair = pm.group(1).strip()
        tm = TIME_RE.search(ln)
        if tm:
            time_s = f"{tm.group(1)}–{tm.group(2)}"

    # Пробуем распознать поля по смыслу
    for ln in lines:
        low = ln.lower()

        if PAIR_RE.search(ln) or TIME_RE.search(ln):
            continue

        # аудитория
        if low.startswith("ауд") or "ауд." in low:
            v = ln.split(".", 1)[-1].strip() if "." in ln else ln
            room = v if v else ln
            continue

        # тип занятия
        if any(x in low for x in ["практич", "лекци", "семинар", "лаборатор", "зачет", "зачёт", "экзам"]):
            # иногда строка "Тип: ..." — тоже сюда попадёт, это ок
            if ":" in ln and low.startswith("тип"):
                ltype = ln.split(":", 1)[-1].strip()
            else:
                ltype = ln
            continue

        # преподаватель
        if teacher == "" and (low.startswith("преп") or "преп" in low):
            teacher = ln.split(":", 1)[-1].strip() if ":" in ln else ln
            continue

        # если похоже на ФИО — тоже считаем преподавателем
        if teacher == "" and len(ln.split()) >= 2 and any(suf in low for suf in ["вна", "овна", "евна", "ич", "вич"]):
            teacher = ln
            continue

        # предмет
        if subject == "":
            subject = ln

    # добираем room из "Ауд. 318В" если не поймали
    if not room:
        for ln in lines:
            if "ауд" in ln.lower() and any(ch.isdigit() for ch in ln):
                room = ln
                break

    # чистим "Тип:" если он так пришёл
    if ltype.lower().startswith("тип"):
        ltype = ltype.split(":", 1)[-1].strip() if ":" in ltype else ltype

    if not subject and not teacher and not room and not ltype:
        return None

    return Lesson(
        day=current_day,
        pair=pair,
        time=time_s,
        subject=subject,
        teacher=teacher,
        room=room,
        lesson_type=ltype,
    )


def parse_schedule(html: str) -> Dict[date, List[Lesson]]:
    soup = BeautifulSoup(html, "html.parser")

    # Находим даты в документе
    date_nodes: List[Tuple[date, object]] = []
    for text_node in soup.find_all(string=DATE_RE):
        d = parse_date(str(text_node))
        if not d:
            continue
        parent = getattr(text_node, "parent", None)
        if parent is None:
            continue
        date_nodes.append((d, parent))

    # Убираем дубли дат
    seen = set()
    uniq: List[Tuple[date, object]] = []
    for d, node in date_nodes:
        if d in seen:
            continue
        seen.add(d)
        uniq.append((d, node))

    schedule: Dict[date, List[Lesson]] = {}
    if not uniq:
        return schedule

    for idx, (d, node) in enumerate(uniq):
        next_node = uniq[idx + 1][1] if idx + 1 < len(uniq) else None

        blocks: List[str] = []
        cur = node
        while True:
            cur = cur.find_next() if cur else None
            if cur is None:
                break
            if next_node is not None and cur == next_node:
                break

            try:
                t = cur.get_text("\n", strip=True)
            except Exception:
                continue

            if "№ пары" in t or "№пары" in t:
                blocks.append(t)

        lessons: List[Lesson] = []
        for b in blocks:
            lesson = extract_lesson_from_block(b, d)
            if lesson:
                lessons.append(lesson)

        # сортируем по номеру пары
        def k(x: Lesson):
            try:
                return int(x.pair)
            except Exception:
                return 999

        lessons.sort(key=k)
        schedule[d] = lessons

    return schedule


# ---------- Message formatting ----------

def format_message(schedule: Dict[date, List[Lesson]]) -> str:
    now = datetime.now(TZ)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    after_tomorrow = today + timedelta(days=2)

    SEP = "━━━━━━━━━━━━━━"

    def day_title(d: date, title: str) -> str:
        return f"🗓 **{title} · {d.strftime('%d.%m.%Y')}**"

    def format_day(d: date, title: str) -> List[str]:
        lessons = schedule.get(d, [])
        out = [day_title(d, title)]
        if not lessons:
            out.append("— пар нет —")
            return out

        for l in lessons:
            b = pair_badge(l.pair)
            time_s = md_escape((l.time or "").strip())
            subj = md_escape((l.subject or "").strip())
            teacher = md_escape((l.teacher or "").strip())
            room = md_escape((l.room or "").strip())

            out.append("")
            out.append(f"**{b} {time_s}**")
            if subj:
                out.append(f"📚 {subj}")
            if teacher:
                out.append(f"👤 {teacher}")
            if room:
                out.append(f"📍 {room}")
            out.append(type_badge(l.lesson_type))

        return out

    parts: List[str] = []
    parts += format_day(today, "Сегодня")
    parts.append("")
    parts.append(SEP)
    parts.append("")
    parts += format_day(tomorrow, "Завтра")
    parts.append("")
    parts.append(SEP)
    parts.append("")
    parts += format_day(after_tomorrow, "Послезавтра")
    parts.append("")
    parts.append(f"🔄 _Обновлено: {now.strftime('%H:%M')} (МСК)_")
    parts.append("Источник: portal.mguu.ru")

    msg = "\n".join(parts).strip()
    return msg[:4096]


# ---------- Telegram API ----------

def tg_api(token: str, method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data


def get_bot_id(token: str) -> int:
    me = tg_api(token, "getMe", {})
    return int(me["result"]["id"])


def get_pinned_message_id_if_ours(token: str, chat_id: str, bot_id: int) -> Optional[int]:
    """
    Возвращает message_id закрепа, если:
    - закреп есть
    - закреплённое сообщение отправлено этим ботом
    Иначе None.
    """
    chat = tg_api(token, "getChat", {"chat_id": chat_id})
    pinned = chat["result"].get("pinned_message")
    if not pinned:
        return None

    from_obj = pinned.get("from") or {}
    from_id = from_obj.get("id")
    if from_id is None:
        return None

    if int(from_id) != int(bot_id):
        return None

    mid = pinned.get("message_id")
    return int(mid) if mid is not None else None


# ---------- Main ----------

def main() -> None:
    token = os.environ.get("BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()

    if not token or not chat_id:
        raise SystemExit("ENV BOT_TOKEN and CHAT_ID are required")

    # 1) Даты “скользящие”
    start = datetime.now(TZ).date()
    end = start + timedelta(days=45)

    # 2) Парсим сайт
    html = fetch_html(start, end)
    schedule = parse_schedule(html)

    # 3) Формируем красивый Markdown
    text = format_message(schedule)

    # 4) Получаем id бота и проверяем закреп
    bot_id = get_bot_id(token)
    pinned_id = get_pinned_message_id_if_ours(token, chat_id, bot_id)

    if pinned_id:
        # редактируем существующий закреп (наш)
        tg_api(
            token,
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": pinned_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
        )
        return

    # 5) Если закрепа нет или он не наш — отправляем новое и закрепляем
    sent = tg_api(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
    )
    new_id = int(sent["result"]["message_id"])

    # закрепляем (если нет прав, просто не упадём)
    try:
        tg_api(
            token,
            "pinChatMessage",
            {
                "chat_id": chat_id,
                "message_id": new_id,
                "disable_notification": True,
            },
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
