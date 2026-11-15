import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp import ClientSession, web
from history import CallHistory, classify
from registration import Registration
from server import create_app, HISTORY
from contacts import LocalContacts, contact_key


def call(**kw):
    base = dict(id='c1', direction='incoming', number='0221234567', state='cancelled', created=100.0,
                connected=None, ended=130.0, last_sip=None, user_declined=False, failure_cause=None, error=None)
    base.update(kw)
    return SimpleNamespace(**base)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'call-history.json'

    def tearDown(self):
        self.temp.cleanup()

    def test_classification(self):
        self.assertEqual(classify(call()), 'missed')
        self.assertEqual(classify(call(state='declined', user_declined=True)), 'declined')
        self.assertEqual(classify(call(connected=105.0, state='ended')), 'answered')
        self.assertEqual(classify(call(direction='outgoing', state='failed', last_sip=486)), 'busy')
        self.assertEqual(classify(call(direction='outgoing', state='failed', last_sip=480)), 'unavailable')
        self.assertEqual(classify(call(direction='outgoing', state='failed', last_sip=480, failure_cause=18)), 'no_answer')
        self.assertEqual(classify(call(direction='outgoing', state='failed', last_sip=404)), 'invalid')
        self.assertEqual(classify(call(direction='outgoing', state='failed', last_sip=500, failure_cause=1)), 'invalid')
        self.assertEqual(classify(call(direction='outgoing', state='failed', last_sip=408)), 'no_answer')
        self.assertEqual(classify(call(direction='outgoing', state='cancelled')), 'cancelled')
        self.assertEqual(classify(call(direction='outgoing', state='failed', last_sip=500)), 'failed')

    def test_persistence_missed_badge_and_delete(self):
        history = CallHistory(self.path)
        history.add(call(id='a'))
        entry = history.add(call(id='b', connected=110.0, ended=170.0, state='ended'))
        self.assertEqual(entry['duration'], 60)
        reopened = CallHistory(self.path)
        listing = reopened.list()
        self.assertEqual([e['id'] for e in listing['entries']], ['b', 'a'])
        self.assertEqual(listing['missedUnseen'], 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        reopened.mark_seen()
        self.assertEqual(reopened.list()['missedUnseen'], 0)
        self.assertTrue(reopened.delete('a'))
        self.assertFalse(reopened.delete('a'))
        reopened.clear()
        self.assertEqual(reopened.list()['entries'], [])
        self.assertFalse(self.path.exists())


class ContactTests(unittest.TestCase):
    def test_keys_match_common_formats(self):
        keys = {contact_key(n) for n in ['+91 98765 43210', '09876543210', '9876543210', '+91-98765-43210']}
        self.assertEqual(keys, {'9876543210'})
        self.assertEqual(contact_key('02212345678'), contact_key('+912212345678'))
        self.assertEqual(contact_key('199'), '199')

    def test_set_rename_remove_and_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'contacts.json'
            book = LocalContacts(path)
            self.assertEqual(book.set('+919876543210', '  Asha \n Rao '), {'key': '9876543210', 'name': 'Asha Rao'})
            self.assertEqual(LocalContacts(path).snapshot()['contacts'], {'9876543210': 'Asha Rao'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(book.favorite('9876543210', True), ['9876543210'])
            self.assertEqual(book.favorite('02212345678', True), ['9876543210', '2212345678'])
            self.assertEqual(book.snapshot()['numbers']['2212345678'], '02212345678')
            book.set('09876543210', '')
            self.assertEqual(book.snapshot()['contacts'], {})
            self.assertIn('9876543210', book.snapshot()['numbers'])  # still a favourite
            self.assertEqual(LocalContacts(path).favorite('2212345678', False), ['9876543210'])
            self.assertNotIn('2212345678', LocalContacts(path).snapshot()['numbers'])
            with self.assertRaises(ValueError):
                book.set('12', 'Too short')
            with self.assertRaises(ValueError):
                book.set('9876543210', 'x' * 81)


class HistoryApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(Registration(Path(self.temp.name) / 'provisioning.json'))
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.base = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
        self.client = ClientSession(headers={'Host': '127.0.0.1:8765'})

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()
        self.temp.cleanup()

    async def test_history_endpoints_and_logout_clears(self):
        self.app[HISTORY].add(call(id='x'))
        async with self.client.get(self.base + '/api/history') as r:
            body = await r.json()
            self.assertEqual(body['missedUnseen'], 1)
            self.assertEqual(body['entries'][0]['result'], 'missed')
        async with self.client.post(self.base + '/api/history/seen') as r:
            self.assertEqual((await r.json())['missedUnseen'], 0)
        async with self.client.delete(self.base + '/api/history/nope') as r:
            self.assertEqual(r.status, 404)
        async with self.client.put(self.base + '/api/contacts', json={'number': '09876543210', 'name': 'Asha'}) as r:
            self.assertEqual((await r.json())['key'], '9876543210')
        async with self.client.get(self.base + '/api/contacts') as r:
            body = await r.json()
            self.assertEqual(body['contacts'], {'9876543210': 'Asha'})
            self.assertEqual(body['numbers'], {'9876543210': '09876543210'})
        async with self.client.put(self.base + '/api/favorites', json={'number': '+91 98765 43210', 'favorite': True}) as r:
            self.assertEqual((await r.json())['favorites'], ['9876543210'])
        async with self.client.put(self.base + '/api/contacts', json={'number': 'x', 'name': 'Bad'}) as r:
            self.assertEqual(r.status, 400)
        async with self.client.post(self.base + '/api/setup/reset') as r:
            self.assertEqual(r.status, 200)
        async with self.client.get(self.base + '/api/history') as r:
            self.assertEqual((await r.json())['entries'], [])
        async with self.client.get(self.base + '/api/contacts') as r:
            self.assertEqual(await r.json(), {'contacts': {}, 'numbers': {}, 'favorites': []})
        async with self.client.get(self.base + '/api/history', headers={'Origin': 'https://evil.test'}) as r:
            self.assertEqual(r.status, 403)


if __name__ == '__main__':
    unittest.main()
