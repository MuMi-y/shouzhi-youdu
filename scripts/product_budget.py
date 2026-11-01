"""Select a persistent product session; never renew limits on restart."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.competition_web.runtime_status import budget_status


class SessionError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@contextmanager
def selection_lock(directory):
    with (directory / '.selection-lock').open('a+b') as stream:
        stream.seek(0)
        if not stream.read(1):
            stream.write(b'1'); stream.flush()
        stream.seek(0)
        if sys.platform == 'win32':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def select_session(runtime, *, new_session=False):
    directory = Path(runtime).resolve() / 'ai-sessions'
    directory.mkdir(parents=True, exist_ok=True)
    pointer = directory / 'current.json'
    with selection_lock(directory):
        previous = None
        if pointer.exists():
            try:
                record = json.loads(pointer.read_text(encoding='utf-8'))
                previous = (directory / record['file']).resolve()
                if previous.parent != directory or previous.suffix != '.sqlite3' or not previous.name.startswith('session-'):
                    raise ValueError('Invalid product session path')
                state = budget_status(previous)
                if state['state'] in {'MISSING', 'UNREADABLE'} or state.get('id') != record['id']:
                    raise ValueError('Product session evidence missing')
            except (ValueError, KeyError, TypeError):
                raise SessionError('SESSION_RECORD_INVALID', '使用时段记录损坏，请保留文件并检查；不能自动重置。') from None
            if state['available']:
                if new_session:
                    raise SessionError('SESSION_STILL_ACTIVE', '当前AI使用时段仍有效，继续使用原限额。')
                return {'path': str(previous), 'created': False, **state}
            if not new_session:
                raise SessionError('SESSION_ENDED', '上一AI使用时段已结束。可明确开启下一时段，或暂不启用AI。')
        node = shutil.which('node')
        if node is None:
            raise SessionError('NODE_MISSING', '未找到已安装的Node运行环境。')
        target = directory / ('session-' + uuid4().hex + '.sqlite3')
        completed = subprocess.run([node, str(ROOT / 'scripts/batch_budget.cjs'), 'create', str(target), '32', '8', '60'],
                                   capture_output=True, text=True, encoding='utf-8', timeout=20)
        if completed.returncode != 0:
            raise SessionError('SESSION_CREATE_FAILED', '使用时段创建失败，旧记录未替换。')
        state = budget_status(target)
        if not state['available']:
            raise SessionError('SESSION_CREATE_FAILED', '新时段无法读取，旧记录未替换。')
        temporary = directory / ('pointer-' + uuid4().hex + '.tmp')
        temporary.write_text(json.dumps({'version': 1, 'id': state['id'], 'file': target.name,
                                         'previous': previous.name if previous else None}, ensure_ascii=False), encoding='utf-8')
        temporary.replace(pointer)
        return {'path': str(target), 'created': True, **state}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--new-session', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(select_session(args.runtime, new_session=args.new_session), ensure_ascii=False))
    except SessionError as error:
        print(json.dumps({'error': error.code, 'message': str(error)}, ensure_ascii=False))
        return 3 if error.code == 'SESSION_ENDED' else 4
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
