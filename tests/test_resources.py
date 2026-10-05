import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ResourceConflict, ValidationError
from src.http_api import create_server


DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
WINDOW = {'sailing_from': '2026-10-10T00:00:00Z', 'sailing_to': '2026-10-12T00:00:00Z'}


def make_record(service, ref, segment='S3', start=120.0, end=135.0):
    return service.create(Actor('creator', 'noc_operator'), ref,
                          dict(DATA, segment=segment, start_km=start, end_km=end))


class ResourceConflictTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.service.set_spare_total(Actor('rm', 'repair_manager'), 100.0)

    def tearDown(self):
        self.temp.cleanup()

    def _approve(self, record, vessel='CS-1', window=None, actor_role='repair_manager'):
        data = {'repair_manager': 'RM-2', 'vessel_name': vessel}
        data.update(window or WINDOW)
        return self.service.act(Actor('rm', actor_role), record['id'], record['version'], 'approve', data)

    def test_vessel_double_booking_blocks_second_record(self):
        r1 = make_record(self.service, 'C-1', 'S3')
        r2 = make_record(self.service, 'C-2', 'S4', 140.0, 155.0)
        self._approve(r1)
        with self.assertRaises(ResourceConflict) as ctx:
            self._approve(r2)
        self.assertEqual(ctx.exception.details['resource_type'], 'vessel')
        self.assertEqual(ctx.exception.details['occupied_by'][0]['reference'], 'C-1')
        # 冲突记录停在原状态，版本不变，输入由调用方保留
        blocked = self.service.get_record(Actor('c', 'noc_operator'), r2['id'])
        self.assertEqual(blocked['state'], 'detected')
        self.assertEqual(blocked['version'], 1)
        # 审计写明被哪项抢修占住
        timeline = self.service.timeline(Actor('c', 'noc_operator'), r2['id'])
        self.assertEqual(timeline[-1]['action'], 'blocked')
        self.assertIn('C-1', timeline[-1]['details']['reason'])

    def test_non_overlapping_windows_share_vessel(self):
        r1 = make_record(self.service, 'C-1', 'S3')
        r2 = make_record(self.service, 'C-2', 'S4', 140.0, 155.0)
        self._approve(r1)
        self._approve(r2, window={'sailing_from': '2026-10-12T00:00:00Z', 'sailing_to': '2026-10-14T00:00:00Z'})

    def test_reassign_releases_old_vessel(self):
        r1 = make_record(self.service, 'C-1', 'S3')
        r2 = make_record(self.service, 'C-2', 'S4', 140.0, 155.0)
        approved = self._approve(r1)
        # C-2抢不到CS-1
        with self.assertRaises(ResourceConflict):
            self._approve(r2)
        # C-1改派CS-2，释放CS-1
        self.service.act(Actor('rm', 'repair_manager'), approved['id'], approved['version'], 'reassign',
                         {'vessel_name': 'CS-2', **WINDOW, 'reason': 'CS-1检修'})
        # C-2保留原输入重提，通过（记录版本未被冲突推进）
        r2_fresh = self.service.get_record(Actor('c', 'noc_operator'), r2['id'])
        self._approve(r2_fresh, 'CS-1')
        snap = self.service.resources_snapshot(Actor('c', 'noc_operator'))
        self.assertEqual(snap['vessels']['CS-1']['active_jobs'], 1)
        self.assertEqual(snap['vessels']['CS-2']['active_jobs'], 1)

    def test_failed_reassign_keeps_original_occupation(self):
        r1 = make_record(self.service, 'C-1', 'S3')
        r2 = make_record(self.service, 'C-2', 'S4', 140.0, 155.0)
        a1 = self._approve(r1, 'CS-1')
        a2 = self._approve(r2, 'CS-2')
        with self.assertRaises(ResourceConflict):
            self.service.act(Actor('rm', 'repair_manager'), a1['id'], a1['version'], 'reassign',
                             {'vessel_name': 'CS-2', **WINDOW})
        record = self.service.get_record(Actor('c', 'noc_operator'), r1['id'])
        self.assertEqual(record['payload']['vessel_name'], 'CS-1')
        self.assertEqual(record['version'], a1['version'])

    def test_cancel_releases_vessel_and_spare(self):
        r1 = make_record(self.service, 'C-1', 'S3')
        a1 = self._approve(r1)
        self.service.act(Actor('rm', 'repair_manager'), a1['id'], a1['version'], 'cancel', {'cancel_reason': '天气转差'})
        r2 = make_record(self.service, 'C-2', 'S4', 140.0, 155.0)
        self._approve(r2)  # CS-1窗口已释放，可占用
        spare = self.service.resources_snapshot(Actor('c', 'noc_operator'))['spare']
        self.assertAlmostEqual(spare['available_km'], 100.0 - 15.75, places=2)

    def test_spare_reserve_consume_and_return(self):
        r = make_record(self.service, 'C-1', 'S3')
        approved = self._approve(r)
        snap = self.service.resources_snapshot(Actor('c', 'noc_operator'))
        self.assertAlmostEqual(snap['spare']['held_km'], 15.75, places=2)
        mobilized = self.service.act(Actor('vm', 'vessel_master'), approved['id'], approved['version'], 'mobilize',
                                     {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1', **WINDOW})
        surveyed = self.service.act(Actor('ce', 'cable_engineer'), mobilized['id'], mobilized['version'], 'survey',
                                    {'survey_complete': True, 'fault_location_km': 128})
        spliced = self.service.act(Actor('ce', 'cable_engineer'), surveyed['id'], surveyed['version'], 'splice',
                                   {'splice_loss_db': 0.12, 'spare_used_km': 16})
        snap = self.service.resources_snapshot(Actor('c', 'noc_operator'))
        self.assertAlmostEqual(snap['spare']['committed_km'], 16.0, places=2)
        self.assertAlmostEqual(snap['spare']['available_km'], 84.0, places=2)
        tested = self.service.act(Actor('no', 'noc_operator'), spliced['id'], spliced['version'], 'test',
                                  {'end_to_end_loss_db': 0.3})
        restored = self.service.act(Actor('no', 'noc_operator'), tested['id'], tested['version'], 'restore',
                                    {'traffic_restored': True, 'restore_capacity_gbps': 400, 'spare_returned_km': 4})
        self.assertEqual(restored['state'], 'restored')
        snap = self.service.resources_snapshot(Actor('c', 'noc_operator'))
        self.assertAlmostEqual(snap['spare']['available_km'], 100.0, places=2)
        # 船也已释放
        self.assertNotIn('CS-1', snap['vessels'])

    def test_spare_overuse_blocks_splice(self):
        # 存量够审批预留(15.75)，但接续申报实际用量16时超出
        self.service.set_spare_total(Actor('rm', 'repair_manager'), 15.75)
        r1 = make_record(self.service, 'C-1', 'S3')
        approved = self._approve(r1)
        mobilized = self.service.act(Actor('vm', 'vessel_master'), approved['id'], approved['version'], 'mobilize',
                                     {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1', **WINDOW})
        surveyed = self.service.act(Actor('ce', 'cable_engineer'), mobilized['id'], mobilized['version'], 'survey',
                                    {'survey_complete': True, 'fault_location_km': 128})
        with self.assertRaises(ResourceConflict) as ctx:
            self.service.act(Actor('ce', 'cable_engineer'), surveyed['id'], surveyed['version'], 'splice',
                             {'splice_loss_db': 0.12, 'spare_used_km': 16})
        self.assertEqual(ctx.exception.details['resource_type'], 'spare')
        stuck = self.service.get_record(Actor('c', 'noc_operator'), r1['id'])
        self.assertEqual(stuck['state'], 'surveyed')

    def test_expired_permit_blocks_approve(self):
        r = make_record(self.service, 'C-1', 'S3')
        with self.assertRaises(ValidationError) as ctx:
            self.service.act(Actor('rm', 'repair_manager'), r['id'], r['version'], 'approve',
                             {'repair_manager': 'RM', 'vessel_name': 'CS-1', 'permit_expiry': '2020-01-01', **WINDOW})
        self.assertIn('许可', str(ctx.exception))

    def test_concurrent_same_vessel_only_one_wins(self):
        r1 = make_record(self.service, 'C-1', 'S3')
        r2 = make_record(self.service, 'C-2', 'S4', 140.0, 155.0)
        results = {}
        barrier = threading.Barrier(2)

        def submit(key, record):
            barrier.wait()
            try:
                self._approve(record)
                results[key] = 'approved'
            except ResourceConflict:
                results[key] = 'conflict'
            except Exception as exc:  # pragma: no cover
                results[key] = 'error:%s' % exc

        t1 = threading.Thread(target=submit, args=('a', r1))
        t2 = threading.Thread(target=submit, args=('b', r2))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(results.values()), ['approved', 'conflict'])
        snap = self.service.resources_snapshot(Actor('c', 'noc_operator'))
        self.assertEqual(snap['vessels']['CS-1']['active_jobs'], 1)

    def test_write_retried_while_database_locked(self):
        approved_id = self._approve(make_record(self.service, 'C-0', 'S0', 10.0, 25.0))['id']
        # 用短busy_timeout的连接触发快速locked错误，验证commit_action自动重试后成功
        original_connect = self.service.repository._connect

        def fast_connect():
            conn = original_connect()
            conn.execute("PRAGMA busy_timeout = 30")
            return conn

        holder = {}

        def hold_lock_then_release():
            import sqlite3 as _sqlite3
            import time
            blocker = _sqlite3.connect(self.service.repository.db_path)
            blocker.execute("BEGIN IMMEDIATE")
            holder['ready'].set()
            time.sleep(0.3)
            blocker.rollback(); blocker.close()

        holder['ready'] = threading.Event()
        locker = threading.Thread(target=hold_lock_then_release)
        locker.start()
        holder['ready'].wait(2)

        self.service.repository._connect = fast_connect
        try:
            record = self.service.get_record(Actor('vm', 'vessel_master'), approved_id)
            mobilized = self.service.act(Actor('vm', 'vessel_master'), record['id'], record['version'], 'mobilize',
                                         {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1', **WINDOW})
        finally:
            self.service.repository._connect = original_connect
            locker.join(timeout=2)
        self.assertEqual(mobilized['state'], 'mobilized')


class MigrationTest(unittest.TestCase):
    def _legacy_db(self, path):
        """模拟旧版本：只有records/audit_events两张表，记录已走到mobilized。"""
        payload = dict(DATA)
        payload.update({'repair_distance_km': 15.0, 'required_spare_km': 15.75,
                        'estimated_repair_hours': 43.5, 'repair_feasible': True,
                        'repair_manager': 'RM', 'vessel_name': 'CS-1', 'weather_window_hours': 40})
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE records (id INTEGER PRIMARY KEY AUTOINCREMENT, reference TEXT NOT NULL UNIQUE,"
            " state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, payload TEXT NOT NULL,"
            " created_by TEXT NOT NULL, updated_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, record_id INTEGER NOT NULL,"
            " action TEXT NOT NULL, actor_id TEXT NOT NULL, version INTEGER NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL)")
        conn.execute("INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at)"
                     " VALUES(?,?,?,?,?,?,?,?)",
                     ('LEGACY-1', 'mobilized', 3, json.dumps(payload), 'c', 'vm',
                      '2026-09-01T00:00:00+00:00', '2026-09-02T00:00:00+00:00'))
        conn.commit(); conn.close()

    def test_old_database_is_upgraded_and_backfilled(self):
        temp = tempfile.TemporaryDirectory()
        path = str(Path(temp.name) / 'old.db')
        self._legacy_db(path)
        service = build_service(path)
        snap = service.resources_snapshot(Actor('c', 'noc_operator'))
        by_type = {}
        for occ in snap['occupations']:
            by_type.setdefault(occ['resource_type'], []).append(occ)
        self.assertEqual(by_type['vessel'][0]['resource_name'], 'CS-1')
        self.assertAlmostEqual(by_type['spare'][0]['reserved_km'], 15.75, places=2)
        # 升级后流程可继续：勘察、接续按实际用量扣减
        record = service.get_record(Actor('c', 'noc_operator'), 1)
        surveyed = service.act(Actor('ce', 'cable_engineer'), 1, record['version'], 'survey',
                               {'survey_complete': True, 'fault_location_km': 128})
        spliced = service.act(Actor('ce', 'cable_engineer'), surveyed['id'], surveyed['version'], 'splice',
                              {'splice_loss_db': 0.1, 'spare_used_km': 16})
        self.assertEqual(spliced['state'], 'spliced')
        # 存量默认取申报最大值20
        self.assertAlmostEqual(snap['spare']['total_km'], 20.0, places=2)
        temp.cleanup()


class HttpConflictTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db = str(Path(self.temp.name) / "http.db")
        service = build_service(db)
        service.set_spare_total(Actor('rm', 'repair_manager'), 100.0)
        self.server = create_server('127.0.0.1', 0, service, Path('/workspace/static'))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.service = service

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _request(self, method, path, body=None, role='repair_manager'):
        conn = HTTPConnection('127.0.0.1', self.port, timeout=5)
        headers = {'X-User-Id': 'rm', 'X-Role': role, 'Content-Type': 'application/json'}
        raw = json.dumps(body or {})
        conn.request(method, path, raw, headers)
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode('utf-8'))
        conn.close()
        return resp.status, payload

    def test_http_conflict_response_names_blocking_record(self):
        _, r1 = self._request('POST', '/api/records', {'reference': 'H-1', 'data': dict(DATA, segment='S9', start_km=500, end_km=515)}, role='noc_operator')
        _, r2 = self._request('POST', '/api/records', {'reference': 'H-2', 'data': dict(DATA, segment='S8', start_km=600, end_km=615)}, role='noc_operator')
        status, _ = self._request('POST', '/api/records/%s/actions/approve' % r1['id'],
                                  {'expected_version': 1, 'data': dict({'repair_manager': 'RM', 'vessel_name': 'CS-1'}, **WINDOW)})
        self.assertEqual(status, 200)
        # 后到者：同一艘船同时段
        status, payload = self._request('POST', '/api/records/%s/actions/approve' % r2['id'],
                                        {'expected_version': 1, 'data': dict({'repair_manager': 'RM', 'vessel_name': 'CS-1'}, **WINDOW)})
        self.assertEqual(status, 409)
        self.assertEqual(payload['error'], 'resource_conflict')
        self.assertEqual(payload['conflict']['occupied_by'][0]['reference'], 'H-1')
        # 资源总览接口
        status, snap = self._request('GET', '/api/resources')
        self.assertEqual(status, 200)
        self.assertIn('spare', snap)

    def test_concurrent_http_requests_only_one_wins(self):
        created = []
        for i in range(2):
            status, rec = self._request('POST', '/api/records',
                                        {'reference': 'P-%s' % i, 'data': dict(DATA, segment='SX%s' % i, start_km=700 + i * 20, end_km=715 + i * 20)},
                                        role='noc_operator')
            self.assertEqual(status, 201)
            created.append(rec)
        outcomes = {}
        barrier = threading.Barrier(2)

        def fire(key, rec):
            barrier.wait()
            status, _ = self._request('POST', '/api/records/%s/actions/approve' % rec['id'],
                                      {'expected_version': 1, 'data': dict({'repair_manager': 'RM', 'vessel_name': 'CS-7'}, **WINDOW)})
            outcomes[key] = status

        threads = [threading.Thread(target=fire, args=(k, r)) for k, r in enumerate(created)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sorted(outcomes.values()), [200, 409])


if __name__ == '__main__':
    unittest.main()
