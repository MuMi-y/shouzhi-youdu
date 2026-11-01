"""G1 integration cases: temporary fictional bank; no model or credentials."""
from contextlib import closing
from datetime import datetime
import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.bank_insights.fixture import create_demo
from src.bank_insights.policy import PolicyError, default_config, validate_config, decode_config, KEY
from src.bank_insights.rules import analysis_policy, build_report, detect_recurring
from src.bank_insights.service import InsightService
from src.competition_web.app import create_app


NOW = datetime.fromisoformat('2026-10-12T12:00:00+08:00')


def config(revision=1, category=20, absolute=10000, recurring=10):
    value = default_config()
    value['revision'] = revision
    value['thresholds']['category_increase'].update(
        minimum_increase_percent=category, minimum_absolute_increase_minor=absolute)
    value['thresholds']['recurring_price_increase']['minimum_increase_percent'] = recurring
    return value


class PolicyIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bank = create_demo(self.root/'bank.sqlite3', clock=lambda: NOW)
        with closing(sqlite3.connect(self.bank.db_path)) as db:
            for key, day, amount in [('policy_old', '2026-05-10', 10000), ('policy_new', '2026-06-10', 12500)]:
                db.execute('INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (key, 'A', 'expense', -amount, '虚构规则配置样本', day,
                            day+'T12:00:00+08:00', None, None, 'SYNTHETIC_POLICY_TEST'))
                db.execute("UPDATE accounts SET balance_minor=balance_minor-?,version=version+1 WHERE account_id='A'", (amount,))
            db.commit()

    def write(self, value):
        (self.root/'insight-policy.json').write_text(json.dumps(value), encoding='utf-8')

    def start(self):
        app = create_app(self.root, clock=lambda: NOW, bank=self.bank)
        app.config['TESTING'] = True
        client = app.test_client()
        return app, client

    def tool(self, app, client, name, args=None):
        response = client.post('/api/tools/'+name, json=args or {}, headers={
            'Authorization': 'Bearer '+app.extensions['bank_v1']['tool_token']})
        self.assertEqual(200, response.status_code, response.get_json())
        return response.get_json()['data']

    def report(self, app, client):
        return self.tool(app, client, 'billing_report', {
            'start_date': '2026-06-01', 'end_date': '2026-06-30'})

    def video(self, data):
        return next(c for c in data['candidates'] if c['label'] == '虚构视频会员')

    def snapshot(self):
        with closing(sqlite3.connect(self.root/'insights.sqlite3')) as db:
            return {table: db.execute('SELECT * FROM '+table+' ORDER BY rowid').fetchall()
                    for table in ('metadata', 'candidates', 'notices', 'feedback')}

    def test_default_actual_http_preserves_rules_and_bank(self):
        before = Path(self.bank.db_path).read_bytes()
        app, client = self.start()
        report = self.report(app, client)
        self.assertEqual(analysis_policy(), report['analysis_policy'])
        self.assertFalse(report['report']['category_changes'][0]['significant_increase_candidate'])
        self.assertEqual(2500, report['report']['category_changes'][0]['change_minor'])
        self.assertTrue(self.video(self.tool(app, client, 'check_insights'))['price_increase'])
        self.assertEqual(before, Path(self.bank.db_path).read_bytes())

    def test_actual_competition_http_custom_thresholds_and_all_metadata(self):
        self.write(config(absolute=1000, recurring=26))
        app, client = self.start()
        report = self.report(app, client)
        self.assertTrue(report['report']['category_changes'][0]['significant_increase_candidate'])
        self.assertEqual(report['analysis_policy'], report['report']['analysis_policy'])
        self.assertEqual(26, report['analysis_policy']['recurring_price_increase']['minimum_increase_percent'])
        data = [self.tool(app, client, name) for name in ('check_insights', 'recurring_candidates', 'insight_status')]
        self.assertTrue(all(not self.video(item)['price_increase'] for item in data))
        self.assertTrue(all(item['analysis_policy'] == report['analysis_policy'] for item in data))
        self.assertTrue(all(item['policy_config'] == report['policy_config'] for item in data))

    def test_percent_setting_changes_same_category_data(self):
        self.write(config(category=26, absolute=1000))
        app, client = self.start()
        self.assertFalse(self.report(app, client)['report']['category_changes'][0]['significant_increase_candidate'])
        self.write(config(2, category=25, absolute=1000))
        app, client = self.start()
        self.assertTrue(self.report(app, client)['report']['category_changes'][0]['significant_increase_candidate'])

    def test_process_restart_missing_file_restores_persisted_configuration(self):
        self.write(config(3, absolute=1000, recurring=26))
        app, client = self.start()
        expected = self.report(app, client)['policy_config']
        (self.root/'insight-policy.json').unlink()
        code = """import json,sys
from datetime import datetime
from src.competition_web.app import create_app
a=create_app(sys.argv[1],clock=lambda:datetime.fromisoformat('2026-10-12T12:00:00+08:00'))
s=a.extensions['competition']
print(json.dumps(s.call_tool('recurring_candidates',{})))
"""
        process = subprocess.run([sys.executable, '-X', 'utf8', '-c', code, str(self.root)],
                                 capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(0, process.returncode, process.stderr)
        data = json.loads(process.stdout)['data']
        self.assertEqual(expected, data['policy_config'])
        self.assertFalse(self.video(data)['price_increase'])

    def test_revision_refresh_is_atomic_and_invalidates_old_price_notice(self):
        app, client = self.start()
        facade = app.extensions['competition']
        candidate = self.video(self.tool(app, client, 'check_insights'))
        facade.insights.user_action('candidate_feedback', {'candidate_id':candidate['candidate_id'],
            'expected_version': candidate['version'], 'decision': 'confirm'})
        original = self.tool(app, client, 'check_insights')
        old_notice = next(n for n in original['notifications'] if n['kind']=='PRICE_INCREASE')
        self.assertEqual('active', old_notice['status'])
        bank_before = Path(self.bank.db_path).read_bytes()
        self.write(config(2, recurring=26))
        app, client = self.start()
        # No explicit scan after restart: new metadata already matches refreshed cache.
        data = self.tool(app, client, 'insight_status')
        current = self.video(data)
        self.assertFalse(current['price_increase'])
        self.assertTrue(current['confirmed'])
        self.assertGreater(current['version'], candidate['version'])
        self.assertEqual('superseded', next(n for n in data['notifications'] if n['notification_id']==old_notice['notification_id'])['status'])
        self.assertEqual([], self.tool(app, client, 'check_insights')['new_notifications'])
        self.assertEqual(bank_before, Path(self.bank.db_path).read_bytes())

    def test_same_revision_conflict_and_rollback_leave_database_unchanged(self):
        self.write(config(3))
        self.start()
        before = self.snapshot()
        for value, code in [(config(2), 'INSIGHT_POLICY_ROLLBACK'),
                            (config(3, recurring=11), 'INSIGHT_POLICY_VERSION_CONFLICT')]:
            with self.subTest(code=code):
                self.write(value)
                with self.assertRaises(PolicyError) as raised: self.start()
                self.assertEqual(code, raised.exception.code)
                self.assertEqual(before, self.snapshot())

    def test_still_active_notice_updates_threshold_text_without_duplicate_push(self):
        app, client = self.start()
        insight = app.extensions['competition'].insights
        item = self.video(self.tool(app, client, 'check_insights'))
        insight.user_action('candidate_feedback', {'candidate_id':item['candidate_id'],
            'expected_version':item['version'], 'decision':'confirm'})
        first = self.tool(app, client, 'check_insights')
        old = next(n for n in first['notifications'] if n['kind']=='PRICE_INCREASE')
        self.write(config(2, recurring=20))
        app, client = self.start()
        fresh = self.tool(app, client, 'insight_status')
        notice = next(n for n in fresh['notifications'] if n['notification_id']==old['notification_id'])
        self.assertEqual('active', notice['status'])
        self.assertIn('20%', notice['message'])
        self.assertEqual(old['created_at'], notice['created_at'])
        self.assertEqual(len(first['notifications']), len(fresh['notifications']))
        self.assertEqual([], self.tool(app, client, 'check_insights')['new_notifications'])

    def test_invalid_file_is_not_ignored_when_persisted_config_exists(self):
        self.start()
        before = self.snapshot()
        (self.root/'insight-policy.json').write_text('{bad', encoding='utf-8')
        with self.assertRaises(PolicyError): self.start()
        self.assertEqual(before, self.snapshot())

    def test_invalid_new_runtime_file_does_not_initialize_insight_database(self):
        self.write({'schema_version':1})
        with self.assertRaises(PolicyError): self.start()
        self.assertFalse((self.root/'insights.sqlite3').exists())

    def test_corrupt_persisted_policy_is_not_reset(self):
        self.start()
        with closing(sqlite3.connect(self.root/'insights.sqlite3')) as db:
            db.execute('UPDATE metadata SET value=? WHERE key=?', ('{}', KEY)); db.commit()
        before = self.snapshot()
        with self.assertRaises(PolicyError): self.start()
        self.assertEqual(before, self.snapshot())

    def test_legacy_migration_default_and_first_custom_revision_one(self):
        app, client = self.start()
        self.tool(app, client, 'check_insights')
        with closing(sqlite3.connect(self.root/'insights.sqlite3')) as db:
            db.execute('DELETE FROM metadata WHERE key=?', (KEY,)); db.commit()
        before = self.snapshot()
        app, client = self.start()
        self.assertEqual(before['candidates'], self.snapshot()['candidates'])
        self.assertEqual(before['notices'], self.snapshot()['notices'])
        with closing(sqlite3.connect(self.root/'insights.sqlite3')) as db:
            db.execute('DELETE FROM metadata WHERE key=?', (KEY,)); db.commit()
        self.write(config(1, recurring=26))
        app, client = self.start()
        self.assertFalse(self.video(self.tool(app, client, 'recurring_candidates'))['price_increase'])

    def test_metadata_write_failure_rolls_back_entire_recalculation(self):
        app, client = self.start()
        self.tool(app, client, 'check_insights')
        before = self.snapshot()
        with closing(sqlite3.connect(self.root/'insights.sqlite3')) as db:
            db.execute("CREATE TRIGGER reject_policy BEFORE INSERT ON metadata WHEN NEW.key='analysis_policy_config' BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
            db.commit()
        self.write(config(2, recurring=26))
        with self.assertRaises(sqlite3.IntegrityError): self.start()
        self.assertEqual(before, self.snapshot())

    def test_bank_read_failure_does_not_activate_new_policy(self):
        self.start()
        before = self.snapshot()
        self.write(config(2, recurring=26))
        with patch.object(InsightService, '_rows', side_effect=ValueError('fixture source unavailable')):
            with self.assertRaises(ValueError): self.start()
        self.assertEqual(before, self.snapshot())

    def test_no_hot_reload_and_no_model_configuration_tool(self):
        app, client = self.start()
        self.write(config(2, recurring=26))
        self.assertTrue(self.video(self.tool(app, client, 'check_insights'))['price_increase'])
        token = app.extensions['bank_v1']['tool_token']
        for name, args in [('check_insights', {'policy':config()}), ('set_insight_policy', config())]:
            response=client.post('/api/tools/'+name,json=args,headers={'Authorization':'Bearer '+token})
            self.assertEqual(409, response.status_code)
        self.assertEqual(1, self.tool(app, client, 'insight_status')['policy_config']['revision'])

    def test_new_revision_preserves_ignore_and_corrections(self):
        app, client = self.start()
        insight = app.extensions['competition'].insights
        candidates = self.tool(app, client, 'check_insights')['candidates']
        video = self.video({'candidates':candidates})
        cloud = next(c for c in candidates if c['label']=='虚构云存储代扣')
        insight.user_action('candidate_feedback', {'candidate_id':video['candidate_id'],'expected_version':video['version'],'decision':'ignore'})
        insight.user_action('candidate_feedback', {'candidate_id':cloud['candidate_id'],'expected_version':cloud['version'],'decision':'correct','amount_minor':3500})
        self.write(config(2, recurring=26))
        app, client = self.start()
        data=self.tool(app, client, 'insight_status')
        self.assertEqual('ignored', self.video(data)['status'])
        self.assertEqual(3500,next(c for c in data['candidates'] if c['candidate_id']==cloud['candidate_id'])['amount_minor'])
        self.assertFalse(any(n['status']=='active' and n['candidate_id']==video['candidate_id'] for n in data['notifications']))


class PolicyValidation(unittest.TestCase):
    def test_strict_types_ranges_versions_and_keys(self):
        bad = []
        for key, vals in [('schema_version',[True,0,2,1.0,'1']), ('revision',[False,0,-1,2147483648,1.0,'2'])]:
            for value in vals:
                item=config();item[key]=value;bad.append(item)
        for group, key, values in [
            ('category_increase','minimum_increase_percent',[True,0,1001,10.5,'20']),
            ('category_increase','minimum_absolute_increase_minor',[False,0,100000001,1.0,'10']),
            ('recurring_price_increase','minimum_increase_percent',[True,0,1001,10.0,'10']),
            ('category_increase','previous_debit_must_be_positive',[False,1,'true']),
            ('recurring_price_increase','previous_observations_must_be_fixed',[False,1])]:
            for value in values:
                item=config();item['thresholds'][group][key]=value;bad.append(item)
        for location in [(),('thresholds',),('thresholds','category_increase'),('thresholds','recurring_price_increase')]:
            item=config();target=item
            for key in location:target=target[key]
            target['unknown']=123;bad.append(item)
        bad += [[],None,{}, {'schema_version':1,'revision':1,'thresholds':{}}]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(PolicyError): validate_config(value)
        self.assertEqual(config(category=1,absolute=1,recurring=1000),validate_config(config(category=1,absolute=1,recurring=1000)))

    def test_duplicate_keys_nonfinite_and_malformed_json(self):
        raw=json.dumps(config())
        for value in [raw.replace('"revision": 1','"revision": 1, "revision": 2'),
                      raw.replace('"minimum_increase_percent": 20','"minimum_increase_percent": NaN'),
                      raw.replace('"minimum_increase_percent": 20','"minimum_increase_percent": Infinity'),
                      '[', 'null']:
            with self.subTest(raw=value):
                with self.assertRaises(PolicyError): decode_config(value)

    def test_rule_inputs_and_returned_metadata_are_copied(self):
        item=config()['thresholds']; output=analysis_policy(item)
        output['category_increase']['minimum_increase_percent']=900
        self.assertEqual(20,item['category_increase']['minimum_increase_percent'])
        self.assertEqual(20,analysis_policy()['category_increase']['minimum_increase_percent'])

    def test_invalid_bytes_large_file_and_directory(self):
        from src.bank_insights.policy import read_config
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);p=root/'insight-policy.json'
            for raw in [b'\xff',b' '*16385]:
                p.write_bytes(raw)
                with self.assertRaises(PolicyError):read_config(root)
            p.unlink();p.mkdir()
            with self.assertRaises(PolicyError):read_config(root)


if __name__ == '__main__':
    unittest.main()
