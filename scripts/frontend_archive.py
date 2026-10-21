"""Verify and extract a pinned static frontend without accepting partial bundles."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import stat
import zipfile

MANIFEST = 'frontend-manifest.json'
MAX_BYTES = 2 * 1024**3
MAX_FILES = 40_000
MAX_MANIFEST_BYTES = 16 * 1024**2
IDENTITY_FIELDS = ('sourceRepository', 'sourceCommit', 'upstreamVersion', 'upstreamCommit')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate JSON key: {key}')
        result[key] = value
    return result


def read_json(data):
    return json.loads(data, object_pairs_hook=_unique_object)


def safe_path(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2048
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(character in value for character in '\\?#%:')
    ):
        raise ValueError('Invalid frontend asset path')
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or any(part in ('', '.', '..') for part in value.split('/')):
        raise ValueError('Frontend asset path escapes its root')
    return value


def load_manifest(data, lock):
    if len(data) > MAX_MANIFEST_BYTES:
        raise ValueError('Frontend manifest exceeds the size limit')
    if hashlib.sha256(data).hexdigest() != lock['manifestSha256']:
        raise ValueError('Frontend manifest SHA256 does not match lock')
    manifest = read_json(data)
    if not isinstance(manifest, dict):
        raise ValueError('Invalid frontend manifest')
    for name in IDENTITY_FIELDS:
        if manifest.get(name) != lock[name]:
            raise ValueError('Frontend manifest identity mismatch: ' + name)
    if type(manifest.get('schema')) is not int or manifest['schema'] != 1 or manifest.get('entry') != 'index.html':
        raise ValueError('Unsupported frontend manifest')
    routes = manifest.get('pageRoutes')
    if not isinstance(routes, list) or not routes or len(routes) > 1000:
        raise ValueError('Missing frontend page routes')
    for route in routes:
        if not isinstance(route, str) or len(route) > 1024 or not route.startswith('^') or not route.endswith('$'):
            raise ValueError('Frontend page routes must be bounded regular expressions')
        re.compile(route)
    records = manifest.get('files')
    if not isinstance(records, list) or not records or len(records) > MAX_FILES:
        raise ValueError('Invalid frontend file inventory')
    files = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError('Invalid frontend file record')
        name = safe_path(record.get('path'))
        if name == MANIFEST or name in files:
            raise ValueError('Duplicate or reserved frontend asset path')
        if not isinstance(record.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', record['sha256']):
            raise ValueError('Invalid frontend file digest')
        if type(record.get('size')) is not int or record['size'] < 0:
            raise ValueError('Invalid frontend file size')
        if not isinstance(record.get('mime'), str) or not re.fullmatch(r'[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+', record['mime']):
            raise ValueError('Invalid frontend file MIME type')
        files[name] = record
    entry = files.get('index.html')
    if not entry or entry['size'] == 0 or entry['mime'] != 'text/html' or sum(item['size'] for item in files.values()) > MAX_BYTES:
        raise ValueError('Missing frontend entry or excessive unpacked size')
    return manifest, files


def verify_bytes(data, record):
    if len(data) != record['size'] or hashlib.sha256(data).hexdigest() != record['sha256']:
        raise ValueError('Frontend asset size or SHA256 mismatch: ' + record['path'])


def verify_directory(directory, lock):
    directory = pathlib.Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('Frontend directory must be a real directory')
    metadata = directory / MANIFEST
    if metadata.is_symlink() or not metadata.is_file() or metadata.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError('Missing or invalid frontend manifest')
    manifest, files = load_manifest(metadata.read_bytes(), lock)
    actual = set()
    for filename in directory.rglob('*'):
        if filename.is_symlink():
            raise ValueError('Frontend assets cannot be symlinks')
        if filename.is_file():
            name = safe_path(filename.relative_to(directory).as_posix())
            actual.add(name)
            if name != MANIFEST:
                if name not in files:
                    raise ValueError('Unlisted frontend asset')
                if filename.stat().st_size != files[name]['size']:
                    raise ValueError('Frontend asset size or SHA256 mismatch: ' + name)
                verify_bytes(filename.read_bytes(), files[name])
        elif not filename.is_dir():
            raise ValueError('Frontend assets must be regular files')
    if actual != set(files) | {MANIFEST}:
        raise ValueError('Frontend asset inventory is incomplete')
    return {'sourceCommit': manifest['sourceCommit'], 'files': len(files)}


def extract_archive(archive, directory, lock):
    directory = pathlib.Path(directory)
    if directory.is_symlink() or (directory.exists() and (not directory.is_dir() or any(directory.iterdir()))):
        raise ValueError('Frontend extraction requires an empty real directory')
    directory.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zipped:
        names = set()
        entries = []
        total = 0
        for info in zipped.infolist():
            name = info.filename.rstrip('/') if info.is_dir() else info.filename
            safe_path(name)
            mode = stat.S_IFMT(info.external_attr >> 16)
            if name in names or mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError('Duplicate, symlink or special ZIP entry')
            names.add(name)
            total += info.file_size
            if total > MAX_BYTES or len(names) > MAX_FILES * 2:
                raise ValueError('Frontend ZIP exceeds unpacking limits')
            if not info.is_dir():
                entries.append(info)
        metadata_info = zipped.getinfo(MANIFEST)
        if metadata_info.file_size > MAX_MANIFEST_BYTES:
            raise ValueError('Frontend manifest exceeds the size limit')
        manifest, files = load_manifest(zipped.read(MANIFEST), lock)
        if {entry.filename for entry in entries} != set(files) | {MANIFEST}:
            raise ValueError('ZIP files do not match the verified inventory')
        for info in entries:
            data = zipped.read(info)
            if info.filename != MANIFEST:
                verify_bytes(data, files[info.filename])
            destination = directory / info.filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
    return verify_directory(directory, lock)
