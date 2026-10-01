from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .domain import (ConflictError, PermissionDenied, ensure_role,
                      normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (ATTACHMENT_UPLOAD_ROLES, AUDIT_ROLES, CREATE_ROLES, ENTITY,
                    RECORD_ROLES, RENEWAL_APPROVE_ROLES, RENEWAL_CREATE_ROLES,
                    RENEWAL_ENTITY, SUPERSEDED, TITLE, VIEW_ROLES,
                    can_add_record, can_view_attachment, completion_blockers,
                    escalation_required, priority_score, renewal_blockers,
                    response_deadline_hours, role_for_transition,
                    validate_equipment_list, validate_transition)


def _snapshot_records(records):
    return [
        {
            "id": r["id"],
            "item_id": r["item_id"],
            "kind": r["kind"],
            "detail": r["detail"],
            "status": r["status"],
            "external_ref": r["external_ref"],
            "created_by": r["created_by"],
            "created_at": r["created_at"],
        }
        for r in records
    ]


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        equipment = validate_equipment_list(payload.get("equipment_list"))
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        facility_id = payload.get("facility_id")
        if facility_id is not None:
            facility_id = require_text(facility_id, "facility_id", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor,
                                           facility_id, equipment)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
            "equipment_count": len(equipment),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def close_record(self, item_id: int, record_id: int, actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if not can_add_record(item["status"]):
            raise ConflictError("许可已被替代，不能处理检查或整改记录")
        record = self.repository.close_record(item_id, record_id)
        self.repository.append_audit("record_close", ENTITY, item_id, actor, {
            "record_id": record_id,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    # ---- renewals ----
    def create_renewal(self, payload: Dict[str, Any], actor: str, role: str,
                        request_id: Optional[str] = None) -> Dict[str, Any]:
        ensure_role(role, RENEWAL_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        if request_id is not None:
            request_id = require_text(request_id, "request_id", 100)
            cached = self.repository.get_idempotency(request_id)
            if cached is not None:
                return cached["response"]
        old_item_id = payload.get("old_item_id")
        if not isinstance(old_item_id, int) or old_item_id < 1:
            raise ValidationError("old_item_id无效")
        old = self.repository.get_item(old_item_id)
        if old["status"] == SUPERSEDED:
            raise ConflictError("旧许可已被替代，不能再次续期")
        title = require_text(payload.get("title"), "title", 200)
        description = payload.get("description")
        if description is not None:
            description = require_text(description, "description")
        severity = normalize_severity(payload.get("severity", old["severity"]))
        quantity = require_number(payload.get("quantity", old["quantity"]), "quantity")
        threshold = require_number(
            payload.get("threshold", old["threshold"]), "threshold", 0.000001)
        equipment = validate_equipment_list(payload.get("equipment_list"), required=True)
        facility_id = old["facility_id"] or str(old["id"])
        records = self.repository.facility_records(facility_id)
        open_count = sum(1 for r in records if r["status"] == "open")
        snapshot = _snapshot_records(records)
        renewal = self.repository.create_renewal(
            request_id, facility_id, old_item_id, old["version"], title, description,
            severity, quantity, threshold, equipment, snapshot, open_count, actor)
        self.repository.append_audit("renewal_create", RENEWAL_ENTITY, renewal["id"], actor, {
            "old_item_id": old_item_id, "old_version": old["version"],
            "facility_id": facility_id, "equipment_count": len(equipment),
            "open_count": open_count, "blocked": open_count > 0,
            "request_id": request_id,
        })
        result = self.enrich_renewal(renewal)
        if request_id:
            self.repository.save_idempotency(request_id, RENEWAL_ENTITY, renewal["id"], result)
        return result

    def approve_renewal(self, renewal_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RENEWAL_APPROVE_ROLES)
        actor = require_text(actor, "actor", 100)
        renewal = self.repository.get_renewal(renewal_id)
        if renewal["status"] == "approved":
            return self.enrich_renewal(renewal)
        facility_id = renewal["facility_id"]
        open_count = self.repository.facility_open_record_count(facility_id)
        if open_count > 0:
            raise ConflictError("；".join(renewal_blockers(open_count)))
        equipment = json.loads(renewal["equipment_list"]) \
            if isinstance(renewal["equipment_list"], str) else renewal["equipment_list"]
        new_item = self.repository.create_item(
            renewal["title"], renewal["description"] or "", renewal["severity"],
            renewal["quantity"], renewal["threshold"], None, actor,
            facility_id, equipment)
        self.repository.mark_item_superseded(renewal["old_item_id"])
        final_records = self.repository.facility_records(facility_id)
        final_snapshot = _snapshot_records(final_records)
        updated = self.repository.approve_renewal(
            renewal_id, new_item["id"], new_item["version"], final_snapshot, 0)
        self.repository.append_audit("renewal_approve", RENEWAL_ENTITY, renewal_id, actor, {
            "old_item_id": renewal["old_item_id"],
            "new_item_id": new_item["id"], "new_version": new_item["version"],
            "facility_id": facility_id, "equipment_count": len(equipment),
        })
        return self.enrich_renewal(updated)

    def get_renewal(self, renewal_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich_renewal(self.repository.get_renewal(renewal_id))

    def list_renewals(self, role: str) -> list:
        self._view(role)
        return [self.enrich_renewal(r) for r in self.repository.list_renewals()]

    # ---- attachments ----
    def _inspector_authorized(self, item: Dict[str, Any], actor: str) -> bool:
        facility_id = item["facility_id"] or str(item["id"])
        return self.repository.inspector_authorized(actor, facility_id)

    def upload_attachment(self, item_id: int, payload: Dict[str, Any], actor: str,
                          role: str) -> Dict[str, Any]:
        ensure_role(role, ATTACHMENT_UPLOAD_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["created_by"] != actor:
            raise PermissionDenied("只能为本机构的许可上传附件")
        filename = require_text(payload.get("filename"), "filename", 255)
        content_type = payload.get("content_type", "text/plain")
        data = require_text(payload.get("data"), "data", 2_000_000)
        attachment = self.repository.create_attachment(
            item_id, filename, content_type, data, actor)
        self.repository.append_audit("attachment_upload", ENTITY, item_id, actor, {
            "attachment_id": attachment["id"], "filename": filename,
        })
        return attachment

    def _attachment_guard(self, item: Dict[str, Any], actor: str, role: str) -> None:
        authorized = self._inspector_authorized(item, actor) if role == "inspector" else False
        if not can_view_attachment(role, item, actor, authorized):
            raise PermissionDenied("无权查看该附件")

    def list_attachments(self, item_id: int, actor: str, role: str) -> list:
        self._view(role)
        item = self.repository.get_item(item_id)
        self._attachment_guard(item, actor, role)
        return self.repository.list_attachments(item_id)

    def view_attachment(self, item_id: int, attachment_id: int, actor: str,
                        role: str) -> Dict[str, Any]:
        self._view(role)
        item = self.repository.get_item(item_id)
        self._attachment_guard(item, actor, role)
        attachment = self.repository.get_attachment(attachment_id)
        if attachment["item_id"] != item_id:
            from .domain import NotFoundError
            raise NotFoundError("附件不存在")
        return attachment

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        if isinstance(result.get("equipment_list"), str):
            result["equipment_list"] = json.loads(result["equipment_list"])
        return result

    @staticmethod
    def enrich_renewal(renewal: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(renewal)
        if isinstance(result.get("equipment_list"), str):
            result["equipment_list"] = json.loads(result["equipment_list"])
        if isinstance(result.get("records_snapshot"), str):
            result["records_snapshot"] = json.loads(result["records_snapshot"])
        result["blocked"] = result["open_count"] > 0
        result["blockers"] = renewal_blockers(result["open_count"])
        return result
