import asyncio,contextlib,copy,io,json,logging,unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
logging.disable(logging.CRITICAL)
with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
    from app import app
    from chatgpt.chatFormat import head_process_response,stream_response,format_not_stream_response
    from chatgpt.ChatService import ChatService
    from chatgpt.webStream import ConversationState,UpstreamError,LifecycleBudget,events

def service():
    return SimpleNamespace(req_model='gpt-6-thinking',strict_model=True,upstream_model=None,
        upstream_thinking_effort=None,thinking_effort='extended',history_disabled=True,upstream_output_limited=False)

def message(mid='final',text='{"ok":true}',channel='final',ctype='text',status='finished_successfully',end=True,role='assistant',recipient='all',**meta):
    return {'id':mid,'author':{'role':role},'channel':channel,'content':{'content_type':ctype,'parts':[text]},
        'status':status,'end_turn':end,'recipient':recipient,'metadata':{'model_slug':'gpt-6-thinking','thinking_effort':'extended',**meta}}

async def lines(*items):
    for item in items:
        if isinstance(item,bytes):yield item
        else:yield ('data: '+(item if isinstance(item,str) else json.dumps(item))).encode()

async def complete(s,*items):
    replay,_=await head_process_response(lines(*items),s)
    return await format_not_stream_response(stream_response(s,replay,'gpt-6-thinking',262144),1,262144,'gpt-6-thinking')

class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def test_misdirected_native_tool_metadata_is_bounded_and_not_executable_output(self):
        state=ConversationState(service())
        state.feed({'message':message('call','PRIVATE_TOOL_INPUT',channel='commentary',recipient='functions.exec')})
        state.feed({'message':message('unknown','PRIVATE',channel='commentary',recipient='private-recipient')})
        state.feed({'message':message('final','No client execution occurred.')})
        summary=state.summary()
        self.assertEqual(summary['upstream_tool_recipients'],{'functions.exec':1,'other':1})
        self.assertNotIn('PRIVATE',json.dumps(summary));self.assertNotIn('private-recipient',json.dumps(summary))
        self.assertEqual(state.candidate()[0]['id'],'final')
    def test_supplied_history_echo_after_target_is_not_a_new_turn(self):
        s = service();s.input_message_id = 'target';s.input_user_ids = ['prior', 'target']
        state = ConversationState(s)
        for mid in ('prior', 'target', 'prior', 'target'):
            state.feed({'type': 'input_message', 'input_message': {'id': mid, 'author': {'role': 'user'}}})
        self.assertTrue(state.user_seen)
        with self.assertRaises(UpstreamError):
            state.feed({'type': 'input_message', 'input_message': {'id': 'foreign', 'author': {'role': 'user'}}})

    async def test_first_terminal_or_nonempty_event_is_not_lost(self):
        for status in ('in_progress','finished_successfully'):
            s=service();items=[{'message':message(status=status)}]
            if status=='in_progress':items.append({'message':message()})
            items.append('[DONE]')
            result=await complete(s,*items)
            self.assertEqual(result['choices'][0]['message']['content'],'{"ok":true}')

    async def test_analysis_and_native_tool_outputs_are_never_public(self):
        s=service()
        result=await complete(s,{'message':message('a','SECRET_SYNTHETIC','analysis')},
            {'message':message('tool','TOOL_SYNTHETIC',channel=None,role='tool')},
            {'message':message('code','CODE_SYNTHETIC',channel=None,ctype='code',recipient='python')},
            {'message':message()},'[DONE]')
        self.assertEqual(result['choices'][0]['message']['content'],'{"ok":true}')

    async def test_each_message_has_independent_text_offset(self):
        s=service()
        result=await complete(s,{'message':message('a','x'*1000,channel='analysis')},
            {'message':message(status='in_progress',text='{"ok":')}, {'message':message()},'[DONE]')
        self.assertEqual(result['choices'][0]['message']['content'],'{"ok":true}')

    async def test_thoughts_only_done_is_typed_error_not_permission_denied(self):
        s=service()
        with self.assertRaises(UpstreamError) as error:
            await complete(s,{'type':'resume_conversation_token','token':'DO_NOT_EXPOSE','conversation_id':'private'},
                {'message':message('thought','SYNTHETIC',channel=None,ctype='thoughts',status='in_progress',end=False)},
                {'type':'message_stream_complete'},'[DONE]')
        self.assertEqual(error.exception.status_code,502)
        self.assertEqual(error.exception.detail['error']['code'],'upstream_no_final_response')
        self.assertNotIn('DO_NOT_EXPOSE',json.dumps(error.exception.detail))

    async def test_upstream_error_is_not_swallowed_or_echoed(self):
        with self.assertRaises(UpstreamError) as error:
            await complete(service(),{'error':{'message':'SECRET_INPUT','code':'bad_upstream'}},'[DONE]')
        self.assertEqual(error.exception.detail['error']['code'],'upstream_error_event')
        self.assertNotIn('SECRET_INPUT',json.dumps(error.exception.detail))

    async def test_in_progress_text_and_done_does_not_mean_completed_final(self):
        with self.assertRaises(UpstreamError):
            await complete(service(),{'message':message(status='in_progress',end=False)},'[DONE]')

    async def test_preamble_then_reasoning_is_not_final(self):
        with self.assertRaises(UpstreamError):
            await complete(service(),{'message':message('pre','I will work',channel=None)},
                {'message':message('thought','private',channel=None,ctype='thoughts',status='in_progress',end=False)},'[DONE]')

    async def test_text_marked_reasoning_is_not_visible(self):
        with self.assertRaises(UpstreamError):
            await complete(service(),{'message':message('hidden','private',channel=None,reasoning_status='is_reasoning')},'[DONE]')

    async def test_v1_root_append_shorthand_and_compound_final(self):
        m=message(text='',status='in_progress',end=False)
        result=await complete(service(),'"v1"',{'p':'','o':'add','v':{'message':m}},
            {'p':'/message/content/parts/0','o':'append','v':'{"ok":'}, {'v':'true}'},
            {'p':'','o':'patch','v':[{'p':'/message/status','o':'replace','v':'finished_successfully'},
                                   {'p':'/message/end_turn','o':'replace','v':True}]},'[DONE]')
        self.assertEqual(result['choices'][0]['message']['content'],'{"ok":true}')

    async def test_multiline_sse_and_structured_text_parts(self):
        m=message();m['content']['parts']=[{'type':'text','text':'{"ok":true}'}]
        raw=('event: delta\ndata: '+json.dumps({'message':m})+'\n\ndata: [DONE]\n\n').encode()
        result=await complete(service(),raw)
        self.assertEqual(result['choices'][0]['message']['content'],'{"ok":true}')

    async def test_bad_patch_fails_closed(self):
        with self.assertRaises(UpstreamError):
            await complete(service(),{'p':'/message/content/parts/1048577','o':'append','v':'text'},'[DONE]')

    async def test_partial_final_keeps_text_and_length(self):
        s=service()
        result=await complete(s,{'message':message(text='partial',status='finished_partial_completion',end=None,finish_details={'type':'max_tokens'})},'[DONE]')
        self.assertTrue(s.upstream_output_limited)
        self.assertEqual(result['choices'][0]['message']['content'],'partial')
        self.assertEqual(result['choices'][0]['finish_reason'],'length')

    def test_readback_requires_exact_user_ancestry_and_current_branch(self):
        state=ConversationState(service())
        payload={'current_node':'f','mapping':{'u':{'parent':None,'message':{'id':'u','author':{'role':'user'}}},
            'f':{'parent':'u','message':message('f')}}}
        self.assertFalse(state.readback(payload,'wrong'))
        self.assertTrue(state.readback(payload,'u'))
        other=copy.deepcopy(payload);other['mapping']['newuser']={'parent':'u','message':{'id':'newuser','author':{'role':'user'}}};other['mapping']['f']['parent']='newuser'
        self.assertFalse(ConversationState(service()).readback(other,'u'))
        cyclic=copy.deepcopy(payload);cyclic['mapping']['f']['parent']='f'
        self.assertFalse(ConversationState(service()).readback(cyclic,'u'))

    def test_readback_rejects_preamble_followed_by_thoughts(self):
        state=ConversationState(service())
        payload={'current_node':'t','mapping':{'u':{'parent':None},'pre':{'parent':'u','message':message('pre',channel=None)},
            't':{'parent':'pre','message':message('t',channel=None,ctype='thoughts',end=False,status='in_progress')}}}
        self.assertFalse(state.readback(payload,'u'))

    async def test_same_conversation_snapshot_recovery_does_not_generate(self):
        s=ChatService.__new__(ChatService);s.req_model='gpt-6-thinking';s.strict_model=True;s.upstream_model=None;s.upstream_thinking_effort=None;s.thinking_effort='extended';s.upstream_output_limited=False
        s.input_message_id='u';s.lifecycle_budget=LifecycleBudget(10,max_reads=2);s.base_url='https://chatgpt.com/backend-api';s.base_headers={}
        state=ConversationState(s);state.conversation_id='c'
        payload={'current_node':'f','mapping':{'u':{'parent':None},'f':{'parent':'u','message':message('f')}}}
        response=SimpleNamespace(status_code=200,json=lambda:payload)
        s.s=SimpleNamespace(get=AsyncMock(return_value=response))
        await s.recover_web_conversation(state)
        self.assertTrue(state.recovered);self.assertEqual(state.poll_reads,1);self.assertEqual(s.lifecycle_budget.generations,0)

    async def test_snapshot_read_budget_prevents_unbounded_polling(self):
        s=ChatService.__new__(ChatService);s.input_message_id='u';s.lifecycle_budget=LifecycleBudget(10,max_reads=2);s.base_url='https://chatgpt.com/backend-api';s.base_headers={}
        state=ConversationState(s);state.conversation_id='c';state.poll_interval=0
        s.s=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(status_code=404)))
        await s.recover_web_conversation(state)
        self.assertEqual(s.s.get.await_count,2)
        self.assertEqual(s.lifecycle_budget.reads,2)

    async def test_http_errors_and_unexpected_json_do_not_echo_private_body(self):
        for status in (403,200):
            s=ChatService.__new__(ChatService);s.base_url='https://chatgpt.com/backend-api';s.data={};s.chat_headers={};s.chat_request={};s.lifecycle_budget=LifecycleBudget(10)
            response=SimpleNamespace(status_code=status,headers={'Content-Type':'application/json'},text='SYNTHETIC_SECRET')
            s.s=SimpleNamespace(post_stream=AsyncMock(return_value=response))
            with self.assertRaises(UpstreamError) as error:await s.send_conversation()
            self.assertNotIn('SYNTHETIC_SECRET',json.dumps(error.exception.detail))
            expected='upstream_http_403' if status==403 else 'unexpected_upstream_content_type'
            self.assertEqual(error.exception.detail['error']['code'],expected)

    def test_generation_budget_is_independent_and_finite(self):
        budget=LifecycleBudget(30,max_generations=2,max_reads=1)
        budget.generation();budget.generation()
        with self.assertRaises(UpstreamError):budget.generation()
        self.assertTrue(budget.read());self.assertFalse(budget.read())

class BufferedProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_preserves_typed_error_and_makes_no_extra_call(self):
        from utils.retry import async_retry
        error=UpstreamError(502,{'error':{'code':'upstream_no_final_response'}})
        call=AsyncMock(side_effect=error)
        with self.assertRaises(UpstreamError) as caught:await async_retry(call,max_retries=0)
        self.assertIs(caught.exception,error);call.assert_awaited_once()

    async def test_prepare_pins_request_without_exporting_routing_token(self):
        s=ChatService.__new__(ChatService);s.base_url='https://chatgpt.com/backend-api';s.chat_headers={};s.lifecycle_budget=LifecycleBudget(10)
        s.chat_request={'model':'gpt-6-thinking','thinking_effort':'extended','parent_message_id':'parent','history_and_training_disabled':True,'messages':['NOT_PREPARE']}
        s.s=SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=200,json=lambda:{'conduit_token':'PRIVATE_ROUTING_TOKEN'})))
        await s.prepare_buffered_conversation()
        args=s.s.post.call_args
        self.assertTrue(args.args[0].endswith('/f/conversation/prepare'))
        self.assertEqual(args.kwargs['json']['model'],'gpt-6-thinking');self.assertEqual(args.kwargs['json']['thinking_effort'],'extended')
        self.assertNotIn('messages',args.kwargs['json']);self.assertEqual(s.chat_headers['x-conduit-token'],'PRIVATE_ROUTING_TOKEN')
        self.assertEqual(s.lifecycle_budget.generations,0)

    async def test_websocket_handoff_filters_topics_and_handles_catchup(self):
        from curl_cffi.const import CurlWsFlag
        s=ChatService.__new__(ChatService);s.base_url='https://chatgpt.com/backend-api';s.host_url='https://chatgpt.com';s.base_headers={};s.lifecycle_budget=LifecycleBudget(10)
        st=ConversationState(service());st.conversation_id='c'
        st.feed({'type':'stream_handoff','conversation_id':'c','options':[{'type':'subscribe_ws_topic','topic_id':'topic'}]})
        def msg(topic,text):return {'type':'message','topic_id':topic,'payload':{'type':'conversation-turn-stream','payload':{'type':'stream-item','encoded_item':text}}}
        def data(m):return 'data: '+json.dumps({'message':m,'conversation_id':'c'})+'\n\n'
        frames=[([{'type':'reply','reply':{'type':'connect'}}],CurlWsFlag.TEXT),
                ([msg('wrong',data(message(text='UNTRUSTED'))),{'type':'reply','reply':{'type':'subscribe','topic_id':'topic','recovered':True,'catchups':[
                    msg('topic',data(message('hidden','SECRET',channel='analysis'))),msg('topic',data(message()))]}}],CurlWsFlag.TEXT)]
        socket=SimpleNamespace(asend=AsyncMock(),aclose=AsyncMock())
        s.s=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(status_code=200,json=lambda:{'websocket_url':'wss://ws.chatgpt.com/ws?verify=PRIVATE'})),
            session=SimpleNamespace(ws_connect=AsyncMock(return_value=socket)),recv_websocket=AsyncMock(side_effect=[(json.dumps(a).encode(),b) for a,b in frames]))
        await s.resume_web_handoff(st)
        self.assertEqual(st.websocket_status,'final_received');self.assertEqual(st.candidate()[0]['content']['parts'],['{"ok":true}'])
        self.assertEqual(socket.asend.await_count,2);socket.aclose.assert_awaited_once()
        self.assertNotIn('PRIVATE',json.dumps(st.summary()));self.assertEqual(s.lifecycle_budget.generations,0)

    def test_topic_recovery_is_offered_by_authenticated_resume_token_without_handoff(self):
        import base64
        claims=base64.urlsafe_b64encode(json.dumps({'turn_topic_id':'conversation-turn-current'}).encode()).decode().rstrip('=')
        state=ConversationState(service())
        state.feed({'type':'resume_conversation_token','kind':'topic','conversation_id':'c','token':'header.'+claims+'.signature'})
        self.assertEqual(state.topic_id,'conversation-turn-current');self.assertFalse(state.handoff)
        self.assertNotIn('signature',json.dumps(state.summary()));self.assertNotIn('conversation-turn-current',json.dumps(state.summary()))
        for token in ('bad','header.invalid.signature','header.W10.signature'):
            other=ConversationState(service());other.feed({'type':'resume_conversation_token','kind':'topic','token':token})
            self.assertIsNone(other.topic_id)

    def test_sse_cursor_counts_transport_events_not_done_or_nested_items(self):
        state=ConversationState(service());state.feed('v1');state.feed({'type':'resume_conversation_token','token':'private'})
        state.feed([{'message':message()}, {'type':'message_stream_complete'}]);state.feed('[DONE]')
        self.assertEqual(state.transport_offset,3)
        copy_state=state.checkpoint();copy_state.feed({'message':message(text='changed')})
        self.assertEqual(state.candidate()[0]['content']['parts'],['{"ok":true}'])

    async def test_native_sse_resume_uses_cursor_and_token_header_without_generation(self):
        s=ChatService.__new__(ChatService);s.base_url='https://chatgpt.com/backend-api';s.base_headers={};s.lifecycle_budget=LifecycleBudget(10)
        st=ConversationState(service());st.conversation_id='c';st.resume_token='PRIVATE';st.transport_offset=25
        response=SimpleNamespace(status_code=200,headers={'Content-Type':'text/event-stream'},aiter_lines=lambda:lines({'message':message()},'[DONE]'),aclose=AsyncMock())
        s.s=SimpleNamespace(post_stream=AsyncMock(return_value=response))
        await s.resume_web_sse(st)
        call=s.s.post_stream.call_args
        self.assertTrue(call.args[0].endswith('/f/conversation/resume'));self.assertEqual(call.kwargs['json'],{'conversation_id':'c','offset':25})
        self.assertEqual(call.kwargs['headers']['x-conduit-token'],'PRIVATE');self.assertNotIn('PRIVATE',json.dumps(call.kwargs['json']))
        self.assertIsNotNone(st.candidate());self.assertEqual(s.lifecycle_budget.generations,0);self.assertEqual(s.lifecycle_budget.read_types,{'sse_resume':1})
        response.aclose.assert_awaited_once()

    async def test_ws_catchup_cannot_advance_native_sse_cursor_or_duplicate_text(self):
        from chatgpt.webStream import text_parts
        s=service();s.input_message_id=None
        expected={}
        async def ws(state):
            # Replays the bootstrap as real WS catchup does, then a new partial.
            state.feed({'type':'resume_conversation_token','token':'private','conversation_id':'c'})
            state.feed({'message':message('partial',text='A',status='in_progress',end=False)})
            state.feed({'p':'/message/content/parts/0','o':'append','v':'B'}) if state.current else None
            state.websocket_frames=2
        async def resume(state):
            expected['offset']=state.transport_offset
            self.assertEqual(text_parts(state.messages['partial']['content']),'A')
            state.feed({'message':message('partial',text='AB')})
        s.resume_web_handoff=ws;s.resume_web_sse=resume
        result=await complete(s,{'type':'resume_conversation_token','token':'private','conversation_id':'c'},
            {'type':'stream_handoff','conversation_id':'c','options':[{'type':'subscribe_ws_topic','topic_id':'topic'}]},
            {'message':message('partial',text='A',status='in_progress',end=False)},'[DONE]')
        self.assertEqual(expected['offset'],3);self.assertEqual(result['choices'][0]['message']['content'],'AB')
        self.assertEqual(result['lifecycle']['websocket_frames'],2);self.assertGreater(result['lifecycle']['websocket_replayed_events'],0)

    async def test_resume_404_does_not_retry_or_start_a_generation(self):
        s=ChatService.__new__(ChatService);s.base_url='https://chatgpt.com/backend-api';s.base_headers={};s.lifecycle_budget=LifecycleBudget(10)
        st=ConversationState(service());st.conversation_id='c';st.resume_token='private'
        s.s=SimpleNamespace(post_stream=AsyncMock(return_value=SimpleNamespace(status_code=404)))
        await s.resume_web_sse(st)
        s.s.post_stream.assert_awaited_once();self.assertEqual(s.lifecycle_budget.generations,0)
        self.assertEqual(st.resume_statuses,{'http_404':1})

    async def test_relative_compound_patches_inherit_outer_path_and_operation(self):
        result=await complete(service(),'"v1"',{'p':'','o':'add','v':{'message':message(text='A',status='in_progress',end=False)}},
            {'p':'/message','o':'patch','v':[{'p':'/content/parts/0','o':'append','v':'B'}]},
            {'v':[{'p':'/status','o':'replace','v':'finished_successfully'},{'p':'/end_turn','o':'replace','v':True}]},'[DONE]')
        self.assertEqual(result['choices'][0]['message']['content'],'AB')

    async def test_final_empty_parts_can_be_initialized_by_append_at_next_index(self):
        initial=message(text='',status='in_progress',end=False);initial['content']['parts']=[]
        result=await complete(service(),'"v1"',{'p':'','o':'add','v':{'message':initial}},
            {'p':'/message/content/parts/0','o':'append','v':'{"ok":true}'},
            {'p':'/message','o':'append','v':{'status':'finished_successfully','end_turn':True}},'[DONE]')
        self.assertEqual(result['choices'][0]['message']['content'],'{"ok":true}')

    def test_truncate_uses_utf16_code_units_and_repairs_split_surrogate_append(self):
        from chatgpt.webStream import text_parts
        self.assertEqual(ConversationState.apply_patch('A😀BC','','truncate',3),'A😀')
        half=ConversationState.apply_patch('A😀BC','','truncate',2)
        self.assertEqual(half,'A\ud83d')
        joined=ConversationState.apply_patch(half,'','append','\ude00')
        self.assertEqual(text_parts({'parts':[joined]}),'A😀')

    def test_sparse_replace_and_array_truncate_growth_preserve_holes(self):
        from chatgpt.webStream import UNDEFINED
        result=ConversationState.apply_patch([],'/3','replace','X')
        self.assertEqual(result,[UNDEFINED,UNDEFINED,UNDEFINED,'X'])
        self.assertEqual(ConversationState.apply_patch([1],'','truncate',3),[1,UNDEFINED,UNDEFINED])

    def test_unexpected_nonstring_append_is_rejected_without_replacing_text(self):
        for value in (None,True,1,{},[]):
            with self.assertRaises(UpstreamError):ConversationState.apply_patch('visible','','append',value)

    def test_root_remove_is_undefined_and_retires_previous_final(self):
        from chatgpt.webStream import UNDEFINED
        state=ConversationState(service());state.patch({'p':'','o':'add','v':{'message':message()}})
        self.assertIsNotNone(state.candidate());state.patch({'p':'','o':'remove'})
        self.assertIs(state.current,UNDEFINED);self.assertIsNone(state.candidate());self.assertIs(state.checkpoint().current,UNDEFINED)

    def test_delta_array_allocation_is_bounded_across_operations(self):
        budget=[3]
        self.assertEqual(len(ConversationState.apply_patch([],'/2','replace','x',budget)),3)
        with self.assertRaises(UpstreamError):ConversationState.apply_patch([], '/0','replace','x',budget)
        with self.assertRaises(UpstreamError):ConversationState.apply_patch([], '', 'truncate',1000001)

    async def test_independent_patch_channels_object_append_and_truncate(self):
        result=await complete(service(),'"v1"',{'c':0,'p':'','o':'add','v':{'message':message(text='ABdiscard',status='in_progress',end=False)}},
            {'c':1,'p':'','o':'add','v':{'message':message('private','SECRET',channel='analysis')}},
            {'c':0,'p':'/message/content/parts/0','o':'truncate','v':2},
            {'p':'/message','o':'append','v':{'status':'finished_successfully','end_turn':True}},'[DONE]')
        self.assertEqual(result['choices'][0]['message']['content'],'AB')

    def test_invalid_patch_diagnostic_reports_shape_without_value_or_identifier(self):
        state=ConversationState(service())
        with self.assertRaises(UpstreamError) as error:state.patch({'p':'/message/content/parts/1048577','o':'append','v':'SECRET'})
        report=error.exception.detail['error']['patch'];self.assertEqual(report['operation'],'append');self.assertEqual(report['value_size'],6)
        self.assertEqual(report['path_shape'],'/message/content/parts/#');self.assertNotIn('SECRET',json.dumps(report));self.assertNotIn('999',json.dumps(report))

    def test_transport_diagnostic_never_exports_signed_url_or_exception_text(self):
        from curl_cffi import CurlError
        from chatgpt.webStream import safe_transport_error
        error=CurlError('Refused WebSockets upgrade: 403. URL wss://ws.chatgpt.com/?verify=PRIVATE_SECRET',22)
        report=safe_transport_error(error)
        self.assertEqual(report,{'exception_class':'CurlError','code':22,'http_status':403})
        self.assertNotIn('PRIVATE_SECRET',json.dumps(report));self.assertNotIn('ws.chatgpt.com',json.dumps(report))
        self.assertEqual(safe_transport_error(ConnectionRefusedError(61,'PRIVATE_SECRET'))['errno'],61)

    async def test_websocket_handshake_error_is_safe_and_stage_specific(self):
        from curl_cffi import CurlError
        s=ChatService.__new__(ChatService);s.base_url='https://chatgpt.com/backend-api';s.host_url='https://chatgpt.com';s.base_headers={};s.lifecycle_budget=LifecycleBudget(10)
        st=ConversationState(service());st.topic_id='topic'
        s.s=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(status_code=200,json=lambda:{'websocket_url':'wss://ws.chatgpt.com/?verify=PRIVATE_SECRET'})),
            session=SimpleNamespace(ws_connect=AsyncMock(side_effect=CurlError('HTTP code 403, PRIVATE_SECRET',22))))
        await s.resume_web_handoff(st)
        self.assertEqual(st.websocket_stage,'handshake');self.assertEqual(st.websocket_error['http_status'],403)
        self.assertNotIn('PRIVATE_SECRET',json.dumps(st.summary()))

    async def test_websocket_rejects_other_host_without_leaking_url(self):
        s=ChatService.__new__(ChatService);s.base_url='https://chatgpt.com/backend-api';s.base_headers={};s.lifecycle_budget=LifecycleBudget(10)
        st=ConversationState(service());st.topic_id='topic'
        s.s=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(status_code=200,json=lambda:{'websocket_url':'wss://evil.example/?verify=PRIVATE'})))
        with self.assertRaises(UpstreamError) as error:await s.resume_web_handoff(st)
        self.assertNotIn('PRIVATE',json.dumps(error.exception.detail))

if __name__ == '__main__':unittest.main(verbosity=2)
