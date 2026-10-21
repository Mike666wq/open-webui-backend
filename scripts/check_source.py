"""Check the backend source snapshot without importing or starting the app."""

import ast
import hashlib
import json
import re
import sys
from pathlib import Path


def check_source(root: Path) -> dict:
    upstream = json.loads((root / 'backend.upstream.json').read_text())
    frontend = json.loads((root / 'frontend.lock.json').read_text())
    package = json.loads((root / 'package.json').read_text())
    if upstream.get('schema') != 1 or upstream.get('sourceRepository') != 'Mike666wq/open-webui-backend':
        raise ValueError('Unexpected backend repository identity')
    if upstream.get('upstreamRepository') != 'open-webui/open-webui':
        raise ValueError('Unexpected upstream repository')
    if package.get('version') != upstream.get('upstreamVersion'):
        raise ValueError('Backend runtime version differs from upstream lock')
    if frontend.get('schema') != 1 or frontend.get('sourceRepository') != 'Mike666wq/open-webui-frontend':
        raise ValueError('Unexpected frontend lock identity')
    for name in ('upstreamVersion', 'upstreamCommit'):
        if upstream.get(name) != frontend.get(name):
            raise ValueError('Frontend/backend compatibility mismatch: ' + name)
    for name in ('upstreamCommit',):
        if not re.fullmatch(r'[0-9a-f]{40}', upstream.get(name, '')):
            raise ValueError('Invalid upstream commit')
    if not re.fullmatch(r'[0-9a-f]{40}', frontend.get('sourceCommit', '')):
        raise ValueError('Invalid frontend commit')
    for name in ('archiveSha256', 'manifestSha256'):
        if not re.fullmatch(r'[0-9a-f]{64}', frontend.get(name, '')):
            raise ValueError('Invalid frontend digest: ' + name)
    expected_url = (
        'https://github.com/Mike666wq/open-webui-frontend/releases/download/'
        f'frontend-{frontend["sourceCommit"][:12]}/open-webui-frontend.zip'
    )
    if frontend.get('archiveUrl') != expected_url:
        raise ValueError('Frontend archive URL is not the locked release')
    dockerfile = (root / 'Dockerfile').read_text()
    if 'FROM ' + upstream['baseImage'] not in dockerfile:
        raise ValueError('Dockerfile does not use the locked runtime image')
    if not re.fullmatch(
        r'ghcr\.io/open-webui/open-webui:v[^\s@]+@sha256:[0-9a-f]{64}', upstream['baseImage']
    ):
        raise ValueError('Runtime image must use an immutable digest')
    for name, expected in upstream['runtimeRequirementsSha256'].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise ValueError('Requirements changed: update the image dependency layer before publishing: ' + name)
    required = (
        'LICENSE', 'LICENSE_NOTICE', 'LICENSE_HISTORY', 'CONTRIBUTOR_LICENSE_AGREEMENT',
        'CHANGELOG.md', 'backend/start.sh', 'backend/open_webui/main.py',
        'backend/open_webui/alembic.ini', 'backend/open_webui/migrations/env.py',
        'static/static/favicon.png',
    )
    for name in required:
        if not (root / name).is_file():
            raise ValueError('Required runtime/license source is missing: ' + name)
    paths = sorted((root / 'backend').rglob('*.py')) + sorted((root / 'scripts').glob('*.py'))
    paths += sorted((root / 'tests').glob('*.py'))
    for path in paths:
        ast.parse(path.read_text(), filename=str(path))
    return {
        'upstreamVersion': upstream['upstreamVersion'],
        'upstreamCommit': upstream['upstreamCommit'],
        'frontendCommit': frontend['sourceCommit'],
        'pythonFilesParsed': len(paths),
    }


if __name__ == '__main__':
    if sys.version_info < (3, 11):
        raise SystemExit('Source checks require Python 3.11 or newer; the backend runtime requires 3.11 or 3.12.')
    print(json.dumps(check_source(Path(__file__).resolve().parents[1]), indent=2))

