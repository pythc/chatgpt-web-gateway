"""Tenant-scoped, bounded state. store=false never writes context to disk."""
import base64
import copy
import hashlib
import json
import os
import secrets
import time
from collections import OrderedDict
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from api.client_tools import fail


class ResponseState:
    def __init__(self, secret=None, directory=None, ttl=900, capacity=128):
        configured = secret or os.getenv('RESPONSES_STATE_SECRET')
        self.cipher = AESGCM(hashlib.sha256(configured.encode()).digest() if configured else secrets.token_bytes(32))
        self.directory = Path(directory or os.environ['RESPONSES_STATE_DIR']) if directory or os.getenv('RESPONSES_STATE_DIR') else None
        if self.directory is not None and not configured:
            raise ValueError('RESPONSES_STATE_SECRET is required for persistent checkpoints')
        self.ttl, self.capacity = ttl, capacity
        self.records = OrderedDict()
        self.requests = OrderedDict()

    @staticmethod
    def owner(key):
        return hashlib.sha256(key.encode()).hexdigest()

    def seal(self, value, owner):
        nonce = secrets.token_bytes(12)
        raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
        return 'gw1.' + base64.urlsafe_b64encode(nonce + self.cipher.encrypt(nonce, raw, owner.encode())).decode()

    def open(self, value, owner):
        try:
            if not isinstance(value, str) or not value.startswith('gw1.') or len(value) > 24 * 1024 * 1024:
                raise ValueError()
            raw = base64.b64decode(value[4:], altchars=b'-_', validate=True)
            return json.loads(self.cipher.decrypt(raw[:12], raw[12:], owner.encode()))
        except Exception:
            fail(400, 'invalid_compaction', 'Gateway compaction state is invalid, expired, or owned by another caller')

    def prune(self):
        now = time.monotonic()
        for collection in (self.records, self.requests):
            for key in list(collection):
                if collection[key]['expires'] < now:
                    del collection[key]
            while len(collection) > self.capacity:
                collection.popitem(last=False)

    def save(self, owner, request, response):
        self.prune()
        data = {'request': copy.deepcopy(request), 'response': copy.deepcopy(response), 'owner': owner,
                'expires': time.monotonic() + self.ttl, 'stored': request.get('store', False)}
        # Bound replay memory independently of declared model context size.
        if len(json.dumps(data).encode()) > 8 * 1024 * 1024:
            return
        key = (owner, response['id'])
        self.records[key] = data
        if self.directory is not None and data['stored']:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = self.directory / (hashlib.sha256(json.dumps(key).encode()).hexdigest() + '.json')
            # An encrypted checkpoint is available only when storage is explicit.
            payload = {'owner': owner, 'expires_at': time.time() + self.ttl,
                       'sealed': self.seal(data, owner)}
            temp = target.with_suffix('.tmp')
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(payload, stream)
            os.replace(temp, target)
        self.prune()

    def get(self, owner, response_id, retrieval=False):
        self.prune()
        key = (owner, response_id)
        row = self.records.get(key)
        if row is None and self.directory is not None:
            target = self.directory / (hashlib.sha256(json.dumps(key).encode()).hexdigest() + '.json')
            if target.is_file():
                try:
                    payload = json.loads(target.read_text())
                    if payload['owner'] == owner and payload['expires_at'] > time.time():
                        row = self.open(payload['sealed'], owner)
                        row['expires'] = time.monotonic() + payload['expires_at'] - time.time()
                        self.records[key] = row
                except (OSError, ValueError, KeyError):
                    row = None
        if row is None or (retrieval and not row['stored']):
            fail(404, 'response_not_found', 'Response state is not available for this caller')
        return copy.deepcopy(row)

    def delete(self, owner, response_id):
        self.get(owner, response_id, retrieval=True)
        self.records.pop((owner, response_id), None)
        if self.directory is not None:
            target = self.directory / (hashlib.sha256(json.dumps((owner, response_id)).encode()).hexdigest() + '.json')
            target.unlink(missing_ok=True)

    def expand(self, owner, request):
        data = copy.deepcopy(request)
        raw = data.get('input', [])
        if isinstance(raw, str):
            raw = [{'role': 'user', 'content': raw}]
        if not isinstance(raw, list):
            fail(400, 'invalid_input', 'input must be text or an item array')
        parent = data.pop('previous_response_id', None)
        if parent:
            row = self.get(owner, parent)
            if row['response'].get('gateway_warmup'):
                for field in ('tools', 'instructions', 'parallel_tool_calls', 'reasoning', 'tool_choice', 'text'):
                    if field not in data and field in row['request']:
                        data[field] = copy.deepcopy(row['request'][field])
            prior = row['request'].get('input', [])
            if isinstance(prior, str):
                prior = [{'role': 'user', 'content': prior}]
            raw = prior + row['response']['output'] + raw
        expanded = []
        for item in raw:
            if not isinstance(item, dict):
                fail(400, 'invalid_input', 'Input items must be objects')
            if item.get('type') == 'compaction' and str(item.get('encrypted_content', '')).startswith('gw1.'):
                checkpoint = self.open(item['encrypted_content'], owner)
                expanded.extend(checkpoint['input'])
            else:
                expanded.append(item)
        data['input'] = expanded
        return data

    def claim(self, owner, key, request):
        self.prune()
        if not key:
            return None
        if len(key) > 128:
            fail(400, 'invalid_idempotency_key', 'Idempotency key is too long')
        ident = (owner, hashlib.sha256(key.encode()).hexdigest())
        digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        prior = self.requests.get(ident)
        if prior:
            if prior['digest'] != digest:
                fail(409, 'idempotency_conflict', 'Idempotency key was used for another request')
            if prior['status'] != 'completed':
                fail(409, 'request_outcome_unknown', 'Request is pending or its remote outcome is unknown; do not replay automatically')
            return copy.deepcopy(prior['response'])
        self.requests[ident] = {'digest': digest, 'status': 'pending', 'expires': time.monotonic() + self.ttl}
        return None

    def finish(self, owner, key, response=None):
        if key:
            ident = (owner, hashlib.sha256(key.encode()).hexdigest())
            row = self.requests.get(ident)
            if row:
                row['status'] = 'completed' if response else 'unknown'
                if response:
                    row['response'] = copy.deepcopy(response)
