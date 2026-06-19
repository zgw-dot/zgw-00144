"""数据持久化模块 - JSON 存储"""

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime

from .models import DefectRecord, SourceRow, ImportLogEntry


class PatrolState:
    """巡检复核状态存储"""

    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.defects_file = self.data_dir / "defects.json"
        self.undo_stack_file = self.data_dir / "undo_stack.json"
        self.meta_file = self.data_dir / "meta.json"
        self.import_log_file = self.data_dir / "import_log.json"

        self.defects: Dict[str, DefectRecord] = {}
        self.undo_stack: List[Dict[str, Any]] = []
        self.batch_id: str = ""
        self.imported_files: List[str] = []
        self.import_logs: List[ImportLogEntry] = []

        self._load()

    def _load(self):
        """从磁盘加载状态"""
        if self.defects_file.exists():
            with open(self.defects_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.defects = {}
                for def_id, def_data in data.items():
                    self.defects[def_id] = DefectRecord.from_dict(def_data)

        if self.undo_stack_file.exists():
            with open(self.undo_stack_file, "r", encoding="utf-8") as f:
                self.undo_stack = json.load(f)

        if self.meta_file.exists():
            with open(self.meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
                self.batch_id = meta.get("batch_id", "")
                self.imported_files = meta.get("imported_files", [])

        if self.import_log_file.exists():
            with open(self.import_log_file, "r", encoding="utf-8") as f:
                logs_data = json.load(f)
                self.import_logs = [ImportLogEntry.from_dict(d) for d in logs_data]

    def save(self):
        """保存状态到磁盘"""
        defects_data = {
            def_id: defect.to_dict()
            for def_id, defect in self.defects.items()
        }
        with open(self.defects_file, "w", encoding="utf-8") as f:
            json.dump(defects_data, f, ensure_ascii=False, indent=2)

        with open(self.undo_stack_file, "w", encoding="utf-8") as f:
            json.dump(self.undo_stack, f, ensure_ascii=False, indent=2)

        meta = {
            "batch_id": self.batch_id,
            "imported_files": self.imported_files,
            "last_updated": datetime.now().isoformat()
        }
        with open(self.meta_file, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        self.save_import_logs()

    def init_batch(self, batch_id: str):
        """初始化新批次"""
        if not self.batch_id:
            self.batch_id = batch_id
        else:
            if self.batch_id != batch_id:
                raise ValueError(
                    f"当前批次为 {self.batch_id}，不能切换为 {batch_id}。"
                    f"如需新建批次，请清空 data 目录或使用 --batch 指定。"
                )

    def add_defect(self, defect: DefectRecord):
        """添加缺陷"""
        self.defects[defect.defect_id] = defect

    def get_defect(self, defect_id: str) -> Optional[DefectRecord]:
        """获取缺陷"""
        return self.defects.get(defect_id)

    def list_defects(self, status: Optional[str] = None, building: Optional[str] = None) -> List[DefectRecord]:
        """列出缺陷"""
        result = list(self.defects.values())
        if status:
            result = [d for d in result if d.status == status]
        if building:
            result = [d for d in result if d.building == building]
        result.sort(key=lambda d: d.first_seen, reverse=True)
        return result

    def push_undo(self, action: str, snapshot: Dict[str, Any]):
        """推入撤销栈"""
        self.undo_stack.append({
            "action": action,
            "timestamp": datetime.now().isoformat(),
            "snapshot": snapshot
        })

    def pop_undo(self) -> Optional[Dict[str, Any]]:
        """弹出撤销项"""
        if not self.undo_stack:
            return None
        return self.undo_stack.pop()

    def can_undo(self) -> bool:
        """是否可以撤销"""
        return len(self.undo_stack) > 0

    def mark_file_imported(self, filename: str):
        """标记文件已导入"""
        if filename not in self.imported_files:
            self.imported_files.append(filename)

    def is_file_imported(self, filename: str) -> bool:
        """检查文件是否已导入"""
        return filename in self.imported_files

    def snapshot_defects(self) -> Dict[str, Any]:
        """获取缺陷快照（用于撤销），包含 imported_files 状态"""
        snapshot = {
            defect_id: defect.to_dict()
            for defect_id, defect in self.defects.items()
        }
        snapshot["__imported_files__"] = list(self.imported_files)
        return snapshot

    def restore_defects(self, snapshot: Dict[str, Any]):
        """从快照恢复缺陷和 imported_files"""
        self.defects = {}
        for key, def_data in snapshot.items():
            if key == "__imported_files__":
                self.imported_files = list(def_data)
                continue
            self.defects[key] = DefectRecord.from_dict(def_data)

    def add_import_log(self, log_entry: ImportLogEntry):
        """添加导入日志"""
        self.import_logs.append(log_entry)

    def save_import_logs(self):
        """单独保存导入日志"""
        logs_data = [log.to_dict() for log in self.import_logs]
        with open(self.import_log_file, "w", encoding="utf-8") as f:
            json.dump(logs_data, f, ensure_ascii=False, indent=2)

    def get_import_logs(self, limit: int = 0) -> List[ImportLogEntry]:
        """获取导入日志，按时间倒序"""
        logs = sorted(self.import_logs, key=lambda x: x.timestamp, reverse=True)
        if limit > 0:
            return logs[:limit]
        return logs

    def get_last_import_log(self, log_type: str = "") -> Optional[ImportLogEntry]:
        """获取最近一次导入/预检日志"""
        logs = self.get_import_logs()
        if log_type:
            logs = [l for l in logs if l.log_type == log_type]
        return logs[0] if logs else None

    def stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        status_counts = {}
        for defect in self.defects.values():
            status_counts[defect.status] = status_counts.get(defect.status, 0) + 1

        building_counts = {}
        for defect in self.defects.values():
            building_counts[defect.building] = building_counts.get(defect.building, 0) + 1

        return {
            "total": len(self.defects),
            "by_status": status_counts,
            "by_building": building_counts,
            "imported_files": len(self.imported_files),
            "undo_stack_size": len(self.undo_stack),
        }
