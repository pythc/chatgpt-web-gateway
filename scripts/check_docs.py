"""Check tracked Markdown destinations without network access or private files."""
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def anchors(text):
    result = set(re.findall(r'(?:id|name)=["\']([^"\']+)', text))
    used = set()
    for title in re.findall(r'^#{1,6}\s+(.+?)\s*#*$', text, re.M):
        title = re.sub(r'<[^>]*>', '', title).lower()
        slug = re.sub(r'[^\w\-\s]', '', title).replace(' ', '-')
        candidate, index = slug, 0
        while candidate in used:
            index += 1
            candidate = f'{slug}-{index}'
        used.add(candidate)
        result.add(candidate)
    return result


def main():
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    failures, count = [], 0
    for name in tracked:
        if not name.endswith('.md'):
            continue
        source = ROOT / name
        text = source.read_text()
        # Code examples are not Markdown links.
        text = re.sub(r'^```.*?^```\s*$', '', text, flags=re.M | re.S)
        destinations = re.findall(r'!?\[[^\]]*\]\(([^\s)]+)(?:\s+"[^"]*")?\)', text)
        destinations += re.findall(r'(?:href|src)=["\']([^"\']+)', text)
        for raw in destinations:
            url = urlsplit(raw.strip('<>'))
            if url.scheme or url.netloc:
                continue
            count += 1
            target = (source.parent / unquote(url.path)).resolve() if url.path else source
            if not target.is_relative_to(ROOT) or not target.exists():
                failures.append(f'{name}: missing local destination {raw}')
            elif url.fragment and target.suffix == '.md' and unquote(url.fragment) not in anchors(target.read_text()):
                failures.append(f'{name}: missing heading {raw}')
    for failure in failures:
        print(failure, file=sys.stderr)
    print(f'{count} local documentation links/anchors; {len(failures)} errors.')
    return bool(failures)


if __name__ == '__main__':
    sys.exit(main())
