"""No network: real ASGI route/middleware and service error wrappers, fake transports."""
import asyncio,contextlib,io,json,logging,types,unittest,uuid
from unittest.mock import AsyncMock,Mock,patch
logging.disable(logging.CRITICAL)
with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
    from app import app
    import api.chat2api as endpoint
    from chatgpt.ChatService import ChatService
    from chatgpt.webStream import ConversationState,LifecycleBudget
    from chatgpt import errorObservation as obs
from curl_cffi import CurlError
from fastapi import HTTPException
# Minimal ASGI client keeps the production image/dependencies unchanged.
class LocalASGIClient:
    def __init__(self,transport,base_url):self.app=transport.app;self.raise_errors=transport.raise_errors
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    async def post(self,path,json,headers):
        body=__import__('json').dumps(json).encode();sent=False;finished=asyncio.Event();status=None;chunks=[]
        async def receive():
            nonlocal sent
            if not sent:sent=True;return {'type':'http.request','body':body,'more_body':False}
            await finished.wait();return {'type':'http.disconnect'}
        async def send(message):
            nonlocal status
            if message['type']=='http.response.start':status=message['status']
            elif message['type']=='http.response.body':
                chunks.append(message.get('body',b''))
                if not message.get('more_body',False):finished.set()
        scope={'type':'http','asgi':{'version':'3.0','spec_version':'2.3'},'http_version':'1.1','method':'POST','scheme':'http','path':path,'raw_path':path.encode(),'query_string':b'','root_path':'','headers':[(k.lower().encode(),v.encode()) for k,v in {**headers,'Content-Type':'application/json'}.items()],'client':('127.0.0.1',12345),'server':('fixture',80)}
        try:await self.app(scope,receive,send)
        except Exception:
            if self.raise_errors or status is None:raise
        raw=b''.join(chunks).decode()
        return types.SimpleNamespace(status_code=status,text=raw,json=lambda:__import__('json').loads(raw))
httpx=types.SimpleNamespace(AsyncClient=LocalASGIClient,ASGITransport=lambda app,raise_app_exceptions=True:types.SimpleNamespace(app=app,raise_errors=raise_app_exceptions))
PRIVATE='PRIVATE_SIGNED_URL_TOKEN_AND_MODEL_BODY'

def completed():return {'id':'c','created':1,'model':'gpt-6-thinking','thinking_effort':'extended','choices':[{'message':{'role':'assistant','content':'{"ok":true}'},'finish_reason':'stop'}],'usage':{'prompt_tokens':1}}
def service():
    return types.SimpleNamespace(impersonate='chrome123',req_token='synthetic-account',user_agent='fixture',base_url='https://invalid.test',base_headers={},data={'stream':False},web_protocol='legacy',chat_headers={},chat_request={},
        req_model='gpt-6-thinking',resp_model='gpt-6-thinking',strict_model=True,upstream_model=None,upstream_thinking_effort=None,thinking_effort='extended',upstream_output_limited=False,
        set_dynamic_data=AsyncMock(),get_chat_requirements=AsyncMock(),prepare_send_conversation=AsyncMock(),send_conversation=AsyncMock(return_value=completed()),close_client=AsyncMock(),lifecycle_state=None)
def real_requirements(s,reply):
    s.ss=types.SimpleNamespace(post=AsyncMock(side_effect=reply if isinstance(reply,BaseException) else None,return_value=reply))
    s.get_chat_requirements=types.MethodType(ChatService.get_chat_requirements,s)

class ErrorObservationTests(unittest.IsolatedAsyncioTestCase):
    async def post(self,factory,payload=None,timeout=None):
        records=[];cid=str(uuid.uuid4());payload=payload or {'messages':[{'role':'user','content':'fixture'}],'auto_continue':False}
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(endpoint,'ChatService',side_effect=factory))
            stack.enter_context(patch.object(endpoint,'append_audit',side_effect=records.append))
            stack.enter_context(patch('chatgpt.ChatService.get_config',return_value={}))
            stack.enter_context(patch('chatgpt.ChatService.get_requirements_token',return_value='synthetic'))
            if timeout is not None:stack.enter_context(patch.object(endpoint,'auto_continue_timeout',timeout))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False),base_url='http://fixture') as client:
                response=await client.post(('/'+endpoint.api_prefix if endpoint.api_prefix else '')+'/v1/chat/completions',json=payload,headers={'Authorization':'Bearer synthetic','X-Client-Request-Id':cid})
        self.assertEqual(len(records),1);self.assertEqual(records[0]['client_request_id'],cid)
        self.assertNotIn(PRIVATE,json.dumps(records));self.assertNotIn(PRIVATE,response.text)
        self.assertIsNone(obs._current.get())
        return response,records[0]

    async def test_responses_requirements_tls35_retries_only_preflight_once(self):
        s=service();s.responses_request=True;s.lifecycle_budget=LifecycleBudget(5)
        reply=types.SimpleNamespace(status_code=200,json=lambda:{'persona':'chatgpt-paid','token':'synthetic'})
        real_requirements(s,reply);s.ss.post.side_effect=[CurlError(PRIVATE,35),reply]
        token=await s.get_chat_requirements()
        self.assertEqual(token,'synthetic');self.assertEqual(s.ss.post.await_count,2)
        self.assertEqual(s.chat_requirements_connect_retries,1)
        self.assertTrue(all(call.args[0].endswith('/sentinel/chat-requirements') for call in s.ss.post.await_args_list))
        self.assertEqual(s.lifecycle_budget.generations,0);s.send_conversation.assert_not_awaited()
        observation=obs.RequestObservation();observation.service=s
        self.assertEqual(observation.trace({},200)['preflight_connect_retries'],1)

    async def test_requirements_retry_excludes_legacy_other_errors_and_cancellation(self):
        for responses,error in ((False,CurlError(PRIVATE,35)),(True,CurlError(PRIVATE,28)),
                                (True,asyncio.CancelledError())):
            with self.subTest(responses=responses,error=type(error).__name__):
                s=service();s.responses_request=responses;s.lifecycle_budget=LifecycleBudget(5)
                real_requirements(s,error)
                expected=asyncio.CancelledError if isinstance(error,asyncio.CancelledError) else HTTPException
                with self.assertRaises(expected):await s.get_chat_requirements()
                self.assertEqual(s.ss.post.await_count,1);self.assertEqual(s.chat_requirements_connect_retries,0)
                s.send_conversation.assert_not_awaited()

    async def test_requirements_second_tls_failure_and_expired_budget_stop(self):
        for expired in (False,True):
            s=service();s.responses_request=True;s.lifecycle_budget=LifecycleBudget(5)
            real_requirements(s,CurlError(PRIVATE,35))
            if expired:s.lifecycle_budget.remaining=Mock(side_effect=[1,0])
            with self.assertRaises(HTTPException):await s.get_chat_requirements()
            self.assertEqual(s.ss.post.await_count,1 if expired else 2)
            self.assertEqual(s.lifecycle_budget.generations,0);s.send_conversation.assert_not_awaited()

    async def test_requirements_http_auth_quota_and_server_errors_are_not_retried(self):
        for status in (401,403,429,500):
            s=service();s.responses_request=True;s.lifecycle_budget=LifecycleBudget(5)
            real_requirements(s,types.SimpleNamespace(status_code=status,headers={},text='synthetic failure'))
            with self.assertRaises(HTTPException):await s.get_chat_requirements()
            self.assertEqual(s.ss.post.await_count,1);self.assertEqual(s.chat_requirements_connect_retries,0)
            s.send_conversation.assert_not_awaited()

    async def test_requirements_curl28_preserved_before_two_http500_wrappers(self):
        s=service();real_requirements(s,CurlError(PRIVATE,28))
        response,row=await self.post(lambda _:s)
        self.assertEqual(response.status_code,500);self.assertEqual(response.json(),{'detail':'Server error'})
        self.assertEqual(row['trace']['failure'],{'stage':'chat_requirements','exception_class':'CurlError','code':28})
        self.assertEqual(row['trace']['budget']['generation_posts'],0);s.close_client.assert_awaited_once()

    async def test_requirements_http500_has_actual_upstream_status(self):
        s=service();real_requirements(s,types.SimpleNamespace(status_code=500,headers={'Content-Type':'application/json'},json=lambda:{'detail':PRIVATE}))
        response,row=await self.post(lambda _:s)
        self.assertEqual(response.status_code,500)
        self.assertEqual(row['trace']['failure'],{'stage':'chat_requirements','exception_class':'HTTPException','upstream_status':500})
        self.assertEqual(row['trace']['budget']['generation_posts'],0)

    async def test_ignored_turnstile_does_not_contaminate_later_failure_stage(self):
        s=service();s.s=types.SimpleNamespace(post=AsyncMock(side_effect=CurlError(PRIVATE,7)))
        reply=types.SimpleNamespace(status_code=200,json=lambda:{'persona':'chatgpt-paid','turnstile':{'required':True},'arkose':'malformed'})
        real_requirements(s,reply)
        with patch('chatgpt.ChatService.turnstile_solver_url','https://invalid.test'):
            response,row=await self.post(lambda _:s)
        self.assertEqual(response.status_code,500)
        self.assertEqual(row['trace']['failure'],{'stage':'chat_requirements','exception_class':'AttributeError','upstream_status':200})

    async def test_challenge_transport_root_error_survives_local403_wrapper(self):
        s=service();s.ark0se_token_url='https://invalid.test'
        reply=types.SimpleNamespace(status_code=200,json=lambda:{'persona':'chatgpt-paid','arkose':{'required':True}})
        real_requirements(s,reply)
        challenge=types.SimpleNamespace(post=AsyncMock(side_effect=CurlError(PRIVATE,28)),close=AsyncMock())
        with patch('chatgpt.ChatService.Client',return_value=challenge):
            response,row=await self.post(lambda _:s)
        self.assertEqual(response.status_code,403)
        self.assertEqual(row['trace']['failure'],{'stage':'challenge','exception_class':'CurlError','code':28})
        challenge.close.assert_awaited_once()

    async def test_model_catalog_wrap_keeps_transport_class_and_numeric_code(self):
        import api.model_catalog as catalog
        s=service();client=types.SimpleNamespace(get=AsyncMock(side_effect=CurlError(PRIVATE,28)),close=AsyncMock())
        async def dynamic(_):await catalog.get_model_catalog('synthetic-uncached-account')
        s.set_dynamic_data=dynamic
        with patch.object(catalog,'verify_token',AsyncMock(return_value='synthetic')),patch.object(catalog,'get_fp',return_value={}),patch.object(catalog,'Client',return_value=client):
            response,row=await self.post(lambda _:s)
        self.assertEqual(response.status_code,502)
        self.assertEqual(row['trace']['failure'],{'stage':'model_catalog','exception_class':'CurlError','code':28})
        self.assertEqual(row['trace']['budget']['generation_posts'],0);client.close.assert_awaited_once()

    async def test_constructor_failure_has_budget_and_stage(self):
        def fail(_):raise RuntimeError(PRIVATE)
        response,row=await self.post(fail)
        self.assertEqual(response.status_code,500)
        self.assertEqual(row['trace']['failure'],{'stage':'service_init','exception_class':'RuntimeError'})
        self.assertEqual(row['trace']['budget']['generation_posts'],0)

    async def test_request_preparation_failure_captured_before_cleanup(self):
        s=service();s.prepare_send_conversation.side_effect=ValueError(PRIVATE)
        response,row=await self.post(lambda _:s)
        self.assertEqual(response.status_code,500)
        self.assertEqual(row['trace']['failure'],{'stage':'request_preparation','exception_class':'ValueError'})
        s.close_client.assert_awaited_once()

    async def test_generation_transport_error_keeps_original_curl_code_and_count(self):
        s=service();s.s=types.SimpleNamespace(post_stream=AsyncMock(side_effect=CurlError(PRIVATE,7)))
        s.send_conversation=types.MethodType(ChatService.send_conversation,s)
        response,row=await self.post(lambda _:s)
        self.assertEqual(response.status_code,502)
        self.assertEqual(row['trace']['failure'],{'stage':'generation_request','exception_class':'CurlError','code':7})
        self.assertEqual(row['trace']['budget']['generation_posts'],1)
        self.assertEqual(response.json()['error']['code'],'upstream_transport_error')

    async def test_buffered_prepare_http500_is_not_mislabelled_generation(self):
        s=service();s.web_protocol='buffered-v1';s.s=types.SimpleNamespace(post=AsyncMock(return_value=types.SimpleNamespace(status_code=500)))
        s.prepare_buffered_conversation=types.MethodType(ChatService.prepare_buffered_conversation,s)
        s.send_conversation=types.MethodType(ChatService.send_conversation,s)
        response,row=await self.post(lambda _:s)
        self.assertEqual(response.status_code,500)
        self.assertEqual(row['trace']['failure'],{'stage':'conversation_preparation','exception_class':'UpstreamError','upstream_status':500})
        self.assertEqual(row['trace']['budget']['generation_posts'],0)

    async def test_outer_deadline_keeps_state_budget_and_timeout_not_inner_cancel(self):
        s=service()
        async def wait():
            obs.stage('websocket_receive');s.lifecycle_budget.generation()
            s.lifecycle_state=ConversationState(s);s.lifecycle_state.websocket_attempts=[{'connection':1,'outcome':'waiting'}]
            await asyncio.Event().wait()
        s.get_chat_requirements=wait
        response,row=await self.post(lambda _:s,{'messages':[{'role':'user','content':'fixture'}],'auto_continue':True},.02)
        self.assertEqual(response.status_code,504)
        self.assertEqual(row['trace']['failure'],{'stage':'websocket_receive','exception_class':'TimeoutError'})
        self.assertEqual(row['trace']['budget']['generation_posts'],1)
        self.assertEqual(row['trace']['lifecycle']['websocket_attempts'],[{'connection':1,'outcome':'waiting'}])
        s.close_client.assert_awaited_once()

    async def test_success_does_not_change_response_or_claim_failure(self):
        s=service();response,row=await self.post(lambda _:s)
        self.assertEqual(response.status_code,200);self.assertNotIn('failure',row['trace'])
        self.assertEqual(response.json()['choices'][0]['message']['content'],'{"ok":true}')

    async def test_escaped_validation_exception_is_audited_once_without_suppression(self):
        records=[];cid=str(uuid.uuid4())
        with patch.object(endpoint,'needs_tool_bridge',side_effect=ValueError(PRIVATE)),patch.object(endpoint,'append_audit',side_effect=records.append):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False),base_url='http://fixture') as client:
                r=await client.post(('/'+endpoint.api_prefix if endpoint.api_prefix else '')+'/v1/chat/completions',json={},headers={'Authorization':'Bearer synthetic','X-Client-Request-Id':cid})
        self.assertEqual(r.status_code,500);self.assertEqual(len(records),1)
        self.assertEqual(records[0]['trace']['failure'],{'stage':'request_validation','exception_class':'ValueError'})
        self.assertEqual(records[0]['trace']['budget']['generation_posts'],0)
        self.assertNotIn(PRIVATE,json.dumps(records));self.assertNotIn(PRIVATE,r.text)

    async def test_contextvar_concurrent_requests_do_not_mix_stage_error_or_budget(self):
        records=[];ids=[str(uuid.uuid4()),str(uuid.uuid4())]
        def factory(token):
            s=service()
            async def fail():
                phase,code=('chat_requirements',28) if token=='first' else ('proof_of_work',7)
                obs.stage(phase)
                if token=='second':s.lifecycle_budget.generation()
                await asyncio.sleep(.005)
                obs.capture(CurlError(PRIVATE,code));raise HTTPException(500,'Server error')
            s.get_chat_requirements=fail;return s
        async def send(client,index):
            return await client.post(('/'+endpoint.api_prefix if endpoint.api_prefix else '')+'/v1/chat/completions',json={'messages':[],'auto_continue':False},headers={'Authorization':'Bearer '+('first' if index==0 else 'second'),'X-Client-Request-Id':ids[index]})
        with patch.object(endpoint,'ChatService',side_effect=factory),patch.object(endpoint,'append_audit',side_effect=records.append):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
                responses=await asyncio.gather(send(client,0),send(client,1))
        self.assertEqual([r.status_code for r in responses],[500,500]);self.assertEqual(len(records),2)
        byid={x['client_request_id']:x['trace'] for x in records}
        self.assertEqual(byid[ids[0]]['failure'],{'stage':'chat_requirements','exception_class':'CurlError','code':28})
        self.assertEqual(byid[ids[1]]['failure'],{'stage':'proof_of_work','exception_class':'CurlError','code':7})
        self.assertEqual(byid[ids[0]]['budget']['generation_posts'],0);self.assertEqual(byid[ids[1]]['budget']['generation_posts'],1)
        self.assertIsNone(obs._current.get());self.assertNotIn(PRIVATE,json.dumps(records))

class SafeMetadataTests(unittest.TestCase):
    def test_never_stringifies_exception_or_exports_custom_type(self):
        class PRIVATE_TYPENAME(Exception):
            code=28;errno=9;response=types.SimpleNamespace(status_code=503)
            def __str__(self):raise AssertionError('must not stringify')
        self.assertEqual(obs.safe_exception(PRIVATE_TYPENAME()),{'exception_class':'OtherError','code':28,'errno':9,'upstream_status':503})

    def test_bad_attributes_are_ignored_without_breaking_original_failure(self):
        class Trouble(Exception):
            @property
            def code(self):raise ValueError(PRIVATE)
            @property
            def response(self):raise ValueError(PRIVATE)
        self.assertEqual(obs.safe_exception(Trouble()),{'exception_class':'OtherError'})

    def test_numeric_fields_are_bounded_and_not_strings_or_booleans(self):
        for value in (True,False,'28',2**40,PRIVATE):
            error=Exception();error.code=value;error.errno=value;error.response=types.SimpleNamespace(status_code=value)
            self.assertEqual(obs.safe_exception(error),{'exception_class':'OtherError'})

    def test_unknown_stage_and_stale_upstream_status_do_not_leak(self):
        o=obs.RequestObservation();o.set_stage('model_catalog');o.upstream_status=200;o.set_stage(PRIVATE);o.capture(ValueError(PRIVATE))
        self.assertEqual(o.failure,{'stage':'other','exception_class':'ValueError'})

    def test_first_root_error_survives_outer_wrapper_and_cleanup_error(self):
        o=obs.RequestObservation();o.set_stage('chat_requirements');o.capture(CurlError(PRIVATE,28));o.capture(HTTPException(500,PRIVATE));o.capture(RuntimeError(PRIVATE))
        self.assertEqual(o.failure,{'stage':'chat_requirements','exception_class':'CurlError','code':28})

    def test_no_context_helpers_do_not_change_behavior(self):
        self.assertIsNone(obs._current.get());obs.stage('initial_sse');obs.capture(ValueError(PRIVATE));obs.upstream_status(500);obs.attach_service(service());obs.attach_budget(LifecycleBudget(1))
        self.assertIsNone(obs._current.get())

    def test_trace_tolerates_partial_decoder_summary_failure(self):
        o=obs.RequestObservation();s=service();s.lifecycle_state=ConversationState(s)
        def bad():raise ValueError(PRIVATE)
        s.lifecycle_state.summary=bad;o.service=s;o.budget=LifecycleBudget(1);o.capture(ValueError(PRIVATE))
        trace=o.trace(None,500);self.assertNotIn('lifecycle',trace);self.assertEqual(trace['budget']['generation_posts'],0);self.assertNotIn(PRIVATE,json.dumps(trace))

if __name__=='__main__':unittest.main(verbosity=2)
