"""Separate fictional G1 demo, fixed date, no worker/model calls and no daily ledger access."""
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def seed_new_fixture(path, now):
    """Initialize only a nonexistent G1 bank; never reset an existing database."""
    from src.bank_v1 import Service
    from src.bank_v1.storage import connection
    if path.exists():
        return
    bank = Service(path, clock=lambda: now)
    with connection(bank.db_path, True) as db:
        db.execute('DELETE FROM transactions')
        db.execute('DELETE FROM arrangements')
        db.execute("UPDATE accounts SET balance_minor=CASE account_id WHEN 'A' THEN 800000 WHEN 'B' THEN 200000 ELSE 0 END")
        for account, amount in [('A', 800000), ('B', 200000)]:
            db.execute('INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?)',
                       ('g1-opening-'+account, account, 'OPENING', amount, 'G1虚构期初', now.date().isoformat(),
                        now.isoformat(), None, None, 'G1_SYNTHETIC_DEMO'))
        for aid, title, kind, amount, offset in [('loan','固定还款','expense',300000,5),
                ('living','生活开支','expense',200000,10),('income','预计收入（未到账）','income',250000,25)]:
            db.execute('INSERT INTO arrangements VALUES(?,?,?,?,?,?,?,?,?)',
                       (aid,1,title,kind,amount,(now+timedelta(days=offset)).date().isoformat(),
                        now.isoformat(),'G1_SYNTHETIC_DEMO','active'))
        db.execute("UPDATE metadata SET value=? WHERE key='scenario'", (
            '{"scenario_id":"g1-fictional","source":"SYNTHETIC_FIXTURE","description":"G1虚构目标演示；固定业务日2026-11-02，无AI调用"}',))


def main():
    from waitress import create_server
    from src.competition_web.app import create_app
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8778)
    args = parser.parse_args()
    # Deliberately distinct from runtime/competition_v1; never accept an arbitrary bank path.
    directory = PROJECT/'runtime/g1_goal_demo'
    directory.mkdir(parents=True, exist_ok=True)
    lock = (directory/'.g1-demo-lock').open('a+b')
    lock.seek(0)
    if not lock.read(1): lock.write(b'1');lock.flush()
    lock.seek(0)
    if sys.platform == 'win32':
        import msvcrt
        try: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError: raise SystemExit('G1 demo already running; no reseed.') from None
    now = datetime(2026,11,2,10,tzinfo=timezone(timedelta(hours=8)))
    seed_new_fixture(directory/'bank.sqlite3',now)
    app = create_app(directory,clock=lambda:now,goals_enabled=True)
    app.extensions['bank_v1']['jobs'].set_setting('worker_error','G1演示仅运行确定性计算；模型和主动分析未启动。')
    print(f'G1 fictional fixed-date demo http://127.0.0.1:{args.port} | NO MODEL | 2026-11-02',flush=True)
    create_server(app,host='127.0.0.1',port=args.port,threads=4).run()


if __name__ == '__main__':main()
