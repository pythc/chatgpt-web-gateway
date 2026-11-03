import random
import asyncio
import struct

from curl_cffi.requests import AsyncSession


class Client:
    def __init__(self, proxy=None, timeout=15, verify=True, impersonate='safari15_3'):
        self.proxies = {"http": proxy, "https": proxy}
        self.timeout = timeout
        self.verify = verify

        self.impersonate = impersonate
        # impersonate=self.impersonate

        # self.ja3 = ""
        # self.akamai = ""
        # ja3=self.ja3, akamai=self.akamai
        self.session = AsyncSession(proxies=self.proxies, timeout=self.timeout, impersonate=self.impersonate, verify=self.verify)
        self.session2 = AsyncSession(proxies=self.proxies, timeout=self.timeout, impersonate=self.impersonate, verify=self.verify)

    async def post(self, *args, **kwargs):
        r = await self.session.post(*args, **kwargs)
        return r

    async def post_stream(self, *args, headers=None, cookies=None, **kwargs):
        if self.session:
            headers = headers or self.session.headers
            cookies = cookies or self.session.cookies
        r = await self.session2.post(*args, headers=headers, cookies=cookies, **kwargs)
        return r

    async def get(self, *args, **kwargs):
        r = await self.session.get(*args, **kwargs)
        return r

    async def request(self, *args, **kwargs):
        r = await self.session.request(*args, **kwargs)
        return r

    async def put(self, *args, **kwargs):
        r = await self.session.put(*args, **kwargs)
        return r

    async def recv_websocket(self, socket):
        # curl_cffi 0.7's arecv spins in an executor on AGAIN. Poll cooperatively
        # so cancellation/deadlines stop reception without leaving a busy thread.
        from curl_cffi import CurlError
        from curl_cffi.const import CurlECode, CurlWsFlag
        chunks, size = [], 0
        while True:
            try:
                chunk, frame = socket.curl.ws_recv()
            except CurlError as error:
                if error.code != CurlECode.AGAIN:raise
                await asyncio.sleep(0.05)
                continue
            chunks.append(chunk);size += len(chunk)
            if size > 16 * 1024 * 1024:
                raise ValueError("websocket frame limit")
            if frame.bytesleft == 0 and not frame.flags & CurlWsFlag.CONT:
                return b"".join(chunks), frame.flags

    async def connect_websocket(self, url, **kwargs):
        # Pinned curl_cffi 0.7 ws_connect leaks its acquired pool slot on
        # handshake failure. Own the checkout until a socket or retirement.
        from curl_cffi.const import CurlOpt
        from curl_cffi.requests.websockets import WebSocket
        session = self.session
        session._check_session_closed()
        curl = await session.pop_curl()
        performing = None
        def retire(completed=None):
            if completed is not None:
                # Retrieve the executor error without logging request data.
                completed.exception()
            try:curl.close()
            finally:session.push_curl(None)
        try:
            session._set_curl_options(curl, "GET", url, **kwargs)
            curl.setopt(CurlOpt.CONNECT_ONLY, 2)
            performing = asyncio.get_running_loop().run_in_executor(None, curl.perform)
            # Cancellation cannot stop curl.perform in its worker. Delay close
            # and capacity return until that exact worker has finished.
            await asyncio.shield(performing)
            return WebSocket(session, curl)
        except BaseException:
            if performing is not None and not performing.done():
                performing.add_done_callback(retire)
            else:
                retire()
            raise

    async def close_websocket(self, socket):
        # curl_cffi 0.7 WebSocket.aclose returns an already-closed Curl to
        # the HTTP pool. Retire it and restore an empty pool slot instead.
        from curl_cffi.const import CurlWsFlag
        def retire():
            try:
                socket.send(struct.pack("!H", 1000), CurlWsFlag.CLOSE)
            finally:
                socket.keep_running = False
                socket.curl.close()
        closing = getattr(socket, "_chat2api_close_future", None)
        if closing is None:
            closing = asyncio.get_running_loop().run_in_executor(None, retire)
            socket._chat2api_close_future = closing
            def released(completed):
                completed.exception()
                socket.session.push_curl(None)
            closing.add_done_callback(released)
        await asyncio.shield(closing)

    async def close(self):
        if hasattr(self, 'session'):
            try:
                await self.session.close()
                del self.session
            except Exception:
                pass
        if hasattr(self, 'session2'):
            try:
                await self.session2.close()
                del self.session2
            except Exception:
                pass
