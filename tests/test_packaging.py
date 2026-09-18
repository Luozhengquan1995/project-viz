"""Isolated release/install behavior checks. Uses only synthetic source/state."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tools'))
import build_release as build
import install_skill as installer

CANARY = b'SYNTHETIC_PRIVATE_DATA_MUST_NOT_BE_DISTRIBUTED'


class PackagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='project-viz-package-test-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / 'synthetic-source'
        self.skill = self.repo / 'skills/project-viz'
        files = {
            'SKILL.md': '---\nname: project-viz\ndescription: Synthetic test skill.\n---\n',
            'LICENSE': 'Synthetic license text for a packaging fixture.\n',
            'THIRD_PARTY_NOTICES.md': 'Synthetic notices.\n',
            'agents/openai.yaml': 'interface:\n  display_name: "Synthetic"\n',
            'references/work-protocol.md': 'Synthetic public protocol.\n',
            'scripts/project_viz.py': 'from project_viz_runtime import VALUE\nprint(VALUE)\n',
            'scripts/project_viz_runtime/__init__.py': 'VALUE = "synthetic installed runtime"\n',
            'scripts/project_viz_runtime/server.py': '# Synthetic public source, not a server.\n',
            'assets/site/index.html': '<!doctype html><title>Synthetic</title>\n',
            'assets/site/flow.js': 'const synthetic = true;\n',
            'assets/site/flow.css': 'body { color: black; }\n',
        }
        for relative, content in files.items():
            self.write(self.skill / relative, content.encode('utf-8'))
        for relative in ('README.md', 'tools/build_release.py', 'tools/install_skill.py', 'tests/test_example.py'):
            self.write(self.repo / relative, b'# Synthetic source fixture.\n')
        for relative in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
            self.write(self.repo / relative, files[relative].encode('utf-8'))
        self.destination = self.base / 'installed skills' / '项目 skill'

    @staticmethod
    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def add_private_files(self):
        for relative in (
            'state/events.sqlite3', 'state/events.sqlite3-wal', 'sessions/rollout.jsonl',
            '.env', 'access-token', '.project-viz-install.json',
            'assets/site/data/private.json', 'assets/site/logs/private.js',
            'scripts/project_viz_runtime/private/secrets.py',
        ):
            self.write(self.skill / relative, CANARY)
        self.write(self.repo / 'private/notes.md', CANARY)
        self.write(self.repo / 'examples/real-project/catalog.json', CANARY)
        self.write(self.repo / 'tests/private/fixture.py', CANARY)

    def test_allowlist_excludes_private_data_in_both_bundles(self):
        self.add_private_files()
        skill_files = build.collect_skill_files(self.skill)
        source_files = build.collect_source_files(self.repo, skill_files)
        self.assertTrue(skill_files)
        self.assertTrue(source_files)
        for collection in (skill_files, source_files):
            self.assertFalse(any(CANARY in data for data in collection.values()))
        self.assertIn('scripts/project_viz_runtime/server.py', skill_files)
        self.assertIn('tests/test_example.py', source_files)
        self.assertNotIn('tests/private/fixture.py', source_files)

    def test_release_cli_produces_byte_identical_archives(self):
        results = []
        for name in ('first', 'second'):
            result = subprocess.run(
                [sys.executable, str(REPO / 'tools/build_release.py'),
                 '--source-root', str(self.repo), '--output-dir', str(self.base / name),
                 '--version', '0.1.0'],
                check=True, capture_output=True, text=True, encoding='utf-8',
            )
            archives = json.loads(result.stdout)['archives']
            self.assertEqual(len(archives), 2)
            results.append([Path(item['path']).read_bytes() for item in archives])
        self.assertEqual(results[0], results[1])

    def test_archive_manifest_metadata_and_licenses(self):
        files = build.collect_skill_files(self.skill)
        output = self.base / 'skill.zip'
        build.write_archive(output, 'project-viz', files)
        with zipfile.ZipFile(output) as archive:
            names = archive.namelist()
            self.assertEqual(names, sorted(names))
            for info in archive.infolist():
                self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                self.assertEqual(info.create_system, 3)
                self.assertEqual(info.external_attr >> 16, 0o100644)
            manifest = json.loads(archive.read('project-viz/BUILD-MANIFEST.json'))
            self.assertEqual({item['path'] for item in manifest['files']}, set(files))
            for item in manifest['files']:
                data = archive.read('project-viz/' + item['path'])
                self.assertEqual(item['bytes'], len(data))
                self.assertEqual(item['sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual(archive.read('project-viz/LICENSE'), (self.repo / 'LICENSE').read_bytes())
            self.assertEqual(archive.read('project-viz/THIRD_PARTY_NOTICES.md'),
                             (self.repo / 'THIRD_PARTY_NOTICES.md').read_bytes())

    def test_install_unicode_space_path_and_self_contained_entry(self):
        result = installer.install(self.skill, self.destination, False)
        self.assertEqual(result['installed'], str(self.destination.resolve()))
        self.assertIsNone(result['backup'])
        self.assertTrue((self.destination / installer.MARKER).is_file())
        executed = subprocess.run(
            [sys.executable, '-B', str(self.destination / 'scripts/project_viz.py')],
            cwd=self.base, check=True, capture_output=True, text=True, encoding='utf-8',
        )
        self.assertEqual(executed.stdout.strip(), 'synthetic installed runtime')
        self.assertTrue((self.destination / 'assets/site/index.html').is_file())

    def test_existing_directory_is_unchanged_without_replace(self):
        self.write(self.destination / 'user-owned.txt', b'keep this original file')
        with self.assertRaisesRegex(ValueError, 'Destination exists'):
            installer.install(self.skill, self.destination, False)
        self.assertEqual((self.destination / 'user-owned.txt').read_bytes(), b'keep this original file')
        self.assertFalse((self.destination / 'SKILL.md').exists())
        self.assertEqual(len(list(self.destination.parent.iterdir())), 1)

    def test_explicit_replace_retains_original_directory(self):
        self.write(self.destination / 'nested/user-owned.txt', b'keep the complete old installation')
        result = installer.install(self.skill, self.destination, True)
        backup = Path(result['backup'])
        self.assertEqual((backup / 'nested/user-owned.txt').read_bytes(), b'keep the complete old installation')
        self.assertTrue((self.destination / 'SKILL.md').is_file())
        self.assertFalse((self.destination / 'nested/user-owned.txt').exists())
        second = installer.install(self.skill, self.destination, True)
        self.assertNotEqual(result['backup'], second['backup'])
        self.assertTrue((Path(second['backup']) / installer.MARKER).is_file())
        self.assertTrue((backup / 'nested/user-owned.txt').is_file())

    def test_installer_never_copies_private_source_state(self):
        self.add_private_files()
        installer.install(self.skill, self.destination, False)
        for path in self.destination.rglob('*'):
            if path.is_file():
                self.assertNotIn(CANARY, path.read_bytes())
        self.assertFalse((self.destination / 'state').exists())
        self.assertFalse((self.destination / 'access-token').exists())
        self.assertEqual((self.skill / 'state/events.sqlite3').read_bytes(), CANARY)

    def test_interrupted_swap_rolls_back_existing_directory(self):
        self.write(self.destination / 'sentinel.txt', b'rollback me')
        rename = Path.rename

        def interrupt_stage(path, target):
            if path.name.startswith('.project-viz-install-'):
                raise OSError('Synthetic install interruption')
            return rename(path, target)

        with patch.object(Path, 'rename', interrupt_stage):
            with self.assertRaisesRegex(OSError, 'Synthetic install interruption'):
                installer.install(self.skill, self.destination, True)
        self.assertEqual((self.destination / 'sentinel.txt').read_bytes(), b'rollback me')
        self.assertFalse((self.destination / 'SKILL.md').exists())
        self.assertEqual(list(self.destination.parent.iterdir()), [self.destination])

    def test_distributable_symlink_and_destination_symlink_are_refused(self):
        target = self.base / 'outside.js'
        target.write_bytes(CANARY)
        link = self.skill / 'assets/site/linked.js'
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as error:
            self.skipTest('Symlink creation is unavailable: ' + str(error))
        with self.assertRaisesRegex(ValueError, 'Symlink is not distributable'):
            build.collect_skill_files(self.skill)
        self.assertEqual(target.read_bytes(), CANARY)
        link.unlink()
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        self.destination.symlink_to(self.skill, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'Destination is a symlink'):
            installer.install(self.skill, self.destination, True)
        self.assertTrue(self.destination.is_symlink())
        self.assertFalse((self.skill / installer.MARKER).exists())


if __name__ == '__main__':
    unittest.main()
