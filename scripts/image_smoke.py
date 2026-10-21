#!/usr/bin/env python3
"""Smoke-test an already pulled image with disposable data and no external network."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener


ROOT = Path(__file__).resolve().parents[1]

# Run inside the exact candidate, without needing to expose metadata publicly.
VERIFY_CONTAINER = r'''
import hashlib, json, pathlib, re, sys
expected = json.load(sys.stdin)
lock = json.loads(pathlib.Path('/app/frontend.lock.json').read_text())
assert lock == expected, 'Image frontend lock differs from checked-out source'
root = pathlib.Path('/app/build').resolve()
raw = (root / 'frontend-manifest.json').read_bytes()
assert hashlib.sha256(raw).hexdigest() == lock['manifestSha256'], 'Manifest hash mismatch'
manifest = json.loads(raw)
for key in ('sourceRepository', 'sourceCommit', 'upstreamVersion', 'upstreamCommit'):
    assert manifest[key] == lock[key], 'Manifest provenance differs: ' + key
assert manifest['schema'] == 1 and manifest['entry'] == 'index.html'
actual = {str(path.relative_to(root)) for path in root.rglob('*') if path.is_file()}
expected_paths = {item['path'] for item in manifest['files']} | {'frontend-manifest.json'}
assert actual == expected_paths, 'Unexpected or missing frontend files in image'
samples = {}
for item in manifest['files']:
    path = root / item['path']
    assert path.resolve().is_relative_to(root), 'Asset escapes frontend directory'
    assert not path.is_symlink(), 'Symlink in frontend image'
    data = path.read_bytes()
    assert len(data) == item['size'], 'Asset size differs: ' + item['path']
    assert hashlib.sha256(data).hexdigest() == item['sha256'], 'Asset hash differs: ' + item['path']
    suffix = path.suffix
    if suffix in ('.js', '.css') and (
        suffix not in samples or item['size'] < samples[suffix]['size']
    ):
        samples[suffix] = item
index = next(item for item in manifest['files'] if item['path'] == 'index.html')
deep_link = '/c/00000000-0000-4000-8000-000000000001'
assert any(re.fullmatch(pattern, deep_link) for pattern in manifest['pageRoutes']), 'Chat route absent'
assert '.js' in samples and '.css' in samples, 'Frontend JS/CSS missing'
print(json.dumps({
    'sourceCommit': manifest['sourceCommit'], 'fileCount': len(manifest['files']),
    'index': index, 'samples': list(samples.values()), 'deepLink': deep_link,
}))
'''


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def docker(*args: str, stdin: str | None = None, timeout: int = 60) -> str:
    result = subprocess.run(
        ['docker', *args], input=stdin, text=True, capture_output=True, timeout=timeout
    )
    if result.returncode:
        raise RuntimeError(f'docker {args[0]} failed: {result.stderr.strip()}')
    return result.stdout.strip()


def get(opener, base_url: str, path: str, expected_status: int = 200) -> bytes:
    if not path.startswith('/') or path.startswith('//'):
        raise ValueError('Only relative resource paths are permitted')
    try:
        with opener.open(base_url + path, timeout=10) as response:
            status, data = response.status, response.read(64 * 1024 * 1024 + 1)
    except HTTPError as error:
        status, data = error.code, error.read(1024 * 1024)
    if status != expected_status:
        raise RuntimeError(f'{path}: expected HTTP {expected_status}, received {status}')
    if len(data) > 64 * 1024 * 1024:
        raise RuntimeError(f'{path}: unexpectedly large response')
    return data


def check_asset(data: bytes, metadata: dict) -> None:
    if len(data) != metadata['size'] or hashlib.sha256(data).hexdigest() != metadata['sha256']:
        raise RuntimeError('Served frontend bytes differ from the locked manifest: ' + metadata['path'])


def smoke(image: str, timeout: int, output: Path) -> dict:
    lock = json.loads((ROOT / 'frontend.lock.json').read_text())
    name = 'owui-smoke-' + secrets.token_hex(6)
    network, volume = name + '-network', name + '-data'
    report = {'image': image, 'status': 'failed', 'checks': []}
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    network_created = volume_created = container_created = False
    opener = build_opener(ProxyHandler({}), NoRedirect())
    # --internal networks silently ignore published ports (moby/moby#53256),
    # so the smoke checks could never reach the container. Use a normal bridge
    # network (ports publish to the host loopback) and keep the container
    # offline by blackholing its outbound route: DNS resolves to a link-local
    # address that never answers, and there is no usable upstream gateway.
    subnet = f'169.254.{secrets.randbelow(200) + 1}.0/24'
    gateway = subnet.rsplit('.', 1)[0] + '.1'
    try:
        docker(
            'network', 'create', '--driver', 'bridge',
            '--subnet', subnet, '--gateway', gateway,
            '--opt', 'com.docker.network.bridge.ip_masq=false',
            network,
        )
        network_created = True
        docker('volume', 'create', volume)
        volume_created = True
        # A timed-out docker run may already have created a container. Always
        # attempt named-container cleanup once creation has been attempted.
        container_created = True
        docker(
            'run', '--detach', '--pull=never', '--platform=linux/amd64',
            '--name', name, '--network', network,
            '--publish', '127.0.0.1::8080',
            '--dns', '169.254.169.254',
            '--mount', f'type=volume,source={volume},target=/app/backend/data',
            '--env', 'DATA_DIR=/app/backend/data',
            '--env', 'HOST=0.0.0.0', '--env', 'PORT=8080',
            '--env', 'DATABASE_URL=sqlite:////app/backend/data/webui.db',
            '--env', 'WEBUI_AUTH=true', '--env', 'ENABLE_SIGNUP=false',
            '--env', 'ENABLE_PERSISTENT_CONFIG=false', '--env', 'OFFLINE_MODE=true',
            '--env', 'ENABLE_OLLAMA_API=false', '--env', 'ENABLE_OPENAI_API=false',
            '--env', 'ENABLE_VERSION_UPDATE_CHECK=false',
            '--env', 'RAG_EMBEDDING_MODEL_AUTO_UPDATE=false',
            '--env', 'RAG_RERANKING_MODEL_AUTO_UPDATE=false',
            '--env', 'WEBUI_SECRET_KEY=' + secrets.token_hex(32),
            image,
        )
        ports = json.loads(docker('inspect', '--format', '{{json .NetworkSettings.Ports}}', name))
        bindings = ports.get('8080/tcp')
        if not bindings:
            raise RuntimeError(f'Port 8080 was not published to the host: {ports}')
        if len(bindings) != 1 or bindings[0]['HostIp'] != '127.0.0.1':
            raise RuntimeError('Smoke container must only publish a localhost port')
        base_url = 'http://127.0.0.1:' + bindings[0]['HostPort']
        deadline = time.monotonic() + timeout
        last_error = 'Server has not started'
        while time.monotonic() < deadline:
            if docker('inspect', '--format', '{{.State.Running}}', name) != 'true':
                raise RuntimeError('Smoke container exited before becoming healthy')
            try:
                health = json.loads(get(opener, base_url, '/health'))
                if health.get('status') is True:
                    break
                last_error = 'Health status was not true'
            except (URLError, TimeoutError, OSError, ValueError, RuntimeError) as error:
                last_error = str(error)
            time.sleep(2)
        else:
            raise RuntimeError(f'Health timeout after {timeout}s: {last_error}')
        report['checks'].append('health')
        version = json.loads(get(opener, base_url, '/api/version'))
        if version.get('version') != lock['upstreamVersion']:
            raise RuntimeError('Backend version differs from the locked official baseline')
        report['checks'].append('backend-version')
        identity = json.loads(docker(
            'exec', '--interactive', name, 'python', '-c', VERIFY_CONTAINER,
            stdin=json.dumps(lock), timeout=180,
        ))
        report.update(identity)
        report['checks'].append('all-image-assets-and-frontend-identity')
        for route in ('/', identity['deepLink']):
            check_asset(get(opener, base_url, route), identity['index'])
        report['checks'].append('index-and-chat-deep-link')
        for asset in identity['samples']:
            check_asset(get(opener, base_url, '/' + quote(asset['path'], safe='/')), asset)
        report['checks'].append('served-javascript-and-css')
        for route in (
            '/_app/immutable/missing-' + name + '.js',
            '/static/missing-' + name + '.css',
            '/not-a-supported-page-' + name,
        ):
            get(opener, base_url, route, expected_status=404)
        report['checks'].append('missing-static-and-unknown-page-404')
        report['status'] = 'passed'
    except Exception as error:
        report['error'] = str(error)
        raise
    finally:
        if container_created:
            for filename, args in (
                ('container.log', ('logs', name)),
                ('state.json', ('inspect', '--format', '{{json .State}}', name)),
            ):
                try:
                    result = subprocess.run(['docker', *args], text=True, capture_output=True, timeout=30)
                    (output / filename).write_text(result.stdout + result.stderr)
                except Exception as error:
                    report.setdefault('cleanupWarnings', []).append(str(error))
        cleanup = []
        if container_created:
            cleanup.append(('container', 'rm', '--force', '--volumes', name))
        if volume_created:
            cleanup.append(('volume', 'rm', volume))
        if network_created:
            cleanup.append(('network', 'rm', network))
        for args in cleanup:
            try:
                docker(*args)
            except Exception as error:
                report.setdefault('cleanupWarnings', []).append(str(error))
        report['elapsedSeconds'] = round(time.monotonic() - started, 2)
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, help='Already-pulled image, preferably image@sha256:...')
    parser.add_argument('--timeout', type=int, default=300, help='Startup deadline in seconds')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/image-smoke')
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    try:
        report = smoke(args.image, args.timeout, args.output)
    except Exception as error:
        print(f'Image smoke failed: {error}', file=sys.stderr)
        return 1
    print(json.dumps({key: report[key] for key in ('status', 'sourceCommit', 'fileCount', 'elapsedSeconds')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
