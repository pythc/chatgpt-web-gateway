import asyncio,contextlib,io,json,logging,time,unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
logging.disable(logging.CRITICAL)
with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
    from app import app
    from chatgpt.ChatService import ChatService
    from chatgpt.webStream import ConversationState,LifecycleBudget,UpstreamError
from curl_cffi import CurlError
from curl_cffi.const import CurlWsFlag

def fixture(seconds=180,reads=120):
    s=ChatService.__new__(ChatService);s.base_url='https://chatgpt.com/backend-api';s.host_url='https://chatgpt.com';s.base_headers={};s.lifecycle_budget=LifecycleBudget(seconds,max_reads=reads)
    s.req_model='gpt-6-thinking';s.strict_model=True;s.upstream_model=None;s.upstream_thinking_effort=None;s.thinking_effort='extended';s.upstream_output_limited=False
    state=ConversationState(s);state.topic_id='current-topic'
    socket=SimpleNamespace(asend=AsyncMock(),aclose=AsyncMock())
    responses=[SimpleNamespace(status_code=200,json=lambda n=n:{'websocket_url':'wss://ws.chatgpt.com/?verify=SYNTHETIC_PRIVATE_'+str(n)}) for n in range(8)]
    s.s=SimpleNamespace(get=AsyncMock(side_effect=responses),session=SimpleNamespace(ws_connect=AsyncMock(return_value=socket)),recv_websocket=AsyncMock(return_value=(b'',CurlWsFlag.CLOSE)))
    return s,state,socket

class WebsocketBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_wait_uses_remaining_budget_instead_of_sixty_seconds(self):
        s,state,socket=fixture();original=asyncio.wait_for;timeouts=[]
        async def observe(coro,timeout):timeouts.append(timeout);return await original(coro,timeout)
        with patch('chatgpt.ChatService.websocket_idle_timeout',3600),patch('chatgpt.ChatService.asyncio.wait_for',side_effect=observe):
            await s.resume_web_handoff(state)
        self.assertTrue(any(170<t<=180 for t in timeouts));self.assertEqual(state.websocket_status,'closed');self.assertEqual(s.lifecycle_budget.generations,0)

    async def test_total_deadline_ends_idle_receive_and_closes(self):
        s,state,socket=fixture(seconds=.02)
        async def idle(_):await asyncio.Event().wait()
        s.s.recv_websocket=idle;started=time.monotonic()
        with patch('chatgpt.ChatService.websocket_idle_timeout',3600):await s.resume_web_handoff(state)
        self.assertLess(time.monotonic()-started,.5);self.assertEqual(state.websocket_status,'deadline_exhausted');socket.aclose.assert_awaited_once();self.assertEqual(s.lifecycle_budget.generations,0)

    async def test_configured_short_idle_is_distinct_from_total_deadline(self):
        s,state,socket=fixture(seconds=1)
        async def idle(_):await asyncio.Event().wait()
        s.s.recv_websocket=idle
        with patch('chatgpt.ChatService.websocket_idle_timeout',.01):await s.resume_web_handoff(state)
        self.assertEqual(state.websocket_status,'idle_timeout');self.assertGreater(s.lifecycle_budget.remaining(),.5);self.assertEqual(s.lifecycle_budget.generations,0)

    async def test_cancel_during_idle_propagates_and_closes(self):
        s,state,socket=fixture();receiving=asyncio.Event()
        async def idle(_):receiving.set();await asyncio.Event().wait()
        s.s.recv_websocket=idle;task=asyncio.create_task(s.resume_web_handoff(state));await receiving.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        socket.aclose.assert_awaited_once();self.assertEqual(s.lifecycle_budget.generations,0)

    async def test_only_401_reissues_fresh_url_and_does_not_regenerate(self):
        s,state,socket=fixture();s.s.session.ws_connect=AsyncMock(side_effect=[CurlError('HTTP code 401 SYNTHETIC_PRIVATE',22),socket])
        with patch('chatgpt.ChatService.websocket_handshake_max_attempts',3):await s.resume_web_handoff(state)
        self.assertEqual(s.s.get.await_count,2);urls=[c.args[0] for c in s.s.session.ws_connect.await_args_list];self.assertNotEqual(urls[0],urls[1]);self.assertEqual(state.websocket_handshake_statuses,{'http_401':1,'http_101':1});self.assertEqual(s.lifecycle_budget.read_types,{'websocket_setup':2});self.assertEqual(s.lifecycle_budget.generations,0);self.assertNotIn('SYNTHETIC_PRIVATE',json.dumps(state.summary()))

    async def test_persistent_401_is_bounded_to_three_attempts(self):
        s,state,socket=fixture();s.s.session.ws_connect=AsyncMock(side_effect=CurlError('HTTP code 401 SYNTHETIC_PRIVATE',22))
        with patch('chatgpt.ChatService.websocket_handshake_max_attempts',3):await s.resume_web_handoff(state)
        self.assertEqual(s.s.get.await_count,3);self.assertEqual(state.websocket_handshake_statuses,{'http_401':3});s.s.recv_websocket.assert_not_awaited();self.assertEqual(state.websocket_status,'transport_error');self.assertEqual(s.lifecycle_budget.generations,0)

    async def test_other_handshake_errors_are_not_retried(self):
        s,state,socket=fixture();s.s.session.ws_connect=AsyncMock(side_effect=CurlError('HTTP code 403 SYNTHETIC_PRIVATE',22))
        await s.resume_web_handoff(state);self.assertEqual(s.s.get.await_count,1);self.assertEqual(state.websocket_error['http_status'],403);self.assertEqual(s.lifecycle_budget.generations,0)

    async def test_shared_read_budget_stops_401_reissue(self):
        s,state,socket=fixture(reads=1);s.s.session.ws_connect=AsyncMock(side_effect=CurlError('HTTP code 401 SYNTHETIC_PRIVATE',22))
        await s.resume_web_handoff(state);self.assertEqual(s.s.get.await_count,1);self.assertEqual(state.websocket_status,'recovery_budget_exhausted');self.assertEqual(s.lifecycle_budget.reads,1);self.assertEqual(s.lifecycle_budget.generations,0)

    async def test_total_deadline_also_bounds_401_reissue(self):
        s,state,socket=fixture(seconds=.02);s.s.session.ws_connect=AsyncMock(side_effect=CurlError('HTTP code 401 SYNTHETIC_PRIVATE',22))
        await s.resume_web_handoff(state);self.assertEqual(s.s.get.await_count,1);self.assertLessEqual(s.lifecycle_budget.remaining(),0);self.assertEqual(s.lifecycle_budget.generations,0)

    async def test_protocol_error_fails_promptly_and_closes(self):
        s,state,socket=fixture();payload={'type':'message','topic_id':'current-topic','payload':{'type':'conversation-turn-stream','payload':{'type':'stream-item','encoded_item':'data: {INVALID}\n\n'}}}
        s.s.recv_websocket=AsyncMock(side_effect=[(json.dumps([{'id':1,'reply':{'type':'connect'}}]).encode(),CurlWsFlag.TEXT),(json.dumps([{'id':2,'reply':{'type':'subscribe','topic_id':'current-topic','recovered':True,'catchups':[]}}]).encode(),CurlWsFlag.TEXT),(json.dumps([payload]).encode(),CurlWsFlag.TEXT)])
        with self.assertRaises(UpstreamError):await s.resume_web_handoff(state)
        socket.aclose.assert_awaited_once();self.assertEqual(s.s.recv_websocket.await_count,3);self.assertEqual(s.lifecycle_budget.generations,0)

if __name__ == '__main__':unittest.main(verbosity=2)
