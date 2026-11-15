import asyncio
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from registration import Registration, digest_authorization, parse_config, private_json
from onboarding import parse_http_response, initial_path

CERT = b'test-router-certificate'
PIN = hashlib.sha256(CERT).hexdigest()
UUID = 'abcdef01-2345-6789-abcd-ef0123456789'
XML = f'''<wap-provisioningdoc><characteristic type="application">
<parm name="appid" value="ap2002"/><parm name="home_network_domain_name" value="example.test"/>
<parm name="private_user_identity" value="sip:alice@example.test"/>
<characteristic type="public_user_identity_list"><parm name="public_user_identity" value="sip:alice@example.test"/></characteristic>
<characteristic type="appauth"><parm name="realm" value="example.test"/><parm name="username" value="alice"/><parm name="userpwd" value="test-secret"/></characteristic>
<characteristic type="lbo_p-cscf_address"><parm name="address" value="jiofiber.local.html:5068"/></characteristic>
<characteristic type="other"><parm name="uuid_value" value="{UUID}"/></characteristic>
</characteristic></wap-provisioningdoc>'''


class LineNumberTests(unittest.TestCase):
    def test_line_number_from_public_identity(self):
        reg = Registration(Path('/nonexistent/provisioning.json'))
        self.assertIsNone(reg.status()['number'])
        reg.config = {'publicIdentity': 'sip:+912212345678@ims.example.test'}
        self.assertEqual(reg.status()['number'], '+912212345678')
        reg.config = {'publicIdentity': 'sip:alice@example.test'}
        self.assertIsNone(reg.line_number())


class ProtocolTests(unittest.TestCase):
    def test_digest_rfc2617_vector(self):
        result = digest_authorization(
            'Digest realm="testrealm@host.com", qop="auth,auth-int", nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", opaque="5ccc069c403ebaf9f0171e9517f40e41"',
            'Mufasa', 'Circle Of Life', 'GET', '/dir/index.html', cnonce='0a4f113b')
        self.assertIn('response="6629fae49393a05397450978507c4ef1"', result)
        self.assertIn('nc=00000001', result)

    def test_cookie_survives_router_blank_lines(self):
        raw = b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\nContent-Type: text/html\r\nx-amn: +91********41\r\r\nSet-Cookie: test-session=xyz\r\r\nServer: router\r\n\r\n'
        code, headers, body = parse_http_response(raw)
        self.assertEqual(code, 200)
        self.assertEqual(headers['set-cookie'], 'test-session=xyz')
        self.assertEqual(body, b'')

    def test_provisioning_xml_body_is_not_mistaken_for_headers(self):
        body = b'<xml>Set-Cookie: not-a-header</xml>'
        raw = f'HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n'.encode() + body
        _, headers, parsed = parse_http_response(raw)
        self.assertNotIn('set-cookie', headers)
        self.assertEqual(parsed, body)

    def test_config_preserves_authorized_identity_and_proxy(self):
        config = parse_config(XML, PIN)
        self.assertEqual(config['instanceId'], UUID)
        self.assertEqual(config['proxyPort'], 5068)
        self.assertEqual(config['username'], 'alice')
        self.assertEqual(config['password'], 'test-secret')
        with self.assertRaises(ValueError):
            parse_config(XML.replace('jiofiber.local.html', 'untrusted.example'), PIN)
        self.assertIn('op_type=add', initial_path({'id':'02:00:00:00:00:01','alias':'Mac'}, 'add'))


class FakeSSL:
    def __init__(self, certificate=CERT): self.certificate = certificate
    def getpeercert(self, binary_form=False): return self.certificate


class FakeRouter:
    def __init__(self, final_code=200, certificate=CERT):
        self.reader = asyncio.StreamReader()
        self.closed = False
        self.requests = []
        self.final_code = final_code
        self.certificate = certificate

    def get_extra_info(self, key):
        return FakeSSL(self.certificate) if key == 'ssl_object' else ('::1', 32123, 0, 0)
    def is_closing(self): return self.closed
    async def drain(self): pass
    async def wait_closed(self): pass
    def close(self):
        self.closed = True
        self.reader.feed_eof()
    def write(self, data):
        message = data.decode()
        self.requests.append(message)
        headers = dict(line.split(': ', 1) for line in message.split('\r\n')[1:] if ': ' in line)
        authenticated = 'Authorization' in headers
        status = self.final_code if authenticated else 401
        challenge = 'WWW-Authenticate: Digest realm="example.test", nonce="test-nonce", algorithm=MD5, qop="auth"\r\n' if not authenticated else ''
        expiry = '0' if headers['Expires'] == '0' else '120'
        response = (f'SIP/2.0 {status} Result\r\nCall-ID: {headers["Call-ID"]}\r\nCSeq: {headers["CSeq"]}\r\n'
                    f'{challenge}Expires: {expiry}\r\nContent-Length: 0\r\n\r\n')
        self.reader.feed_data(response.encode())


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'provisioning.json'
        private_json(self.path, parse_config(XML, PIN))
        self.registration = Registration(self.path)
    async def asyncTearDown(self):
        await self.registration.close()
        self.temp.cleanup()

    async def test_digest_registration_exact_instance_renewal_and_cleanup(self):
        router = FakeRouter()
        async def connect(*args, **kwargs): return router.reader, router
        with patch('registration.asyncio.open_connection', connect):
            status = await self.registration.register()
            self.assertTrue(status['registered'])
            self.assertEqual(status['sipCode'], 200)
            self.assertIn(f'+sip.instance="<{UUID}>"', router.requests[0])
            self.assertNotIn('urn:uuid:', router.requests[0])
            self.assertNotIn('test-secret', json.dumps(status))
            self.assertNotIn('test-secret', router.requests[1])
            self.assertFalse(self.registration.refresh_task.done())
            status = await self.registration.register()
            self.assertTrue(status['registered'])
            self.assertIn('CSeq: 3 REGISTER', router.requests[-1])
            await self.registration.close()
            self.assertIn('Expires: 0', router.requests[-1])
            self.assertNotIn('Contact: *', router.requests[-1])
            self.assertFalse(self.registration.status()['registered'])

    async def test_forbidden_cannot_be_shown_as_registered(self):
        router = FakeRouter(final_code=403)
        async def connect(*args, **kwargs): return router.reader, router
        with patch('registration.asyncio.open_connection', connect):
            status = await self.registration.register()
            self.assertFalse(status['registered'])
            self.assertEqual(status['sipCode'], 403)
            self.assertEqual(len(router.requests), 2)

    async def test_certificate_change_stops_before_any_credentials(self):
        router = FakeRouter(certificate=b'different-certificate')
        async def connect(*args, **kwargs): return router.reader, router
        with patch('registration.asyncio.open_connection', connect):
            status = await self.registration.register()
            self.assertFalse(status['registered'])
            self.assertIn('certificate changed', status['error'])
            self.assertEqual(router.requests, [])
