import asyncio, contextlib, copy, io, json, logging, unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
logging.disable(logging.CRITICAL)
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    from app import app
    import api.chat2api as endpoint
    from api.continuation import complete_with_continuation, merge_continuation, continuation_boundary
    from api.client_tools import ClientToolBridge
    from api.tokens import num_tokens_from_content
    from fastapi import HTTPException

DATA={'messages':[{'role':'user','content':'Complete the requested output'}]}

def result(text,finish='stop',model='gpt-6-thinking'):
    return {'id':'chatcmpl-test','object':'chat.completion','created':1,'model':model,'thinking_effort':'extended',
            'choices':[{'index':0,'message':{'role':'assistant','content':text},'finish_reason':finish}],
            'usage':{'prompt_tokens':10,'completion_tokens':1,'total_tokens':11}}

def service(limited=False):
    return SimpleNamespace(req_token='private-test-account',upstream_output_limited=limited,close_client=AsyncMock())

class ContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_completion_closes_without_followup(self):
        s=service();run=AsyncMock(return_value=(s,result('OK')))
        before=copy.deepcopy(DATA)
        out=await complete_with_continuation(DATA,run)
        self.assertEqual(out['choices'][0]['message']['content'],'OK')
        self.assertEqual(out['continuation']['additional_rounds'],0)
        self.assertEqual(DATA,before);s.close_client.assert_awaited_once()

    async def test_partial_line_restarted_without_duplicate_and_account_pinned(self):
        a,b=service(True),service()
        run=AsyncMock(side_effect=[(a,result('first\nsecond par','length')),(b,result('second paragraph\nthird'))])
        out=await complete_with_continuation(DATA,run)
        self.assertEqual(out['choices'][0]['message']['content'],'first\nsecond paragraph\nthird')
        self.assertEqual(out['choices'][0]['finish_reason'],'stop')
        self.assertEqual(out['continuation']['additional_rounds'],1)
        second,pin=run.await_args_list[1].args
        self.assertEqual(pin,'private-test-account')
        self.assertEqual(second['messages'][-2]['content'],'first\n')
        a.close_client.assert_awaited_once();b.close_client.assert_awaited_once()
        self.assertEqual(out['usage']['prompt_tokens'],20)

    async def test_interrupted_escape_wrapper_is_transport_not_output(self):
        run=AsyncMock(side_effect=[(service(True),result('<escape>first\nsecond par','length')),(service(),result('second paragraph</escape>'))])
        out=await complete_with_continuation(DATA,run)
        self.assertEqual(out['choices'][0]['message']['content'],'first\nsecond paragraph')
        self.assertEqual(out['choices'][0]['finish_reason'],'stop')
        # Literal tags inside the body are preserved; arbitrary JSX is never executed.
        run=AsyncMock(return_value=(service(),result('<escape>literal <escape>inner</escape></escape>')))
        out=await complete_with_continuation(DATA,run)
        self.assertEqual(out['choices'][0]['message']['content'],'literal <escape>inner</escape>')

    async def test_wrapped_fragmented_json_still_requires_full_schema_validation(self):
        tool={'type':'function','function':{'name':'lookup','parameters':{'type':'object','properties':{'key':{'const':'sample'}},'required':['key']}}}
        bridge=ClientToolBridge({**DATA,'tools':[tool],'tool_choice':'required'})
        wire=json.dumps({'bridge':bridge.nonce,'kind':'tool_calls','calls':[{'name':'lookup','arguments':{'key':'sample'}}]})
        cut=wire.index('sample')+2
        run=AsyncMock(side_effect=[(service(True),result('<escape>'+wire[:cut],'length')),(service(),result(wire[cut:]))])
        assembled=await complete_with_continuation(bridge.upstream_request(),run)
        out=await bridge.completion(assembled)
        self.assertEqual(out['choices'][0]['finish_reason'],'tool_calls')
        self.assertEqual(len(out['choices'][0]['message']['tool_calls']),1)

    def test_literal_escape_tags_at_fragment_boundaries_are_preserved(self):
        from api.continuation import unwrap_escape
        text,wrapped=unwrap_escape('<escape>{"payload":"literal</escape>',partial=True)
        self.assertEqual(text,'{"payload":"literal</escape>')
        self.assertTrue(wrapped)
        text,wrapped=unwrap_escape('<escape>literal',partial=True,previously_wrapped=True,initial=False)
        self.assertEqual(text,'<escape>literal')
        text,wrapped=unwrap_escape('<escape>literal</escape>',partial=False,initial=False)
        self.assertEqual(text,'<escape>literal</escape>')

    async def test_local_length_does_not_trigger_more_requests(self):
        run=AsyncMock(return_value=(service(False),result('local cut','length')))
        out=await complete_with_continuation(DATA,run)
        self.assertEqual(out['continuation']['stop_reason'],'local_limit')
        run.assert_awaited_once()

    async def test_round_limit_preserves_partial_length(self):
        run=AsyncMock(return_value=(service(True),result('partial','length')))
        out=await complete_with_continuation(DATA,run,max_rounds=0)
        self.assertEqual(out['continuation']['stop_reason'],'round_limit')
        self.assertEqual(out['choices'][0]['finish_reason'],'length')
        run.assert_awaited_once()

    async def test_explicit_small_budget_is_honored_without_followup(self):
        run=AsyncMock(return_value=(service(True),result('word '*20,'length')))
        out=await complete_with_continuation({**DATA,'max_completion_tokens':4},run)
        self.assertEqual(out['usage']['completion_tokens'],4)
        self.assertEqual(out['continuation']['stop_reason'],'total_limit')
        self.assertEqual(run.await_args.args[0]['max_tokens'],4)
        run.assert_awaited_once()

    async def test_no_progress_or_unverified_boundary_preserves_original(self):
        for following in ('second par','unrelated new answer'):
            run=AsyncMock(side_effect=[(service(True),result('first\nsecond par','length')),(service(),result(following))])
            out=await complete_with_continuation(DATA,run)
            self.assertEqual(out['choices'][0]['message']['content'],'first\nsecond par')
            self.assertEqual(out['continuation']['stop_reason'],'unverified_boundary')
            self.assertEqual(out['choices'][0]['finish_reason'],'length')

    async def test_failure_keeps_partial_and_first_failure_still_raises(self):
        s=service(True)
        run=AsyncMock(side_effect=[(s,result('partial','length')),HTTPException(429,'busy')])
        out=await complete_with_continuation(DATA,run)
        self.assertEqual(out['continuation']['stop_reason'],'upstream_error')
        self.assertEqual(out['choices'][0]['message']['content'],'partial')
        s.close_client.assert_awaited_once()
        with self.assertRaises(HTTPException):
            await complete_with_continuation(DATA,AsyncMock(side_effect=HTTPException(429,'busy')))

    async def test_model_changes_do_not_mix_responses(self):
        run=AsyncMock(side_effect=[(service(True),result('partial','length')),(service(),result('other',model='other'))])
        out=await complete_with_continuation(DATA,run)
        self.assertEqual(out['continuation']['stop_reason'],'model_mismatch')
        self.assertEqual(out['choices'][0]['message']['content'],'partial')

    async def test_external_conversation_context_is_not_replayed(self):
        run=AsyncMock(return_value=(service(True),result('partial','length')))
        out=await complete_with_continuation({**DATA,'conversation_id':'existing'},run)
        self.assertEqual(out['continuation']['stop_reason'],'external_conversation_context')
        run.assert_awaited_once()

    def test_exact_overlap_and_code_whitespace_preserved(self):
        prefix='a'*100
        self.assertEqual(merge_continuation(prefix,prefix[-80:]+'tail',prefix,None),prefix+'tail')
        text='def f():\n    return 12'
        before,tail=continuation_boundary(text)
        self.assertEqual(merge_continuation(text,'    return 123\n',before,tail),'def f():\n    return 123\n')

    def test_full_replay_and_exact_repeated_boundary_are_merged(self):
        self.assertEqual(merge_continuation('prefix','prefix suffix','prefix',None),'prefix suffix')
        old='first\n'+'long line '*20+'\npartial'
        before,tail=continuation_boundary(old)
        following='long line '*20+'\npartial rest'
        self.assertEqual(merge_continuation(old,following,before,tail),old+' rest')
        self.assertEqual(merge_continuation('first\npartial','\npartial rest','first\n','partial'),'first\npartial rest')

    async def test_fragmented_tool_json_is_validated_only_after_full_assembly(self):
        tool={'type':'function','function':{'name':'lookup','parameters':{'type':'object','properties':{'key':{'const':'sample'}},'required':['key']}}}
        bridge=ClientToolBridge({**DATA,'tools':[tool],'tool_choice':'required'})
        wire=json.dumps({'bridge':bridge.nonce,'kind':'tool_calls','calls':[{'name':'lookup','arguments':{'key':'sample'}}]})
        cut=wire.index('sample')+2
        run=AsyncMock(side_effect=[(service(True),result(wire[:cut],'length')),(service(),result(wire[cut:]))])
        assembled=await complete_with_continuation(bridge.upstream_request(),run)
        out=await bridge.completion(assembled)
        self.assertEqual(out['choices'][0]['finish_reason'],'tool_calls')
        self.assertEqual(len(out['choices'][0]['message']['tool_calls']),1)
        self.assertEqual(json.loads(out['choices'][0]['message']['tool_calls'][0]['function']['arguments']),{'key':'sample'})
        # Exhausted retries must never expose a truncated call to the client.
        with self.assertRaises(HTTPException):
            await bridge.completion(await complete_with_continuation(bridge.upstream_request(),AsyncMock(return_value=(service(True),result(wire[:cut],'length'))),max_rounds=0))

    async def test_deadline_cancels_inflight_and_returns_504(self):
        cancelled=asyncio.Event()
        async def run(*args):
            try:await asyncio.sleep(10)
            finally:cancelled.set()
        with self.assertRaises(HTTPException) as error:
            await complete_with_continuation(DATA,run,timeout=.01)
        self.assertEqual(error.exception.status_code,504)
        self.assertTrue(cancelled.is_set())

    async def test_invalid_limits_fail_before_upstream(self):
        for value in (0,-1,True,'65536'):
            run=AsyncMock()
            with self.assertRaises(HTTPException):await complete_with_continuation({**DATA,'max_tokens':value},run)
            run.assert_not_called()

    async def test_route_default_auto_continues_and_sse_reassembles(self):
        for stream in (False,True):
            a,b=service(True),service()
            fake=SimpleNamespace(state=SimpleNamespace(),json=AsyncMock(return_value={**DATA,'stream':stream,'stream_options':{'include_usage':True}}))
            run=AsyncMock(side_effect=[(a,result('line one\nline t','length')),(b,result('line two'))])
            with patch.object(endpoint,'process',run),patch.object(endpoint,'auto_continue',True):
                response=await endpoint.send_conversation(fake,SimpleNamespace(credentials='local-key'))
            if not stream:
                out=json.loads(response.body);content=out['choices'][0]['message']['content']
            else:
                frames=[frame async for frame in response.body_iterator]
                self.assertTrue(frames[-1].startswith('data: [DONE]'))
                events=[json.loads(f[6:]) for f in frames if f.startswith('data: {')]
                content=''.join(e['choices'][0]['delta'].get('content','') for e in events if e['choices'])
                self.assertEqual(events[0]['continuation']['additional_rounds'],1)
            self.assertEqual(content,'line one\nline two')
            a.close_client.assert_awaited_once();b.close_client.assert_awaited_once()

    async def test_route_tool_visible_budget_is_capped_before_wire_envelope(self):
        fake=SimpleNamespace(state=SimpleNamespace(),json=AsyncMock(return_value={**DATA,'tools':[],'max_tokens':131072}))
        run=AsyncMock(return_value=(service(),result('word '*30)))
        with patch.object(endpoint,'process',run),patch.object(endpoint,'auto_continue',True),patch.object(endpoint,'auto_continue_max_tokens',8):
            response=await endpoint.send_conversation(fake,SimpleNamespace(credentials='local-key'))
        self.assertEqual(json.loads(response.body)['usage']['completion_tokens'],8)
        self.assertEqual(run.await_args.args[0]['max_tokens'],8)

    async def test_process_cancellation_closes_service_in_setup_and_generation(self):
        async def slow(*args):await asyncio.sleep(10)
        for stage in ('setup','generation'):
            fake=SimpleNamespace(req_token='account',set_dynamic_data=AsyncMock(side_effect=slow if stage=='setup' else None),
                get_chat_requirements=AsyncMock(),prepare_send_conversation=AsyncMock(),
                send_conversation=AsyncMock(side_effect=slow),close_client=AsyncMock())
            with patch.object(endpoint,'ChatService',return_value=fake):
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(endpoint.process(DATA,'local-key'),.01)
            fake.close_client.assert_awaited_once()

    async def test_route_opt_out_and_invalid_controls(self):
        s=service(True)
        fake=SimpleNamespace(state=SimpleNamespace(),json=AsyncMock(return_value={**DATA,'auto_continue':False}))
        with patch.object(endpoint,'process',AsyncMock(return_value=(s,result('partial','length')))) as run:
            response=await endpoint.send_conversation(fake,SimpleNamespace(credentials='local-key'))
            self.assertNotIn('continuation',json.loads(response.body))
            run.assert_awaited_once()
            await response.background()
        s.close_client.assert_awaited_once()
        for control in ({'auto_continue':'true'},{'stream_options':{'include_usage':'yes'}}):
            fake.json=AsyncMock(return_value={**DATA,**control})
            with self.assertRaises(HTTPException):await endpoint.send_conversation(fake,SimpleNamespace(credentials='local-key'))

if __name__ == '__main__':unittest.main(verbosity=2)
