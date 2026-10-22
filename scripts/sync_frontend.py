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


def latest_frontend():
    """Select by main history, not Release creation order or latest (which may be an APK)."""
    candidates = {}
    for page in range(1, 11):
        releases = api(f'/repos/{FRONTEND}/releases?per_page=100&page={page}')
        for release in releases:
            tag = release['tag_name']
            if not release['draft'] and not release['prerelease'] and re.fullmatch(r'frontend-[0-9a-f]{12}', tag):
                candidates[tag[9:]] = release
        if len(releases) < 100:
            break
    else:
        raise ValueError('Too many releases to resolve safely')
    if not candidates:
        return None
    for page in range(1, 101):
        commits = api(f'/repos/{FRONTEND}/commits?sha=main&per_page=100&page={page}')
        for commit in commits:
            sha = commit['sha']
            release = candidates.get(sha[:12])
            if release:
                return sha, release
        if len(commits) < 100:
            break
    raise ValueError('No published frontend bundle belongs to main history')


def product_changed(before, after):
    """Ignore documentation/config-only commits when deciding whether to rebuild an image."""
    files = git('diff', '--name-only', before, after).splitlines()
    return any(not (name.startswith(('docs/', '.devcontainer/')) or name.lower().endswith('.md') or
                    Path(name).name.startswith(('LICENSE', 'COPYING', 'NOTICE'))) for name in files)


def main():
    if os.environ['GITHUB_REF'] != 'refs/heads/main':
        raise ValueError('Pipeline synchronization must run on main')
    resolved = latest_frontend()
    if resolved is None:
        if '--check' in sys.argv:
            raise ValueError('No verified frontend has been published')
        print('Waiting for the first verified frontend bundle.')
        return
    sha, release = resolved
    tag = 'frontend-' + sha[:12]
    if release['draft'] or release['prerelease'] or release['tag_name'] != tag:
        raise ValueError('Frontend release is not a stable published bundle')
    prefix = f'https://github.com/{FRONTEND}/releases/download/{tag}/'
    assets = {asset['name']: asset['browser_download_url'] for asset in release['assets']}
    for name in ('open-webui-frontend.zip', 'frontend-manifest.json', 'SHA256SUMS'):
        if assets.get(name) != prefix + name:
            raise ValueError('Missing or unexpected frontend release asset: ' + name)
    current = json.loads(Path('frontend.lock.json').read_text())
    lock = make_lock(current, sha, request(assets['frontend-manifest.json']), request(assets['SHA256SUMS']))
    # Re-resolve published products, so a docs/native-only main commit never blocks consumers.
    if latest_frontend()[0] != sha:
        if '--check' in sys.argv:
            raise ValueError('Published frontend advanced; retry after synchronization')
        print('Published frontend advanced during resolution; waiting for the next sync.')
        return
    if '--check' in sys.argv:
        if lock != current:
            raise ValueError('Stale frontend.lock.json; run frontend-sync before building')
        print('Verified latest published frontend product: ' + sha)
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
    runs = api(f'/repos/{repo}/actions/workflows/{workflow}/runs?event=workflow_dispatch&branch=main&per_page=1')['workflow_runs']
    previous = runs[0]['head_sha'] if runs else None
    needs_build = changed or not previous or product_changed(previous, head)
    # Recover a commit whose dispatch failed; never repeatedly rebuild failed runs on a timer.
    force = os.environ.get('PIPELINE_FORCE') == 'true'
    if needs_build or force:
        api(f'/repos/{repo}/actions/workflows/{workflow}/dispatches', {'ref': 'main'})
        message = f'Dispatched {workflow} for {head}; frontend {sha}.'
    else:
        message = f'Already dispatched {workflow} for {head}: ' + runs[0]['html_url']
    print(message)
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
        summary.write(message + '\n')


if __name__ == '__main__':
    main()
