"""Run inside a clean built image with --network none and no account mounts."""
from pathlib import Path
import os
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ROOT = Path('/app')


def main():
    for name in ('.env', 'data', 'diagnostics', '.git', 'TODO.md', 'docs/assets'):
        if (ROOT / name).exists():
            raise RuntimeError(f'Unexpected runtime image content: {name}')
    assert (ROOT / '.env.example').is_file()
    with tempfile.TemporaryDirectory(prefix='gateway-image-smoke-') as directory:
        work = Path(directory)
        (work / 'version.txt').write_bytes((ROOT / 'version.txt').read_bytes())
        env = {'PATH': os.environ['PATH'], 'PYTHONPATH': str(ROOT),
               'PYTHON_DOTENV_DISABLED': '1', 'API_PREFIX': 'smoke',
               'AUTHORIZATION': 'synthetic-smoke-key', 'CHATGPT_TOKEN_FILE': '',
               'ENABLE_GATEWAY': 'false', 'SCHEDULED_REFRESH': 'false'}
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        with (work / 'server.log').open('w+') as log:
            proc = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app:app',
                '--host', '127.0.0.1', '--port', str(port), '--lifespan', 'off'],
                cwd=work, env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        with urlopen(f'http://127.0.0.1:{port}/smoke/openapi.json', timeout=1) as response:
                            assert response.status == 200
                        break
                    except (URLError, TimeoutError):
                        if proc.poll() is not None or time.monotonic() >= deadline:
                            log.seek(0)
                            raise RuntimeError('Synthetic image startup failed: ' + log.read()[-2000:])
                        time.sleep(0.1)
                for path in ('/smoke/tokens', '/smoke/tokens/upload'):
                    try:
                        urlopen(f'http://127.0.0.1:{port}{path}', timeout=2)
                    except HTTPError as error:
                        assert error.code == 404, error.code
                    else:
                        raise RuntimeError('Removed account route is available')
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
    print('Image packaging and synthetic HTTP startup passed; no inference.')


if __name__ == '__main__':
    main()
