import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
import urllib.request
import urllib.error

from pribor.budget import Budget
from pribor.llm import Config
from pribor.server import create_server


class PublicHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory()
        cls.budget = Budget(Path(cls.temp.name)/'budget.sqlite','0.50','0.017678')
        cls.server = create_server(0,Config(),'https://demo.example',cls.budget)
        cls.thread = threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(timeout=3)
        cls.temp.cleanup()

    def request(self,path,body=None,origin=None):
        headers={'Host':'demo.example','Content-Type':'application/json'}
        if origin:headers['Origin']=origin
        try:return urllib.request.urlopen(urllib.request.Request(self.url+path,data=body,headers=headers),timeout=3)
        except urllib.error.HTTPError as error:return error

    def test_public_config_reports_budget_without_key(self):
        with self.request('/api/config') as response:data=json.load(response)
        self.assertTrue(data['public_demo'])
        self.assertFalse(data['ready'])
        self.assertEqual(data['budget']['remaining_usd'],0.482322)

    def test_public_origin_and_saved_examples(self):
        with self.request('/api/examples/testo-510',origin='https://demo.example') as response:
            self.assertEqual(response.code,200)
        self.assertEqual(self.budget.snapshot()['spent_usd'],0.017678)

    def test_wrong_origin_rejected(self):
        with self.request('/api/generate',b'{}',origin='https://other.example') as response:
            self.assertEqual(response.code,403)

    def test_public_body_limit(self):
        with self.request('/api/generate',b' '*16001,origin='https://demo.example') as response:
            self.assertEqual(response.code,413)
