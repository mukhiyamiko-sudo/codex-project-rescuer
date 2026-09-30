from __future__ import annotations

import copy
import csv
import json
import locale
import os
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


STATE_NAME = ".codex-global-state.json"
BACKUP_ROOT_NAME = "project-restorer-backups"
BACKUP_FILE_NAMES = (
    STATE_NAME,
    f"{STATE_NAME}.bak",
    "state_5.sqlite",
    "state_5.sqlite-wal",
    "state_5.sqlite-shm",
)


class RestorerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Project:
    app_id: str
    name: str
    roots: tuple[str, ...]
    position: int
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True)
class ScanItem:
    project: Project
    status: str
    reason: str

    @property
    def selected_by_default(self) -> bool:
        return self.status == "missing"


@dataclass(frozen=True)
class RestoreResult:
    added_names: list[str]
    backup_dir: Path


@dataclass(frozen=True)
class RollbackResult:
    restored_from: Path
    safety_backup: Path


def load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RestorerError(f"找不到 Codex 状态文件：{path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RestorerError(f"Codex 状态文件无法读取或格式损坏：{path}") from exc
    if not isinstance(state, dict):
        raise RestorerError("Codex 状态文件的顶层结构不是对象，已停止操作。")
    for key, expected in (("local-projects", dict), ("project-order", list)):
        if key in state and not isinstance(state[key], expected):
            raise RestorerError(f"Codex 状态字段 {key} 的格式不兼容，已停止操作。")
    mapping_key = "app-server-project-id-by-legacy-project-id-by-host"
    if mapping_key in state and not isinstance(state[mapping_key], dict):
        raise RestorerError(f"Codex 状态字段 {mapping_key} 的格式不兼容，已停止操作。")
    return state


def load_history(database_path: Path) -> list[Project]:
    if not database_path.is_file():
        raise RestorerError(f"找不到 Codex 项目数据库：{database_path}")
    uri = f"file:{database_path.resolve().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        rows = connection.execute(
            """
            SELECT p.id, p.name, p.position, p.created_at_ms, p.updated_at_ms,
                   r.position, r.path
            FROM projects AS p
            LEFT JOIN project_roots AS r ON r.project_id = p.id
            ORDER BY p.position, r.position
            """
        ).fetchall()
    except sqlite3.Error as exc:
        raise RestorerError("Codex 项目数据库结构无法识别，已停止操作。") from exc
    finally:
        if "connection" in locals():
            connection.close()

    grouped: dict[str, dict] = {}
    for app_id, name, position, created, updated, _root_position, root in rows:
        record = grouped.setdefault(
            app_id,
            {
                "name": name,
                "position": position,
                "created": created,
                "updated": updated,
                "roots": [],
            },
        )
        if root:
            record["roots"].append(root)
    return [
        Project(
            app_id=app_id,
            name=record["name"],
            roots=tuple(record["roots"]),
            position=record["position"],
            created_at_ms=record["created"],
            updated_at_ms=record["updated"],
        )
        for app_id, record in grouped.items()
    ]


def normalize_name(name: str) -> str:
    return str(name).strip().casefold()


def normalize_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.expandvars(os.path.expanduser(str(path).strip()))))


def _current_indexes(state: dict) -> tuple[set[str], set[str], set[str]]:
    names: set[str] = set()
    paths: set[str] = set()
    for project in state.get("local-projects", {}).values():
        if isinstance(project, dict):
            names.add(normalize_name(project.get("name", "")))
            paths.update(normalize_path(path) for path in project.get("rootPaths", []) if path)
    mappings = state.get("app-server-project-id-by-legacy-project-id-by-host", {})
    app_ids = {
        app_id
        for host_mapping in mappings.values()
        if isinstance(host_mapping, dict)
        for app_id in host_mapping.values()
        if isinstance(app_id, str)
    }
    return names, paths, app_ids


def scan_projects(codex_home: Path) -> list[ScanItem]:
    codex_home = Path(codex_home)
    state = load_state(codex_home / STATE_NAME)
    history = load_history(codex_home / "state_5.sqlite")
    current_names, current_paths, mapped_app_ids = _current_indexes(state)
    items: list[ScanItem] = []
    for project in history:
        roots = {normalize_path(path) for path in project.roots}
        if project.app_id in mapped_app_ids:
            items.append(ScanItem(project, "current", "当前 Codex 已登记此项目"))
        elif normalize_name(project.name) in current_names:
            items.append(ScanItem(project, "current", "名称重复，保留当前 Codex 项目"))
        elif roots & current_paths:
            items.append(ScanItem(project, "current", "路径重复，保留当前 Codex 项目"))
        elif not any(Path(path).is_dir() for path in project.roots):
            items.append(ScanItem(project, "path_missing", "历史项目文件夹已不存在"))
        else:
            items.append(ScanItem(project, "missing", "可安全恢复"))
    return items


def _host_key(state: dict, codex_home: Path) -> str:
    mappings = state.get("app-server-project-id-by-legacy-project-id-by-host", {})
    expected = f"local:{codex_home}"
    if expected in mappings:
        return expected
    local_keys = [key for key, value in mappings.items() if key.startswith("local:") and isinstance(value, dict)]
    return local_keys[0] if len(local_keys) == 1 else expected


def merge_state(state: dict, projects: Iterable[Project], codex_home: Path) -> tuple[dict, list[str], list[str]]:
    merged = copy.deepcopy(state)
    local_projects = merged.setdefault("local-projects", {})
    project_order = merged.setdefault("project-order", [])
    mapping_root = merged.setdefault("app-server-project-id-by-legacy-project-id-by-host", {})
    host_mapping = mapping_root.setdefault(_host_key(merged, codex_home), {})
    current_names, current_paths, mapped_app_ids = _current_indexes(merged)
    added_names: list[str] = []
    added_app_ids: list[str] = []

    for project in sorted(projects, key=lambda item: item.position):
        existing_roots = [path for path in project.roots if Path(path).is_dir()]
        normalized_roots = {normalize_path(path) for path in existing_roots}
        if (
            not existing_roots
            or project.app_id in mapped_app_ids
            or normalize_name(project.name) in current_names
            or bool(normalized_roots & current_paths)
        ):
            continue
        legacy_id = str(uuid.uuid4())
        local_projects[legacy_id] = {
            "id": legacy_id,
            "name": project.name.strip(),
            "rootPaths": existing_roots,
            "createdAt": project.created_at_ms,
            "updatedAt": project.updated_at_ms,
        }
        project_order.append(legacy_id)
        host_mapping[legacy_id] = project.app_id
        current_names.add(normalize_name(project.name))
        current_paths.update(normalized_roots)
        mapped_app_ids.add(project.app_id)
        added_names.append(project.name.strip())
        added_app_ids.append(project.app_id)
    return merged, added_names, added_app_ids


def atomic_write_json(path: Path, data: dict) -> None:
    path = Path(path)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        load_state(temp_path)
        os.replace(temp_path, path)
    except (OSError, RestorerError) as exc:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)
        raise RestorerError(f"写入 Codex 状态文件失败：{exc}") from exc


def create_backup(codex_home: Path, prefix: str = "restore") -> Path:
    codex_home = Path(codex_home)
    backup_root = codex_home / BACKUP_ROOT_NAME
    backup_root.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_dir = backup_root / f"{prefix}-{stamp}"
    backup_dir.mkdir()
    try:
        for name in BACKUP_FILE_NAMES:
            source = codex_home / name
            if source.is_file():
                shutil.copy2(source, backup_dir / name)
        if not (backup_dir / STATE_NAME).is_file():
            raise RestorerError("备份中缺少 Codex 主状态文件。")
    except Exception:
        shutil.rmtree(backup_dir, ignore_errors=True)
        raise
    return backup_dir


def _restore_state_files(backup_dir: Path, codex_home: Path) -> None:
    source = Path(backup_dir) / STATE_NAME
    if not source.is_file():
        raise RestorerError(f"备份缺少主状态文件：{source}")
    shutil.copy2(source, Path(codex_home) / STATE_NAME)
    backup_state = Path(backup_dir) / f"{STATE_NAME}.bak"
    if backup_state.is_file():
        shutil.copy2(backup_state, Path(codex_home) / f"{STATE_NAME}.bak")


def _prune_backups(backup_root: Path, keep: Path) -> None:
    for path in Path(backup_root).iterdir():
        if path.is_dir() and path.resolve() != Path(keep).resolve():
            shutil.rmtree(path)


def _verify_restore(path: Path, before: dict, added_app_ids: list[str], codex_home: Path) -> None:
    after = load_state(path)
    for legacy_id, project in before.get("local-projects", {}).items():
        if after.get("local-projects", {}).get(legacy_id) != project:
            raise RestorerError(f"验证失败：现有项目 {project.get('name', legacy_id)} 被修改。")
    before_order = before.get("project-order", [])
    if after.get("project-order", [])[: len(before_order)] != before_order:
        raise RestorerError("验证失败：当前项目排序被修改。")
    mapping_root = after.get("app-server-project-id-by-legacy-project-id-by-host", {})
    host_mapping = mapping_root.get(_host_key(after, codex_home), {})
    mapped = set(host_mapping.values())
    if not set(added_app_ids).issubset(mapped):
        raise RestorerError("验证失败：恢复项目的 ID 映射不完整。")


def restore_projects(codex_home: Path, selected_app_ids: set[str], check_processes: bool = True) -> RestoreResult:
    codex_home = Path(codex_home)
    if check_processes:
        running = codex_processes_running()
        if running:
            raise RestorerError(f"Codex 仍在运行（{', '.join(running)}），请完全退出后再恢复。")
    before = load_state(codex_home / STATE_NAME)
    candidates = {
        item.project.app_id: item.project
        for item in scan_projects(codex_home)
        if item.status == "missing" and item.project.app_id in selected_app_ids
    }
    merged, added_names, added_app_ids = merge_state(before, candidates.values(), codex_home)
    if not added_names:
        raise RestorerError("没有选中可恢复的缺失项目。")
    backup_dir = create_backup(codex_home)
    try:
        atomic_write_json(codex_home / STATE_NAME, merged)
        _verify_restore(codex_home / STATE_NAME, before, added_app_ids, codex_home)
    except Exception as exc:
        _restore_state_files(backup_dir, codex_home)
        if isinstance(exc, RestorerError):
            raise
        raise RestorerError(f"恢复失败，已自动回滚：{exc}") from exc
    _prune_backups(backup_dir.parent, backup_dir)
    return RestoreResult(added_names=added_names, backup_dir=backup_dir)


def rollback_latest(codex_home: Path, check_processes: bool = True) -> RollbackResult:
    codex_home = Path(codex_home)
    if check_processes:
        running = codex_processes_running()
        if running:
            raise RestorerError(f"Codex 仍在运行（{', '.join(running)}），请完全退出后再回滚。")
    backup_root = codex_home / BACKUP_ROOT_NAME
    backups = sorted((path for path in backup_root.glob("*") if path.is_dir()), key=lambda path: path.name)
    if not backups:
        raise RestorerError("没有可用的恢复工具备份。")
    source_backup = backups[-1]
    safety_backup = create_backup(codex_home, prefix="pre-rollback")
    try:
        _restore_state_files(source_backup, codex_home)
        load_state(codex_home / STATE_NAME)
    except Exception as exc:
        _restore_state_files(safety_backup, codex_home)
        raise RestorerError(f"回滚失败，已恢复回滚前状态：{exc}") from exc
    _prune_backups(backup_root, safety_backup)
    return RollbackResult(restored_from=source_backup, safety_backup=safety_backup)


def codex_processes_from_tasklist(output: str) -> list[str]:
    found: list[str] = []
    for row in csv.reader(output.splitlines()):
        if row and row[0].casefold() in {"chatgpt.exe", "codex.exe"} and row[0] not in found:
            found.append(row[0])
    return found


def codex_processes_running() -> list[str]:
    if os.name != "nt":
        return []
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding=locale.getpreferredencoding(False),
            errors="replace",
            check=True,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RestorerError(f"无法确认 Codex 是否已退出：{exc}") from exc
    return codex_processes_from_tasklist(result.stdout)
