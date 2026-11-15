"""First-run authorization of this gateway with the local JioFiber router.

Shared by the interactive ``enroll.py`` helper and the in-app setup screens:

1. Inspect the router's TLS certificate and let the user trust it (pinned).
2. Ask the router to provision this client. A fresh client gets HTTP 407
   ("not whitelisted"), so an OTP is requested with ``op_type=add``.
3. Submit the OTP the router sent to the registered mobile; the router answers
   with the SIP provisioning XML, which is parsed and stored privately.

The OTP session cookie stays inside this process and is never returned to the
browser. Every credential-bearing request enforces the saved certificate pin.
"""
import hashlib
import hmac
import json
import re
import secrets
import socket
import ssl
import threading
import time
from urllib.parse import urlencode

from registration import CONFIG, RUNTIME, parse_config, private_json

HOST = 'jiofiber.local.html'
PORT = 8443
LIMIT = 1024 * 1024
OTP_LIFETIME = 600


class SetupError(Exception):
    """A user-presentable setup failure."""


def parse_http_response(raw):
    if not raw:
        raise ValueError('Router closed the connection without a response')
    first = raw.splitlines()[0].decode('ascii', errors='replace')
    match = re.fullmatch(r'HTTP/1\.[01] (\d{3}) .+', first)
    if not match:
        raise ValueError('Invalid local provisioning response')
    length = re.search(rb'(?im)^Content-Length:[ \t]*(\d+)', raw)
    if not length:
        raise ValueError('Router response omitted Content-Length')
    size = int(length[1])
    if size > LIMIT or len(raw) < size:
        raise ValueError('Invalid router response length')
    # Router firmware can emit extra CRs/blank lines before Set-Cookie. Using
    # the declared body size avoids silently discarding its OTP session cookie.
    head = raw[:-size] if size else raw
    body = raw[-size:] if size else b''
    headers = {}
    for line in head.decode('iso-8859-1').splitlines()[1:]:
        if ':' in line:
            key, value = line.split(':', 1)
            headers[key.strip().lower()] = value.strip()
    return int(match[1]), headers, body


def _connect():
    try:
        raw = socket.create_connection((HOST, PORT), timeout=8)
    except socket.gaierror:
        raise SetupError(f'Cannot find the JioFiber router ({HOST}). Connect this Mac to your '
                         'JioFiber Wi-Fi or LAN and use the router’s own DNS.') from None
    except OSError as error:
        raise SetupError(f'Cannot reach the JioFiber router on port {PORT}: {error.strerror or error}') from None
    # The router presents a local certificate; it is accepted only by pin.
    ctx = ssl._create_unverified_context()
    try:
        return ctx.wrap_socket(raw, server_hostname=HOST)
    except Exception:
        raw.close()
        raise


def request(path, fingerprint, cookie=None):
    if any(c in path for c in '\r\n') or (cookie and any(c in cookie for c in '\r\n')):
        raise ValueError('Invalid provisioning request')
    with _connect() as conn:
        actual = hashlib.sha256(conn.getpeercert(binary_form=True)).hexdigest()
        if not hmac.compare_digest(actual, fingerprint):
            raise SetupError('Router certificate differs from the saved trust pin')
        headers = [f'GET {path} HTTP/1.1', f'Host: {HOST}:{PORT}',
                   'Accept: */*', 'Connection: close']
        if cookie:
            headers.append('Cookie: ' + cookie)
        conn.sendall(('\r\n'.join(headers) + '\r\n\r\n').encode())
        chunks = []
        total = 0
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > LIMIT:
                raise ValueError('Provisioning response too large')
            chunks.append(chunk)
    return parse_http_response(b''.join(chunks))


def initial_path(identity, operation=None):
    params = {'terminal_sw_version': '1', 'terminal_vendor': 'Apple', 'terminal_model': 'Mac',
              'SMS_port': '0', 'act_type': 'volatile', 'IMSI': '', 'msisdn': '', 'IMEI': '',
              'vers': '0', 'token': '', 'rcs_state': '0', 'rcs_version': '5.1B',
              'rcs_profile': 'joyn_blackbird', 'client_vendor': 'JUIC', 'default_sms_app': '2',
              'default_vvm_app': '0', 'device_type': 'vvm', 'client_version': 'JSEAndrd-1.0',
              'mac_address': identity['id'], 'nwk_intf': 'wifi', 'alias': identity['alias']}
    if operation:
        params['op_type'] = operation
    return '/?' + urlencode(params)


def certificate_details(der):
    details = {'certificateSha256': hashlib.sha256(der).hexdigest()}
    try:
        from cryptography import x509
        cert = x509.load_der_x509_certificate(der)
        from cryptography.x509.oid import NameOID
        organization = cert.issuer.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
        details['organization'] = organization[0].value if organization else None
        details['selfSigned'] = cert.issuer == cert.subject
        details['subject'] = cert.subject.rfc4514_string()
        details['issuer'] = cert.issuer.rfc4514_string()
        details['validUntil'] = cert.not_valid_after_utc.date().isoformat()
    except Exception:
        pass
    return details


def ipv6_available():
    try:
        return bool(socket.getaddrinfo(HOST, 5068, socket.AF_INET6, socket.SOCK_STREAM))
    except OSError:
        return False


class Onboarding:
    def __init__(self, runtime=RUNTIME, config_path=None):
        self.runtime = runtime
        self.config_path = config_path or (CONFIG if runtime == RUNTIME else runtime / 'provisioning.json')
        self.trust_path = runtime / 'router-trust.json'
        self.identity_path = runtime / 'client-identity.json'
        self.lock = threading.Lock()
        self.cookie = None
        self.cookie_expires = 0
        self.otp_target = None

    def fingerprint(self):
        try:
            value = json.loads(self.trust_path.read_text())['certificateSha256']
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return value if isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) else None

    def otp_pending(self):
        return bool(self.cookie) and time.time() < self.cookie_expires

    def status(self):
        return {'trusted': self.fingerprint() is not None, 'provisioned': self.config_path.exists(),
                'otpPending': self.otp_pending(), 'otpTarget': self.otp_target if self.otp_pending() else None}

    def identity(self):
        try:
            identity = json.loads(self.identity_path.read_text())
            if re.fullmatch(r'[0-9a-f]{2}(:[0-9a-f]{2}){5}', identity.get('id', '')) and identity.get('alias'):
                return identity
        except (OSError, ValueError, AttributeError):
            pass
        # Locally administered MAC-style client ID, unique to this installation.
        identity = {'id': ':'.join(f'{b:02x}' for b in bytes([2]) + secrets.token_bytes(5)),
                    'alias': 'Mac Gateway'}
        private_json(self.identity_path, identity)
        return identity

    def inspect_router(self):
        """Read (without trusting) the certificate the router presents now."""
        with _connect() as conn:
            details = certificate_details(conn.getpeercert(binary_form=True))
        details['ipv6'] = ipv6_available()
        details['trusted'] = details['certificateSha256'] == self.fingerprint()
        return details

    def trust(self, fingerprint):
        if not isinstance(fingerprint, str) or not re.fullmatch(r'[a-f0-9]{64}', fingerprint):
            raise SetupError('Invalid certificate fingerprint')
        current = self.inspect_router()['certificateSha256']
        if not hmac.compare_digest(current, fingerprint):
            raise SetupError('The router certificate changed while you were confirming it. Check again.')
        private_json(self.trust_path, {'certificateSha256': fingerprint})
        return self.status()

    def _save(self, body, fingerprint):
        if not body:
            raise SetupError('The router did not return a voice configuration')
        if not body.lstrip().startswith(b'<'):
            raise SetupError('This router returned an encrypted configuration, which this app '
                             'cannot read yet. Your router firmware is not supported.')
        try:
            config = parse_config(body, fingerprint)
        except Exception as error:
            raise SetupError(f'Could not read the router’s voice configuration: {error}') from None
        private_json(self.config_path, config)
        self.cookie = None
        self.otp_target = None
        return config

    def start(self):
        """Request provisioning. Returns 'provisioned' or 'otp_sent'."""
        with self.lock:
            fingerprint = self.fingerprint()
            if not fingerprint:
                raise SetupError('Trust the router certificate first')
            identity = self.identity()
            code, headers, body = request(initial_path(identity), fingerprint)
            if code == 200 and body:
                self._save(body, fingerprint)
                return {'state': 'provisioned'}
            if code != 407:
                raise SetupError(f'The router refused voice setup (HTTP {code}). '
                                 'Check that fixed-line calling is active on this connection.')
            code, headers, body = request(initial_path(identity, 'add'), fingerprint)
            if code == 200 and body.lstrip().startswith(b'<'):
                self._save(body, fingerprint)
                return {'state': 'provisioned'}
            cookie = headers.get('set-cookie')
            if code != 200 or not cookie:
                raise SetupError(f'The router did not start OTP authorization (HTTP {code}). '
                                 'You may have reached its device limit; remove an old device and retry.')
            self.cookie = cookie.split(';', 1)[0]
            self.cookie_expires = time.time() + OTP_LIFETIME
            target = headers.get('x-amn', '')
            self.otp_target = target if re.fullmatch(r'[0-9Xx*+ ]{4,20}', target) else None
            return {'state': 'otp_sent', 'otpTarget': self.otp_target}

    def verify(self, otp):
        with self.lock:
            if not isinstance(otp, str) or not re.fullmatch(r'\d{5,6}', otp):
                raise SetupError('Enter the exact 5- or 6-digit code you received')
            if not self.otp_pending():
                raise SetupError('The code request expired. Send a new code.')
            fingerprint = self.fingerprint()
            code, headers, body = request('/?OTP=' + otp, fingerprint, self.cookie)
            if code != 200 or not body:
                raise SetupError('The router did not accept that code. Check it and try again.'
                                 if code in (401, 403, 407) else f'Code verification failed (HTTP {code})')
            self._save(body, fingerprint)
            return {'state': 'provisioned'}

    def reset(self):
        """Forget provisioning and trust so the app can be set up again."""
        with self.lock:
            for path in (self.config_path, self.trust_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            self.cookie = None
            self.otp_target = None
