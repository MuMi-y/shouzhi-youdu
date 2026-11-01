"""Local browser and agent boundaries. The domain service owns all banking rules."""

from datetime import datetime, timezone, timedelta
import hashlib
import hmac
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, render_template, request, session
from werkzeug.exceptions import HTTPException

from src.bank_v1 import Service, DomainError
from .jobs import JobStore
from .model_route import model_identity


class RequestError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status


def persistent_secret(directory, name):
    path = directory / name
    try:
        with path.open('x', encoding='ascii') as stream:
            stream.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    result = path.read_text(encoding='ascii').strip()
    if len(result) < 32:
        raise ValueError('Local service secret is invalid')
    return result


def json_object():
    if not request.is_json:
        raise RequestError('INVALID_JSON', '请提交 JSON 对象。')
    body = request.get_json()
    if type(body) is not dict:
        raise RequestError('INVALID_JSON', '请求必须是对象。')
    return body


def require_model_ready(app):
    if 'runtime_status' in app.extensions:
        runtime = app.extensions['runtime_status']()
        if not runtime['ready_for_requests']:
            raise RequestError('AI_UNAVAILABLE', runtime['reason'], 503)


def create_app(data_dir, *, clock=None, service=None, tool_token=None, history=None, jobs=None):
    directory = Path(data_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    clock = clock or (lambda: datetime.now(timezone(timedelta(hours=8))))
    service = service if service is not None else Service(directory / 'bank.sqlite3', clock=clock)
    jobs = jobs if jobs is not None else JobStore(directory / 'jobs.sqlite3', clock=clock)
    # Rotate per service generation: orphaned old DSH processes cannot access a restarted bank.
    token = tool_token or secrets.token_hex(32)
    history_token = secrets.token_hex(32)
    (directory / '.tool-token').write_text(token, encoding='ascii')
    (directory / '.moneydata-tool-token').write_text(history_token, encoding='ascii')
    app = Flask(__name__)
    app.json.ensure_ascii = False
    app.config.update(SECRET_KEY=persistent_secret(directory, '.session-secret'),
                      TRUSTED_HOSTS=['127.0.0.1', 'localhost'],
                      SESSION_COOKIE_NAME='bank_v1_' + hashlib.sha256(str(directory).encode()).hexdigest()[:16], SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE='Strict', MAX_CONTENT_LENGTH=32768)
    app.extensions['bank_v1'] = dict(service=service, jobs=jobs, tool_token=token, clock=clock,
                                     history=history, history_token=history_token)
    from .handoff_store import HandoffError

    @app.errorhandler(HandoffError)
    def handoff_error(error):
        return jsonify(error={'code':str(error),'message':'本轮记录或恢复条件尚未满足；请核对原操作，不重复办理。'}),409

    @app.before_request
    def authorize():
        host = request.host  # Enforce the trusted host before creating a session.
        if request.path == '/api/tool-schema' or request.path.startswith('/api/tools/'):
            supplied = request.headers.get('Authorization', '')
            g.request_capability = None
            gateway = getattr(service, 'request_gateway', None)
            if hmac.compare_digest(supplied.encode(), ('Bearer ' + token).encode()):
                g.tool_scope = 'bank'
            elif gateway is not None and supplied.startswith('Bearer ') and gateway.recognizes(supplied[7:]):
                g.tool_scope, g.request_capability = 'bank', supplied[7:]
            elif history is not None and hmac.compare_digest(supplied.encode(), ('Bearer ' + history_token).encode()):
                g.tool_scope = 'moneydata'
            else:
                raise RequestError('TOOL_AUTH_REQUIRED', '工具接口认证失败。', 403)
            return
        # A tool token never grants browser confirmation rights, even alongside cookies.
        if request.headers.get('Authorization'):
            raise RequestError('WRONG_AUTH_CHANNEL', '此入口仅接受本地用户会话。', 403)
        if request.method == 'POST':
            origin = request.headers.get('Origin', '')
            try:
                parsed = urlsplit(origin)
            except ValueError:
                raise RequestError('INVALID_ORIGIN', '请求来源无效。', 403) from None
            if (parsed.scheme != request.scheme or parsed.netloc != host or parsed.path
                    or parsed.query or parsed.fragment or parsed.username or parsed.password):
                raise RequestError('INVALID_ORIGIN', '请从本地页面提交操作。', 403)
            supplied = request.headers.get('X-CSRF-Token', '')
            expected = session.get('csrf_token', '')
            if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
                raise RequestError('INVALID_CSRF', '会话校验失败，请刷新页面。', 403)
        elif request.path == '/' or request.path == '/api/state':
            if 'csrf_token' not in session:
                session['csrf_token'] = secrets.token_urlsafe(32)

    @app.after_request
    def headers(response):
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                                 'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
                                 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.get('/')
    def index():
        return render_template('index.html')

    @app.get('/workbench')
    def workbench():
        return render_template('workbench.html')

    @app.get('/api/health')
    def health():
        return jsonify(app='bank-agent-v1', version='2', ok=True)

    @app.get('/api/tool-schema')
    def schema():
        if g.tool_scope == 'moneydata':
            return jsonify(tools=history.tool_specs(), scope='moneydata', business_date=None)
        specs = service.request_gateway.specs(g.request_capability) if g.request_capability else service.tool_specs()
        return jsonify(tools=specs, scope='bank', business_date=service.state()['business_date'])

    @app.post('/api/tools/<name>')
    def tool(name):
        if (name.startswith('moneydata_')) != (g.tool_scope == 'moneydata'):
            raise RequestError('WRONG_DATA_SCOPE', '该工具不属于当前资料范围。', 403)
        if g.tool_scope == 'moneydata':
            try:
                return jsonify(history.call_tool(name, json_object()))
            except ValueError as error:
                raise RequestError(getattr(error, 'code', 'INVALID_ARGUMENTS'), str(error)) from None
        if g.request_capability:
            return jsonify(service.request_gateway.call(g.request_capability, name, json_object()))
        return jsonify(service.call_tool(name, json_object()))

    @app.get('/api/history/<view>')
    def history_view(view):
        if history is None:
            raise RequestError('HISTORY_UNAVAILABLE', '完整历史资料尚未加载，请用项目启动入口运行。', 503)
        if view not in {'overview', 'transactions', 'summary', 'forecast'}:
            raise RequestError('NOT_FOUND', '没有这个历史查询。', 404)
        args = request.args.to_dict()
        for name in ('offset', 'limit'):
            if name in args:
                try:
                    args[name] = int(args[name])
                except ValueError:
                    raise RequestError('INVALID_ARGUMENTS', '分页参数须为整数。') from None
        try:
            return jsonify(history.call_tool('moneydata_' + view, args))
        except ValueError as error:
            raise RequestError('INVALID_ARGUMENTS', str(error)) from None

    @app.get('/api/state')
    def state():
        value = service.state()
        # Mandate details are disclosed through an explicitly authorized query receipt.
        value.pop('mandates', None)
        value.update(csrf_token=session['csrf_token'], jobs=jobs.recent(50),
                     history_available=history is not None,
                     worker_error=jobs.setting('worker_error'),
                     schedule=jobs.setting('schedule', {'enabled': True, 'cadence': 'daily'}),
                     model=(app.extensions['runtime_status']() if 'runtime_status' in app.extensions else
                            {'managed': False, 'mode': 'UNREGISTERED', 'ready_for_requests': None,
                             'reason': '此启动方式未登记AI工作进程状态。',
                             'runtime': 'DeepSeek Harness', **model_identity()}))
        if jobs.handoff:
            from .job_display import project
            value['jobs'] = [project(job,service,jobs.handoff) for job in value['jobs']]
        return jsonify(value)

    @app.post('/api/actions/<name>')
    def action(name):
        allowed = {'confirm', 'reject', 'challenge', 'inject_transaction', 'due_check', 'retry_event', 'prepare_match', 'reject_match'}
        if name not in allowed:
            raise RequestError('UNKNOWN_ACTION', '不支持此用户操作。', 404)
        body = json_object()
        if name == 'retry_event' or (name == 'due_check' and body.get('idempotency_key')):
            require_model_ready(app)
        return jsonify(service.user_action(name, body))

    @app.post('/api/chat')
    def chat():
        body = json_object()
        if set(body) - {'prompt', 'scope'} or 'prompt' not in body or type(body['prompt']) is not str or not 1 <= len(body['prompt'].strip()) <= 6000:
            raise RequestError('INVALID_PROMPT', '请输入 1—6000 字的需求。')
        scope = body.get('scope', 'bank')
        if scope not in ('bank', 'moneydata') or (scope == 'moneydata' and history is None):
            raise RequestError('INVALID_SCOPE', '请选择已加载的资料范围。')
        require_model_ready(app)
        try:
            # A stable native DSH session can be resumed after process restarts.
            session_key = 'chat_session' if scope == 'bank' else 'moneydata_session'
            job = jobs.add(body['prompt'].strip(), session_id=jobs.setting(session_key, ''), scope=scope)
        except ValueError as error:
            raise RequestError('QUEUE_FULL', str(error), 429) from None
        return jsonify(job), 202

    @app.get('/api/jobs/<job_id>')
    def job(job_id):
        if not session.get('csrf_token'):
            raise RequestError('SESSION_REQUIRED', '请先打开本地页面。', 403)
        value = jobs.get(job_id)
        if value is None:
            raise RequestError('NOT_FOUND', '未找到此分析任务。', 404)
        if jobs.handoff:
            from .job_display import project
            value = project(value,service,jobs.handoff)
        return jsonify(value)

    @app.post('/api/jobs/<job_id>/retry')
    def retry_job(job_id):
        if json_object():
            raise RequestError('INVALID_ARGUMENTS', '恢复不接受替换请求参数。')
        require_model_ready(app)
        try:
            return jsonify(jobs.retry(job_id)), 202
        except ValueError as error:
            raise RequestError('RETRY_UNAVAILABLE', str(error), 409) from None

    @app.post('/api/schedule')
    def schedule():
        body = json_object()
        if (set(body) != {'enabled', 'cadence'} or type(body['enabled']) is not bool
                or type(body['cadence']) is not str or body['cadence'] not in {'daily', 'weekly'}):
            raise RequestError('INVALID_SCHEDULE', '请选择每日或每周检查。')
        jobs.set_setting('schedule', body)
        return jsonify(body)

    @app.errorhandler(RequestError)
    def request_error(error):
        return jsonify(error={'code': error.code, 'message': error.message}), error.status

    @app.errorhandler(DomainError)
    def domain_error(error):
        return jsonify(error={'code': error.code, 'message': error.message}), 409

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error={'code': f'HTTP_{error.code}', 'message': '请求格式、地址或方法无效。'}), error.code

    @app.errorhandler(Exception)
    def unexpected(error):
        app.logger.exception('V1 local request failed')
        return jsonify(error={'code': 'INTERNAL_ERROR', 'message': '请求未完成。若涉及银行操作，请查询原回执，勿另起一笔重试。'}), 500

    return app
