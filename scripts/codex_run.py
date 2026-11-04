"""Run the real Codex CLI against this gateway without editing user config."""
import argparse
import json
import os
import subprocess
import urllib.request
import urllib.error


def provider():
    base = os.environ['OPENAI_BASE_URL'].rstrip('/')
    return {'name': 'ChatGPT Web Gateway', 'base_url': base, 'env_key': 'OPENAI_API_KEY',
            'wire_api': 'responses', 'supports_websockets': False,
            'request_max_retries': 0, 'stream_max_retries': 0,
            'model_catalog_url': base + '/codex/models'}


def toml_inline(value):
    return '{' + ','.join(json.dumps(k) + '=' + ('true' if v is True else 'false' if v is False else json.dumps(v)) for k, v in value.items()) + '}'


def check_catalog(config, model):
    """Fail before inference instead of letting Codex silently use fallback metadata."""
    request = urllib.request.Request(config['model_catalog_url'] + '?client_version=0.166.0',
        headers={'Authorization': 'Bearer ' + os.environ['OPENAI_API_KEY']})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            catalog = json.load(response)
        if not any(x.get('slug') == model for x in catalog['models']):
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError('Gateway catalog is unavailable or the selected model is absent; no inference started') from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('prompt')
    parser.add_argument('--cwd', default=os.getcwd())
    parser.add_argument('--model', default=os.getenv('CHATGPT_MODEL', 'gpt-6-thinking'))
    parser.add_argument('--sandbox', choices=('read-only', 'workspace-write'), default='read-only')
    parser.add_argument('--ephemeral', action='store_true')
    parser.add_argument('--isolate-config', action='store_true', help='Ignore user config for a disposable acceptance fixture')
    parser.add_argument('--websocket', action='store_true', help='Opt in to the gateway single-lane WebSocket transport')
    args = parser.parse_args()
    config = provider();config['supports_websockets'] = args.websocket
    check_catalog(config, args.model)
    command = ['codex', 'exec', '-C', args.cwd, '-m', args.model, '--sandbox', args.sandbox,
               '-c', 'model_provider="web_gateway"', '-c', 'model_providers.web_gateway=' + toml_inline(config)]
    if args.ephemeral:command.append('--ephemeral')
    if args.isolate_config:command.append('--ignore-user-config')
    command.append(args.prompt)
    return subprocess.call(command)


if __name__ == '__main__':
    raise SystemExit(main())
