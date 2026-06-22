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

# Данные для Rambler (fallback)
RAMBLER_USER = os.getenv('RAMBLER_USER')
RAMBLER_PASSWORD = os.getenv('RAMBLER_PASSWORD')
RAMBLER_SENDER_NAME = os.getenv('RAMBLER_SENDER_NAME')
RAMBLER_SMTP_SERVER = os.getenv('RAMBLER_SMTP_SERVER')
RAMBLER_SMTP_PORT = os.getenv('RAMBLER_SMTP_PORT')
RAMBLER_SMTP_USE_SSL = os.getenv('RAMBLER_SMTP_USE_SSL')

def print_message(message):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{timestamp} - {message}")

if not host or not username or not password:
    raise ValueError("Необходимо заполнить все поля для MikroTik в файле .env")

if not GMAIL_USER or not GMAIL_PASSWORD or not EMAIL_TO:
    raise ValueError("Необходимо заполнить все поля для Gmail в файле .env")

def decode_ucs2_pdu(pdu_hex):
    """Декодирует кириллицу из сырого PDU (UCS-2 / UTF-16BE)"""
    try:
        if not pdu_hex or len(pdu_hex) < 30:
            return None
        
        idx = pdu_hex.lower().find("0008")
        if idx == -1:
            return None
        
        text_data = pdu_hex[idx + 20:]
        
        if text_data.startswith("050003") or text_data.startswith("060804"):
            udh_len = int(text_data[:2], 16)
            text_data = text_data[(udh_len + 1) * 2:]
            
        bytes_data = bytes.fromhex(text_data)
        return bytes_data.decode('utf-16-be', errors='ignore').strip()
    except Exception:
        return None

def merge_segmented_sms(sms_list):
    """Склеивает сегментированные СМС от конца к началу при совпадении sender и timestamp"""
    for i in range(len(sms_list) - 1, 0, -1):
        current_sms = sms_list[i]
        previous_sms = sms_list[i - 1]
        
        if current_sms['sender'] == previous_sms['sender'] and current_sms['timestamp'] == previous_sms['timestamp']:
            previous_sms['text'] += " " + current_sms['text']
            sms_list.pop(i)
            
    return sms_list

def generate_hash(sms_dict):
    """Генерирует MD5 хэш на основе отправителя, времени и текста СМС"""
    hash_string = f"{sms_dict['sender']}_{sms_dict['timestamp']}_{sms_dict['text']}"
    return hashlib.md5(hash_string.encode('utf-8')).hexdigest()

def send_email_notification(messages_to_send):
    """Отправляет email с СМС через SMTP сервер Gmail или Rambler (в случае неудачи)"""
    msg = MIMEMultipart()
    msg['To'] = EMAIL_TO
    msg['Subject'] = f"Новые SMS с MikroTik ({len(messages_to_send)} шт.)"

    body = "Обнаружены неотправленные входящие сообщения:\n\n"
    for i, sms in enumerate(messages_to_send, start=1):
        body += f"{i}. Отправитель: {sms['sender']}\n"
        body += f"   Время: {sms['timestamp']}\n"
        body += f"   Текст: {sms['text']}\n"
        body += f"   Хэш: {sms['hash']}\n"
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

    # Если Gmail упал, а параметры для Rambler заполнены — пробуем Rambler
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

def sync_sms_to_file(new_sms_list, file_path):
    """Сохраняет новые уникальные СМС в файл с sent_at = null.
    Для старых записей без флага sent_at форсирует его в null."""
    existing_sms = []
    existing_hashes = set()

    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                existing_sms = json.load(f)
                
                for item in existing_sms:
                    if 'sent_at' not in item:
                        item['sent_at'] = None
                        
                existing_hashes = {item['hash'] for item in existing_sms if 'hash' in item}
        except Exception as e:
            print_message(f"Предупреждение: не удалось прочитать {file_path} ({e}).")

    fresh_count = 0
    for sms in new_sms_list:
        if sms['hash'] not in existing_hashes:
            sms['sent_at'] = None
            existing_sms.append(sms)
            existing_hashes.add(sms['hash'])
            fresh_count += 1

    if fresh_count > 0 or any(item['sent_at'] is None for item in existing_sms):
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(existing_sms, f, ensure_ascii=False, indent=4)
        if fresh_count > 0:
            print_message(f"Добавлено новых СМС в базу (ожидают отправки): {fresh_count}")
            
    return file_path

def process_email_queue(file_path):
    """Ищет в файле все сообщения с sent_at == null, отправляет их и обновляет статус в файле"""
    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        return

    with open(file_path, 'r', encoding='utf-8') as f:
        all_sms = json.load(f)

    queue = [sms for sms in all_sms if sms.get('sent_at') is None]

    if not queue:
        print_message("Нет сообщений для отправки почты.")
        return

    print_message(f"Найдено сообщений для отправки: {len(queue)}")
    
    if send_email_notification(queue):
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sent_hashes = {sms['hash'] for sms in queue}
        
        for sms in all_sms:
            if sms['hash'] in sent_hashes:
                sms['sent_at'] = now_str
                
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(all_sms, f, ensure_ascii=False, indent=4)
        print_message("Статус сообщений в базе обновлен на 'отправлено'.")
    else:
        print_message("Отправка сорвалась. Сообщения попробуют отправиться при следующем запуске скрипта.")

# --- Основной цикл выполнения ---
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
    
    output_cleaned = "\n".join([line.strip() for line in output.splitlines()])
    blocks = re.split(r'\n*(?=\d+\s+source=)', output_cleaned)
    
    sms_features_list = []
    
    for block in blocks:
        if "source=" not in block:
            continue
            
        phone_match = re.search(r'phone="?([^"\n\s]+)"?', block)
        timestamp_match = re.search(r'timestamp="?([^"\n]+)"?', block)
        msg_match = re.search(r'message="?([^"\n]+)"?', block)
        pdu_match = re.search(r'pdu="?([A-F0-9]+)"?', block)
        
        sender = phone_match.group(1) if phone_match else "Unknown"
        timestamp = timestamp_match.group(1) if timestamp_match else "Unknown"
        text = msg_match.group(1) if msg_match else ""
        pdu = pdu_match.group(1) if pdu_match else ""
        
        if pdu:
            pdu_lines = re.findall(r'\b[A-F0-9]{20,}\b', block)
            if pdu_lines:
                pdu = "".join(pdu_lines)
        
        if ("?" in text or not text) and pdu:
            decoded_text = decode_ucs2_pdu(pdu)
            if decoded_text:
                text = decoded_text
        
        sms_data = {
            "sender": sender,
            "timestamp": timestamp,
            "text": text
        }
        sms_features_list.append(sms_data)
        
    merged_sms_list = merge_segmented_sms(sms_features_list)
    
    for sms in merged_sms_list:
        sms['hash'] = generate_hash(sms)
    
    sync_sms_to_file(merged_sms_list, FILE_NAME)
    process_email_queue(FILE_NAME)

except Exception as e:
    error_json = json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)
    print(error_json)
finally:
    ssh.close()