import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import main  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'inbox_2026-10-16.txt')


def load_inbox():
    with open(FIXTURE, encoding='utf-8') as f:
        return main.parse_inbox(f.read())


def gsm7_pdu(text, udh=b''):
    """Собирает SMS-DELIVER PDU в GSM-7 (только базовая таблица) с необязательным UDH"""
    fill = (len(udh) * 8 + 6) // 7 if udh else 0
    septets = [main.GSM7_BASIC.index(ch) for ch in text]
    bits = 0
    for i, code in enumerate(septets):
        bits |= code << (7 * (fill + i))
    udl = fill + len(septets)
    body = bits.to_bytes((udl * 7 + 7) // 8, 'little')
    if udh:
        body = udh + body[len(udh):]
    first_octet = 0x44 if udh else 0x04
    header = bytes([0x00, first_octet, 0x04, 0x91, 0x21, 0x43, 0x00, 0x00]) + bytes(7)
    return (header + bytes([udl]) + body).hex().upper()


class ParsePduTest(unittest.TestCase):
    def test_gsm7_reference_pdu(self):
        parsed = main.parse_pdu('07911326040000F0040B911346610089F60000208062917314080CC8F71D14969741F977FD07')
        self.assertEqual(parsed, {"ref": None, "total": 1, "seq": 1, "text": "How are you?"})

    def test_gsm7_with_udh_skips_fill_bits(self):
        parsed = main.parse_pdu(gsm7_pdu("Hello world", bytes.fromhex('050003AA0302')))
        self.assertEqual((parsed['ref'], parsed['total'], parsed['seq']), (0xAA, 3, 2))
        self.assertEqual(parsed['text'], "Hello world")

    def test_16bit_reference(self):
        parsed = main.parse_pdu(gsm7_pdu("Hi", bytes.fromhex('06080412340201')))
        self.assertEqual((parsed['ref'], parsed['total'], parsed['seq']), (0x1234, 2, 1))
        self.assertEqual(parsed['text'], "Hi")

    def test_garbage_returns_none(self):
        self.assertIsNone(main.parse_pdu('07'))


class ParseInboxTest(unittest.TestCase):
    def test_real_inbox(self):
        parts = load_inbox()
        self.assertEqual(len(parts), 14)
        self.assertEqual(len({part['hash'] for part in parts}), 14)
        self.assertTrue(all(part['sender'] == 'RSCHS' for part in parts))
        self.assertEqual([(p['ref'], p['seq']) for p in parts if p['ref'] == 114], [(114, 1), (114, 2), (114, 3)])


class GroupingTest(unittest.TestCase):
    def setUp(self):
        self.parts = load_inbox()
        for part in self.parts:
            part['received_at'] = '2026-10-16 10:00:00'
            part['sent_at'] = None

    def rendered(self):
        return {g[0]['ref']: main.render_group(g) for g in main.build_groups(self.parts)}

    def test_full_messages_glued_without_separator(self):
        messages = self.rendered()
        self.assertEqual(len(messages), 7)
        self.assertEqual(
            messages[253]['text'],
            "Внимание! На территории Республики Татарстан режим «Ракетная опасность» отменен.",
        )
        self.assertTrue(messages[114]['text'].endswith("Необходимо принять меры безопасности."))
        self.assertIsNone(messages[114]['note'])

    def test_orphan_part_marked_missing(self):
        text = self.rendered()[41]['text']
        self.assertTrue(text.startswith(" опасность»"))
        self.assertTrue(text.endswith("[части 2 из 2; нет: 1]"))

    def test_late_parts_resend_whole_message(self):
        message = [p for p in self.parts if p['ref'] == 114]
        message[0]['sent_at'] = '2026-10-16 10:00:00'
        message[1]['received_at'] = message[2]['received_at'] = '2026-10-16 10:05:00'

        groups = [g for g in main.build_groups(self.parts) if g[0]['ref'] == 114]
        self.assertEqual(len(groups), 1)
        rendered = main.render_group(groups[0])
        self.assertTrue(rendered['text'].startswith("Внимание! Угроза атаки БПЛА"))
        self.assertTrue(rendered['text'].endswith("меры безопасности."))
        self.assertEqual(rendered['note'], "дополнено, собрано полностью")

    def test_partial_resend_still_incomplete(self):
        message = [p for p in self.parts if p['ref'] == 114]
        message[0]['sent_at'] = '2026-10-16 10:00:00'
        self.parts.remove(message[2])

        rendered = main.render_group([p for p in self.parts if p['ref'] == 114])
        self.assertEqual(rendered['note'], "дополнено")
        self.assertTrue(rendered['text'].endswith("[части 1, 2 из 3; нет: 3]"))

    def test_part_after_window_opens_new_group(self):
        message = [p for p in self.parts if p['ref'] == 114]
        message[0]['sent_at'] = message[1]['sent_at'] = '2026-10-16 10:00:00'
        message[2]['received_at'] = '2026-10-17 10:00:01'

        groups = [g for g in main.build_groups(self.parts) if g[0]['ref'] == 114]
        self.assertEqual([len(g) for g in groups], [2, 1])
        self.assertTrue(main.render_group(groups[1])['text'].endswith("[части 3 из 3; нет: 1, 2]"))

    def test_repeated_seq_opens_new_group(self):
        duplicate = dict(next(p for p in self.parts if p['ref'] == 253 and p['seq'] == 1), hash='other')
        duplicate['received_at'] = '2026-10-16 11:00:00'
        self.parts.append(duplicate)

        groups = [g for g in main.build_groups(self.parts) if g[0]['ref'] == 253]
        self.assertEqual([len(g) for g in groups], [2, 1])


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'messages.json')

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_sent_records_are_not_resent(self):
        legacy = [
            {"sender": "RSCHS", "timestamp": "2026-10-16 09:55:23+07:00", "text": "старый формат",
             "hash": "legacy1", "sent_at": "2026-10-16 10:05:14"},
        ]
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(legacy, f)

        main.sync_sms_to_file(load_inbox(), self.path)
        with open(self.path, encoding='utf-8') as f:
            stored = json.load(f)

        new_parts = [item for item in stored if 'seq' in item]
        self.assertEqual(len(new_parts), 14)
        migrated = [p for p in new_parts if p['timestamp'] == "2026-10-16 09:55:23+07:00"]
        self.assertEqual([p['sent_at'] for p in migrated], ["2026-10-16 10:05:14"] * 3)
        self.assertEqual(sum(1 for p in new_parts if p['sent_at'] is None), 11)

    def test_queue_sends_and_marks_parts(self):
        main.sync_sms_to_file(load_inbox(), self.path)
        with mock.patch.object(main, 'send_email_notification', return_value=True) as send:
            main.process_email_queue(self.path)
        self.assertEqual(len(send.call_args.args[0]), 7)

        with open(self.path, encoding='utf-8') as f:
            self.assertTrue(all(p['sent_at'] for p in json.load(f)))

        # Повторный проход с тем же inbox ничего не отправляет
        main.sync_sms_to_file(load_inbox(), self.path)
        with mock.patch.object(main, 'send_email_notification') as send:
            main.process_email_queue(self.path)
        send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
