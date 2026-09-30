from __future__ import annotations

import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

import core


STATUS_TEXT = {
    "missing": "可恢复",
    "current": "已存在",
    "path_missing": "路径不存在",
}


class RestorerApp:
    def __init__(self, root: tk.Tk, codex_home: Path | None = None):
        self.root = root
        self.codex_home = Path(codex_home or (Path.home() / ".codex"))
        self.items: dict[str, tuple[core.ScanItem, tk.BooleanVar]] = {}
        self.root.title("Codex 项目恢复工具")
        self.root.geometry("920x650")
        self.root.minsize(760, 520)
        self._configure_style()
        self._build_ui()

    def _configure_style(self):
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 19, "bold"), foreground="#172033")
        style.configure("Sub.TLabel", font=("Microsoft YaHei UI", 10), foreground="#5f6b7f")
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 10, "bold"), foreground="#4352d7")
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(16, 9))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(13, 8))
        style.configure("TCheckbutton", font=("Microsoft YaHei UI", 10))

    def _build_ui(self):
        self.root.configure(bg="#f3f6fb")
        outer = tk.Frame(self.root, bg="#f3f6fb", padx=24, pady=20)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg="#f3f6fb")
        header.pack(fill="x")
        ttk.Label(header, text="Codex 项目恢复工具", style="Title.TLabel", background="#f3f6fb").pack(anchor="w")
        ttk.Label(
            header,
            text="扫描历史项目，只补充当前 Codex 中缺失的项目；同名或同路径项目永远保留当前版本。",
            style="Sub.TLabel",
            background="#f3f6fb",
        ).pack(anchor="w", pady=(5, 0))

        notice = tk.Label(
            outer,
            text="恢复或回滚前，请先从系统托盘完全退出 Codex。扫描项目可以在 Codex 运行时进行。",
            bg="#fff4d7",
            fg="#725100",
            anchor="w",
            padx=14,
            pady=10,
            font=("Microsoft YaHei UI", 10),
        )
        notice.pack(fill="x", pady=(16, 12))

        actions = ttk.Frame(outer)
        actions.pack(fill="x")
        ttk.Button(actions, text="扫描项目", command=self.scan, style="Accent.TButton").pack(side="left")
        self.restore_button = ttk.Button(actions, text="恢复勾选项目", command=self.restore, state="disabled")
        self.restore_button.pack(side="left", padx=8)
        self.rollback_button = ttk.Button(actions, text="一键回滚", command=self.rollback, state="disabled")
        self.rollback_button.pack(side="left")
        self.status_var = tk.StringVar(value="等待扫描")
        ttk.Label(actions, textvariable=self.status_var, style="Status.TLabel").pack(side="right")

        list_card = tk.Frame(outer, bg="white", highlightbackground="#dfe5ef", highlightthickness=1)
        list_card.pack(fill="both", expand=True, pady=(12, 10))
        headings = tk.Frame(list_card, bg="#edf1f7")
        headings.pack(fill="x")
        for text, width in (("选择 / 状态", 18), ("项目名称", 24), ("历史路径", 70)):
            tk.Label(
                headings,
                text=text,
                width=width,
                anchor="w",
                bg="#edf1f7",
                fg="#344054",
                padx=9,
                pady=8,
                font=("Microsoft YaHei UI", 9, "bold"),
            ).pack(side="left")

        self.canvas = tk.Canvas(list_card, bg="white", highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_card, orient="vertical", command=self.canvas.yview)
        self.rows = tk.Frame(self.canvas, bg="white")
        self.rows.bind("<Configure>", lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.window_id = self.canvas.create_window((0, 0), window=self.rows, anchor="nw")
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(self.window_id, width=event.width))
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.log = tk.Text(
            outer,
            height=5,
            bg="#101828",
            fg="#d0d5dd",
            insertbackground="white",
            relief="flat",
            font=("Consolas", 9),
            state="disabled",
        )
        self.log.pack(fill="x")
        self._log("程序已就绪。点击“扫描项目”开始。")

    def _log(self, message: str):
        line = f"[{datetime.now():%H:%M:%S}] {message}"
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        try:
            with (self.codex_home / "project-restorer.log").open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass

    def _clear_rows(self):
        for child in self.rows.winfo_children():
            child.destroy()
        self.items.clear()

    def scan(self):
        self._clear_rows()
        self.status_var.set("正在扫描…")
        self.root.update_idletasks()
        try:
            results = core.scan_projects(self.codex_home)
            running = core.codex_processes_running()
        except core.RestorerError as exc:
            self.status_var.set("扫描失败")
            self._log(str(exc))
            messagebox.showerror("扫描失败", str(exc), parent=self.root)
            return

        recoverable = 0
        for index, item in enumerate(results):
            row = tk.Frame(self.rows, bg="white" if index % 2 == 0 else "#fafbfc")
            row.pack(fill="x")
            variable = tk.BooleanVar(value=item.selected_by_default)
            check = ttk.Checkbutton(row, text=STATUS_TEXT[item.status], variable=variable)
            check.configure(state="normal" if item.status == "missing" else "disabled")
            check.pack(side="left", padx=(9, 4), pady=7)
            tk.Label(
                row,
                text=item.project.name.strip(),
                width=25,
                anchor="w",
                bg=row["bg"],
                fg="#172033",
                font=("Microsoft YaHei UI", 10),
            ).pack(side="left", padx=(4, 8))
            tk.Label(
                row,
                text="；".join(item.project.roots) or "无历史路径",
                anchor="w",
                bg=row["bg"],
                fg="#667085",
                font=("Microsoft YaHei UI", 9),
            ).pack(side="left", fill="x", expand=True, padx=(0, 10))
            self.items[item.project.app_id] = (item, variable)
            recoverable += item.status == "missing"

        if not results:
            tk.Label(self.rows, text="数据库中没有历史项目记录。", bg="white", fg="#667085", pady=22).pack()
        backup_root = self.codex_home / core.BACKUP_ROOT_NAME
        has_backup = backup_root.is_dir() and any(path.is_dir() for path in backup_root.iterdir())
        write_allowed = not running
        self.restore_button.configure(state="normal" if recoverable and write_allowed else "disabled")
        self.rollback_button.configure(state="normal" if has_backup and write_allowed else "disabled")
        if running:
            self.status_var.set(f"发现 {recoverable} 个缺失项目；请先退出 Codex")
            self._log(f"扫描完成，但检测到 Codex 正在运行：{', '.join(running)}。")
        else:
            self.status_var.set(f"发现 {recoverable} 个可恢复项目")
            self._log(f"扫描完成：{recoverable} 个缺失项目可恢复。")

    def restore(self):
        selected = {
            app_id
            for app_id, (item, variable) in self.items.items()
            if item.status == "missing" and variable.get()
        }
        if not selected:
            messagebox.showinfo("没有选择", "请至少勾选一个可恢复项目。", parent=self.root)
            return
        if not messagebox.askyesno(
            "确认恢复",
            f"将恢复 {len(selected)} 个项目。程序会先备份状态，并且不会覆盖当前项目。是否继续？",
            parent=self.root,
        ):
            return
        try:
            result = core.restore_projects(self.codex_home, selected)
        except core.RestorerError as exc:
            self._log(str(exc))
            messagebox.showerror("恢复失败", str(exc), parent=self.root)
            return
        names = "、".join(result.added_names)
        self._log(f"恢复成功：{names}。备份：{result.backup_dir}")
        messagebox.showinfo("恢复成功", f"已恢复：{names}\n\n现在可以重新启动 Codex。", parent=self.root)
        self.scan()

    def rollback(self):
        if not messagebox.askyesno(
            "确认回滚",
            "将恢复最近一次备份，并保留回滚前状态作为新的安全备份。是否继续？",
            parent=self.root,
        ):
            return
        try:
            result = core.rollback_latest(self.codex_home)
        except core.RestorerError as exc:
            self._log(str(exc))
            messagebox.showerror("回滚失败", str(exc), parent=self.root)
            return
        self._log(f"回滚成功，来源：{result.restored_from}")
        messagebox.showinfo("回滚成功", "已恢复最近一次备份。现在可以重新启动 Codex。", parent=self.root)
        self.scan()
