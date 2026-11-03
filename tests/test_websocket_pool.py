"""Actual pinned curl_cffi handles; no network, credentials or model calls."""
import asyncio, threading, unittest
from unittest.mock import Mock, patch
from curl_cffi import Curl, CurlError
from curl_cffi.const import CurlOpt
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.websockets import WebSocket
from utils.Client import Client

class WebsocketPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_curl_preparation_preserves_opaque_signed_ws_urls(self):
        values=['A%252BB%252FC%253D','A%2BB%2FC%3D','A%20B%2bC%3d','Ab_c-09','A+B/C=','A%25252B','A%2fB%252F','A%25B%2BC']
        for value in values:
            with self.subTest(value=value):
                session=AsyncSession(max_clients=1);client=Client.__new__(Client);client.session=session
                url='wss://ws.chatgpt.com/ws/synthetic?verify='+value;prepared=[];original=session._set_curl_options
                def capture(*args,**kwargs):
                    result=original(*args,**kwargs);prepared.append(result[0].url);return result
                session._set_curl_options=capture
                with patch.object(Curl,'perform',return_value=None):
                    socket=await client.connect_websocket(url,headers={'origin':'https://chatgpt.com'},quote=False,timeout=2)
                self.assertEqual(prepared,[url]);socket.send=Mock();await client.close_websocket(socket)
                self.assertEqual(session.pool.qsize(),1);await session.close()

    async def until(self, predicate):
        for _ in range(200):
            if predicate():return
            await asyncio.sleep(.005)
        self.fail("fixture worker did not finish")

    async def make(self):
        session = AsyncSession(max_clients=1)
        curl = await session.pop_curl()
        socket = WebSocket(session, curl)
        socket.send = Mock()
        return session, curl, socket

    async def test_native_close_reproduces_poisoned_pool_without_network(self):
        session, curl, socket = await self.make()
        await socket.aclose()
        recycled = await session.pop_curl()
        self.assertIs(recycled, curl)
        with self.assertRaises(TypeError):recycled.setopt(CurlOpt.URL, b"https://example.invalid/")
        await session.close()

    async def test_fixed_close_supplies_fresh_handle_for_next_endpoint(self):
        session, curl, socket = await self.make()
        client = Client.__new__(Client)
        await client.close_websocket(socket)
        fresh = await session.pop_curl()
        self.assertIsNot(fresh, curl)
        fresh.setopt(CurlOpt.URL, b"https://example.invalid/")
        self.assertFalse(socket.keep_running)
        self.assertTrue(socket.send.called)
        fresh.close()
        await session.close()

    async def test_send_failure_still_retires_handle_and_releases_pool_capacity(self):
        session, curl, socket = await self.make()
        socket.send.side_effect = ConnectionResetError("synthetic")
        client = Client.__new__(Client)
        with self.assertRaises(ConnectionResetError):await client.close_websocket(socket)
        with self.assertRaises(ConnectionResetError):await client.close_websocket(socket)
        self.assertEqual(socket.send.call_count, 1)
        self.assertEqual(session.pool.qsize(), 1)
        fresh = await asyncio.wait_for(session.pop_curl(), timeout=1)
        self.assertIsNot(fresh, curl)
        fresh.setopt(CurlOpt.URL, b"https://example.invalid/")
        self.assertFalse(socket.keep_running)
        fresh.close()
        await session.close()

    async def test_native_handshake_failure_reproduces_lost_pool_capacity(self):
        session = AsyncSession(max_clients=1)
        with patch.object(Curl, "perform", side_effect=CurlError("HTTP code 401 FIXTURE", 22)):
            with self.assertRaises(CurlError):await session.ws_connect("wss://example.invalid/")
        self.assertEqual(session.pool.qsize(), 0)
        with self.assertRaises(asyncio.TimeoutError):await asyncio.wait_for(session.pop_curl(), .02)
        await session.close()

    async def test_repeated_handshake_failures_restore_capacity(self):
        session = AsyncSession(max_clients=1)
        client = Client.__new__(Client);client.session = session
        with patch.object(Curl, "perform", side_effect=CurlError("HTTP code 401 FIXTURE", 22)):
            for _ in range(3):
                with self.assertRaises(CurlError):await client.connect_websocket("wss://example.invalid/")
                self.assertEqual(session.pool.qsize(), 1)
        fresh = await session.pop_curl();fresh.setopt(CurlOpt.URL,b"https://example.invalid/");fresh.close()
        await session.close()

    async def test_option_failure_restores_exactly_one_slot(self):
        session = AsyncSession(max_clients=1)
        client = Client.__new__(Client);client.session = session
        with self.assertRaises(TypeError):await client.connect_websocket("wss://example.invalid/", invalid_fixture_option=True)
        self.assertEqual(session.pool.qsize(), 1)
        await session.close()

    async def test_successful_handshake_owns_handle_until_single_retirement(self):
        session = AsyncSession(max_clients=1)
        client = Client.__new__(Client);client.session = session
        with patch.object(Curl, "perform", return_value=None):
            socket = await client.connect_websocket("wss://example.invalid/",headers={"origin":"https://example.invalid"},timeout=2)
        self.assertIsInstance(socket,WebSocket);self.assertEqual(session.pool.qsize(),0)
        socket.send=Mock()
        await client.close_websocket(socket)
        self.assertEqual(session.pool.qsize(),1)
        fresh=await session.pop_curl();self.assertIsNot(fresh,socket.curl)
        fresh.setopt(CurlOpt.URL,b"https://example.invalid/");fresh.close();await session.close()

    async def test_cancel_before_checkout_does_not_add_phantom_slot(self):
        session = AsyncSession(max_clients=1);held = await session.pop_curl()
        client = Client.__new__(Client);client.session = session
        task = asyncio.create_task(client.connect_websocket("wss://example.invalid/"))
        await asyncio.sleep(.01);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(session.pool.qsize(), 0)
        held.close();session.push_curl(None);await session.close()

    async def test_cancel_during_handshake_defers_close_until_worker_finishes(self):
        session = AsyncSession(max_clients=1)
        client = Client.__new__(Client);client.session = session
        entered, release = threading.Event(), threading.Event();closed=[];original=Curl.close
        def perform(curl):
            entered.set()
            if not release.wait(2):raise RuntimeError("fixture timeout")
        def close(curl):closed.append(curl);original(curl)
        with patch.object(Curl,"perform",perform),patch.object(Curl,"close",close):
            task=asyncio.create_task(client.connect_websocket("wss://example.invalid/"))
            try:
                await self.until(entered.is_set);task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
                self.assertEqual(closed,[]);self.assertEqual(session.pool.qsize(),0)
            finally:release.set()
            await self.until(lambda:session.pool.qsize()==1)
            self.assertEqual(len(closed),1)
            # Remove the closed handle reference before its finalizer runs.
            closed.clear()
        await session.close()

    async def test_repeated_close_and_cancellation_return_capacity_once_after_worker(self):
        session,curl,socket=await self.make();client=Client.__new__(Client)
        entered,release=threading.Event(),threading.Event()
        def send(*args):
            entered.set()
            if not release.wait(2):raise RuntimeError("fixture timeout")
        socket.send=Mock(side_effect=send)
        task=asyncio.create_task(client.close_websocket(socket))
        try:
            await self.until(entered.is_set);task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertEqual(session.pool.qsize(),0)
            second=asyncio.create_task(client.close_websocket(socket));await asyncio.sleep(.01)
            self.assertFalse(second.done());self.assertEqual(socket.send.call_count,1)
        finally:release.set()
        await second
        self.assertEqual(session.pool.qsize(),1)
        await client.close_websocket(socket)
        self.assertEqual(session.pool.qsize(),1);self.assertEqual(socket.send.call_count,1)
        await session.close()

if __name__ == "__main__":unittest.main()
