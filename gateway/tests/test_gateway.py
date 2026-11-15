import asyncio
import sys
import unittest
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp import ClientSession, web
from aiortc import AudioStreamTrack, RTCPeerConnection, RTCSessionDescription, RTCConfiguration
from server import create_app, parse_sip, PEERS, CALLS
from registration import Registration


class ParserTests(unittest.TestCase):
    def test_audio_codecs_exclude_video_and_dtmf(self):
        result = parse_sip('SIP/2.0 200 OK\r\nAllow: OPTIONS, REGISTER\r\n\r\nm=audio 123 RTP/AVP 104 101\r\na=rtpmap:104 AMR-WB/16000\r\na=rtpmap:101 telephone-event/8000\r\nm=video 125 RTP/AVP 97\r\na=rtpmap:97 H264/90000\r\n')
        self.assertEqual(result['audioCodecs'], ['AMR-WB'])
        self.assertEqual(result['code'], 200)
        self.assertEqual(result['methods'], ['OPTIONS', 'REGISTER'])


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(Registration(Path(self.temp.name) / 'provisioning.json'))
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base = f'http://127.0.0.1:{port}'
        self.client = ClientSession(headers={'Host': '127.0.0.1:8765'})

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()
        self.temp.cleanup()

    async def test_foreign_origin_and_host_cannot_access_router(self):
        async with self.client.get(self.base + '/api/status', headers={'Origin':'https://example.com'}) as r:
            self.assertEqual(r.status, 403)
        async with self.client.get(self.base + '/', headers={'Host':'evil.example:8765'}) as r:
            self.assertEqual(r.status, 403)
        async with self.client.get(self.base + '/api/status', headers={'Sec-Fetch-Site':'cross-site'}) as r:
            self.assertEqual(r.status, 403)
        self.assertEqual(len(self.app[PEERS]), 0)

    async def test_bad_offer_releases_peer(self):
        async with self.client.post(self.base + '/api/audio/offer', json={'sdp':'invalid','type':'offer'}) as r:
            self.assertEqual(r.status, 400)
        self.assertEqual(len(self.app[PEERS]), 0)

    async def test_call_offer_validation_has_no_side_effects(self):
        async with self.client.post(self.base + '/api/calls', json={'number':'9876543210'}) as r:
            self.assertEqual(r.status, 400)
        self.assertIsNone(self.app[CALLS].current)
        async with self.client.post(self.base + '/api/calls', headers={'Origin':'https://example.com'}, json={}) as r:
            self.assertEqual(r.status, 403)
        self.assertIsNone(self.app[CALLS].current)

    async def test_audio_roundtrip_and_explicit_cleanup(self):
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        received = asyncio.get_running_loop().create_future()
        @pc.on('track')
        def track(track):
            if not received.done(): received.set_result(track)
        pc.addTrack(AudioStreamTrack())
        try:
            await pc.setLocalDescription(await pc.createOffer())
            async with self.client.post(self.base + '/api/audio/offer', json={'sdp':pc.localDescription.sdp,'type':'offer'}) as r:
                self.assertEqual(r.status, 200)
                result = await r.json()
            self.assertEqual(result['purpose'], 'local-audio-loopback')
            await pc.setRemoteDescription(RTCSessionDescription(sdp=result['sdp'],type=result['type']))
            track = await asyncio.wait_for(received, 5)
            frame = await asyncio.wait_for(track.recv(), 8)
            self.assertGreater(frame.samples, 0)
            self.assertEqual(frame.sample_rate, 48000)
            async with self.client.delete(self.base + '/api/audio/' + result['sessionId']) as r:
                self.assertEqual(r.status, 200)
            self.assertEqual(len(self.app[PEERS]), 0)
        finally:
            await pc.close()

if __name__ == '__main__': unittest.main()
