import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp import ClientSession, web
from calls import Calls
from history import CallHistory
from registration import Registration
from screening import CallScreening, in_window
from server import create_app
from test_calls import FakeMedia, FakeRegistration
from test_media import SDP


class Clock:
    def __init__(self, when):
        self.now = datetime(*when).timestamp()

    def __call__(self):
        return self.now


class ScreeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'screening.json'
        self.clock = Clock((2026, 11, 15, 12, 0))
        self.screen = CallScreening(self.path, self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def test_block_matches_any_format_and_persists(self):
        self.screen.block('+91 98765 43210')
        self.assertEqual(self.screen.decide('09876543210'), 'blocked')
        self.assertEqual(CallScreening(self.path, self.clock).decide('9876543210'), 'blocked')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.screen.unblock('9876543210'))
        self.assertIsNone(self.screen.decide('9876543210'))
        with self.assertRaises(ValueError):
            self.screen.block('12')

    def test_manual_and_timed_dnd(self):
        self.assertIsNone(self.screen.decide('9876543210'))
        self.screen.update_dnd({'enabled': True, 'repeatCallers': False})
        self.assertEqual(self.screen.decide('9876543210'), 'silenced')
        self.assertEqual(self.screen.dnd_status()['activeReason'], 'manual')
        self.screen.update_dnd({'minutes': 60})
        status = self.screen.dnd_status()
        self.assertEqual((status['active'], status['activeReason']), (True, 'timer'))
        self.clock.now += 3601
        self.assertFalse(self.screen.dnd_status()['active'])
        self.assertIsNone(self.screen.decide('9876543210'))
        with self.assertRaises(ValueError):
            self.screen.update_dnd({'minutes': 0})

    def test_schedule_crosses_midnight(self):
        self.assertTrue(in_window(23 * 60, 22 * 60, 7 * 60))
        self.assertTrue(in_window(6 * 60 + 59, 22 * 60, 7 * 60))
        self.assertFalse(in_window(7 * 60, 22 * 60, 7 * 60))
        self.assertTrue(in_window(13 * 60, 12 * 60, 14 * 60))
        self.screen.update_dnd({'scheduleEnabled': True, 'start': '22:00', 'end': '07:00', 'repeatCallers': False})
        self.assertFalse(self.screen.dnd_status()['active'])
        self.clock.now = datetime(2026, 11, 15, 23, 30).timestamp()
        status = self.screen.dnd_status()
        self.assertEqual(status['activeReason'], 'schedule')
        self.assertEqual(datetime.fromtimestamp(status['activeUntil']), datetime(2026, 11, 16, 7, 0))
        self.screen.update_dnd({'enabled': False})  # off for tonight only
        self.assertFalse(self.screen.dnd_status()['active'])
        self.clock.now = datetime(2026, 11, 16, 22, 30).timestamp()
        self.assertEqual(self.screen.dnd_status()['activeReason'], 'schedule')
        with self.assertRaises(ValueError):
            self.screen.update_dnd({'start': '25:00'})

    def test_repeated_caller_gets_through(self):
        self.screen.update_dnd({'enabled': True})
        self.assertEqual(self.screen.decide('9876543210'), 'silenced')
        self.clock.now += 60
        self.assertIsNone(self.screen.decide('+919876543210'))
        self.assertEqual(self.screen.decide('9123456789'), 'silenced')
        self.clock.now += 600
        self.assertEqual(self.screen.decide('9123456789'), 'silenced')


class ScreenedIncomingCallTests(unittest.IsolatedAsyncioTestCase):
    async def invite(self, verdict_setup):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        history = CallHistory(Path(temp.name) / 'history.json')
        screen = CallScreening(Path(temp.name) / 'screening.json')
        verdict_setup(screen)
        reg = FakeRegistration()
        calls = Calls(reg, FakeMedia, history=history, screening=screen)
        headers = {'via': ['SIP/2.0/TLS router.example.test;branch=z9hG4bKremote'],
                   'from': ['<sip:+919876543210@example.test>;tag=caller'], 'to': ['<sip:alice@example.test>'],
                   'call-id': ['screened-call'], 'cseq': ['1 INVITE'], 'contact': ['<sip:bob@router.example.test>']}
        handled = await calls.incoming('INVITE sip:alice@example.test SIP/2.0', headers, SDP.encode())
        self.assertTrue(handled)
        self.assertIsNone(calls.current)
        await calls.close()
        return reg.sent, history.list()['entries']

    async def test_blocked_caller_is_declined_and_logged(self):
        sent, entries = await self.invite(lambda s: s.block('09876543210'))
        self.assertTrue(sent[0].startswith('SIP/2.0 603 Decline'))
        self.assertIn(';tag=', sent[0].split('To: ', 1)[1].split('\r\n', 1)[0])
        self.assertEqual((entries[0]['result'], entries[0]['number']), ('blocked', '+919876543210'))

    async def test_do_not_disturb_is_silent_and_logged(self):
        sent, entries = await self.invite(lambda s: s.update_dnd({'enabled': True}))
        self.assertTrue(sent[0].startswith('SIP/2.0 480'))
        self.assertEqual(entries[0]['result'], 'silenced')


class ScreeningApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_endpoints_and_logout(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runner = web.AppRunner(create_app(Registration(Path(temp.name) / 'provisioning.json')))
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        base = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
        async with ClientSession(headers={'Host': '127.0.0.1:8765'}) as client:
            async with client.post(base + '/api/blocked', json={'number': '+91 98765 43210'}) as r:
                self.assertEqual((await r.json())['blocked'][0]['key'], '9876543210')
            async with client.put(base + '/api/dnd', json={'minutes': 30}) as r:
                self.assertEqual((await r.json())['activeReason'], 'timer')
            async with client.put(base + '/api/dnd', json={'start': 'late'}) as r:
                self.assertEqual(r.status, 400)
            async with client.delete(base + '/api/blocked/1111111111') as r:
                self.assertEqual(r.status, 404)
            async with client.post(base + '/api/setup/reset') as r:
                self.assertEqual(r.status, 200)
            async with client.get(base + '/api/screening') as r:
                body = await r.json()
                self.assertEqual((body['blocked'], body['dnd']['active']), ([], False))
        await runner.cleanup()


if __name__ == '__main__':
    unittest.main()
