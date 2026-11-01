"""Advance durable tasks independently of slow model calls; scan changed facts only."""
import sqlite3
from contextlib import closing
import threading
from datetime import date, timedelta
from src.bank_v1.common import business_date


class CompetitionPump:
    def __init__(self, service, jobs, *, clock):
        self.service, self.jobs, self.clock = service, jobs, clock
        self.stop_event = threading.Event()
        self.thread = None
        self.fingerprint = None

    def step(self):
        self.service.advance_tasks()
        # Append-only transaction ledger: checking these aggregates is cheap and
        # does not reconstruct financial reports on every browser refresh.
        with closing(sqlite3.connect(self.service.db_path)) as db:
            count, last = db.execute('SELECT count(*),max(created_at) FROM transactions').fetchone()
        current = (count, last)
        today = date.fromisoformat(business_date(self.clock()))
        schedule = self.jobs.setting('schedule', {'enabled': True, 'cadence': 'daily'})
        previous = self.jobs.setting('last_insight_date')
        days = 7 if schedule['cadence'] == 'weekly' else 1
        if getattr(self.service, 'goal_followup', None):
            last_goal = self.jobs.setting('last_goal_date')
            goal_due = schedule['enabled'] and (last_goal is None or today >= date.fromisoformat(last_goal) + timedelta(days=days))
            self.service.host_action('goal-followup.enqueue',{'periodic_due':goal_due},
                lambda:self.service.goal_followup.enqueue(self.jobs, periodic_due=goal_due),origin='SYSTEM')
            if goal_due:
                self.jobs.set_setting('last_goal_date', today.isoformat())
        due = schedule['enabled'] and (previous is None or today >= date.fromisoformat(previous) + timedelta(days=days))
        if current != self.fingerprint or due:
            self.service.host_action('insights.check',{},self.service.insights.check,origin='SYSTEM',effect_class='INSIGHT_WRITE')
            self.fingerprint = current
            self.jobs.set_setting('last_insight_date', today.isoformat())

    def start(self):
        self.thread = threading.Thread(target=self.run, name='competition-tasks', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.step()
                self.jobs.set_setting('competition_error', None)
            except Exception as error:
                self.jobs.set_setting('competition_error', type(error).__name__)
            self.stop_event.wait(1)
