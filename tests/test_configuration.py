"""File-based setup, credential privacy and removed management routes."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from utils.credentials import read_token_file

ROOT = Path(__file__).resolve().parents[1]


class CredentialFileTests(unittest.TestCase):
    def test_formats_long_values_deduplication_and_read_only_input(self):
        token = 'synthetic_' + 'x' * 36039
        for text, expected in [('# comment\n first\nsecond\nfirst\n', ['first', 'second']),
                (json.dumps({'accessToken': token, 'user': {'name': 'not a credential'}}), [token]),
                (json.dumps(['first', 'second', 'first']), ['first', 'second']), ('\ufefffirst\n', ['first']),
                ('\n # empty file\n', [])]:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'account.json'
                path.write_text(text);path.chmod(0o400)
                before = path.read_bytes()
                self.assertEqual(read_token_file(path), expected)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(path.stat().st_mode & 0o777, 0o400)

    def test_invalid_credentials_do_not_echo_file_contents(self):
        marker = 'PRIVATE_CREDENTIAL_VALUE'
        for text in ['{"accessToken":"' + marker, json.dumps({'token': marker}),
                json.dumps({'accessToken': None}), json.dumps([marker, 17]), marker + ' invalid whitespace']:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'account.txt';path.write_text(text)
                with self.assertRaises(ValueError) as caught:read_token_file(path)
                self.assertNotIn(marker, str(caught.exception))

    def test_startup_uses_configured_file_and_preserves_legacy_path(self):
        for configured in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                work = Path(directory);(work/'version.txt').write_text('fixture')
                (work/'data').mkdir();(work/'data/token.txt').write_text('legacy_token\n')
                path = work/'credentials.json';path.write_text('{"accessToken":"configured_token"}')
                env = {**os.environ, 'PYTHONPATH': str(ROOT), 'PYTHON_DOTENV_DISABLED': '1'}
                env.pop('CHATGPT_TOKEN_FILE', None)
                if configured:env['CHATGPT_TOKEN_FILE'] = str(path)
                result = subprocess.run([sys.executable, '-c',
                    'import utils.globals as g; assert g.token_list == ["' +
                    ('configured_token' if configured else 'legacy_token') + '"]'],
                    cwd=work, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((work/'data/token.txt').read_text(), 'legacy_token\n')
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory);(work/'version.txt').write_text('fixture')
            env = {**os.environ, 'PYTHONPATH': str(ROOT), 'PYTHON_DOTENV_DISABLED': '1',
                   'CHATGPT_TOKEN_FILE': '', 'RESPONSES_BACKEND': 'native'}
            result = subprocess.run([sys.executable, '-c', 'import utils.globals as g; assert g.token_list == []'],
                cwd=work, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((work/'data/token.txt').exists())

    def test_explicit_missing_file_and_invalid_json_fail_startup(self):
        for content in (None, '{"accessToken":"PRIVATE_CREDENTIAL_VALUE'):
            with tempfile.TemporaryDirectory() as directory:
                work = Path(directory);(work/'version.txt').write_text('fixture')
                path = work/'account.json'
                if content is not None:path.write_text(content)
                env = {**os.environ, 'PYTHONPATH': str(ROOT), 'PYTHON_DOTENV_DISABLED': '1',
                       'CHATGPT_TOKEN_FILE': str(path)}
                result = subprocess.run([sys.executable, '-c', 'import utils.globals'],
                    cwd=work, env=env, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('PRIVATE_CREDENTIAL_VALUE', result.stdout + result.stderr)
                self.assertFalse((work/'data/token.txt').exists())

    def test_optional_configuration_generator_preserves_existing_files(self):
        spec = importlib.util.spec_from_file_location('configuration_fixture', ROOT/'scripts/configure.py')
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory);(work/'.env.example').write_text((ROOT/'.env.example').read_text())
            module.ROOT = work
            with contextlib.redirect_stdout(io.StringIO()) as output:module.main()
            self.assertNotIn('/tokens', output.getvalue())
            self.assertNotIn('local-', output.getvalue())
            self.assertEqual((work/'.env').stat().st_mode & 0o777, 0o600)
            before = (work/'.env').read_bytes()
            (work/'data/token.txt').write_text('preserved_token')
            with self.assertRaises(SystemExit):module.main()
            self.assertEqual((work/'.env').read_bytes(), before)
            self.assertEqual((work/'data/token.txt').read_text(), 'preserved_token')


class ConfigurationRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_management_routes_are_absent_and_cannot_mutate_credentials(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            from app import app
            from utils.configs import api_prefix
            import utils.globals as state
        prefix = '/' + api_prefix if api_prefix else ''
        before = list(state.token_list)
        for method, suffix in [('GET','/tokens'),('POST','/tokens/upload'),('POST','/tokens/clear'),
                ('POST','/tokens/error'),('GET','/tokens/add/synthetic'),('POST','/seed_tokens/clear')]:
            async def receive():return {'type':'http.request','body':b'text=synthetic','more_body':False}
            sent = []
            async def send(message):sent.append(message)
            path = prefix + suffix
            await app({'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':method,
                'scheme':'http','path':path,'raw_path':path.encode(),'query_string':b'',
                'headers':[],'client':('127.0.0.1',1),'server':('fixture',80),'root_path':''}, receive, send)
            self.assertEqual(next(m['status'] for m in sent if m['type']=='http.response.start'), 404)
        self.assertEqual(state.token_list, before)
        schema = app.openapi()
        self.assertEqual(schema['info']['title'], 'ChatGPT Web Gateway')
        self.assertFalse(any('/tokens' in p or '/seed_tokens' in p for p in schema['paths']))


if __name__ == '__main__':unittest.main(verbosity=2)
