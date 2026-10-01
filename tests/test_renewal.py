import tempfile
import unittest
from pathlib import Path

from src.domain import (ConflictError, NotFoundError, PermissionDenied,
                        ValidationError)
from src.repository import Repository
from src.service import Service

EQUIPMENT = [
    {"name": "scrubber-1", "pollutants": ["so2", "pm"]},
    {"name": "bag-filter", "pollutants": ["pm"]},
]
LIMITS = {"so2": 50, "pm": 30}


class RenewalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.permit = self.service.create_item(
            {"title": "permit A", "description": "old permit", "severity": "high",
             "quantity": 8, "threshold": 10, "external_ref": "PERMIT-1",
             "facility": "boiler-1", "equipment": EQUIPMENT},
            "creator", "applicant")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _permit(self, external_ref, facility="boiler-1"):
        return self.service.create_item(
            {"title": "permit " + external_ref, "description": "permit",
             "severity": "medium", "quantity": 3, "threshold": 10,
             "external_ref": external_ref, "facility": facility},
            "creator", "applicant")

    def _draft(self, request_id="REN-1", **overrides):
        payload = {"request_id": request_id, "permit_id": self.permit["id"],
                   "emission_limits": dict(LIMITS)}
        payload.update(overrides)
        return self.service.create_renewal(payload, "applicant-1", "applicant")

    def _submit(self, renewal):
        return self.service.transition_renewal(
            renewal["id"], "submitted", renewal["version"],
            "applicant-1", "applicant")

    def _approve(self, renewal):
        return self.service.transition_renewal(
            renewal["id"], "approved", renewal["version"],
            "manager-1", "compliance_manager")

    def test_draft_carries_permit_version_and_equipment(self):
        renewal = self._draft()
        self.assertEqual(renewal["status"], "draft")
        self.assertEqual(renewal["permit_id"], self.permit["id"])
        self.assertEqual(renewal["permit_version"], self.permit["version"])
        self.assertEqual(renewal["equipment"], EQUIPMENT)
        self.assertEqual(renewal["emission_limits"], LIMITS)
        self.assertEqual(renewal["facility"], "boiler-1")
        self.assertFalse(renewal["replayed"])

    def test_limits_must_match_equipment_coverage(self):
        with self.assertRaises(ValidationError):
            self._draft(emission_limits={"nox": 40})
        with self.assertRaises(ValidationError):
            self._draft(emission_limits={})
        with self.assertRaises(ValidationError):
            self._draft(facility="other-facility")

    def test_duplicate_request_id_keeps_single_renewal(self):
        first = self._draft()
        second = self._draft()
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["replayed"])
        self.assertEqual(len(self.service.list_renewals("viewer")), 1)
        recovered = self.service.get_renewal_by_request("REN-1", "viewer")
        self.assertEqual(recovered["id"], first["id"])
        with self.assertRaises(NotFoundError):
            self.service.get_renewal_by_request("REN-MISSING", "viewer")

    def test_facility_groups_records_and_closed_rectification_passes(self):
        other = self._permit("PERMIT-2")
        self.service.add_record(
            other["id"], {"kind": "inspection", "detail": "三月现场检查",
                          "status": "closed", "external_ref": "INSP-1"},
            "inspector-1", "inspector")
        self.service.add_record(
            self.permit["id"], {"kind": "rectification", "detail": "更换滤袋",
                                "status": "closed", "external_ref": "RECT-1"},
            "inspector-1", "inspector")
        renewal = self._draft()
        grouped = renewal["records"]
        self.assertEqual(len(grouped["inspections"]), 1)
        self.assertEqual(len(grouped["rectifications"]), 1)
        self.assertEqual(renewal["blockers"], [])
        approved = self._approve(self._submit(renewal))
        self.assertEqual(approved["status"], "approved")
        snapshot = approved["snapshot"]
        self.assertEqual(snapshot["old_permit"]["id"], self.permit["id"])
        self.assertEqual(snapshot["old_permit"]["version"], self.permit["version"])
        self.assertEqual(snapshot["new_permit"]["emission_limits"], LIMITS)
        self.assertEqual(snapshot["new_permit"]["equipment"], EQUIPMENT)
        self.assertEqual(snapshot["progress"]["closed"], 2)
        self.assertEqual(snapshot["progress"]["open"], 0)
        self.assertEqual(len(snapshot["records"]), 2)

    def test_open_rectification_blocks_with_evidence_then_close_unblocks(self):
        record = self.service.add_record(
            self.permit["id"], {"kind": "rectification", "detail": "在线监测未校准",
                                "status": "open", "external_ref": "RECT-OPEN"},
            "inspector-1", "inspector")
        renewal = self._submit(self._draft())
        self.assertEqual(len(renewal["open_rectifications"]), 1)
        with self.assertRaises(ConflictError) as ctx:
            self._approve(renewal)
        message = str(ctx.exception)
        self.assertIn("RECT-OPEN", message)
        self.assertIn("在线监测未校准", message)
        self.service.close_record(self.permit["id"], record["id"],
                                  "inspector-1", "inspector")
        approved = self._approve(renewal)
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["snapshot"]["progress"]["open"], 0)

    def test_snapshot_frozen_after_approval(self):
        renewal = self._approve(self._submit(self._draft()))
        self.service.add_record(
            self.permit["id"], {"kind": "inspection", "detail": "批准后补录",
                                "status": "open", "external_ref": "INSP-LATE"},
            "inspector-1", "inspector")
        again = self.service.get_renewal(renewal["id"], "viewer")
        self.assertEqual(again["snapshot"]["progress"]["total"], 0)
        self.assertEqual(again["snapshot"]["records"], [])
        with self.assertRaises(ConflictError):
            self._approve(again)
        with self.assertRaises(ConflictError):
            self.service.upload_attachment(
                renewal["id"], {"name": "late.txt", "content": "x"},
                "applicant-1", "applicant")

    def test_stale_permit_version_blocks_approval(self):
        renewal = self._submit(self._draft())
        self.service.transition(self.permit["id"], "submitted",
                                self.permit["version"], "applicant-1", "applicant")
        with self.assertRaises(ConflictError) as ctx:
            self._approve(renewal)
        self.assertIn("旧许可版本已变更", str(ctx.exception))

    def test_attachment_acl_rejects_inspector(self):
        renewal = self._draft()
        attachment = self.service.upload_attachment(
            renewal["id"], {"name": "监测报告.pdf", "content": "base64-data"},
            "applicant-1", "applicant")
        self.assertEqual(attachment["renewal_id"], renewal["id"])
        with self.assertRaises(PermissionDenied):
            self.service.list_attachments(renewal["id"], "inspector")
        with self.assertRaises(PermissionDenied):
            self.service.list_attachments(renewal["id"], "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.upload_attachment(
                renewal["id"], {"name": "x", "content": "y"},
                "inspector-1", "inspector")
        self.assertEqual(
            len(self.service.list_attachments(renewal["id"], "compliance_manager")), 1)
        self.assertEqual(
            len(self.service.list_attachments(renewal["id"], "applicant")), 1)

    def test_backfill_duplicate_record_rejected_within_facility(self):
        other = self._permit("PERMIT-3")
        self.service.add_record(
            self.permit["id"], {"kind": "rectification", "detail": "已关闭整改",
                                "status": "closed", "external_ref": "RECT-OLD"},
            "inspector-1", "inspector")
        with self.assertRaises(ConflictError):
            self.service.add_record(
                other["id"], {"kind": "rectification", "detail": "跨月补录同一条",
                              "status": "open", "external_ref": "RECT-OLD"},
                "inspector-1", "inspector")
        foreign = self._permit("PERMIT-4", facility="kiln-2")
        record = self.service.add_record(
            foreign["id"], {"kind": "rectification", "detail": "其他设施不受限",
                            "status": "open", "external_ref": "RECT-OLD"},
            "inspector-1", "inspector")
        self.assertEqual(record["status"], "open")

    def test_renewal_version_conflict_and_roles(self):
        renewal = self._draft()
        with self.assertRaises(PermissionDenied):
            self.service.transition_renewal(
                renewal["id"], "submitted", renewal["version"],
                "inspector-1", "inspector")
        with self.assertRaises(ConflictError):
            self.service.transition_renewal(
                renewal["id"], "submitted", 99, "applicant-1", "applicant")
        submitted = self._submit(renewal)
        with self.assertRaises(PermissionDenied):
            self.service.transition_renewal(
                submitted["id"], "approved", submitted["version"],
                "applicant-1", "applicant")


if __name__ == "__main__":
    unittest.main()
