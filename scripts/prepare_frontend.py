#!/usr/bin/env python3
"""Install only a verified, pinned frontend into this repository's .build/frontend."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.request

from frontend_archive import extract_archive, read_json, verify_directory

REPOSITORY = 'Mike666wq/open-webui-frontend'
UPSTREAM_VERSION = '0.12.2'
UPSTREAM_COMMIT = '6a18585ff4c2e5f6805f682222cfbc46dc669ef8'
MAX_ARCHIVE_BYTES = 1024**3
DOWNLOAD_CHUNK_BYTES = 1024**2
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def validate_lock(value):
    if (
        not isinstance(value, dict)
        or type(value.get('schema')) is not int
        or value['schema'] != 1
        or value.get('sourceRepository') != REPOSITORY
        or not isinstance(value.get('sourceCommit'), str)
        or not re.fullmatch(r'[0-9a-f]{40}', value['sourceCommit'])
        or value.get('upstreamVersion') != UPSTREAM_VERSION
        or value.get('upstreamCommit') != UPSTREAM_COMMIT
        or any(not isinstance(value.get(name), str) or not re.fullmatch(r'[0-9a-f]{64}', value[name]) for name in ('archiveSha256', 'manifestSha256'))
    ):
        raise ValueError('Invalid pinned frontend identity or digest')
    expected_url = f'https://github.com/{REPOSITORY}/releases/download/frontend-{value["sourceCommit"][:12]}/open-webui-frontend.zip'
    if value.get('archiveUrl') != expected_url:
        raise ValueError('Frontend archive URL must identify the pinned repository release asset')
    return value


def sha256_file(filename):
    digest = hashlib.sha256()
    with open(filename, 'rb') as stream:
        for chunk in iter(lambda: stream.read(DOWNLOAD_CHUNK_BYTES), b''):
            digest.update(chunk)
    return digest.hexdigest()


def download_archive(url, destination):
    request = urllib.request.Request(url, headers={'User-Agent': 'open-webui-backend-frontend-preparer/1'})
    with urllib.request.urlopen(request, timeout=300) as response:
        content_length = response.headers.get('Content-Length')
        if content_length is not None and int(content_length) > MAX_ARCHIVE_BYTES:
            raise ValueError('Frontend archive exceeds 1 GiB')
        length = 0
        with open(destination, 'xb') as output:
            while True:
                chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                length += len(chunk)
                if length > MAX_ARCHIVE_BYTES:
                    raise ValueError('Frontend archive exceeds 1 GiB')
                output.write(chunk)


def prepare_frontend(project_root=PROJECT_ROOT, archive=None):
    project_root = Path(project_root).resolve()
    lock_path = project_root / 'frontend.lock.json'
    if lock_path.is_symlink() or not lock_path.is_file():
        raise ValueError('frontend.lock.json must be a regular file in the backend repository')
    lock = validate_lock(read_json(lock_path.read_bytes()))
    parent = project_root / '.build'
    destination = parent / 'frontend'
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise ValueError('Frontend build parent must be a real directory')
    if destination.is_symlink():
        raise ValueError('Frontend output cannot be a symlink')
    if destination.exists():
        if not destination.is_dir():
            raise ValueError('Frontend output must be a directory')
        if any(destination.iterdir()):
            try:
                result = verify_directory(destination, lock)
            except (ValueError, OSError, KeyError, TypeError) as error:
                raise ValueError('Refusing to overwrite a nonempty unverified frontend output') from error
            return {**result, 'destination': str(destination), 'cached': True}
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.frontend-', dir=parent) as temporary:
        temporary_path = Path(temporary)
        if archive is None:
            archive_path = temporary_path / 'frontend.zip'
            download_archive(lock['archiveUrl'], archive_path)
        else:
            archive_path = Path(archive)
        if archive_path.is_symlink() or not archive_path.is_file():
            raise ValueError('Frontend archive must be a regular file')
        if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError('Frontend archive exceeds 1 GiB')
        if sha256_file(archive_path) != lock['archiveSha256']:
            raise ValueError('Frontend archive SHA256 does not match frontend.lock.json')
        staged = temporary_path / 'frontend'
        result = extract_archive(archive_path, staged, lock)
        # Validate the target again after downloading/extracting. A populated or
        # symlinked destination must never be removed to make installation fit.
        if parent.is_symlink() or destination.is_symlink():
            raise ValueError('Frontend output cannot be a symlink')
        if destination.exists():
            if not destination.is_dir() or any(destination.iterdir()):
                raise ValueError('Refusing to overwrite a nonempty frontend output')
            destination.rmdir()
        os.replace(staged, destination)
    return {**result, 'destination': str(destination), 'cached': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, help='Verify a local release ZIP instead of downloading it')
    args = parser.parse_args()
    try:
        print(json.dumps(prepare_frontend(archive=args.archive)))
    except Exception as error:
        print(f'Frontend preparation failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
