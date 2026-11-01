import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import threading
from src.competition_web.runtime_status import dependency_status,RuntimeStatus
class DependencyTests(unittest.TestCase):
 def test_mismatch_and_missing_block_ready(self):
  with tempfile.TemporaryDirectory() as d:
   self.assertFalse(dependency_status(d)['available'])
   Path(d,'package.json').write_text(json.dumps({'version':'0.2.1-rc.2'}))
   self.assertFalse(dependency_status(d)['available'])
   worker=SimpleNamespace(thread=SimpleNamespace(is_alive=lambda:True),stop_event=threading.Event())
   with patch.dict('os.environ',{'DSH_PACKAGE_PATH':d}),patch('src.competition_web.runtime_status.budget_status',return_value={'available':True}):
    state=RuntimeStatus(mode='MODEL',worker=worker,budget_path='ignored',build_id='test',capabilities={}).view()
    self.assertFalse(state['ready_for_requests']);self.assertIn('版本不兼容',state['reason'])
   Path(d,'package.json').write_text(json.dumps({'version':'0.1.1-rc.2'}))
   self.assertTrue(dependency_status(d)['available'])
if __name__=='__main__':unittest.main()
