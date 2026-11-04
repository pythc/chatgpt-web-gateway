"""Bounded, out-of-process validation of custom-tool grammars; no execution."""
import asyncio
import json
import re
import sys
from api.client_tools import fail

WORKER = '''import json,sys
try:
 x=json.load(sys.stdin)
 if x['syntax']=='regex':
  import regex
  if x.get('compile_only'):regex.compile(x['definition']);ok=True
  else:ok=regex.fullmatch(x['definition'],x['input'],timeout=0.5) is not None
 else:
  from lark import Lark
  # Codex apply_patch uses the OpenAI Lark dialect's nullable line
  # terminal. Python Lark rejects zero-width terminals; an optional
  # nonempty terminal accepts the same language, including empty lines.
  parser=Lark(x['definition'],parser='earley')
  if x.get('compile_only'):ok=True
  else:parser.parse(x['input']);ok=True
 print('ok' if ok else 'invalid')
except Exception:print('invalid')
'''


def normalize_lark(definition):
    # Match full literals so text inside quoted strings, comments or another
    # regex is never rewritten as grammar syntax.
    tokens = r'"(?:\\.|[^"\\])*"|//[^\n]*|/(?:\\.|[^/\\\n])*/[imsulx]*'
    return re.sub(tokens, lambda m: '/(.+)/?' if m.group() == '/(.*)/' else m.group(), definition)


def check_format(fmt):
    if not isinstance(fmt, dict) or fmt.get('type') not in ('text', 'grammar'):
        fail(400, 'invalid_custom_tool_format', 'Custom format must be text or grammar')
    if fmt['type'] == 'grammar' and (fmt.get('syntax') not in ('lark', 'regex') or
            not isinstance(fmt.get('definition'), str) or not 0 < len(fmt['definition']) <= 65536):
        fail(400, 'invalid_custom_tool_grammar', 'Unsupported or oversized grammar')
    if fmt.get('syntax') == 'lark' and any(not line.strip().startswith('%import common.')
            for line in fmt['definition'].splitlines() if line.strip().startswith('%import')):
        fail(400, 'unsupported_grammar_import', 'Only built-in common grammar imports are supported')


async def validate_input(fmt, value, compile_only=False):
    if not isinstance(value, str) or len(value) > 1024 * 1024:
        fail(502, 'invalid_custom_tool_input', 'Custom input must be bounded text')
    if fmt.get('type') != 'grammar':
        return
    if fmt.get('syntax') == 'lark':
        fmt = {**fmt, 'definition': normalize_lark(fmt['definition'])}
    proc = await asyncio.create_subprocess_exec(sys.executable, '-c', WORKER,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(json.dumps({**fmt, 'input': value, 'compile_only': compile_only}).encode()), 3)
        if proc.returncode or out.strip() != b'ok':
            fail(400 if compile_only else 502, 'invalid_custom_tool_grammar' if compile_only else 'custom_tool_grammar_mismatch', 'Custom grammar validation failed')
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
