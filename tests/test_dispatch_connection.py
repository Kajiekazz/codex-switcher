"""Pre-dispatch retries never replay a queue write or touch a live desktop."""
import time
from unittest import mock

from codex_switcher.appserver import AppServerError
from codex_switcher.goal_resume import GoalResumeService
from test_auto_resume import AutoResumeEnv, THREAD
from test_goal_resume import FakeSession, NullDesktop, _FakeClock


class InitializationRetryTests(AutoResumeEnv):
    def make_service(self, sessions, sleep=None):
        clock = _FakeClock()
        service = GoalResumeService(
            session_factory=mock.Mock(side_effect=sessions),
            goals_provider=lambda: [], execution_provider=lambda ids: {},
            history_path=self.root / (str(time.monotonic_ns()) + '.jsonl'),
            sessions_provider=lambda **_: {}, desktop_client=NullDesktop(),
            clock=clock, sleep=sleep or clock.sleep,
        )
        self.addCleanup(service._receipt_tracker.stop)
        if service._queue_watch:
            self.addCleanup(service._queue_watch.stop)
        self.addCleanup(lambda: service._thread and service._thread.join(3))
        return service, clock

    @staticmethod
    def broken():
        session = FakeSession()
        session.initialize = mock.Mock(side_effect=AppServerError('initialize timed out after 30s'))
        session.close = mock.Mock(wraps=session.close)
        return session

    def finish(self, service):
        service._thread.join(3)
        job = service.status()['job']
        self.assertNotEqual(job['state'], 'running')
        return job

    def start(self, service, **options):
        service.start([THREAD], dry_run=False, confirm=True, started_via='switch:fixture@example.com', **options)

    def test_reconnect_waits_ten_seconds_and_submits_only_on_initialized_connection(self):
        broken, ready = self.broken(), FakeSession()
        service, clock = self.make_service([broken, ready])
        self.start(service)
        job = self.finish(service)
        self.assertEqual(job['state'], 'done')
        self.assertEqual(job['initialization_attempts'], 2)
        self.assertIsNone(job['initialization_retry_at'])
        self.assertGreaterEqual(clock.value, 1010)
        self.assertTrue(job['dispatch_initialized'])
        broken.close.assert_called_once()
        self.assertEqual(broken.methods(), [])
        self.assertEqual(ready.methods().count('thread/queue/add'), 1)
        self.assertEqual(service.history()[0]['initialization_attempts'], 2)

    def test_three_failed_connections_finish_with_the_actual_error(self):
        broken = [self.broken() for _ in range(3)]
        service, clock = self.make_service(broken)
        self.start(service)
        job = self.finish(service)
        self.assertEqual(job['state'], 'failed')
        self.assertEqual(job['initialization_attempts'], 3)
        self.assertEqual(job['results'], [])
        self.assertEqual(job['error'], 'initialize timed out after 30s')
        self.assertIsNone(job['initialization_retry_at'])
        self.assertGreaterEqual(clock.value, 1020)
        self.assertTrue(all(session.close.call_count == 1 for session in broken))

    def test_cancel_during_retry_delay_starts_no_replacement_process(self):
        broken, ready = self.broken(), FakeSession()
        service, _ = self.make_service([broken, ready])
        service._sleep = lambda _: service.cancel()
        self.start(service)
        self.assertEqual(self.finish(service)['state'], 'cancelled')
        self.assertEqual(service._session_factory.call_count, 1)
        self.assertEqual(ready.methods(), [])

    def test_chat_that_started_during_reconnect_is_not_queued_again(self):
        broken, ready = self.broken(), FakeSession()
        service, clock = self.make_service([broken, ready])
        current = {}
        service._execution_provider = lambda ids: {THREAD: dict(current)}
        def sleep(seconds):
            current.update(state='running', turn_id='new-live-turn')
            clock.sleep(seconds)
        service._sleep = sleep
        self.start(service)
        result = self.finish(service)['results'][0]
        self.assertTrue(result['already_running'])
        self.assertNotIn('thread/queue/add', ready.methods())

    def test_pause_during_reconnect_is_respected_before_submission(self):
        broken, ready = self.broken(), FakeSession()
        service, clock = self.make_service([broken, ready])
        def sleep(seconds):
            service._pauses[THREAD] = {'at': 'fixture-pause'}
            clock.sleep(seconds)
        service._sleep = sleep
        self.start(service)
        result = self.finish(service)['results'][0]
        self.assertEqual(result['error'], 'paused_by_user')
        self.assertNotIn('thread/queue/add', ready.methods())

    def test_queue_timeout_is_never_retried_by_reconnecting(self):
        ready = FakeSession(queue_error='thread/queue/add timed out after 30s')
        service, _ = self.make_service([ready])
        self.start(service)
        self.assertEqual(self.finish(service)['state'], 'failed')
        self.assertEqual(service._session_factory.call_count, 1)
        self.assertEqual(ready.methods().count('thread/queue/add'), 1)

    def test_manual_preview_and_capacity_keep_their_existing_retry_paths(self):
        for started_via, dry_run in ((None, False), ('switch:fixture@example.com', True), ('capacity_recovery', False)):
            with self.subTest(started_via=started_via, dry_run=dry_run):
                service, _ = self.make_service([self.broken()])
                service.start([THREAD], dry_run=dry_run, confirm=True, started_via=started_via)
                self.assertEqual(self.finish(service)['state'], 'failed')
                self.assertEqual(service._session_factory.call_count, 1)
