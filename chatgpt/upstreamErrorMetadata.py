"""Fixed error metadata only. Never retain upstream error/message text or identifiers."""
import hashlib
import re

CODES = frozenset({'model_not_found','model_not_available','unsupported_model','invalid_model',
    'invalid_request','invalid_request_error','rate_limit_exceeded','rate_limit_error','quota_exceeded',
    'usage_limit_exceeded','usage_limit','insufficient_quota','token_expired','authentication_error','unauthorized',
    'permission_denied','access_denied','internal_error','server_error','service_unavailable',
    'temporarily_unavailable','request_failed','model_cap_exceeded','model_not_supported',
    'model_access_denied','model_not_allowed','model_rate_limit_exceeded','model_quota_exceeded',
    'temporary_chat_not_supported','not_available_in_temporary_chat','model_not_available_in_temporary_chat',
    'unsupported_thinking_effort','invalid_thinking_effort','conversation_mode_not_supported',
    'conversation_not_found','no_available_models','invalid_api_key','invalid_sentinel_token','invalid_conduit_token'})
TYPES = frozenset({'server_error','invalid_request_error','rate_limit_error','authentication_error',
    'permission_error','model_error'})
FIELDS = ('code','error_code','type','message','detail','can_retry','status','status_code','http_status','error_reason')
WORDS = frozenset(('model models pro gpt invalid unknown internal generic server service request response '
    'upstream backend gateway account token rate quota limit cap exceeded reached denied forbidden access '
    'available unavailable supported unsupported failed failure error input message messages content policy '
    'moderation blocked context length large max temporary chat conversation processing inference execution '
    'thinking effort reasoning deprecated not is has too for in timeout network unusual activity detected '
    'authentication authorization expired permission subscription plan free plus exhausted credits api key '
    'id header validation mismatch schema missing required captcha challenge sentinel conduit search image '
    'file browsing tool routing rollout capacity overloaded deactivated sensitive safety user streaming '
    'generation provider connection disconnected unable trouble went wrong something later again try').split())


def text_features(value):
    """Fixed vocabulary and structure only, never free text or unknown tokens."""
    if not isinstance(value,str):return None
    bounded=value[:8192].lower()
    return {'length':min(len(value),1000000), 'truncated':len(value)>8192,
        'known_words':sorted(set(re.findall(r'[a-z]+',bounded)) & WORDS)}


def json_type(value):
    for cls, name in ((type(None),'null'),(bool,'bool'),(str,'str'),(int,'int'),(float,'float'),(dict,'dict'),(list,'list')):
        if type(value) is cls:return name
    return 'other'


def metadata(value, envelope, location):
    roots = ([value] if isinstance(value,dict) else []) + ([envelope] if isinstance(envelope,dict) else [])
    result = {'location':location if location in ('event','delta') else 'other','error_value_type':json_type(value),
        'known_fields_present':[key for key in FIELDS if any(key in root for root in roots)]}
    # Free-form codes can contain identifiers or credentials: allow known literal
    # categories, bounded numeric codes, or a one-way hash of unknown short codes.
    code = next((root[key] for root in roots for key in ('error_code','code') if key in root), None)
    result['code_value_type'] = json_type(code)
    if type(code) is int and -(2**31)<=code<2**31:result['code']=code
    elif isinstance(code,str):
        if code in CODES:result['code']=code
        elif len(code)<=256:result['unknown_code_sha256']=hashlib.sha256(code.encode('utf-8','surrogatepass')).hexdigest()
        else:result['code_too_long']=True
        result['code_features']=text_features(code)
        if len(code)<=256:
            # Letter/digit/punctuation classes reveal format, not actual content.
            result['code_shape']=re.sub(r'[^ad_ .:-]', '?', ''.join(
                'a' if char.isalpha() else 'd' if char.isdigit() else char for char in code))
    if isinstance(value,str):result['error_text_features']=text_features(value)
    result['message_text_features']=[feature for root in roots for key in ('message','detail','error_reason')
        if (feature:=text_features(root.get(key))) is not None][:6]
    if isinstance(value,dict):
        kind=value.get('type')
        if isinstance(kind,str) and kind in TYPES:result['error_type']=kind
        elif 'type' in value:result['error_type']='other'
    for root in roots:
        retry=root.get('can_retry')
        if type(retry) is bool:
            result['can_retry']=retry;break
    for root in roots:
        status=next((root[key] for key in ('http_status','status_code','status')
                     if type(root.get(key)) is int and 100<=root[key]<=599),None)
        if status is not None:
            result['reported_status']=status;break
    return result
