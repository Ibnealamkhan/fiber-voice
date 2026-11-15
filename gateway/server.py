"""Local, independently registered SIP/WebRTC Fiber voice gateway."""
import asyncio
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import ssl
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, web
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration
from registration import CONFIG, Registration
from calls import Calls, LIVE
from onboarding import Onboarding, SetupError
from history import CallHistory
from contacts import LocalContacts
from screening import CallScreening

ROOT = Path(__file__).parent
ROUTER = 'jiofiber.local.html'
PORT = int(os.environ.get('PORT', '8765'))
PEERS = web.AppKey('peers', dict)
TASKS = web.AppKey('tasks', set)
DIAGNOSTIC_LOCK = web.AppKey('diagnostic_lock', asyncio.Lock)
REGISTRATION = web.AppKey('registration', Registration)
CALLS = web.AppKey('calls', Calls)
ONBOARDING = web.AppKey('onboarding', Onboarding)
HISTORY = web.AppKey('history', CallHistory)
CONTACTS = web.AppKey('contacts', LocalContacts)
SCREENING = web.AppKey('screening', CallScreening)


def parse_sip(message):
    head, _, body = message.partition('\r\n\r\n')
    lines = head.split('\r\n')
    if not re.fullmatch(r'SIP/2.0 \d{3} .+', lines[0]):
        raise ValueError('Router did not return a SIP response')
    headers = {}
    for line in lines[1:]:
        if ':' in line:
            key, value = line.split(':', 1)
            headers[key.lower()] = value.strip()
    codecs = []
    in_audio = False
    for line in body.splitlines():
        if line.startswith('m='):
            in_audio = line.startswith('m=audio ')
        if in_audio and line.startswith('a=rtpmap:'):
            name = line.split(' ', 1)[1].split('/')[0]
            if name.lower() != 'telephone-event' and name not in codecs:
                codecs.append(name)
    return {'code': int(lines[0].split()[1]), 'status': lines[0],
            'audioCodecs': codecs,
            'methods': [x.strip() for x in headers.get('allow', '').split(',') if x.strip()]}


def sip_probe():
    # This diagnostic does not REGISTER, INVITE, or alter the existing phone session.
    target = socket.getaddrinfo(ROUTER, 5061, socket.AF_INET6, socket.SOCK_STREAM)[0][4]
    ctx = ssl._create_unverified_context()  # Router has a local self-signed certificate.
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as raw:
        raw.settimeout(5)
        raw.connect(target)
        with ctx.wrap_socket(raw, server_hostname=ROUTER) as conn:
            local = conn.getsockname()[0]
            message = '\r\n'.join([
                f'OPTIONS sip:{ROUTER} SIP/2.0',
                f'Via: SIP/2.0/TLS [{local}];branch=z9hG4bK{secrets.token_hex(8)};rport',
                'Max-Forwards: 0',
                f'From: <sip:gateway-probe@{ROUTER}>;tag={secrets.token_hex(6)}',
                f'To: <sip:{ROUTER}>',
                f'Call-ID: {secrets.token_hex(12)}@localhost', 'CSeq: 1 OPTIONS',
                'User-Agent: FiberGateway-Probe', 'Content-Length: 0', '', ''])
            conn.sendall(message.encode())
            data = b''
            while len(data) < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b'\r\n\r\n' in data:
                    head, body = data.split(b'\r\n\r\n', 1)
                    length = re.search(rb'(?im)^Content-Length:\s*(\d+)', head)
                    if len(body) >= (int(length[1]) if length else 0):
                        break
            result = parse_sip(data.decode(errors='replace'))
            result['certificateSha256'] = hashlib.sha256(conn.getpeercert(binary_form=True)).hexdigest()
            result['transport'] = 'IPv6 / TLS / 5061'
            return result


async def diagnose(registration=None, calls=None):
    result = {'gatewayRegistered': False, 'callingAvailable': False,
              'blockers': []}
    async with ClientSession(timeout=ClientTimeout(total=7), trust_env=False) as session:
        async def fetch(path):
            async with session.get(f'https://{ROUTER}:7443/{path}', ssl=False,
                                   allow_redirects=False) as resp:
                if resp.status != 200:
                    raise ValueError(f'HTTP {resp.status}')
                raw = await resp.content.read(65537)
                if len(raw) > 65536:
                    raise ValueError('Router response too large')
                return json.loads(raw)
        async def account():
            try:
                a = await fetch('request_account')
                result['account'] = {'number': a.get('msisdn'), 'mode': a.get('mode')}
            except Exception as error:
                result['account'] = {'error': str(error)}
        async def reachability():
            try:
                a = await fetch('getSrvReachabilityDetails')
                result['reachability'] = {key: a.get(key) for key in ['PcscfReachability', 'CCSReachability']}
            except Exception as error:
                result['reachability'] = {'error': str(error)}
        async def sip():
            try:
                result['sip'] = await asyncio.to_thread(sip_probe)
            except Exception as error:
                result['sip'] = {'error': str(error)}
        await asyncio.gather(account(), reachability(), sip())
    state = registration.status() if registration else {'registered': False, 'state': 'needs_authorization'}
    result['registration'] = state
    result['gatewayRegistered'] = state['registered']
    if not state['registered']:
        result['blockers'].append(state.get('error') or 'Authorize and register this gateway with the router.')
    if calls:
        capability = calls.status()
        result['callingAvailable'] = capability['available']
        result['calling'] = capability
        if not capability['codecs']['ready']:
            result['blockers'].append(capability['codecs']['error'])
    return result


@web.middleware
async def local_only(request, handler):
    # Reject foreign Host/Origin before any diagnostic or media endpoint executes.
    try:
        host = urlsplit('http://' + request.host)
        if host.hostname not in ('localhost', '127.0.0.1', '::1') or (host.port or 80) != PORT:
            raise ValueError()
    except ValueError:
        raise web.HTTPForbidden(text='Local gateway only')
    origin = request.headers.get('Origin')
    if origin and origin != f'{request.scheme}://{request.host}':
        raise web.HTTPForbidden(text='Use the gateway page on localhost')
    if request.headers.get('Sec-Fetch-Site') == 'cross-site':
        raise web.HTTPForbidden(text='Cross-site request rejected')
    response = await handler(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'; media-src 'self' blob:; object-src 'none'; frame-ancestors 'none'"
    response.headers['Permissions-Policy'] = 'microphone=(self), camera=(self)'
    return response


async def status(request):
    app = request.app
    if app[DIAGNOSTIC_LOCK].locked():
        raise web.HTTPTooManyRequests(text='Diagnostics are already running')
    async with app[DIAGNOSTIC_LOCK]:
        result = await diagnose(app[REGISTRATION], app[CALLS])
    return web.json_response(result)


async def retry_registration(request):
    state = await request.app[REGISTRATION].register()
    return web.json_response(state, status=200 if state['registered'] else 409)


async def registration_status(request):
    return web.json_response({**request.app[REGISTRATION].status(), 'calling': request.app[CALLS].status()})


async def json_object(request):
    if request.content_type != 'application/json':
        raise web.HTTPUnsupportedMediaType()
    try:
        value = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text='Expected JSON')
    if not isinstance(value, dict):
        raise web.HTTPBadRequest(text='Expected a JSON object')
    return value


async def call_peer(app, call, body):
    if body.get('type') != 'offer' or not isinstance(body.get('sdp'), str):
        raise ValueError('Expected a WebRTC audio offer')
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    call.peer = pc

    video = getattr(call.media, 'video', None)

    @pc.on('track')
    def track_received(track):
        if track.kind == 'audio':
            call.browser_track = track
            call.attach_task = call.media.spawn(call.media.attach(track))
        elif track.kind == 'video' and video:
            video.spawn(video.attach(track))

    @pc.on('connectionstatechange')
    async def changed():
        if pc.connectionState == 'connected' and call.state in LIVE and not getattr(call, 'invite_started', False):
            call.invite_started = True
            if call.direction == 'outgoing':
                call.spawn(app[CALLS].invite(call))
            elif call.state == 'incoming':
                call.spawn(app[CALLS].answer(call))
        elif pc.connectionState in ('failed', 'closed'):
            # The browser leg may have moved to a conference call; hang up whichever call owns it now.
            owner = next((c for c in list(app[CALLS].dialogs.values()) if c.peer is pc and c.state in LIVE), None)
            if owner:
                owner.spawn(app[CALLS].hangup(owner))

    pc.addTrack(call.media.track)
    if video:
        pc.addTrack(video.track)
    allowed = {'audio', 'video'} if video else {'audio'}
    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=body['sdp'], type='offer'))
        if not pc.getTransceivers() or any(t.kind not in allowed for t in pc.getTransceivers()):
            raise ValueError('Unsupported browser media for this call')
        await pc.setLocalDescription(await pc.createAnswer())
    except Exception:
        await app[CALLS].finish(call, 'failed', 'Could not negotiate browser audio')
        raise ValueError('Could not negotiate browser audio')
    async def connect_timeout():
        await asyncio.sleep(15)
        if pc.connectionState != 'connected' and call.state in LIVE:
            await app[CALLS].hangup(call)
    call.spawn(connect_timeout())
    return {'sdp': pc.localDescription.sdp, 'type': 'answer', 'callId': call.id}


async def create_call(request):
    body = await json_object(request)
    if body.get('type') != 'offer' or not isinstance(body.get('sdp'), str):
        raise web.HTTPBadRequest(text='Expected a WebRTC audio offer')
    try:
        call = await request.app[CALLS].prepare_outgoing(body.get('number', ''), video=body.get('video') is True)
        answer = await call_peer(request.app, call, body)
        return web.json_response(answer)
    except ValueError as error:
        raise web.HTTPBadRequest(text=str(error))


def selected_call(request):
    call = request.app[CALLS].current
    if not call or call.id != request.match_info['id']:
        raise web.HTTPNotFound(text='Call not found')
    return call


async def calls_status(request):
    return web.json_response(request.app[CALLS].status())


async def answer_call(request):
    call = selected_call(request)
    if call.direction != 'incoming' or call.state != 'incoming' or not call.media:
        raise web.HTTPConflict(text='No incoming call ready to answer')
    if call.peer:
        raise web.HTTPConflict(text='Browser audio is already connecting')
    body = await json_object(request)
    await request.app[CALLS].choose_video(call, body.get('video') is True)
    try:
        return web.json_response(await call_peer(request.app, call, body))
    except ValueError as error:
        raise web.HTTPBadRequest(text=str(error))


async def end_call(request):
    call = selected_call(request)
    await request.app[CALLS].hangup(call)
    return web.json_response(call.public())


async def add_participant(request):
    call = selected_call(request)
    body = await json_object(request)
    try:
        conf = await request.app[CALLS].add_participant(call, body.get('number', ''))
    except ValueError as error:
        raise web.HTTPConflict(text=str(error))
    return web.json_response(conf.public())


async def cancel_participant(request):
    call = selected_call(request)
    await request.app[CALLS].cancel_add(call)
    return web.json_response(call.public())


async def dtmf_call(request):
    call = selected_call(request)
    body = await json_object(request)
    digit = body.get('digit')
    if call.state != 'active' or not isinstance(digit, str) or len(digit) != 1 or digit not in '0123456789*#ABCD':
        raise web.HTTPBadRequest(text='An active call and one valid digit are required')
    if call.media.remote.get('eventPt') is None:
        raise web.HTTPConflict(text='Telephone events were not negotiated')
    call.media.spawn(call.media.dtmf(digit))
    return web.json_response({'sent': True})


async def router_account():
    try:
        async with ClientSession(timeout=ClientTimeout(total=6), trust_env=False) as session:
            async with session.get(f'https://{ROUTER}:7443/request_account', ssl=False,
                                   allow_redirects=False) as resp:
                if resp.status != 200:
                    return None
                raw = await resp.content.read(65537)
                account = json.loads(raw) if len(raw) <= 65536 else {}
                return account.get('msisdn') or None
    except Exception:
        return None


def setup_error(error, status=400):
    return web.json_response({'error': str(error)}, status=status)


async def setup_status(request):
    return web.json_response({**request.app[ONBOARDING].status(),
                              'registration': request.app[REGISTRATION].status()})


async def setup_router(request):
    try:
        details, number = await asyncio.gather(
            asyncio.to_thread(request.app[ONBOARDING].inspect_router), router_account())
    except SetupError as error:
        return setup_error(error, 502)
    except Exception as error:
        return setup_error(f'Could not check the router: {error}', 502)
    return web.json_response({**details, 'number': number})


async def after_provisioning(app):
    state = await app[REGISTRATION].register()
    return {**app[ONBOARDING].status(), 'registration': state}


async def setup_trust(request):
    body = await json_object(request)
    try:
        await asyncio.to_thread(request.app[ONBOARDING].trust, body.get('certificateSha256'))
    except SetupError as error:
        return setup_error(error, 409)
    except Exception as error:
        return setup_error(f'Could not check the router: {error}', 502)
    return web.json_response(request.app[ONBOARDING].status())


async def setup_otp(request):
    try:
        result = await asyncio.to_thread(request.app[ONBOARDING].start)
    except SetupError as error:
        return setup_error(error, 409)
    except Exception as error:
        return setup_error(f'Router setup failed: {error}', 502)
    if result['state'] == 'provisioned':
        return web.json_response(await after_provisioning(request.app))
    return web.json_response({**request.app[ONBOARDING].status(), **result})


async def setup_verify(request):
    body = await json_object(request)
    try:
        await asyncio.to_thread(request.app[ONBOARDING].verify, body.get('otp'))
    except SetupError as error:
        return setup_error(error, 409)
    except Exception as error:
        return setup_error(f'Code verification failed: {error}', 502)
    return web.json_response(await after_provisioning(request.app))


async def setup_reset(request):
    calls = request.app[CALLS]
    if calls.current and calls.current.state in LIVE:
        await calls.hangup(calls.current)
    await request.app[REGISTRATION].reset()
    await asyncio.to_thread(request.app[ONBOARDING].reset)
    await asyncio.to_thread(request.app[HISTORY].clear)
    await asyncio.to_thread(request.app[CONTACTS].clear)
    await asyncio.to_thread(request.app[SCREENING].clear)
    return web.json_response(request.app[ONBOARDING].status())


async def history_list(request):
    try:
        limit = max(1, min(500, int(request.query.get('limit', '200'))))
    except ValueError:
        raise web.HTTPBadRequest(text='Invalid limit')
    return web.json_response(await asyncio.to_thread(request.app[HISTORY].list, limit))


async def history_seen(request):
    return web.json_response(await asyncio.to_thread(request.app[HISTORY].mark_seen))


async def history_delete(request):
    if not await asyncio.to_thread(request.app[HISTORY].delete, request.match_info['id']):
        raise web.HTTPNotFound(text='Call log entry not found')
    return web.json_response({'deleted': True})


async def history_clear(request):
    await asyncio.to_thread(request.app[HISTORY].clear)
    return web.json_response({'cleared': True})


async def contacts_list(request):
    return web.json_response(await asyncio.to_thread(request.app[CONTACTS].snapshot))


async def favorites_set(request):
    body = await json_object(request)
    try:
        favorites = await asyncio.to_thread(request.app[CONTACTS].favorite, body.get('number'), bool(body.get('favorite')))
    except ValueError as error:
        raise web.HTTPBadRequest(text=str(error))
    return web.json_response({'favorites': favorites})


async def contacts_set(request):
    body = await json_object(request)
    try:
        result = await asyncio.to_thread(request.app[CONTACTS].set, body.get('number'), body.get('name', ''))
    except ValueError as error:
        raise web.HTTPBadRequest(text=str(error))
    return web.json_response(result)


async def screening_status(request):
    return web.json_response(await asyncio.to_thread(request.app[SCREENING].status))


async def block_number(request):
    body = await json_object(request)
    try:
        await asyncio.to_thread(request.app[SCREENING].block, body.get('number'))
    except ValueError as error:
        raise web.HTTPBadRequest(text=str(error))
    return web.json_response(await asyncio.to_thread(request.app[SCREENING].status))


async def unblock_number(request):
    if not await asyncio.to_thread(request.app[SCREENING].unblock, request.match_info['key']):
        raise web.HTTPNotFound(text='Number is not blocked')
    return web.json_response(await asyncio.to_thread(request.app[SCREENING].status))


async def update_dnd(request):
    body = await json_object(request)
    try:
        dnd = await asyncio.to_thread(request.app[SCREENING].update_dnd, body)
    except ValueError as error:
        raise web.HTTPBadRequest(text=str(error))
    return web.json_response(dnd)


async def watch_parent(pid):
    # When launched by the Mac app, exit (and deregister) if the app goes away.
    while os.getppid() == pid:
        await asyncio.sleep(2)
    os.kill(os.getpid(), signal.SIGTERM)


async def startup(app):
    app[REGISTRATION].start()
    parent = os.environ.get('GATEWAY_PARENT_PID', '')
    if parent.isdigit():
        task = asyncio.create_task(watch_parent(int(parent)))
        app[TASKS].add(task)
        task.add_done_callback(app[TASKS].discard)


async def offer(request):
    if request.content_type != 'application/json':
        raise web.HTTPUnsupportedMediaType()
    if len(request.app[PEERS]) >= 2:
        raise web.HTTPTooManyRequests(text='Stop the existing audio test first')
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text='Expected JSON')
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text='Expected an offer object')
    if body.get('type') != 'offer' or not isinstance(body.get('sdp'), str):
        raise web.HTTPBadRequest(text='Expected a WebRTC audio offer')
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    session_id = secrets.token_urlsafe(18)
    request.app[PEERS][session_id] = pc

    @pc.on('track')
    def on_track(track):
        if track.kind == 'audio':
            pc.addTrack(track)  # Actual received browser audio, returned over WebRTC.

    @pc.on('connectionstatechange')
    async def changed():
        if pc.connectionState in ('failed', 'closed'):
            request.app[PEERS].pop(session_id, None)
            await pc.close()
    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=body['sdp'], type='offer'))
        if not pc.getTransceivers() or any(t.kind != 'audio' for t in pc.getTransceivers()):
            raise ValueError('Audio only')
        await pc.setLocalDescription(await pc.createAnswer())
    except Exception:
        request.app[PEERS].pop(session_id, None)
        await pc.close()
        raise web.HTTPBadRequest(text='Could not negotiate audio')
    async def expire():
        await asyncio.sleep(120)
        peer = request.app[PEERS].pop(session_id, None)
        if peer:
            await peer.close()
    task = asyncio.create_task(expire())
    request.app[TASKS].add(task)
    task.add_done_callback(request.app[TASKS].discard)
    return web.json_response({'sdp': pc.localDescription.sdp, 'type': 'answer',
                              'sessionId': session_id, 'purpose': 'local-audio-loopback'})


async def stop(request):
    peer = request.app[PEERS].pop(request.match_info['id'], None)
    if peer:
        await peer.close()
    return web.json_response({'stopped': True})


async def cleanup(app):
    await app[CALLS].close()
    await app[REGISTRATION].close()
    for task in list(app[TASKS]):
        task.cancel()
    await asyncio.gather(*list(app[TASKS]), return_exceptions=True)
    await asyncio.gather(*(pc.close() for pc in list(app[PEERS].values())))


def create_app(registration=None, onboarding=None):
    app = web.Application(middlewares=[local_only], client_max_size=128 * 1024)
    app[PEERS] = {}
    app[TASKS] = set()
    app[DIAGNOSTIC_LOCK] = asyncio.Lock()
    app[REGISTRATION] = registration or Registration()
    config_path = getattr(app[REGISTRATION], 'config_path', CONFIG)
    app[HISTORY] = CallHistory(config_path.parent / 'call-history.json')
    app[CONTACTS] = LocalContacts(config_path.parent / 'contacts.json')
    app[SCREENING] = CallScreening(config_path.parent / 'screening.json')
    app[CALLS] = Calls(app[REGISTRATION], history=app[HISTORY], screening=app[SCREENING])
    app[ONBOARDING] = onboarding or Onboarding(config_path.parent, config_path)
    app.router.add_get('/api/setup', setup_status)
    app.router.add_get('/api/setup/router', setup_router)
    app.router.add_post('/api/setup/trust', setup_trust)
    app.router.add_post('/api/setup/otp', setup_otp)
    app.router.add_post('/api/setup/verify', setup_verify)
    app.router.add_post('/api/setup/reset', setup_reset)
    app.router.add_get('/api/history', history_list)
    app.router.add_post('/api/history/seen', history_seen)
    app.router.add_delete('/api/history', history_clear)
    app.router.add_delete('/api/history/{id}', history_delete)
    app.router.add_get('/api/contacts', contacts_list)
    app.router.add_put('/api/contacts', contacts_set)
    app.router.add_put('/api/favorites', favorites_set)
    app.router.add_get('/api/screening', screening_status)
    app.router.add_post('/api/blocked', block_number)
    app.router.add_delete('/api/blocked/{key}', unblock_number)
    app.router.add_put('/api/dnd', update_dnd)
    app.router.add_get('/api/status', status)
    app.router.add_post('/api/registration/retry', retry_registration)
    app.router.add_get('/api/registration/status', registration_status)
    app.router.add_get('/api/calls/current', calls_status)
    app.router.add_post('/api/calls', create_call)
    app.router.add_post('/api/calls/{id}/answer', answer_call)
    app.router.add_post('/api/calls/{id}/dtmf', dtmf_call)
    app.router.add_post('/api/calls/{id}/participants', add_participant)
    app.router.add_post('/api/calls/{id}/participants/cancel', cancel_participant)
    app.router.add_delete('/api/calls/{id}', end_call)
    app.router.add_post('/api/audio/offer', offer)
    app.router.add_delete('/api/audio/{id}', stop)
    async def index(request):
        return web.FileResponse(ROOT / 'static/index.html')
    app.router.add_get('/', index)
    for name in ['app.js', 'style.css', 'setup.js', 'icon.svg']:
        async def asset(request, name=name):
            return web.FileResponse(ROOT / 'static' / name)
        app.router.add_get('/' + name, asset)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


if __name__ == '__main__':
    web.run_app(create_app(), host='127.0.0.1', port=PORT, access_log=None,
                print=lambda message: print(message, flush=True))
