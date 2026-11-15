"""Run with the bundled Windows interpreter on a Windows build host."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
from media import AmrCodec, codec_capabilities
import server
import desktop

capabilities = codec_capabilities()
assert capabilities['ready'], capabilities
for wideband in (False, True):
    codec = AmrCodec(wideband)
    try:
        frame = codec.encode(bytes(codec.samples * 2))
        assert len(codec.decode(frame)) == codec.samples * 2
    finally:
        codec.close()
print('Bundled Windows Python, WebRTC, and AMR-NB/WB codecs loaded successfully')
