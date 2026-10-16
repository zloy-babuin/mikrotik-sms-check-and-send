import os
import re
import json
import hashlib
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
import paramiko
import datetime

load_dotenv()

host = os.getenv('MIKROTIK_HOST')
username = os.getenv('MIKROTIK_USERNAME')
password = os.getenv('MIKROTIK_PASSWORD')
FILE_NAME = "messages.json"

# Данные для Gmail
GMAIL_USER = os.getenv('GMAIL_USER')
GMAIL_PASSWORD = os.getenv('GMAIL_PASSWORD')
EMAIL_TO = os.getenv('EMAIL_TO')

# Данные для Yandex (первый fallback)
YANDEX_USER = os.getenv('YANDEX_USER')
YANDEX_PASSWORD = os.getenv('YANDEX_PASSWORD')

# Данные для Rambler (второй fallback)
RAMBLER_USER = os.getenv('RAMBLER_USER')
RAMBLER_PASSWORD = os.getenv('RAMBLER_PASSWORD')
RAMBLER_SENDER_NAME = os.getenv('RAMBLER_SENDER_NAME')
RAMBLER_SMTP_SERVER = os.getenv('RAMBLER_SMTP_SERVER')
RAMBLER_SMTP_PORT = os.getenv('RAMBLER_SMTP_PORT')
RAMBLER_SMTP_USE_SSL = os.getenv('RAMBLER_SMTP_USE_SSL')

DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Сколько ждать недостающие части многочастного СМС (от получения первой части)
GROUP_WINDOW = datetime.timedelta(hours=24)

def print_message(message):
    timestamp = datetime.datetime.now().strftime(DATETIME_FORMAT)
    print(f"{timestamp} - {message}")

# Базовая таблица GSM 03.38 (7-битная кодировка) и таблица расширения после ESC (0x1B)
GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXTENSION = {
    0x0A: '\f', 0x14: '^', 0x28: '{', 0x29: '}', 0x2F: '\\',
    0x3C: '[', 0x3D: '~', 0x3E: ']', 0x40: '|', 0x65: '€',
}

def decode_gsm7(data, septet_count, skip_septets=0):
    """Декодирует упакованный 7-битный текст, пропуская septet'ы, занятые UDH"""
    bits = int.from_bytes(data, 'little')
    chars = []
    escape = False
    for i in range(skip_septets, septet_count):
        code = (bits >> (7 * i)) & 0x7F
        if escape:
            chars.append(GSM7_EXTENSION.get(code, ' '))
            escape = False
        elif code == 0x1B:
            escape = True
        else:
            chars.append(GSM7_BASIC[code])
    return "".join(chars)

def dcs_alphabet(dcs):
    """Алфавит по TP-DCS: 0 — GSM-7, 1 — 8 бит, 2 — UCS-2"""
    if dcs & 0x80 == 0:
        return (dcs >> 2) & 0x03
    if dcs & 0xF0 == 0xF0:
        return (dcs >> 2) & 0x01
    if dcs & 0xF0 == 0xE0:
        return 2
    return 0

def parse_pdu(pdu_hex):
    """Разбирает SMS-DELIVER PDU.
    Возвращает dict с ref/total/seq (из UDH) и text (None для 8-битных данных)
    или None, если PDU разобрать не удалось."""
    try:
        data = bytes.fromhex(pdu_hex)
        pos = data[0] + 1                            # SMSC: длина + адрес
        first_octet = data[pos]
        pos += 1
        address_digits = data[pos]
        pos += 2 + (address_digits + 1) // 2         # длина, тип адреса, адрес
        pos += 1                                     # TP-PID
        dcs = data[pos]
        pos += 1
        pos += 7                                     # TP-SCTS
        udl = data[pos]
        pos += 1
        user_data = data[pos:]

        ref, total, seq = None, 1, 1
        udh_size = 0
        if first_octet & 0x40:                       # TP-UDHI: есть заголовок UDH
            udh_size = user_data[0] + 1
            i = 1
            while i < udh_size:
                iei, iel = user_data[i], user_data[i + 1]
                value = user_data[i + 2:i + 2 + iel]
                if iei == 0x00 and iel == 3:         # склейка, 8-битный номер сообщения
                    ref, total, seq = value[0], value[1], value[2]
                elif iei == 0x08 and iel == 4:       # склейка, 16-битный номер сообщения
                    ref, total, seq = (value[0] << 8) | value[1], value[2], value[3]
                i += 2 + iel

        if ref is not None and not (1 <= seq <= total):
            ref, total, seq = None, 1, 1

        alphabet = dcs_alphabet(dcs)
        if alphabet == 2:
            text = user_data[udh_size:udl].decode('utf-16-be', errors='replace')
        elif alphabet == 0:
            # UDL считается в septet'ах, UDH дополняется до целого septet'а
            text = decode_gsm7(user_data, udl, (udh_size * 8 + 6) // 7)
        else:
            text = None

        return {"ref": ref, "total": total, "seq": seq, "text": text}
    except Exception:
        return None

def parse_inbox(output):
    """Разбирает вывод /tool/sms/inbox/print detail в список частей СМС"""
    output = output.replace('\r', '')
    blocks = re.split(r'\n(?=\s*\d+\s+source=)', output)

    parts = []
    for block in blocks:
        if "source=" not in block:
            continue

        phone_match = re.search(r'phone="?([^"\s]+)"?', block)
        timestamp_match = re.search(r'timestamp="([^"]+)"', block)
        msg_match = re.search(r'message="((?:[^"\\]|\\.)*)"', block, re.S)
        pdu_match = re.search(r'pdu="([0-9A-Fa-f\s]+)"', block)

        sender = phone_match.group(1) if phone_match else "Unknown"
        timestamp = timestamp_match.group(1) if timestamp_match else "Unknown"
        # MikroTik переносит длинные значения на новую строку с отступом
        text = re.sub(r'\n\s*', '', msg_match.group(1)) if msg_match else ""
        pdu = re.sub(r'\s', '', pdu_match.group(1)).upper() if pdu_match else ""

        parsed = parse_pdu(pdu) if pdu else None
        ref, total, seq = None, 1, 1
        if parsed:
            ref, total, seq = parsed['ref'], parsed['total'], parsed['seq']
            if parsed['text'] is not None:
                text = parsed['text']

        if pdu:
            part_hash = hashlib.md5(pdu.encode('ascii')).hexdigest()
        else:
            part_hash = hashlib.md5(f"{sender}_{timestamp}_{text}".encode('utf-8')).hexdigest()

        parts.append({
            "sender": sender,
            "timestamp": timestamp,
            "text": text,
            "ref": ref,
            "total": total,
            "seq": seq,
            "hash": part_hash,
        })

    return parts

def send_email_notification(messages_to_send):
    """Отправляет email с СМС через SMTP сервер Gmail, Yandex или Rambler (по очереди при неудаче)"""
    msg = MIMEMultipart()
    msg['To'] = EMAIL_TO
    msg['Subject'] = f"Новые SMS с MikroTik ({len(messages_to_send)} шт.)"

    body = "Обнаружены неотправленные входящие сообщения:\n\n"
    for i, sms in enumerate(messages_to_send, start=1):
        note = f" ({sms['note']})" if sms.get('note') else ""
        body += f"{i}. Отправитель: {sms['sender']}{note}\n"
        body += f"   Время: {sms['timestamp']}\n"
        body += f"   Текст: {sms['text']}\n"
        body += "-" * 50 + "\n"

    msg.attach(MIMEText(body, 'plain', 'utf-8'))

    def _send_via_smtp(smtp_host, smtp_port, use_ssl, user, pwd, from_addr, to_addr, message_obj):
        try:
            # Корректируем заголовок From под конкретный сервер авторизации
            if 'From' in message_obj:
                del message_obj['From']
            message_obj['From'] = from_addr

            if use_ssl:
                server = smtplib.SMTP_SSL(smtp_host, int(smtp_port), timeout=30)
                server.ehlo()
            else:
                server = smtplib.SMTP(smtp_host, int(smtp_port), timeout=30)
                server.ehlo()
                server.starttls()
                server.ehlo()

            server.login(user, pwd)
            server.sendmail(from_addr, to_addr, message_obj.as_string())
            server.quit()
            return True, None
        except Exception as err:
            return False, err

    # Попытка через Gmail
    try:
        ok, err = _send_via_smtp('smtp.gmail.com', 587, False, GMAIL_USER, GMAIL_PASSWORD, GMAIL_USER, EMAIL_TO, msg)
        if ok:
            print_message("Уведомление на email успешно отправлено через Gmail.")
            return True
        else:
            print_message(f"Gmail failed: {err}")
    except Exception as e:
        print_message(f"Gmail exception: {e}")

    # Если Gmail упал, а параметры для Yandex заполнены — пробуем Yandex
    if YANDEX_USER and YANDEX_PASSWORD:
        ok, err = _send_via_smtp('smtp.yandex.ru', 465, True, YANDEX_USER, YANDEX_PASSWORD, YANDEX_USER, EMAIL_TO, msg)
        if ok:
            print_message("Уведомление на email успешно отправлено через Yandex.")
            return True
        else:
            print_message(f"Yandex failed: {err}")

    # Если и Yandex упал, а параметры для Rambler заполнены — пробуем Rambler
    if RAMBLER_USER and RAMBLER_PASSWORD and RAMBLER_SMTP_SERVER and RAMBLER_SMTP_PORT:
        use_ssl = False
        if RAMBLER_SMTP_USE_SSL:
            try:
                use_ssl = RAMBLER_SMTP_USE_SSL.strip().lower() in ('1', 'true', 'yes')
            except Exception:
                use_ssl = False

        # Определяем отправителя: имя из env или сам адрес почты
        rambler_from = RAMBLER_SENDER_NAME if RAMBLER_SENDER_NAME else RAMBLER_USER

        ok, err = _send_via_smtp(RAMBLER_SMTP_SERVER, RAMBLER_SMTP_PORT, use_ssl, RAMBLER_USER, RAMBLER_PASSWORD, rambler_from, EMAIL_TO, msg)
        if ok:
            print_message("Уведомление на email успешно отправлено через Rambler.")
            return True
        else:
            print_message(f"Rambler failed: {err}")

    print_message("Ошибка при отправке почты через все настроенные SMTP-сервера.")
    return False

def sync_sms_to_file(new_parts, file_path):
    """Сохраняет новые части СМС в файл с sent_at = null и received_at = сейчас.
    Части, уже отправленные старой версией скрипта (записи без seq), помечаются отправленными."""
    existing_sms = []

    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                existing_sms = json.load(f)
        except Exception as e:
            print_message(f"Предупреждение: не удалось прочитать {file_path} ({e}).")

    changed = False
    for item in existing_sms:
        if 'sent_at' not in item:
            item['sent_at'] = None
            changed = True

    existing_hashes = {item['hash'] for item in existing_sms if 'hash' in item}
    # Старый формат: одна запись на склеенное по времени сообщение, без ref/total/seq
    legacy_sent = {
        (item['sender'], item['timestamp']): item['sent_at']
        for item in existing_sms
        if 'seq' not in item and item.get('sent_at')
    }

    now_str = datetime.datetime.now().strftime(DATETIME_FORMAT)
    fresh_count = 0
    for sms in new_parts:
        if sms['hash'] in existing_hashes:
            continue
        sms['received_at'] = now_str
        sms['sent_at'] = legacy_sent.get((sms['sender'], sms['timestamp']))
        existing_sms.append(sms)
        existing_hashes.add(sms['hash'])
        changed = True
        if sms['sent_at'] is None:
            fresh_count += 1

    if changed:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(existing_sms, f, ensure_ascii=False, indent=4)
    if fresh_count > 0:
        print_message(f"Добавлено новых частей СМС в базу (ожидают отправки): {fresh_count}")

    return file_path

def _parse_datetime(value):
    try:
        return datetime.datetime.strptime(value, DATETIME_FORMAT)
    except (TypeError, ValueError):
        return datetime.datetime.min

def build_groups(all_sms):
    """Группирует части многочастных СМС по (sender, ref, total).
    Группа живёт GROUP_WINDOW от получения первой части; часть, пришедшая позже
    или с уже имеющимся номером seq, открывает новую группу (ref мог переиспользоваться)."""
    groups = []
    open_groups = {}

    for sms in sorted(all_sms, key=lambda item: _parse_datetime(item.get('received_at'))):
        if sms.get('ref') is None or sms.get('total', 1) <= 1:
            groups.append([sms])
            continue

        key = (sms['sender'], sms['ref'], sms['total'])
        received = _parse_datetime(sms.get('received_at'))
        group = open_groups.get(key)
        if (
            group is None
            or received - _parse_datetime(group[0].get('received_at')) > GROUP_WINDOW
            or any(part['seq'] == sms['seq'] for part in group)
        ):
            group = []
            groups.append(group)
            open_groups[key] = group
        group.append(sms)

    return groups

def render_group(group):
    """Собирает текст СМС из всех имеющихся частей группы и помечает неполные/дополненные"""
    parts = sorted(group, key=lambda item: item.get('seq', 1))
    total = parts[0].get('total', 1)
    text = "".join(part['text'] for part in parts)

    if total > 1:
        have = [part['seq'] for part in parts]
        missing = [n for n in range(1, total + 1) if n not in have]
        if missing:
            text += (
                f" [части {', '.join(map(str, have))} из {total};"
                f" нет: {', '.join(map(str, missing))}]"
            )
    else:
        missing = []

    note = None
    if any(part.get('sent_at') for part in parts):
        note = "дополнено" if missing else "дополнено, собрано полностью"

    return {
        "sender": parts[0]['sender'],
        "timestamp": parts[0]['timestamp'],
        "text": text,
        "note": note,
    }

def process_email_queue(file_path):
    """Отправляет группы, где есть неотправленные части (вместе с уже отправленными частями группы),
    и обновляет статус новых частей в файле"""
    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        return

    with open(file_path, 'r', encoding='utf-8') as f:
        all_sms = json.load(f)

    queue = [
        group for group in build_groups(all_sms)
        if any(part.get('sent_at') is None for part in group)
    ]

    if not queue:
        print_message("Нет сообщений для отправки почты.")
        return

    print_message(f"Найдено сообщений для отправки: {len(queue)}")

    if send_email_notification([render_group(group) for group in queue]):
        now_str = datetime.datetime.now().strftime(DATETIME_FORMAT)
        for group in queue:
            for part in group:
                if part.get('sent_at') is None:
                    part['sent_at'] = now_str

        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(all_sms, f, ensure_ascii=False, indent=4)
        print_message("Статус сообщений в базе обновлен на 'отправлено'.")
    else:
        print_message("Отправка сорвалась. Сообщения попробуют отправиться при следующем запуске скрипта.")

def main():
    if not host or not username or not password:
        raise ValueError("Необходимо заполнить все поля для MikroTik в файле .env")

    if not GMAIL_USER or not GMAIL_PASSWORD or not EMAIL_TO:
        raise ValueError("Необходимо заполнить все поля для Gmail в файле .env")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        ssh.connect(
            hostname=host,
            username=username,
            password=password,
            timeout=10,
            look_for_keys=False,
            allow_agent=False
        )

        stdin, stdout, stderr = ssh.exec_command("/tool/sms/inbox/print detail without-paging")
        output = stdout.read().decode('utf-8', errors='ignore')

        sync_sms_to_file(parse_inbox(output), FILE_NAME)
        process_email_queue(FILE_NAME)

    except Exception as e:
        error_json = json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)
        print(error_json)
    finally:
        ssh.close()

if __name__ == "__main__":
    main()
