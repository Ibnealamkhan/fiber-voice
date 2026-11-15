import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asyncio
import unittest
from fractions import Fraction
from unittest.mock import patch

import av
from aiohttp import ClientSession, web
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
import video as video_module
from media import MediaSession, parse_sdp
from server import create_app
from test_call_audio import SimulatedRouter, Tone
from test_calls import eventually
from video import VideoSession, NetworkEncoder


class Camera(VideoStreamTrack):
    """Synthetic 640x480 camera with a moving gradient."""

    def __init__(self):
        super().__init__()
        self.index = 0

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        picture = av.VideoFrame(640, 480, 'yuv420p')
        row = bytes(((x + self.index * 6) % 256) for x in range(640))
        picture.planes[0].update((row * 480)[:picture.planes[0].buffer_size])
        for plane in picture.planes[1:]:
            plane.update(bytes([128]) * plane.buffer_size)
        self.index += 1
        picture.pts, picture.time_base = pts, time_base
        return picture


def loopback(parser):
    def wrapped(sdp, *args, **kwargs):
        parsed = parser(sdp.replace('IN IP6 ::1', 'IN IP6 2001:db8::5'), *args, **kwargs)
        if parsed:
            parsed['address'] = '::1'
        return parsed
    return wrapped


class CompleteVideoCallTest(unittest.IsolatedAsyncioTestCase):
    async def test_browser_video_to_h264_rtp_and_back(self):
        remote = await MediaSession('::1').open()
        remote.video = await VideoSession('::1').open()
        reg = SimulatedRouter(remote)
        runner = web.AppRunner(create_app(reg))
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        base = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        got_video = asyncio.get_running_loop().create_future()

        @pc.on('track')
        def track(value):
            if value.kind == 'video' and not got_video.done():
                got_video.set_result(value)
        pc.addTrack(Tone())
        pc.addTrack(Camera())
        try:
            with patch('media.parse_sdp', loopback(parse_sdp)), patch('video.parse_video', loopback(video_module.parse_video)):
                await pc.setLocalDescription(await pc.createOffer())
                async with ClientSession(headers={'Host': '127.0.0.1:8765'}) as client:
                    async with client.post(base + '/api/calls', json={'number': '9876543210', 'video': True,
                                                                      'sdp': pc.localDescription.sdp, 'type': 'offer'}) as r:
                        self.assertEqual(r.status, 200, await r.text())
                        answer = await r.json()
                    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer['sdp'], type=answer['type']))
                    for _ in range(100):
                        if reg.call_handler.current.state in ('active', 'failed', 'ended', 'cancelled'):
                            break
                        await asyncio.sleep(0.05)
                    self.assertEqual(reg.call_handler.current.state, 'active', reg.call_handler.current.public())
                    call = reg.call_handler.current
                    self.assertTrue(call.public()['videoActive'])
                    invite = next(s for s in reg.sent if s.startswith('INVITE') and 'm=video' in s)
                    self.assertIn('H264/90000', invite)

                    # Browser camera -> gateway (H.264 encode) -> simulated network (decode).
                    network_frame = await asyncio.wait_for(remote.video.frames.get(), 15)
                    self.assertIn((network_frame.width, network_frame.height), [(640, 480), (320, 240)])

                    # Simulated network camera -> gateway (H.264 decode) -> browser (WebRTC).
                    remote.video.encoder = remote.video.encoder or NetworkEncoder(600_000, '31')
                    remote.video.spawn(remote.video.attach(Camera()))
                    browser_track = await asyncio.wait_for(got_video, 10)
                    browser_frame = await asyncio.wait_for(browser_track.recv(), 15)
                    self.assertGreater(browser_frame.width, 0)
                    stats = call.media.stats()['video']
                    self.assertGreater(stats['videoFramesSent'], 0)
                    self.assertGreater(stats['videoFramesDecoded'], 0)
                    async with client.delete(base + '/api/calls/' + answer['callId']) as r:
                        self.assertEqual(r.status, 200)
                    self.assertEqual(call.state, 'ended')
        finally:
            await pc.close()
            await runner.cleanup()
            await remote.close()


if __name__ == '__main__':
    unittest.main()
