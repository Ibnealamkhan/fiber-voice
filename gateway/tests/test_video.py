import asyncio
import struct
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import av
from calls import Calls
from media import MediaSession
from test_calls import FakeMedia, FakeRegistration, eventually
from test_media import SDP
from video import VideoSession, parse_video

VIDEO = ('m=video 41000 RTP/AVPF 102 103\r\nb=AS:640\r\n'
         'a=rtpmap:102 H264/90000\r\na=fmtp:102 profile-level-id=42c01f;packetization-mode=1\r\n'
         'a=imageattr:102 send [x=640,y=480] [x=480,y=640] recv [x=640,y=480] [x=480,y=640]\r\n'
         'a=rtcp-fb:102 nack pli\r\n'
         'a=rtpmap:103 H264/90000\r\na=fmtp:103 profile-level-id=42c00c;packetization-mode=1\r\n'
         'a=sendrecv\r\n')
OFFER = SDP + VIDEO


def frame(index, width=640, height=480):
    picture = av.VideoFrame(width, height, 'yuv420p')
    luma = bytes(((x + index * 8) % 256) for x in range(width)) * height
    picture.planes[0].update(luma[:picture.planes[0].buffer_size])
    for plane in picture.planes[1:]:
        plane.update(bytes([128]) * plane.buffer_size)
    return picture


class FakeTrack:
    def __init__(self, count):
        self.count, self.index = count, 0

    async def recv(self):
        if self.index >= self.count:
            await asyncio.sleep(3600)
        self.index += 1
        await asyncio.sleep(1 / 14)
        return frame(self.index)


class VideoSdpTests(unittest.TestCase):
    def test_parse_jio_style_offer(self):
        remote = parse_video(OFFER)
        self.assertEqual((remote['pt'], remote['width'], remote['height'], remote['protocol']), (102, 640, 480, 'RTP/AVPF'))
        self.assertEqual(remote['port'], 41000)
        self.assertIsNone(parse_video(SDP))
        self.assertIsNone(parse_video(SDP + VIDEO.replace('41000', '0', 1)))
        self.assertIsNone(parse_video(SDP + VIDEO.replace('packetization-mode=1', 'packetization-mode=0')))

    def test_small_profile_selects_240p(self):
        offer = SDP + VIDEO.replace('m=video 41000 RTP/AVPF 102 103', 'm=video 41000 RTP/AVPF 103')
        remote = parse_video(offer)
        self.assertEqual((remote['pt'], remote['width'], remote['height']), (103, 320, 240))


class VideoAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_answer_declines_video_without_a_video_session(self):
        media = await MediaSession('::1').open()
        try:
            media.negotiate(OFFER, incoming=True)
            answer = media.sdp(answer=True)
            self.assertIn('m=audio ', answer)
            self.assertIn('m=video 0 RTP/AVPF 102', answer)
            self.assertLess(answer.index('m=audio'), answer.index('m=video'))
        finally:
            await media.close()

    async def test_answer_accepts_video_with_a_video_session(self):
        media = await MediaSession('::1').open()
        media.video = await VideoSession('::1').open()
        try:
            media.negotiate(OFFER, incoming=True)
            answer = media.sdp(answer=True)
            self.assertIn(f'm=video {media.video.port} RTP/AVPF 102', answer)
            self.assertIn('a=fmtp:102 profile-level-id=42c01f;packetization-mode=1', answer)
            self.assertIn('a=rtcp-fb:102 nack pli', answer)
            offer = MediaSession.sdp(media)
            self.assertIn('m=video', offer)
            self.assertIn('H264/90000', offer)
        finally:
            await media.close()


class VideoRoundTripTests(unittest.IsolatedAsyncioTestCase):
    async def test_h264_rtp_roundtrip_and_keyframe_request(self):
        sender = await VideoSession('::1').open()
        receiver = await VideoSession('::1').open()
        try:
            remote = {'pt': 104, 'address': '::1', 'protocol': 'RTP/AVPF', 'level': '42c01f',
                      'width': 320, 'height': 240, 'direction': 'sendrecv', 'bitrate': 256_000}
            sender.remote = {**remote, 'port': receiver.port, 'rtcpPort': receiver.port + 1}
            receiver.remote = {**remote, 'port': sender.port, 'rtcpPort': sender.port + 1}
            from video import NetworkEncoder
            sender.encoder = NetworkEncoder(256_000, '12')
            task = asyncio.create_task(sender.attach(FakeTrack(12)))
            got = await asyncio.wait_for(receiver.frames.get(), 10)
            self.assertEqual((got.width, got.height), (320, 240))
            self.assertGreater(sender.counters['videoFramesSent'], 0)
            self.assertGreater(receiver.counters['videoFramesDecoded'], 0)
            # A PLI from the far end makes the next encoded picture a keyframe.
            self.assertGreaterEqual(receiver.counters['pliSent'], 1)  # asked for a keyframe before the first picture
            sender.force_keyframe = False
            before = sender.counters['pliReceived']
            pli = struct.pack('!BBHII', 0x81, 206, 2, 1, sender.ssrc)
            sender.receive_rtcp(pli, ('::1', receiver.port + 1))
            self.assertTrue(sender.force_keyframe)
            self.assertEqual(sender.counters['pliReceived'], before + 1)
            task.cancel()
        finally:
            await sender.close()
            await receiver.close()


class VideoSignalingTests(unittest.IsolatedAsyncioTestCase):
    async def test_outgoing_video_invite_has_feature_tags_and_video_sdp(self):
        reg = FakeRegistration('cancel')
        calls = Calls(reg, FakeMedia)
        try:
            call = await calls.prepare_outgoing('9876543210', video=False)
            await calls.finish(call, 'cancelled')
            call = await calls.prepare_outgoing('9876543210', video=True)
            self.assertTrue(call.video)
            self.assertIsNotNone(call.media.video)
            call.invite_started = True
            call.spawn(calls.invite(call))
            await eventually(lambda: any(s.startswith('INVITE') and 'Authorization' in s for s in reg.sent))
            invite = next(s for s in reg.sent if s.startswith('INVITE') and 'Authorization' in s)
            self.assertIn(';video\r\n', invite.split('Contact: ', 1)[1].split('\r\n', 1)[0] + '\r\n')
            self.assertIn('Accept-Contact: *;+g.3gpp.icsi-ref="urn%3Aurn-7%3A3gpp-service.ims.icsi.mmtel";video', invite)
            await calls.hangup(call)
        finally:
            await calls.close()

    async def test_incoming_video_can_be_answered_voice_only(self):
        reg = FakeRegistration()
        calls = Calls(reg, FakeMedia)
        try:
            headers = {'via': ['SIP/2.0/TLS router.example.test;branch=z9hG4bKv'], 'from': ['<sip:bob@example.test>;tag=caller'],
                       'to': ['<sip:alice@example.test>'], 'call-id': ['video-call'], 'cseq': ['1 INVITE'],
                       'contact': ['<sip:bob@router.example.test>']}
            await calls.incoming('INVITE sip:alice@example.test SIP/2.0', headers, OFFER.encode())
            await eventually(lambda: any(s.startswith('SIP/2.0 180') for s in reg.sent))
            call = calls.current
            self.assertTrue(call.offered_video)
            self.assertTrue(call.public()['offeredVideo'])
            await calls.choose_video(call, False)
            self.assertIsNone(call.media.video)
            self.assertFalse(call.video)
            await calls.hangup(call)
        finally:
            await calls.close()


if __name__ == '__main__':
    unittest.main()
