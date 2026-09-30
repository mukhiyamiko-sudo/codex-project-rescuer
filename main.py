from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import core


def run_self_test(report_path: Path) -> int:
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_home = root / ".codex"
            current_root = root / "current-project"
            restored_root = root / "restored-project"
            codex_home.mkdir()
            current_root.mkdir()
            restored_root.mkdir()
            state = {
                "local-projects": {
                    "legacy-current": {
                        "id": "legacy-current",
                        "name": "当前示例项目",
                        "rootPaths": [str(current_root)],
                        "createdAt": 1,
                        "updatedAt": 2,
                    }
                },
                "project-order": ["legacy-current"],
                "app-server-project-id-by-legacy-project-id-by-host": {
                    f"local:{codex_home}": {"legacy-current": "app-current"}
                },
            }
            state_path = codex_home / core.STATE_NAME
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            database = sqlite3.connect(codex_home / "state_5.sqlite")
            database.executescript(
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
            database.execute(
                "INSERT INTO projects(id,name,position,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?)",
                ("app-current-old", "当前示例项目", 0, 1, 2),
            )
            database.execute(
                "INSERT INTO project_roots(project_id,position,path) VALUES(?,?,?)",
                ("app-current-old", 0, str(root / "old-current")),
            )
            database.execute(
                "INSERT INTO projects(id,name,position,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?)",
                ("app-restored", "待恢复示例项目", 1, 3, 4),
            )
            database.execute(
                "INSERT INTO project_roots(project_id,position,path) VALUES(?,?,?)",
                ("app-restored", 0, str(restored_root)),
            )
            database.commit()
            database.close()

            result = core.restore_projects(codex_home, {"app-current-old", "app-restored"}, check_processes=False)
            after = core.load_state(state_path)
            report = {
                "ok": True,
                "restored_count": len(result.added_names),
                "current_project_preserved": after["local-projects"]["legacy-current"] == state["local-projects"]["legacy-current"],
                "backup_count": len(list((codex_home / core.BACKUP_ROOT_NAME).iterdir())),
            }
    except Exception as exc:
        report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report.get("ok") else 1


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--self-test":
        output = Path(sys.argv[2]) if len(sys.argv) >= 3 else Path.cwd() / "self-test-report.json"
        return run_self_test(output)

    import tkinter as tk
    from app import RestorerApp

    root = tk.Tk()
    RestorerApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
