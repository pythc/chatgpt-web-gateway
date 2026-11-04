"""Generate local-only configuration; never overwrite it or print credentials."""
from pathlib import Path
import os
import secrets

ROOT = Path(__file__).resolve().parents[1]


def main():
    text = (ROOT / '.env.example').read_text()
    text = text.replace('replace-with-random-prefix', 'local-' + secrets.token_hex(16))
    text = text.replace('replace-with-random-client-key', 'local-' + secrets.token_hex(32))
    try:
        fd = os.open(ROOT / '.env', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit('.env already exists; kept unchanged.')
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)
    (ROOT / 'data').mkdir(mode=0o700, exist_ok=True)
    print('Created .env (0600) and data/. Credentials were not printed.')
    print('Add your ChatGPT access token or session JSON to data/token.txt (0600).')
    print('Next: docker compose up -d --build')


if __name__ == '__main__':
    main()
