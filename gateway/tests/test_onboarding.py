import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp import ClientSession, web
import onboarding
from onboarding import Onboarding, SetupError
from registration import Registration
from server import create_app
from test_registration import PIN, XML


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.setup = Onboarding(self.runtime, self.runtime / 'provisioning.json')

    def tearDown(self):
        self.temp.cleanup()

    def trust(self):
        with patch.object(Onboarding, 'inspect_router', return_value={'certificateSha256': PIN}):
            self.setup.trust(PIN)

    def test_trust_requires_presented_certificate(self):
        with patch.object(Onboarding, 'inspect_router', return_value={'certificateSha256': 'b' * 64}):
            with self.assertRaises(SetupError):
                self.setup.trust(PIN)
        self.assertFalse(self.setup.status()['trusted'])
        self.trust()
        self.assertTrue(self.setup.status()['trusted'])
        self.assertEqual((self.runtime / 'router-trust.json').stat().st_mode & 0o777, 0o600)

    def test_start_requires_trust(self):
        with self.assertRaises(SetupError):
            self.setup.start()

    def test_otp_flow_saves_private_provisioning(self):
        self.trust()
        calls = []

        def fake_request(path, fingerprint, cookie=None):
            calls.append((path, fingerprint, cookie))
            self.assertEqual(fingerprint, PIN)
            if len(calls) == 1:
                return 407, {}, b''
            if len(calls) == 2:
                self.assertIn('op_type=add', path)
                return 200, {'set-cookie': 'session=abc; Path=/', 'x-amn': 'XXXXXX1234'}, b''
            self.assertEqual(path, '/?OTP=123456')
            self.assertEqual(cookie, 'session=abc')
            return 200, {}, XML.encode()

        with patch.object(onboarding, 'request', side_effect=fake_request):
            result = self.setup.start()
            self.assertEqual(result, {'state': 'otp_sent', 'otpTarget': 'XXXXXX1234'})
            status = self.setup.status()
            self.assertTrue(status['otpPending'])
            self.assertNotIn('abc', json.dumps(status))
            with self.assertRaises(SetupError):
                self.setup.verify('12ab')
            self.assertEqual(self.setup.verify('123456'), {'state': 'provisioned'})
        config = json.loads((self.runtime / 'provisioning.json').read_text())
        self.assertEqual(config['username'], 'alice')
        self.assertEqual(config['certificateSha256'], PIN)
        self.assertFalse(self.setup.status()['otpPending'])
        identity = json.loads((self.runtime / 'client-identity.json').read_text())
        self.assertIn(identity['id'], calls[0][0].replace('%3A', ':'))

    def test_encrypted_configuration_is_reported(self):
        self.trust()
        with patch.object(onboarding, 'request', return_value=(200, {}, b'\x01\x02binary')):
            with self.assertRaisesRegex(SetupError, 'encrypted'):
                self.setup.start()
        self.assertFalse(self.setup.status()['provisioned'])

    def test_reset_forgets_trust_and_provisioning(self):
        self.trust()
        (self.runtime / 'provisioning.json').write_text('{}')
        self.setup.reset()
        self.assertEqual(self.setup.status()['trusted'], False)
        self.assertEqual(self.setup.status()['provisioned'], False)


class SetupApiTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_setup_status_and_errors(self):
        async with self.client.get(self.base + '/api/setup') as r:
            self.assertEqual(r.status, 200)
            body = await r.json()
            self.assertEqual((body['trusted'], body['provisioned']), (False, False))
        async with self.client.post(self.base + '/api/setup/verify', json={'otp': '123456'}) as r:
            self.assertEqual(r.status, 409)
            self.assertIn('error', await r.json())
        async with self.client.post(self.base + '/api/setup/otp') as r:
            self.assertEqual(r.status, 409)
        async with self.client.post(self.base + '/api/setup/reset', headers={'Origin': 'https://example.com'}) as r:
            self.assertEqual(r.status, 403)


if __name__ == '__main__':
    unittest.main()
