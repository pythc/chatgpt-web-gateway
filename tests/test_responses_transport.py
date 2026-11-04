"""Real loopback HTTP/WebSocket transport; synthetic upstream, no model usage."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

ROOT = Path(__file__).resolve().parents[1]
REQUESTS = []


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):pass
    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        REQUESTS.append(data)
        self.send_response(200);self.send_header('Content-Type', 'text/event-stream');self.end_headers()
        item = {'id': 'msg_' + uuid.uuid4().hex, 'type': 'message', 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': 'OK', 'annotations': []}]}
        response = {'id': 'resp_' + uuid.uuid4().hex, 'object': 'response', 'created_at': int(time.time()),
                    'model': data['model'], 'status': 'completed', 'output': [item],
                    'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
        events = [{'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                  {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                  {'type': 'response.completed', 'response': response}]
        try:
            for event in events:
                self.wfile.write(('data: ' + json.dumps(event) + '\n\n').encode());self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):pass


class TransportTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='gateway-transport-')
        cls.work = Path(cls.temp.name);(cls.work/'version.txt').write_text('transport-test')
        cls.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        s = socket.socket();s.bind(('127.0.0.1', 0));cls.port = s.getsockname()[1];s.close()
        env = {**os.environ, 'PYTHONPATH': str(ROOT), 'PYTHON_DOTENV_DISABLED': '1',
               'API_PREFIX': 'transport-test', 'AUTHORIZATION': 'synthetic',
               'RESPONSES_BACKEND': 'native', 'RESPONSES_UPSTREAM_BASE_URL': f'http://127.0.0.1:{cls.upstream.server_port}/v1',
               'RESPONSES_UPSTREAM_KEY': 'synthetic-upstream', 'RESPONSES_UPSTREAM_PROXY': '',
               'RESPONSES_WEBSOCKET_ENABLED': 'true', 'RESPONSES_ALLOWED_MODELS': 'synthetic',
               'SCHEDULED_REFRESH': 'false', 'ENABLE_GATEWAY': 'false'}
        # Never inherit an operator's credentials/state into a synthetic server.
        for name in ('RESPONSES_CODEX_AUTH_FILE', 'RESPONSES_STATE_DIR'):
            env.pop(name, None)
        cls.process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app:app', '--host', '127.0.0.1',
            '--port', str(cls.port), '--lifespan', 'off'], cwd=cls.work, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.http = f'http://127.0.0.1:{cls.port}/transport-test/v1'
        cls.ws = f'ws://127.0.0.1:{cls.port}/transport-test/v1/responses'
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        end = time.monotonic() + 10
        while time.monotonic() < end:
            try:
                with cls.opener.open(cls.http.removesuffix('/v1') + '/openapi.json', timeout=.2):break
            except Exception:time.sleep(.05)
        else:raise RuntimeError('Synthetic gateway startup failed')

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate();cls.process.wait(timeout=5)
        cls.upstream.shutdown();cls.upstream.server_close();cls.temp.cleanup()

    async def terminal(self, socket):
        events = []
        while True:
            value = json.loads(await asyncio.wait_for(socket.recv(), 3));events.append(value)
            if value.get('type') in ('response.completed', 'response.failed', 'error'):
                return events

    async def test_websocket_real_transport_and_incremental_continuation(self):
        async with connect(self.ws, additional_headers={'Authorization': 'Bearer synthetic'}, proxy=None) as ws:
            await ws.send(json.dumps({'type': 'response.create', 'model': 'synthetic', 'input': 'first'}))
            first = await self.terminal(ws);response_id = first[-1]['response']['id']
            self.assertEqual(first[-1]['type'], 'response.completed')
            await ws.send(json.dumps({'type': 'response.create', 'model': 'synthetic', 'input': 'second',
                                     'previous_response_id': response_id}))
            second = await self.terminal(ws);self.assertEqual(second[-1]['type'], 'response.completed')
            sent = REQUESTS[-1];self.assertNotIn('previous_response_id', sent)
            self.assertEqual(len(sent['input']), 3)
            self.assertEqual(sent['input'][0]['content'], 'first')
            self.assertEqual(sent['input'][-1]['content'], 'second')
            self.assertFalse(sent['store'])

    async def test_websocket_wrong_key_and_unsupported_lane(self):
        with self.assertRaises(InvalidStatus):
            async with connect(self.ws, additional_headers={'Authorization': 'Bearer wrong'}, proxy=None):pass
        async with connect(self.ws, additional_headers={'Authorization': 'Bearer synthetic'}, proxy=None) as ws:
            await ws.send(json.dumps({'type': 'response.create', 'stream_id': 'lane2', 'model': 'synthetic', 'input': 'x'}))
            self.assertEqual((await self.terminal(ws))[-1]['error']['code'], 'unsupported_stream_id')
            await ws.send(json.dumps({'type': 'response.create', 'model': 'synthetic', 'input': 'x', 'previous_response_id': 'unknown'}))
            self.assertEqual((await self.terminal(ws))[-1]['error']['code'], 'response_not_found')

    async def test_websocket_warmup_never_generates_and_preserves_request_state(self):
        before = len(REQUESTS)
        async with connect(self.ws, additional_headers={'Authorization': 'Bearer synthetic'}, proxy=None) as ws:
            await ws.send(json.dumps({'type': 'response.create', 'model': 'synthetic', 'generate': False,
                'instructions': 'fixture constraint', 'input': 'prepared context'}))
            warm = (await self.terminal(ws))[-1]['response']
            self.assertTrue(warm['gateway_warmup']);self.assertEqual(len(REQUESTS), before)
            await ws.send(json.dumps({'type': 'response.create', 'model': 'synthetic', 'generate': True,
                'previous_response_id': warm['id'], 'input': 'actual turn'}))
            self.assertEqual((await self.terminal(ws))[-1]['type'], 'response.completed')
            self.assertEqual(len(REQUESTS), before + 1)
            self.assertEqual(REQUESTS[-1]['instructions'], 'fixture constraint')
            self.assertEqual(REQUESTS[-1]['input'][0]['content'], 'prepared context')
            self.assertNotIn('generate', REQUESTS[-1])

    async def test_http_retrieval_delete_and_idempotent_completed_replay(self):
        payload = {'model': 'synthetic', 'input': 'local stored test', 'store': True}
        key = uuid.uuid4().hex
        def post():
            req = urllib.request.Request(self.http + '/responses', data=json.dumps(payload).encode(), headers={
                'Authorization': 'Bearer synthetic', 'Content-Type': 'application/json', 'Idempotency-Key': key})
            with self.opener.open(req, timeout=3) as response:return json.load(response)
        one = await asyncio.to_thread(post);count = len(REQUESTS);two = await asyncio.to_thread(post)
        self.assertEqual(one['id'], two['id']);self.assertEqual(len(REQUESTS), count)
        req = urllib.request.Request(self.http + '/responses/' + one['id'], headers={'Authorization': 'Bearer synthetic'})
        with self.opener.open(req, timeout=3) as response:self.assertEqual(json.load(response)['id'], one['id'])
        req.method = 'DELETE'
        with self.opener.open(req, timeout=3) as response:self.assertTrue(json.load(response)['deleted'])
        req.method = 'GET'
        with self.assertRaises(urllib.error.HTTPError) as error:self.opener.open(req, timeout=3)
        self.assertEqual(error.exception.code, 404)


if __name__ == '__main__':unittest.main(verbosity=2)
