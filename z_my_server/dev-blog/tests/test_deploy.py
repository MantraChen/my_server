import importlib.util
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

COMPONENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(COMPONENT))
from source import read_source

spec = importlib.util.spec_from_file_location("blog_deploy", COMPONENT / "deploy.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


@contextmanager
def connection(path):
    database = sqlite3.connect(path)
    try:
        with database:
            yield database
    finally:
        database.close()


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        (self.data / "backups").mkdir()
        self.unit = self.root / deploy.SERVICE
        self.db = self.data / "data.db"
        with connection(self.db) as database:
            database.execute("CREATE TABLE posts(id INTEGER PRIMARY KEY, slug TEXT)")
            database.execute("INSERT INTO posts(slug) VALUES ('preserved')")
        self.source = read_source()
        self.old = self.root / "old"
        self.new = self.root / "new"
        for directory in (self.old, self.new):
            directory.mkdir()
            (directory / deploy.SERVICE).write_bytes(b"new unit")
            (directory / ".devblog-release.json").write_text(json.dumps({"upstream_commit": "a" * 40}))
        for key, value in {"BASE": self.root, "DATA": self.data, "UNIT": self.unit}.items():
            patcher = patch.object(deploy, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_pin_rejects_branch_names_other_repositories_and_extra_keys(self):
        manifest = self.root / "source.json"
        for source in ({**self.source, "commit": "main"},
                       {**self.source, "repository": "attacker/code"},
                       {**self.source, "command": "touch unexpected"}):
            manifest.write_text(json.dumps(source))
            with self.assertRaises(ValueError):
                read_source(manifest)
        manifest.write_text(json.dumps(self.source))
        self.assertEqual(read_source(manifest), self.source)

    def test_site_and_base_reject_unsafe_or_ambiguous_configuration(self):
        manifest = self.root / "source.json"
        for field, values in {
            "site": ["http://dyyjs.com", "https://dyyjs.com/howielog", "https://user@dyyjs.com", "https://dyyjs.com\nextra=value"],
            "base": ["/", "//host", "/howielog/", "/../howielog", "/howielog?x=1", "howielog"],
        }.items():
            for value in values:
                manifest.write_text(json.dumps({**self.source, field: value}))
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    read_source(manifest)

    def test_cached_release_cannot_use_another_base(self):
        release = self.root / "releases" / ("b" * 40)
        release.mkdir(parents=True)
        (release / ".devblog-release.json").write_text(json.dumps({
            "server_commit": "b" * 40, "upstream_commit": self.source["commit"],
            "site": self.source["site"], "base": "/wrong",
        }))
        with patch.object(deploy, "root_owned"), self.assertRaisesRegex(RuntimeError, "reviewed source"):
            deploy.prepare_release("b" * 40, self.source, "rollback")

    def test_env_cannot_override_database_host_or_execute_shell(self):
        value = "ADMIN_PASSWORD_HASH=" + "$2b$12$" + "a" * 53
        deploy.validate_env(value + "\n# comment\n")
        for text in (value + "\nHOST=0.0.0.0", value + "\nDATABASE_PATH=/tmp/new.db",
                     value + "\nNODE_OPTIONS=--require=bad.js", value + "$(id)",
                     value + "\n" + value, "ADMIN_PASSWORD_HASH=placeholder"):
            with self.assertRaises(ValueError):
                deploy.validate_env(text)

    def test_backup_includes_uncheckpointed_wal_and_refuses_overwrite(self):
        with connection(self.db) as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute("INSERT INTO posts(slug) VALUES ('from-wal')")
            database.commit()
            backup = self.data / "backups" / "snapshot.db"
            deploy.backup_database(self.db, backup)
            with connection(backup) as copy:
                self.assertEqual(copy.execute("SELECT slug FROM posts ORDER BY id").fetchall(),
                                 [("preserved",), ("from-wal",)])
            with self.assertRaises(RuntimeError):
                deploy.backup_database(self.db, backup)

    def test_missing_database_is_not_created(self):
        absent = self.data / "missing.db"
        with self.assertRaises(sqlite3.OperationalError):
            deploy.check_database(absent)
        self.assertFalse(absent.exists())

    def test_rehearsal_modifies_only_a_copy(self):
        def migration(_release, database, _backups):
            with connection(database) as db:
                db.execute("INSERT INTO posts(slug) VALUES ('migration-copy')")
        with patch.object(deploy.shutil, "chown"), patch.object(deploy, "migrate", side_effect=migration):
            deploy.rehearse(self.new)
        with connection(self.db) as database:
            self.assertEqual(database.execute("SELECT slug FROM posts").fetchall(), [("preserved",)])
        self.assertEqual(list(self.data.glob("rehearsal-*")), [])

    def test_build_failure_does_not_stop_service(self):
        with patch.object(deploy, "preflight"), patch.object(deploy, "prepare_release", side_effect=RuntimeError("build")), \
                patch.object(deploy, "activate") as activate, patch.object(deploy, "rehearse") as rehearse:
            with self.assertRaisesRegex(RuntimeError, "build"):
                deploy.deploy("deploy", "b" * 40)
            activate.assert_not_called()
            rehearse.assert_not_called()

    def test_rehearsal_failure_does_not_stop_service(self):
        with patch.object(deploy, "preflight"), patch.object(deploy, "prepare_release", return_value=self.new), \
                patch.object(deploy, "activate") as activate, patch.object(deploy, "rehearse", side_effect=RuntimeError("rehearse")):
            with self.assertRaisesRegex(RuntimeError, "rehearse"):
                deploy.deploy("deploy", "b" * 40)
            activate.assert_not_called()

    def test_rollback_does_not_build_an_unverified_release(self):
        (self.root / "releases").mkdir()
        with self.assertRaisesRegex(RuntimeError, "already verified"):
            deploy.prepare_release("b" * 40, self.source, "rollback")

    def activation(self, migration_failure=False, health_failure=False, start_failure=False, existing=True):
        self.unit.write_bytes(b"previous unit") if existing else None
        calls = []
        def run(*args, **kwargs):
            calls.append(args)
            if start_failure and args == ("systemctl", "start", deploy.SERVICE) and calls.count(args) == 1:
                raise RuntimeError("start failed")
        def migrate(*_args):
            if migration_failure:
                raise RuntimeError("migration failed")
            # Simulate writes after a forward migration. Rollback must preserve them.
            with connection(self.db) as database:
                database.execute("INSERT INTO posts(slug) VALUES ('new-data')")
        health = Mock(side_effect=[RuntimeError("health failed"), None] if health_failure else None)
        with patch.object(deploy, "get_current", return_value=self.old if existing else None), \
                patch.object(deploy, "set_current") as current, \
                patch.object(deploy, "service_state", side_effect=lambda verb: ("active" if verb == "is-active" else "enabled") if existing else "inactive"), \
                patch.object(deploy, "run", side_effect=run), patch.object(deploy.subprocess, "run"), \
                patch.object(deploy, "migrate", side_effect=migrate), patch.object(deploy, "wait_healthy", health):
            if migration_failure or health_failure or start_failure:
                with self.assertRaisesRegex(RuntimeError, "failed"):
                    deploy.activate(self.new, self.source)
                self.assertEqual(current.call_args.args[0], self.old if existing else None)
                if existing:
                    health.assert_any_call("a" * 40, "")  # Legacy root deployment restored.
                self.assertEqual(self.unit.read_bytes() if self.unit.exists() else None,
                                 b"previous unit" if existing else None)
            else:
                deploy.activate(self.new, self.source)
                self.assertEqual(current.call_args.args[0], self.new)
                health.assert_called_once_with(self.source["commit"], self.source["base"])
                self.assertIn(("systemctl", "enable", deploy.SERVICE), calls)
        with connection(self.db) as database:
            rows = database.execute("SELECT slug FROM posts").fetchall()
            self.assertEqual(rows, [("preserved",)] if migration_failure else [("preserved",), ("new-data",)])
        self.assertEqual(len(list((self.data / "backups").glob("*.db"))), 1)
        return calls

    def test_success_checks_exact_release_before_enabling_service(self):
        self.activation()

    def test_migration_failure_restores_service_and_retains_database(self):
        calls = self.activation(migration_failure=True)
        self.assertIn(("systemctl", "start", deploy.SERVICE), calls)

    def test_health_failure_restores_previous_code_and_keeps_new_writes(self):
        self.activation(health_failure=True)

    def test_start_failure_restarts_previous_service(self):
        calls = self.activation(start_failure=True)
        self.assertEqual(calls.count(("systemctl", "start", deploy.SERVICE)), 2)

    def test_initial_activation_failure_removes_unit_and_current(self):
        self.activation(health_failure=True, existing=False)

    def test_unit_replacement_preserves_outer_transaction_hardlink(self):
        self.unit.write_bytes(b"old unit")
        snapshot = self.root / "snapshot"
        os.link(self.unit, snapshot)
        deploy.write_unit(b"new unit")
        self.assertEqual(snapshot.read_bytes(), b"old unit")
        self.assertEqual(self.unit.read_bytes(), b"new unit")

    def test_wrong_release_health_is_rejected(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.status = 200
        response.read.return_value = json.dumps({"status": "ok", "release": "wrong"}).encode()
        opener = Mock()
        opener.open.return_value = response
        with patch.object(deploy.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "pinned upstream"):
                deploy.wait_healthy(self.source["commit"], self.source["base"], attempts=1)
            opener.open.assert_called_once_with("http://127.0.0.1:4321/howielog/api/health", timeout=2)

    @unittest.skipIf(os.name == "nt", "POSIX symlink permissions are checked in Linux CI")
    def test_current_switch_and_removal(self):
        deploy.set_current(self.old)
        self.assertEqual(deploy.get_current(), self.old)
        deploy.set_current(self.new)
        self.assertEqual(deploy.get_current(), self.new)
        deploy.set_current(None)
        self.assertIsNone(deploy.get_current())


if __name__ == "__main__":
    unittest.main()
