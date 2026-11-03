import asyncio, contextlib, io, json, logging, unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

logging.disable(logging.CRITICAL)
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    from app import app
    from chatgpt.ChatService import ChatService
    from chatgpt.chatFormat import head_process_response, format_not_stream_response, record_upstream_model, stream_response
    from api.model_catalog import select_thinking_effort
    from fastapi import HTTPException

CATALOG=[{'slug':'gpt-6-thinking','thinking_efforts':[{'thinking_effort':e} for e in ('min','standard','extended','max')]}, {'slug':'gpt-6-pro','thinking_efforts':[{'thinking_effort':'standard'}]}]

async def lines(*events):
    for event in events:
        yield ('data: '+json.dumps(event)).encode()

class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def select(self,data):
        service=ChatService.__new__(ChatService)
        service.data=data;service.req_token='dummy-account'
        with patch('chatgpt.ChatService.get_model_catalog',new=AsyncMock(return_value=CATALOG)):
            await service.set_model()
        return service

    async def test_default_is_thinking_high_not_pro(self):
        service=await self.select({})
        self.assertEqual(service.req_model,'gpt-6-thinking')
        self.assertEqual(service.thinking_effort,'extended')
        self.assertTrue(service.strict_model)

    async def test_pro_remains_explicit_without_high(self):
        service=await self.select({'model':'gpt-6-pro'})
        self.assertEqual(service.req_model,'gpt-6-pro')
        self.assertIsNone(service.thinking_effort)

    async def test_unknown_never_falls_back(self):
        for model in ('gpt-6-pro-typo','gpt-4o-typo','unknown','not-auto'):
            with self.subTest(model=model):
                with self.assertRaises(HTTPException) as error:await self.select({'model':model})
                self.assertEqual(error.exception.status_code,404)

    async def test_invalid_efforts_rejected(self):
        for data in ({'model':'gpt-6-pro','reasoning_effort':'high'}, {'model':'gpt-6-thinking','reasoning_effort':[]}, {'model':'gpt-6-thinking','reasoning_effort':'high','thinking_effort':'max'}):
            with self.subTest(data=data):
                with self.assertRaises(HTTPException) as error:await self.select(data)
                self.assertEqual(error.exception.status_code,400)

    async def test_auto_stays_explicit_auto(self):
        service=await self.select({'model':'auto'})
        self.assertEqual(service.req_model,'auto')
        self.assertFalse(service.strict_model)

    def service(self):
        return SimpleNamespace(req_model='gpt-6-thinking',strict_model=True,thinking_effort='extended',upstream_model=None,upstream_thinking_effort=None)

    async def test_header_confirms_real_model_and_effort(self):
        service=self.service()
        response=lines({'message':{'author':{'role':'assistant'},'status':'in_progress','metadata':{'model_slug':'gpt-6-thinking','thinking_effort':'extended'}}})
        _,started=await head_process_response(response,service)
        self.assertTrue(started)
        self.assertEqual(service.upstream_model,'gpt-6-thinking')
        self.assertEqual(service.upstream_thinking_effort,'extended')

    async def test_upstream_model_mismatch_rejected(self):
        service=self.service()
        response=lines({'message':{'author':{'role':'assistant'},'status':'in_progress','metadata':{'model_slug':'gpt-4o'}}})
        with self.assertRaises(HTTPException) as error:await head_process_response(response,service)
        self.assertEqual(error.exception.status_code,502)

    async def test_nonstream_reports_observed_metadata(self):
        async def response():
            yield 'data: '+json.dumps({'model':'gpt-6-thinking','thinking_effort':'extended','choices':[{'delta':{'content':'OK'},'finish_reason':'stop'}]})
            yield 'data: [DONE]'
        with patch('chatgpt.chatFormat.split_tokens_from_content',new=AsyncMock(return_value=('OK',1,'stop'))):
            output=await format_not_stream_response(response(),1,32,'auto')
        self.assertEqual(output['model'],'gpt-6-thinking')
        self.assertEqual(output['thinking_effort'],'extended')

    async def test_nonstream_does_not_turn_stream_error_into_success(self):
        async def response():yield 'data: '+json.dumps({'error':{'error':'upstream_model_mismatch'}})
        with self.assertRaises(HTTPException):await format_not_stream_response(response(),1,32,'gpt-6-thinking')

    async def test_upstream_partial_completion_keeps_tail_and_length(self):
        service=self.service();service.history_disabled=True
        def event(status,text,finish=None):
            return {'message':{'id':'same-message','author':{'role':'assistant'},'status':status,'channel':'final',
                    'content':{'content_type':'text','parts':[text]},'metadata':{'model_slug':'gpt-6-thinking',
                    'thinking_effort':'extended',**({'finish_details':{'type':finish}} if finish else {})}}}
        frames=[frame async for frame in stream_response(service,lines(
            event('in_progress','visible '),event('finished_partial_completion','visible tail','max_tokens'),
            {}),'gpt-6-thinking',65536)]
        events=[json.loads(f[6:]) for f in frames if f.startswith('data: {')]
        self.assertEqual(''.join(e['choices'][0]['delta'].get('content','') for e in events),'visible tail')
        self.assertEqual(events[-1]['choices'][0]['finish_reason'],'length')
        async def response():
            for frame in frames:yield frame
        result=await format_not_stream_response(response(),1,65536,'gpt-6-thinking')
        self.assertEqual(result['choices'][0]['message']['content'],'visible tail')
        self.assertEqual(result['choices'][0]['finish_reason'],'length')

    async def test_other_upstream_partial_failure_is_not_success(self):
        service=self.service();service.history_disabled=True
        response=lines({'message':{'id':'partial','status':'finished_partial_completion','channel':'final','content':{'content_type':'text','parts':['partial']},'author':{'role':'assistant'},
            'metadata':{'finish_details':{'type':'interrupted'}}}})
        frames=[frame async for frame in stream_response(service,response,'gpt-6-thinking',65536)]
        self.assertTrue(any('upstream_incomplete_response' in f for f in frames))

    def test_changed_upstream_effort_rejected(self):
        with self.assertRaises(HTTPException):record_upstream_model(self.service(),{'author':{'role':'assistant'},'metadata':{'model_slug':'gpt-6-thinking','thinking_effort':'standard'}})

if __name__ == '__main__':unittest.main(verbosity=2)
