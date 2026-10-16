# mikrotik-sms-check-and-send

Пересылает входящие SMS с LTE-модема MikroTik на почту.

Раз в 5 минут (cron) скрипт `main.py`:
1. заходит на роутер по **SSH** (`paramiko`) и выполняет `/tool/sms/inbox/print detail without-paging`;
2. разбирает PDU каждой части SMS и сохраняет новые части в `messages.json`;
3. собирает многочастные SMS и отправляет одно письмо со всеми неотправленными сообщениями;
4. пробует каналы по очереди: **Gmail → Yandex → Rambler**. Если не сработал ни один, сообщения остаются в очереди до следующего запуска.

Скрипт делает один проход и завершается, демона нет.

## Поток данных

```
MikroTik inbox ──ssh──▶ parse_inbox / parse_pdu ──▶ messages.json (одна запись = одна часть)
                                                          │
                              build_groups / render_group ◀┘
                                                          │
                                       send_email_notification ──▶ SMTP ──▶ EMAIL_TO
```

### Разбор SMS (`parse_pdu`)
Поле `message` от MikroTik не годится: кириллицу (UCS-2) он показывает как `????`. Поэтому текст берётся из `pdu`, который разбирается строго по полям SMS-DELIVER:
- **SMSC, first octet, адрес, PID, DCS, SCTS, UDL**;
- **UDH**, если в first octet выставлен бит `0x40`. Из IE `0x00` (8-битный номер) или `0x08` (16-битный номер) берутся `ref`, `total` и `seq`: номер сообщения, число частей и номер части;
- **текст по DCS:** UCS-2 → UTF-16BE, GSM-7 → распаковка septet'ов с учётом заполнения после UDH, 8-битные данные → поле `message` от MikroTik.

Если PDU не разобрался, запись становится одночастной с текстом из `message`.

### Сборка частей (`build_groups`, `render_group`)
- **Группа** — это части с одинаковыми `(sender, ref, total)`. Внутри группы части сортируются по `seq` и склеиваются **без разделителя**: оператор режет текст посреди слова.
- **Окно группы — 24 часа** (`GROUP_WINDOW`) от `received_at` первой части. Часть, пришедшая позже, или часть с уже имеющимся `seq` открывает новую группу: `ref` всего 1 байт и переиспользуется.
- **Неполное сообщение уходит сразу** с пометкой `[части 2 из 2; нет: 1]`.
- **Дособирание:** если пришла новая часть уже отправленного сообщения, письмо отправляется ещё раз со **всеми** имеющимися частями. В заголовке пункта стоит пометка `(дополнено)` или `(дополнено, собрано полностью)`. Поэтому сообщение уходит не больше `total` раз.

Inbox роутера вмещает около 14 частей, старые вытесняются. Поэтому части без пары и части, пришедшие в разные запуски, — нормальная ситуация.

## `messages.json`
Это одновременно база и очередь. Файл лежит рядом со скриптом и в git не попадает.

```json
{
    "sender": "RSCHS",
    "timestamp": "2026-10-16 09:55:23+07:00",
    "text": "...",
    "ref": 114, "total": 3, "seq": 1,
    "hash": "md5 от PDU — уникальный ключ части",
    "received_at": "2026-10-16 10:25:07",
    "sent_at": "2026-10-16 10:25:14"
}
```

- `timestamp` берётся от MikroTik, с часовым поясом сети. `received_at` и `sent_at` — локальное время pizero.
- Записи без `seq` остались от старой версии, где части склеивались по времени. Это история, в рассылке они не участвуют. Новая часть, у которой `sender` и `timestamp` совпадают с уже отправленной старой записью, при первом сохранении сразу помечается отправленной.

> ⚠️ **Если `messages.json` потерять, всё, что сейчас лежит в inbox роутера, уйдёт на почту повторно.**

## Настройка (`.env`)

| Переменная | Назначение |
|---|---|
| `MIKROTIK_HOST`, `MIKROTIK_USERNAME`, `MIKROTIK_PASSWORD` | SSH-доступ к роутеру |
| `EMAIL_TO` | куда слать письма (общий для всех каналов) |
| `GMAIL_USER`, `GMAIL_PASSWORD` | основной канал, `smtp.gmail.com:587` STARTTLS, пароль приложения. **Обязательны**, без них скрипт не стартует |
| `YANDEX_USER`, `YANDEX_PASSWORD` | первый fallback, `smtp.yandex.ru:465` SSL, пароль приложения |
| `RAMBLER_USER`, `RAMBLER_PASSWORD`, `RAMBLER_SENDER_NAME`, `RAMBLER_SMTP_SERVER`, `RAMBLER_SMTP_PORT`, `RAMBLER_SMTP_USE_SSL` | второй fallback. Сервер задаётся явно, поэтому годится любой SMTP. `RAMBLER_SENDER_NAME` — адрес в From, должен совпадать с логином |

Шаблон — `.env.example`.

## Локальный запуск и тесты

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env              # заполнить
venv/bin/python main.py           # реальный проход: читает роутер и шлёт почту

venv/bin/python -m unittest discover -s tests -v
```

Тесты используют `tests/fixtures/inbox_2026-10-16.txt`: реальный вывод `print detail` с рассылками МЧС (14 частей, UCS-2, в том числе часть без пары). Сеть и SMTP в тестах не нужны.

## Продакшен

| Что | Где |
|---|---|
| Хост | `pizero`, 192.168.1.4, пользователь `alexey`. Это Raspberry Pi Zero, рядом крутится `pi-bm280-get-send` |
| Доступ | из локальной сети `ssh alexey@192.168.1.4`, иначе через jump-хост: `ssh -J pi alexey@192.168.1.4` |
| Код | `/opt/mikrotik-sms-check-and-send` (git clone), venv `venv/` |
| Конфиг | `/opt/mikrotik-sms-check-and-send/.env`, владелец root, права 700 |
| Запуск | root crontab (`sudo crontab -l`): `*/5 * * * * /opt/mikrotik-sms-check-and-send/start.sh >> /var/log/sms-check.log 2>&1` |
| Лог | `/var/log/sms-check.log`, ротация в `/etc/logrotate.d/mikrotik-sms` |
| База | `/opt/mikrotik-sms-check-and-send/messages.json`, пишет root |

Скрипт запускается от root, а рабочая копия принадлежит `alexey`, поэтому `git` выполняется от `alexey`, без sudo.

### Обновление
Перезапускать ничего не нужно: следующий запуск cron возьмёт новый код. После изменения `requirements.txt` нужно выполнить `venv/bin/pip install -r requirements.txt`.

**GitHub с pizero закрыт** (`Connection refused`). Обычный `git pull` работает только через прокси, если он жив:

```bash
cd /opt/mikrotik-sms-check-and-send
ALL_PROXY=socks5h://192.168.1.26:2080 git pull
```

Надёжный путь — **git bundle с рабочей машины**. Коммиты те же, что в GitHub, история не расходится:

```bash
# на рабочей машине, в клоне с запушенными изменениями
OLD=$(ssh -J pi alexey@192.168.1.4 'git -C /opt/mikrotik-sms-check-and-send rev-parse HEAD')
git bundle create /tmp/m.bundle $OLD..main
scp -J pi /tmp/m.bundle alexey@192.168.1.4:/tmp/m.bundle
ssh -J pi alexey@192.168.1.4 'cd /opt/mikrotik-sms-check-and-send \
  && git pull --ff-only /tmp/m.bundle main \
  && git fetch /tmp/m.bundle main:refs/remotes/origin/main \
  && rm /tmp/m.bundle && git log --oneline -1'
```

Чтобы проверить новую версию, не дожидаясь cron, запускайте её не около минут, кратных 5: иначе запуск пересечётся с cron и письмо может уйти дважды.

```bash
sudo /opt/mikrotik-sms-check-and-send/start.sh | sudo tee -a /var/log/sms-check.log
```

## Диагностика

```bash
sudo tail -30 /var/log/sms-check.log
sudo python3 -c "import json;d=json.load(open('/opt/mikrotik-sms-check-and-send/messages.json'));print('в очереди:',sum(x['sent_at'] is None for x in d))"
```

Известные проблемы (на 16.10.2026):
- **Gmail:** `[Errno 101] Network is unreachable`. Google целиком недоступен из сети pizero: на IPv4 соединение отклоняется, IPv6-маршрута нет. Письма уходят через Yandex, а на попытку Gmail каждый запуск тратит около 5 секунд.
- **Rambler:** `Connection unexpectedly closed` на `login`. Rambler отклоняет пароль от ящика, а создать пароль приложения не даёт: возвращает 500. Фолбэк фактически не работает.

Проверить SMTP-логин без отправки письма:

```bash
cd /opt/mikrotik-sms-check-and-send && sudo venv/bin/python - <<'EOF'
import smtplib
from dotenv import dotenv_values
e = dotenv_values(".env")
s = smtplib.SMTP_SSL("smtp.yandex.ru", 465, timeout=20); s.ehlo()
print(s.login(e["YANDEX_USER"], e["YANDEX_PASSWORD"])); s.quit()
EOF
```

Посмотреть, что лежит в inbox роутера (только чтение):

```bash
ssh <MIKROTIK_USERNAME>@<MIKROTIK_HOST> '/tool/sms/inbox/print detail without-paging'
```

## Файлы
- `main.py` — весь код: разбор, хранение, сборка, отправка.
- `start.sh` — активирует `venv` и запускает `main.py`; его вызывает cron.
- `tests/` — unittest и фикстура реального inbox.
- `.env.example` — шаблон конфигурации.
- `.continue/` — настройки IDE-ассистента, к работе скрипта отношения не имеют.
