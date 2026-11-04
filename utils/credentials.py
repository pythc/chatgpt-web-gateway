"""Read operator-managed credentials without modifying or displaying them."""
import json
from pathlib import Path


def read_token_file(path):
    text = Path(path).read_text(encoding='utf-8-sig').strip()
    if not text:
        return []
    if text.startswith(('{', '[')):
        try:
            value = json.loads(text)
        except ValueError:
            raise ValueError('Credential file contains invalid JSON') from None
        if isinstance(value, dict):
            values = [value.get('accessToken')]
        elif isinstance(value, list):
            values = value
        else:
            values = [None]
    else:
        values = [line.strip() for line in text.splitlines()
                  if line.strip() and not line.lstrip().startswith('#')]
    tokens = []
    for value in values:
        if not isinstance(value, str) or not value.strip() or any(c.isspace() for c in value.strip()):
            raise ValueError('Use complete tokens, one per line, a JSON string array, or session JSON with accessToken')
        token = value.strip()
        if token not in tokens:
            tokens.append(token)
    return tokens
