import contextlib,io,json,logging,unittest,uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
logging.disable(logging.CRITICAL)
with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
    from app import app
    import api.chat2api as endpoint
    from fastapi import HTTPException
    from starlette.requests import Request
    from starlette.responses import Response
    from chatgpt.webStream import UpstreamError

class ObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_correlation_headers_and_allowlisted_audit(self):
        cid=str(uuid.uuid4());secret='SYNTHETIC_SECRET_DO_NOT_LOG'
        scope={'type':'http','method':'POST','scheme':'http','path':'/private-prefix/v1/chat/completions','headers':[(b'x-client-request-id',cid.encode()),(b'authorization',secret.encode())],'query_string':b''}
        request=Request(scope);records=[]
        async def call(req):
            req.state.gateway_trace={'budget':{'generation_posts':1,'snapshot_reads':2}}
            return Response('SYNTHETIC_PRIVATE_RESPONSE')
        with patch.object(endpoint,'append_audit',side_effect=records.append):
            response=await endpoint.gateway_observation(request,call)
        self.assertEqual(response.headers['X-Client-Request-Id'],cid)
        uuid.UUID(response.headers['X-Request-Id'])
        self.assertEqual(len(records),1)
        self.assertNotIn(secret,json.dumps(records));self.assertNotIn('SYNTHETIC_PRIVATE_RESPONSE',json.dumps(records));self.assertNotIn('private-prefix',json.dumps(records))

    async def test_invalid_client_id_is_not_reflected(self):
        request=Request({'type':'http','method':'GET','scheme':'http','path':'/health','headers':[(b'x-client-request-id',b'not-a-uuid')],'query_string':b''})
        response=await endpoint.gateway_observation(request,AsyncMock(return_value=Response('OK')))
        self.assertNotIn('X-Client-Request-Id',response.headers)
        uuid.UUID(response.headers['X-Request-Id'])

    async def test_typed_error_has_request_id_and_no_model_text(self):
        req=SimpleNamespace(state=SimpleNamespace(request_id='safe-id'))
        err=UpstreamError(502,{'error':{'code':'upstream_no_final_response','message':'No visible final','lifecycle':{'snapshot_reads':2}}})
        response=await endpoint.client_tool_error(req,err)
        self.assertEqual(json.loads(response.body)['error']['request_id'],'safe-id')

    async def test_json_format_does_not_accept_invalid_or_nonfinite_output(self):
        for text in ('not json','[]','{"x":NaN}','{"x":1,"x":2}'):
            req=SimpleNamespace(state=SimpleNamespace(),json=AsyncMock(return_value={'messages':[{'role':'user','content':'JSON'}],'response_format':{'type':'json_object'}}))
            service=SimpleNamespace(req_token='dummy',upstream_output_limited=False,close_client=AsyncMock())
            result={'id':'c','created':1,'model':'gpt-6-thinking','choices':[{'message':{'role':'assistant','content':text},'finish_reason':'stop'}],'usage':{'prompt_tokens':1}}
            with patch.object(endpoint,'process',AsyncMock(return_value=(service,result))):
                with self.assertRaises(UpstreamError):await endpoint.send_conversation(req,SimpleNamespace(credentials='local'))
            service.close_client.assert_awaited_once()

if __name__ == '__main__':unittest.main(verbosity=2)
