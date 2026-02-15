import os
import re
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup

TZ = ZoneInfo("Europe/Moscow")

BASE_SCHEDULE_URL = "https://portal.mguu.ru/student/scheduler1.php?groupid=000000213&groupname=23%D0%93%D0%9C%D0%A3-%D0%A3%D0%93%D0%A511.2&startDate=16.12.2025&endDate=31.01.2026#schedule"


def tg_call(method: str, token: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error {method}: {data}")
    return data["result"]


def build_schedule_url() -> str:
    today = datetime.now(TZ).date()
    end = today + timedelta(days=45)

    start_str = today.strftime("%d.%m.%Y")
    end_str = end.strftime("%d.%m.%Y")

    u = urlparse(BASE_SCHEDULE_URL)
    q = parse_qs(u.query)

    q["startDate"] = [start_str]
    q["endDate"] = [end_str]

    new_query = urlencode(q, doseq=True)
    new_u = u._replace(query=new_query)
    return urlunparse(new_u)


def fetch_page_text(url: str) -> str:
    r = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; SchedulePinnedBot/1.0)"},
        timeout=30,
    )
    r.raise_for_status()
    if not r.encoding:
        r.encoding = r.apparent_encoding or "utf-8"

    soup = BeautifulSoup(r.text, "html.parser")
    return soup.get_text("\n", strip=True)


def parse_schedule(text: str) -> dict[date, list[dict]]:
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    date_re = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
    pair_re = re.compile(r"^№\s*пары\s*-\s*(\d+)\s*$")
    time_re = re.compile(r"^\d{2}:\d{2}\s*-\s*\d{2}:\d{2}$")

    def clean(s: str) -> str:
        s = s.strip()
        if s.startswith("|"):
            s = s[1:].strip()
        return s

    out: dict[date, list[dict]] = {}
    cur_date: date | None = None
    i = 0

    while i < len(lines):
        ln = lines[i]

        if date_re.match(ln):
            dd, mm, yyyy = ln.split(".")
            cur_date = date(int(yyyy), int(mm), int(dd))
            out.setdefault(cur_date, [])
            i += 1
            continue

        m = pair_re.match(ln)
        if cur_date and m:
            pair_num = m.group(1)

            time_str = ""
            j = i + 1
            if j < len(lines) and time_re.match(lines[j]):
                time_str = lines[j].replace(" - ", "–")
                j += 1

            payload = []
            while j < len(lines):
                if date_re.match(lines[j]) or pair_re.match(lines[j]):
                    break
                payload.append(clean(lines[j]))
                if len(payload) >= 6:
                    break
                j += 1

            payload = [p for p in payload if p]
            subject = payload[0] if len(payload) > 0 else ""
            teacher = payload[1] if len(payload) > 1 else ""
            room = payload[2] if len(payload) > 2 else ""
            ltype = payload[3] if len(payload) > 3 else ""

            out[cur_date].append(
                {
                    "pair": pair_num,
                    "time": time_str,
                    "subject": subject,
                    "teacher": teacher,
                    "room": room,
                    "type": ltype,
                }
            )

            i = j
            continue

        i += 1

    out = {d: lessons for d, lessons in out.items() if lessons}
    return out


def format_message(schedule: dict[date, list[dict]]) -> str:
    now = datetime.now(TZ)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    after_tomorrow = today + timedelta(days=2)

    def format_day(d: date, title: str) -> list[str]:
        lessons = schedule.get(d, [])
        block = [f"🗓 {title} ({d.strftime('%d.%m.%Y')})", ""]
        if not lessons:
            block.append("— пар нет —")
            block.append("")
            return block

        for l in lessons:
            line1 = f"{l['pair']}) {l['time']}".strip()
            block.append(line1)

            if l["subject"]:
                block.append(l["subject"])
            if l["teacher"]:
                block.append(f"Преп.: {l['teacher']}")
            if l["room"]:
                block.append(f"{l['room']}")
            if l["type"]:
                block.append(f"Тип: {l['type']}")
            block.append("")
        return block

    parts = []
    parts += format_day(today, "Сегодня")
    parts += format_day(tomorrow, "Завтра")
    parts += format_day(after_tomorrow, "Послезавтра")
    parts.append(f"🔄 Обновлено: {now.strftime('%H:%M')} (МСК)")
    parts.append("Источник: portal.mguu.ru")

    msg = "\n".join(parts).strip()
    return msg[:4096]


def main():
    token = os.environ.get("BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()
print("DEBUG CHAT_ID =", chat_id)

    if not token or not chat_id:
        raise SystemExit("Нужно задать BOT_TOKEN и CHAT_ID в переменных окружения.")

    url = build_schedule_url()
    page_text = fetch_page_text(url)
    schedule = parse_schedule(page_text)
    message_text = format_message(schedule)

    me = tg_call("getMe", token, {})
    bot_id = me["id"]

    chat = tg_call("getChat", token, {"chat_id": chat_id})
    pinned = chat.get("pinned_message")

    if pinned and pinned.get("from", {}).get("id") == bot_id:
        msg_id = pinned["message_id"]
        tg_call(
            "editMessageText",
            token,
            {"chat_id": chat_id, "message_id": msg_id, "text": message_text, "disable_web_page_preview": True},
        )
        print("DEBUG edited pinned message_id =", msg_id)
        return

    sent = tg_call(
        "sendMessage",
        token,
        {"chat_id": chat_id, "text": message_text, "disable_web_page_preview": True},
    )
print("DEBUG sendMessage result message_id =", sent.get("message_id"))
    msg_id = sent["message_id"]
    tg_call(
        "pinChatMessage",
        token,
        {"chat_id": chat_id, "message_id": msg_id, "disable_notification": True},
    )


if __name__ == "__main__":
    main()
