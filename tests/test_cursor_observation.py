"""Offline regression checks; no real payloads, accounts or network."""
import asyncio,ast,hashlib,json,struct,types,unittest
from pathlib import Path
from unittest.mock import patch
f=types.ModuleType('fixture')
tree=ast.parse((Path(__file__).resolve().parent / 'fixtures' / 'envelope_fixture.py').read_text())
tree.body=[n for n in tree.body if not (isinstance(n,ast.Expr) and isinstance(n.value,ast.Call) and isinstance(n.value.func,ast.Attribute) and n.value.func.attr=='main' and isinstance(n.value.func.value,ast.Name) and n.value.func.value.id=='unittest')]
exec(compile(tree,'frozen_fixture','exec'),f.__dict__)
def rejected(kind):
    value={'type':'subscribe','topic_id':'topic'}
    if kind!='missing':value['recovered']={'false':False,'null':None,'invalid_type':'false'}[kind]
    return f.frame({'id':2,'reply':value})
def close(payload=struct.pack('!H',1001)+b'PRIVATE_CLOSE_REASON'):return payload,f.CurlWsFlag.CLOSE

class CursorObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_reply_categories_preserve_legacy_failure(self):
        for kind in ('false','missing','null','invalid_type'):
            with self.subTest(kind=kind):
                s,state,sockets=f.fixture([[f.connect(),f.subscribe(f.seed()),close()],[f.connect(),rejected(kind)]])
                await f.run(s,state)
                self.assertEqual(state.websocket_status,'cursor_not_recovered')
                a,b=state.summary()['websocket_attempts']
                self.assertEqual(a['outcome'],'peer_close_frame');self.assertEqual(a['close_code'],1001)
                self.assertEqual(b['recovered_field'],kind)
                self.assertEqual(b['outcome'],'recovery_refused' if kind=='false' else 'invalid_recovered_field')
                self.assertEqual(a['frames'],2);self.assertEqual(b['frames'],2)
                self.assertEqual(s.lifecycle_budget.generations,0)
                for sock in sockets:sock.aclose.assert_awaited_once()
                public=json.dumps(state.summary())
                for private in ('PRIVATE_', 'p2', 'conversation-turn-stream', 'encoded_item'):
                    self.assertNotIn(private,public)
                for row in (a,b):self.assertGreaterEqual(row['exited_ms'],row['started_ms'])

    async def test_transport_error_survives_reconnect(self):
        s,state,_=f.fixture([[f.connect(),f.subscribe(f.seed()),f.CurlError('PRIVATE_SIGNED_URL',52)],[f.connect(),rejected('false')]])
        await f.run(s,state)
        self.assertEqual(state.websocket_attempts[0]['error']['code'],52)
        self.assertEqual(state.websocket_attempts[0]['outcome'],'transport_error')
        self.assertEqual(state.websocket_attempts[1]['outcome'],'recovery_refused')
        self.assertNotIn('PRIVATE_',json.dumps(state.summary()))

    async def test_empty_and_short_close_payload_do_not_change_termination(self):
        for payload in (b'',b'x'):
            s,state,_=f.fixture([[f.connect(),close(payload)]])
            await f.run(s,state)
            self.assertEqual(state.websocket_status,'closed');self.assertIsNone(state.websocket_attempts[0]['close_code'])

    async def test_mixed_cursor_mismatch_is_observed_without_policy_change(self):
        mixed=f.seed();mixed[-1].pop('offset')
        s,state,sockets=f.fixture([[f.connect(),f.subscribe(mixed,'batch-ack'),close()],[f.connect(),rejected('false')]])
        await f.run(s,state)
        a,b=state.websocket_attempts
        self.assertFalse(a['native_cursor_matches_applied'])
        self.assertEqual(a['applied_cursor_origin'],'subscription_last_offset')
        self.assertEqual(a['catchups_with_offset'],2);self.assertEqual(a['catchups_without_offset'],1)
        self.assertEqual(b['requested_cursor_hash'],hashlib.sha256(b'batch-ack').hexdigest())
        command=json.loads(sockets[1].asend.await_args_list[1].args[0])[0]['command']
        self.assertEqual(command['offset'],'batch-ack')

    async def test_live_cursor_advancement_keeps_comparison_meaningful(self):
        s,state,_=f.fixture([[f.connect(),f.subscribe(f.seed(),'batch-ack'),f.frame(f.append()),f.frame(f.control('done','done'))]])
        await f.run(s,state)
        a=state.websocket_attempts[0]
        self.assertTrue(a['native_cursor_matches_applied']);self.assertEqual(a['applied_cursor_origin'],'message_offset')
        self.assertEqual(a['live_messages'],2)

    async def test_success_preserves_timing_and_observation(self):
        s,state,_=f.fixture([[f.connect(),f.subscribe(f.seed()+[f.finish('p3','f','a')])]])
        state.transport_observation['initial_sse_exited_ms']=123
        origin=state._observation_origin
        await f.run(s,state)
        self.assertIsNotNone(state.candidate());self.assertEqual(state.transport_observation['initial_sse_exited_ms'],123)
        self.assertEqual(state._observation_origin,origin)
        self.assertEqual(state.websocket_attempts[0]['outcome'],'final_received')

    async def test_unsolicited_subscribe_does_not_add_unbound_local_failure(self):
        s,state,_=f.fixture([[rejected('missing')]])
        await f.run(s,state)
        self.assertEqual(state.websocket_status,'cursor_not_recovered')
        self.assertEqual(state.websocket_attempts[0]['recovered_field'],'missing')
        self.assertIsNone(state.websocket_error)

    async def test_checkpoint_and_sse_initial_phase_timing(self):
        import time
        s,_,_=f.fixture([]);origin=time.monotonic()-1
        s._diagnostic_sse_started=origin;s._diagnostic_sse_headers=origin+.1;s._diagnostic_sse_head_ready=origin+.2
        state=f.ConversationState(s)
        self.assertEqual(state.transport_observation,{'initial_sse_headers_ms':100.,'initial_sse_head_ready_ms':200.})
        state.websocket_attempts.append({'outcome':'peer_close_frame','close_code':1001})
        saved=state.checkpoint();state.websocket_attempts[0]['close_code']=1000
        self.assertEqual(saved.websocket_attempts[0]['close_code'],1001)

    async def test_sse_fallback_preserves_attempts_and_initial_exit_time(self):
        from chatgpt.chatFormat import stream_response
        s,_,_=f.fixture([])
        async def lines():
            for value in ({'type':'resume_conversation_token','token':'PRIVATE_TOKEN','conversation_id':'conversation'},
                          {'type':'stream_handoff','conversation_id':'conversation','options':[{'type':'subscribe_ws_topic','topic_id':'topic'}]}):
                yield ('data: '+json.dumps(value)).encode()
        expected={}
        async def ws(state):
            expected['exit']=state.transport_observation['initial_sse_exited_ms']
            state.websocket_topic_kind='other'
            state.websocket_attempts.append({'connection':1,'outcome':'peer_close_frame','close_code':1001})
        async def resume(state):
            self.assertEqual(state.websocket_attempts[0]['close_code'],1001)
            self.assertEqual(state.transport_observation['initial_sse_exited_ms'],expected['exit'])
        s.resume_web_handoff=ws;s.resume_web_sse=resume;s.recover_web_conversation=f.AsyncMock()
        frames=[item async for item in stream_response(s,lines(),'gpt-6-thinking',100)]
        error=json.loads(frames[0][6:])['error']
        self.assertEqual(error['code'],'upstream_no_final_response')
        self.assertEqual(error['lifecycle']['websocket_attempts'][0]['close_code'],1001)

    async def test_attempt_cap_does_not_change_reconnect_budget(self):
        sequences=[[f.connect(),f.subscribe(f.seed() if i==0 else []),close()] for i in range(34)]
        s,state,_=f.fixture(sequences)
        # Fixture endpoint responses default to eight; extend only synthetic reads.
        response=types.SimpleNamespace(status_code=200,json=lambda:{'websocket_url':'wss://ws.chatgpt.com/?verify=PRIVATE_CREDENTIAL'})
        s.s.get.side_effect=None;s.s.get.return_value=response
        with patch('chatgpt.ChatService.websocket_receive_max_reconnects',33),patch('chatgpt.ChatService.websocket_handshake_max_attempts',1):
            await f.run(s,state)
        self.assertEqual(state.websocket_connections,34)
        self.assertEqual(len(state.websocket_attempts),32);self.assertTrue(state.websocket_attempts_truncated)
        self.assertEqual(s.lifecycle_budget.generations,0)

if __name__=='__main__':unittest.main(verbosity=2)
