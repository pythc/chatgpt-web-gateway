"""Opt-in real Codex/model acceptance in a disposable fixture.

Requires OPENAI_BASE_URL, OPENAI_API_KEY, CHATGPT_MODEL. This spends model
usage. It never changes user config; MCP uses a local read-only fixture.
Only the sanitized summary is written. No automatic retries are performed.
"""
import argparse
import hashlib
import json
import os
import re
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
    parser.add_argument('--mcp-python', help='Python with requirements.examples.txt installed')
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
    with tempfile.TemporaryDirectory(prefix='gateway-codex-acceptance-') as directory, \
            tempfile.TemporaryDirectory(prefix='gateway-codex-home-') as home_directory:
        root = Path(directory)
        # --ignore-user-config excludes config.toml, not installed skills or
        # managed plugin state. Isolate this disposable client invocation too.
        client_home = Path(home_directory)
        # A CLI launched from Codex Desktop can inherit its tool pipe/session.
        # The fixture must own its tools, while preserving ordinary PATH/proxy
        # and gateway provider credentials from the invoking environment.
        client_env = {k: v for k, v in os.environ.items() if not k.startswith('CODEX_')}
        client_env['CODEX_HOME'] = str(client_home)
        command = ['codex', 'exec', '--ignore-user-config', '--ignore-rules', '--ephemeral',
            '--skip-git-repo-check', '--sandbox', 'workspace-write' if args.case == 'coding' else 'read-only',
            '-C', directory, '-m', model, '-c', 'model_provider="gateway_acceptance"',
            '-c', 'model_providers.gateway_acceptance=' + inline(config),
            '-c', 'web_search="disabled"', '-c', 'features.apps=false',
            '-c', 'features.plugins=false', '-c', 'model_reasoning_effort="high"', '--json']
        receipt = root / 'mcp-receipt.jsonl'
        if args.case == 'coding':
            (root / 'calculator.py').write_text('def add(a, b):\n    return a - b\n')
            tests = root / 'test_calculator.py'
            tests.write_text('import unittest\nfrom calculator import add\nclass TestAdd(unittest.TestCase):\n'
                '    def test_positive(self): self.assertEqual(add(17, 25), 42)\n'
                '    def test_negative(self): self.assertEqual(add(-3, 8), 5)\n')
            # Normal coding clients inspect Git state and diffs after testing.
            # Keep that verification inside a complete disposable repository.
            subprocess.run(['git', 'init', '-q', directory], check=True)
            subprocess.run(['git', 'add', 'calculator.py', 'test_calculator.py'], cwd=directory, check=True)
            subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                '-c', 'user.name=Gateway acceptance', '-c', 'user.email=acceptance@example.invalid',
                'commit', '-qm', 'Acceptance baseline'], cwd=directory, check=True)
            digest = hashlib.sha256(tests.read_bytes()).digest()
            baseline = subprocess.run(['python3', '-m', 'unittest'], cwd=directory, capture_output=True)
            prompt = ('Inspect calculator.py and test_calculator.py. Fix add without modifying tests. '
                      'Use client shell tools to inspect files, apply_patch to change the implementation, '
                      'and client shell tools to run python3 -m unittest after the patch. Report verified results.')
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
        summary = {'case': args.case, 'model_requested': model, 'reasoning_effort': 'high',
                   'real_upstream': True, 'passed': False}
        try:
            run = subprocess.run(command + [prompt], capture_output=True, text=True,
                                 env=client_env, timeout=args.timeout)
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
                verified_by_client = any(r.get('type') == 'item.completed' and
                    r.get('item', {}).get('type') == 'command_execution' and r['item'].get('exit_code') == 0 and
                    re.search(r'\bpython(?:3(?:\.\d+)?)?\s+-m\s+unittest\b', r['item'].get('command', ''))
                    for r in rows)
                summary.update(baseline_failed=baseline.returncode != 0, final_tests_passed=final.returncode == 0,
                               tests_unchanged=hashlib.sha256(tests.read_bytes()).digest() == digest,
                               unit_tests_executed_by_client=verified_by_client)
                valid = (summary['baseline_failed'] and summary['final_tests_passed'] and summary['tests_unchanged']
                         and verified_by_client and summary['client_command_items'] >= 2 and summary['client_patch_items'] >= 1)
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
