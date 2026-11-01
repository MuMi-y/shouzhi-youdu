"""Bounded, local Docker readiness shared by HTTP state and the AI worker."""
import json
import subprocess
import threading
import time


def probe():
    try:
        result = subprocess.run(['docker', 'info', '--format', '{{.OSType}}'],
                                capture_output=True, text=True, timeout=3,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode == 0 and result.stdout.strip() == 'linux':
            return {'available': True, 'reason': ''}
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {'available': False, 'reason': 'AI执行环境尚未就绪。请打开Docker Desktop，等待引擎启动；恢复后本页会自动重连，已有记录保留。'}


class DockerHealth:
    def __init__(self, *, check=probe, ttl=5):
        self.check, self.ttl = check, ttl
        self.lock = threading.Lock()
        self.expires = 0
        self.result = None

    def __call__(self):
        with self.lock:
            if time.monotonic() >= self.expires:
                self.result = self.check()
                self.expires = time.monotonic() + self.ttl
            return dict(self.result)


if __name__ == '__main__':
    state = probe()
    print(json.dumps(state, ensure_ascii=False))
    raise SystemExit(0 if state['available'] else 1)
