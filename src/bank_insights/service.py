"""Fixed-identity read-only bank adapter and separate durable user feedback."""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from .rules import analysis_policy, build_report, canonical, detect_recurring, digest, display
from .notifications import group_notifications, groups_with_new_notices
from .policy import KEY as POLICY_KEY, read_config, decode_config, choose_config, default_policy, config_metadata


class InsightError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def valid_date(value):
    if type(value) is not str:
        raise InsightError('INVALID_ARGUMENTS', '日期须为YYYY-MM-DD')
    try:
        if date.fromisoformat(value).isoformat() != value: raise ValueError()
    except ValueError as error:
        raise InsightError('INVALID_ARGUMENTS', '日期须为有效YYYY-MM-DD') from error


def tool_specs():
    period = {'type': 'string', 'format': 'date'}
    definitions = [
        ('billing_report', '请求日期区间账单及前一等长期间比较，退款/内部转账分离；按amounts展示金额。period_metadata分别标明本期/前期起止、含首尾天数及完整单自然月或任意窗口；完整月不证明资料齐全。任意窗口只称该期间，完整月只称那个月，不称稳定每月结余或未来存钱能力。metrics给出消费/收入及净流入/收入两个独立比例，不自行取补数，净流入不等于可支配储蓄。description_groups按原描述和交易类型分组，最多20组，截断及遗漏金额/笔数明确；原描述不等于商户身份或行业，编号不能推语义。description_comparison在完整原描述×交易类型并集上比较后再显示前20组，按每组change的整数分及debit/credit_change_rate.change_percent展示变化；比例定义在根metric_definitions，路径相对各item，不挪用全期同值差额。两期未观察到与未显示不同；说其余不变须核完整changed_group_count及kind_summary/隐藏变化计数，不能因隐藏净差为0断言没有变化。NOT_REQUESTED表示未计算，不是零变化；原描述不是商户身份、组流量不是当前余额，资料不保证现实全量。不能代替周期识别；周期候选用recurring_candidates，变化刷新用check_insights。',
         {'start_date': period, 'end_date': period, 'compare_previous': {'type': 'boolean'}}, ['start_date', 'end_date']),
        ('recurring_candidates', '读取已扫描周期扣费候选、具名观察字段和用户反馈，用于周期/订阅取证；候选不证明合同或正式代扣，不查询授权；本次变化检查用check_insights刷新。', {}, []),
        ('insight_status', '仅读取已有关注项及提醒缓存，不扫描流水，不等于重新检查；历史提醒是否仍有效见status。', {}, []),
        ('check_insights', '用户要求检查、重新检查或查有没有变化时先调用本工具；扫描已授权完整流水并记录有限候选及去重提醒。insight_status仅读缓存不等于扫描。本工具不修改银行资金或查询正式代扣。', {}, [])]
    return [{'name': n, 'description': d, 'parameters': {'type': 'object', 'properties': p,
        'required': r, 'additionalProperties': False}} for n, d, p, r in definitions]


@contextmanager
def connect(path, write=False):
    db = sqlite3.connect(path, timeout=20)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


class InsightService:
    def __init__(self, db_path, *, bank, clock):
        self.db_path, self.bank, self.clock = Path(db_path), bank, clock
        if self.db_path.resolve() == Path(bank.db_path).resolve():
            raise ValueError('关注数据库必须与银行数据库分离')
        requested_policy = read_config(self.db_path.parent)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with connect(self.db_path, True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY,version INTEGER NOT NULL,body TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS notices(id TEXT PRIMARY KEY,body TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY,at TEXT NOT NULL,candidate_id TEXT NOT NULL,body TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            stored_row = db.execute('SELECT value FROM metadata WHERE key=?', (POLICY_KEY,)).fetchone()
            stored_policy = decode_config(stored_row[0]) if stored_row else None
            selected_policy = choose_config(stored_policy, requested_policy)
        self._policy_config = selected_policy
        self._policy = analysis_policy(selected_policy['thresholds'])
        old_values = stored_policy['thresholds'] if stored_policy else default_policy()
        if self._policy != old_values:
            # Candidate/notice refresh and config activation commit together.
            self._check(policy_to_store=selected_policy)
        elif stored_policy != selected_policy:
            with connect(self.db_path, True) as db:
                db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',
                           (POLICY_KEY, canonical(selected_policy)))


    def _now(self):
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('clock must return aware datetime')
        return now.astimezone(timezone(timedelta(hours=8)))

    def _rows(self):
        now = self._now()
        db = sqlite3.connect(Path(self.bank.db_path).resolve().as_uri()+'?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            rows = db.execute('''SELECT t.*,a.currency FROM transactions t JOIN accounts a USING(account_id)
                WHERE a.subject_id='U1' AND t.business_date<=? ORDER BY t.business_date,t.transaction_id''',
                (now.date().isoformat(),)).fetchall()
            result = []
            for row in rows:
                created = datetime.fromisoformat(row['created_at'])
                if created.tzinfo is None or created > now: continue
                if row['currency'] != 'CNY':
                    raise InsightError('UNSUPPORTED_CURRENCY', '银行币种不符合固定CNY合同，不能合并计算')
                result.append({'id': row['transaction_id'], 'date': row['business_date'],
                    'account_id': row['account_id'], 'kind': row['kind'], 'amount_minor': row['amount_minor'],
                    'description': row['description'], 'category': row['kind'], 'source': row['source']})
            return result
        finally:
            db.close()

    def tool_specs(self): return tool_specs()

    def _wrap(self, data):
        data = {**data, 'analysis_policy': analysis_policy(self._policy),
                'policy_config': config_metadata(self._policy_config)}
        day = self._now().date().isoformat()
        return {'evidence_id': 'insight_'+digest([day, data])[:24], 'scope': 'bank',
                'currency': 'CNY', 'as_of': day, 'data': data}

    def call_tool(self, name, args):
        spec = next((s for s in tool_specs() if s['name'] == name), None)
        if spec is None: raise InsightError('UNKNOWN_TOOL', '不支持该分析工具')
        schema = spec['parameters']
        if type(args) is not dict or set(args)-set(schema['properties']) or set(schema['required'])-set(args):
            raise InsightError('INVALID_ARGUMENTS', '缺少必要参数或存在未知字段')
        if name == 'billing_report':
            valid_date(args['start_date']); valid_date(args['end_date'])
            if args['start_date'] > args['end_date'] or args['end_date'] > self._now().date().isoformat():
                raise InsightError('INVALID_ARGUMENTS', '账单区间不得倒置或包含未来')
            if type(args.get('compare_previous', True)) is not bool:
                raise InsightError('INVALID_ARGUMENTS', 'compare_previous须为布尔值')
            if args.get('compare_previous', True):
                start = date.fromisoformat(args['start_date'])
                days = (date.fromisoformat(args['end_date'])-start).days+1
                if (start-date.min).days < days:
                    raise InsightError('INVALID_ARGUMENTS', '前一等长区间超出日期范围，请关闭前期比较')
            return self._wrap({'report': build_report(self._rows(), currency='CNY', policy=self._policy, **args)})
        if name == 'check_insights': return self.check()
        data = self.state()
        return self._wrap({'candidates': data['candidates']} if name == 'recurring_candidates' else data)

    def state(self):
        with connect(self.db_path) as db:
            candidates = [json.loads(r[0]) for r in db.execute('SELECT body FROM candidates ORDER BY id')]
            notifications = [json.loads(r[0]) for r in db.execute('SELECT body FROM notices ORDER BY rowid DESC LIMIT 100')]
            # History remains bounded; active grouping must not lose a reason to that limit.
            all_notices = [json.loads(r[0]) for r in db.execute('SELECT body FROM notices')]
            active = [notice for notice in all_notices if notice['status'] == 'active']
            return {'candidates': candidates, 'notifications': notifications,
                'notification_groups': group_notifications(active, candidates),
                'last_check': next((r[0] for r in db.execute("SELECT value FROM metadata WHERE key='last_check'")), None)}

    def check(self):
        return self._check()

    def _check(self, *, policy_to_store=None):
        now, rows = self._now(), self._rows()
        observed = detect_recurring(rows, as_of=now.date().isoformat(), currency='CNY', policy=self._policy)
        new_notices = []
        with connect(self.db_path, True) as db:
            observed_ids = {item['candidate_id'] for item in observed}
            for row in db.execute('SELECT body FROM candidates').fetchall():
                saved = json.loads(row[0])
                if saved['candidate_id'] not in observed_ids and saved.get('currently_observed', True):
                    saved['currently_observed'] = False
                    saved['version'] += 1
                    db.execute('UPDATE candidates SET version=?,body=? WHERE id=?',
                        (saved['version'], canonical(saved), saved['candidate_id']))
            for item in observed:
                old_row = db.execute('SELECT body FROM candidates WHERE id=?', (item['candidate_id'],)).fetchone()
                old = json.loads(old_row[0]) if old_row else None
                corrections = dict(old['corrections']) if old else {}
                correction_history = list(old.get('correction_history', [])) if old else []
                corrected_due = corrections.get('next_due_date')
                if corrected_due and old:
                    # A next-date correction describes one occurrence, not an eternal date.
                    # New same-merchant evidence consumes that observation override only;
                    # it does not confirm a contract or change a bank arrangement.
                    new_observations = [(ref, day) for ref, day in zip(item['evidence_refs'], item['observed_dates'])
                        if ref not in old['evidence_refs'] and
                        date.fromisoformat(day) >= date.fromisoformat(corrected_due)-timedelta(days=2)]
                    if new_observations:
                        ref, day = min(new_observations, key=lambda value: value[1])
                        correction_history.append({'next_due_date': corrected_due,
                            'observed_transaction_id': ref, 'observed_date': day,
                            'boundary': '已观察到后续同商户出账，结束本次日期覆盖；不证明正式合同履约。'})
                        corrections.pop('next_due_date')
                item.update({'currently_observed': True, 'status': old['status'] if old else 'proposed',
                    'confirmed': old['confirmed'] if old else False, 'corrections': corrections,
                    'correction_history': correction_history,
                    'version': old['version']+(old['evidence_hash'] != item['evidence_hash'] or not old.get('currently_observed', True)) if old else 1})
                item.update(item['corrections'])
                item['amount'] = display(item['amount_minor'])
                db.execute('INSERT OR REPLACE INTO candidates VALUES(?,?,?)', (item['candidate_id'], item['version'], canonical(item)))
            candidates = [json.loads(r[0]) for r in db.execute('SELECT body FROM candidates')]
            by_id = {item['candidate_id']: item for item in candidates}
            for row in db.execute('SELECT id,body FROM notices').fetchall():
                notice = json.loads(row['body'])
                candidate = by_id[notice['candidate_id']]
                outdated = (notice['due_date'] != candidate['next_due_date'] or
                            notice['amount_minor'] != candidate['amount_minor'] or
                            candidate['status'] in ('ignored', 'rejected') or not candidate.get('currently_observed', True) or
                            (notice['kind'] == 'RENEWAL_DUE' and notice['due_date'] < now.date().isoformat()) or
                            (notice['kind'] == 'PRICE_INCREASE' and (not candidate.get('price_increase') or
                             'amount_minor' in candidate.get('corrections', {}))))
                if outdated and notice['status'] == 'active':
                    notice['status'] = 'superseded'
                    db.execute('UPDATE notices SET body=? WHERE id=?', (canonical(notice), row['id']))
            for item in candidates:
                if not item['confirmed'] or item['status'] in ('ignored', 'rejected') or not item.get('currently_observed', True): continue
                days = (date.fromisoformat(item['next_due_date'])-now.date()).days
                kinds = []
                if 0 <= days <= 7: kinds.append(('RENEWAL_DUE', '已确认关注的周期支出临近推算日期，请核对是否需要保留。'))
                if item['price_increase'] and 'amount_minor' not in item['corrections']:
                    kinds.append(('PRICE_INCREASE', f"最近周期出账较前期固定金额上升至少{analysis_policy(self._policy)['recurring_price_increase']['minimum_increase_percent']}%；候选原因待核对，不自动认定商户涨价。"))
                if days < -7: kinds.append(('MISSED_EXPECTED_CHARGE', '推算日期后7天仍未观察到同商户周期扣费；不代表违约或合同终止。'))
                for kind, message in kinds:
                    nid = 'notice_'+digest([item['candidate_id'], kind, item['next_due_date'], item['amount_minor'], item['corrections']])[:24]
                    notice = {'notification_id': nid, 'candidate_id': item['candidate_id'], 'kind': kind,
                        'message': message, 'created_at': now.isoformat(), 'status': 'active', 'currency': 'CNY',
                        'amount_minor': item['amount_minor'], 'amount': item['amount'], 'due_date': item['next_due_date'],
                        'evidence_refs': item['evidence_refs'], 'candidate_version': item['version']}
                    if db.execute('INSERT OR IGNORE INTO notices VALUES(?,?)', (nid, canonical(notice))).rowcount:
                        new_notices.append(notice)
                    else:
                        existing = json.loads(db.execute(
                            'SELECT body FROM notices WHERE id=?', (nid,)).fetchone()[0])
                        changed = existing['message'] != message
                        existing['message'] = message
                        if existing['status'] == 'superseded':
                            # Current candidate/kind checks already established validity.
                            # Restore this same notice without another notification push.
                            existing.update({'status': 'active', 'reactivated_at': now.isoformat(),
                                'candidate_version': item['version'], 'evidence_refs': item['evidence_refs']})
                            changed = True
                        if changed:
                            db.execute('UPDATE notices SET body=? WHERE id=?', (canonical(existing), nid))
            db.execute("INSERT OR REPLACE INTO metadata VALUES('last_check',?)", (now.isoformat(),))
            if policy_to_store is not None:
                db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',
                           (POLICY_KEY, canonical(policy_to_store)))
        state = self.state()
        return self._wrap({**state, 'new_notifications': new_notices,
            'new_notification_groups': groups_with_new_notices(state['notification_groups'], new_notices)})

    def user_action(self, name, args):
        if name != 'candidate_feedback': raise InsightError('UNKNOWN_ACTION', '不支持该用户动作')
        allowed = {'candidate_id', 'expected_version', 'decision', 'label', 'amount_minor', 'next_due_date'}
        required = {'candidate_id', 'expected_version', 'decision'}
        if type(args) is not dict or set(args)-allowed or required-set(args):
            raise InsightError('INVALID_ARGUMENTS', '反馈字段不完整或含未知字段')
        if type(args['candidate_id']) is not str or not 1 <= len(args['candidate_id']) <= 100 or type(args['expected_version']) is not int or args['expected_version'] < 1:
            raise InsightError('INVALID_ARGUMENTS', '候选ID或版本错误')
        decision = args['decision']
        if type(decision) is not str or decision not in ('confirm', 'reject', 'ignore', 'correct'):
            raise InsightError('INVALID_ARGUMENTS', '不支持该反馈')
        corrections = {k: v for k, v in args.items() if k not in required}
        if (decision == 'correct' and not corrections) or (decision != 'correct' and corrections):
            raise InsightError('INVALID_ARGUMENTS', '只有更正动作接受且必须提供更正字段')
        if 'label' in corrections and (type(corrections['label']) is not str or not 1 <= len(corrections['label'].strip()) <= 200):
            raise InsightError('INVALID_ARGUMENTS', '名称须为1至200字符')
        if 'amount_minor' in corrections and (type(corrections['amount_minor']) is not int or not 1 <= corrections['amount_minor'] <= 100000000):
            raise InsightError('INVALID_ARGUMENTS', '金额须为正整数分')
        if 'next_due_date' in corrections:
            valid_date(corrections['next_due_date'])
            delta = (date.fromisoformat(corrections['next_due_date'])-self._now().date()).days
            if not 0 <= delta <= 366: raise InsightError('INVALID_ARGUMENTS', '更正日期须在今日至一年内')
        with connect(self.db_path, True) as db:
            row = db.execute('SELECT body FROM candidates WHERE id=?', (args['candidate_id'],)).fetchone()
            if not row: raise InsightError('NOT_FOUND', '候选不存在，请先检查流水')
            item = json.loads(row[0])
            if item['version'] != args['expected_version']:
                raise InsightError('STALE_VERSION', '候选已变化，请重新读取并确认')
            item['version'] += 1
            item['status'] = {'confirm': 'confirmed', 'correct': 'confirmed', 'reject': 'rejected', 'ignore': 'ignored'}[decision]
            item['confirmed'] = decision in ('confirm', 'correct')
            item['corrections'].update(corrections)
            item.update(corrections)
            item['amount'] = display(item['amount_minor'])
            db.execute('UPDATE candidates SET version=?,body=? WHERE id=?', (item['version'], canonical(item), item['candidate_id']))
            db.execute('INSERT INTO feedback(at,candidate_id,body) VALUES(?,?,?)', (self._now().isoformat(), item['candidate_id'], canonical(args)))
            for n in db.execute('SELECT id,body FROM notices').fetchall():
                notice = json.loads(n['body'])
                if notice['candidate_id'] == item['candidate_id'] and decision != 'confirm':
                    notice['status'] = 'superseded'
                    db.execute('UPDATE notices SET body=? WHERE id=?', (canonical(notice), n['id']))
        return self._wrap({'candidate': item, 'boundary': '反馈只修改关注偏好，不查询或变更银行代扣授权，也不解除商户合同。'})
