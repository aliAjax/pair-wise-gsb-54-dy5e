import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


def make_data(cable='SEA-1', segment='S3'):
    return {
        'cable': cable, 'segment': segment,
        'start_km': 120.0, 'end_km': 135.0, 'repair_distance_km': 15.0,
        'required_spare_km': 15.75, 'estimated_repair_hours': 20.0,
        'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True,
        'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400,
    }


APPROVE = {
    'repair_manager': 'RM-2', 'vessel_name': 'CS-LOCK',
    'voyage_start': '2026-10-10T00:00:00Z', 'voyage_end': '2026-10-12T00:00:00Z',
    'vessel_spare_km': 200.0,
}


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)
        self.records = []
        for i in range(6):
            self.records.append(
                self.service.create(Actor("c", "noc_operator"), "LOCK-%d" % i, make_data(segment="S%d" % (20 + i)))
            )

    def tearDown(self):
        self.temp.cleanup()

    def test_only_one_dispatcher_wins_same_vessel(self):
        results = {}
        barrier = threading.Barrier(len(self.records))

        def worker(index, record):
            barrier.wait()
            actor = Actor("dispatcher-%d" % index, "repair_manager")
            try:
                out = self.service.act(actor, record["id"], 1, "approve", dict(APPROVE))
                results[index] = ("ok", out["id"])
            except Conflict as exc:
                results[index] = ("conflict", exc.details.get("held_by", {}).get("reference"))

        threads = [threading.Thread(target=worker, args=(i, r)) for i, r in enumerate(self.records)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        winners = [k for k, value in results.items() if value[0] == "ok"]
        losers = [k for k, value in results.items() if value[0] == "conflict"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), len(self.records) - 1)
        winner_ref = "LOCK-%d" % winners[0]
        for index in losers:
            self.assertEqual(results[index][1], winner_ref)
            record = self.service.get_record(Actor("c", "noc_operator"), self.records[index]["id"])
            self.assertEqual(record["state"], "detected")
            self.assertEqual(record["version"], 1)

        # 后到者保留输入、换船后仍可成功
        loser = losers[0]
        retried = self.service.act(
            Actor("dispatcher-%d" % loser, "repair_manager"),
            self.records[loser]["id"], 1, "approve",
            dict(APPROVE, vessel_name="CS-OTHER"),
        )
        self.assertEqual(retried["state"], "approved")
        self.assertEqual(retried["resource"]["vessel"], "CS-OTHER")


class UpgradeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "old.db")
        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            """
            CREATE TABLE records (
                id INTEGER PRIMARY KEY AUTOINCREMENT, reference TEXT UNIQUE, state TEXT, version INTEGER,
                payload TEXT, created_by TEXT, updated_by TEXT, created_at TEXT, updated_at TEXT
            );
            CREATE TABLE audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, record_id INTEGER, action TEXT, actor_id TEXT,
                version INTEGER, details TEXT, created_at TEXT
            );
            """
        )
        payload = dict(make_data())
        payload["vessel_name"] = "CS-OLD"
        connection.execute(
            "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) "
            "VALUES('OLD-1','mobilized',3,?,'c','v','2026-10-01','2026-10-02')",
            (json.dumps(payload),),
        )
        connection.commit()
        connection.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_old_schema_backfills_allocations(self):
        service = build_service(self.db_path)
        record = service.get_record(Actor("c", "noc_operator"), 1)
        self.assertEqual(record["resource"]["vessel"], "CS-OLD")
        self.assertEqual(record["resource"]["reserved_km"], 15.75)
        self.assertEqual(record["resource"]["status"], "held")

        new = service.create(Actor("c", "noc_operator"), "NEW-1", make_data(cable="SEA-9", segment="S1"))
        with self.assertRaises(Conflict) as ctx:
            service.act(Actor("rm", "repair_manager"), new["id"], 1, "approve", dict(APPROVE, vessel_name="CS-OLD"))
        self.assertEqual(ctx.exception.details["held_by"]["reference"], "OLD-1")

        # 存量记录继续走完流程，回填占用按实耗扣减、恢复归还
        service.act(Actor("ce", "cable_engineer"), 1, 3, "survey",
                    {"survey_complete": True, "fault_location_km": 128})
        service.act(Actor("ce", "cable_engineer"), 1, 4, "splice",
                    {"splice_loss_db": 0.1, "spare_used_km": 15.5})
        service.act(Actor("no", "noc_operator"), 1, 5, "test", {"end_to_end_loss_db": 0.2})
        service.act(Actor("no", "noc_operator"), 1, 6, "restore",
                    {"traffic_restored": True, "restore_capacity_gbps": 400})
        allocation = service.get_record(Actor("c", "noc_operator"), 1)["resource"]
        self.assertEqual(allocation["status"], "released")
        self.assertEqual(allocation["returned_km"], 0.25)

        # 升级幂等：重新打开不会重复回填
        build_service(self.db_path)
        connection = sqlite3.connect(self.db_path)
        count = connection.execute(
            "SELECT COUNT(*) FROM resource_allocations WHERE record_id=1"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
