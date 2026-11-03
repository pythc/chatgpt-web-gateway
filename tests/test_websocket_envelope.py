import asyncio,contextlib,io,json,logging,unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
logging.disable(logging.CRITICAL)
with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
    from app import app
    from chatgpt.ChatService import ChatService
    from chatgpt.webStream import ConversationState,LifecycleBudget,UpstreamError,events
from curl_cffi import CurlError
from curl_cffi.const import CurlWsFlag

def event(value,ending=''):return 'data: '+json.dumps(value)+ending
def user():return {'type':'input_message','input_message':{'id':'u','author':{'role':'user'}}}
def message(text='A',done=False):return {'id':'f','author':{'role':'assistant'},'channel':'final','content':{'content_type':'text','parts':[text]},'status':'finished_successfully' if done else 'in_progress','end_turn':done,'metadata':{'model_slug':'gpt-6-thinking','thinking_effort':'extended'}}
def packet(offset,value,sid,parent=None,ending='',**changes):
    inner={'type':'stream-item','conversation_id':'conversation','turn_id':'turn','stream_item_id':sid,'parent_stream_item_id':parent,'encoded_item':event(value,ending)};inner.update(changes)
    return {'type':'message','topic_id':'topic','offset':offset,'payload':{'type':'conversation-turn-stream','payload':inner}}
def control(offset,kind,**changes):
    inner={'type':kind,'conversation_id':'conversation','turn_id':'turn'};inner.update(changes)
    return {'type':'message','topic_id':'topic','offset':offset,'payload':{'type':'conversation-turn-stream','payload':inner}}
def seed():return [packet('p0','v1','v'),packet('p1',user(),'u','v'),packet('p2',{'p':'','o':'add','v':{'message':message()}},'a','u')]
def append(offset='p3',text='B',sid='b',parent='a'):return packet(offset,{'p':'/message/content/parts/0','o':'append','v':text},sid,parent)
def finish(offset='p5',sid='f',parent='c'):return packet(offset,{'p':'','o':'patch','v':[{'p':'/message/status','o':'replace','v':'finished_successfully'},{'p':'/message/end_turn','o':'replace','v':True}]},sid,parent)
def frame(*items):return json.dumps(list(items)).encode(),CurlWsFlag.TEXT
def connect():return frame({'id':1,'reply':{'type':'connect'}})
def subscribe(items=(),last_offset=None):return frame({'id':2,'reply':{'type':'subscribe','topic_id':'topic','recovered':True,'catchups':list(items),**({'last_offset':last_offset} if last_offset is not None else {})}})
def fixture(sequences):
    s=ChatService.__new__(ChatService);s.base_url='https://chatgpt.com/backend-api';s.host_url='https://chatgpt.com';s.base_headers={};s.lifecycle_budget=LifecycleBudget(5)
    s.input_message_id='u';s.req_model='gpt-6-thinking';s.strict_model=True;s.upstream_model=None;s.upstream_thinking_effort=None;s.thinking_effort='extended';s.upstream_output_limited=False
    state=ConversationState(s);state.topic_id='topic';state.conversation_id='conversation';state.resume_token='PRIVATE_CREDENTIAL'
    sockets=[SimpleNamespace(asend=AsyncMock(),aclose=AsyncMock()) for _ in sequences];streams={id(sock):list(seq) for sock,seq in zip(sockets,sequences)}
    async def receive(sock):
        value=streams[id(sock)].pop(0)
        if isinstance(value,BaseException):raise value
        return value
    responses=[SimpleNamespace(status_code=200,json=lambda:{'websocket_url':'wss://ws.chatgpt.com/?verify=PRIVATE_CREDENTIAL'}) for _ in range(8)]
    s.s=SimpleNamespace(get=AsyncMock(side_effect=responses),session=SimpleNamespace(ws_connect=AsyncMock(side_effect=sockets)),recv_websocket=AsyncMock(side_effect=receive))
    return s,state,sockets
async def run(s,state):
    original=asyncio.sleep
    async def quick(_):await original(0)
    with patch('chatgpt.ChatService.asyncio.sleep',side_effect=quick):await s.resume_web_handoff(state)

class EnvelopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_literal_unicode_separators_are_native_json_content(self):
        for separator in ('\u0085','\u2028','\u2029'):
            for ending in ('','\n\n','\r\n\r\n'):
                with self.subTest(separator=ord(separator),ending=ending):
                    text='LEFT'+separator+'RIGHT';value={'message':message(text,True)}
                    final=packet('p1',value,'f','u');final['payload']['payload']['encoded_item']='data: '+json.dumps(value,ensure_ascii=False)+ending
                    s,state,_=fixture([[connect(),subscribe([packet('p0',user(),'u'),final])]])
                    await run(s,state);self.assertEqual(state.candidate()[0]['content']['parts'],[text]);self.assertEqual(s.lifecycle_budget.generations,0)

    async def test_literal_unicode_separators_survive_native_sse_decoder(self):
        for separator in ('\u0085','\u2028','\u2029'):
            with self.subTest(separator=ord(separator)):
                value={'text':'LEFT'+separator+'RIGHT'}
                async def lines():yield ('data: '+json.dumps(value,ensure_ascii=False)+'\r\n\r\n').encode()
                decoded=[item async for item in events(lines())]
                self.assertEqual(decoded,[value])

    async def test_complete_single_items_with_no_blank_lf_or_crlf(self):
        for ending in ('','\n','\n\n','\r\n\r\n'):
            with self.subTest(ending=ending):
                s,state,_=fixture([[connect(),subscribe([packet('p0',user(),'u',ending=ending),packet('p1',{'message':message('FINAL',True)},'f','u',ending=ending)])]])
                await run(s,state);self.assertEqual(state.candidate()[0]['content']['parts'],['FINAL']);self.assertEqual(state.websocket_pending_characters,0);self.assertEqual(s.lifecycle_budget.generations,0)
                if ending in ('','\n'):self.assertEqual(state.websocket_items_without_separator,2)

    async def test_native_same_id_different_offsets_deduplicates_nonidempotent_append(self):
        b=append();duplicate=append('different-offset');items=seed()+[b,duplicate,append('p4','C','c','b'),finish()];s,state,_=fixture([[connect(),subscribe(items)]])
        await run(s,state);self.assertEqual(state.candidate()[0]['content']['parts'],['ABC']);self.assertEqual(state.websocket_stream_id_duplicates,1);self.assertEqual(state.websocket_stream_items_with_id,7);self.assertEqual(s.lifecycle_budget.generations,0)

    async def test_native_id_dedup_survives_reconnect_and_new_pubsub_cursor(self):
        s,state,sockets=fixture([[connect(),subscribe(seed()+[append()]),CurlError('SYNTHETIC',52)],[connect(),subscribe([append('different-offset'),append('p4','C','c','b'),finish()])]])
        await run(s,state);self.assertEqual(state.candidate()[0]['content']['parts'],['ABC']);self.assertEqual(state.websocket_reconnects,1);self.assertEqual(state.websocket_stream_id_duplicates,1)
        command=json.loads(sockets[1].asend.await_args_list[1].args[0])[0]['command'];self.assertEqual(command['offset'],'p3')

    async def test_same_id_changed_payload_is_rejected(self):
        s,state,_=fixture([[connect(),subscribe(seed()+[append(),append('changed','WRONG')])]])
        with self.assertRaises(UpstreamError) as error:await run(s,state)
        self.assertEqual(error.exception.detail['error']['code'],'upstream_stream_item_conflict');self.assertEqual(state.websocket_stream_item_conflicts,1);self.assertIsNone(state.candidate())

    async def test_same_id_changed_parent_is_rejected_even_if_parent_seen(self):
        s,state,_=fixture([[connect(),subscribe(seed()+[append(),append('changed','B','b','v')])]])
        with self.assertRaises(UpstreamError) as error:await run(s,state)
        self.assertEqual(error.exception.detail['error']['code'],'upstream_stream_item_conflict');self.assertEqual(state.websocket_stream_item_conflicts,1)

    async def test_envelope_conversation_and_turn_conflicts_are_rejected(self):
        for field in ('conversation_id','turn_id'):
            with self.subTest(field=field):
                changed=append('changed');changed['payload']['payload'][field]='DIFFERENT';s,state,_=fixture([[connect(),subscribe(seed()+[append(),changed])]])
                with self.assertRaises(UpstreamError) as error:await run(s,state)
                self.assertEqual(error.exception.detail['error']['code'],'upstream_turn_mismatch');self.assertEqual(state.websocket_stream_binding_conflicts,1);self.assertIsNone(state.candidate())

    async def test_missing_parent_does_not_blindly_apply_delta(self):
        s,state,sockets=fixture([[connect(),subscribe(seed()+[append('gap','B','b','MISSING_PARENT')])]])
        await run(s,state);self.assertEqual(state.websocket_status,'stream_parent_missing');self.assertEqual(state.websocket_stream_parent_misses,1);self.assertIsNone(state.candidate());self.assertEqual(s.lifecycle_budget.generations,0);sockets[0].aclose.assert_awaited_once()

    async def test_parent_only_needs_to_have_been_seen_not_be_last(self):
        s,state,_=fixture([[connect(),subscribe(seed()+[append('p3','B','b','u'),finish('p4','f','b')])]])
        await run(s,state);self.assertEqual(state.candidate()[0]['content']['parts'],['AB']);self.assertEqual(state.websocket_stream_parent_misses,0)

    async def test_done_without_final_stops_before_late_final_or_socket_close(self):
        s,state,sockets=fixture([[connect(),subscribe(seed()),frame(control('done','done')),frame(packet('late',{'message':message('LATE',True)},'late','a'))]])
        await run(s,state);self.assertTrue(state.websocket_terminal_received);self.assertEqual(state.websocket_status,'stream_done');self.assertIsNone(state.candidate());self.assertEqual(s.s.recv_websocket.await_count,3);self.assertEqual(state.websocket_reconnects,0);self.assertEqual(s.lifecycle_budget.generations,0);sockets[0].aclose.assert_awaited_once()

    async def test_heartbeat_is_not_output_or_completion(self):
        s,state,_=fixture([[connect(),subscribe(seed()),frame(control('hb','heartbeat',conversation_id=None)),frame(append()),frame(finish('p4','f','b'))]])
        await run(s,state);self.assertEqual(state.candidate()[0]['content']['parts'],['AB']);self.assertEqual(state.websocket_heartbeats,1);self.assertFalse(state.websocket_terminal_received)

    async def test_done_after_heartbeat_still_has_no_final(self):
        s,state,_=fixture([[connect(),subscribe(seed()),frame(control('hb','heartbeat')),frame(control('done','done'))]])
        await run(s,state);self.assertEqual(state.websocket_heartbeats,1);self.assertTrue(state.websocket_terminal_received);self.assertIsNone(state.candidate());self.assertEqual(s.s.recv_websocket.await_count,4)

    async def test_incomplete_legacy_tail_at_done_is_not_returned(self):
        partial=append();inner=partial['payload']['payload'];inner.pop('stream_item_id');inner.pop('parent_stream_item_id');inner['encoded_item']='data: {"SYNTHETIC_PRIVATE":'
        s,state,_=fixture([[connect(),subscribe(seed()),frame(partial),frame(control('done','done'))]])
        await run(s,state);self.assertEqual(state.websocket_status,'stream_done_incomplete');self.assertGreater(state.websocket_pending_characters,0);self.assertIsNone(state.candidate());self.assertNotIn('SYNTHETIC_PRIVATE',json.dumps(state.summary()))

    async def test_native_multiple_events_in_one_id_is_not_a_valid_single_item(self):
        value=packet('bad',user(),'x');value['payload']['payload']['encoded_item']=event(user(),'\n\n')+event({'message':message('WRONG',True)},'\n\n');s,state,_=fixture([[connect(),subscribe([value])]])
        await run(s,state);self.assertEqual(state.websocket_status,'stream_item_invalid');self.assertIsNone(state.candidate())

    async def test_legacy_multiple_events_without_native_identity_remain_supported(self):
        value=packet('old',user(),None);inner=value['payload']['payload'];inner.pop('stream_item_id');inner.pop('parent_stream_item_id');inner['encoded_item']=event(user(),'\n\n')+event({'message':message('FINAL',True)},'\n\n');s,state,_=fixture([[connect(),subscribe([value])]])
        await run(s,state);self.assertEqual(state.candidate()[0]['content']['parts'],['FINAL'])

    async def test_heartbeat_cannot_change_bound_turn(self):
        s,state,_=fixture([[connect(),subscribe(seed()),frame(control('hb','heartbeat',turn_id='DIFFERENT'))]])
        with self.assertRaises(UpstreamError) as error:await run(s,state)
        self.assertEqual(error.exception.detail['error']['code'],'upstream_turn_mismatch');self.assertIsNone(state.candidate())

    async def test_encoded_sse_done_does_not_replace_outer_turn_done(self):
        sentinel=packet('sse-done',None,'sse-done','a');sentinel['payload']['payload']['encoded_item']='data: [DONE]';s,state,_=fixture([[connect(),subscribe(seed()+[sentinel,append('p4','B','b','sse-done'),finish('p5','f','b')])]])
        await run(s,state);self.assertEqual(state.candidate()[0]['content']['parts'],['AB']);self.assertFalse(state.websocket_terminal_received)

    async def test_same_cursor_changed_binding_is_not_hidden_by_offset_dedup(self):
        changed=append();changed['payload']['payload']['turn_id']='DIFFERENT';s,state,_=fixture([[connect(),subscribe(seed()+[append(),changed])]])
        with self.assertRaises(UpstreamError) as error:await run(s,state)
        self.assertEqual(error.exception.detail['error']['code'],'upstream_turn_mismatch')

    async def test_invalid_native_item_has_no_partial_text_or_generation(self):
        invalid=packet('bad',user(),'bad');invalid['payload']['payload']['encoded_item']='data: {"PRIVATE_INVALID"';s,state,_=fixture([[connect(),subscribe([invalid])]])
        await run(s,state);self.assertEqual(state.websocket_status,'stream_item_invalid');self.assertEqual(s.lifecycle_budget.generations,0);self.assertNotIn('PRIVATE_INVALID',json.dumps(state.summary()))

if __name__ == '__main__':unittest.main(verbosity=2)
