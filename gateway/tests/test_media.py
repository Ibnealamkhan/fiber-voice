import asyncio
import math
import struct
import sys
import unittest
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import av
from media import AmrCodec, pack_amr, unpack_amr, parse_rtp, parse_sdp, MediaSession, codec_capabilities

SDP='v=0\r\nc=IN IP6 2001:db8::5\r\nm=audio 52000 RTP/AVP 120 127\r\na=rtpmap:120 AMR-WB/16000\r\na=fmtp:120 octet-align=1;mode-set=0,1,2\r\na=rtpmap:127 telephone-event/16000\r\n'

class MediaTests(unittest.TestCase):
    def test_windows_bundled_codec_uses_dll_and_does_not_hide_load_errors(self):
        from media import library
        with patch('media.os.name', 'nt'), patch.dict('os.environ', {'GATEWAY_LIB_DIR': 'bundled'}), \
             patch('media.os.path.isfile', return_value=True), patch('media.C.CDLL') as load:
            library('opencore-amrnb')
            self.assertTrue(load.call_args.args[0].endswith('libopencore-amrnb-0.dll'))
            load.side_effect = OSError('Missing dependency')
            with self.assertRaisesRegex(OSError, 'Missing dependency'):
                library('opencore-amrnb')

    def test_all_native_modes_and_both_rfc4867_payloads(self):
        self.assertTrue(codec_capabilities()['ready'])
        for wb in (False,True):
            codec=AmrCodec(wb)
            pcm=struct.pack('<'+'h'*codec.samples,*[int(6000*math.sin(2*math.pi*440*i/codec.rate)) for i in range(codec.samples)])
            independent=av.CodecContext.create('amrwb' if wb else 'amrnb','r')
            try:
                for mode in range(9 if wb else 8):
                    codec.mode=mode;frame=codec.encode(pcm)
                    for aligned in (False,True):
                        payload=pack_amr([frame,frame],wb,aligned)
                        cmr,decoded=unpack_amr(payload,wb,aligned)
                        self.assertEqual(cmr,15);self.assertEqual(decoded,[frame,frame])
                    self.assertEqual(len(codec.decode(frame)),len(pcm))
                    frames=independent.decode(av.Packet(frame))
                    self.assertEqual(sum(f.samples for f in frames),codec.samples)
                    self.assertTrue(any(bytes(f.planes[0])!=bytes(f.planes[0].buffer_size) for f in frames))
            finally:codec.close()

    def test_known_octet_aligned_and_bandwidth_efficient_headers(self):
        frame=b'\x3c'+bytes(31)  # FT=7 (12.2 kb/s), Q=1, 244 speech bits.
        self.assertEqual(pack_amr([frame]),b'\xf0\x3c'+bytes(31))
        self.assertEqual(pack_amr([frame],octet_align=False),b'\xf3\xc0'+bytes(30))
        with self.assertRaises(ValueError):unpack_amr(b'\xf0\x3c\0')
        with self.assertRaises(ValueError):unpack_amr(b'\xf0\x4c')

    def test_rtp_csrc_extension_padding_and_invalid_lengths(self):
        packet=struct.pack('!BBHII',0xb1,120,42,1000,19)+bytes(4)+struct.pack('!HH',1,1)+bytes(4)+b'audio'+b'\0\0\0\4'
        self.assertEqual(parse_rtp(packet),(120,42,1000,19,b'audio'))
        with self.assertRaises(ValueError):parse_rtp(packet[:16])
        with self.assertRaises(ValueError):parse_rtp(b'bad')

    def test_sdp_selects_offered_payload_and_mode_set(self):
        s=parse_sdp(SDP)
        self.assertEqual((s['codec'],s['pt'],s['mode'],s['eventPt']),('AMR-WB',120,2,127))
        with self.assertRaises(ValueError):parse_sdp(SDP.replace('octet-align=1','octet-align=0'))
        with self.assertRaises(ValueError):parse_sdp(SDP.replace('2001:db8::5','::1'))
        with self.assertRaises(ValueError):parse_sdp(SDP.replace('octet-align=1','octet-align=1;interleaving=2'))

class MediaNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_amr_rtp_roundtrip_over_ipv6_udp(self):
        # Explicit test-only loopback endpoints; production SDP rejects loopback.
        a=await MediaSession('::1').open();b=await MediaSession('::1').open()
        try:
            from unittest.mock import patch
            real=parse_sdp
            def local(sdp,require_offered=True):
                result=real(sdp.replace('IN IP6 ::1','IN IP6 2001:db8::5'),require_offered)
                result['address']='::1';return result
            with patch('media.parse_sdp',local):
                a.negotiate(b.sdp());b.negotiate(a.sdp())
            for i in range(15):
                samples=[int(6000*math.sin(2*math.pi*440*(i*a.codec.samples+j)/a.rate)) for j in range(a.codec.samples)]
                a.input_queue.put_nowait(struct.pack('<'+'h'*len(samples),*samples))
            await asyncio.sleep(.22)
            self.assertGreater(a.counters['rtpSent'],4)
            self.assertGreater(b.counters['rtpReceived'],4)
            pcm=b.receive_pcm()
            self.assertEqual(len(pcm),640)
            out=await b.track.recv()
            self.assertEqual((out.sample_rate,out.samples),(48000,960))
            self.assertGreater(b.stats()['bytesReceived'],100)
        finally:await a.close();await b.close()

class DtmfTests(unittest.IsolatedAsyncioTestCase):
    async def test_rfc4733_event_duration_and_repeated_end(self):
        packets=[]
        class Capture:
            def sendto(self,packet,address):packets.append(packet)
            def close(self):pass
        media=MediaSession('::1');media.rtp=Capture()
        media.remote={'address':'::1','port':5000,'eventPt':127}
        try:
            await media.dtmf('5')
            self.assertEqual(len(packets),11)
            parsed=[parse_rtp(p) for p in packets]
            self.assertTrue(packets[0][1]&128)
            self.assertTrue(all(p[2]==parsed[0][2] for p in parsed))
            for payload in [p[4] for p in parsed[-3:]]:
                event,flags,duration=struct.unpack('!BBH',payload)
                self.assertEqual(event,5);self.assertTrue(flags&128);self.assertEqual(duration,2560)
        finally:await media.close()
