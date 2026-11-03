import asyncio,contextlib,io,json,logging,unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
logging.disable(logging.CRITICAL)
with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
    from app import app
    from chatgpt.ChatService import ChatService
    from chatgpt.webStream import ConversationState,LifecycleBudget,UpstreamError
from curl_cffi import CurlError
from curl_cffi.const import CurlWsFlag

def data(x):return 'data: '+json.dumps(x)+'\n\n'
def msg(mid,role='assistant',text='A',done=False):
    return {'id':mid,'author':{'role':role},'channel':'final' if role=='assistant' else None,'content':{'content_type':'text','parts':[text]},'status':'finished_successfully' if done or role=='user' else 'in_progress','end_turn':done,'metadata':{'model_slug':'gpt-6-thinking','thinking_effort':'extended'} if role=='assistant' else {}}
def item(offset,encoded,topic='topic'):
    return {'type':'message','topic_id':topic,'offset':offset,'payload':{'type':'conversation-turn-stream','payload':{'type':'stream-item','encoded_item':encoded}}}
def frame(*items):return json.dumps(list(items)).encode(),CurlWsFlag.TEXT
def connect():return frame({'id':1,'reply':{'type':'connect'}})
def subscribe(catchups=(),recovered=True,last_offset=None):return frame({'id':2,'reply':{'type':'subscribe','topic_id':'topic','recovered':recovered,'catchups':list(catchups),**({'last_offset':last_offset} if last_offset is not None else {})}})
def initial(offset='opaque-z'):
    return item(offset,data('v1')+data({'p':'','o':'add','v':{'message':msg('u','user','public')}})+data({'p':'','o':'add','v':{'message':msg('f')}}))
def append(offset='opaque-a',text='B',done=False):
    encoded=data({'p':'/message/content/parts/0','o':'append','v':text})
    if done:encoded+=data({'p':'','o':'patch','v':[{'p':'/message/status','o':'replace','v':'finished_successfully'},{'p':'/message/end_turn','o':'replace','v':True}]})
    return item(offset,encoded)
def lost():return CurlError('WS_RECV SYNTHETIC_PRIVATE',52)

def fixture(sequences,reads=120,seconds=20):
    s=ChatService.__new__(ChatService);s.base_url='https://chatgpt.com/backend-api';s.host_url='https://chatgpt.com';s.base_headers={};s.lifecycle_budget=LifecycleBudget(seconds,max_reads=reads)
    s.input_message_id='u';s.req_model='gpt-6-thinking';s.strict_model=True;s.upstream_model=None;s.upstream_thinking_effort=None;s.thinking_effort='extended';s.upstream_output_limited=False
    state=ConversationState(s);state.topic_id='topic';state.conversation_id='bound-conversation';state.resume_token='SYNTHETIC_PRIVATE'
    sockets=[SimpleNamespace(asend=AsyncMock(),aclose=AsyncMock()) for _ in sequences];streams={id(sock):list(seq) for sock,seq in zip(sockets,sequences)}
    async def receive(sock):
        value=streams[id(sock)].pop(0)
        if isinstance(value,BaseException):raise value
        return value
    responses=[SimpleNamespace(status_code=200,json=lambda n=n:{'websocket_url':'wss://ws.chatgpt.com/?verify=SYNTHETIC_PRIVATE_'+str(n)}) for n in range(len(sequences)+2)]
    s.s=SimpleNamespace(get=AsyncMock(side_effect=responses),session=SimpleNamespace(ws_connect=AsyncMock(side_effect=sockets)),recv_websocket=receive)
    return s,state,sockets

async def run(s,state,reconnects=5):
    original=asyncio.sleep
    async def quick(_):await original(0)
    with patch('chatgpt.ChatService.websocket_receive_max_reconnects',reconnects),patch('chatgpt.ChatService.asyncio.sleep',side_effect=quick):await s.resume_web_handoff(state)

class WebsocketResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_trusted_presigned_ws_url_reaches_connector_without_normalization(self):
        s,state,sockets=fixture([[connect(),subscribe([initial(),append('end','B',True)])]])
        s.s.connect_websocket=AsyncMock(return_value=sockets[0])
        await run(s,state)
        self.assertEqual(state.candidate()[0]['content']['parts'],['AB'])
        self.assertIs(s.s.connect_websocket.await_args.kwargs['quote'],False)
        self.assertEqual(s.s.connect_websocket.await_args.args[0],'wss://ws.chatgpt.com/?verify=SYNTHETIC_PRIVATE_0')
        self.assertEqual(s.lifecycle_budget.generations,0)

    async def test_same_topic_opaque_cursor_reconnect_deduplicates_catchup_and_live(self):
        a,b,c=initial(),append(),append('opaque-c','C',True)
        s,state,sockets=fixture([[connect(),subscribe([a,b]),lost()],[connect(),subscribe([b]),frame(b,c)]])
        await run(s,state);self.assertEqual(state.candidate()[0]['content']['parts'],['ABC']);self.assertEqual(state.websocket_connections,2);self.assertEqual(state.websocket_reconnects,1);self.assertEqual(state.websocket_duplicate_messages,2)
        command=json.loads(sockets[1].asend.await_args_list[1].args[0])[0]['command'];self.assertEqual(command,{'type':'subscribe','topic_id':'topic','offset':'opaque-a'});self.assertEqual(s.lifecycle_budget.generations,0);self.assertEqual(s.lifecycle_budget.reads,2)
        self.assertNotIn('SYNTHETIC_PRIVATE',json.dumps(state.summary()))

    async def test_offset_zero_catchup_uses_fresh_decoder_not_bootstrap_append(self):
        a,b,c=initial(),append(),append('opaque-c','C',True);s,state,_=fixture([[connect(),subscribe([a,b,c])]])
        state.snapshot(msg('u','user','public'));state.snapshot(msg('f',text='AB'))
        await run(s,state);self.assertEqual(state.candidate()[0]['content']['parts'],['ABC'])

    async def test_partial_sse_and_split_crlf_survive_reconnect_and_duplicate(self):
        full=data({'p':'/message/content/parts/0','o':'append','v':'B'}).replace('\n','\r\n')
        for cut in (full.index('append')+3,len(full)-1):
            with self.subTest(cut=cut):
                first=item('fragment-1',full[:cut]);second=item('fragment-2',full[cut:]);final=append('finish','C',True)
                s,state,_=fixture([[connect(),subscribe([initial(),first]),lost()],[connect(),subscribe([first,second]),frame(final)]])
                await run(s,state);self.assertEqual(state.candidate()[0]['content']['parts'],['ABC']);self.assertEqual(state.websocket_duplicate_messages,1)

    async def test_live_before_ack_waits_for_catchup_and_deduplicates(self):
        b=append();s,state,_=fixture([[connect(),frame(b),subscribe([initial(),b]),frame(append('end','C',True))]])
        await run(s,state);self.assertEqual(state.candidate()[0]['content']['parts'],['ABC']);self.assertEqual(state.websocket_duplicate_messages,1)

    async def test_missing_cursor_does_not_reconnect_or_apply_offset_zero(self):
        s,state,_=fixture([[connect(),subscribe([initial(None)]),lost()]])
        await run(s,state);self.assertIsNone(state.candidate());self.assertEqual(state.websocket_connections,1);self.assertEqual(state.websocket_reconnects,0);self.assertEqual(state.websocket_status,'cursor_unavailable')

    async def test_not_recovered_ack_does_not_append_catchup_to_existing_state(self):
        s,state,_=fixture([[connect(),subscribe([initial()]),lost()],[connect(),subscribe([append('bad','WRONG',True)],False)]])
        await run(s,state);self.assertIsNone(state.candidate());self.assertEqual(state.websocket_status,'cursor_not_recovered');self.assertNotIn('WRONG',json.dumps(state.messages))

    async def test_initial_not_recovered_ignores_catchups_and_keeps_bootstrap_for_live(self):
        s,state,_=fixture([[connect(),subscribe([append('bad','WRONG',True)],False),frame(append('good','B',True))]])
        state.feed('v1');state.feed({'p':'','o':'add','v':{'message':msg('u','user','public')}});state.feed({'p':'','o':'add','v':{'message':msg('f')}})
        await run(s,state);self.assertEqual(state.candidate()[0]['content']['parts'],['AB']);self.assertNotIn('WRONG',json.dumps(state.messages))

    async def test_same_cursor_with_changed_payload_is_typed_error_and_closes(self):
        a=initial();s,state,sockets=fixture([[connect(),subscribe([a]),frame(item(a['offset'],data({'message':msg('wrong',text='WRONG',done=True)})))]])
        with self.assertRaises(UpstreamError) as error:await run(s,state)
        self.assertEqual(error.exception.detail['error']['code'],'upstream_cursor_conflict');sockets[0].aclose.assert_awaited_once();self.assertEqual(s.lifecycle_budget.generations,0)

    async def test_later_user_turn_cannot_supply_the_original_answer(self):
        other=item('other',data({'p':'','o':'add','v':{'message':msg('later','user','different')}}));s,state,sockets=fixture([[connect(),subscribe([initial(),other,append('end','WRONG',True)])]])
        with self.assertRaises(UpstreamError) as error:await run(s,state)
        self.assertEqual(error.exception.detail['error']['code'],'upstream_turn_mismatch');self.assertIsNone(state.candidate());sockets[0].aclose.assert_awaited_once()

    async def test_other_topics_do_not_change_target_cursor_or_answer(self):
        wrong=append('wrong','WRONG',True);wrong['topic_id']='another';s,state,_=fixture([[connect(),subscribe([initial(),wrong]),frame(append('end','B',True))]])
        await run(s,state);self.assertEqual(state.candidate()[0]['content']['parts'],['AB']);self.assertEqual(state.topic_id,'topic')

    async def test_shared_read_budget_prevents_new_connection(self):
        s,state,_=fixture([[connect(),subscribe([initial()]),lost()]],reads=1)
        await run(s,state);self.assertEqual(s.s.get.await_count,1);self.assertEqual(state.websocket_status,'recovery_budget_exhausted');self.assertEqual(s.lifecycle_budget.generations,0)

    async def test_receive_reconnect_count_is_bounded(self):
        s,state,sockets=fixture([[connect(),subscribe([initial()]),lost()],[connect(),subscribe(),lost()]])
        await run(s,state,reconnects=1);self.assertEqual(state.websocket_connections,2);self.assertEqual(state.websocket_reconnects,1);self.assertEqual(s.s.get.await_count,2);self.assertEqual(s.lifecycle_budget.generations,0)
        for sock in sockets:sock.aclose.assert_awaited_once()

    async def test_closed_connection_can_recover_only_with_applied_cursor(self):
        s,state,_=fixture([[connect(),subscribe([initial()]),(b'',CurlWsFlag.CLOSE)],[connect(),subscribe([append('end','B',True)])]])
        await run(s,state);self.assertEqual(state.candidate()[0]['content']['parts'],['AB']);self.assertEqual(state.websocket_reconnects,1)

    async def test_nonconnection_error_does_not_reconnect(self):
        s,state,_=fixture([[connect(),subscribe([initial()]),ValueError('SYNTHETIC_PRIVATE')]])
        await run(s,state);self.assertEqual(s.s.get.await_count,1);self.assertEqual(state.websocket_reconnects,0);self.assertNotIn('SYNTHETIC_PRIVATE',json.dumps(state.summary()))

    async def test_unrecovered_wire_changes_do_not_corrupt_bootstrap(self):
        s,state,_=fixture([[connect(),subscribe([initial(),append(None,'B')]),lost()]])
        state.snapshot(msg('u','user','public'));state.snapshot(msg('f',text='BASELINE'))
        await run(s,state);self.assertEqual(state.messages['f']['content']['parts'],['BASELINE']);self.assertIsNone(state.candidate())

    async def test_applied_catchup_batch_last_offset_is_a_valid_resume_boundary(self):
        a=initial(None);b=append(None,'B');s,state,sockets=fixture([[connect(),subscribe([a,b],last_offset='batch-z'),lost()],[connect(),subscribe([append(None,'C',True)],last_offset='batch-a')]])
        await run(s,state);self.assertEqual(state.candidate()[0]['content']['parts'],['ABC']);command=json.loads(sockets[1].asend.await_args_list[1].args[0])[0]['command'];self.assertEqual(command['offset'],'batch-z')

    async def test_replayed_unidentified_batch_is_deduplicated_by_ack_cursor(self):
        batch=[initial(None),append(None,'B')];s,state,_=fixture([[connect(),subscribe(batch,last_offset='batch-z'),lost()],[connect(),subscribe(batch,last_offset='batch-z'),frame(append('end','C',True))]])
        await run(s,state);self.assertEqual(state.candidate()[0]['content']['parts'],['ABC']);self.assertEqual(state.websocket_duplicate_messages,2)

    async def test_only_tail_of_unidentified_batch_can_be_replayed_at_same_ack(self):
        a=initial(None);b=append(None,'B');s,state,_=fixture([[connect(),subscribe([a,b],last_offset='batch-z'),lost()],[connect(),subscribe([b],last_offset='batch-z'),frame(append('end','C',True))]])
        await run(s,state);self.assertEqual(state.candidate()[0]['content']['parts'],['ABC']);self.assertEqual(state.websocket_duplicate_messages,1)

    async def test_full_then_tail_then_full_replay_keeps_canonical_batch(self):
        a=initial(None);b=append(None,'B');s,state,_=fixture([[connect(),subscribe([a,b],last_offset='batch-z'),lost()],[connect(),subscribe([b],last_offset='batch-z'),lost()],[connect(),subscribe([a,b],last_offset='batch-z'),frame(append('end','C',True))]])
        await run(s,state);self.assertEqual(state.candidate()[0]['content']['parts'],['ABC']);self.assertEqual(state.websocket_duplicate_messages,3);self.assertEqual(state.websocket_connections,3);self.assertEqual(s.lifecycle_budget.generations,0)

    async def test_matching_tail_cannot_hide_unknown_prefix_or_new_user(self):
        prefixes=[append(None,'UNEXPECTED'),item(None,data({'type':'input_message','input_message':msg('later','user','different')}))]
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                a=initial(None);b=append(None,'B');s,state,_=fixture([[connect(),subscribe([a,b],last_offset='batch-z'),lost()],[connect(),subscribe([prefix,b],last_offset='batch-z'),frame(append('end','WRONG',True))]])
                with self.assertRaises(UpstreamError) as error:await run(s,state)
                self.assertEqual(error.exception.detail['error']['code'],'upstream_cursor_conflict');self.assertIsNone(state.candidate());self.assertEqual(s.lifecycle_budget.generations,0)

    async def test_empty_initial_last_offset_is_not_treated_as_applied_cursor(self):
        s,state,_=fixture([[connect(),subscribe(last_offset='not-applied'),lost()]])
        await run(s,state);self.assertEqual(s.s.get.await_count,1);self.assertFalse(state.websocket_cursor_available)

    async def test_conflicting_unidentified_batch_at_same_cursor_fails_closed(self):
        s,state,sockets=fixture([[connect(),subscribe([initial(None)],last_offset='batch-z'),lost()],[connect(),subscribe([append(None,'WRONG',True)],last_offset='batch-z')]])
        with self.assertRaises(UpstreamError) as error:await run(s,state)
        self.assertEqual(error.exception.detail['error']['code'],'upstream_cursor_conflict');self.assertIsNone(state.candidate())
        for sock in sockets:sock.aclose.assert_awaited_once()

    async def test_wire_metadata_does_not_leak_into_native_sse_fallback(self):
        incomplete=msg('f');incomplete['metadata']['finish_details']={'type':'max_tokens'}
        packet=item(None,data({'message':msg('u','user','public')})+data({'message':incomplete}))
        s,state,_=fixture([[connect(),subscribe([packet]),lost()]])
        await run(s,state);self.assertFalse(s.upstream_output_limited);self.assertIsNone(s.upstream_model);self.assertIsNone(s.upstream_thinking_effort)
        async def native_lines():
            for event in ({'message':msg('u','user','public')},{'message':msg('native',text='NATIVE',done=True)}):yield data(event)
        response=SimpleNamespace(status_code=200,headers={'Content-Type':'text/event-stream'},aiter_lines=native_lines,aclose=AsyncMock());s.s.post_stream=AsyncMock(return_value=response)
        await s.resume_web_sse(state);self.assertEqual(state.candidate()[0]['content']['parts'],['NATIVE']);self.assertEqual(state.candidate()[1],'stop');self.assertFalse(s.upstream_output_limited);self.assertEqual(s.upstream_model,'gpt-6-thinking');self.assertEqual(s.lifecycle_budget.generations,0)

    async def test_native_input_message_later_user_is_rejected(self):
        def inp(mid,role='user'):return {'type':'input_message','input_message':msg(mid,role,'public')}
        packet=item('batch',data(inp('u'))+data(inp('later'))+data({'message':msg('wrong',text='WRONG',done=True)}));s,state,sockets=fixture([[connect(),subscribe([packet])]])
        with self.assertRaises(UpstreamError) as error:await run(s,state)
        self.assertEqual(error.exception.detail['error']['code'],'upstream_turn_mismatch');self.assertIsNone(state.candidate());sockets[0].aclose.assert_awaited_once()

    async def test_nested_native_input_message_later_user_is_rejected(self):
        incoming=lambda mid:{'type':'input_message','input_message':msg(mid,'user','public')}
        packet=item('batch',data([[incoming('old')],[incoming('u'),[incoming('later')]],{'message':msg('wrong',text='WRONG',done=True)}]));s,state,_=fixture([[connect(),subscribe([packet])]])
        with self.assertRaises(UpstreamError) as error:await run(s,state)
        self.assertEqual(error.exception.detail['error']['code'],'upstream_turn_mismatch');self.assertIsNone(state.candidate())

    async def test_historical_native_users_before_target_are_allowed(self):
        incoming=lambda mid:{'type':'input_message','input_message':msg(mid,'user','public')}
        packet=item('batch',data([[incoming('old'),incoming('older')],incoming('u'),{'message':msg('right',text='RIGHT',done=True)}]));s,state,_=fixture([[connect(),subscribe([packet])]])
        await run(s,state);self.assertEqual(state.candidate()[0]['content']['parts'],['RIGHT']);self.assertEqual(s.lifecycle_budget.generations,0)

    async def test_system_inputs_after_target_and_duplicate_target_are_allowed(self):
        inp=lambda mid,role:{'type':'input_message','input_message':msg(mid,role,'public')}
        packet=item('batch',data([inp('u','user'),[inp('system','system'),inp('u','user')],{'message':msg('right',text='RIGHT',done=True)}]));s,state,_=fixture([[connect(),subscribe([packet])]])
        await run(s,state);self.assertEqual(state.candidate()[0]['content']['parts'],['RIGHT']);self.assertEqual(s.lifecycle_budget.generations,0)

if __name__ == '__main__':unittest.main(verbosity=2)
