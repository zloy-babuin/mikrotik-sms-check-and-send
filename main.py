import os
import re
import json
import hashlib
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
import paramiko

load_dotenv()

host = os.getenv('MIKROTIK_HOST')
username = os.getenv('MIKROTIK_USERNAME')
password = os.getenv('MIKROTIK_PASSWORD')
FILE_NAME = "messages.json"

# Данные для Gmail
GMAIL_USER = os.getenv('GMAIL_USER')
GMAIL_PASSWORD = os.getenv('GMAIL_PASSWORD')
EMAIL_TO = os.getenv('EMAIL_TO')

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

def send_email_notification(new_messages):
    """Отправляет email с новыми СМС через SMTP сервер Gmail"""
    msg = MIMEMultipart()
    msg['From'] = GMAIL_USER
    msg['To'] = EMAIL_TO
    msg['Subject'] = f"Новые SMS с MikroTik ({len(new_messages)} шт.)"

    # Формируем читаемый текст письма
    body = "Обнаружены новые входящие сообщения:\n\n"
    for i, sms in enumerate(new_messages, start=1):
        body += f"{i}. Отправитель: {sms['sender']}\n"
        body += f"   Время: {sms['timestamp']}\n"
        body += f"   Текст: {sms['text']}\n"
        body += f"   Хэш: {sms['hash']}\n"
        body += "-" * 50 + "\n"

    msg.attach(MIMEText(body, 'plain', 'utf-8'))

    try:
        # Подключаемся к SMTP-серверу Gmail (порт 587 с последующим TLS)
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()  # Шифруем соединение
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())
        server.quit()
        print("Уведомление на email успешно отправлено.")
    except Exception as email_err:
        print(f"Ошибка при отправке почты: {email_err}")

def save_and_process_sms(new_sms_list, file_path):
    """Сохраняет уникальные СМС в файл и инициирует отправку почты"""
    existing_sms = []
    existing_hashes = set()

    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                existing_sms = json.load(f)
                existing_hashes = {item['hash'] for item in existing_sms if 'hash' in item}
        except Exception as e:
            print(f"Предупреждение: не удалось прочитать {file_path} ({e}).")

    # Отбираем только те сообщения, которых у нас еще не было
    fresh_sms_detected = []
    for sms in new_sms_list:
        if sms['hash'] not in existing_hashes:
            existing_sms.append(sms)
            existing_hashes.add(sms['hash'])
            fresh_sms_detected.append(sms)

    if fresh_sms_detected:
        # Записываем обновленный массив в файл
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(existing_sms, f, ensure_ascii=False, indent=4)
        print(f"Успешно добавлено новых сообщений в локальный файл: {len(fresh_sms_detected)}")
        
        # Отправляем только действительно новые сообщения на email
        send_email_notification(fresh_sms_detected)
    else:
        print("Новых уникальных сообщений нет. Отправка почты не требуется.")
        
    return existing_sms

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
    
    # Обрабатываем сохранение и отправку уведомлений
    final_file_state = save_and_process_sms(merged_sms_list, FILE_NAME)

except Exception as e:
    error_json = json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)
    print(error_json)
finally:
    ssh.close()
