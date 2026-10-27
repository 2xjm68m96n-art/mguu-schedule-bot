import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Europe/Moscow')
API = 'https://api.lk-upm.ru/api'
GROUP_ID = '000000213'
DAYS = 4


def request_json(url, payload=None):
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    headers = {
        'User-Agent': 'MGUUScheduleBot/2.0',
        'Accept': 'application/json',
    }

    if data is not None:
        headers['Content-Type'] = 'application/json'

    try:
        with urlopen(
            Request(url, data=data, headers=headers),
            timeout=60,
        ) as response:
            result = json.load(response)
    except HTTPError as error:
        raise RuntimeError(
            f'Источник ответил HTTP {error.code}'
        ) from None
    except (URLError, TimeoutError, ValueError):
        raise RuntimeError(
            'Не удалось получить данные сервиса'
        ) from None

    if not isinstance(result, dict) or result.get('ok') is False:
        raise RuntimeError('Сервис вернул ошибку или неожиданный ответ')

    return result


def tg_call(method, token, payload):
    data = request_json(
        f'https://api.telegram.org/bot{token}/{method}',
        payload,
    )

    if not data.get('ok'):
        raise RuntimeError('Ошибка Telegram API')

    return data['result']


def parse_date(value):
    return datetime.strptime(value, '%d.%m.%Y').date()


def subject_key(value):
    match = re.match(
        r'^(?:лекция|практика|практическое занятие|семинар)'
        r'\s*["«](.+?)["»]',
        value,
        re.I,
    )

    value = match.group(1) if match else value
    return re.sub(r'[^a-zа-яё0-9]+', ' ', value.lower()).strip()


def fetch_schedule():
    data = request_json(f'{API}/schedule-by-id/{GROUP_ID}')

    if (
        data.get('group', {}).get('id') != GROUP_ID
        or not isinstance(data.get('lessons'), list)
    ):
        raise RuntimeError(
            'Неверная группа или структура расписания; закреп не изменён'
        )

    if data.get('stale'):
        raise RuntimeError(
            'Сайт пометил расписание как устаревшее; закреп не изменён'
        )

    if not data['lessons']:
        raise RuntimeError(
            'Источник вернул полностью пустое расписание; закреп не изменён'
        )

    fields = (
        'date',
        'number',
        'time',
        'subject',
        'teacher',
        'room',
        'type',
        'subgroup',
    )

    lessons = []
    seen = set()

    for item in data['lessons']:
        for key in fields:
            if not isinstance(item.get(key), str):
                raise RuntimeError(
                    f'Изменилась структура расписания: {key}'
                )

        parse_date(item['date'])
        signature = tuple(item[key] for key in fields)

        if signature not in seen:
            seen.add(signature)
            lessons.append(item)

    return lessons


def fetch_control_points(today, student_id):
    year_id = os.environ.get(
        'ACADEMIC_YEAR_ID',
        '000000033',
    ).strip()

    semester = '1' if 2 <= today.month <= 8 else '0'
    query = urlencode({'year': year_id, 'sem': semester})

    detail = request_json(
        f'{API}/rating/detail/'
        f'{quote(student_id, safe="")}?{query}'
    )

    if not isinstance(detail.get('rows'), list):
        raise RuntimeError('Изменилась структура источника КТ')

    rows = [
        row
        for row in detail['rows']
        if row.get('subjectURL')
    ]

    def load_subject(row):
        query = urlencode({'url': row['subjectURL']})
        return request_json(f'{API}/rating/subject?{query}')

    with ThreadPoolExecutor(max_workers=3) as pool:
        subjects = list(pool.map(load_subject, rows))

    result = {}

    for row, subject in zip(rows, subjects):
        if not isinstance(subject.get('points'), list):
            raise RuntimeError(
                'Изменилась структура контрольных точек'
            )

        for point in subject['points']:
            try:
                day = parse_date(point.get('date', ''))
            except (ValueError, TypeError):
                continue

            if today <= day < today + timedelta(days=DAYS):
                key = (subject_key(row['subject']), day)
                title = str(point.get('title', '')).strip()

                if title and title not in result.setdefault(key, []):
                    result[key].append(title)

    return result


def format_message(lessons, points, today, now, kt_enabled):
    labels = [
        'Сегодня',
        'Завтра',
        'Послезавтра',
        'Через 3 дня',
    ]

    parts = []

    for offset, title in enumerate(labels):
        day = today + timedelta(days=offset)

        parts.extend([
            f'🗓 {title} ({day:%d.%m.%Y})',
            '',
        ])

        items = sorted(
            (
                item
                for item in lessons
                if parse_date(item['date']) == day
            ),
            key=lambda item: item['time'],
        )

        if not items:
            parts.extend(['— пар нет —', ''])

        for item in items:
            time_text = re.sub(
                r'\s*-\s*',
                '–',
                item['time'],
            )

            parts.extend([
                f"{item['number']}) {time_text}",
                item['subject'],
            ])

            if item['teacher']:
                parts.append(f"Преп.: {item['teacher']}")

            if item['room']:
                parts.append(item['room'])

            if item['type']:
                parts.append(f"Тип: {item['type']}")

            if item['subgroup']:
                parts.append(f"Подгруппа: {item['subgroup']}")

            key = (subject_key(item['subject']), day)

            for point in points.get(key, []):
                parts.append(f'⚠️ КТ · {point}')

            parts.append('')

    if not kt_enabled:
        parts.append('ℹ️ КТ пока не подключены')

    parts.extend([
        f'🔄 Обновлено: {now:%H:%M} (МСК)',
        'Источник: lk-upm.ru',
    ])

    message = '\n'.join(parts).strip()

    if len(message.encode('utf-16-le')) // 2 > 4096:
        raise RuntimeError(
            'Расписание длиннее лимита Telegram. '
            'Закреп сохранён без изменений; '
            'требуется разделение сообщений'
        )

    return message


def main():
    token = os.environ.get('BOT_TOKEN', '').strip()
    chat_id = os.environ.get('CHAT_ID', '').strip()
    student_id = os.environ.get('KT_STUDENT_ID', '').strip()

    if not token or not chat_id:
        raise SystemExit(
            'Задайте BOT_TOKEN и CHAT_ID в GitHub Secrets'
        )

    now = datetime.now(TZ)
    lessons = fetch_schedule()

    points = (
        fetch_control_points(now.date(), student_id)
        if student_id
        else {}
    )

    text = format_message(
        lessons,
        points,
        now.date(),
        now,
        bool(student_id),
    )

    bot_id = tg_call('getMe', token, {})['id']

    chat = tg_call(
        'getChat',
        token,
        {'chat_id': chat_id},
    )

    pinned = chat.get('pinned_message')

    if pinned and pinned.get('from', {}).get('id') == bot_id:
        if pinned.get('text') != text:
            tg_call(
                'editMessageText',
                token,
                {
                    'chat_id': chat_id,
                    'message_id': pinned['message_id'],
                    'text': text,
                    'disable_web_page_preview': True,
                },
            )

        print('Закреп обновлён')
        return

    sent = tg_call(
        'sendMessage',
        token,
        {
            'chat_id': chat_id,
            'text': text,
            'disable_web_page_preview': True,
        },
    )

    tg_call(
        'pinChatMessage',
        token,
        {
            'chat_id': chat_id,
            'message_id': sent['message_id'],
            'disable_notification': True,
        },
    )

    print('Сообщение отправлено и закреплено')


if __name__ == '__main__':
    main()
