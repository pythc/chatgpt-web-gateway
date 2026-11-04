"""Acceptance gates must reject transport fallback and incomplete evidence."""
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import web_qualification as suite


def record(transport=None, warmup=False):
    trace = {'backend': 'web', 'actual_model': 'fixture-model', 'response_status': 'completed',
             'budget': {'generation_posts': 0 if warmup else 1}}
    if warmup:trace['warmup'] = True
    return {'status': 200, 'transport': transport, 'trace': trace}


class QualificationGateTests(unittest.TestCase):
    def audit(self, rows, case='websocket', offset=0):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'audit.jsonl'
            path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
            args = types.SimpleNamespace(case=case, audit_file=path)
            return suite.verify_audit(args, offset, 'fixture-model')

    def test_websocket_requires_actual_generation_and_generation_free_warmup(self):
        result = self.audit([record('websocket', warmup=True), record('websocket')])
        self.assertEqual(result['websocket_generation_records'], 1)
        self.assertEqual(result['websocket_warmups'], 1)
        self.assertEqual(result['warmup_generation_posts'], 0)
        self.assertEqual(result['http_generation_records'], 0)

    def test_http_fallback_and_inference_warmup_are_rejected(self):
        with self.assertRaisesRegex(suite.QualificationFailure, 'cli_fell_back_to_http'):
            self.audit([record()])
        warmup = record('websocket', warmup=True);warmup['trace']['budget']['generation_posts'] = 1
        with self.assertRaisesRegex(suite.QualificationFailure, 'warmup_started_inference'):
            self.audit([warmup, record('websocket')])

    def test_wrong_backend_model_and_failed_terminal_are_rejected(self):
        for field, value in [('backend', 'native'), ('actual_model', 'other-model'), ('response_status', 'failed')]:
            row = record('websocket');row['trace'][field] = value
            with self.subTest(field=field), self.assertRaises(suite.QualificationFailure):self.audit([row])

    def test_two_turns_require_two_completed_generation_receipts(self):
        self.assertEqual(self.audit([record(), record()], 'previous-response')['generation_posts'], 2)
        with patch.object(suite.time, 'monotonic', side_effect=[0, 10]):
            with self.assertRaisesRegex(suite.QualificationFailure, 'generation_audit_missing'):
                self.audit([record()], 'previous-response')

    def test_app_server_never_accepts_failed_followup_as_completed(self):
        class Sink(io.StringIO):
            def close(self):self.saved = self.getvalue();super().close()
        for status in ('completed', 'failed'):
            messages = [{'id': 1, 'result': {}}, {'id': 2, 'result': {'thread': {'id': 'fixture-thread'}}}]
            for text, state in [('ACK', 'completed'), ('FIXTURE_abc', status)]:
                messages += [{'method': 'item/agentMessage/delta', 'params': {'delta': text}},
                    {'method': 'turn/completed', 'params': {'threadId': 'fixture-thread', 'turn': {'status': state}}}]
            sink = Sink();process = types.SimpleNamespace(stdin=sink,
                stdout=io.StringIO('\n'.join(json.dumps(m) for m in messages) + '\n'), poll=lambda: 0)
            with patch.object(suite.subprocess, 'Popen', return_value=process), \
                    patch.object(suite.secrets, 'token_hex', return_value='abc'):
                args = types.SimpleNamespace(timeout=1)
                if status == 'failed':
                    with self.assertRaisesRegex(suite.QualificationFailure, 'app_server_turn_not_completed'):
                        suite.app_server(args, {}, 'fixture-model', Path('/fixture'), {})
                else:
                    result = suite.app_server(args, {}, 'fixture-model', Path('/fixture'), {})
                    self.assertEqual(result['app_server_turns_completed'], 2)
                    sent = [json.loads(line) for line in sink.saved.splitlines()]
                    starts = [m for m in sent if m.get('method') == 'turn/start']
                    self.assertEqual([m['params']['threadId'] for m in starts], ['fixture-thread'] * 2)
                    self.assertNotIn('FIXTURE_abc', starts[1]['params']['input'][0]['text'])


if __name__ == '__main__':unittest.main(verbosity=2)
