"""Desktop-owned gateway: dynamic loopback port, readiness file, stdin lifetime."""
import asyncio
import json
import os
from pathlib import Path
import sys

from aiohttp import web
import server


async def serve():
    runner = web.AppRunner(server.create_app(), access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        # Update the Host/Origin validator before announcing readiness.
        server.PORT = runner.addresses[0][1]
        ready = Path(os.environ['GATEWAY_READY_FILE'])
        temp = ready.with_suffix('.tmp')
        temp.write_text(json.dumps({'port': server.PORT}), encoding='utf-8')
        os.replace(temp, ready)
        # A close request, or the parent crashing, closes this private pipe.
        await asyncio.to_thread(sys.stdin.buffer.read, 1)
    finally:
        await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(serve())
