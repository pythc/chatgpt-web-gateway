"""Opt-in live web qualification. Uses real model usage; never retries inference.

Set OPENAI_BASE_URL, OPENAI_API_KEY and CHATGPT_MODEL. Use a quiet local instance
and its audit file to verify the backend and actual WebSocket transport.
Only a sanitized summary is saved; client config and fixtures are disposable.
"""
import argparse
import json
import os
from pathlib import Path
import queue
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
import urllib.error
from codex_run import provider, check_catalog
from codex_acceptance import inline


class QualificationFailure(Exception):
    pass


def require(condition, code):
    if not condition:raise QualificationFailure(code)


def output_text(value):
    return '\n'.join(p['text'] for item in value.get('output', []) if item.get('type') == 'message'
                     for p in item.get('content', []) if p.get('type') == 'output_text')


def client_env(home):
    env = {k: v for k, v in os.environ.items() if not k.startswith('CODEX_')}
    env['CODEX_HOME'] = str(home)
    return env


def command(kind, config, model):
    args = ['codex', kind, '-c', 'model_provider="gateway_qualification"',
        '-c', 'model_providers.gateway_qualification=' + inline(config),
        '-c', 'features.apps=false', '-c', 'features.plugins=false',
        '-c', 'web_search="disabled"', '-c', 'model_reasoning_effort="high"']
    return args


def cli_run(config, model, work, env, prompt, timeout, extra=()):
    args = command('exec', config, model) + ['--ignore-user-config', '--ignore-rules',
        '--ephemeral', '--skip-git-repo-check', '--sandbox', 'read-only', '-C', str(work),
        '-m', model, '--json', *extra, prompt]
    run = subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout)
    rows = []
    for line in run.stdout.splitlines():
        try:rows.append(json.loads(line))
        except ValueError:pass
    texts = [r.get('item', {}).get('text', '') for r in rows if r.get('type') == 'item.completed'
             and r.get('item', {}).get('type') == 'agent_message']
    result = {'cli_exit': run.returncode, 'turn_completed': any(r.get('type') == 'turn.completed' for r in rows)}
    require(run.returncode == 0 and result['turn_completed'], 'cli_turn_not_completed')
    return result, texts


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)


def http_mcp(args, config, model, work, env):
    receipt = work / 'mcp-receipts.jsonl'
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0));port = probe.getsockname()[1]
    fixture = Path(__file__).resolve().parents[1] / 'tests/fixtures/codex_mcp_server.py'
    server = subprocess.Popen([args.mcp_python, str(fixture)], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, env={**env, 'MCP_FIXTURE_TRANSPORT': 'streamable-http',
        'MCP_FIXTURE_PORT': str(port), 'MCP_FIXTURE_RECEIPTS': str(receipt)})
    try:
        end = time.monotonic() + 10
        while time.monotonic() < end:
            require(server.poll() is None, 'mcp_server_exited')
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.1):break
            except OSError:time.sleep(.05)
        else:raise QualificationFailure('mcp_server_not_ready')
        result, texts = cli_run(config, model, work, env,
            'Use the fixture MCP tool fixture_lookup with key mcp_sample. Return only the exact value '
            'it returns. Do not use shell or other tools.', args.timeout,
            ['-c', 'mcp_servers.fixture=' + inline({'url': f'http://127.0.0.1:{port}/mcp'})])
        records = [json.loads(line) for line in receipt.read_text().splitlines()] if receipt.exists() else []
        verified = bool(records) and any(t.strip() == records[-1]['value'] for t in texts)
        result.update(mcp_transport='streamable-http', mcp_executions=len(records), exact_value_verified=verified)
        require(verified, 'mcp_execution_or_value_missing')
        return result
    finally:stop(server)


def app_server(args, config, model, work, env):
    process = subprocess.Popen(command('app-server', config, model), env=env, text=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    inbox = queue.Queue()
    def read():
        try:
            for line in process.stdout:inbox.put(line)
        finally:inbox.put(None)
    reader = threading.Thread(target=read, daemon=True);reader.start()
    def send(value):
        process.stdin.write(json.dumps(value) + '\n');process.stdin.flush()
    marker = 'FIXTURE_' + secrets.token_hex(12)
    prompts = [f'Remember the fixture code is {marker}. Reply exactly ACK. Do not use tools.',
               'What fixture code did I provide? Reply only with the exact code. Do not use tools.']
    thread_id = None;turns = [];text = '';finished_text = '';end = time.monotonic() + args.timeout
    send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {
        'name': 'gateway-qualification', 'title': 'Gateway qualification', 'version': '1'}}})
    try:
        while len(turns) < 2:
            remaining = end - time.monotonic()
            if remaining <= 0:raise TimeoutError()
            try:line = inbox.get(timeout=remaining)
            except queue.Empty:raise TimeoutError() from None
            require(line is not None, 'app_server_exited')
            message = json.loads(line)
            if 'id' in message and 'method' in message:
                if message['method'] in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval'):
                    send({'id': message['id'], 'result': {'decision': 'decline'}})
                else:send({'id': message['id'], 'error': {'code': -32601, 'message': 'Unsupported qualification request'}})
            elif 'error' in message:raise QualificationFailure('app_server_rpc_error')
            elif message.get('id') == 1 and 'result' in message:
                send({'method': 'initialized'})
                send({'id': 2, 'method': 'thread/start', 'params': {'cwd': str(work), 'model': model,
                    'modelProvider': 'gateway_qualification', 'sandbox': 'read-only',
                    'approvalPolicy': 'on-request', 'ephemeral': True}})
            elif message.get('id') == 2 and 'result' in message:
                thread_id = message['result']['thread']['id']
                send({'id': 3, 'method': 'turn/start', 'params': {'threadId': thread_id,
                    'input': [{'type': 'text', 'text': prompts[0]}]}})
            elif message.get('method') == 'item/agentMessage/delta':text += message['params']['delta']
            elif message.get('method') == 'item/completed':
                item = message.get('params', {}).get('item', {})
                if item.get('type') == 'agentMessage':finished_text = item.get('text', '')
            elif message.get('method') == 'turn/completed':
                params = message['params'];require(params.get('threadId') == thread_id, 'app_server_thread_changed')
                require(params['turn']['status'] == 'completed', 'app_server_turn_not_completed')
                turns.append((text or finished_text).strip());text = '';finished_text = ''
                if len(turns) == 1:
                    send({'id': 4, 'method': 'turn/start', 'params': {'threadId': thread_id,
                        'input': [{'type': 'text', 'text': prompts[1]}]}})
        require(turns == ['ACK', marker], 'app_server_context_not_preserved')
        return {'app_server_turns_completed': 2, 'same_thread': True, 'exact_followup_verified': True}
    finally:
        process.stdin.close();stop(process);reader.join(timeout=2);process.stdout.close()


def previous_response(args, config, model):
    def post(value):
        request = urllib.request.Request(config['base_url'] + '/responses', data=json.dumps(value).encode(),
            headers={'Authorization': 'Bearer ' + os.environ['OPENAI_API_KEY'], 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=args.timeout) as response:result = json.load(response)
        require(result.get('status') == 'completed' and result.get('model') == model, 'response_not_completed')
        require(result.get('gateway_upstream_store') is False, 'upstream_storage_not_disabled')
        return result
    marker = 'FIXTURE_' + secrets.token_hex(12)
    common = {'model': model, 'reasoning': {'effort': 'high'}, 'store': False}
    first = post({**common, 'input': f'Remember the fixture code is {marker}. Reply exactly ACK.'})
    require(output_text(first).strip() == 'ACK', 'first_response_not_acknowledged')
    following = 'What fixture code did I provide? Reply only with the exact code.'
    second = post({**common, 'previous_response_id': first['id'], 'input': following})
    require(output_text(second).strip() == marker, 'previous_response_context_missing')
    return {'responses_completed': 2, 'previous_response_id_used': True,
            'fact_absent_from_followup': marker not in following, 'exact_followup_verified': True,
            'upstream_storage_disabled': True}


def audit_rows(path, offset):
    if not path or not path.exists():return []
    with path.open('rb') as stream:stream.seek(offset);lines = stream.read().splitlines()
    result = []
    for line in lines:
        try:result.append(json.loads(line))
        except ValueError:pass
    return result


def verify_audit(args, offset, model):
    end = time.monotonic() + 5
    expected = 1 if args.case == 'websocket' else 2
    while True:
        rows = audit_rows(args.audit_file, offset)
        generations = [r for r in rows if (r.get('trace', {}).get('budget') or {}).get('generation_posts', 0)]
        if len(generations) >= expected or time.monotonic() >= end:break
        time.sleep(.05)
    require(len(generations) >= expected, 'generation_audit_missing')
    require(all(r.get('status') == 200 and r['trace'].get('backend') == 'web'
        and r['trace'].get('actual_model') == model and r['trace'].get('response_status') == 'completed'
        for r in generations), 'web_backend_audit_failed')
    result = {'gateway_backend_verified': 'web', 'generation_posts': sum(r['trace']['budget']['generation_posts'] for r in generations)}
    if args.case == 'websocket':
        require(all(r.get('transport') == 'websocket' for r in generations), 'cli_fell_back_to_http')
        warmups = [r for r in rows if r.get('trace', {}).get('warmup')]
        require(all(r['trace']['budget']['generation_posts'] == 0 for r in warmups), 'warmup_started_inference')
        result.update(websocket_generation_records=len(generations), http_generation_records=0,
                      websocket_warmups=len(warmups), warmup_generation_posts=0)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', required=True, choices=('http-mcp', 'app-server', 'previous-response', 'websocket'))
    parser.add_argument('--mcp-python')
    parser.add_argument('--audit-file', type=Path, required=True, help='Audit file of the isolated local web instance')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=300)
    args = parser.parse_args()
    if args.case == 'http-mcp' and not args.mcp_python:parser.error('--mcp-python is required')
    if args.timeout < 1:parser.error('--timeout must be positive')
    config = provider();model = os.environ['CHATGPT_MODEL'];check_catalog(config, model)
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(args.output.with_suffix('.started'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
        json.dump({'case': args.case, 'model': model, 'started_at': time.time()}, stream)
    offset = args.audit_file.stat().st_size if args.audit_file.exists() else 0
    began = time.monotonic();result = {'case': args.case, 'model_requested': model, 'reasoning_effort': 'high',
        'real_upstream': True, 'passed': False}
    with tempfile.TemporaryDirectory(prefix='gateway-qualification-') as directory, \
            tempfile.TemporaryDirectory(prefix='gateway-qualification-home-') as home:
        work = Path(directory);env = client_env(home)
        try:
            if args.case == 'http-mcp':result.update(http_mcp(args, config, model, work, env))
            elif args.case == 'app-server':result.update(app_server(args, config, model, work, env))
            elif args.case == 'previous-response':result.update(previous_response(args, config, model))
            else:
                config['supports_websockets'] = True
                values, texts = cli_run(config, model, work, env,
                    'Reply exactly GATEWAY_WEBSOCKET_OK. Do not use tools.', args.timeout)
                require(any(t.strip() == 'GATEWAY_WEBSOCKET_OK' for t in texts), 'websocket_answer_mismatch')
                result.update(values)
            result.update(verify_audit(args, offset, model));result['passed'] = True
        except QualificationFailure as error:result['failure_code'] = str(error)
        except (TimeoutError, subprocess.TimeoutExpired):result.update(remote_outcome='unknown', no_automatic_retry=True)
        except Exception as error:result['error_class'] = type(error).__name__
    result['elapsed_seconds'] = round(time.monotonic() - began, 3)
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:json.dump(result, stream, indent=2)
    print(json.dumps(result));return 0 if result['passed'] else 1


if __name__ == '__main__':raise SystemExit(main())
