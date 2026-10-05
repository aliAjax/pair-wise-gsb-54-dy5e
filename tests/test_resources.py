import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError


def make_data(cable='SEA-1', segment='S3', **overrides):
    data = {
        'cable': cable, 'segment': segment,
        'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3,
        'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True,
        'capacity_gbps': 400, 'permit_expires_at': '2027-01-01T00:00:00Z',
    }
    data.update(overrides)
    return data


APPROVE_CS1 = {
    'repair_manager': 'RM-2', 'vessel_name': 'CS-1',
    'voyage_start': '2026-10-10T00:00:00Z', 'voyage_end': '2026-10-12T00:00:00Z',
    'vessel_spare_km': 40.0,
}


class ResourceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.manager = Actor("rm", "repair_manager")

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, reference, **overrides):
        return self.service.create(Actor("creator", "noc_operator"), reference, make_data(**overrides))

    def _approve(self, record, payload=None, version=None):
        return self.service.act(
            self.manager, record["id"], version if version is not None else record["version"],
            "approve", payload if payload is not None else dict(APPROVE_CS1),
        )

    def test_same_vessel_same_period_second_approval_is_stopped(self):
        first = self._create("RES-1")
        second = self._create("RES-2", cable='SEA-2', segment='S9')
        approved = self._approve(first)
        self.assertEqual(approved["resource"]["vessel"], "CS-1")
        self.assertEqual(approved["resource"]["reserved_km"], 15.75)
        with self.assertRaises(Conflict) as ctx:
            self._approve(second)
        self.assertIn("RES-1", str(ctx.exception))
        details = ctx.exception.details
        self.assertEqual(details["resource"], "vessel")
        self.assertEqual(details["held_by"]["reference"], "RES-1")
        # 冲突停在原记录：状态、版本均未变，输入可修改后重试
        untouched = self.service.get_record(Actor("creator", "noc_operator"), second["id"])
        self.assertEqual(untouched["state"], "detected")
        self.assertEqual(untouched["version"], 1)

    def test_spare_capacity_check(self):
        first = self._create("RES-10")
        second = self._create("RES-11", cable='SEA-2', segment='S9')
        tight = dict(APPROVE_CS1, vessel_spare_km=20.0)
        self._approve(first, tight)
        with self.assertRaises(Conflict) as ctx:
            self._approve(second, dict(tight, voyage_start='2026-10-15', voyage_end='2026-10-16'))
        self.assertEqual(ctx.exception.details["resource"], "spare")
        self.assertEqual(round(ctx.exception.details["remaining_km"], 2), 4.25)
        # 备缆池放大后，非重叠航行可通过
        approved = self._approve(second, dict(APPROVE_CS1, voyage_start='2026-10-15', voyage_end='2026-10-16'))
        self.assertEqual(approved["state"], "approved")

    def test_failed_reassign_keeps_original_hold(self):
        first = self._create("RES-20")
        second = self._create("RES-21", cable='SEA-2', segment='S9')
        self._approve(first)
        second = self._approve(second, dict(APPROVE_CS1, vessel_name='CS-2', voyage_start='2026-11-01', voyage_end='2026-11-02'))
        with self.assertRaises(Conflict):
            self.service.act(self.manager, second["id"], second["version"], "reassign", {
                'vessel_name': 'CS-1',
                'voyage_start': '2026-10-11T00:00:00Z', 'voyage_end': '2026-10-12T00:00:00Z',
                'vessel_spare_km': 40.0,
            })
        untouched = self.service.get_record(Actor("creator", "noc_operator"), second["id"])
        self.assertEqual(untouched["version"], 2)
        self.assertEqual(untouched["resource"]["vessel"], "CS-2")

    def test_reassign_releases_old_hold(self):
        first = self._create("RES-30")
        second = self._create("RES-31", cable='SEA-2', segment='S9')
        self._approve(first, dict(APPROVE_CS1, vessel_name='CS-2'))
        second = self._approve(second, dict(APPROVE_CS1))
        # CS-1 被 RES-31 占着，RES-30 改派 CS-1 同期应失败
        with self.assertRaises(Conflict):
            self.service.act(self.manager, first["id"], 2, "reassign", dict(APPROVE_CS1))
        # RES-31 改派走，原占用释放
        self.service.act(self.manager, second["id"], 2, "reassign", {
            'vessel_name': 'CS-3', 'voyage_start': '2026-12-01', 'voyage_end': '2026-12-02',
            'vessel_spare_km': 40.0, 'reassign_reason': 'CS-1到期检修',
        })
        moved = self.service.act(self.manager, first["id"], 2, "reassign", dict(APPROVE_CS1))
        self.assertEqual(moved["state"], "approved")
        self.assertEqual(moved["version"], 3)
        self.assertEqual(moved["resource"]["vessel"], "CS-1")

    def test_splice_deducts_actual_use_and_restore_returns_remainder(self):
        record = self._create("RES-40")
        record = self._approve(record, dict(APPROVE_CS1, vessel_spare_km=30.0))
        record = self.service.act(Actor("vm", "vessel_master"), record["id"], record["version"], "mobilize",
                                  {'weather_window_hours': 40, 'available_spare_km': 30, 'vessel_name': 'CS-1'})
        record = self.service.act(Actor("ce", "cable_engineer"), record["id"], record["version"], "survey",
                                  {'survey_complete': True, 'fault_location_km': 128})
        record = self.service.act(Actor("ce", "cable_engineer"), record["id"], record["version"], "splice",
                                  {'splice_loss_db': 0.12, 'spare_used_km': 15.5})
        allocation = self.service.get_record(Actor("ce", "cable_engineer"), record["id"])["resource"]
        self.assertEqual(allocation["used_km"], 15.5)
        self.assertEqual(allocation["status"], "held")
        record = self.service.act(Actor("no", "noc_operator"), record["id"], record["version"], "test",
                                  {'end_to_end_loss_db': 0.3})
        record = self.service.act(Actor("no", "noc_operator"), record["id"], record["version"], "restore",
                                  {'traffic_restored': True, 'restore_capacity_gbps': 400})
        allocation = record["resource"]
        self.assertEqual(allocation["status"], "released")
        self.assertEqual(allocation["returned_km"], 0.25)
        # 恢复后船空出来：新抢修同船同期可通过
        other = self._create("RES-41", cable='SEA-3', segment='S1')
        approved = self._approve(other, dict(APPROVE_CS1, vessel_spare_km=20.0))
        self.assertEqual(approved["state"], "approved")

    def test_cancel_releases_full_hold(self):
        record = self._create("RES-50")
        record = self._approve(record)
        cancelled = self.service.act(self.manager, record["id"], record["version"], "cancel",
                                     {'cancel_reason': '海况恶化'})
        allocation = cancelled["resource"]
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(allocation["status"], "released")
        self.assertEqual(allocation["returned_km"], 15.75)

    def test_expired_permit_stops_approval(self):
        record = self._create("RES-60", permit_expires_at='2020-01-01T00:00:00Z')
        with self.assertRaises(ValidationError) as ctx:
            self._approve(record)
        self.assertIn("许可", str(ctx.exception))
        untouched = self.service.get_record(Actor("creator", "noc_operator"), record["id"])
        self.assertEqual(untouched["state"], "detected")

    def test_reassign_requires_valid_permit(self):
        record = self._create("RES-70")
        record = self._approve(record)
        with self.assertRaises(ValidationError):
            self.service.act(self.manager, record["id"], record["version"], "reassign", {
                'vessel_name': 'CS-9', 'permit_expires_at': '2020-01-01T00:00:00Z',
            })


if __name__ == "__main__":
    unittest.main()
