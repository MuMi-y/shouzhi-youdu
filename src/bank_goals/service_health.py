"""Persist unfinished analysis independently from whether a new notice is useful."""


def initialize(db, now):
    db.execute('''CREATE TABLE IF NOT EXISTS goal_service_health(
        id INTEGER PRIMARY KEY CHECK(id=1), pending_check_id TEXT,
        last_attempt_check_id TEXT, last_success_check_id TEXT,
        recovery_count INTEGER NOT NULL DEFAULT 0 CHECK(recovery_count BETWEEN 0 AND 1),
        retry_check_id TEXT, retry_parent_check_id TEXT, updated_at TEXT NOT NULL)''')
    if db.execute('SELECT 1 FROM goal_service_health WHERE id=1').fetchone():
        return
    last = db.execute("SELECT * FROM goal_checks WHERE state NOT IN ('QUIET','NO_MODEL') ORDER BY rowid DESC LIMIT 1").fetchone()
    accepted = db.execute("SELECT check_id FROM goal_checks WHERE state='ACCEPTED' ORDER BY rowid DESC LIMIT 1").fetchone()
    pending = last['check_id'] if last and last['state'] != 'ACCEPTED' else None
    attempted = last['check_id'] if last and last['state'] != 'PENDING' else None
    db.execute('INSERT INTO goal_service_health VALUES(1,?,?,?,0,NULL,NULL,?)',
               (pending, attempted, accepted['check_id'] if accepted else None, now))


def read(db):
    return dict(db.execute('SELECT * FROM goal_service_health WHERE id=1').fetchone())


def requested(db, check_id, reasons, now):
    health = read(db)
    refreshed_only = set(reasons) <= {'PENDING_EXPLANATION_REFRESHED'}
    if not health['pending_check_id'] or not refreshed_only:
        db.execute('''UPDATE goal_service_health SET pending_check_id=?,last_attempt_check_id=NULL,
            recovery_count=0,retry_check_id=NULL,retry_parent_check_id=NULL,updated_at=? WHERE id=1''',
            (check_id, now))


def started(db, check_id, now):
    db.execute('''UPDATE goal_service_health SET pending_check_id=COALESCE(pending_check_id,?),
        last_attempt_check_id=?,updated_at=? WHERE id=1''', (check_id, check_id, now))


def finished(db, check_id, state, now, *, is_latest):
    # An old async result must not overwrite the health of its newer successor.
    if not is_latest:
        return
    if state == 'ACCEPTED':
        db.execute('''UPDATE goal_service_health SET pending_check_id=NULL,last_attempt_check_id=?,
            last_success_check_id=?,recovery_count=0,retry_check_id=NULL,
            retry_parent_check_id=NULL,updated_at=? WHERE id=1''', (check_id, check_id, now))
    else:
        started(db, check_id, now)


def view(db, check, *, ai_enabled, active):
    health = read(db)
    if not active:
        state = 'IDLE'
    elif not ai_enabled:
        state = 'NO_MODEL'
    elif check and check['state'] in {'PENDING', 'RUNNING'}:
        state = check['state']
    elif health['pending_check_id']:
        state = 'RECOVERY_EXHAUSTED' if health['recovery_count'] else 'NEEDS_RECOVERY'
    else:
        state = 'CURRENT' if health['last_success_check_id'] else 'IDLE'
    notice = ('NOT_CHECKED' if not check else 'QUIET' if check['state'] == 'QUIET' else
              'RECOVERY' if check['reasons'] == ['TRUSTED_READONLY_RECOVERY'] else 'IMPORTANT')
    return {**{k: v for k, v in health.items() if k != 'id'}, 'state': state,
            'notice_decision': notice,
            'can_retry': state == 'NEEDS_RECOVERY' and bool(health['last_attempt_check_id']),
            'retry_limit': 1}
