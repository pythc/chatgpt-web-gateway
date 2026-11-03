import asyncio, contextlib, copy, io, json, logging, unittest
from unittest.mock import AsyncMock, patch

logging.disable(logging.CRITICAL)
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    from app import app
    from api.client_tools import ClientToolBridge, needs_tool_bridge
    from fastapi import HTTPException

TOOL={'type':'function','function':{'name':'lookup','description':'Client fixture lookup','strict':True,'parameters':{'type':'object','properties':{'key':{'type':'string','enum':['sample']},'count':{'type':'integer','minimum':1}},'required':['key'],'additionalProperties':False}}}

class ClientToolTests(unittest.IsolatedAsyncioTestCase):
    def bridge(self,**kwargs):
        data={'messages':[{'role':'user','content':'Use the fixture'}],'tools':[copy.deepcopy(TOOL)]}
        data.update(kwargs)
        return ClientToolBridge(data)

    def response(self,bridge,**kwargs):
        envelope={'bridge':bridge.nonce,'kind':'tool_calls','calls':[{'name':'lookup','arguments':{'key':'sample'}}]}
        envelope.update(kwargs)
        return {'id':'chatcmpl-fixture','object':'chat.completion','created':1,'model':'gpt-6-thinking','thinking_effort':'extended','choices':[{'message':{'content':json.dumps(envelope)},'finish_reason':'stop'}],'usage':{'prompt_tokens':20,'completion_tokens':30,'total_tokens':50}}

    def assert_status(self,status,fn):
        with self.assertRaises(HTTPException) as error:fn()
        self.assertEqual(error.exception.status_code,status)

    async def test_default_preserves_answer_above_old_4096_limit(self):
        from api.tokens import num_tokens_from_content
        bridge=self.bridge()
        self.assertEqual(bridge.limit,262144)
        content='word ' * 5000
        self.assertGreater(await num_tokens_from_content(content,'gpt-6-thinking'),4096)
        result=await bridge.completion(self.response(bridge,kind='final',content=content))
        self.assertEqual(result['choices'][0]['message']['content'],content)
        self.assertEqual(result['choices'][0]['finish_reason'],'stop')

    def test_explicit_large_limit_is_not_capped_and_null_uses_default(self):
        for field in ('max_tokens','max_completion_tokens'):
            with self.subTest(field=field):
                bridge=self.bridge(**{field:131072})
                self.assertEqual(bridge.limit,131072)
                self.assertEqual(bridge.upstream_request()['max_tokens'],132096)
        self.assertEqual(self.bridge(max_tokens=8,max_completion_tokens=None).limit,8)
        self.assertEqual(self.bridge(max_tokens=None,max_completion_tokens=None).limit,262144)

    async def test_explicit_small_limit_still_truncates_final_answer(self):
        bridge=self.bridge(max_completion_tokens=8,max_tokens=100)
        result=await bridge.completion(self.response(bridge,kind='final',content='word ' * 30))
        self.assertEqual(bridge.limit,8)
        self.assertEqual(result['choices'][0]['finish_reason'],'length')
        self.assertEqual(result['usage']['completion_tokens'],8)

    async def test_upstream_length_is_preserved_for_plain_final_answer(self):
        bridge=self.bridge()
        upstream=self.response(bridge)
        upstream['choices'][0]['message']['content']='partial plain answer'
        upstream['choices'][0]['finish_reason']='length'
        result=await bridge.completion(upstream)
        self.assertEqual(result['choices'][0]['message']['content'],'partial plain answer')
        self.assertEqual(result['choices'][0]['finish_reason'],'length')

    async def test_known_complete_wrappers_and_no_arbitrary_json_extraction(self):
        bridge=self.bridge(tool_choice='required')
        for prefix,suffix in (('<escape>','</escape>'),('```json\n','\n```'),('```\n','\n```')):
            upstream=self.response(bridge)
            text=upstream['choices'][0]['message']['content']
            upstream['choices'][0]['message']['content']=prefix+text+suffix
            result=await bridge.completion(upstream)
            self.assertEqual(result['choices'][0]['finish_reason'],'tool_calls')
        for prefix,suffix in (('arbitrary prefix ',''),('<escape>',''),('',' trailing text')):
            upstream=self.response(bridge)
            text=upstream['choices'][0]['message']['content']
            upstream['choices'][0]['message']['content']=prefix+text+suffix
            with self.assertRaises(HTTPException):await bridge.completion(upstream)

    async def test_auto_plain_text_and_user_json_remain_final_answers(self):
        bridge=self.bridge()
        for text in ('MCP_RESULT_fixture','{"result":42}','{"bridge":"Golden Gate"}'):
            upstream=self.response(bridge)
            upstream['choices'][0]['message']['content']=text
            result=await bridge.completion(upstream)
            self.assertEqual(result['choices'][0]['message']['content'],text)
            self.assertEqual(result['choices'][0]['finish_reason'],'stop')
            self.assertNotIn('tool_calls',result['choices'][0]['message'])

    async def test_auto_malformed_protocol_is_not_hidden_as_plain_text(self):
        bridge=self.bridge();upstream=self.response(bridge)
        upstream['choices'][0]['message']['content']='{"bridge":"'+bridge.nonce+'","kind":"tool_calls",'
        with self.assertRaises(HTTPException):await bridge.completion(upstream)

    async def test_standard_nonstream_tool_call_contract(self):
        bridge=self.bridge(tool_choice={'type':'function','function':{'name':'lookup'}})
        result=await bridge.completion(self.response(bridge))
        choice=result['choices'][0];call=choice['message']['tool_calls'][0]
        self.assertEqual(choice['finish_reason'],'tool_calls')
        self.assertIsNone(choice['message']['content'])
        self.assertTrue(call['id'].startswith('call_'))
        self.assertEqual(call['type'],'function')
        self.assertEqual(json.loads(call['function']['arguments']),{'key':'sample'})

    async def test_bad_schema_arguments_never_reach_client(self):
        for arguments in ({'key':'wrong'},{'key':'sample','count':True},{'key':'sample','extra':1},{}):
            bridge=self.bridge()
            with self.subTest(arguments=arguments):
                with self.assertRaises(HTTPException) as error:await bridge.completion(self.response(bridge,calls=[{'name':'lookup','arguments':arguments}]))
                self.assertEqual(error.exception.status_code,502)

    async def test_unknown_and_wrong_forced_names_rejected(self):
        bridge=self.bridge(tool_choice={'type':'function','function':{'name':'lookup'}})
        with self.assertRaises(HTTPException):await bridge.completion(self.response(bridge,calls=[{'name':'not_declared','arguments':{}}]))

    async def test_none_forbids_calls(self):
        bridge=self.bridge(tool_choice='none')
        result=await bridge.completion(self.response(bridge))
        self.assertNotIn('tool_calls',result['choices'][0]['message'])
        self.assertEqual(result['choices'][0]['finish_reason'],'stop')

    async def test_required_forbids_plain_answer(self):
        bridge=self.bridge(tool_choice='required')
        with self.assertRaises(HTTPException):await bridge.completion(self.response(bridge,kind='final',content='guessed result'))

    async def test_auto_and_none_allow_final_answer(self):
        for choice in ('auto','none'):
            bridge=self.bridge(tool_choice=choice)
            with patch('api.client_tools.split_tokens_from_content',new=AsyncMock(return_value=('READY',1,'stop'))):
                response=self.response(bridge,kind='final',content='READY')
                if choice=='none':response['choices'][0]['message']['content']='READY'
                result=await bridge.completion(response)
            self.assertEqual(result['choices'][0]['message']['content'],'READY')
            self.assertEqual(result['choices'][0]['finish_reason'],'stop')

    async def test_parallel_false_forbids_multiple_calls(self):
        bridge=self.bridge(parallel_tool_calls=False)
        calls=[{'name':'lookup','arguments':{'key':'sample'}}]*2
        with self.assertRaises(HTTPException):await bridge.completion(self.response(bridge,calls=calls))

    async def test_parallel_has_unique_correlatable_ids(self):
        bridge=self.bridge()
        result=await bridge.completion(self.response(bridge,calls=[{'name':'lookup','arguments':{'key':'sample'}}]*2))
        calls=result['choices'][0]['message']['tool_calls']
        self.assertEqual(len({c['id'] for c in calls}),2)

    def test_history_null_assistant_and_two_results(self):
        calls=[{'id':name,'type':'function','function':{'name':'lookup','arguments':'{"key":"sample"}'}} for name in ('call_a','call_b')]
        messages=[{'role':'user','content':'Fetch both'},{'role':'assistant','content':None,'tool_calls':calls},{'role':'tool','tool_call_id':'call_b','content':'B'},{'role':'tool','tool_call_id':'call_a','content':'A'}]
        bridge=self.bridge(messages=messages)
        request=bridge.upstream_request()
        self.assertEqual([m['role'] for m in request['messages']],[ 'system','user','assistant','user','user'])
        result=json.loads(request['messages'][-1]['content'])
        self.assertEqual(result['tool_call_id'],'call_a')
        self.assertEqual(result['content'],'A')

    def test_orphan_missing_duplicate_results_rejected(self):
        call={'id':'call_a','type':'function','function':{'name':'lookup','arguments':'{}'}}
        assistant={'role':'assistant','content':None,'tool_calls':[call]}
        tool={'role':'tool','tool_call_id':'call_a','content':'value'}
        for messages in ([tool],[assistant],[assistant,{'role':'user','content':'skip'}],[assistant,tool,tool]):
            with self.subTest(messages=messages):self.assert_status(400,lambda:self.bridge(messages=messages))

    def test_invalid_request_controls_rejected(self):
        for kwargs in ({'tool_choice':'typo'},{'tool_choice':{'type':'function','function':{'name':'missing'}}},{'parallel_tool_calls':'false'},{'n':2},{'n':True},{'max_tokens':0},{'stream_options':{'include_usage':'yes'}},{'response_format':{'type':'json_object'}}):
            with self.subTest(kwargs=kwargs):self.assert_status(400,lambda:self.bridge(**kwargs))

    def test_remote_schema_refs_and_duplicate_names_rejected(self):
        tool=copy.deepcopy(TOOL);tool['function']['parameters']={'type':'object','properties':{'x':{'$ref':'http://127.0.0.1/private'}}}
        self.assert_status(400,lambda:self.bridge(tools=[tool]))
        self.assert_status(400,lambda:self.bridge(tools=[TOOL,TOOL]))

    async def test_local_refs_are_validated(self):
        tool=copy.deepcopy(TOOL);tool['function']['parameters']={'type':'object','properties':{'key':{'$ref':'#/$defs/key'}},'required':['key'],'$defs':{'key':{'const':'sample'}}}
        bridge=self.bridge(tools=[tool]);result=await bridge.completion(self.response(bridge))
        self.assertEqual(result['choices'][0]['finish_reason'],'tool_calls')

    async def test_bad_truncated_or_wrong_envelope_fails_closed(self):
        for text in ('not json',json.dumps({'bridge':'wrong','kind':'final','content':'fake'}),'{"bridge":"a","bridge":"b"}', '{"x":NaN}'):
            bridge=self.bridge(tool_choice='required');upstream=self.response(bridge);upstream['choices'][0]['message']['content']=text
            with self.subTest(text=text):
                with self.assertRaises(HTTPException):await bridge.completion(upstream)
        bridge=self.bridge(tool_choice='required');upstream=self.response(bridge);upstream['choices'][0]['finish_reason']='length'
        with self.assertRaises(HTTPException):await bridge.completion(upstream)

    async def test_stream_reassembles_arguments_ids_indexes_usage_done(self):
        bridge=self.bridge(stream=True,stream_options={'include_usage':True})
        completion=await bridge.completion(self.response(bridge,calls=[{'name':'lookup','arguments':{'key':'sample'}}]*2))
        frames=[frame async for frame in bridge.chunks(completion)]
        self.assertEqual(frames[-1],'data: [DONE]\n\n')
        aggregates={};finish=None;usage=None
        for frame in frames[:-1]:
            event=json.loads(frame[6:])
            if not event['choices']:usage=event['usage'];continue
            choice=event['choices'][0]
            if choice['finish_reason']:finish=choice['finish_reason']
            for delta in choice['delta'].get('tool_calls',[]):
                value=aggregates.setdefault(delta['index'],{'arguments':''})
                if delta.get('id'):value['id']=delta['id']
                if delta['function'].get('name'):value['name']=delta['function']['name']
                value['arguments']+=delta['function'].get('arguments','')
        self.assertEqual(finish,'tool_calls');self.assertIsNotNone(usage)
        self.assertEqual(set(aggregates),{0,1})
        for index,value in aggregates.items():
            self.assertEqual(value['id'],completion['choices'][0]['message']['tool_calls'][index]['id'])
            self.assertEqual(json.loads(value['arguments']),{'key':'sample'})

    def test_plain_chat_is_not_intercepted_and_input_not_mutated(self):
        data={'messages':[{'role':'user','content':'hi'}]}
        self.assertFalse(needs_tool_bridge(data))
        data['tools']=[copy.deepcopy(TOOL)];before=copy.deepcopy(data)
        bridge=ClientToolBridge(data);bridge.upstream_request()
        self.assertEqual(data,before)

if __name__ == '__main__':unittest.main(verbosity=2)
