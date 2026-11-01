import tempfile, unittest
from datetime import datetime, timezone
from src.competition_web.app import create_app
class ChatFlowTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.app=create_app(self.tmp.name,clock=lambda:datetime(2026,10,20,7,tzinfo=timezone.utc),goals_enabled=True,goal_followup=True)
  self.client=self.app.test_client();self.client.get('/')
  self.app.extensions['runtime_status']=lambda:{'managed':True,'mode':'MODEL','ready_for_requests':True}
  state=self.client.get('/api/state').json
  self.headers={'Origin':'http://localhost','X-CSRF-Token':state['csrf_token']}
 def tearDown(self):self.tmp.cleanup()
 def test_chat_entry_and_preserved_workbench(self):
  html=self.client.get('/').text
  self.assertIn('chat.js',html);self.assertNotIn('id="total"',html)
  self.assertIn('id="confirm-dialog"',html);self.assertIn('id="goal-dialog"',html)
  self.assertIn('id="total"',self.client.get('/workbench').text)
 def test_manual_check_idempotency_and_scheduled_day_independent(self):
  post=lambda body:self.client.post('/api/actions/due_check',json=body,headers=self.headers)
  first=post({'idempotency_key':'one'});self.assertEqual(200,first.status_code)
  self.assertEqual(first.json,post({'idempotency_key':'one'}).json)
  second=post({'idempotency_key':'two'});self.assertNotEqual(first.json['event_id'],second.json['event_id'])
  daily=post({});self.assertEqual(daily.json,post({}).json)
  self.assertNotEqual(daily.json['event_id'],first.json['event_id'])
  self.assertEqual(409,post({'date':'2026-10-21'}).status_code)
  self.app.extensions['runtime_status']=lambda:{'managed':True,'ready_for_requests':False,'reason':'dependency missing'}
  self.assertEqual(503,post({'idempotency_key':'three'}).status_code)
if __name__=='__main__':unittest.main()
