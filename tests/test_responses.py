"""Responses wire, state, ownership, grammars, and native-event regressions."""
import asyncio
import contextlib
import copy
import io
import json
import logging
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
logging.disable(logging.CRITICAL)
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    from app import app
    import api.responses as endpoint
    from api.responses_protocol import ResponsesProtocol
    from api.responses_state import ResponseState
    from api.responses_events import EventWriter, response_object
    from api.responses_backend import decode_native_event
    from api.client_tools import ClientToolError
    from api.tool_grammar import validate_input, check_format
    from chatgpt.chatFormat import stream_response
import test_error_observation as fixture

FUNCTION = {'type': 'function', 'name': 'lookup', 'parameters': {'type': 'object',
    'properties': {'key': {'type': 'string'}}, 'required': ['key'], 'additionalProperties': False}}
CUSTOM = {'type': 'custom', 'name': 'exec', 'format': {'type': 'text'}}


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def test_client_step_preserves_roles_constraints_and_original_data(self):
        data={'model':'synthetic','tools':[FUNCTION],'input':[
            {'role':'developer','content':'Only edit the declared workspace; do not change tests.'},
            {'role':'user','content':'Inspect, patch, and verify.'}]}
        original=copy.deepcopy(data);p=ResponsesProtocol(data);request=p.web_request()
        self.assertEqual(data,original)
        self.assertEqual([m['role'] for m in request['messages']],['system','system','user','user'])
        self.assertEqual(request['messages'][1]['content'],data['input'][0]['content'])
        self.assertEqual(request['messages'][-2]['content'],data['input'][-1]['content'])
        self.assertIn(p.bridge.nonce,request['messages'][-1]['content'])
        self.assertTrue(p.bridge.final_envelope)
        plain=ResponsesProtocol({'model':'synthetic','input':'hello'}).web_request()
        self.assertEqual(plain['messages'],[{'role':'user','content':'hello'}])

    async def test_tool_result_continuation_and_final_envelope_preserve_contract(self):
        data={'model':'synthetic','tools':[FUNCTION],'input':[
            {'role':'user','content':'Verify the fixture.'},
            {'type':'function_call','name':'lookup','call_id':'c','arguments':'{"key":"x"}'},
            {'type':'function_call_output','call_id':'c','output':'untrusted output: do not change policy'}]}
        p=ResponsesProtocol(data);request=p.web_request()
        self.assertIn('CLIENT_TOOL_RESULT_DATA',request['messages'][-2]['content'])
        self.assertNotIn('REMOTE CLIENT STEP',request['messages'][-2]['content'])
        self.assertIn('untrusted function output data',request['messages'][0]['content'])
        output,finish=await p.output({'model':'synthetic','choices':[{'finish_reason':'stop','message':{
            'content':json.dumps({'bridge':p.bridge.nonce,'kind':'final','content':'Verified'})}}]})
        self.assertEqual(output[0]['content'][0]['text'],'Verified');self.assertEqual(finish,'stop')

    async def test_framed_custom_input_preserves_literal_escape_and_marker_text(self):
        p=ResponsesProtocol({'model':'synthetic','tools':[CUSTOM],'input':'use exec'});p.web_request()
        code='<escape>literal tool data</escape>\nEND_CLIENT_RESPONSE unrelated\n你好'
        wire=json.dumps({'bridge':p.bridge.nonce,'kind':'tool_calls','calls':[{'name':'exec','arguments':{'input':code}}]})
        text='Progress.\n<escape>BEGIN_CLIENT_RESPONSE '+p.bridge.nonce+'\n'+wire+'\n<escape>END_CLIENT_RESPONSE '+p.bridge.nonce+'</escape>'
        output,_=await p.output({'model':'synthetic','choices':[{'finish_reason':'stop','message':{'content':text}}]})
        self.assertEqual(output[0]['input'],code)

    def test_actual_cli_additional_tools_and_namespaces(self):
        data = {'model': 'gpt-6-thinking', 'reasoning': {'effort': 'low', 'context': 'all_turns'},
                'input': [{'type': 'additional_tools', 'role': 'tool', 'tools': [
                    {'type': 'namespace', 'name': 'functions', 'tools': [FUNCTION, CUSTOM]}]},
                    {'role': 'developer', 'content': [{'type': 'input_text', 'text': 'constraints'}]},
                    {'role': 'user', 'content': 'hello'}]}
        p = ResponsesProtocol(data);request = p.web_request()
        self.assertEqual(set(p.registry), {'functions__lookup', 'functions__exec'})
        self.assertEqual(request['reasoning_effort'], 'low')
        self.assertFalse(request['stream']);self.assertTrue(request['history_disabled'])
        self.assertNotIn('additional_tools', [m.get('type') for m in request['messages']])

    async def test_custom_input_round_trip_preserves_exact_patch(self):
        p = ResponsesProtocol({'model': 'synthetic', 'tools': [CUSTOM], 'input': 'use exec'})
        p.web_request();code = 'const x = "引号\\n";\ntext(x);\n'
        completion = {'model': 'synthetic', 'choices': [{'finish_reason': 'stop', 'message': {'content':
            json.dumps({'bridge': p.bridge.nonce, 'kind': 'tool_calls', 'calls': [{'name': 'exec', 'arguments': {'input': code}}]})}}]}
        output, finish = await p.output(completion)
        self.assertEqual(output[0]['type'], 'custom_tool_call');self.assertEqual(output[0]['input'], code)
        replay = ResponsesProtocol({'model': 'synthetic', 'tools': [CUSTOM], 'input': [
            {'role': 'user', 'content': 'go'}, output[0],
            {'type': 'custom_tool_call_output', 'call_id': output[0]['call_id'], 'output': 'done'}]})
        replay.web_request();self.assertEqual(replay.messages[-1]['tool_call_id'], output[0]['call_id'])

    async def test_function_namespace_preserved_in_output(self):
        p = ResponsesProtocol({'model': 'synthetic', 'tools': [{'type': 'namespace', 'name': 'mcp', 'tools': [FUNCTION]}], 'input': 'go'})
        p.web_request();raw = {'bridge': p.bridge.nonce, 'kind': 'tool_calls', 'calls': [{'name': 'mcp__lookup', 'arguments': {'key': 'x'}}]}
        output, _ = await p.output({'model': 'synthetic', 'choices': [{'message': {'content': json.dumps(raw)}, 'finish_reason': 'stop'}]})
        self.assertEqual((output[0]['namespace'], output[0]['name']), ('mcp', 'lookup'))

    def test_parallel_out_of_order_results_keep_ids(self):
        calls = [{'type': 'function_call', 'call_id': 'c' + str(i), 'name': 'lookup', 'arguments': '{"key":"x"}'} for i in range(2)]
        p = ResponsesProtocol({'model': 'synthetic', 'tools': [FUNCTION], 'input': [{'role': 'user', 'content': 'go'}, *calls,
             {'type': 'function_call_output', 'call_id': 'c1', 'output': 'one'},
             {'type': 'function_call_output', 'call_id': 'c0', 'output': 'zero'}]})
        p.web_request();self.assertEqual([m['tool_call_id'] for m in p.messages if m['role'] == 'tool'], ['c1', 'c0'])

    def test_missing_duplicate_and_foreign_results_are_rejected(self):
        cases = [
          [{'role': 'user', 'content': 'go'}, {'type': 'function_call_output', 'call_id': 'foreign', 'output': 'x'}],
          [{'role': 'user', 'content': 'go'}, {'type': 'function_call', 'call_id': 'c', 'name': 'lookup', 'arguments': '{}'}]]
        for items in cases:
            with self.assertRaises(ClientToolError):ResponsesProtocol({'model': 'x', 'tools': [FUNCTION], 'input': items}).web_request()

    def test_native_reasoning_is_not_fabricated_or_treated_as_final(self):
        with self.assertRaises(ClientToolError):ResponsesProtocol({'input': [{'type': 'reasoning', 'encrypted_content': 'PRIVATE'}]})

    def test_unknown_tools_and_unsupported_parts_fail_before_generation(self):
        for tool in ({'type': 'web_search'}, {'type': 'custom', 'name': 'x', 'format': {'type': 'grammar', 'syntax': 'python', 'definition': 'x'}}):
            with self.assertRaises(ClientToolError):ResponsesProtocol({'input': 'x', 'tools': [tool]})
        with self.assertRaises(ClientToolError):ResponsesProtocol({'input': [{'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'https://invalid.test'}]}]})

    async def test_regex_and_actual_cli_lark_grammar(self):
        await validate_input({'type': 'grammar', 'syntax': 'regex', 'definition': r'OK[0-9]+'}, 'OK42')
        grammar = 'start: SOURCE\nSOURCE: /[\\s\\S]+/\n'
        await validate_input({'type': 'grammar', 'syntax': 'lark', 'definition': grammar}, 'text("你好");\n')
        with self.assertRaises(ClientToolError):await validate_input({'type': 'grammar', 'syntax': 'regex', 'definition': r'OK[0-9]+'}, 'bad')

    async def test_codex_patch_nullable_line_grammar(self):
        fmt = {'type': 'grammar', 'syntax': 'lark', 'definition':
            'start: "*** Begin Patch" LF ("+" /(.*)/ LF)+ "*** End Patch" LF?\n%import common.LF\n'}
        await validate_input(fmt, '', compile_only=True)
        await validate_input(fmt, '*** Begin Patch\n+\n+你好\n*** End Patch\n')
        with self.assertRaises(ClientToolError):
            await validate_input(fmt, '*** Begin Patch\nINVALID\n*** End Patch\n')
        await validate_input({'type': 'grammar', 'syntax': 'lark',
                              'definition': 'start: "/(.*)/"\n'}, '/(.*)/')

    def test_invalid_nested_namespace_and_format_are_client_errors(self):
        with self.assertRaises(ClientToolError):
            ResponsesProtocol({'input': 'x', 'tools': [{'type': 'namespace', 'name': 'a', 'tools': [
                {'type': 'namespace', 'name': 'b', 'tools': [FUNCTION]}]}]})
        with self.assertRaises(ClientToolError):
            ResponsesProtocol({'input': 'x', 'text': {'format': 'invalid'}}).web_request()


class StateTests(unittest.TestCase):
    def setUp(self):self.s = ResponseState(secret='synthetic-state-key')

    def test_store_false_memory_replay_but_no_retrieval_or_disk(self):
        r = response_object('x');r.update(status='completed', output=[{'role': 'assistant', 'content': 'one'}])
        self.s.save('owner', {'input': 'question', 'store': False}, r)
        self.assertEqual(self.s.expand('owner', {'previous_response_id': r['id'], 'input': 'two'})['input'][-1]['content'], 'two')
        with self.assertRaises(ClientToolError):self.s.get('owner', r['id'], retrieval=True)
        with self.assertRaises(ClientToolError):self.s.get('other', r['id'])

    def test_encrypted_persistent_checkpoint_permissions_and_restore(self):
        with tempfile.TemporaryDirectory() as d:
            s = ResponseState(secret='synthetic-state-key', directory=d)
            r = response_object('x');r.update(status='completed', output=[])
            s.save('owner', {'input': 'PRIVATE_BODY', 'store': True}, r)
            p = next(Path(d).glob('*.json'));self.assertNotIn('PRIVATE_BODY', p.read_text());self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            restored = ResponseState(secret='synthetic-state-key', directory=d)
            self.assertEqual(restored.get('owner', r['id'], retrieval=True)['request']['input'], 'PRIVATE_BODY')
            restored.delete('owner', r['id']);self.assertFalse(p.exists())

    def test_compaction_authenticated_and_owner_scoped(self):
        value = self.s.seal({'input': [{'role': 'user', 'content': 'PRIVATE'}]}, 'a')
        self.assertNotIn('PRIVATE', value)
        self.assertEqual(self.s.expand('a', {'input': [{'type': 'compaction', 'encrypted_content': value}]})['input'][0]['content'], 'PRIVATE')
        for token, owner in ((value, 'b'), (value[:-4] + 'abcd', 'a'), ('foreign', 'a')):
            with self.assertRaises(ClientToolError):self.s.open(token, owner)

    def test_idempotency_no_replay_pending_unknown_or_changed_payload(self):
        data = {'input': 'go'};self.assertIsNone(self.s.claim('a', 'k', data))
        for value in (data, {'input': 'different'}):
            with self.assertRaises(ClientToolError):self.s.claim('a', 'k', value)
        self.s.finish('a', 'k')
        with self.assertRaises(ClientToolError):self.s.claim('a', 'k', data)
        r = response_object('x');self.s.finish('a', 'k', r)
        self.assertEqual(self.s.claim('a', 'k', data)['id'], r['id'])
        self.assertIsNone(self.s.claim('b', 'k', data))

    def test_expired_or_evicted_parent_does_not_trigger_generation(self):
        s = ResponseState(ttl=-1);r = response_object('x');s.save('a', {'input': []}, r)
        with self.assertRaises(ClientToolError):s.expand('a', {'input': [], 'previous_response_id': r['id']})


class NativeCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_v2_compaction_preserves_opaque_state_and_closes_stream(self):
        import api.responses_backend as backend
        from chatgpt.webStream import LifecycleBudget
        seen = [];closed = []
        item = {'type': 'compaction', 'encrypted_content': 'synthetic-opaque-native-state'}
        async def stream(data, budget):
            try:
                seen.append(data)
                yield {'type': 'response.completed', 'response': {'id': 'resp_native', 'output': [item]}}
                await asyncio.Event().wait()
            finally:closed.append(True)
        with patch.dict('os.environ', {'RESPONSES_COMPACTION_MODE': 'responses'}), patch.object(backend, 'native_stream', stream):
            value = await backend.native_compact({'model': 'synthetic', 'input': [{'role': 'user', 'content': 'one'}]}, LifecycleBudget(1))
        self.assertEqual(value['output'], [item]);self.assertTrue(closed)
        self.assertEqual(len(seen), 1);self.assertEqual(seen[0]['input'][-1], {'type': 'compaction_trigger'})

    async def test_invalid_v2_compaction_does_not_fall_back_to_another_generation(self):
        import api.responses_backend as backend
        from chatgpt.webStream import LifecycleBudget
        calls = []
        async def stream(*args):
            calls.append(1)
            yield {'type': 'response.completed', 'response': {'id': 'bad', 'output': [{'type': 'message'}]}}
        with patch.dict('os.environ', {'RESPONSES_COMPACTION_MODE': 'responses'}), patch.object(backend, 'native_stream', stream):
            with self.assertRaises(ClientToolError):
                await backend.native_compact({'model': 'synthetic', 'input': []}, LifecycleBudget(1))
        self.assertEqual(calls, [1])


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_framed_tool_output_is_never_streamed_as_plain_text(self):
        from chatgpt.webStream import LifecycleBudget
        payload={}
        async def configure(value):payload.update(value)
        async def send():
            protocol=json.loads(payload['messages'][0]['content'].rsplit('\n',1)[-1]);nonce=protocol['bridge']
            text='Progress before client call.\nBEGIN_CLIENT_RESPONSE '+nonce+'\n'+json.dumps({
                'bridge':nonce,'kind':'tool_calls','calls':[{'name':'lookup','arguments':{'key':'x'}}]})+'\nEND_CLIENT_RESPONSE '+nonce
            async def frames():
                for part in (text[:8],text[8:]):
                    yield 'data: '+json.dumps({'choices':[{'delta':{'content':part}}]})+'\n\n'
                yield 'data: '+json.dumps({'choices':[{'delta':{},'finish_reason':'stop'}]})+'\n\n'
            return frames()
        service=types.SimpleNamespace(prompt_tokens=1,upstream_model='synthetic',
            set_dynamic_data=configure,get_chat_requirements=AsyncMock(),prepare_send_conversation=AsyncMock(),
            send_conversation=send,close_client=AsyncMock())
        result=response_object('synthetic');frames=[]
        with patch.object(endpoint,'ChatService',return_value=service):
            async for frame in endpoint.web_events({'model':'synthetic','tools':[FUNCTION],'input':'lookup'},
                types.SimpleNamespace(credentials='synthetic'),LifecycleBudget(1),result,EventWriter()):frames.append(frame)
        self.assertFalse(service.responses_live)
        self.assertFalse(any('response.output_text.delta' in f for f in frames))
        self.assertEqual(result['output'][0]['type'],'function_call')
        self.assertEqual(result['status'],'completed')

    async def test_heartbeat_closes_its_producer_on_disconnect(self):
        from api.responses_transport import heartbeat
        closed = asyncio.Event()
        async def waiting():
            try:
                yield ': start\n\n'
                await asyncio.Event().wait()
            finally:closed.set()
        stream = heartbeat(waiting(), interval=.01)
        await stream.__anext__();await stream.aclose()
        await asyncio.wait_for(closed.wait(), 1)

    async def test_terminal_is_committed_before_client_closes_stream(self):
        from types import SimpleNamespace
        from chatgpt.webStream import LifecycleBudget
        state = ResponseState(secret='synthetic')
        final = response_object('gpt-6.1-terra');final['status'] = 'completed'
        closed = asyncio.Event()
        async def native(*args):
            try:
                yield {'type': 'response.completed', 'response': final}
                await asyncio.Event().wait()
            finally:closed.set()
        req = SimpleNamespace(state=SimpleNamespace(lifecycle_budget=LifecycleBudget(1)))
        credentials = SimpleNamespace(credentials='synthetic')
        with patch.object(endpoint, 'STATE', state), patch.object(endpoint, 'backend_mode', return_value='native'), patch.object(endpoint, 'native_stream', native):
            stream = endpoint.events({'model': 'gpt-6.1-terra', 'input': []}, credentials, req, 'owner', None)
            await stream.__anext__()  # comment
            frame = await stream.__anext__()
            self.assertIn('response.completed', frame)
            self.assertEqual(req.state.gateway_protocol_terminal, 'completed')
            self.assertEqual(state.get('owner', final['id'])['response']['status'], 'completed')
            await stream.aclose();self.assertTrue(closed.is_set())

    async def test_completed_protocol_disconnect_is_not_499(self):
        from chatgpt.requestScope import GatewayRequestScope
        from chatgpt.webStream import LifecycleBudget
        from test_request_scope import drive
        disconnect = asyncio.Event();rows = []
        async def app(scope, receive, send):
            await receive()
            scope['state']['gateway_protocol_terminal'] = 'completed'
            await send({'type': 'http.response.start', 'status': 200, 'headers': [(b'content-type', b'text/event-stream')]})
            await send({'type': 'http.response.body', 'body': b'event: response.completed\n\n', 'more_body': True})
            disconnect.set();await asyncio.Event().wait()
        wrapper = GatewayRequestScope(app, rows.append, lambda _: None, lambda: LifecycleBudget(1))
        await drive(wrapper, disconnect=disconnect)
        self.assertEqual(rows[0]['status'], 200);self.assertEqual(rows[0]['outcome'], 'protocol_completed')
        self.assertTrue(rows[0]['local_task_done'])

    async def test_failed_sse_protocol_is_audited_as_failure_even_with_http_200(self):
        from chatgpt.requestScope import GatewayRequestScope
        from chatgpt.webStream import LifecycleBudget
        from test_request_scope import drive
        rows = []
        async def application(scope, receive, send):
            await receive();scope['state']['gateway_protocol_terminal'] = 'failed'
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'failed', 'more_body': False})
        await drive(GatewayRequestScope(application, rows.append, lambda _: None, lambda: LifecycleBudget(1)))
        self.assertEqual(rows[0]['status'], 502);self.assertEqual(rows[0]['response_status_sent'], 200)

    async def test_sse_failure_after_http_body_disconnect_keeps_failure_audit(self):
        from chatgpt.requestScope import GatewayRequestScope
        from chatgpt.webStream import LifecycleBudget
        from test_request_scope import drive
        rows=[];disconnect=asyncio.Event()
        async def application(scope,receive,send):
            await receive();scope['state']['gateway_protocol_terminal']='failed'
            await send({'type':'http.response.start','status':200,'headers':[]})
            await send({'type':'http.response.body','body':b'failed','more_body':False})
            disconnect.set();await asyncio.sleep(.02)
        await drive(GatewayRequestScope(application,rows.append,lambda _:None,lambda:LifecycleBudget(1)),disconnect=disconnect)
        self.assertEqual(rows[0]['status'],502);self.assertEqual(rows[0]['outcome'],'protocol_failed')

    def test_logical_failure_keeps_safe_exception_metadata_with_http_200(self):
        from chatgpt.errorObservation import RequestObservation
        value=RequestObservation();value.set_stage('chat_requirements');value.capture(TimeoutError('PRIVATE_URL'))
        trace=value.trace({'response_status':'failed'},200)
        self.assertEqual(trace['failure']['stage'],'chat_requirements')
        self.assertEqual(trace['failure']['exception_class'],'TimeoutError')
        self.assertNotIn('PRIVATE_URL',json.dumps(trace))

    async def frames(self, texts):
        service = types.SimpleNamespace(req_model='synthetic', strict_model=False, upstream_model=None,
             upstream_thinking_effort=None, thinking_effort=None, history_disabled=True, input_message_id=None,
             responses_live=True, upstream_output_limited=False)
        marks = {}
        async def upstream():
            for i, text in enumerate(texts):
                if i:await asyncio.sleep(.02)
                done = i == len(texts) - 1
                if done:marks['final_at'] = time.monotonic()
                yield ('data: ' + json.dumps({'message': {'id': 'm', 'author': {'role': 'assistant'}, 'channel': 'final',
                     'content': {'content_type': 'text', 'parts': [text]}, 'status': 'finished_successfully' if done else 'in_progress', 'end_turn': done}}) + '\n\n').encode()
            yield b'data: [DONE]'
        result = []
        async for frame in stream_response(service, upstream(), 'synthetic', 1024):
            result.append(frame)
            if '"content": "A"' in frame:marks['first_text_at'] = time.monotonic()
        return result, marks

    async def test_real_incremental_text_precedes_upstream_final(self):
        frames, marks = await self.frames(['A', 'AB'])
        self.assertLess(marks['first_text_at'], marks['final_at'])
        self.assertEqual(frames[-1].strip(), 'data: [DONE]')

    async def test_rewrite_after_committed_text_is_explicit_failure(self):
        frames, _ = await self.frames(['A', 'wrong'])
        self.assertTrue(any('upstream_text_rewritten' in x for x in frames))
        self.assertFalse(any(x.strip() == 'data: [DONE]' for x in frames))

    def test_native_error_redaction_and_terminal_events(self):
        e = decode_native_event(json.dumps({'type': 'response.failed', 'response': {'id': 'r', 'error': {'code': 'usage_limit', 'message': 'PRIVATE'}}}))
        self.assertEqual(e['response']['error']['code'], 'usage_limit');self.assertNotIn('PRIVATE', json.dumps(e))
        writer = EventWriter();r = response_object('x');r['status'] = 'incomplete'
        events = [json.loads(x.split('\ndata: ')[1]) for x in writer.replay(r)]
        self.assertEqual(events[-1]['type'], 'response.incomplete')
        self.assertEqual([e['sequence_number'] for e in events], list(range(len(events))))


class RouteTests(unittest.IsolatedAsyncioTestCase):
    def test_web_catalog_uses_actual_models_and_local_caps(self):
        from api.codex_catalog import web_catalog
        data = web_catalog([{'slug': 'gpt-6-thinking', 'title': 'Thinking', 'thinking_efforts': [{'thinking_effort': 'extended'}]},
                            {'slug': 'gpt-6.1-terra-wm', 'is_work_mode_model': True}], '0.166.0')
        self.assertEqual([x['slug'] for x in data['models']], ['gpt-6-thinking'])
        self.assertEqual(data['models'][0]['default_reasoning_level'], 'high')
        self.assertEqual(data['models'][0]['input_modalities'], ['text'])
        self.assertFalse(data['models'][0]['supports_search_tool'])
    async def post(self, data, mock=None, path='/responses', headers=None):
        headers = {'Authorization': 'Bearer synthetic', **(headers or {})}
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(endpoint, 'STATE', ResponseState(secret='test')))
            stack.enter_context(patch.object(endpoint, 'authorization_list', []))
            if mock:stack.enter_context(patch.object(endpoint, 'web_events', mock))
            stack.enter_context(patch('api.chat2api.append_audit'))
            stack.enter_context(patch('api.chat2api.append_activity'))
            async with fixture.LocalASGIClient(types.SimpleNamespace(app=app, raise_errors=False), 'http://fixture') as client:
                return await client.post(endpoint.PREFIX + path, json=data, headers=headers)

    async def test_responses_routes_and_invalid_input(self):
        for value in ({}, {'model': 'x', 'input': 'x', 'stream': 'true'}, {'model': 'x', 'input': 'x', 'background': True}, {'model': 'x', 'input': 'x', 'generate': False}):
            response = await self.post(value);self.assertEqual(response.status_code, 400)
        self.assertIn(endpoint.PREFIX + '/responses/compact', app.openapi()['paths'])

    async def test_complete_function_call_and_terminal_event_via_actual_asgi(self):
        async def fake(data, credentials, budget, response, writer):
            response.update(status='completed', output=[{'id': 'fc', 'type': 'function_call', 'status': 'completed', 'call_id': 'call1', 'name': 'lookup', 'arguments': '{}'}])
            for event in writer.item(response['output'][0], 0):yield event
            yield writer.frame('response.completed', response=response)
        response = await self.post({'model': 'x', 'input': 'go', 'stream': True, 'tools': [FUNCTION]}, fake)
        self.assertEqual(response.status_code, 200)
        self.assertIn('response.function_call_arguments.done', response.text);self.assertIn('response.completed', response.text)
        self.assertNotIn('[DONE]', response.text)

    async def test_midstream_failure_has_no_success_terminal(self):
        async def broken(*args):
            yield ': progress\n\n'
            raise ValueError('PRIVATE')
        response = await self.post({'model': 'x', 'input': 'go', 'stream': True}, broken)
        self.assertIn('response.failed', response.text);self.assertNotIn('response.completed', response.text);self.assertNotIn('PRIVATE', response.text)

    async def test_new_response_route_is_deadline_owned(self):
        async def slow(*args):
            await asyncio.sleep(1)
            yield ': never\n\n'
        import api.chat2api as routes
        with patch.object(routes, 'auto_continue_timeout', .02):
            response = await self.post({'model': 'x', 'input': 'go'}, slow)
        self.assertEqual(response.status_code, 504)

    async def test_compaction_without_generation_preserves_pending_tools(self):
        items = [{'role': 'user', 'content': str(i)} for i in range(5)]
        items += [{'type': 'function_call', 'name': 'lookup', 'call_id': 'pending', 'arguments': '{}'}]
        response = await self.post({'model': 'x', 'input': items}, path='/responses/compact')
        self.assertEqual(response.status_code, 200);self.assertTrue(response.json()['gateway_compaction'])

    async def test_v2_compaction_trigger_produces_one_replayable_opaque_item(self):
        response = await self.post({'model': 'x', 'input': [{'role': 'user', 'content': 'retain me'},
                                                         {'type': 'compaction_trigger'}]})
        self.assertEqual(response.status_code, 200)
        value = response.json();self.assertEqual(value['status'], 'completed')
        self.assertEqual([x['type'] for x in value['output']], ['compaction'])
        self.assertNotIn('retain me', value['output'][0]['encrypted_content'])

    async def test_compaction_does_not_split_overlapping_call_result_pairs(self):
        items = [{'role': 'user', 'content': str(i)} for i in range(20)]
        # Initial split=8 cuts c2; moving to 6 then cuts c1 (whose call
        # was already visited). The boundary must converge to 2.
        items[2] = {'type': 'function_call', 'name': 'lookup', 'call_id': 'c1', 'arguments': '{}'}
        items[6] = {'type': 'function_call', 'name': 'lookup', 'call_id': 'c2', 'arguments': '{}'}
        items[7] = {'type': 'function_call_output', 'call_id': 'c1', 'output': 'one'}
        items[10] = {'type': 'function_call_output', 'call_id': 'c2', 'output': 'two'}
        async def summary(data, credentials, budget, response, writer):
            self.assertNotIn('function_call', data['input'][0]['content'])
            response.update(status='completed', output=[{'type': 'message', 'content': [{'type': 'output_text', 'text': 'summary'}]}])
            yield writer.frame('response.completed', response=response)
        with patch.object(endpoint, 'web_events', summary):
            response = await self.post({'model': 'x', 'input': items}, path='/responses/compact')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['output'][1:], items[2:])


if __name__ == '__main__':
    unittest.main(verbosity=2)
