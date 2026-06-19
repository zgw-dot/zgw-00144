"""数据持久化模块 - JSON 存储"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime

from .models import DefectRecord, ImportLogEntry, ReviewLogEntry, DraftEntry, DraftTemplate, AuditLogEntry, TemplateVersion


class PatrolState:
    """巡检复核状态存储"""

    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.defects_file = self.data_dir / "defects.json"
        self.undo_stack_file = self.data_dir / "undo_stack.json"
        self.meta_file = self.data_dir / "meta.json"
        self.import_log_file = self.data_dir / "import_log.json"
        self.review_log_file = self.data_dir / "review_log.json"
        self.drafts_file = self.data_dir / "drafts.json"
        self.templates_file = self.data_dir / "templates.json"
        self.audit_log_file = self.data_dir / "audit_log.json"
        self.versions_file = self.data_dir / "versions.json"

        self.defects: Dict[str, DefectRecord] = {}
        self.undo_stack: List[Dict[str, Any]] = []
        self.batch_id: str = ""
        self.imported_files: List[str] = []
        self.import_logs: List[ImportLogEntry] = []
        self.review_logs: List[ReviewLogEntry] = []
        self.drafts: Dict[str, DraftEntry] = {}
        self.templates: Dict[str, DraftTemplate] = {}
        self.audit_logs: List[AuditLogEntry] = []
        self.versions: Dict[str, TemplateVersion] = {}

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

        if self.review_log_file.exists():
            with open(self.review_log_file, "r", encoding="utf-8") as f:
                review_data = json.load(f)
                self.review_logs = [ReviewLogEntry.from_dict(d) for d in review_data]

        if self.drafts_file.exists():
            with open(self.drafts_file, "r", encoding="utf-8") as f:
                drafts_data = json.load(f)
                self.drafts = {}
                for draft_id, draft_data in drafts_data.items():
                    self.drafts[draft_id] = DraftEntry.from_dict(draft_data)

        if self.templates_file.exists():
            with open(self.templates_file, "r", encoding="utf-8") as f:
                templates_data = json.load(f)
                self.templates = {}
                for tpl_id, tpl_data in templates_data.items():
                    self.templates[tpl_id] = DraftTemplate.from_dict(tpl_data)

        if self.audit_log_file.exists():
            with open(self.audit_log_file, "r", encoding="utf-8") as f:
                audit_data = json.load(f)
                self.audit_logs = [AuditLogEntry.from_dict(d) for d in audit_data]

        if self.versions_file.exists():
            with open(self.versions_file, "r", encoding="utf-8") as f:
                versions_data = json.load(f)
                self.versions = {}
                for ver_id, ver_data in versions_data.items():
                    self.versions[ver_id] = TemplateVersion.from_dict(ver_data)

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
        self.save_review_logs()
        self.save_drafts()
        self.save_templates()
        self.save_audit_logs()
        self.save_versions()

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

    def push_undo(self, action: str, snapshot: Dict[str, Any], review_entries: Optional[List[Dict[str, Any]]] = None):
        """推入撤销栈"""
        item = {
            "action": action,
            "timestamp": datetime.now().isoformat(),
            "snapshot": snapshot
        }
        if review_entries:
            item["review_entries"] = review_entries
        self.undo_stack.append(item)

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
        """获取缺陷快照（用于撤销），包含 imported_files 和 import_logs 状态"""
        snapshot = {
            defect_id: defect.to_dict()
            for defect_id, defect in self.defects.items()
        }
        snapshot["__imported_files__"] = list(self.imported_files)
        snapshot["__import_logs__"] = [log.to_dict() for log in self.import_logs]
        return snapshot

    def restore_defects(self, snapshot: Dict[str, Any]):
        """从快照恢复缺陷、imported_files 和 import_logs"""
        self.defects = {}
        for key, def_data in snapshot.items():
            if key == "__imported_files__":
                self.imported_files = list(def_data)
                continue
            if key == "__import_logs__":
                self.import_logs = [ImportLogEntry.from_dict(d) for d in def_data]
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

    def add_review_log(self, log_entry: ReviewLogEntry):
        """添加复核日志"""
        self.review_logs.append(log_entry)

    def save_review_logs(self):
        """单独保存复核日志"""
        logs_data = [log.to_dict() for log in self.review_logs]
        with open(self.review_log_file, "w", encoding="utf-8") as f:
            json.dump(logs_data, f, ensure_ascii=False, indent=2)

    def get_review_logs(
        self,
        defect_id: str = "",
        handler: str = "",
        log_type: str = "",
        draft_id: str = "",
        limit: int = 0
    ) -> List[ReviewLogEntry]:
        """获取复核日志，按时间倒序，支持筛选"""
        logs = sorted(self.review_logs, key=lambda x: x.timestamp, reverse=True)
        if defect_id:
            logs = [l for l in logs if l.defect_id == defect_id]
        if handler:
            logs = [l for l in logs if l.handler == handler]
        if log_type:
            logs = [l for l in logs if l.log_type == log_type]
        if draft_id:
            logs = [l for l in logs if l.draft_id == draft_id]
        if limit > 0:
            return logs[:limit]
        return logs

    def get_last_review_log(self, defect_id: str = "") -> Optional[ReviewLogEntry]:
        """获取最近一次复核日志"""
        logs = self.get_review_logs(defect_id=defect_id)
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

    def save_drafts(self):
        """单独保存草稿"""
        drafts_data = {
            draft_id: draft.to_dict()
            for draft_id, draft in self.drafts.items()
        }
        with open(self.drafts_file, "w", encoding="utf-8") as f:
            json.dump(drafts_data, f, ensure_ascii=False, indent=2)

    def add_draft(self, draft: DraftEntry):
        """添加草稿"""
        self.drafts[draft.draft_id] = draft

    def get_draft(self, draft_id: str) -> Optional[DraftEntry]:
        """获取草稿"""
        return self.drafts.get(draft_id)

    def update_draft(self, draft: DraftEntry):
        """更新草稿"""
        if draft.draft_id in self.drafts:
            self.drafts[draft.draft_id] = draft

    def list_drafts(
        self,
        status: Optional[str] = None,
        limit: int = 0
    ) -> List[DraftEntry]:
        """列出草稿，按创建时间倒序"""
        drafts = list(self.drafts.values())
        drafts.sort(key=lambda d: d.created_at, reverse=True)
        if status:
            drafts = [d for d in drafts if d.status == status]
        if limit > 0:
            drafts = drafts[:limit]
        return drafts

    def get_drafts_for_defect(self, defect_id: str) -> List[DraftEntry]:
        """查询缺陷相关的草稿（包含该缺陷的草稿）"""
        result = []
        for draft in self.drafts.values():
            if any(item.defect_id == defect_id for item in draft.items):
                result.append(draft)
        result.sort(key=lambda d: d.created_at, reverse=True)
        return result

    def save_templates(self):
        """单独保存模板"""
        templates_data = {
            tpl_id: tpl.to_dict()
            for tpl_id, tpl in self.templates.items()
        }
        with open(self.templates_file, "w", encoding="utf-8") as f:
            json.dump(templates_data, f, ensure_ascii=False, indent=2)

    def add_template(self, template: DraftTemplate):
        """添加模板"""
        self.templates[template.template_id] = template

    def get_template(self, template_id: str) -> Optional[DraftTemplate]:
        """获取模板"""
        return self.templates.get(template_id)

    def get_template_by_name(self, name: str) -> Optional[DraftTemplate]:
        """按名称获取模板"""
        for tpl in self.templates.values():
            if tpl.name == name:
                return tpl
        return None

    def update_template(self, template: DraftTemplate):
        """更新模板"""
        if template.template_id in self.templates:
            self.templates[template.template_id] = template

    def delete_template(self, template_id: str) -> bool:
        """删除模板，返回是否成功"""
        if template_id in self.templates:
            del self.templates[template_id]
            return True
        return False

    def list_templates(self) -> List[DraftTemplate]:
        """列出所有模板，按创建时间倒序"""
        templates = list(self.templates.values())
        templates.sort(key=lambda t: t.created_at, reverse=True)
        return templates

    def add_audit_log(self, entry: AuditLogEntry):
        """添加审计日志"""
        self.audit_logs.append(entry)

    def save_audit_logs(self):
        """单独保存审计日志"""
        logs_data = [log.to_dict() for log in self.audit_logs]
        with open(self.audit_log_file, "w", encoding="utf-8") as f:
            json.dump(logs_data, f, ensure_ascii=False, indent=2)

    def get_audit_logs(
        self,
        action: str = "",
        target_type: str = "",
        target_id: str = "",
        limit: int = 0
    ) -> List[AuditLogEntry]:
        """获取审计日志，按时间倒序，支持筛选"""
        logs = sorted(self.audit_logs, key=lambda x: x.timestamp, reverse=True)
        if action:
            logs = [l for l in logs if l.action == action]
        if target_type:
            logs = [l for l in logs if l.target_type == target_type]
        if target_id:
            logs = [l for l in logs if l.target_id == target_id]
        if limit > 0:
            return logs[:limit]
        return logs

    def save_versions(self):
        versions_data = {
            ver_id: ver.to_dict()
            for ver_id, ver in self.versions.items()
        }
        with open(self.versions_file, "w", encoding="utf-8") as f:
            json.dump(versions_data, f, ensure_ascii=False, indent=2)

    def add_version(self, version: TemplateVersion):
        self.versions[version.version_id] = version

    def get_version(self, version_id: str) -> Optional[TemplateVersion]:
        return self.versions.get(version_id)

    def get_version_by_name(self, template_id: str, version_name: str) -> Optional[TemplateVersion]:
        for ver in self.versions.values():
            if ver.template_id == template_id and ver.version_name == version_name:
                return ver
        return None

    def update_version(self, version: TemplateVersion):
        if version.version_id in self.versions:
            self.versions[version.version_id] = version

    def delete_version(self, version_id: str) -> bool:
        if version_id in self.versions:
            del self.versions[version_id]
            return True
        return False

    def list_versions(self, template_id: str = "") -> List[TemplateVersion]:
        versions = list(self.versions.values())
        if template_id:
            versions = [v for v in versions if v.template_id == template_id]
        versions.sort(key=lambda v: v.published_at, reverse=True)
        return versions

    def delete_versions_by_template(self, template_id: str) -> int:
        to_delete = [vid for vid, v in self.versions.items() if v.template_id == template_id]
        for vid in to_delete:
            del self.versions[vid]
        return len(to_delete)
