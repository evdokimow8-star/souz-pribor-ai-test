import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import Mock

from pribor.budget import Budget, RESERVE
from pribor.domain import AppError
from pribor.llm import Config
from tests.test_domain import product, draft
from tests.test_llm import Response, envelope


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name)/'budget.sqlite'
        self.budget = Budget(self.path, '0.50', '0.017678')

    def tearDown(self):
        self.temp.cleanup()

    def test_initial_cost_and_reservation_survive_restart(self):
        self.budget.reserve('127.0.0.1')
        second = Budget(self.path, '0.50', '0.017678')
        self.assertEqual(second.snapshot()['held_usd'],0.1)
        self.assertEqual(second.snapshot()['spent_usd'],0.017678)

    def test_cannot_raise_or_reset_budget(self):
        for cap, initial in [('1','0.017678'),('0.50','0')]:
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                Budget(self.path,cap,initial)

    def test_concurrent_reservations_stay_within_cap(self):
        def reserve(i):
            try:return self.budget.reserve(str(i))
            except AppError:return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            accepted = [x for x in pool.map(reserve,range(16)) if x]
        self.assertEqual(len(accepted),4)
        self.assertLessEqual(self.budget.snapshot()['held_usd']+self.budget.snapshot()['spent_usd'],0.5)

    def test_per_client_limit(self):
        for _ in range(3):
            self.budget.settle(self.budget.reserve('client'),0.001)
        with self.assertRaises(AppError) as error:
            self.budget.reserve('client')
        self.assertEqual(error.exception.code,'demo_rate_limit')

    def test_unknown_cost_keeps_reservation(self):
        ticket=self.budget.reserve('client')
        self.budget.settle(ticket,None)
        self.assertEqual(self.budget.snapshot()['held_usd'],0.1)

    def test_infinite_cost_keeps_reservations_and_explains_block(self):
        config = Config('fake', 'google/gemini-3-flash-preview')
        transport = Mock()
        body = envelope().replace(b'"cost": 0.00123', b'"cost": 1e999')
        transport.open.side_effect = lambda *args, **kwargs: Response(body)
        for index in range(4):
            result = self.budget.generate(product(), config, str(index), transport)
            self.assertIsNone(result['generation']['reported_cost_usd'])
        snapshot = self.budget.snapshot()
        self.assertEqual(snapshot['held_attempts'], 4)
        self.assertEqual(snapshot['held_usd'], 0.4)
        self.assertEqual(snapshot['spent_usd'], 0.017678)
        self.assertFalse(snapshot['can_generate'])
        self.assertEqual(snapshot['unavailable_reason'], 'unconfirmed_cost')
        with self.assertRaises(AppError) as error:
            self.budget.generate(product(), config, 'another', transport)
        self.assertEqual(error.exception.code, 'demo_pending_cost')
        self.assertEqual(transport.open.call_count, 4)

    def test_known_cost_is_rounded_up_and_releases_rest(self):
        ticket=self.budget.reserve('client')
        self.budget.settle(ticket,0.0010001)
        self.assertEqual(self.budget.snapshot()['held_usd'],0)
        self.assertEqual(self.budget.snapshot()['spent_usd'],0.018679)

    def test_real_transport_contract_with_mock_and_price_caps(self):
        transport=Mock();transport.open.return_value=Response(envelope())
        result=self.budget.generate(product(),Config('fake','google/gemini-3-flash-preview'),'client',transport)
        self.assertEqual(result['generation']['mode'],'live_api')
        request=transport.open.call_args.args[0]
        self.assertEqual(json.loads(request.data)['provider']['max_price'],{'prompt':1,'completion':3})
        self.assertEqual(self.budget.snapshot()['held_usd'],0)

    def test_rejected_text_still_accounts_for_paid_answer(self):
        bad=draft();bad['commercial']['text']='Работает с Bluetooth.'
        transport=Mock();transport.open.return_value=Response(envelope(bad))
        with self.assertRaises(AppError):
            self.budget.generate(product(),Config('fake','google/gemini-3-flash-preview'),'client',transport)
        self.assertAlmostEqual(self.budget.snapshot()['spent_usd'],0.017678+0.00123)

    def test_timeout_keeps_reservation_no_retry(self):
        transport=Mock();transport.open.side_effect=TimeoutError()
        with self.assertRaises(AppError):
            self.budget.generate(product(),Config('fake','google/gemini-3-flash-preview'),'client',transport)
        self.assertEqual(self.budget.snapshot()['held_usd'],0.1)
        transport.open.assert_called_once()


if __name__=='__main__':
    unittest.main()
