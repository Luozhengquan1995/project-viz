#!/usr/bin/env python3
"""Install the self-contained Project Viz skill without modifying user state."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

from build_release import collect_skill_files

MARKER = '.project-viz-install.json'


def install(source: Path, destination: Path, replace: bool) -> dict:
    source = source.expanduser().resolve()
    # Check the final entry before resolve() so a symlink cannot be overwritten.
    destination = destination.expanduser().absolute()
    if destination.is_symlink():
        raise ValueError('Destination is a symlink; choose a real skill directory')
    destination = destination.resolve()
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError('Source and destination must not overlap')
    if destination == Path(destination.anchor):
        raise ValueError('Destination must be a skill directory, not a filesystem root')
    if destination.exists() and not replace:
        raise ValueError('Destination exists; use --replace to retain a backup and replace it')
    if destination.exists() and not destination.is_dir():
        raise ValueError('Destination must be a directory')
    files = collect_skill_files(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.project-viz-install-', dir=destination.parent))
    backup = None
    try:
        for name, data in files.items():
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        marker = {'schemaVersion': 1, 'owner': 'project-viz-installer', 'skill': 'project-viz',
                  'files': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}}
        (stage / MARKER).write_text(json.dumps(marker, indent=2) + '\n', encoding='utf-8')
        if destination.exists():
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            backup = destination.with_name(destination.name + '.backup-' + stamp + '-' + uuid.uuid4().hex[:8])
            destination.rename(backup)
        try:
            stage.rename(destination)
        except OSError:
            if backup is not None and backup.exists() and not destination.exists():
                backup.rename(destination)
            raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {'installed': str(destination), 'files': len(files),
            'backup': str(backup) if backup else None,
            'note': 'Project runtime state and global instructions were not changed.'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'skills/project-viz')
    parser.add_argument('--destination', type=Path,
                        default=Path.home() / '.agents/skills/project-viz',
                        help='Exact destination skill folder (not its parent)')
    parser.add_argument('--replace', action='store_true',
                        help='Replace an existing directory, retaining a sibling backup')
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.source, args.destination, args.replace), indent=2))
        return 0
    except (OSError, ValueError) as error:
        print(json.dumps({'error': str(error)}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
