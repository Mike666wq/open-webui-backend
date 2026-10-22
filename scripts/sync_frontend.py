#!/usr/bin/env python3
"""Resolve a published frontend from main; no cross-repository write token needed."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request

FRONTEND = 'Mike666wq/open-webui-frontend'
API = 'https://api.github.com'


def request(url, data=None):
    headers = {'User-Agent': 'open-webui-pipeline', 'Accept': 'application/vnd.github+json'}
    # Never forward the repository token to release asset/CDN hosts.
    if url.startswith(API + '/'):
        headers['Authorization'] = 'Bearer ' + os.environ['GH_TOKEN']
    if data is not None:
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, headers=headers, data=None if data is None else json.dumps(data).encode())
    with urllib.request.urlopen(req, timeout=60) as response:
        value = response.read(16 * 1024 * 1024 + 1)
    if len(value) > 16 * 1024 * 1024:
        raise ValueError('Metadata exceeds size limit')
    return value


def api(path, data=None):
    raw = request(API + path, data)
    return json.loads(raw) if raw else None


def make_lock(current, sha, manifest_bytes, checksums):
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('Invalid frontend source SHA')
    manifest = json.loads(manifest_bytes)
    for name, expected in [('schema', 1), ('sourceRepository', FRONTEND), ('sourceCommit', sha),
                           ('upstreamVersion', current['upstreamVersion']), ('upstreamCommit', current['upstreamCommit'])]:
        if manifest.get(name) != expected:
            raise ValueError('Incompatible frontend manifest: ' + name)
    hashes = {}
    for line in checksums.decode().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64}) [ *](open-webui-frontend\.zip|frontend-manifest\.json)', line)
        if not match or match[2] in hashes:
            raise ValueError('Invalid or duplicate release checksum')
        hashes[match[2]] = match[1]
    if set(hashes) != {'open-webui-frontend.zip', 'frontend-manifest.json'}:
        raise ValueError('Release checksums are incomplete')
    if hashlib.sha256(manifest_bytes).hexdigest() != hashes['frontend-manifest.json']:
        raise ValueError('Frontend manifest checksum mismatch')
    return dict(schema=1, sourceRepository=FRONTEND, sourceCommit=sha,
                upstreamVersion=manifest['upstreamVersion'], upstreamCommit=manifest['upstreamCommit'],
                archiveSha256=hashes['open-webui-frontend.zip'], manifestSha256=hashes['frontend-manifest.json'],
                archiveUrl=f'https://github.com/{FRONTEND}/releases/download/frontend-{sha[:12]}/open-webui-frontend.zip')


def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()


def main():
    if os.environ['GITHUB_REF'] != 'refs/heads/main':
        raise ValueError('Pipeline synchronization must run on main')
    sha = git('ls-remote', f'https://github.com/{FRONTEND}.git', 'refs/heads/main').split()[0]
    tag = 'frontend-' + sha[:12]
    try:
        release = api(f'/repos/{FRONTEND}/releases/tags/{tag}')
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        if '--check' in sys.argv:
            raise ValueError('Frontend main is not published yet; run frontend-sync after its release succeeds')
        print('Frontend main has not passed its release pipeline yet. Downstream builds wait for the next sync.')
        return
    if release['draft'] or release['prerelease'] or release['tag_name'] != tag:
        raise ValueError('Frontend release is not a stable published bundle')
    prefix = f'https://github.com/{FRONTEND}/releases/download/{tag}/'
    assets = {asset['name']: asset['browser_download_url'] for asset in release['assets']}
    for name in ('open-webui-frontend.zip', 'frontend-manifest.json', 'SHA256SUMS'):
        if assets.get(name) != prefix + name:
            raise ValueError('Missing or unexpected frontend release asset: ' + name)
    current = json.loads(Path('frontend.lock.json').read_text())
    lock = make_lock(current, sha, request(assets['frontend-manifest.json']), request(assets['SHA256SUMS']))
    # An older queued sync must never roll a consumer back after frontend main advances.
    if git('ls-remote', f'https://github.com/{FRONTEND}.git', 'refs/heads/main').split()[0] != sha:
        if '--check' in sys.argv:
            raise ValueError('Frontend main advanced; retry after synchronization')
        print('Frontend main advanced during resolution; waiting for its next successful release.')
        return
    if '--check' in sys.argv:
        if lock != current:
            raise ValueError('Stale frontend.lock.json; run frontend-sync before building')
        print('Verified latest published frontend: ' + sha)
        return
    changed = lock != current
    if changed:
        Path('frontend.lock.json').write_text(json.dumps(lock, indent=2) + '\n')
        git('config', 'user.name', 'github-actions[bot]')
        git('config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
        git('add', 'frontend.lock.json')
        git('commit', '-m', f'chore: consume verified frontend {sha[:12]}')
        # Non-force push rejects concurrent edits instead of overwriting them.
        git('push', 'origin', 'HEAD:main')
    head = git('rev-parse', 'HEAD')
    repo = os.environ['GITHUB_REPOSITORY']
    workflow = os.environ['DOWNSTREAM_WORKFLOW']
    if git('ls-remote', 'origin', 'refs/heads/main').split()[0] != head:
        print('Consumer main advanced; the newer push/scheduled sync will handle it.')
        return
    # Android release preparation creates a version-bump commit. It belongs to
    # the existing run, and must not start a new release on every scheduled sync.
    marker = re.search(r'^openwebui-release-run:([0-9]+)$', git('log', '-1', '--format=%B'), re.MULTILINE)
    if workflow == 'android.yml' and marker and not changed:
        print('Release preparation already belongs to run ' + marker[1] + '; retry that run if needed.')
        return
    runs = api(f'/repos/{repo}/actions/workflows/{workflow}/runs?head_sha={head}&event=workflow_dispatch&per_page=1')['workflow_runs']
    # Recover a commit whose dispatch failed; never repeatedly rebuild failed runs on a timer.
    force = os.environ.get('PIPELINE_FORCE') == 'true'
    if not runs or force:
        api(f'/repos/{repo}/actions/workflows/{workflow}/dispatches', {'ref': 'main'})
        message = f'Dispatched {workflow} for {head}; frontend {sha}.'
    else:
        message = f'Already dispatched {workflow} for {head}: ' + runs[0]['html_url']
    print(message)
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
        summary.write(message + '\n')


if __name__ == '__main__':
    main()
