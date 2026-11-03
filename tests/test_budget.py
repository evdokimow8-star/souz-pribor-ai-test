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

    def test_operator_series_shares_dollar_budget_with_public_demo(self):
        path = Path(self.temp.name) / "dollar.sqlite"
        public = Budget(path, "1", "0.017678")
        operator = Budget(path, "1", "0.017678", requests_per_window=10)
        for _ in range(10):
            operator.settle(operator.reserve("operator"), 0.003)
        with self.assertRaises(AppError) as error:
            operator.reserve("operator")
        self.assertEqual(error.exception.code, "demo_rate_limit")
        self.assertEqual(public.snapshot()["limit_usd"], 1)
        self.assertEqual(public.snapshot()["spent_usd"], 0.047678)
        with self.assertRaises(ValueError):
            Budget(Path(self.temp.name) / "too-large.sqlite", "1.01")

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

    def test_gemini_38_uses_its_price_cap_and_preserves_budget(self):
        transport = Mock()
        transport.open.return_value = Response(envelope())
        model = "google/gemini-3.8-flash"
        card = self.budget.generate(product(), Config("fake", model), "client", transport)
        request = transport.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], model)
        self.assertEqual(payload["provider"]["max_price"], {"prompt": 1, "completion": 3.75})
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertEqual(card["generation"]["model"], model)
        self.assertEqual(self.budget.snapshot()["limit_usd"], 0.5)
        self.assertEqual(self.budget.snapshot()["held_usd"], 0)
        transport.open.assert_called_once()

    def test_argon_repairs_rejected_flash_and_charges_both_attempts(self):
        config = Config("fake", "google/gemini-3.8-flash")
        bad = draft()
        bad["commercial"]["text"] = "Работает с Bluetooth."
        transport = Mock()
        transport.open.side_effect = [Response(envelope(bad)), Response(envelope())]
        card = self.budget.generate(product(), config, "client", transport)
        first, repair = [json.loads(call.args[0].data) for call in transport.open.call_args_list]
        self.assertEqual(first["model"], config.model)
        self.assertEqual(repair["model"], "google/gemini-4-argon")
        self.assertEqual(repair["provider"]["max_price"], {"prompt": 2, "completion": 10})
        self.assertEqual(repair["response_format"], first["response_format"])
        self.assertEqual(repair["messages"][1], first["messages"][1])
        self.assertIn("commercial", repair["messages"][2]["content"])
        self.assertNotIn("Bluetooth", repair["messages"][2]["content"])
        self.assertEqual(card["generation"]["model"], repair["model"])
        self.assertEqual(card["generation"]["first_model"], first["model"])
        self.assertEqual(card["generation"]["reported_cost_usd"], 0.00246)
        self.assertEqual(self.budget.snapshot()["spent_usd"], 0.020138)
        for _ in range(2):
            self.budget.settle(self.budget.reserve("client"), 0)
        with self.assertRaisesRegex(AppError, "Лимит генераций"):
            self.budget.reserve("client")

    def test_rejected_argon_is_charged_without_another_attempt(self):
        bad = draft()
        bad["commercial"]["text"] = "Работает с Bluetooth."
        transport = Mock()
        transport.open.side_effect = [Response(envelope(bad)), Response(envelope(bad))]
        with self.assertRaises(AppError) as error:
            self.budget.generate(product(), Config("fake", "google/gemini-3.8-flash"), "client", transport)
        self.assertEqual(error.exception.code, "draft_rejected")
        self.assertEqual(transport.open.call_count, 2)
        self.assertEqual(self.budget.snapshot()["spent_usd"], 0.020138)

    def test_repair_needs_its_own_budget_reservation(self):
        for i in range(3):
            self.budget.reserve(str(i))
        bad = draft()
        bad["commercial"]["text"] = "Работает с Bluetooth."
        transport = Mock()
        response = envelope(bad).replace(b'"cost": 0.00123', b'"cost": 0.09')
        transport.open.return_value = Response(response)
        with self.assertRaises(AppError) as error:
            self.budget.generate(product(), Config("fake", "google/gemini-3.8-flash"), "client", transport)
        self.assertEqual(error.exception.code, "draft_rejected")
        transport.open.assert_called_once()
        self.assertEqual(self.budget.snapshot()["spent_usd"], 0.107678)
        self.assertEqual(self.budget.snapshot()["held_usd"], 0.3)

    def test_timeout_keeps_reservation_no_retry(self):
        transport=Mock();transport.open.side_effect=TimeoutError()
        with self.assertRaises(AppError):
            self.budget.generate(product(),Config('fake','google/gemini-3.8-flash'),'client',transport)
        self.assertEqual(self.budget.snapshot()['held_usd'],0.1)
        transport.open.assert_called_once()


if __name__=='__main__':
    unittest.main()
