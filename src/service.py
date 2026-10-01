from __future__ import annotations

from typing import Any, Dict, Optional

from .audit import utc_now
from .domain import (ConflictError, NotFoundError, ValidationError, ensure_role,
                     normalize_emission_limits, normalize_equipment,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (ATTACHMENT_UPLOAD_ROLES, ATTACHMENT_VIEW_ROLES, AUDIT_ROLES,
                    CREATE_ROLES, DEFAULT_FACILITY, ENTITY, INSPECTION_KIND,
                    RECORD_ROLES, RECTIFICATION_KIND, RENEWAL_CREATE_ROLES,
                    RENEWAL_ENTITY, RENEWAL_STATES, TITLE, VIEW_ROLES,
                    completion_blockers, escalation_required,
                    limit_equipment_gaps, priority_score, renewal_blockers,
                    renewal_transition_role, response_deadline_hours,
                    role_for_transition, validate_renewal_transition,
                    validate_transition)


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
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        facility = payload.get("facility")
        if facility is None:
            facility = DEFAULT_FACILITY
        else:
            facility = require_text(facility, "facility", 100)
        equipment = normalize_equipment(payload.get("equipment"))
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor, facility,
                                           equipment)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "facility": facility,
            "priority": priority_score(severity, quantity, threshold),
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
        item = self.repository.get_item(item_id)
        if external_ref is not None and self.repository.find_record_by_external_ref_in_facility(
                item["facility"], external_ref) is not None:
            raise ConflictError("同一设施下记录唯一标识已存在")
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
            from .domain import ConflictError
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

    def create_renewal(self, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, RENEWAL_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        request_id = require_text(payload.get("request_id"), "request_id", 100)
        permit_id = payload.get("permit_id")
        if not isinstance(permit_id, int) or isinstance(permit_id, bool) or permit_id < 1:
            raise ValidationError("permit_id必须是正整数")
        permit = self.repository.get_item(permit_id)
        facility = payload.get("facility")
        if facility is None:
            facility = permit["facility"]
        else:
            facility = require_text(facility, "facility", 100)
            if facility != permit["facility"]:
                raise ValidationError("facility必须与旧许可所属设施一致")
        emission_limits = normalize_emission_limits(payload.get("emission_limits"))
        equipment = permit["equipment"]
        gaps = limit_equipment_gaps(emission_limits, equipment)
        if gaps:
            raise ValidationError("排放上限与治理设备名单不一致：" + "、".join(gaps))
        try:
            renewal = self.repository.create_renewal(
                request_id, facility, permit_id, permit["version"], equipment,
                emission_limits, actor)
        except ConflictError:
            existing = self.repository.get_renewal_by_request(request_id)
            if existing is None:
                raise
            result = self.enrich_renewal(existing)
            result["replayed"] = True
            return result
        self.repository.append_audit("renewal_create", RENEWAL_ENTITY,
                                     renewal["id"], actor, {
                                         "request_id": request_id,
                                         "permit_id": permit_id,
                                         "permit_version": permit["version"],
                                         "facility": facility,
                                     })
        result = self.enrich_renewal(renewal)
        result["replayed"] = False
        return result

    def get_renewal(self, renewal_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich_renewal(self.repository.get_renewal(renewal_id))

    def get_renewal_by_request(self, request_id: str, role: str) -> Dict[str, Any]:
        self._view(role)
        renewal = self.repository.get_renewal_by_request(request_id)
        if renewal is None:
            raise NotFoundError("续期单不存在")
        return self.enrich_renewal(renewal)

    def list_renewals(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        if status is not None and status not in RENEWAL_STATES:
            raise ValidationError("未知状态")
        return [self.enrich_renewal(renewal)
                for renewal in self.repository.list_renewals(status)]

    def transition_renewal(self, renewal_id: int, target: str,
                           expected_version: int, actor: str,
                           role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        renewal = self.repository.get_renewal(renewal_id)
        validate_renewal_transition(renewal["status"], target)
        ensure_role(role, renewal_transition_role(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValidationError("expected_version必须是正整数")
        snapshot = None
        if target == RENEWAL_STATES[-1]:
            permit = self.repository.get_item(renewal["permit_id"])
            problems = []
            if permit["version"] != renewal["permit_version"]:
                problems.append(
                    f"旧许可版本已变更（草案基于v{renewal['permit_version']}，"
                    f"当前v{permit['version']}）")
            if permit["equipment"] != renewal["equipment"]:
                problems.append("治理设备名单与旧许可不一致")
            gaps = limit_equipment_gaps(renewal["emission_limits"],
                                        renewal["equipment"])
            if gaps:
                problems.append("排放上限与治理设备名单不一致：" + "、".join(gaps))
            records = self.repository.records_for_facility(renewal["facility"])
            open_rectifications = [r for r in records
                                   if r["kind"] == RECTIFICATION_KIND
                                   and r["status"] == "open"]
            problems.extend(renewal_blockers(open_rectifications))
            if problems:
                raise ConflictError("；".join(problems))
            snapshot = {
                "old_permit": {
                    "id": permit["id"], "title": permit["title"],
                    "version": permit["version"], "status": permit["status"],
                    "facility": permit["facility"],
                    "equipment": permit["equipment"],
                    "quantity": permit["quantity"],
                    "threshold": permit["threshold"],
                },
                "new_permit": {
                    "renewal_id": renewal["id"], "facility": renewal["facility"],
                    "equipment": renewal["equipment"],
                    "emission_limits": renewal["emission_limits"],
                    "based_on_permit_version": renewal["permit_version"],
                },
                "records": records,
                "progress": {
                    "total": len(records),
                    "open": sum(1 for r in records if r["status"] == "open"),
                    "closed": sum(1 for r in records if r["status"] == "closed"),
                    "open_rectifications": [r["id"] for r in open_rectifications],
                },
                "approved_by": actor,
                "frozen_at": utc_now(),
            }
        updated = self.repository.transition_renewal(
            renewal_id, target, expected_version, snapshot, actor)
        self.repository.append_audit("renewal_transition", RENEWAL_ENTITY,
                                     renewal_id, actor, {
                                         "from": renewal["status"], "to": target,
                                     })
        return self.enrich_renewal(updated)

    def upload_attachment(self, renewal_id: int, payload: Dict[str, Any],
                          actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, ATTACHMENT_UPLOAD_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 200)
        content = require_text(payload.get("content"), "content", 100000)
        renewal = self.repository.get_renewal(renewal_id)
        if renewal["status"] == RENEWAL_STATES[-1]:
            raise ConflictError("续期已批准，附件已固定")
        attachment = self.repository.add_attachment(renewal_id, name, content, actor)
        self.repository.append_audit("attachment", RENEWAL_ENTITY, renewal_id,
                                     actor, {
                                         "attachment_id": attachment["id"],
                                         "name": name,
                                     })
        return attachment

    def list_attachments(self, renewal_id: int, role: str) -> list:
        ensure_role(role, ATTACHMENT_VIEW_ROLES)
        self.repository.get_renewal(renewal_id)
        return self.repository.list_attachments(renewal_id)

    def enrich_renewal(self, renewal: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(renewal)
        records = self.repository.records_for_facility(renewal["facility"])
        inspections = [r for r in records if r["kind"] == INSPECTION_KIND]
        rectifications = [r for r in records if r["kind"] == RECTIFICATION_KIND]
        others = [r for r in records
                  if r["kind"] not in (INSPECTION_KIND, RECTIFICATION_KIND)]
        open_rectifications = [r for r in rectifications if r["status"] == "open"]
        result["records"] = {
            "inspections": inspections,
            "rectifications": rectifications,
            "others": others,
        }
        result["open_rectifications"] = open_rectifications
        result["blockers"] = (renewal_blockers(open_rectifications)
                              if renewal["status"] != RENEWAL_STATES[-1] else [])
        return result

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

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
        return result
