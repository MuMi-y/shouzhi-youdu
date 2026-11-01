"""Public status and package references agree without reading model credentials."""
import unittest
from pathlib import Path
from src.bank_v1_web.model_route import model_identity, model_route
from src.competition_web.runtime_status import RuntimeStatus
from scripts.competition_package import FILES
from scripts.competition_acceptance_live import source_hashes


class ModelRouteTests(unittest.TestCase):
    def test_status_reports_configuration_not_provider_probe(self):
        status = RuntimeStatus(mode='NO_MODEL',worker=None,budget_path=None,build_id='test',capabilities={}).view()
        self.assertEqual('deepseek-v4.1-flash',status['model'])
        self.assertEqual(model_identity()['route'],status['route'])
        self.assertEqual('NOT_PROBED_BY_STATUS_QUERY',status['provider_status'])
        self.assertFalse(status['ready_for_requests'])

    def test_package_and_source_freeze_include_route(self):
        frozen = {Path(name).as_posix() for name in source_hashes()}
        for name in ['config/model_route.json','scripts/model_route.cjs']:
            self.assertIn(name,FILES)
            self.assertIn(name,frozen)
        self.assertEqual({'provider','model','baseURL','apiKeyEnv'},set(model_route()))
