import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service


EQUIPMENT = ["催化燃烧装置", "布袋除尘器", "低氮燃烧器"]


class RenewalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _make_permit(self, facility_id="FAC-1", title="排污许可证", actor="owner",
                     equipment=None, severity="high", quantity=12.0, threshold=6.0):
        return self.service.create_item({
            "title": title, "description": "大气污染物排放", "severity": severity,
            "quantity": quantity, "threshold": threshold,
            "facility_id": facility_id,
            "equipment_list": equipment if equipment is not None else EQUIPMENT,
        }, actor, "applicant")

    def _renew(self, permit, request_id="REQ-1", equipment=None, actor="owner"):
        return self.service.create_renewal({
            "old_item_id": permit["id"], "title": "排污许可证（续期）",
            "quantity": permit["quantity"], "threshold": permit["threshold"],
            "equipment_list": equipment if equipment is not None else EQUIPMENT,
        }, actor, "applicant", request_id=request_id)

    def test_draft_carries_old_version_and_equipment(self):
        permit = self._make_permit()
        renewal = self._renew(permit)
        self.assertEqual(renewal["status"], "draft")
        self.assertEqual(renewal["old_item_id"], permit["id"])
        self.assertEqual(renewal["old_version"], permit["version"])
        self.assertEqual(renewal["equipment_list"], EQUIPMENT)
        self.assertEqual(renewal["facility_id"], "FAC-1")

    def test_gathers_records_by_facility_closed_does_not_block(self):
        permit = self._make_permit()
        other = self._make_permit(facility_id="FAC-2", title="另一设施许可证")
        self.service.add_record(permit["id"], {
            "kind": "rectification", "detail": "整改已完成", "status": "closed",
        }, "owner", "applicant")
        self.service.add_record(other["id"], {
            "kind": "rectification", "detail": "FAC-2的整改", "status": "closed",
        }, "owner", "applicant")
        renewal = self._renew(permit)
        # 只归拢同一设施的记录
        self.assertEqual(len(renewal["records_snapshot"]), 1)
        self.assertEqual(renewal["records_snapshot"][0]["detail"], "整改已完成")
        # 已关闭整改不拦审批
        self.assertFalse(renewal["blocked"])
        self.assertEqual(renewal["open_count"], 0)
        self.assertEqual(renewal["blockers"], [])
        approved = self.service.approve_renewal(renewal["id"], "manager", "compliance_manager")
        self.assertEqual(approved["status"], "approved")
        self.assertIsNotNone(approved["new_item_id"])

    def test_open_rectification_keeps_evidence_and_blocks_approval(self):
        permit = self._make_permit()
        rec = self.service.add_record(permit["id"], {
            "kind": "rectification", "detail": "VOCs治理设施未正常运行", "status": "open",
        }, "inspector", "inspector")
        renewal = self._renew(permit)
        # 仍开启的整改保留证据
        self.assertEqual(len(renewal["records_snapshot"]), 1)
        self.assertEqual(renewal["records_snapshot"][0]["id"], rec["id"])
        self.assertEqual(renewal["records_snapshot"][0]["status"], "open")
        # 挡住批准
        self.assertTrue(renewal["blocked"])
        self.assertEqual(renewal["open_count"], 1)
        with self.assertRaises(ConflictError):
            self.service.approve_renewal(renewal["id"], "manager", "compliance_manager")
        # 整改关闭后不再拦审批
        self.service.close_record(permit["id"], rec["id"], "inspector", "inspector")
        approved = self.service.approve_renewal(renewal["id"], "manager", "compliance_manager")
        self.assertEqual(approved["status"], "approved")

    def test_network_failure_recovery_by_request_id_keeps_one_renewal(self):
        permit = self._make_permit()
        first = self._renew(permit, request_id="REQ-NET-1")
        # 模拟断网后按原请求编号重试
        retry = self._renew(permit, request_id="REQ-NET-1")
        self.assertEqual(first["id"], retry["id"])
        self.assertEqual(first["request_id"], retry["request_id"])
        renewals = self.service.list_renewals("viewer")
        self.assertEqual(len(renewals), 1)
        # 不同请求编号才会创建新的续期单
        second = self._renew(permit, request_id="REQ-NET-2")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(self.service.list_renewals("viewer")), 2)

    def test_duplicate_submit_without_request_id_still_one_per_old_permit(self):
        permit = self._make_permit()
        first = self.service.create_renewal({
            "old_item_id": permit["id"], "title": "续期草案",
            "equipment_list": EQUIPMENT,
        }, "owner", "applicant", request_id="REQ-DUP")
        retry = self.service.create_renewal({
            "old_item_id": permit["id"], "title": "续期草案",
            "equipment_list": EQUIPMENT,
        }, "owner", "applicant", request_id="REQ-DUP")
        self.assertEqual(first["id"], retry["id"])
        self.assertEqual(len(self.service.list_renewals("viewer")), 1)

    def test_inspector_unauthorized_attachment_rejected(self):
        permit_a = self._make_permit(facility_id="FAC-A")
        permit_b = self._make_permit(facility_id="FAC-B", title="B设施许可证")
        self.service.upload_attachment(permit_a["id"], {
            "filename": "检测报告.txt", "data": "FAC-A 检测数据",
        }, "owner", "applicant")
        # inspector 只在 FAC-B 有检查记录
        self.service.add_record(permit_b["id"], {
            "kind": "inspection", "detail": "B设施检查", "status": "closed",
        }, "stranger", "inspector")
        # 无任何记录的 inspector 直接拒绝
        with self.assertRaises(PermissionDenied):
            self.service.list_attachments(permit_a["id"], "nosy", "inspector")
        with self.assertRaises(PermissionDenied):
            self.service.view_attachment(permit_a["id"], 1, "nosy", "inspector")
        # 越权（非本设施）的 inspector 直接拒绝
        with self.assertRaises(PermissionDenied):
            self.service.list_attachments(permit_a["id"], "stranger", "inspector")
        # 本设施 inspector 可查看
        self.service.add_record(permit_a["id"], {
            "kind": "inspection", "detail": "A设施检查", "status": "closed",
        }, "insp", "inspector")
        att = self.service.view_attachment(permit_a["id"], 1, "insp", "inspector")
        self.assertEqual(att["data"], "FAC-A 检测数据")
        # 申请人和管理员可查看
        self.assertEqual(len(self.service.list_attachments(permit_a["id"], "owner", "applicant")), 1)
        self.assertEqual(len(self.service.list_attachments(permit_a["id"], "manager", "compliance_manager")), 1)

    def test_approval_freezes_old_and_new_permits_and_progress(self):
        permit = self._make_permit()
        rec = self.service.add_record(permit["id"], {
            "kind": "rectification", "detail": "整改完成", "status": "closed",
        }, "owner", "applicant")
        renewal = self._renew(permit)
        approved = self.service.approve_renewal(renewal["id"], "manager", "compliance_manager")
        # 旧许可被固定（替代），新许可建立
        old = self.service.get_item(permit["id"], "viewer")
        self.assertEqual(old["status"], "superseded")
        new = self.service.get_item(approved["new_item_id"], "viewer")
        self.assertEqual(new["facility_id"], "FAC-1")
        self.assertEqual(new["equipment_list"], EQUIPMENT)
        self.assertEqual(new["version"], approved["new_version"])
        # 检查、整改进度固定为续期时的快照
        self.assertEqual(len(approved["records_snapshot"]), 1)
        self.assertEqual(approved["records_snapshot"][0]["id"], rec["id"])
        # 审批后旧许可不能再补录（跨月补录无法带回）
        with self.assertRaises(ConflictError):
            self.service.add_record(permit["id"], {
                "kind": "rectification", "detail": "跨月补录", "status": "open",
            }, "owner", "applicant")
        # 新许可仍可正常记录
        new_rec = self.service.add_record(new["id"], {
            "kind": "inspection", "detail": "新许可首次检查", "status": "open",
        }, "inspector", "inspector")
        self.assertEqual(new_rec["item_id"], new["id"])
        self.assertTrue(self.repo.verify_audit_chain())

    def test_renewal_requires_equipment_list(self):
        permit = self._make_permit()
        with self.assertRaises(ValidationError):
            self.service.create_renewal({
                "old_item_id": permit["id"], "title": "续期",
            }, "owner", "applicant", request_id="REQ-NO-EQ")

    def test_cannot_renew_superseded_permit(self):
        permit = self._make_permit()
        renewal = self._renew(permit)
        self.service.approve_renewal(renewal["id"], "manager", "compliance_manager")
        with self.assertRaises(ConflictError):
            self.service.create_renewal({
                "old_item_id": permit["id"], "title": "再次续期",
                "equipment_list": EQUIPMENT,
            }, "owner", "applicant", request_id="REQ-AGAIN")


if __name__ == "__main__":
    unittest.main()
