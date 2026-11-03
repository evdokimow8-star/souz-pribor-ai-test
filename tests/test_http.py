import json
import threading
import unittest
import urllib.error
import urllib.request

from pribor.llm import Config
from pribor.server import create_server


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server(0, Config())
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def request(self, path, body=None, headers=None):
        req = urllib.request.Request(self.base + path, data=body,
                                     headers=headers or {})
        try:
            return urllib.request.urlopen(req, timeout=3)
        except urllib.error.HTTPError as error:
            return error

    def test_config_does_not_expose_key(self):
        with self.request("/api/config") as r:
            data = json.load(r)
            self.assertFalse(data["ready"])
            self.assertNotIn("api_key", data)

    def test_page_and_security_headers(self):
        with self.request("/") as r:
            self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
            self.assertEqual(r.headers["Cache-Control"], "no-store")
            self.assertIn("lang=\"ru\"", r.read().decode())

    def test_foreign_origin_blocked(self):
        with self.request("/api/generate", b'{}', {"Content-Type": "application/json", "Origin": "https://evil.test"}) as r:
            self.assertEqual(r.code, 403)

    def test_foreign_host_blocked(self):
        with self.request("/", headers={"Host": "evil.test"}) as r:
            self.assertEqual(r.code, 403)

    def test_wrong_content_type_blocked(self):
        with self.request("/api/generate", b'{}') as r:
            self.assertEqual(r.code, 415)

    def test_duplicate_keys_rejected(self):
        with self.request("/api/generate", b'{"name":"a","name":"b"}', {"Content-Type": "application/json"}) as r:
            self.assertEqual(r.code, 422)
            self.assertEqual(json.load(r)["error"]["code"], "invalid_json")

    def test_no_api_configuration(self):
        raw = json.dumps({"name": "Прибор", "description": "Измеряет давление"}).encode()
        with self.request("/api/generate", raw, {"Content-Type": "application/json"}) as r:
            self.assertEqual(r.code, 503)

    def test_traversal_and_unknown_example(self):
        for path in ["/../.env", "/api/examples/../../.env", "/api/examples/unknown"]:
            with self.subTest(path=path), self.request(path) as r:
                self.assertEqual(r.code, 404)

    def test_payload_size(self):
        with self.request("/api/generate", b' ' * 32769, {"Content-Type": "application/json"}) as r:
            self.assertEqual(r.code, 413)


if __name__ == "__main__":
    unittest.main()
