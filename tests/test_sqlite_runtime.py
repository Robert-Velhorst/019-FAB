"""Native identity and synthetic bundled-engine to pinned-engine compatibility."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fab_sqlite_runtime import ROOT, platform_key, require_stable_sqlite, runtime_evidence


class TestSQLiteRuntime(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows compiled ABI policy')
    def test_windows_abi_does_not_require_processor_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(platform_key(), 'windows-x86_64')

    def test_actual_engine_and_required_features(self):
        sqlite = require_stable_sqlite()
        evidence = runtime_evidence()
        self.assertEqual(evidence['version'], '3.54.1')
        self.assertEqual(evidence['platform'], platform_key())
        self.assertIn('THREADSAFE=1', evidence['compile_options'])
        with sqlite.connect(':memory:') as connection:
            self.assertEqual(connection.execute('SELECT sqlite_version()').fetchone()[0], '3.54.1')
            self.assertEqual(connection.execute("SELECT json_extract('{\"value\":7}', '$.value')").fetchone()[0], 7)
            connection.execute('CREATE VIRTUAL TABLE search USING fts5(value)')
            connection.execute('CREATE VIRTUAL TABLE spatial USING rtree(id,min,max)')
        connection.close()

    def isolated_root(self, directory):
        root = Path(directory)
        shutil.copyfile(ROOT / 'fab_sqlite_runtime.py', root / 'fab_sqlite_runtime.py')
        shutil.copyfile(ROOT / 'sqlite-runtime.json', root / 'sqlite-runtime.json')
        shutil.copytree(ROOT / 'runtime_bootstrap', root / 'runtime_bootstrap', ignore=shutil.ignore_patterns('__pycache__'))
        return root

    def child(self, root, code, bootstrap=False):
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        if bootstrap:
            env['PYTHONPATH'] = os.pathsep.join([str(root / 'runtime_bootstrap'), str(root)])
        return subprocess.run([sys.executable, *([] if bootstrap else ['-S']), '-c', code],
                              cwd=root, env=env, capture_output=True, text=True, timeout=60)

    def test_missing_runtime_prevents_database_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.isolated_root(directory)
            child = self.child(root, "from fab_sqlite_runtime import require_stable_sqlite; s=require_stable_sqlite(); s.connect('operator.sqlite3')")
            self.assertNotEqual(child.returncode, 0)
            self.assertIn('Verified SQLite runtime missing', child.stderr)
            self.assertFalse((root / 'operator.sqlite3').exists())

    def test_preinitialized_binding_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.isolated_root(directory)
            child = self.child(root, 'import sqlite3; from fab_sqlite_runtime import require_stable_sqlite; require_stable_sqlite()')
            self.assertNotEqual(child.returncode, 0)
            self.assertIn('initialized before', child.stderr)

    def test_sitecustomize_fails_closed_before_application(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.isolated_root(directory)
            child = self.child(root, "open('application-started', 'w').close()", bootstrap=True)
            self.assertEqual(child.returncode, 78)
            self.assertIn('verified SQLite bootstrap failed', child.stderr)
            self.assertFalse((root / 'application-started').exists())

    def test_tampered_native_library_is_rejected_before_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.isolated_root(directory)
            spec = json.loads((root / 'sqlite-runtime.json').read_text())
            key = platform_key()
            target = root / '.fab-runtime' / 'sqlite' / spec['version'] / key
            target.mkdir(parents=True)
            (target / spec[key]['library']).write_bytes(b'not a native library')
            if key.startswith('linux'):
                (target / 'integrity.json').write_text(json.dumps({'version':spec['version'],
                    'source_sha3_256':spec[key]['archive_sha3_256'], 'binary_sha256':'0' * 64}))
            child = self.child(root, 'from fab_sqlite_runtime import require_stable_sqlite; require_stable_sqlite()')
            self.assertNotEqual(child.returncode, 0)
            self.assertIn('integrity check failed', child.stderr)

    def test_synthetic_bundled_engine_ledger_preserves_rows_and_approval(self):
        # -S and no bootstrap path deliberately select the interpreter's original
        # SQLite in a fresh process. run_path loads the unchanged stdlib-only
        # ledger implementation without importing the application's early guard.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = self.child(root, '''import json, runpy, sqlite3
ledger_type=runpy.run_path(%r)['LocalOperationsLedger']
ledger=ledger_type('synthetic.sqlite3')
record=ledger.upsert_bookkeeping_record({'sourceType':'document','status':'validated',
    'targetSystem':'waveapps_business','vendorName':'Synthetic Vendor','amount':12.34,
    'recordDate':'2026-10-23'})
with sqlite3.connect(ledger.path) as c:
    c.execute("CREATE TABLE fab_upgrade_evidence(id INTEGER PRIMARY KEY, value TEXT)")
    c.execute("INSERT INTO fab_upgrade_evidence VALUES(1, 'synthetic only')")
    schema=c.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%%' ORDER BY type,name").fetchall()
    rows=c.execute('SELECT * FROM bookkeeping_records').fetchall()
print(json.dumps({'bundled_version':sqlite3.sqlite_version,'schema':schema,'rows':rows}))
''' % str(ROOT / 'src/operations/local_ledger.py'))
            self.assertEqual(child.returncode, 0, child.stderr)
            before = json.loads(child.stdout)
            sqlite = require_stable_sqlite()
            from src.operations.local_ledger import LocalOperationsLedger
            ledger = LocalOperationsLedger(str(root / 'synthetic.sqlite3'))
            connection = ledger._connect()
            try:
                self.assertEqual(connection.execute('SELECT sqlite_version()').fetchone()[0], '3.54.1')
                self.assertEqual(connection.execute('PRAGMA journal_mode').fetchone()[0].lower(), 'wal')
                self.assertEqual(connection.execute('PRAGMA foreign_keys').fetchone()[0], 1)
                self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                schema = connection.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
                self.assertEqual([list(row) for row in schema], before['schema'])
                rows = connection.execute('SELECT * FROM bookkeeping_records').fetchall()
                self.assertEqual([list(row) for row in rows], before['rows'])
                self.assertEqual(connection.execute('SELECT value FROM fab_upgrade_evidence').fetchone()[0], 'synthetic only')
                connection.execute('BEGIN')
                connection.execute("UPDATE fab_upgrade_evidence SET value='rolled back'")
                connection.rollback()
                self.assertEqual(connection.execute('SELECT value FROM fab_upgrade_evidence').fetchone()[0], 'synthetic only')
                with sqlite.connect(str(root / 'synthetic-backup.sqlite3')) as backup:
                    connection.backup(backup)
                    self.assertEqual(backup.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                    self.assertEqual(backup.execute('SELECT * FROM bookkeeping_records').fetchall(), [tuple(row) for row in rows])
                backup.close()
            finally:
                connection.close()


if __name__ == '__main__':
    unittest.main()
