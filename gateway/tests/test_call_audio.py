import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asyncio
import re
import math
import struct
import unittest
from fractions import Fraction
from unittest.mock import patch

import av
from aiohttp import ClientSession, web
from aiortc import AudioStreamTrack, RTCPeerConnection, RTCSessionDescription, RTCConfiguration
from server import create_app
from media import MediaSession,parse_sdp
from test_calls import FakeRegistration,eventually

class Tone(AudioStreamTrack):
    def __init__(self):super().__init__();self.pts=0;self.start=None
    async def recv(self):
        loop=asyncio.get_running_loop()
        if self.start is None:self.start=loop.time()
        await asyncio.sleep(max(0,self.start+self.pts/8000-loop.time()))
        frame=av.AudioFrame(format='s16',layout='mono',samples=160)
        pcm=struct.pack('<'+'h'*160,*[int(5000*math.sin(2*math.pi*440*(self.pts+i)/8000)) for i in range(160)])
        frame.planes[0].update(pcm);frame.sample_rate=8000;frame.time_base=Fraction(1,8000);frame.pts=self.pts;self.pts+=160
        return frame

class SimulatedRouter(FakeRegistration):
    def __init__(self,media,pt=120):super().__init__();self.media=media;self.pt=pt
    def start(self):pass
    async def close(self):pass
    def reply(self,method,seq,code,extra=(),body=''):
        return super().reply(method,seq,code,extra,self.media.sdp(answer=True) if body else '')
    async def _send(self,message):
        if message.startswith('INVITE') and self.media.remote is None:
            sdp=message.split('\r\n\r\n',1)[1]
            sdp=re.sub(r'(?m)^(m=audio [0-9]+ RTP/AVP )[^\r\n]+',lambda m:m[1]+str(self.pt),sdp)
            self.media.negotiate(sdp,incoming=True)
        await super()._send(message)

class CompleteAudioTest(unittest.IsolatedAsyncioTestCase):
    async def test_browser_opus_to_amr_rtp_and_back(self):
        await self.roundtrip(120)

    async def test_browser_opus_to_narrowband_bandwidth_efficient_rtp(self):
        await self.roundtrip(119)

    async def roundtrip(self,pt):
        remote=await MediaSession('::1').open();reg=SimulatedRouter(remote,pt)
        real=parse_sdp
        def local_sdp(sdp,require_offered=True):
            parsed=real(sdp.replace('IN IP6 ::1','IN IP6 2001:db8::5'),require_offered)
            parsed['address']='::1';return parsed
        runner=web.AppRunner(create_app(reg));await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        base=f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
        pc=RTCPeerConnection(RTCConfiguration(iceServers=[]));received=asyncio.get_running_loop().create_future()
        @pc.on('track')
        def track(value):
            if not received.done():received.set_result(value)
        pc.addTrack(Tone())
        try:
            with patch('media.parse_sdp',local_sdp):
                await pc.setLocalDescription(await pc.createOffer())
                async with ClientSession(headers={'Host':'127.0.0.1:8765'}) as client:
                    async with client.post(base+'/api/calls',json={'number':'9876543210','sdp':pc.localDescription.sdp,'type':'offer'}) as r:
                        self.assertEqual(r.status,200);answer=await r.json()
                    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer['sdp'],type=answer['type']))
                    await eventually(lambda:reg.call_handler.current.state=='active')
                    remote.spawn(remote.attach(Tone()))
                    return_track=await asyncio.wait_for(received,5)
                    audible=False
                    for _ in range(25):
                        frame=await asyncio.wait_for(return_track.recv(),3)
                        if max(abs(v) for v in memoryview(bytes(frame.planes[0])).cast('h'))>100:audible=True
                    call=reg.call_handler.current
                    self.assertGreater(call.media.stats()['rtpSent'],10)
                    self.assertGreater(call.media.stats()['rtpReceived'],10)
                    self.assertGreater(remote.stats()['rtpReceived'],10)
                    self.assertIsNone(call.media.error)
                    self.assertIsNone(remote.error)
                    self.assertTrue(audible,'WebRTC must receive decoded AMR speech, not only silence')
                    peak=0
                    for _ in range(40):
                        pcm=remote.receive_pcm()
                        peak=max(peak,max(abs(v) for v in memoryview(pcm).cast('h')))
                    self.assertGreater(peak,100,'The simulated SIP peer must receive browser speech')
                    async with client.delete(base+'/api/calls/'+answer['callId']) as r:self.assertEqual(r.status,200)
                    self.assertEqual(call.state,'ended')
        finally:
            await pc.close();await runner.cleanup();await remote.close()
