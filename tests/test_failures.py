import sqlite3
import tempfile
import unittest
from pathlib import Path

import src.repository as repository_module
from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, WriteUnavailable


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
FLOW = [('approve', 'repair_manager', {'repair_manager': 'RM-2'}, 'approved'), ('mobilize', 'vessel_master', {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1'}, 'mobilized'), ('survey', 'cable_engineer', {'survey_complete': True, 'fault_location_km': 128}, 'surveyed'), ('splice', 'cable_engineer', {'splice_loss_db': 0.12, 'spare_used_km': 16}, 'spliced'), ('test', 'noc_operator', {'end_to_end_loss_db': 0.3}, 'tested'), ('restore', 'noc_operator', {'traffic_restored': True, 'restore_capacity_gbps': 400}, 'restored')]


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_permission_and_duplicate(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(Actor("outsider", "outsider"), "CABLE-30001", CREATE_DATA)
        self.service.create(Actor("creator", "noc_operator"), "CABLE-30001", CREATE_DATA)
        with self.assertRaises(Conflict):
            self.service.create(Actor("creator", "noc_operator"), "CABLE-30001", CREATE_DATA)

    def test_stale_version_is_rejected(self):
        record = self.service.create(Actor("creator", "noc_operator"), "CABLE-30001", CREATE_DATA)
        first = FLOW[0]
        record = self.service.act(Actor("operator", first[1]), record["id"], record["version"], first[0], first[2])
        second = FLOW[1]
        with self.assertRaises(Conflict):
            self.service.act(Actor("operator", second[1]), record["id"], record["version"] - 1, second[0], second[2])

    def test_write_lock_failure_is_retried(self):
        record = self.service.create(Actor("creator", "noc_operator"), "CABLE-40001", CREATE_DATA)
        original_connect = repository_module.sqlite3.connect
        state = {"attempts": 0}

        class FlakyConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if isinstance(sql, str) and sql.lstrip().upper().startswith("BEGIN IMMEDIATE"):
                    state["attempts"] += 1
                    if state["attempts"] < 3:
                        raise sqlite3.OperationalError("database is locked")
                return super().execute(sql, *args, **kwargs)

        def flaky_connect(*args, **kwargs):
            kwargs["factory"] = FlakyConnection
            return original_connect(*args, **kwargs)

        repository_module.sqlite3.connect = flaky_connect
        try:
            approved = self.service.act(
                Actor("operator", FLOW[0][1]), record["id"], record["version"], FLOW[0][0], FLOW[0][2]
            )
        finally:
            repository_module.sqlite3.connect = original_connect
        self.assertEqual(approved["state"], "approved")
        self.assertEqual(state["attempts"], 3)

    def test_persistent_lock_failure_returns_write_busy(self):
        original_connect = repository_module.sqlite3.connect

        class LockedConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if isinstance(sql, str) and sql.lstrip().upper().startswith("BEGIN IMMEDIATE"):
                    raise sqlite3.OperationalError("database is locked")
                return super().execute(sql, *args, **kwargs)

        def locked_connect(*args, **kwargs):
            kwargs["factory"] = LockedConnection
            return original_connect(*args, **kwargs)

        repository_module.sqlite3.connect = locked_connect
        try:
            with self.assertRaises(WriteUnavailable):
                self.service.create(Actor("creator", "noc_operator"), "CABLE-40002", CREATE_DATA)
        finally:
            repository_module.sqlite3.connect = original_connect
