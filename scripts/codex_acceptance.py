"""Opt-in real Codex/model acceptance in a disposable fixture.

Requires OPENAI_BASE_URL, OPENAI_API_KEY, CHATGPT_MODEL. This spends model
usage. It never changes user config; MCP uses a local read-only fixture.
Only the sanitized summary is written. No automatic retries are performed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from codex_run import provider, check_catalog


def inline(value):
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(k) + '=' + inline(v) for k, v in value.items()) + '}'
    return json.dumps(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=('hello', 'coding', 'mcp'), required=True)
    parser.add_argument('--mcp-python', help='Python with examples/requirements.txt installed')
    parser.add_argument('--output', default='diagnostics/codex-acceptance.json')
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('--websocket', action='store_true', help='Opt in to single-lane gateway WebSocket')
    args = parser.parse_args()
    if args.case == 'mcp' and not args.mcp_python:
        parser.error('--mcp-python is required for MCP acceptance')
    if args.timeout < 1:parser.error('--timeout must be positive')
    model = os.environ['CHATGPT_MODEL']
    config = provider();config['supports_websockets'] = args.websocket
    check_catalog(config, model)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Exclusive marker prevents accidental replay after an ambiguous timeout.
    marker = output.with_suffix('.started')
    with os.fdopen(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as f:
        json.dump({'case': args.case, 'model': model, 'started_at': time.time()}, f)
    with tempfile.TemporaryDirectory(prefix='gateway-codex-acceptance-') as directory:
        root = Path(directory)
        command = ['codex', 'exec', '--ignore-user-config', '--ignore-rules', '--ephemeral',
            '--skip-git-repo-check', '--sandbox', 'workspace-write' if args.case == 'coding' else 'read-only',
            '-C', directory, '-m', model, '-c', 'model_provider="gateway_acceptance"',
            '-c', 'model_providers.gateway_acceptance=' + inline(config),
            '-c', 'web_search="disabled"', '-c', 'features.apps=false', '--json']
        receipt = root / 'mcp-receipt.jsonl'
        if args.case == 'coding':
            (root / 'calculator.py').write_text('def add(a, b):\n    return a - b\n')
            tests = root / 'test_calculator.py'
            tests.write_text('import unittest\nfrom calculator import add\nclass TestAdd(unittest.TestCase):\n'
                '    def test_positive(self): self.assertEqual(add(17, 25), 42)\n'
                '    def test_negative(self): self.assertEqual(add(-3, 8), 5)\n')
            digest = hashlib.sha256(tests.read_bytes()).digest()
            baseline = subprocess.run(['python3', '-m', 'unittest'], cwd=directory, capture_output=True)
            prompt = ('Inspect calculator.py and test_calculator.py. Fix add without modifying tests. '
                      'Use tools to inspect, edit and run python3 -m unittest. Report verified results.')
        elif args.case == 'mcp':
            if not args.mcp_python:parser.error('--mcp-python is required for MCP acceptance')
            fixture = Path(__file__).resolve().parents[1] / 'tests/fixtures/codex_mcp_server.py'
            mcp = {'command': args.mcp_python, 'args': [str(fixture)],
                   'env': {'MCP_FIXTURE_RECEIPTS': str(receipt)}}
            command += ['-c', 'mcp_servers.fixture=' + inline(mcp)]
            prompt = ('Use the fixture MCP tool fixture_lookup with key mcp_sample. '
                      'Return only its exact value. Do not use shell or other tools.')
        else:
            prompt = 'Reply exactly GATEWAY_CODEX_OK. Do not use tools.'
        began = time.monotonic()
        summary = {'case': args.case, 'model_requested': model, 'real_upstream': True, 'passed': False}
        try:
            run = subprocess.run(command + [prompt], capture_output=True, text=True, timeout=args.timeout)
            rows = []
            for line in run.stdout.splitlines():
                try:rows.append(json.loads(line))
                except ValueError:pass
            texts = [r.get('item', {}).get('text', '') for r in rows if r.get('type') == 'item.completed'
                     and r.get('item', {}).get('type') == 'agent_message']
            summary.update(cli_exit=run.returncode, turn_completed=any(r.get('type') == 'turn.completed' for r in rows))
            summary['client_command_items'] = sum(r.get('type') == 'item.completed' and
                r.get('item', {}).get('type') == 'command_execution' for r in rows)
            summary['client_patch_items'] = sum(r.get('type') == 'item.completed' and
                r.get('item', {}).get('type') == 'file_change' for r in rows)
            if args.case == 'coding':
                final = subprocess.run(['python3', '-m', 'unittest'], cwd=directory, capture_output=True)
                summary.update(baseline_failed=baseline.returncode != 0, final_tests_passed=final.returncode == 0,
                               tests_unchanged=hashlib.sha256(tests.read_bytes()).digest() == digest)
                valid = summary['baseline_failed'] and summary['final_tests_passed'] and summary['tests_unchanged']
            elif args.case == 'mcp':
                receipts = [json.loads(line) for line in receipt.read_text().splitlines()] if receipt.exists() else []
                summary['mcp_executions'] = len(receipts)
                valid = bool(receipts) and any(receipts[-1]['value'] in t for t in texts)
            else:
                valid = any(t.strip() == 'GATEWAY_CODEX_OK' for t in texts)
            summary['passed'] = valid and run.returncode == 0 and summary['turn_completed']
        except subprocess.TimeoutExpired:
            summary.update(remote_outcome='unknown', no_automatic_retry=True)
        summary['elapsed_seconds'] = round(time.monotonic() - began, 3)
        with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as f:
            json.dump(summary, f, indent=2)
        print(json.dumps(summary))
        return 0 if summary['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
