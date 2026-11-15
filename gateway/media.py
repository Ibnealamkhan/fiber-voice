"""AMR/AMR-WB RFC 4867 RTP bridge for browser WebRTC audio."""
import asyncio
import ctypes as C
import ctypes.util
import ipaddress
import os
import secrets
import socket
import struct
import time
from fractions import Fraction

import av
from aiortc import AudioStreamTrack

NB_BITS = [95, 103, 118, 134, 148, 159, 204, 244, 39, None, None, None, None, None, None, 0]
WB_BITS = [132, 177, 253, 285, 317, 365, 397, 461, 477, 40, None, None, None, None, 0, 0]
OFFER_CODECS = {120: ('AMR-WB', True), 121: ('AMR-WB', False), 118: ('AMR', True), 119: ('AMR', False)}


def library(name):
    # Bundled copies (Mac app) take precedence over system/Homebrew libraries.
    bundled = os.environ.get('GATEWAY_LIB_DIR')
    if bundled:
        try:
            return C.CDLL(os.path.join(bundled, 'lib' + name + '.dylib'))
        except OSError:
            pass
    path = ctypes.util.find_library(name)
    if not path:
        for prefix in ['/opt/homebrew/lib/', '/usr/local/lib/']:
            candidate = prefix + 'lib' + name + '.dylib'
            try:
                return C.CDLL(candidate)
            except OSError:
                pass
        raise RuntimeError('Missing codec library: ' + name + '. Install opencore-amr and vo-amrwbenc.')
    return C.CDLL(path)


class AmrCodec:
    def __init__(self, wideband=False, mode=None):
        self.wideband = wideband
        self.rate = 16000 if wideband else 8000
        self.samples = self.rate // 50
        self.bits = WB_BITS if wideband else NB_BITS
        self.mode = (8 if wideband else 7) if mode is None else mode
        self.encoder = None
        self.decoder = None
        enc_lib = library('vo-amrwbenc') if wideband else library('opencore-amrnb')
        dec_lib = library('opencore-amrwb') if wideband else enc_lib
        prefix_e = 'E_IF_' if wideband else 'Encoder_Interface_'
        prefix_d = 'D_IF_' if wideband else 'Decoder_Interface_'
        ei = getattr(enc_lib, prefix_e + 'init'); ei.restype = C.c_void_p
        ei.argtypes = [] if wideband else [C.c_int]
        di = getattr(dec_lib, prefix_d + 'init'); di.restype = C.c_void_p; di.argtypes = []
        self._encode = getattr(enc_lib, prefix_e + ('encode' if wideband else 'Encode'))
        self._encode.restype = C.c_int
        self._encode.argtypes = [C.c_void_p, C.c_int, C.POINTER(C.c_short), C.POINTER(C.c_ubyte), C.c_int]
        self._decode = getattr(dec_lib, prefix_d + ('decode' if wideband else 'Decode'))
        self._decode.restype = None
        self._decode.argtypes = [C.c_void_p, C.POINTER(C.c_ubyte), C.POINTER(C.c_short), C.c_int]
        self._exit_e = getattr(enc_lib, prefix_e + 'exit'); self._exit_e.argtypes = [C.c_void_p]; self._exit_e.restype = None
        self._exit_d = getattr(dec_lib, prefix_d + 'exit'); self._exit_d.argtypes = [C.c_void_p]; self._exit_d.restype = None
        self.encoder = ei() if wideband else ei(0)
        self.decoder = di()
        if not self.encoder or not self.decoder:
            self.close()
            raise RuntimeError('AMR codec initialization failed')
        self.last_frame = bytes([15 << 3 | 4])

    def encode(self, pcm):
        if len(pcm) != self.samples * 2 or not 0 <= self.mode <= (8 if self.wideband else 7):
            raise ValueError(f'Invalid AMR input frame: {len(pcm)} PCM bytes, expected {self.samples*2}, mode {self.mode}')
        speech = (C.c_short * self.samples).from_buffer_copy(pcm)
        out = (C.c_ubyte * 128)()
        length = self._encode(self.encoder, self.mode, speech, out, 0)
        if not 1 <= length <= 128:
            raise RuntimeError('AMR encoder returned an invalid frame')
        return bytes(out[:length])

    def decode(self, frame=None):
        lost = frame is None
        frame = self.last_frame if lost else frame
        ft = frame[0] >> 3 & 15
        bits = self.bits[ft]
        if bits is None or len(frame) != 1 + (bits + 7) // 8:
            raise ValueError('Invalid AMR speech frame')
        # Pad the native input allocation for decoders which inspect whole words.
        source = (C.c_ubyte * 128)()
        source[:len(frame)] = frame
        out = (C.c_short * self.samples)()
        self._decode(self.decoder, source, out, int(lost or not bool(frame[0] & 4)))
        if not lost:
            self.last_frame = frame
        return bytes(out)

    def close(self):
        if self.encoder:
            self._exit_e(self.encoder); self.encoder = None
        if self.decoder:
            self._exit_d(self.decoder); self.decoder = None


def codec_capabilities():
    try:
        for wideband in (False, True):
            codec = AmrCodec(wideband)
            codec.close()
        return {'ready': True, 'codecs': ['AMR-WB', 'AMR'], 'packetization': ['octet-aligned', 'bandwidth-efficient']}
    except Exception as error:
        return {'ready': False, 'error': str(error)}


def frame_bits(frame, table):
    if not frame:
        raise ValueError('Empty AMR frame')
    ft = frame[0] >> 3 & 15
    size = table[ft]
    if size is None or len(frame) != 1 + (size + 7) // 8:
        raise ValueError('Invalid AMR frame size')
    return ft, frame[0] >> 2 & 1, size


def pack_amr(frames, wideband=False, octet_align=True, cmr=15):
    if not 1 <= len(frames) <= 10 or not 0 <= cmr <= 15:
        raise ValueError('Invalid AMR payload')
    table = WB_BITS if wideband else NB_BITS
    meta = [frame_bits(f, table) for f in frames]
    if octet_align:
        return bytes([cmr << 4]) + bytes([(int(i < len(frames)-1) << 7) | (ft << 3) | (q << 2)
                                         for i, (ft, q, _) in enumerate(meta)]) + b''.join(f[1:] for f in frames)
    bits = f'{cmr:04b}'
    bits += ''.join(f'{int(i < len(frames)-1):01b}{ft:04b}{q:01b}' for i, (ft, q, _) in enumerate(meta))
    bits += ''.join(''.join(f'{b:08b}' for b in f[1:])[:size] for f, (_, _, size) in zip(frames, meta))
    bits += '0' * (-len(bits) % 8)
    return int(bits, 2).to_bytes(len(bits)//8, 'big')


def unpack_amr(payload, wideband=False, octet_align=True):
    table = WB_BITS if wideband else NB_BITS
    if not payload or len(payload) > 1500:
        raise ValueError('Invalid AMR RTP payload length')
    bits = ''.join(f'{b:08b}' for b in payload)
    cmr = int(bits[:4], 2)
    cursor = 8 if octet_align else 4
    toc = []
    while True:
        width = 8 if octet_align else 6
        if cursor + width > len(bits) or len(toc) >= 10:
            raise ValueError('Invalid AMR table of contents')
        entry = bits[cursor:cursor+width]; cursor += width
        more, ft, quality = int(entry[0]), int(entry[1:5], 2), int(entry[5])
        if table[ft] is None:
            raise ValueError('Reserved AMR frame type')
        toc.append((ft, quality))
        if not more:
            break
    frames = []
    for ft, quality in toc:
        size = table[ft]
        width = ((size + 7)//8)*8 if octet_align else size
        if cursor + width > len(bits):
            raise ValueError('Truncated AMR frame')
        speech = bits[cursor:cursor+size]; cursor += width
        speech += '0' * (-len(speech) % 8)
        data = int(speech, 2).to_bytes(len(speech)//8, 'big') if speech else b''
        frames.append(bytes([(ft << 3) | (quality << 2)]) + data)
    if len(bits) - cursor > 7:
        raise ValueError('Unexpected AMR payload data')
    return cmr, frames


def parse_rtp(packet):
    if len(packet) < 12 or packet[0] >> 6 != 2:
        raise ValueError('Invalid RTP packet')
    pos = 12 + (packet[0] & 15) * 4
    if pos > len(packet):
        raise ValueError('Truncated RTP CSRC list')
    if packet[0] & 0x10:
        if pos + 4 > len(packet): raise ValueError('Truncated RTP extension')
        pos += 4 + struct.unpack_from('!H', packet, pos+2)[0] * 4
    end = len(packet)
    if packet[0] & 0x20:
        pad = packet[-1]
        if not pad or pad > end-pos: raise ValueError('Invalid RTP padding')
        end -= pad
    if pos > end: raise ValueError('Truncated RTP header')
    seq, timestamp, ssrc = struct.unpack_from('!HII', packet, 2)
    return packet[1] & 127, seq, timestamp, ssrc, packet[pos:end]


def parse_sdp(sdp, require_offered=True):
    if len(sdp) > 65536:
        raise ValueError('SDP too large')
    global_connection = None
    audio = None
    current = None
    for line in sdp.replace('\r', '').split('\n'):
        if line.startswith('m='):
            fields = line[2:].split()
            current = {'kind': fields[0], 'port': int(fields[1].split('/')[0]), 'protocol': fields[2],
                       'pts': [int(x) for x in fields[3:]], 'maps': {}, 'fmtp': {}, 'direction': 'sendrecv'}
            if current['kind'] == 'audio' and audio is None:
                audio = current
        elif line.startswith('c='):
            fields = line[2:].split()
            address = fields[2].split('/')[0]
            if current is None: global_connection = address
            elif current is audio: audio['address'] = address
        elif current is audio and audio is not None:
            if line.startswith('a=rtpmap:'):
                pt, value = line[9:].split(None, 1)
                parts = value.split('/')
                audio['maps'][int(pt)] = (parts[0].upper(), int(parts[1]), int(parts[2]) if len(parts)>2 else 1)
            elif line.startswith('a=fmtp:'):
                pt, value = line[7:].split(None, 1)
                audio['fmtp'][int(pt)] = {k.strip().lower(): v.strip() for item in value.split(';') if '=' in item for k, v in [item.split('=', 1)]}
            elif line in ['a=sendrecv', 'a=sendonly', 'a=recvonly', 'a=inactive']:
                audio['direction'] = line[2:]
            elif line.startswith('a=rtcp:'):
                audio['rtcpPort'] = int(line[7:].split()[0])
    if audio is None or audio['protocol'] != 'RTP/AVP' or not 0 < audio['port'] < 65535:
        raise ValueError('No supported RTP audio in SDP')
    address = audio.get('address', global_connection)
    ip = ipaddress.ip_address(address)
    if ip.version != 6 or ip.is_multicast or ip.is_loopback or ip.is_unspecified:
        raise ValueError('A unicast IPv6 media address is required')
    audio['address'] = str(ip)
    for pt in audio['pts']:
        name, rate, channels = audio['maps'].get(pt, ('', 0, 0))
        if name not in ('AMR', 'AMR-WB') or channels != 1 or rate != (16000 if name == 'AMR-WB' else 8000):
            continue
        params = audio['fmtp'].get(pt, {})
        if any(params.get(k, '0') != '0' for k in ['crc', 'robust-sorting', 'interleaving']):
            continue
        if params.get('octet-align', '0') not in ('0','1'):
            continue
        aligned = params.get('octet-align', '0') == '1'
        if require_offered and OFFER_CODECS.get(pt) != (name, aligned):
            continue
        modes = [int(x) for x in params.get('mode-set', '0,1,2,3,4,5,6,7' + (',8' if name == 'AMR-WB' else '')).split(',')]
        if not modes or any(m < 0 or m > (8 if name == 'AMR-WB' else 7) for m in modes):
            continue
        audio.update({'pt': pt, 'codec': name, 'rate': rate, 'octetAlign': aligned, 'mode': max(modes), 'modes': modes,
                      'modePeriod': int(params.get('mode-change-period', '1')), 'modeNeighbor': params.get('mode-change-neighbor', '0') == '1'})
        event = next((p for p in audio['pts'] if audio['maps'].get(p) == ('TELEPHONE-EVENT', rate, 1)), None)
        audio['eventPt'] = event
        return audio
    raise ValueError('No compatible AMR/AMR-WB audio format in SDP')


class ReturnAudio(AudioStreamTrack):
    def __init__(self, media):
        super().__init__()
        self.media = media
        self.pts = 0
        self.started = None

    async def recv(self):
        if self.readyState != 'live':
            from aiortc.mediastreams import MediaStreamError
            raise MediaStreamError
        loop = asyncio.get_running_loop()
        if self.started is None: self.started = loop.time()
        await asyncio.sleep(max(0, self.started + self.pts/48000 - loop.time()))
        pcm = self.media.receive_pcm()
        # Speech sample rates divide 48 kHz exactly. PyAV handles interpolation.
        source = av.AudioFrame(format='s16', layout='mono', samples=len(pcm)//2)
        source.planes[0].update(pcm); source.sample_rate = self.media.rate
        source.pts = None
        if self.media.return_resampler is None:
            self.media.return_resampler = av.AudioResampler(format='s16', layout='mono', rate=48000)
        for frame in self.media.return_resampler.resample(source):
            self.media.return_fifo.write(frame)
        out = self.media.return_fifo.read(960)
        if out is None:
            out = av.AudioFrame(format='s16', layout='mono', samples=960)
            out.planes[0].update(bytes(1920))
        out.sample_rate = 48000; out.time_base = Fraction(1,48000); out.pts = self.pts
        self.pts += 960
        return out


class RTPProtocol(asyncio.DatagramProtocol):
    def __init__(self, media, rtcp=False): self.media, self.rtcp = media, rtcp
    def datagram_received(self, data, address):
        if self.rtcp:
            self.media.counters['rtcpReceived'] += 1
        else:
            self.media.receive_packet(data, address)


class MediaSession:
    def __init__(self, local_address):
        self.local_address = local_address
        self.rtp = self.rtcp = None
        self.port = None
        self.remote = None
        self.codec = None
        self.rate = 16000
        self.ssrc = secrets.randbits(32)
        self.sequence = secrets.randbits(16)
        self.timestamp = secrets.randbits(32)
        self.frames = {}
        self.play_timestamp = None
        self.first_arrival = None
        self.newest_timestamp = None
        self.input_queue = asyncio.Queue(maxsize=15)
        self.tasks = set()
        self.track = ReturnAudio(self)
        self.return_resampler = None
        self.return_fifo = av.AudioFifo()
        self.closed = False
        self.muted = False
        self.counters = {'rtpSent':0,'rtpReceived':0,'invalidPackets':0,'lateFrames':0,'concealedFrames':0,'discardedInputFrames':0,'rtcpReceived':0,'bytesSent':0,'bytesReceived':0}
        self.session_id = secrets.randbits(48)
        self.version = 0
        self.target_mode = None
        self.dtmf_lock = asyncio.Lock()
        self.payload_octets_sent = 0
        self.error = None
        self.on_error = None

    async def open(self):
        loop = asyncio.get_running_loop()
        # Reserve an RTP/RTCP pair inside the native client's configured range.
        for port in range(52000, 52200, 2):
            sockets = []
            try:
                for p in (port, port+1):
                    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
                    sock.setblocking(False); sockets.append(sock)
                    sock.bind((self.local_address, p))
                self.rtp, _ = await loop.create_datagram_endpoint(lambda:RTPProtocol(self), sock=sockets[0])
                self.rtcp, _ = await loop.create_datagram_endpoint(lambda:RTPProtocol(self, True), sock=sockets[1])
                self.port = port
                return self
            except OSError:
                for sock in sockets: sock.close()
        raise RuntimeError('No free local RTP/RTCP ports in 52000–52200')

    def sdp(self, answer=False):
        if answer and not self.remote: raise ValueError('No remote audio offer')
        self.version += 1
        codec_pts = list(OFFER_CODECS) if not answer else [self.remote['pt']]
        events = [126,127] if not answer else ([self.remote['eventPt']] if self.remote.get('eventPt') is not None else [])
        lines = ['v=0',f'o=- {self.session_id} {self.version} IN IP6 {self.local_address}',
                 's=Fiber voice',f'c=IN IP6 {self.local_address}','t=0 0',
                 f'm=audio {self.port} RTP/AVP ' + ' '.join(map(str, codec_pts+events)),
                 f'a=rtcp:{self.port+1} IN IP6 {self.local_address}', 'a=ptime:20', 'a=maxptime:200']
        direction = self.remote['direction'] if answer else 'sendrecv'
        lines.append('a=' + {'sendonly':'recvonly','recvonly':'sendonly'}.get(direction,direction))
        for pt in codec_pts:
            name, aligned = OFFER_CODECS[pt] if not answer else (self.remote['codec'],self.remote['octetAlign'])
            rate = 16000 if name=='AMR-WB' else 8000
            lines += [f'a=rtpmap:{pt} {name}/{rate}',f'a=fmtp:{pt} octet-align={int(aligned)}']
        for pt in events:
            rate = (16000 if pt==127 else 8000) if not answer else self.remote['rate']
            lines += [f'a=rtpmap:{pt} telephone-event/{rate}',f'a=fmtp:{pt} 0-15']
        return '\r\n'.join(lines)+'\r\n'

    def negotiate(self, sdp, incoming=False):
        remote = parse_sdp(sdp, require_offered=not incoming)
        if self.codec and remote['codec'] != self.remote['codec']:
            raise ValueError('Changing AMR family within an established call is unsupported')
        if not self.codec:
            self.codec = AmrCodec(remote['codec']=='AMR-WB',remote['mode'])
            previous_rate = self.rate
            self.rate = self.codec.rate
            if previous_rate != self.rate:
                while not self.input_queue.empty(): self.input_queue.get_nowait()
                self.return_resampler = None
                self.return_fifo = av.AudioFifo()
        self.remote = remote
        self.target_mode = remote['mode']
        if not any(getattr(task, '_fiber_sender', False) for task in self.tasks):
            task = self.spawn(self.send_audio()); task._fiber_sender = True
            self.spawn(self.send_reports())

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine); self.tasks.add(task)
        def done(finished):
            self.tasks.discard(finished)
            if finished.cancelled(): return
            error = finished.exception()
            if error and not self.closed:
                self.error = str(error)
                if self.on_error: self.on_error(self.error)
        task.add_done_callback(done)
        return task

    async def attach(self, track):
        rate = self.rate
        resampler = av.AudioResampler(format='s16', layout='mono', rate=rate)
        fifo = av.AudioFifo()
        try:
            while not self.closed:
                source = await track.recv()
                if rate != self.rate:
                    rate = self.rate
                    resampler = av.AudioResampler(format='s16', layout='mono', rate=rate)
                    fifo = av.AudioFifo()
                    while not self.input_queue.empty(): self.input_queue.get_nowait()
                # Input timestamps restart for each WebRTC attachment; FIFO needs only PCM.
                source.pts = None
                for frame in resampler.resample(source):
                    frame.pts = None; fifo.write(frame)
                while fifo.samples >= self.rate//50:
                    frame = fifo.read(self.rate//50)
                    pcm = bytes(frame.planes[0])[:(self.rate//50)*2]
                    if self.input_queue.full(): self.input_queue.get_nowait()
                    self.input_queue.put_nowait(pcm)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Ending a browser track is handled by the associated peer/session cleanup.
            return

    def send_packet(self, payload, pt, timestamp, marker=False):
        if self.closed or not self.remote: return
        packet = struct.pack('!BBHII',0x80,pt|(0x80 if marker else 0),self.sequence,timestamp & 0xffffffff,self.ssrc)+payload
        self.sequence=(self.sequence+1)&0xffff
        self.rtp.sendto(packet,(self.remote['address'],self.remote['port']))
        self.counters['rtpSent']+=1; self.counters['bytesSent']+=len(packet)
        self.payload_octets_sent+=len(payload)

    async def send_audio(self):
        start=asyncio.get_running_loop().time(); count=0
        while not self.closed:
            await asyncio.sleep(max(0,start+count*.02-asyncio.get_running_loop().time()))
            if self.remote['direction'] not in ('sendonly','inactive'):
                pcm=self.input_queue.get_nowait() if not self.input_queue.empty() else bytes(self.codec.samples*2)
                if len(pcm) != self.codec.samples*2:
                    # A producer can straddle the first SDP format selection.
                    self.counters['discardedInputFrames']+=1
                    pcm=bytes(self.codec.samples*2)
                if self.muted: pcm=bytes(self.codec.samples*2)
                period=max(1,self.remote['modePeriod'])
                if count % period == 0 and self.target_mode != self.codec.mode:
                    choices=self.remote['modes']
                    if self.remote['modeNeighbor']:
                        candidates=[m for m in choices if (self.codec.mode<m<=self.target_mode) or (self.target_mode<=m<self.codec.mode)]
                        if candidates: self.codec.mode=min(candidates,key=lambda m:abs(m-self.codec.mode))
                    else:self.codec.mode=self.target_mode
                frame=self.codec.encode(pcm)
                self.send_packet(pack_amr([frame],self.codec.wideband,self.remote['octetAlign']),self.remote['pt'],self.timestamp,marker=count==0)
            self.timestamp=(self.timestamp+self.codec.samples)&0xffffffff; count+=1
            if count*.02 < asyncio.get_running_loop().time()-start-.1:
                start=asyncio.get_running_loop().time()-count*.02

    def receive_packet(self, packet, address):
        if not self.remote or self.closed: return
        if address[0] != self.remote['address'] or address[1] != self.remote['port']: return
        try:
            pt,seq,timestamp,ssrc,payload=parse_rtp(packet)
            if pt != self.remote['pt']: return
            cmr,frames=unpack_amr(payload,self.codec.wideband,self.remote['octetAlign'])
            if cmr in self.remote['modes']: self.target_mode=cmr
            if self.newest_timestamp is None: unwrapped=timestamp
            else:unwrapped=self.newest_timestamp+((timestamp-(self.newest_timestamp&0xffffffff)+(1<<31))%(1<<32)-(1<<31))
            self.newest_timestamp=max(self.newest_timestamp or unwrapped,unwrapped)
            if self.first_arrival is None:self.first_arrival=time.monotonic()
            for i,frame in enumerate(frames):
                stamp=unwrapped+i*self.codec.samples
                if self.play_timestamp is not None and stamp<self.play_timestamp:self.counters['lateFrames']+=1
                else:self.frames[stamp]=frame
            if len(self.frames)>100:
                for stamp in sorted(self.frames)[:-50]:self.frames.pop(stamp,None)
                self.play_timestamp=min(self.frames)
            self.counters['rtpReceived']+=1;self.counters['bytesReceived']+=len(packet)
        except (ValueError,IndexError,struct.error): self.counters['invalidPackets']+=1

    def receive_pcm(self):
        if not self.codec:return bytes(self.rate//50*2)
        if self.play_timestamp is None:
            if not self.frames or time.monotonic()-self.first_arrival<.06:return bytes(self.codec.samples*2)
            self.play_timestamp=min(self.frames)
        frame=self.frames.pop(self.play_timestamp,None)
        self.play_timestamp+=self.codec.samples
        # Missing speech frames can be normal AMR DTX, not necessarily packet loss.
        if frame is None:self.counters['concealedFrames']+=1
        return self.codec.decode(frame)

    async def send_reports(self):
        while not self.closed:
            await asyncio.sleep(5)
            now=time.time()+2208988800
            ntp=int(now);fraction=int((now-ntp)*(1<<32))
            sr=struct.pack('!BBHIIIIII',0x80,200,6,self.ssrc,ntp,fraction,self.timestamp,self.counters['rtpSent'],self.payload_octets_sent)
            name=b'fiber-gateway';chunk=struct.pack('!I',self.ssrc)+bytes([1,len(name)])+name+b'\0'
            chunk+=bytes(-len(chunk)%4)
            sdes=struct.pack('!BBH',0x81,202,len(chunk)//4)+chunk
            self.rtcp.sendto(sr+sdes,(self.remote['address'],self.remote.get('rtcpPort',self.remote['port']+1)))

    async def dtmf(self,digit):
        async with self.dtmf_lock:
            if not self.closed:await self._dtmf(digit)

    async def _dtmf(self,digit):
        if not self.remote or self.remote.get('eventPt') is None:raise ValueError('Telephone events were not negotiated')
        event='0123456789*#ABCD'.index(digit)
        stamp=self.timestamp; samples=self.rate//50
        for i in range(1,9):
            self.send_packet(struct.pack('!BBH',event,10,i*samples),self.remote['eventPt'],stamp,marker=i==1)
            await asyncio.sleep(.02)
        for _ in range(3):
            self.send_packet(struct.pack('!BBH',event,0x80|10,8*samples),self.remote['eventPt'],stamp)
            await asyncio.sleep(.02)

    def stats(self):
        return {**self.counters,'codec':self.remote['codec'] if self.remote else None,
                'error':self.error,
                'packetization':('octet-aligned' if self.remote['octetAlign'] else 'bandwidth-efficient') if self.remote else None}

    async def close(self):
        if self.closed:return
        self.closed=True;self.track.stop()
        for task in list(self.tasks):task.cancel()
        await asyncio.gather(*list(self.tasks),return_exceptions=True)
        if self.rtcp and self.remote:
            self.rtcp.sendto(struct.pack('!BBHI',0x81,203,1,self.ssrc),(self.remote['address'],self.remote.get('rtcpPort',self.remote['port']+1)))
        if self.rtp:self.rtp.close()
        if self.rtcp:self.rtcp.close()
        if self.codec:self.codec.close()
