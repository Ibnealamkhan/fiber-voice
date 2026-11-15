"""H.264 video bridge between browser WebRTC video and the landline network's RTP video.

Network leg: H.264 Constrained Baseline, packetization-mode 1 (RFC 6184), RTP/AVPF on IPv6,
640x480 or 320x240 at 15 fps, with RTCP PLI keyframe requests in both directions. These match
what Jio's own client advertises (H264-PM1-480 / H264-PM1-240, ``nack pli``, ``imageattr``).

Browser leg: any codec aiortc negotiates with the browser (VP8/H.264); frames are decoded by
aiortc, scaled, re-encoded here, and the reverse for the other direction. Encoding/decoding
runs on dedicated threads so the audio path is never blocked.
"""
import asyncio
import fractions
import ipaddress
import re
import secrets
import socket
import struct
import time
from concurrent.futures import ThreadPoolExecutor

import av
from aiortc import VideoStreamTrack
from aiortc.codecs.h264 import H264Decoder, H264Encoder, H264PayloadDescriptor
from aiortc.jitterbuffer import JitterFrame

from media import media_sections, parse_rtp

CLOCK = 90000
FPS = 15
# Offered formats, best first: payload type -> (profile-level-id, width, height, kbit/s, x264 level)
OFFER_FORMATS = {104: ('42c01f', 640, 480, 600, '31'), 105: ('42c00c', 320, 240, 256, '12')}
TIME_BASE = fractions.Fraction(1, CLOCK)


def parse_video(sdp):
    """The remote's accepted H.264 video, or None when there is no (enabled) video section."""
    session, sections = media_sections(sdp)
    video = next((s for s in sections if s[0] == 'video'), None)
    if video is None or video[1] == 0:
        return None
    kind, port, protocol, fmts, lines = video
    if protocol not in ('RTP/AVP', 'RTP/AVPF') or not 0 < port < 65535:
        return None
    address = None
    for line in session + lines:
        if line.startswith('c='):
            address = line[2:].split()[2].split('/')[0]
    maps, fmtp, attrs, direction, rtcp_port = {}, {}, {}, 'sendrecv', None
    for line in lines:
        if line.startswith('a=rtpmap:'):
            pt, value = line[9:].split(None, 1)
            maps[int(pt)] = value.split('/')[0].upper()
        elif line.startswith('a=fmtp:'):
            pt, value = line[7:].split(None, 1)
            fmtp[int(pt)] = {k.strip().lower(): v.strip() for item in value.split(';') if '=' in item for k, v in [item.split('=', 1)]}
        elif line.startswith('a=imageattr:'):
            pt, value = line[12:].split(None, 1)
            attrs[pt] = value
        elif line in ('a=sendrecv', 'a=sendonly', 'a=recvonly', 'a=inactive'):
            direction = line[2:]
        elif line.startswith('a=rtcp:'):
            rtcp_port = int(line[7:].split()[0])
    try:
        ip = ipaddress.ip_address(address or '')
    except ValueError:
        return None
    if ip.version != 6 or ip.is_multicast or ip.is_loopback or ip.is_unspecified:
        return None
    for pt in (int(f) for f in fmts if f.isdigit()):
        params = fmtp.get(pt, {})
        if maps.get(pt) != 'H264' or params.get('packetization-mode', '0') != '1':
            continue
        level = params.get('profile-level-id', '42c01f').lower()
        if not re.fullmatch(r'[0-9a-f]{6}', level) or level[:2] not in ('42', '4d', '58', '64'):
            continue
        width, height = (640, 480) if int(level[4:], 16) >= 0x15 else (320, 240)
        match = re.search(r'recv\s*\[x=(\d+),y=(\d+)', attrs.get(str(pt), attrs.get('*', '')))
        if match:
            width, height = min(int(match[1]), 640), min(int(match[2]), 640)
            if width * height > 640 * 480:
                width, height = (640, 480) if width >= height else (480, 640)
        return {'pt': pt, 'address': str(ip), 'port': port, 'rtcpPort': rtcp_port or port + 1,
                'protocol': protocol, 'level': level, 'width': width - width % 2, 'height': height - height % 2,
                'direction': direction, 'bitrate': 600_000 if width * height >= 640 * 480 else 256_000}
    return None


class NetworkEncoder(H264Encoder):
    """aiortc's H.264 packetizer with x264 settings matching the advertised profile/level."""

    def __init__(self, bitrate, level):
        super().__init__()
        self.fixed_bitrate, self.level = bitrate, level

    def _encode_frame(self, frame, force_keyframe):
        if self.codec and (frame.width != self.codec.width or frame.height != self.codec.height):
            self.codec = None
        if self.codec is None:
            codec = av.CodecContext.create('libx264', 'w')
            codec.width, codec.height = frame.width, frame.height
            codec.bit_rate = self.fixed_bitrate
            codec.pix_fmt = 'yuv420p'
            codec.framerate = fractions.Fraction(FPS, 1)
            codec.time_base = fractions.Fraction(1, FPS)
            codec.gop_size = FPS * 2
            codec.options = {'level': self.level, 'tune': 'zerolatency', 'preset': 'veryfast'}
            codec.profile = 'Baseline'
            self.codec = codec
        frame.pict_type = av.video.frame.PictureType.I if force_keyframe else av.video.frame.PictureType.NONE
        data = b''.join(bytes(packet) for packet in self.codec.encode(frame))
        if data:
            yield from self._split_bitstream(data)


class ReturnVideo(VideoStreamTrack):
    """Network video, decoded, delivered to the browser."""

    def __init__(self, video):
        super().__init__()
        self.video = video

    async def recv(self):
        frame = await self.video.frames.get()
        return frame


class VideoProtocol(asyncio.DatagramProtocol):
    def __init__(self, video, rtcp=False):
        self.video, self.rtcp = video, rtcp

    def datagram_received(self, data, address):
        (self.video.receive_rtcp if self.rtcp else self.video.receive_packet)(data, address)


class VideoSession:
    def __init__(self, local_address):
        self.local_address = local_address
        self.rtp = self.rtcp = None
        self.port = None
        self.remote = None
        self.closed = False
        self.ssrc = secrets.randbits(32)
        self.sequence = secrets.randbits(16)
        self.remote_ssrc = None
        self.frames = asyncio.Queue(maxsize=4)
        self.track = ReturnVideo(self)
        self.encoder = None
        self.decoder = H264Decoder()
        self.encode_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='video-encode')
        self.decode_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='video-decode')
        self.force_keyframe = True
        self.assembling = {}
        self.decoded_any = False
        self.last_pli = 0
        self.last_sent = 0
        self.start = time.monotonic()
        self.tasks = set()
        self.counters = {'videoSent': 0, 'videoReceived': 0, 'videoFramesSent': 0, 'videoFramesDecoded': 0,
                         'videoInvalid': 0, 'pliReceived': 0, 'pliSent': 0}

    async def open(self):
        loop = asyncio.get_running_loop()
        for port in range(52100, 52200, 2):  # upper half of the client's media range; audio starts at 52000
            sockets = []
            try:
                for p in (port, port + 1):
                    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
                    sock.setblocking(False)
                    sockets.append(sock)
                    sock.bind((self.local_address, p))
                self.rtp, _ = await loop.create_datagram_endpoint(lambda: VideoProtocol(self), sock=sockets[0])
                self.rtcp, _ = await loop.create_datagram_endpoint(lambda: VideoProtocol(self, True), sock=sockets[1])
                self.port = port
                return self
            except OSError:
                for sock in sockets:
                    sock.close()
        raise RuntimeError('No free local video RTP/RTCP ports')

    @property
    def active(self):
        return bool(self.remote) and self.remote['direction'] != 'inactive'

    def sdp_lines(self, answer=False):
        """The m=video section for our offer, or our answer to the remote's negotiated video."""
        if answer and not self.remote:
            return None
        formats = {self.remote['pt']: (self.remote['level'], self.remote['width'], self.remote['height'],
                                       self.remote['bitrate'] // 1000, '')} if answer else OFFER_FORMATS
        protocol = self.remote['protocol'] if answer else 'RTP/AVPF'
        lines = [f'm=video {self.port} {protocol} ' + ' '.join(map(str, formats)),
                 f'b=AS:{max(f[3] for f in formats.values())}',
                 f'a=rtcp:{self.port + 1} IN IP6 {self.local_address}']
        direction = self.remote['direction'] if answer else 'sendrecv'
        lines.append('a=' + {'sendonly': 'recvonly', 'recvonly': 'sendonly'}.get(direction, direction))
        for pt, (level, width, height, _, _) in formats.items():
            lines += [f'a=rtpmap:{pt} H264/{CLOCK}', f'a=fmtp:{pt} profile-level-id={level};packetization-mode=1',
                      f'a=imageattr:{pt} send [x={width},y={height}] [x={height},y={width}] '
                      f'recv [x={width},y={height}] [x={height},y={width}]',
                      f'a=rtcp-fb:{pt} nack pli']
        return lines

    def negotiate(self, sdp):
        remote = parse_video(sdp)
        if remote is None:
            self.remote = None
            return False
        changed = not self.remote or remote['pt'] != self.remote['pt'] or remote['width'] != self.remote['width']
        self.remote = remote
        if changed or self.encoder is None:
            level = '31' if max(remote['width'], remote['height']) > 320 else '12'
            self.encoder = NetworkEncoder(remote['bitrate'], level)
            self.force_keyframe = True
        if not self.tasks:
            self.spawn(self.reports())
        return True

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    # ---- browser -> network ----

    async def attach(self, track):
        loop = asyncio.get_running_loop()
        while not self.closed:
            try:
                frame = await track.recv()
            except Exception:
                return
            remote = self.remote
            now = time.monotonic()
            if not remote or remote['direction'] in ('recvonly', 'inactive') or now - self.last_sent < 1 / FPS - 0.005:
                continue
            self.last_sent = now
            width, height = remote['width'], remote['height']
            if (frame.height > frame.width) != (height > width):
                width, height = height, width  # follow the camera's orientation
            keyframe, self.force_keyframe = self.force_keyframe, False
            try:
                payloads, timestamp = await loop.run_in_executor(self.encode_pool, self._encode, frame, width, height, keyframe, now)
            except Exception:
                self.counters['videoInvalid'] += 1
                continue
            self.send_frame(payloads, timestamp)

    def _encode(self, frame, width, height, keyframe, now):
        scaled = frame.reformat(width=width, height=height, format='yuv420p')
        scaled.pts = int((now - self.start) * CLOCK)
        scaled.time_base = TIME_BASE
        return self.encoder.encode(scaled, force_keyframe=keyframe)

    def send_frame(self, payloads, timestamp):
        if not payloads or not self.remote or self.closed:
            return
        target = (self.remote['address'], self.remote['port'])
        for index, payload in enumerate(payloads):
            marker = 0x80 if index == len(payloads) - 1 else 0
            header = struct.pack('!BBHII', 0x80, marker | self.remote['pt'], self.sequence, timestamp & 0xffffffff, self.ssrc)
            self.sequence = (self.sequence + 1) & 0xffff
            self.rtp.sendto(header + payload, target)
            self.counters['videoSent'] += 1
        self.counters['videoFramesSent'] += 1

    # ---- network -> browser ----

    def receive_packet(self, packet, address):
        if not self.remote or self.closed or address[0] != self.remote['address']:
            return
        try:
            pt, seq, timestamp, ssrc, payload = parse_rtp(packet)
        except (ValueError, struct.error):
            self.counters['videoInvalid'] += 1
            return
        if pt != self.remote['pt'] or not payload:
            return
        self.remote_ssrc = ssrc
        self.counters['videoReceived'] += 1
        parts = self.assembling.setdefault(timestamp, {})
        parts[seq] = payload
        if packet[1] & 0x80:  # marker: last packet of this picture
            self.assembling.pop(timestamp, None)
            for stale in [t for t in self.assembling if (timestamp - t) & 0xffffffff < 1 << 31]:
                self.assembling.pop(stale, None)  # older, incomplete pictures
            ordered = [parts[s] for s in sorted(parts, key=lambda s: (seq - s) & 0xffff, reverse=True)]
            self.spawn(self.decode(ordered, timestamp))
        elif len(self.assembling) > 8:
            self.assembling.pop(next(iter(self.assembling)), None)
        if not self.decoded_any:
            self.request_keyframe()

    async def decode(self, payloads, timestamp):
        try:
            data = b''.join(H264PayloadDescriptor.parse(p)[1] for p in payloads)
            frames = await asyncio.get_running_loop().run_in_executor(
                self.decode_pool, self.decoder.decode, JitterFrame(data=data, timestamp=timestamp))
        except Exception:
            frames = []
        if not frames:
            self.request_keyframe()
            return
        self.decoded_any = True
        for frame in frames:
            frame.pts = int((time.monotonic() - self.start) * CLOCK)
            frame.time_base = TIME_BASE
            if self.frames.full():
                self.frames.get_nowait()
            self.frames.put_nowait(frame)
            self.counters['videoFramesDecoded'] += 1

    # ---- RTCP ----

    def request_keyframe(self):
        now = time.monotonic()
        if not self.remote or self.remote_ssrc is None or now - self.last_pli < 1:
            return
        self.last_pli = now
        pli = struct.pack('!BBHII', 0x81, 206, 2, self.ssrc, self.remote_ssrc)
        self.rtcp.sendto(self.receiver_report() + pli, (self.remote['address'], self.remote['rtcpPort']))
        self.counters['pliSent'] += 1

    def receiver_report(self):
        return struct.pack('!BBHI', 0x80, 201, 1, self.ssrc)

    def receive_rtcp(self, data, address):
        if not self.remote or address[0] != self.remote['address']:
            return
        pos = 0
        while pos + 4 <= len(data):
            first, kind, length = struct.unpack_from('!BBH', data, pos)
            if first >> 6 != 2:
                return
            # Payload-specific feedback: PLI (FMT 1) or FIR (FMT 4) -> send a keyframe next.
            if kind == 206 and (first & 0x1f) in (1, 4):
                self.force_keyframe = True
                self.counters['pliReceived'] += 1
            pos += (length + 1) * 4

    async def reports(self):
        while not self.closed:
            await asyncio.sleep(5)
            if not self.remote:
                continue
            now = time.time() + 2208988800
            ntp = int(now)
            sr = struct.pack('!BBHIIIIII', 0x80, 200, 6, self.ssrc, ntp, int((now - ntp) * (1 << 32)),
                             int((time.monotonic() - self.start) * CLOCK) & 0xffffffff,
                             self.counters['videoSent'], 0)
            self.rtcp.sendto(sr, (self.remote['address'], self.remote['rtcpPort']))

    def stats(self):
        return {**self.counters, 'active': self.active,
                'resolution': f"{self.remote['width']}x{self.remote['height']}" if self.remote else None}

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.track.stop()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        if self.rtcp and self.remote:
            try:
                self.rtcp.sendto(struct.pack('!BBHI', 0x81, 203, 1, self.ssrc), (self.remote['address'], self.remote['rtcpPort']))
            except OSError:
                pass
        for transport in (self.rtp, self.rtcp):
            if transport:
                transport.close()
        # Let any picture being encoded/decoded finish before the codecs can be freed:
        # releasing x264/FFmpeg state under a running worker thread crashes the process.
        for pool in (self.encode_pool, self.decode_pool):
            await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)
        self.encoder = self.decoder = None
