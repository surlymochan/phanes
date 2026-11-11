"""The read-only lease audit of the fleet capacity coordinator (Refs #2157, #1795).

The coordinator of ``swrm/bin/fleet-capacity`` keeps a lease on every
reservation: the row of the ``reservations`` table carries ``lease_expires``,
the owner renews it and the boot evidence of the owner fences the reclaim.
Nobody looks at those rows from the outside, so three failures stay invisible
(Refs #1795, #2157):

* a *double claim*: the same run (or the same card) holds two reservations at
  the same time, so two workers work one card;
* an *expired, unreclaimed* reservation: the lease ran out and nobody
  released it, so the capacity of the box is lost;
* a *ghost*: the run of a reservation is over (finished, failed, dead,
  budget_exceeded) but the reservation is still there.

This suite is the contract of the audit that has to find them.  The fixture is
built on the *current* coordinator schema of ``main``: the database is created
by the real ``swrm/bin/fleet-capacity`` binary, so the rows of the fixture can
never drift away from the table the coordinator really uses.

What the audit has to do (Refs #2157):

* it opens the coordinator database read-only: the bytes of the database, its
  rows and its schema are the same before and after a round, and the audit may
  only write its own dedupe state file;
* every finding is a structured alert with ``kind``, ``host``, ``run_id``,
  ``dedupe_key``, ``first_seen`` and ``detail``;
* the same ``dedupe_key`` is reported as ``new`` once and as ``ongoing``
  afterwards, so a round does not repeat the same finding;
* the health output carries ``ghost_leases``, ``expired_unreclaimed``,
  ``double_claims`` and ``lease_age_seconds``, plus the counts of the rolling
  24 hour window; the window counts the findings whose ``first_seen`` falls
  inside it and never the historical stock of the database;
* the coordinator schema carries no acquire queue time, so the wait metric is
  ``null`` with a ``reason`` instead of an invented number;
* a reservation whose run record cannot be read is skipped and counted, never
  guessed.

The issue allows either a dedicated ``swrm/bin/fleet-capacity-audit`` or a
read-only ``op: audit`` of ``swrm/bin/fleet-capacity``; the suite accepts both
and the implementation states which one it chose.  The options of the command
are ``--db``, ``--runs``, ``--state``, ``--grace-seconds`` and ``--health``
(the same names as the keys of the ``op: audit`` request).
"""
import datetime
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
CAPACITY = HERE.parent / 'bin' / 'fleet-capacity'
AUDIT = HERE.parent / 'bin' / 'fleet-capacity-audit'

# The rolling window of the health numbers (24 hours).
WINDOW_S = 24 * 3600
# The metric names of the health output (Refs #2157).
METRIC_NAMES = ('ghost_leases', 'expired_unreclaimed', 'double_claims')


def iso(epoch):
    """An ISO-8601 UTC stamp of an epoch."""
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat(timespec='seconds')


def digest(path):
    """The sha256 of a file."""
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def _is_json(path):
    """Is the file a JSON document?  An empty lock file is not."""
    try:
        json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    return True


class FleetCapacityAuditTests(unittest.TestCase):
    """The findings the read-only audit has to produce (Refs #2157)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fleet-capacity-audit-')
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.state_home = self.root / 'state'
        self.state_home.mkdir()
        self.runs = self.root / 'runs'
        self.runs.mkdir()
        self.audit_state = self.root / 'audit'
        self.audit_state.mkdir()
        self.state_path = self.audit_state / 'audit-state.json'
        self.coordinator = self.root / 'coordinator'
        self.coordinator.mkdir()
        self.db = self.coordinator / 'capacity.sqlite'
        self.create_database()

    # ------------------------------------------------------------------ setup

    def env(self):
        """A private environment: the audit must never touch the real home."""
        environment = dict(os.environ)
        environment['HOME'] = str(self.home)
        environment['OPENSWARM_HOME'] = str(self.state_home)
        environment['OPENSWARM_FLEET_CAPACITY_DIR'] = str(self.coordinator)
        environment.pop('OPENSWARM_FLEET_GUARD_CONFIG', None)
        return environment

    def create_database(self):
        """Create the coordinator database with the schema of the real binary."""
        process = subprocess.run([sys.executable, str(CAPACITY), 'broker'],
                                 input=json.dumps({'op': 'status'}), text=True,
                                 capture_output=True, timeout=60, env=self.env())
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(self.db.exists(), 'the coordinator did not create its database')

    def add_reservation(self, token, host, run_id, backend='pi', created=None,
                        expires=None, provider=None):
        """One reservation of the coordinator (a lease on a slot)."""
        moment = time.time()
        row = (token, host, run_id, backend, provider,
               moment - 30 if created is None else created,
               moment + 300 if expires is None else expires,
               None)
        with sqlite3.connect(self.db) as db:
            db.execute('INSERT INTO reservations (token,host,run_id,backend,provider,'
                       'created,lease_expires,verified_boot_id) VALUES (?,?,?,?,?,?,?,?)', row)
        return row

    def add_run(self, run_id, issue=None, title=None, status='running', name='result.json'):
        """One run record: the card of a run and whether the run is over."""
        directory = self.runs / run_id
        directory.mkdir(parents=True, exist_ok=True)
        record = {'id': run_id, 'status': status, 'state': status}
        if issue is not None:
            record['issue'] = issue
        if title is not None:
            record['title'] = title
        (directory / name).write_text(json.dumps(record), encoding='utf-8')

    # ------------------------------------------------------------------- run

    def audit(self, db=None, runs=None, state=None, grace=None, health=False):
        """Run the read-only audit and return its report and its process."""
        argv = []
        stdin = None
        if AUDIT.exists():
            argv = [sys.executable, str(AUDIT)]
            if db is not None:
                argv += ['--db', str(db)]
            if runs is not None:
                argv += ['--runs', str(runs)]
            if state is not None:
                argv += ['--state', str(state)]
            if grace is not None:
                argv += ['--grace-seconds', str(grace)]
            if health:
                argv += ['--health']
        else:
            argv = [sys.executable, str(CAPACITY), 'broker']
            request = {'op': 'audit'}
            if db is not None:
                request['db'] = str(db)
            if runs is not None:
                request['runs'] = str(runs)
            if state is not None:
                request['state'] = str(state)
            if grace is not None:
                request['grace_seconds'] = grace
            if health:
                request['health'] = True
            stdin = json.dumps(request)
        process = subprocess.run(argv, input=stdin, text=True, capture_output=True,
                                 timeout=120, env=self.env())
        text = process.stdout.strip()
        report = None
        if text:
            try:
                report = json.loads(text)
            except ValueError:
                report = None
        if not isinstance(report, dict) or not self.is_audit_report(report):
            self.fail('the read-only lease audit produced no report (Refs #2157): '
                      'swrm/bin/fleet-capacity-audit or a read-only "op: audit" of '
                      'swrm/bin/fleet-capacity is missing; command=%s rc=%s stdout=%r stderr=%r'
                      % (argv, process.returncode, text[:400], process.stderr[:400]))
        return report, process

    def is_audit_report(self, report):
        """Is this the report of an audit round?  An error is not a report."""
        if not isinstance(report, dict):
            return False
        for key in ('alerts', 'findings', 'metrics', 'health', 'window_24h', 'window'):
            if key in report:
                return True
        return False

    # -------------------------------------------------------------- accessors

    def alerts(self, report):
        """The findings of the report."""
        value = report.get('alerts')
        if not isinstance(value, list):
            value = report.get('findings')
        if not isinstance(value, list):
            self.fail('the audit report carries no alerts (Refs #2157): %s'
                      % json.dumps(report)[:400])
        return [row for row in value if isinstance(row, dict)]

    def kinds(self, report):
        """The kinds of the findings, sorted."""
        return sorted(str(row.get('kind')) for row in self.alerts(report))

    def live_metrics(self, report):
        """The metrics of the moment (the live snapshot)."""
        containers = [report.get('metrics'), report.get('leases'), report.get('health')]
        for container in containers:
            if isinstance(container, dict):
                if any(name in container for name in METRIC_NAMES):
                    return container
                for value in container.values():
                    if isinstance(value, dict) and any(name in value for name in METRIC_NAMES):
                        return value
        self.fail('the audit report carries no lease metrics (Refs #2157): %s'
                  % json.dumps(report)[:400])

    def window_metrics(self, report):
        """The metrics of the rolling 24 hour window."""
        containers = [report.get('window_24h'), report.get('window')]
        for container in (report.get('metrics'), report.get('health')):
            if isinstance(container, dict):
                containers += [container.get('window_24h'), container.get('window')]
        for container in containers:
            if isinstance(container, dict):
                return container
        self.fail('the audit report carries no rolling window metrics (Refs #2157): %s'
                  % json.dumps(report)[:400])

    def status_of(self, alert):
        """The status of a finding: ``new`` the first time, ``ongoing`` after."""
        for key in ('status', 'state', 'alert_status', 'dedupe_status'):
            value = alert.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
        if alert.get('new') is True:
            return 'new'
        if alert.get('ongoing') is True:
            return 'ongoing'
        return None

    def reason_of(self, report):
        """The reason a metric is null: a non empty text of the report."""
        containers = [self.live_metrics(report), report]
        for container in containers:
            for key, value in container.items():
                if not isinstance(value, (str, dict)):
                    continue
                if isinstance(value, str) and 'reason' in key.lower() and value.strip():
                    return value.strip()
                if isinstance(value, dict):
                    inner = value.get('reason')
                    if isinstance(inner, str) and inner.strip():
                        return inner.strip()
        return None

    def counters(self, report):
        """The counters of the report: the (name, value) pairs of its numbers."""
        found = []
        for container in (report, self.live_metrics(report)):
            for key, value in container.items():
                if isinstance(value, int) and not isinstance(value, bool):
                    found.append((key, value))
        return found

    def age_state(self, path, seconds):
        """Move the first_seen of every finding of the state file into the past."""
        text = pathlib.Path(path).read_text(encoding='utf-8')
        payload = json.loads(text)
        old = time.time() - seconds
        found = []

        def walk(node):
            if isinstance(node, dict):
                for key, value in list(node.items()):
                    if key == 'first_seen':
                        found.append(value)
                        node[key] = (old if isinstance(value, (int, float))
                                     and not isinstance(value, bool) else iso(old))
                    else:
                        walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)
        if not found:
            self.fail('the audit state file must remember the first_seen of every '
                      'finding (Refs #2157): %s' % text[:400])
        pathlib.Path(path).write_text(json.dumps(payload), encoding='utf-8')

    # ------------------------------------------------------------------ cases

    def test_a_normal_sample_reports_no_findings(self):
        # Two live reservations of two different cards: nothing is wrong.
        self.add_reservation('a' * 32, 'broker', 'run-normal', backend='pi')
        self.add_reservation('b' * 32, 'worker', 'run-normal-2', backend='cursor')
        self.add_run('run-normal', issue=1001, title='card 1001')
        self.add_run('run-normal-2', issue=1002, title='card 1002')

        report, process = self.audit(db=self.db, runs=self.runs, state=self.state_path)

        self.assertEqual(process.returncode, 0, report)
        self.assertEqual(self.kinds(report), [])
        metrics = self.live_metrics(report)
        self.assertEqual(metrics.get('ghost_leases'), 0)
        self.assertEqual(metrics.get('expired_unreclaimed'), 0)
        self.assertEqual(metrics.get('double_claims'), 0)

    def test_two_reservations_of_one_run_are_a_double_claim(self):
        # The same run holds two reservations: the card is claimed twice.
        self.add_reservation('c' * 32, 'broker', 'run-dup', backend='pi')
        self.add_reservation('d' * 32, 'worker', 'run-dup', backend='cursor')

        report, _ = self.audit(db=self.db, runs=self.runs, state=self.state_path)

        alerts = self.alerts(report)
        self.assertEqual([row.get('kind') for row in alerts], ['double_claim'])
        alert = alerts[0]
        self.assertEqual(alert.get('run_id'), 'run-dup')
        self.assertTrue(alert.get('host'), 'an alert must name the host of the finding')
        self.assertTrue(alert.get('dedupe_key'), 'an alert must carry a dedupe key')
        self.assertTrue(alert.get('first_seen'), 'an alert must carry the moment it was seen')
        self.assertTrue(alert.get('detail'), 'an alert must carry its detail')
        # Both holders of the conflicting claim are named.
        self.assertIn('broker', json.dumps(alert))
        self.assertIn('worker', json.dumps(alert))
        self.assertEqual(self.live_metrics(report).get('double_claims'), 1)

    def test_two_active_reservations_of_one_card_are_a_double_claim(self):
        # Two different runs, one card: the card of the run records decides.
        self.add_reservation('e' * 32, 'broker', 'run-card-1', backend='pi')
        self.add_reservation('f' * 32, 'worker', 'run-card-2', backend='cursor')
        self.add_run('run-card-1', issue=2157, title='the card')
        self.add_run('run-card-2', issue=2157, title='the card')

        report, _ = self.audit(db=self.db, runs=self.runs, state=self.state_path)

        self.assertEqual([row.get('kind') for row in self.alerts(report)], ['double_claim'])
        self.assertEqual(self.live_metrics(report).get('double_claims'), 1)

    def test_the_card_is_read_from_a_run_record_named_run_json(self):
        # The run record of a card may be stored as run.json as well.
        self.add_reservation('g' * 32, 'broker', 'run-json-1', backend='pi')
        self.add_reservation('h' * 32, 'worker', 'run-json-2', backend='cursor')
        self.add_run('run-json-1', issue=4242, title='the card', name='run.json')
        self.add_run('run-json-2', issue=4242, title='the card', name='run.json')

        report, _ = self.audit(db=self.db, runs=self.runs, state=self.state_path)

        self.assertEqual([row.get('kind') for row in self.alerts(report)], ['double_claim'])
        self.assertEqual(self.live_metrics(report).get('double_claims'), 1)

    def test_an_expired_reservation_that_was_not_reclaimed_is_reported(self):
        # The lease ran out long ago and the row is still there.
        moment = time.time()
        self.add_reservation('i' * 32, 'broker', 'run-expired', backend='pi',
                             created=moment - 3600, expires=moment - 300)

        report, _ = self.audit(db=self.db, runs=self.runs, state=self.state_path)

        alerts = self.alerts(report)
        self.assertEqual([row.get('kind') for row in alerts], ['expired_unreclaimed'])
        self.assertEqual(alerts[0].get('run_id'), 'run-expired')
        self.assertEqual(self.live_metrics(report).get('expired_unreclaimed'), 1)
        # Without a run record nothing proves a ghost: the run is simply unknown.
        self.assertEqual(self.live_metrics(report).get('ghost_leases'), 0)

    def test_a_reservation_inside_the_grace_period_is_not_reported(self):
        # The lease ran out a moment ago: the grace period is still running.
        moment = time.time()
        self.add_reservation('j' * 32, 'broker', 'run-grace', backend='pi',
                             created=moment - 120, expires=moment - 30)

        report, _ = self.audit(db=self.db, runs=self.runs, state=self.state_path)

        self.assertEqual(self.alerts(report), [])
        self.assertEqual(self.live_metrics(report).get('expired_unreclaimed'), 0)

    def test_a_reservation_of_a_finished_run_is_a_ghost(self):
        # The run is over but the reservation is still held.
        self.add_reservation('k' * 32, 'worker', 'run-ghost', backend='cursor')
        self.add_run('run-ghost', issue=3003, title='the ghost card', status='failed')

        report, _ = self.audit(db=self.db, runs=self.runs, state=self.state_path)

        alerts = self.alerts(report)
        self.assertEqual([row.get('kind') for row in alerts], ['ghost'])
        self.assertEqual(alerts[0].get('run_id'), 'run-ghost')
        self.assertEqual(self.live_metrics(report).get('ghost_leases'), 1)
        self.assertEqual(self.live_metrics(report).get('expired_unreclaimed'), 0)

    def test_the_health_metrics_count_the_findings_and_refuse_to_invent_a_wait_time(self):
        moment = time.time()
        # A double claim of one run and a double claim of one card.
        self.add_reservation('l' * 32, 'broker', 'run-dup', backend='pi')
        self.add_reservation('m' * 32, 'worker', 'run-dup', backend='cursor')
        self.add_reservation('n' * 32, 'broker', 'run-card-1', backend='pi')
        self.add_reservation('o' * 32, 'worker', 'run-card-2', backend='cursor')
        # An expired reservation that was not reclaimed.
        self.add_reservation('p' * 32, 'local', 'run-expired', backend='agy',
                             created=moment - 3600, expires=moment - 300)
        # A reservation of a run that is over.
        self.add_reservation('q' * 32, 'worker', 'run-ghost', backend='copilot')
        self.add_run('run-card-1', issue=2157, title='the card')
        self.add_run('run-card-2', issue=2157, title='the card')
        self.add_run('run-ghost', issue=3003, title='the ghost card', status='failed')

        report, _ = self.audit(db=self.db, runs=self.runs, state=self.state_path, health=True)

        metrics = self.live_metrics(report)
        self.assertEqual(metrics.get('double_claims'), 2)
        self.assertEqual(metrics.get('expired_unreclaimed'), 1)
        self.assertEqual(metrics.get('ghost_leases'), 1)
        # The coordinator schema carries no acquire queue time: the wait metric
        # is null with a reason, never an invented number.
        self.assertIsNone(metrics.get('lease_age_seconds'),
                          'the coordinator records no acquire queue time: the wait '
                          'metric must be null, not a number of its own')
        self.assertTrue(self.reason_of(report),
                        'a null metric must carry a reason (Refs #2157)')

    def test_a_second_round_reports_the_same_findings_as_ongoing(self):
        self.add_reservation('r' * 32, 'broker', 'run-dup', backend='pi')
        self.add_reservation('s' * 32, 'worker', 'run-dup', backend='cursor')

        first, _ = self.audit(db=self.db, runs=self.runs, state=self.state_path)
        second, _ = self.audit(db=self.db, runs=self.runs, state=self.state_path)

        first_alerts = self.alerts(first)
        second_alerts = self.alerts(second)
        self.assertEqual([self.status_of(row) for row in first_alerts], ['new'])
        self.assertEqual([self.status_of(row) for row in second_alerts], ['ongoing'])
        self.assertEqual([row.get('dedupe_key') for row in first_alerts],
                         [row.get('dedupe_key') for row in second_alerts])
        self.assertEqual(second_alerts[0].get('first_seen'), first_alerts[0].get('first_seen'))

    def test_the_rolling_window_ignores_the_stock_that_is_older_than_the_window(self):
        moment = time.time()
        self.add_reservation('t' * 32, 'broker', 'run-expired', backend='pi',
                             created=moment - 3600, expires=moment - 300)

        first, _ = self.audit(db=self.db, runs=self.runs, state=self.state_path)
        window = self.window_metrics(first)
        self.assertEqual(window.get('expired_unreclaimed'), 1,
                         'a finding that was just seen belongs to the window')

        # The finding was first seen more than a day ago: it is stock, not news.
        self.age_state(self.state_path, WINDOW_S + 3600)
        second, _ = self.audit(db=self.db, runs=self.runs, state=self.state_path)

        window = self.window_metrics(second)
        self.assertEqual(window.get('expired_unreclaimed'), 0,
                         'the window counts the findings whose first_seen falls inside '
                         'it, never the historical stock of the database')
        self.assertEqual(self.live_metrics(second).get('expired_unreclaimed'), 1,
                         'the live snapshot still counts the reservation that is there')

    def test_a_reservation_without_a_run_record_is_skipped_and_counted(self):
        # The card of these two reservations cannot be read: they are skipped.
        self.add_reservation('u' * 32, 'broker', 'run-unknown-1', backend='pi')
        self.add_reservation('v' * 32, 'worker', 'run-unknown-2', backend='cursor')

        report, _ = self.audit(db=self.db, runs=self.runs, state=self.state_path)

        self.assertEqual(self.kinds(report), [],
                         'a card that cannot be read is skipped, never guessed')
        skipped = [value for name, value in self.counters(report)
                   if re.search(r'skip|unread|missing|unknown|without', name, re.I)]
        self.assertTrue(skipped and max(skipped) >= 1,
                        'the audit must count the run records it could not read: %s'
                        % json.dumps(report)[:400])

    def test_the_state_file_of_the_audit_belongs_next_to_the_database(self):
        # The state file of the audit is configurable, but its default is next
        # to the coordinator database it reads (Refs #2157).
        self.add_reservation('y' * 32, 'broker', 'run-dup', backend='pi')
        self.add_reservation('z' * 32, 'worker', 'run-dup', backend='cursor')
        before = set(self.files(self.coordinator))

        self.audit(db=self.db, runs=self.runs)

        added = sorted(set(self.files(self.coordinator)) - before)
        states = [name for name in added
                  if _is_json(self.coordinator / name)]
        self.assertTrue(states,
                        'the state file of the audit belongs next to the database: %s' % added)

    def test_the_audit_does_not_write_the_coordinator_database(self):
        self.add_reservation('w' * 32, 'broker', 'run-dup', backend='pi')
        self.add_reservation('x' * 32, 'worker', 'run-dup', backend='cursor')
        before_bytes = digest(self.db)
        before_rows = self.rows()
        before_schema = self.schema()
        before_files = self.files(self.coordinator)
        before_runs = self.files(self.runs)

        # No --state: the state file of the audit belongs next to the database.
        report, _ = self.audit(db=self.db, runs=self.runs)

        self.assertEqual([row.get('kind') for row in self.alerts(report)], ['double_claim'])
        self.assertEqual(digest(self.db), before_bytes,
                         'the audit must not write the coordinator database')
        self.assertEqual(self.rows(), before_rows)
        self.assertEqual(self.schema(), before_schema)
        self.assertEqual(self.files(self.runs), before_runs,
                         'the audit must not touch the run records')
        added = sorted(set(self.files(self.coordinator)) - set(before_files))
        # The state file of the audit (and, at most, a lock beside it) is the
        # only thing the audit may add; a SQLite journal would be a write.
        self.assertLessEqual(len(added), 2,
                             'the audit may only write its own state file: %s' % added)
        for name in added:
            self.assertNotIn('-wal', name)
            self.assertNotIn('-journal', name)
            self.assertNotIn('-shm', name)
            self.assertNotEqual(name, self.db.name)

    # ------------------------------------------------------------- inspection

    def rows(self):
        """Every row of the reservations table."""
        with sqlite3.connect(self.db) as db:
            return db.execute('SELECT token,host,run_id,backend,provider,created,'
                              'lease_expires,verified_boot_id FROM reservations '
                              'ORDER BY token').fetchall()

    def schema(self):
        """The schema of the coordinator database."""
        with sqlite3.connect(self.db) as db:
            return db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall()

    def files(self, directory):
        """The files of a directory and their digest."""
        found = {}
        for path in sorted(pathlib.Path(directory).rglob('*')):
            if path.is_file():
                found[str(path.relative_to(directory))] = digest(path)
        return found


if __name__ == '__main__':
    unittest.main()
