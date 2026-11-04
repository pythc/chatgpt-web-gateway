"""Explicit web/native adapters. Never silently exchange models or identities."""
import asyncio
from contextlib import aclosing
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import HTTPException
from api.client_tools import fail
from chatgpt import errorObservation as observation
from utils.Client import Client


def backend_mode():
    value = os.getenv('RESPONSES_BACKEND', 'web')
    if value not in ('web', 'native'):
        raise ValueError('RESPONSES_BACKEND must be web or native')
    return value


def native_settings():
    base = os.getenv('RESPONSES_UPSTREAM_BASE_URL', 'https://api.openai.com/v1').rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')):
        fail(500, 'invalid_upstream_configuration', 'Native upstream requires HTTPS or loopback HTTP')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        fail(500, 'invalid_upstream_configuration', 'Upstream base URL must not embed credentials or queries')
    token = os.getenv('RESPONSES_UPSTREAM_KEY')
    account = os.getenv('RESPONSES_UPSTREAM_ACCOUNT')
    auth_path = os.getenv('RESPONSES_CODEX_AUTH_FILE')
    if auth_path:
        # Explicit, read-only integration with the operator's own Codex login.
        # Credentials remain in process; no refresh, copy, or logging.
        try:
            saved = json.loads(Path(auth_path).read_text())
            tokens = saved.get('tokens') or {}
            token = tokens.get('access_token') or saved.get('OPENAI_API_KEY')
            account = tokens.get('account_id')
        except (OSError, ValueError):
            fail(503, 'upstream_credentials_unavailable', 'Configured upstream login could not be read')
    if not token:
        fail(503, 'upstream_credentials_unavailable', 'Configure a native upstream credential')
    headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
               'Accept': 'text/event-stream', 'originator': 'codex_cli_rs',
               'User-Agent': 'codex_cli_rs/0.166.0', 'OpenAI-Beta': 'responses=experimental'}
    if account:
        headers['ChatGPT-Account-ID'] = account
    return base, headers


def native_client():
    return Client(proxy=os.getenv('RESPONSES_UPSTREAM_PROXY') or None,
                  impersonate='chrome123', timeout=180)


async def native_stream(data, budget):
    base, headers = native_settings()
    client = native_client()
    # This adapter manages local retrieval separately. Codex subscription
    # inference is stateless; do not enable upstream storage implicitly.
    payload = {**data, 'stream': True, 'store': False}
    for field in ('auto_continue', 'history_disabled'):
        payload.pop(field, None)
    observation.stage('generation_request')
    budget.generation()
    try:
        r = await client.post_stream(base + '/responses', headers=headers, json=payload,
                                    stream=True, timeout=max(.01, budget.remaining()))
        observation.upstream_status(r.status_code)
        if r.status_code != 200:
            fail(r.status_code if r.status_code in (400, 401, 403, 404, 408, 409, 429) else 502,
                 'upstream_http_' + str(r.status_code), 'Native upstream rejected the request')
        observation.stage('initial_sse')
        size = 0
        parts = []
        async for line in r.aiter_lines():
            if isinstance(line, bytes):
                line = line.decode('utf-8')
            size += len(line)
            if size > 32 * 1024 * 1024:
                fail(502, 'upstream_stream_limit', 'Native stream exceeded the bounded response size')
            line = line.rstrip('\r\n')
            if not line:
                if parts:
                    raw = '\n'.join(parts);parts = []
                    if raw != '[DONE]':
                        yield decode_native_event(raw)
            elif line.startswith('data:'):
                parts.append(line[5:].lstrip(' '))
        if parts and '\n'.join(parts) != '[DONE]':
            yield decode_native_event('\n'.join(parts))
    except HTTPException:
        raise
    except Exception as error:
        observation.capture(error)
        fail(502, 'native_transport_error', 'Native upstream transport failed; remote outcome may be unknown')
    finally:
        await client.close()


def decode_native_event(raw):
    try:
        event = json.loads(raw)
        if not isinstance(event, dict) or not isinstance(event.get('type'), str):
            raise ValueError()
    except (ValueError, TypeError):
        fail(502, 'invalid_upstream_event', 'Invalid native response event')
    if event['type'] in ('error', 'response.failed'):
        # Do not forward arbitrary provider error strings or identifiers.
        error = event.get('error') or (event.get('response') or {}).get('error') or {}
        from chatgpt.upstreamErrorMetadata import metadata
        safe = metadata(error, event, 'event')
        event = {**event, 'error': {'type': 'upstream_error', 'code': safe.get('code', 'upstream_error'),
                                   'message': 'Native upstream reported a failure'}}
        if isinstance(event.get('response'), dict):
            event['response'] = {**event['response'], 'error': event['error']}
    return event


_catalog = {}


async def native_catalog(version):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        fail(400, 'invalid_client_version', 'client_version must be major.minor.patch')
    base, headers = native_settings()
    key = (version, base, hashlib.sha256((headers['Authorization'] + headers.get('ChatGPT-Account-ID', '')).encode()).hexdigest())
    cached = _catalog.get(key)
    if cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    client = native_client()
    try:
        r = await client.get(base + '/models', headers=headers, params={'client_version': version}, timeout=20)
        if r.status_code != 200:
            fail(r.status_code if r.status_code in (401, 403, 404, 429) else 502,
                 'upstream_catalog_unavailable', 'Could not retrieve upstream model metadata')
        data = r.json()
        if not isinstance(data, dict) or not isinstance(data.get('models'), list):
            fail(502, 'invalid_model_catalog', 'Native upstream did not return Codex Models format')
        allowed = {x for x in os.getenv('RESPONSES_ALLOWED_MODELS', '').split(',') if x}
        if allowed:
            data = {**data, 'models': [m for m in data['models'] if m.get('slug') in allowed]}
        _catalog[key] = (time.monotonic(), data)
        return data
    except HTTPException:
        raise
    except Exception:
        fail(502, 'catalog_transport_error', 'Could not connect to the configured model catalog')
    finally:
        await client.close()


async def native_compact(data, budget):
    mode = os.getenv('RESPONSES_COMPACTION_MODE', 'endpoint')
    if mode == 'responses':
        # Current Codex subscription transport requests V2 compaction as an
        # ordinary Responses turn. Select it explicitly; never retry a failed
        # endpoint call as another generation.
        payload = {k: data[k] for k in ('model', 'input', 'instructions') if k in data}
        payload['input'] = payload.get('input', []) + [{'type': 'compaction_trigger'}]
        final = None
        async with aclosing(native_stream(payload, budget)) as stream:
            async for event in stream:
                if event['type'] == 'response.completed':final = event.get('response')
                if event['type'] in ('response.completed', 'response.incomplete', 'response.failed', 'error'):break
        if not final or len(final.get('output', [])) != 1 or final['output'][0].get('type') != 'compaction':
            fail(502, 'invalid_compaction', 'Native compaction did not return one canonical compaction item')
        return {'id': final['id'], 'object': 'response.compaction',
                'created_at': final.get('created_at', int(time.time())), 'output': final['output'],
                'usage': final.get('usage')}
    if mode != 'endpoint':
        fail(500, 'invalid_compaction_mode', 'Configure endpoint or responses compaction mode')
    base, headers = native_settings()
    client = native_client()
    payload = {k: data[k] for k in ('model', 'input', 'instructions') if k in data}
    observation.stage('generation_request')
    budget.generation()
    try:
        r = await client.post(base + '/responses/compact', headers=headers, json=payload,
                              timeout=max(.01, budget.remaining()))
        observation.upstream_status(r.status_code)
        if r.status_code != 200:
            fail(r.status_code if r.status_code in (400, 401, 403, 404, 429) else 502,
                 'upstream_compaction_error', 'Native compaction failed')
        value = r.json()
        if not isinstance(value, dict) or not isinstance(value.get('output'), list):
            fail(502, 'invalid_compaction', 'Invalid native compaction response')
        # The entire native window, including opaque state, is canonical.
        return value
    except HTTPException:
        raise
    except Exception as error:
        observation.capture(error)
        fail(502, 'compaction_transport_error', 'Native compaction transport failed')
    finally:
        await client.close()
