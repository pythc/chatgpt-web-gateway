"""Run every offline suite in its own process with fresh synthetic state."""
from pathlib import Path
import os
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    failures = []
    total = 0
    # Config/globals use cwd for data and version.txt. Tests must not touch a
    # developer's configured account pool or load their local .env.
    with tempfile.TemporaryDirectory(prefix='web-gateway-tests-') as temp:
        work = Path(temp)
        (work / 'version.txt').write_text((ROOT / 'version.txt').read_text())
        env = {**os.environ, 'PYTHONPATH': str(ROOT), 'DEFAULT_MODEL': 'gpt-6-thinking',
               'DEFAULT_REASONING_EFFORT': 'high', 'API_PREFIX': 'test-only',
               'AUTHORIZATION': '', 'SCHEDULED_REFRESH': 'false', 'ENABLE_GATEWAY': 'false',
               'PYTHON_DOTENV_DISABLED': '1'}
        for name in list(env):
            if name.startswith(('RESPONSES_', 'CODEX_WEB_')) or name in ('CODEX_MODEL_CATALOG_FILE', 'CHATGPT_TOKEN_FILE'):
                env.pop(name)
        for suite in sorted((ROOT / 'tests').glob('test_*.py')):
            try:
                result = subprocess.run([sys.executable, str(suite)], cwd=work, env=env,
                                        capture_output=True, text=True, timeout=60)
            except subprocess.TimeoutExpired:
                failures.append(suite.name)
                print(f'FAIL {suite.name}: suite timeout', flush=True)
                continue
            output = result.stdout + result.stderr
            match = re.search(r'Ran (\d+) tests?', output)
            count = int(match.group(1)) if match else 0
            total += count
            passed = result.returncode == 0 and count > 0
            print(f'{"PASS" if passed else "FAIL"} {suite.name}: {count}', flush=True)
            if not passed:
                failures.append(suite.name)
                print(output[-6000:])
    print(f'{total} tests; {len(failures)} failed suites.')
    return bool(failures)


if __name__ == '__main__':
    sys.exit(main())
