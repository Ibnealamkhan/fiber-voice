"""Independent, authenticated SIP/TLS registration for an authorized Fiber client."""
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import ssl
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit

# The Mac app keeps private settings in Application Support via GATEWAY_RUNTIME.
RUNTIME = Path(os.environ.get('GATEWAY_RUNTIME') or Path(__file__).parent / '.runtime')
CONFIG = RUNTIME / 'provisioning.json'


def private_json(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temp = path.with_suffix('.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def parse_config(xml, certificate_sha256):
    root = ET.fromstring(xml)
    app = next((n for n in root.iter('characteristic')
                if n.get('type') == 'application' and any(
                    p.get('name') == 'appid' and p.get('value') == 'ap2002'
                    for p in n.findall('parm'))), None)
    if app is None:
        raise ValueError('Provisioning response has no IMS application')
    def values(node):
        return {p.get('name'): p.get('value', '') for p in node.iter('parm')}
    data = values(app)
    auth = next(n for n in app.iter('characteristic') if n.get('type') == 'appauth')
    credentials = values(auth)
    proxy = next(n for n in app.iter('characteristic') if n.get('type') == 'lbo_p-cscf_address')
    address = values(proxy)['address']
    endpoint = urlsplit('//' + address)
    config = {'publicIdentity': data['public_user_identity'],
              'privateIdentity': data.get('private_user_identity', ''),
              'domain': data['home_network_domain_name'],
              'username': credentials['username'], 'password': credentials['userpwd'],
              'realm': credentials['realm'], 'proxyHost': endpoint.hostname,
              'proxyPort': endpoint.port or 5061,
              'instanceId': data.get('uuid_value', ''),
              'certificateSha256': certificate_sha256, 'expires': 3600}
    for key in ['publicIdentity', 'domain', 'username', 'password', 'realm', 'proxyHost']:
        if not config[key] or any(c in config[key] for c in '\r\n'):
            raise ValueError('Missing or invalid provisioned SIP setting: ' + key)
    if config['proxyHost'].lower() != 'jiofiber.local.html':
        raise ValueError('Provisioned proxy is not the expected local Fiber router')
    if not re.fullmatch(r'[a-f0-9]{64}', certificate_sha256):
        raise ValueError('A verified router certificate fingerprint is required')
    return config


def parse_headers(head):
    lines = head.decode('utf-8', errors='replace').split('\r\n')
    headers = {}
    for line in lines[1:]:
        if ':' in line:
            k, v = line.split(':', 1)
            headers.setdefault(k.lower(), []).append(v.strip())
    return lines[0], headers


def first(headers, name, default=''):
    return headers.get(name.lower(), [default])[0]


def digest_parameters(challenge):
    if not challenge.lower().startswith('digest '):
        raise ValueError('Only provisioned Digest authentication is supported')
    return {m[1].lower(): (m[2] if m[2] is not None else m[3]) for m in re.finditer(
        r'([\w-]+)\s*=\s*(?:"([^"\r\n]*)"|([^,\s]+))', challenge[7:])}


def digest_authorization(challenge, username, password, method, uri, nonce_count=1, cnonce=None, body=""):
    p = digest_parameters(challenge)
    algorithm = p.get('algorithm', 'MD5').upper()
    hash_name = {'MD5': 'md5', 'MD5-SESS': 'md5', 'SHA-256': 'sha256',
                 'SHA-256-SESS': 'sha256'}.get(algorithm)
    if hash_name is None:
        raise ValueError('Unsupported SIP authentication algorithm')
    def H(s):
        return hashlib.new(hash_name, s.encode()).hexdigest()
    realm, nonce = p['realm'], p['nonce']
    cnonce = cnonce or secrets.token_hex(12)
    ha1 = H(f'{username}:{realm}:{password}')
    if algorithm.endswith('-SESS'):
        ha1 = H(f'{ha1}:{nonce}:{cnonce}')
    offered = [v.strip().lower() for v in p.get('qop', '').split(',') if v.strip()]
    if offered and 'auth' not in offered and 'auth-int' not in offered:
        raise ValueError('Unsupported SIP authentication qop')
    qop = 'auth' if 'auth' in offered else ('auth-int' if offered else None)
    ha2 = H(f'{method}:{uri}' + (':' + H(body) if qop == 'auth-int' else ''))
    nc = f'{nonce_count:08x}'
    response = H(f'{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}' if qop else f'{ha1}:{nonce}:{ha2}')
    def quote(value):
        if any(c in value for c in '\r\n'):
            raise ValueError('Invalid SIP header value')
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    attrs = {'username': username, 'realm': realm, 'nonce': nonce, 'uri': uri, 'response': response}
    out = ['Digest ' + ', '.join(f'{k}={quote(v)}' for k, v in attrs.items()), 'algorithm=' + algorithm]
    if p.get('opaque'):
        out.append('opaque=' + quote(p['opaque']))
    if qop:
        out += ['qop=' + qop, 'nc=' + nc, 'cnonce=' + quote(cnonce)]
    elif algorithm.endswith('-SESS'):
        out.append('cnonce=' + quote(cnonce))
    return ', '.join(out)


class Registration:
    def __init__(self, config_path=CONFIG):
        self.config_path = config_path
        self.config = None
        self.writer = None
        self.reader_task = None
        self.refresh_task = None
        self.monitor_task = None
        self.pending = {}
        self.lock = asyncio.Lock()
        self.write_lock = asyncio.Lock()
        self.call_id = secrets.token_hex(16) + '@fiber-gateway'
        self.tag = secrets.token_hex(10)
        self.cseq = 0
        self.contact = None
        self.auth = None
        self.nonce_count = 0
        self.registered = False
        self.registered_until = 0
        self.last_code = None
        self.last_error = None
        self.transport = None
        self.call_handler = None

    def start(self):
        self.monitor_task = asyncio.create_task(self._monitor())

    async def _monitor(self):
        while True:
            if not self.status()['registered'] and self.last_code not in (401, 403):
                await self.register()
            await asyncio.sleep(15)

    def status(self):
        active = self.registered and time.time() < self.registered_until and self.writer is not None and not self.writer.is_closing()
        return {'registered': bool(active), 'provisioned': self.config_path.exists(),
                'sipCode': self.last_code, 'error': self.last_error,
                'expiresAt': self.registered_until if active else None,
                'transport': self.transport, 'number': self.line_number(),
                'state': 'registered' if active else ('provisioned' if self.config_path.exists() else 'needs_authorization')}

    def line_number(self):
        # The fixed-line number from the provisioned public identity (not a secret).
        try:
            identity = (self.config or {}).get('publicIdentity', '')
            user = identity.split(':', 1)[1].split('@', 1)[0].split(';', 1)[0]
            return user if re.fullmatch(r'\+?\d{6,15}', user) else None
        except (IndexError, AttributeError):
            return None

    async def _connect(self):
        if self.writer and not self.writer.is_closing():
            return
        config = self.config
        ctx = ssl._create_unverified_context()
        reader, writer = await asyncio.wait_for(asyncio.open_connection(
            config['proxyHost'], config['proxyPort'], ssl=ctx,
            server_hostname=config['proxyHost'], family=socket.AF_INET6, limit=65536), 8)
        peer_cert = writer.get_extra_info('ssl_object').getpeercert(binary_form=True)
        fingerprint = hashlib.sha256(peer_cert).hexdigest()
        if not hmac.compare_digest(fingerprint, config['certificateSha256']):
            writer.close()
            await writer.wait_closed()
            raise ValueError('Router certificate changed; registration stopped before sending credentials')
        self.writer = writer
        self.auth = None
        self.nonce_count = 0
        local = writer.get_extra_info('sockname')
        user = config['publicIdentity'].split(':', 1)[1].split('@', 1)[0]
        self.contact = f'<sip:{user}@[{local[0]}]:{local[1]};transport=tls>'
        instance = config.get('instanceId')
        if instance and re.fullmatch(r'[A-Za-z0-9:._-]+', instance):
            # Preserve the router-issued value exactly; Jio's native client only wraps it in <>.
            self.contact += f';+sip.instance="<{instance}>"'
        self.transport = f'IPv6 / TLS / {config["proxyPort"]}'
        self.reader_task = asyncio.create_task(self._read(reader, writer))

    async def _send(self, message):
        async with self.write_lock:
            if not self.writer:
                raise ConnectionError('SIP connection is not open')
            self.writer.write(message.encode())
            await self.writer.drain()

    async def _read(self, reader, writer):
        try:
            while True:
                head = await reader.readuntil(b'\r\n\r\n')
                head = head.lstrip(b'\r\n')
                if not head:
                    continue
                start, headers = parse_headers(head)
                length = int(first(headers, 'content-length', '0'))
                if length < 0 or length > 1024 * 1024:
                    raise ValueError('Invalid SIP response length')
                body = await reader.readexactly(length)
                if start.startswith('SIP/2.0 '):
                    seq = int(first(headers, 'cseq').split()[0])
                    future = self.pending.get(seq)
                    if future and not future.done() and first(headers, 'call-id') == self.call_id:
                        code = int(start.split()[1])
                        if code >= 200:
                            future.set_result((code, headers, body))
                    elif self.call_handler:
                        await self.call_handler.response(start, headers, body)
                else:
                    method = start.split()[0]
                    if self.call_handler and await self.call_handler.incoming(start, headers, body):
                        continue
                    # Registration-event NOTIFY and keepalive OPTIONS must be acknowledged.
                    code = '200 OK' if method in ('OPTIONS', 'NOTIFY') else '480 Temporarily Unavailable'
                    reply = ['SIP/2.0 ' + code]
                    reply += ['Via: ' + v for v in headers.get('via', [])]
                    reply += [f'{name}: {first(headers, name)}' for name in ['From', 'To', 'Call-ID', 'CSeq']]
                    reply += ['Content-Length: 0', '', '']
                    await self._send('\r\n'.join(reply))
        except asyncio.CancelledError:
            raise
        except Exception:
            if self.writer is writer:
                self.registered = False
                self.last_error = 'SIP connection closed; reconnecting'
                if self.call_handler:
                    asyncio.create_task(self.call_handler.disconnected())
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(ConnectionError('SIP connection closed'))
        finally:
            writer.close()

    async def _request(self, expiry):
        self.cseq += 1
        config = self.config
        uri = 'sip:' + config['domain']
        local = self.writer.get_extra_info('sockname')
        lines = [f'REGISTER {uri} SIP/2.0',
                 f'Via: SIP/2.0/TLS [{local[0]}]:{local[1]};branch=z9hG4bK{secrets.token_hex(12)};rport',
                 'Max-Forwards: 70', f'From: <{config["publicIdentity"]}>;tag={self.tag}',
                 f'To: <{config["publicIdentity"]}>', 'Call-ID: ' + self.call_id,
                 f'CSeq: {self.cseq} REGISTER', f'Contact: {self.contact};expires={expiry}',
                 f'Expires: {expiry}', 'Supported: path, gruu', 'User-Agent: FiberGateway/0.1',
                 'X-Client: JSE']
        if self.auth:
            name, challenge = self.auth
            self.nonce_count += 1
            digest = digest_authorization(challenge, config['username'], config['password'],
                                          'REGISTER', uri, self.nonce_count)
            lines.append(name + ': ' + digest)
        lines += ['Content-Length: 0', '', '']
        future = asyncio.get_running_loop().create_future()
        self.pending[self.cseq] = future
        try:
            await self._send('\r\n'.join(lines))
            return await asyncio.wait_for(future, 12)
        finally:
            self.pending.pop(self.cseq, None)

    async def register(self):
        async with self.lock:
            self.last_code = None
            if not self.config_path.exists():
                self.last_error = 'Authorize this gateway with the router first'
                return self.status()
            try:
                self.config = json.loads(self.config_path.read_text())
                await self._connect()
                expiry = int(self.config.get('expires', 3600))
                for attempt in range(3):
                    code, headers, _ = await self._request(expiry)
                    self.last_code = code
                    if code in (401, 407):
                        header = 'www-authenticate' if code == 401 else 'proxy-authenticate'
                        challenge = first(headers, header)
                        params = digest_parameters(challenge)
                        if params.get('realm') != self.config['realm']:
                            raise ValueError('SIP challenge realm differs from authorized provisioning')
                        if self.auth and params.get('stale', '').lower() != 'true':
                            raise ValueError('Provisioned SIP credentials were rejected')
                        self.auth = ('Authorization' if code == 401 else 'Proxy-Authorization', challenge)
                        self.nonce_count = 0
                        continue
                    if code == 423:
                        expiry = int(first(headers, 'min-expires', '3600'))
                        if not 60 <= expiry <= 86400:
                            raise ValueError('Unsupported registration lifetime')
                        continue
                    if code != 200:
                        raise ValueError(f'SIP REGISTER rejected with {code}')
                    granted = expiry
                    for contact in headers.get('contact', []):
                        if self.contact.split('>')[0] in contact:
                            match = re.search(r'(?:^|;)\s*expires=(\d+)', contact)
                            if match:
                                granted = min(granted, int(match[1]))
                    granted = min(granted, int(first(headers, 'expires', str(granted))))
                    if granted <= 0:
                        raise ValueError('Router returned an expired registration')
                    self.registered_until = time.time() + granted
                    self.registered = True
                    self.last_error = None
                    if self.refresh_task and self.refresh_task is not asyncio.current_task():
                        self.refresh_task.cancel()
                    self.refresh_task = asyncio.create_task(self._refresh(max(10, granted * 0.75)))
                    return self.status()
                raise ValueError('Registration challenge negotiation did not complete')
            except Exception as error:
                self.registered = False
                self.last_error = str(error)
                return self.status()

    async def _refresh(self, delay):
        await asyncio.sleep(delay)
        await self.register()

    async def reset(self):
        """Deregister and forget the session so setup can run again."""
        await self.close()
        self.writer = self.reader_task = self.refresh_task = self.monitor_task = None
        self.config = self.auth = self.contact = self.transport = None
        self.last_code = self.last_error = None
        self.registered_until = 0
        self.call_id = secrets.token_hex(16) + '@fiber-gateway'
        self.tag = secrets.token_hex(10)
        self.start()

    async def close(self):
        for task in [self.monitor_task, self.refresh_task]:
            if task:
                task.cancel()
        if self.status()['registered']:
            # Remove only this client's exact Contact, never the line's other devices.
            try:
                async with self.lock:
                    await asyncio.wait_for(self._request(0), 3)
            except Exception:
                pass
        self.registered = False
        tasks = [self.refresh_task, self.reader_task, self.monitor_task]
        for task in tasks:
            if task:
                task.cancel()
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass
        await asyncio.gather(*(t for t in tasks if t), return_exceptions=True)
