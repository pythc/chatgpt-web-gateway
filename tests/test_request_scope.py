import asyncio,json,sys,time,types,unittest,uuid
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import test_error_observation as f
from chatgpt.requestScope import GatewayRequestScope,ACTIVE_REQUESTS,CLEANUP_TASKS
import chatgpt.requestScope as scope_module
from chatgpt.webStream import LifecycleBudget
from chatgpt import errorObservation as obs
import anyio

async def drive(app,cid=None,token='fixture',disconnect=None,payload=None,body_delay=0,disconnect_after_body=False):
    cid=cid or str(uuid.uuid4());disconnect=disconnect or asyncio.Event();messages=[];sent=False
    raw=json.dumps(payload or {'messages':[{'role':'user','content':f.PRIVATE}],'auto_continue':False}).encode()
    async def receive():
        nonlocal sent
        if not sent:
            sent=True
            if body_delay:await asyncio.sleep(body_delay)
            return {'type':'http.request','body':raw,'more_body':False}
        await disconnect.wait();return {'type':'http.disconnect'}
    async def send(message):
        messages.append(message)
        if disconnect_after_body and message['type']=='http.response.body' and not message.get('more_body'):disconnect.set()
    path=('/'+f.endpoint.api_prefix if f.endpoint.api_prefix else '')+'/v1/chat/completions'
    request={'type':'http','asgi':{'version':'3.0','spec_version':'2.3'},'http_version':'1.1','method':'POST','scheme':'http','path':path,'raw_path':path.encode(),'query_string':b'','root_path':'','headers':[(b'authorization',('Bearer '+token).encode()),(b'content-type',b'application/json'),(b'x-client-request-id',cid.encode())],'client':('127.0.0.1',12345),'server':('fixture',80)}
    await app(request,receive,send);return messages

class RequestScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_mode_has_hard_deadline_and_one_terminal_audit(self):
        s=f.service();s.get_chat_requirements=asyncio.Event().wait;events=[]
        with patch.object(f.endpoint,'append_activity',side_effect=events.append):
            response,row=await f.ErrorObservationTests().post(lambda _:s,timeout=.02)
        self.assertEqual(response.status_code,504);self.assertEqual(row['outcome'],'deadline_exceeded')
        self.assertTrue(row['local_task_done']);self.assertEqual(row['trace']['budget']['generation_posts'],0)
        self.assertEqual(row['trace']['failure']['exception_class'],'TimeoutError');s.close_client.assert_awaited_once()
        self.assertEqual(sum(x['event']=='started' for x in events),1);self.assertEqual(sum(x['event']=='finished' for x in events),1)
        self.assertNotIn(f.PRIVATE,json.dumps(events));self.assertFalse(ACTIVE_REQUESTS);self.assertFalse(CLEANUP_TASKS)

    async def test_client_disconnect_cancels_only_owned_work_and_audits499(self):
        s=f.service();started=asyncio.Event();disconnect=asyncio.Event();audits=[];events=[]
        async def wait():started.set();await asyncio.Event().wait()
        s.get_chat_requirements=wait
        with patch.object(f.endpoint,'ChatService',return_value=s),patch.object(f.endpoint,'append_audit',side_effect=audits.append),patch.object(f.endpoint,'append_activity',side_effect=events.append):
            work=asyncio.create_task(drive(f.app,disconnect=disconnect))
            await asyncio.wait_for(started.wait(),1);disconnect.set();await asyncio.wait_for(work,1)
        self.assertEqual(len(audits),1);self.assertEqual(audits[0]['status'],499);self.assertEqual(audits[0]['outcome'],'client_disconnected')
        self.assertTrue(audits[0]['local_task_done']);self.assertIsNone(audits[0]['remote_generation_stopped'])
        s.close_client.assert_awaited_once();self.assertFalse(ACTIVE_REQUESTS);self.assertFalse(CLEANUP_TASKS)
        self.assertNotIn(f.PRIVATE,json.dumps(events+audits))

    async def test_disconnect_does_not_cancel_another_request(self):
        first=f.service();second=f.service();started=[asyncio.Event(),asyncio.Event()];release=asyncio.Event();disconnect=asyncio.Event();audits=[]
        async def wait_first():started[0].set();await asyncio.Event().wait()
        async def wait_second():started[1].set();await release.wait()
        first.get_chat_requirements=wait_first;second.get_chat_requirements=wait_second
        with patch.object(f.endpoint,'ChatService',side_effect=lambda token:first if token=='first' else second),patch.object(f.endpoint,'append_audit',side_effect=audits.append),patch.object(f.endpoint,'append_activity'):
            one=asyncio.create_task(drive(f.app,token='first',disconnect=disconnect));two=asyncio.create_task(drive(f.app,token='second'))
            await asyncio.wait_for(asyncio.gather(*(x.wait() for x in started)),1)
            disconnect.set();await asyncio.wait_for(one,1)
            self.assertFalse(two.done());second.close_client.assert_not_awaited()
            release.set();await asyncio.wait_for(two,1)
        self.assertEqual(sorted(x['status'] for x in audits),[200,499]);self.assertFalse(ACTIVE_REQUESTS)

    async def test_pending_cleanup_does_not_publish_a_false_terminal(self):
        release=asyncio.Event();audits=[];events=[];started=asyncio.Event()
        async def stubborn(scope,receive,send):
            await receive();started.set()
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:
                with anyio.CancelScope(shield=True):await release.wait()
                raise
        app=GatewayRequestScope(stubborn,audits.append,events.append,lambda:LifecycleBudget(.01))
        with patch.object(scope_module,'CLEANUP_GRACE_SECONDS',.01):
            await asyncio.wait_for(drive(app),1)
            self.assertEqual(len(audits),0);self.assertEqual(len(CLEANUP_TASKS),1)
            self.assertTrue(any(x['event']=='cleanup_pending' for x in events))
            release.set()
            for _ in range(100):
                if audits:break
                await asyncio.sleep(.001)
        self.assertEqual(len(audits),1);self.assertEqual(audits[0]['status'],504);self.assertTrue(audits[0]['local_task_done'])
        self.assertFalse(ACTIVE_REQUESTS);self.assertFalse(CLEANUP_TASKS)

    async def test_late_worker_cannot_replace_timeout_or_emit_late_content(self):
        audits=[];events=[];release=asyncio.Event()
        async def late(scope,receive,send):
            await receive()
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:
                with anyio.CancelScope(shield=True):await release.wait()
            await send({'type':'http.response.start','status':200,'headers':[]})
            await send({'type':'http.response.body','body':f.PRIVATE.encode()})
        app=GatewayRequestScope(late,audits.append,events.append,lambda:LifecycleBudget(.01))
        with patch.object(scope_module,'CLEANUP_GRACE_SECONDS',.01):
            messages=await drive(app);self.assertFalse(audits);release.set()
            for _ in range(100):
                if audits:break
                await asyncio.sleep(.001)
        self.assertEqual([x['status'] for x in messages if x['type']=='http.response.start'],[504])
        self.assertNotIn(f.PRIVATE.encode(),b''.join(x.get('body',b'') for x in messages))
        self.assertEqual(audits[0]['status'],504);self.assertEqual(audits[0]['response_status_sent'],504)
        self.assertFalse(CLEANUP_TASKS)

    async def test_stream_body_is_guarded_after_headers(self):
        audits=[];events=[]
        async def stream(scope,receive,send):
            await receive();await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'text/event-stream')]});await asyncio.Event().wait()
        app=GatewayRequestScope(stream,audits.append,events.append,lambda:LifecycleBudget(.01))
        messages=await asyncio.wait_for(drive(app),1)
        self.assertEqual(audits[0]['status'],504);self.assertEqual(audits[0]['response_status_sent'],200)
        body=b''.join(x.get('body',b'') for x in messages)
        self.assertIn(b'gateway_request_deadline',body);self.assertNotIn(b'[DONE]',body)
        self.assertTrue(audits[0]['local_task_done'])

    async def test_disconnect_after_final_body_keeps_success(self):
        audits=[];events=[];cancelled=False
        async def normal(scope,receive,send):
            nonlocal cancelled
            await receive();await send({'type':'http.response.start','status':200,'headers':[]});await send({'type':'http.response.body','body':b'{}','more_body':False})
            try:await asyncio.sleep(.005)
            except asyncio.CancelledError:cancelled=True;raise
        app=GatewayRequestScope(normal,audits.append,events.append,lambda:LifecycleBudget(1))
        await drive(app,disconnect_after_body=True)
        self.assertFalse(cancelled);self.assertEqual(audits[0]['status'],200);self.assertEqual(audits[0]['outcome'],'response')

    async def test_journal_bounds_stages_and_excludes_request_body(self):
        audits=[];events=[]
        async def normal(scope,receive,send):
            await receive()
            for _ in range(300):obs.stage('initial_sse')
            await send({'type':'http.response.start','status':200,'headers':[]});await send({'type':'http.response.body','body':b'{}'})
        app=GatewayRequestScope(normal,audits.append,events.append,lambda:LifecycleBudget(1))
        await drive(app);self.assertEqual(sum(x['event']=='stage' for x in events),256)
        self.assertEqual(sum(x['event']=='stages_truncated' for x in events),1)
        self.assertNotIn(f.PRIVATE,json.dumps(events+audits))

    async def test_body_time_is_in_shared_total_budget(self):
        s=f.service();s.get_chat_requirements=asyncio.Event().wait;audits=[];events=[];started=time.monotonic()
        with patch.object(f.endpoint,'ChatService',return_value=s),patch.object(f.endpoint,'append_audit',side_effect=audits.append),patch.object(f.endpoint,'append_activity',side_effect=events.append),patch.object(f.endpoint,'auto_continue_timeout',.03):
            await asyncio.wait_for(drive(f.app,body_delay=.02),1)
        self.assertLess(time.monotonic()-started,.15);self.assertEqual(audits[0]['status'],504)
        self.assertEqual(audits[0]['trace']['budget']['generation_posts'],0)
        self.assertIn('started_at',audits[0]);self.assertIn('finished_at',audits[0])

    async def test_worker_timeout_is_not_mislabelled_as_total_budget_expiry(self):
        audits=[];events=[]
        async def failure(scope,receive,send):await receive();raise TimeoutError(f.PRIVATE)
        app=GatewayRequestScope(failure,audits.append,events.append,lambda:LifecycleBudget(1))
        await drive(app)
        self.assertEqual(audits[0]['status'],500);self.assertEqual(audits[0]['outcome'],'unhandled_error')
        self.assertEqual(audits[0]['trace']['failure']['exception_class'],'TimeoutError')
        self.assertNotIn(f.PRIVATE,json.dumps(audits+events))

    async def test_server_cancellation_finishes_owned_request_and_audits_once(self):
        audits=[];events=[];started=asyncio.Event()
        async def forever(scope,receive,send):await receive();started.set();await asyncio.Event().wait()
        app=GatewayRequestScope(forever,audits.append,events.append,lambda:LifecycleBudget(1))
        task=asyncio.create_task(drive(app));await started.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(len(audits),1);self.assertEqual(audits[0]['outcome'],'server_cancelled');self.assertFalse(ACTIVE_REQUESTS)

if __name__=='__main__':unittest.main(verbosity=2)
