"""Describe actual local process capability, separately from provider success."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from src.bank_v1_web.model_route import model_identity
from .docker_health import DockerHealth


def budget_status(path, now_ms=None):
    if path is None:
        return {'state': 'MISSING', 'available': False}
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute('SELECT * FROM batch').fetchall()
        if len(rows) != 1:
            raise ValueError('Invalid budget')
        row = dict(rows[0])
        if any(type(row[key]) is not int or row[key] < 0 for key in
               ('provider_limit', 'dsh_limit', 'provider_used', 'dsh_used', 'deadline_ms')):
            raise ValueError('Invalid budget counters')
        state = row['state']
        if state == 'ACTIVE':
            if now_ms >= row['deadline_ms']:
                state = 'EXPIRED'
            elif row['provider_used'] >= row['provider_limit'] or row['dsh_used'] >= row['dsh_limit']:
                state = 'EXHAUSTED'
        return {'id': row['id'], 'state': state, 'available': state == 'ACTIVE',
                'provider_remaining': max(0, row['provider_limit'] - row['provider_used']),
                'dsh_remaining': max(0, row['dsh_limit'] - row['dsh_used']),
                'deadline_ms': row['deadline_ms'], 'stop_reason': row['stop_reason']}
    except (OSError, sqlite3.Error, KeyError, ValueError, TypeError):
        return {'state': 'UNREADABLE', 'available': False}


def dependency_status(package=None):
    package = Path(package or os.environ.get('DSH_PACKAGE_PATH') or
                   Path(__file__).resolve().parents[2] / 'node-runtime/node_modules/@deepseek-ai/dsh')
    try:
        version = json.loads((package / 'package.json').read_text(encoding='utf-8'))['version']
    except (OSError, ValueError, KeyError, TypeError):
        return {'available': False, 'reason': '项目的DSH依赖缺失，请运行 npm ci --prefix node-runtime --ignore-scripts --legacy-peer-deps。'}
    return {'available': version == '0.1.1-rc.2', 'version': version,
            'reason': '' if version == '0.1.1-rc.2' else '项目DSH版本不兼容，需要0.1.1-rc.2；尚未调用模型。'}


def build_identity(project):
    """A content identity, not a claim of release acceptance or provider health."""
    root = Path(project)
    paths = [p for folder in ('src', 'plugins/bank-v1') for p in (root / folder).rglob('*')
             if p.is_file() and p.suffix in ('.py', '.js', '.css', '.html', '.txt', '.yml', '.json')
             and '__pycache__' not in p.parts]
    paths.extend(root / p for p in ('scripts/competition_web.py', 'scripts/start_product.ps1',
                                   'scripts/start_competition.ps1', 'scripts/ensure_docker.ps1', 'scripts/product_budget.py', 'scripts/check_dsh_runtime.cjs', 'scripts/model_route.cjs', 'scripts/dsh_v1.cjs',
                                   'scripts/dsh_sandbox.cjs', 'scripts/dsh_container.cjs', 'scripts/sandbox_layout.cjs', 'scripts/sandbox_provider.cjs',
                                   'node-runtime/package.json', 'node-runtime/package-lock.json', 'config/model_route.json', 'config/settings.example.yaml', '启动有数.cmd'))
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(root).as_posix().encode('utf-8'))
        digest.update(path.read_bytes())
    return 'work-' + digest.hexdigest()[:12]


class RuntimeStatus:
    def __init__(self, *, mode, worker, budget_path, build_id, capabilities, environment=None):
        self.mode, self.worker, self.budget_path = mode, worker, budget_path
        self.build_id, self.capabilities = build_id, capabilities
        self.environment = environment if environment is not None else DockerHealth()

    def view(self):
        running = bool(self.worker and self.worker.thread and self.worker.thread.is_alive()
                       and not self.worker.stop_event.is_set())
        budget = budget_status(self.budget_path) if self.mode == 'MODEL' else {'state': 'NOT_USED', 'available': False}
        dependency = dependency_status()
        environment = self.environment() if self.mode == 'MODEL' else {'available': False, 'reason': ''}
        ready = self.mode == 'MODEL' and running and budget['available'] and dependency['available'] and environment['available']
        reason = ('本次未启动AI；账本查看与确定性功能仍可使用。' if self.mode != 'MODEL' else
                  dependency['reason'] if not dependency['available'] else
                  environment['reason'] if not environment['available'] else
                  'AI工作进程没有运行。' if not running else
                  '本次AI使用时段已结束或限额不可用；已有银行记录仍保留。' if not budget['available'] else
                  '本机可受理AI请求；模型服务是否成功以每次实际结果为准。')
        return {'managed': True, 'mode': self.mode, 'worker_running': running,
                'ready_for_requests': ready, 'reason': reason, 'budget': budget, 'dependency': dependency, 'environment': environment,
                'build_id': self.build_id, 'capabilities': self.capabilities,
                'runtime': 'DeepSeek Harness', **model_identity(),
                'provider_status': 'NOT_PROBED_BY_STATUS_QUERY'}
