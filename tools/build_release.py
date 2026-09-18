#!/usr/bin/env python3
"""Build deterministic, allowlisted skill and source archives; stdlib only."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile

SKILL_EXACT = {
    'SKILL.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
    'agents/openai.yaml', 'references/work-protocol.md',
    'scripts/project_viz.py',
}
SOURCE_EXACT = {
    'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', '.gitignore',
    'docs/preview.png',
    'tools/build_release.py', 'tools/install_skill.py',
    'ci/github-actions-tests.yml',
    'examples/synthetic-project/README.md',
    'examples/synthetic-project/PLAN.md',
    'examples/synthetic-project/catalog.json',
    'examples/synthetic-project/reports/import-validation.md',
}
SITE_SUFFIXES = {'.html', '.css', '.js', '.svg', '.png', '.ico', '.woff2'}
DENIED_PARTS = {
    '__pycache__', '.git', '.venv', 'venv', 'node_modules', 'dist', 'build',
    'state', 'private', 'logs', 'sessions', '.project-viz', '.project-viz-state',
}
DENIED_NAMES = {'access-token', 'token', '.project-viz-install.json'}
DENIED_SUFFIXES = {'.jsonl', '.sqlite', '.sqlite3', '.db', '.log', '.pid', '.pem', '.key'}


def allowed_path(relative: str) -> bool:
    path = PurePosixPath(relative)
    if path.is_absolute() or '..' in path.parts:
        return False
    if any(part.lower() in DENIED_PARTS for part in path.parts):
        return False
    name = path.name.lower()
    return not (name in DENIED_NAMES or name.startswith('.env') or
                path.suffix.lower() in DENIED_SUFFIXES or
                any(marker in name for marker in ('.sqlite-', '.sqlite3-', '.db-')))


def skill_path_allowed(relative: str) -> bool:
    if not allowed_path(relative):
        return False
    if relative in SKILL_EXACT:
        return True
    path = PurePosixPath(relative)
    if relative.startswith('scripts/project_viz_runtime/') and path.suffix == '.py':
        return True
    if relative.startswith('assets/site/') and path.suffix.lower() in SITE_SUFFIXES:
        return True
    return False


def safe_file(root: Path, relative: str) -> bytes:
    """Do not follow a symlink at any level of a distributed file path."""
    cursor = root
    for part in PurePosixPath(relative).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f'Symlink is not distributable: {relative}')
    if not cursor.is_file() or not cursor.resolve().is_relative_to(root.resolve()):
        raise ValueError(f'Not a regular source file: {relative}')
    return cursor.read_bytes()


def collect_skill_files(skill_root: Path) -> dict[str, bytes]:
    if skill_root.is_symlink() or not skill_root.is_dir():
        raise ValueError('Skill source must be a real directory')
    files = {}
    for path in sorted(skill_root.rglob('*')):
        relative = path.relative_to(skill_root).as_posix()
        if skill_path_allowed(relative) and (path.is_file() or path.is_symlink()):
            files[relative] = safe_file(skill_root, relative)
    required = SKILL_EXACT | {'assets/site/index.html'}
    missing = sorted(required - files.keys())
    if missing:
        raise ValueError('Incomplete skill source: ' + ', '.join(missing))
    return files


def collect_source_files(root: Path, skill_files: dict[str, bytes]) -> dict[str, bytes]:
    files = {'skills/project-viz/' + name: data for name, data in skill_files.items()}
    for relative in sorted(SOURCE_EXACT):
        if (root / relative).exists():
            files[relative] = safe_file(root, relative)
    for path in sorted((root / 'tests').rglob('*.py')) if (root / 'tests').exists() else []:
        relative = path.relative_to(root).as_posix()
        if allowed_path(relative):
            files[relative] = safe_file(root, relative)
    for required in ('README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
                     'tools/build_release.py', 'tools/install_skill.py'):
        if required not in files:
            raise ValueError(f'Missing source bundle file: {required}')
    return files


def write_archive(path: Path, prefix: str, files: dict[str, bytes]) -> dict:
    manifest = {
        'schemaVersion': 1,
        'files': [{'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                  for name, data in sorted(files.items())],
    }
    contents = dict(files)
    contents['BUILD-MANIFEST.json'] = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative, data in sorted(contents.items()):
            entry = zipfile.ZipInfo(prefix + '/' + relative, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return {'path': str(path), 'files': len(contents), 'bytes': path.stat().st_size,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output-dir', type=Path, default=Path('dist'))
    parser.add_argument('--version', default='0.2.0')
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?', args.version):
        parser.error('version must be a semantic version without path characters')
    try:
        root = args.source_root.resolve()
        skill = collect_skill_files(root / 'skills/project-viz')
        source = collect_source_files(root, skill)
        for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
            if skill[name] != source[name]:
                raise ValueError(f'Skill and source {name} must match')
        output = args.output_dir.resolve()
        archives = [
            write_archive(output / f'project-viz-skill-{args.version}.zip', 'project-viz', skill),
            write_archive(output / f'project-viz-source-{args.version}.zip', f'project-viz-{args.version}', source),
        ]
        print(json.dumps({'version': args.version, 'archives': archives}, indent=2))
        return 0
    except (OSError, ValueError) as error:
        print(json.dumps({'error': str(error)}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
