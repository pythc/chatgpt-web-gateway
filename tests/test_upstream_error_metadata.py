import asyncio,contextlib,io,json,logging,sys,types,unittest
logging.disable(logging.CRITICAL)
with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
 from app import app
 from chatgpt.webStream import ConversationState,UpstreamError
 from chatgpt.upstreamErrorMetadata import metadata
PRIVATE='PRIVATE_TOKEN_AND_INPUT_MUST_NOT_APPEAR'

def state():return ConversationState(types.SimpleNamespace(input_message_id=None))

class ErrorMetadataTests(unittest.TestCase):
 def test_confirmed_usage_limit_is_safe_literal_and_remains_terminal(self):
  import hashlib
  self.assertEqual(hashlib.sha256(b'usage_limit').hexdigest(),'dc0a4ee0e4511a0252399f3d4bcfd886320f42db4084ef512e907822c964aa69')
  s=state()
  with self.assertRaises(UpstreamError) as e:s.feed({'error':PRIVATE,'error_code':'usage_limit','message':None})
  self.assertEqual(s.summary()['upstream_error']['code'],'usage_limit')
  self.assertEqual(e.exception.detail['error']['code'],'upstream_error_event')
  self.assertNotIn(PRIVATE,json.dumps(s.summary()))

 def test_error_text_features_use_only_fixed_vocabulary(self):
  value=metadata('The model is unavailable: '+PRIVATE,{'error_code':'model_provider_new_private_code','message':'Unusual activity detected '+PRIVATE},'event')
  self.assertNotIn(PRIVATE,json.dumps(value));self.assertNotIn('new_private_code',json.dumps(value))
  self.assertIn('unavailable',value['error_text_features']['known_words']);self.assertIn('activity',value['message_text_features'][0]['known_words'])
  self.assertEqual(value['code_shape'],'aaaaa_aaaaaaaa_aaa_aaaaaaa_aaaa')

 def test_error_text_features_are_bounded_and_ignore_non_strings(self):
  value=metadata('a'*9000+PRIVATE,{'error_code':'x'*9000,'message':{'private':PRIVATE}},'event')
  self.assertTrue(value['code_features']['truncated']);self.assertTrue(value['error_text_features']['truncated'])
  self.assertNotIn('code_shape',value);self.assertEqual(value['message_text_features'],[]);self.assertNotIn(PRIVATE,json.dumps(value))

 def test_non_latin_and_surrogates_are_never_echoed(self):
  value=metadata({'code':'\ud800'+PRIVATE+'秘密'},{},'event')
  self.assertNotIn(PRIVATE,json.dumps(value));self.assertNotIn('秘密',json.dumps(value))

 def test_known_error_code_preserves_false_retry_without_raw_message(self):
  s=state()
  with self.assertRaises(UpstreamError) as e:s.feed({'type':'error','error':{'code':'model_not_available','message':PRIVATE,'can_retry':False,'status':403}})
  self.assertEqual(e.exception.detail['error']['code'],'upstream_error_event')
  info=s.summary()['upstream_error'];self.assertEqual(info['code'],'model_not_available');self.assertIs(info['can_retry'],False);self.assertEqual(info['reported_status'],403)
  self.assertNotIn(PRIVATE,json.dumps(s.summary()));self.assertNotIn(PRIVATE,json.dumps(e.exception.detail))

 def test_unknown_codes_are_hashed_not_echoed(self):
  result=metadata({'code':PRIVATE,'message':PRIVATE,'type':PRIVATE},None,'event')
  self.assertNotIn(PRIVATE,json.dumps(result));self.assertNotIn('code',result);self.assertEqual(len(result['unknown_code_sha256']),64);self.assertEqual(result['error_type'],'other')

 def test_top_level_error_fields_are_preserved_as_metadata(self):
  s=state()
  with self.assertRaises(UpstreamError):s.feed({'error':PRIVATE,'error_code':'rate_limit_exceeded','can_retry':True,'http_status':429})
  self.assertEqual(s.upstream_error['code'],'rate_limit_exceeded');self.assertEqual(s.upstream_error['error_value_type'],'str');self.assertIs(s.upstream_error['can_retry'],True)

 def test_delta_error_path_keeps_same_error_semantics(self):
  s=state()
  with self.assertRaises(UpstreamError):s.feed({'p':'','o':'add','v':{'error':{'code':'invalid_thinking_effort','detail':PRIVATE}}})
  self.assertEqual(s.upstream_error['location'],'delta');self.assertEqual(s.upstream_error['code'],'invalid_thinking_effort');self.assertNotIn(PRIVATE,json.dumps(s.summary()))

 def test_kind_error_without_error_value_still_fails_safely(self):
  s=state()
  with self.assertRaises(UpstreamError):s.feed({'type':'error','message':PRIVATE})
  self.assertEqual(s.upstream_error['error_value_type'],'null');self.assertNotIn(PRIVATE,json.dumps(s.summary()))

 def test_long_and_surrogate_codes_do_not_break_error_handling(self):
  long=metadata({'code':PRIVATE*1000},None,'event');self.assertTrue(long['code_too_long']);self.assertNotIn('unknown_code_sha256',long)
  short=metadata({'code':'\ud800'},None,'event');self.assertEqual(len(short['unknown_code_sha256']),64)

 def test_invalid_retry_status_and_code_types_are_not_interpreted(self):
  for value in (True,'403',{},[],3.5,2**40):
   result=metadata({'code':value,'status':value,'can_retry':'false'},None,'event');self.assertNotIn('reported_status',result);self.assertNotIn('can_retry',result);self.assertNotIn('code',result)

 def test_success_does_not_gain_error_metadata(self):
  s=state();s.feed({'message':{'id':'f','author':{'role':'assistant'},'channel':'final','content':{'content_type':'text','parts':['OK']},'status':'finished_successfully','end_turn':True}})
  self.assertIsNotNone(s.candidate());self.assertIsNone(s.summary()['upstream_error'])

class WSErrorMetadataTests(unittest.IsolatedAsyncioTestCase):
 async def test_ws_delta_error_metadata_survives_decoder_state_boundary(self):
  import ast
  from pathlib import Path
  f=types.ModuleType('envelope_fixture');tree=ast.parse((Path(__file__).resolve().parent / 'fixtures' / 'envelope_fixture.py').read_text());tree.body=[n for n in tree.body if not(isinstance(n,ast.Expr) and isinstance(n.value,ast.Call) and isinstance(n.value.func,ast.Attribute) and n.value.func.attr=='main' and isinstance(n.value.func.value,ast.Name) and n.value.func.value.id=='unittest')];exec(compile(tree,'fixture','exec'),f.__dict__)
  error=f.packet('err',{'error':{'code':'server_error','message':PRIVATE}},'err','a')
  s,st,_=f.fixture([[f.connect(),f.subscribe(f.seed()+[error])]])
  with self.assertRaises(UpstreamError):await f.run(s,st)
  self.assertEqual(st.upstream_error['code'],'server_error');self.assertEqual(st.upstream_error['location'],'event');self.assertNotIn(PRIVATE,json.dumps(st.summary()))

if __name__=='__main__':unittest.main(verbosity=2)
