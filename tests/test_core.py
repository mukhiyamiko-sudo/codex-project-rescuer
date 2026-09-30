import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import core


class RestorerCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / ".codex"
        self.home.mkdir()
        self.current_root = Path(self.temp.name) / "current"
        self.new_root = Path(self.temp.name) / "new"
        self.second_root = Path(self.temp.name) / "second"
        for path in (self.current_root, self.new_root, self.second_root):
            path.mkdir()
        self.state_path = self.home / ".codex-global-state.json"
        self.db_path = self.home / "state_5.sqlite"
        self._write_state()
        self._write_database()

    def tearDown(self):
        self.temp.cleanup()

    def _write_state(self):
        state = {
            "untouched": {"keep": True},
            "local-projects": {
                "legacy-current": {
                    "id": "legacy-current",
                    "name": "当前示例项目",
                    "rootPaths": [str(self.current_root)],
                    "createdAt": 100,
                    "updatedAt": 101,
                }
            },
            "project-order": ["legacy-current"],
            "app-server-project-id-by-legacy-project-id-by-host": {
                f"local:{self.home}": {"legacy-current": "app-current"}
            },
        }
        self.state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        (self.home / ".codex-global-state.json.bak").write_text("old-backup", encoding="utf-8")

    def _write_database(self):
        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            """
            CREATE TABLE projects (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
              position INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL
            );
            CREATE TABLE project_roots (
              project_id TEXT NOT NULL, position INTEGER NOT NULL, path TEXT NOT NULL,
              PRIMARY KEY (project_id, position)
            );
            """
        )
        projects = [
            ("app-old-current", " 当前示例项目 ", 0, 10, 11, str(Path(self.temp.name) / "old-current")),
            ("app-same-path", "别的名字", 1, 20, 21, str(self.current_root).upper()),
            ("app-new", "待恢复示例项目", 2, 30, 31, str(self.new_root)),
            ("app-second", "第二个示例项目", 3, 40, 41, str(self.second_root)),
            ("app-missing", "路径已丢失", 4, 50, 51, str(Path(self.temp.name) / "missing")),
        ]
        for app_id, name, position, created, updated, root in projects:
            connection.execute(
                "INSERT INTO projects(id,name,position,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?)",
                (app_id, name, position, created, updated),
            )
            connection.execute(
                "INSERT INTO project_roots(project_id,position,path) VALUES(?,?,?)",
                (app_id, 0, root),
            )
        connection.commit()
        connection.close()

    def test_scan_prefers_current_projects_by_name_and_path(self):
        items = {item.project.app_id: item for item in core.scan_projects(self.home)}

        self.assertEqual(items["app-old-current"].status, "current")
        self.assertEqual(items["app-same-path"].status, "current")
        self.assertEqual(items["app-new"].status, "missing")
        self.assertEqual(items["app-missing"].status, "path_missing")
        self.assertTrue(items["app-new"].selected_by_default)
        self.assertFalse(items["app-missing"].selected_by_default)

    def test_restore_adds_only_selected_and_preserves_current_state(self):
        before = core.load_state(self.state_path)

        result = core.restore_projects(self.home, {"app-new"}, check_processes=False)
        after = core.load_state(self.state_path)

        self.assertEqual(result.added_names, ["待恢复示例项目"])
        self.assertEqual(after["untouched"], before["untouched"])
        self.assertEqual(after["local-projects"]["legacy-current"], before["local-projects"]["legacy-current"])
        self.assertEqual(after["project-order"][0], "legacy-current")
        self.assertEqual(len(after["local-projects"]), 2)
        restored_id = after["project-order"][1]
        self.assertEqual(after["local-projects"][restored_id]["name"], "待恢复示例项目")
        mapping = after["app-server-project-id-by-legacy-project-id-by-host"][f"local:{self.home}"]
        self.assertEqual(mapping[restored_id], "app-new")
        self.assertEqual(len(list((self.home / "project-restorer-backups").iterdir())), 1)

    def test_second_restore_keeps_only_latest_backup_and_rollback_undoes_it(self):
        core.restore_projects(self.home, {"app-new"}, check_processes=False)
        state_after_first = core.load_state(self.state_path)
        core.restore_projects(self.home, {"app-second"}, check_processes=False)
        backup_root = self.home / "project-restorer-backups"
        self.assertEqual(len(list(backup_root.iterdir())), 1)

        core.rollback_latest(self.home, check_processes=False)
        rolled_back = core.load_state(self.state_path)

        self.assertEqual(rolled_back, state_after_first)
        self.assertEqual(len(list(backup_root.iterdir())), 1)

    def test_corrupt_state_never_gets_overwritten(self):
        original = "{not-json"
        self.state_path.write_text(original, encoding="utf-8")

        with self.assertRaises(core.RestorerError):
            core.restore_projects(self.home, {"app-new"}, check_processes=False)

        self.assertEqual(self.state_path.read_text(encoding="utf-8"), original)

    def test_process_parser_detects_codex_processes_only(self):
        tasklist = (
            '"ChatGPT.exe","100","Console","1","10,000 K"\n'
            '"codex.exe","101","Console","1","20,000 K"\n'
            '"notepad.exe","102","Console","1","2,000 K"\n'
        )

        self.assertEqual(core.codex_processes_from_tasklist(tasklist), ["ChatGPT.exe", "codex.exe"])


if __name__ == "__main__":
    unittest.main()
