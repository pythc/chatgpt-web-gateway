"""Minimal real app-server client: threads, follow-up turns, and safe approvals.

Run with OPENAI_BASE_URL, OPENAI_API_KEY and CHATGPT_MODEL configured.
The gateway stays a model provider; Codex owns tools and execution.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from codex_run import provider, toml_inline, check_catalog


def main():
    p = argparse.ArgumentParser()
    p.add_argument('prompt')
    p.add_argument('--followup')
    p.add_argument('--resume')
    p.add_argument('--cwd', default=os.getcwd())
    p.add_argument('--persist', action='store_true', help='Explicitly retain the Codex thread locally')
    p.add_argument('--isolate-mcp', action='store_true', help='Do not start MCP servers from user config')
    args = p.parse_args()
    config = provider()
    check_catalog(config, os.environ.get('CHATGPT_MODEL', 'gpt-6-thinking'))
    command = ['codex', 'app-server', '-c', 'model_provider="web_gateway"',
               '-c', 'model_providers.web_gateway=' + toml_inline(config)]
    if args.isolate_mcp:command += ['-c', 'mcp_servers={}']
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    def send(value):
        process.stdin.write(json.dumps(value) + '\n');process.stdin.flush()
    send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'web-gateway-example', 'version': '1'}}})
    prompts = iter([args.prompt] + ([args.followup] if args.followup else []))
    thread_id = None
    try:
        for line in process.stdout:
            message = json.loads(line)
            if message.get('id') == 1 and 'result' in message:
                send({'method': 'initialized'})
                params = {'cwd': args.cwd, 'model': os.environ.get('CHATGPT_MODEL', 'gpt-6-thinking'),
                          'modelProvider': 'web_gateway', 'sandbox': 'read-only', 'approvalPolicy': 'on-request'}
                if args.resume:
                    params['threadId'] = args.resume
                    method = 'thread/resume'
                else:
                    params['ephemeral'] = not args.persist
                    method = 'thread/start'
                send({'id': 2, 'method': method, 'params': params})
            elif message.get('id') == 2 and 'result' in message:
                thread_id = message['result']['thread']['id']
                send({'id': 3, 'method': 'turn/start', 'params': {'threadId': thread_id,
                    'input': [{'type': 'text', 'text': next(prompts)}]}})
            elif message.get('method') == 'item/agentMessage/delta':
                print(message['params']['delta'], end='', flush=True)
            elif message.get('method') == 'turn/completed':
                print()
                if message['params']['turn']['status'] != 'completed':return 1
                following = next(prompts, None)
                if following is None:return 0
                send({'id': 4, 'method': 'turn/start', 'params': {'threadId': thread_id,
                    'input': [{'type': 'text', 'text': following}]}})
            elif 'id' in message and 'method' in message:
                # This minimal read-only example never grants execution or
                # filesystem permissions. Production clients must render the
                # approval/elicitation UI, or use the real Codex CLI directly.
                if message['method'] in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval'):
                    send({'id': message['id'], 'result': {'decision': 'decline'}})
                else:
                    send({'id': message['id'], 'error': {'code': -32601, 'message': 'Use an interactive Codex client for this request'}})
            elif 'error' in message:
                print('App-server request failed', file=sys.stderr);return 1
        return 1
    finally:
        if process.stdin:process.stdin.close()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=5)


if __name__ == '__main__':
    raise SystemExit(main())
