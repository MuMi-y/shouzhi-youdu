"""Run the competition edition on a separate persistent bank, fail closed for MFA."""
import argparse
import atexit
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def main():
    from waitress import create_server
    from src.bank_v1.security import SecurityPolicy, TotpPasswordVerifier
    from src.bank_insights.fixture import create_demo
    from src.bank_v1_web.worker import DshRunner, Worker
    from src.competition_web.app import create_app
    from src.competition_web.pump import CompetitionPump
    from src.moneydata_history import MoneyDataService, prepare_history
    from src.competition_web.runtime_status import RuntimeStatus, build_identity

    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8773)
    parser.add_argument('--data-dir', type=Path, default=PROJECT / 'runtime/competition_v1')
    parser.add_argument('--no-model', action='store_true', help='Start bank and task pump without DSH calls')
    parser.add_argument('--without-history', action='store_true', help='Explicitly omit optional source history')
    parser.add_argument('--enable-goals', action='store_true', help='Enable deterministic G1 goal planning')
    parser.add_argument('--goal-followup', action='store_true', help='Enable G2 goal tools and deterministic continuous checks')
    parser.add_argument('--goal-ai', action='store_true', help='Enable goal explanations in existing worker; requires a shared batch budget')
    parser.add_argument('--batch-budget', type=Path, help='Existing host SQLite budget created explicitly with batch_budget.cjs')
    args = parser.parse_args()
    if args.goal_followup and not args.enable_goals:
        parser.error('--goal-followup requires --enable-goals')
    if args.goal_ai and (not args.goal_followup or args.no_model or not args.batch_budget):
        parser.error('--goal-ai requires --goal-followup, --batch-budget and model mode')
    if args.batch_budget and not args.batch_budget.is_file():
        parser.error('Budget file must already exist; this launcher never creates or resets it')
    if not args.no_model and not args.batch_budget:
        parser.error('Model mode requires --batch-budget; use start_product.ps1 for a bounded product session')
    directory = args.data_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    lock = (directory / '.service-lock').open('a+b')
    lock.seek(0)
    if not lock.read(1):
        lock.write(b'1'); lock.flush()
    lock.seek(0)
    if sys.platform == 'win32':
        import msvcrt
        try: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError: raise SystemExit('This runtime is already running.') from None
    else:
        import fcntl
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    clock = lambda: datetime.now(timezone(timedelta(hours=8)))
    if not (directory / 'bank.sqlite3').exists():
        create_demo(directory / 'bank.sqlite3', clock=clock)
    history = None
    if not args.without_history:
        prepare_history(PROJECT, directory / 'moneydata.sqlite3')
        history = MoneyDataService(directory / 'moneydata.sqlite3')
    policy = SecurityPolicy(mode='independent', verifier=TotpPasswordVerifier(directory / 'identity.sqlite3'))
    app = create_app(directory, clock=clock, history=history, security_policy=policy, goals_enabled=args.enable_goals,
                     goal_followup=args.goal_followup, goal_ai=args.goal_ai, service_lock=lock)
    server = create_server(app, host='127.0.0.1', port=args.port, threads=8)
    extension = app.extensions['bank_v1']
    pump = CompetitionPump(app.extensions['competition'], extension['jobs'], clock=clock)
    pump.start(); atexit.register(pump.stop)
    worker = None
    if not args.no_model:
        runner = DshRunner(PROJECT, directory, f'http://127.0.0.1:{args.port}', extension['tool_token'],
                           history_token=extension['history_token'], sandbox=True, timeout=300, batch_budget=args.batch_budget,
                           request_gateway=extension['service'].request_gateway)
        worker = Worker(extension['service'], extension['jobs'], runner, clock=clock)
    runtime_status = RuntimeStatus(mode='NO_MODEL' if args.no_model else 'MODEL', worker=worker,
        budget_path=args.batch_budget, build_id=build_identity(PROJECT), capabilities={
            'goals': args.enable_goals, 'goal_followup': args.goal_followup,
            'goal_ai': args.goal_ai, 'history': history is not None, 'bank': True, 'tasks': True})
    app.extensions['runtime_status'] = runtime_status.view
    if worker:
        worker.can_run = lambda: runtime_status.view()['ready_for_requests']
        worker.start(); atexit.register(worker.stop)
    print(f'Youshu Competition http://127.0.0.1:{args.port} | separate simulated bank | DSH sandbox', flush=True)
    server.run()


if __name__ == '__main__':
    main()
