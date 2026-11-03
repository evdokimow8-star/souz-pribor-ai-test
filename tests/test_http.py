import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

from pribor.llm import ARGON_MODEL, Config, generate
from pribor.server import create_server
from tests.test_domain import draft, product
from tests.test_llm import Response, envelope


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

    def test_binary_assets_use_correct_mime_and_keep_security_headers(self):
        cases = [
            ("/assets/manrope-cyrillic.woff2", "font/woff2", b"wOF2"),
            ("/assets/manrope-latin.woff2", "font/woff2", b"wOF2"),
            ("/assets/testo-510.jpg", "image/jpeg", b"\xff\xd8\xff"),
            ("/assets/testo-511.jpg", "image/jpeg", b"\xff\xd8\xff"),
        ]
        for path, mime, signature in cases:
            with self.subTest(path=path), self.request(path) as r:
                self.assertEqual(r.code, 200)
                raw = r.read()
                self.assertEqual(r.headers["Content-Type"], mime)
                self.assertTrue(raw.startswith(signature))
                self.assertEqual(int(r.headers["Content-Length"]), len(raw))
                self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
                self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
                self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])

    def test_static_asset_allowlist_rejects_unlisted_files_and_traversal(self):
        for path in ["/assets/", "/assets/unknown.jpg", "/assets/app.js",
                     "/assets/../app.js", "/assets/%2e%2e/.env", "/assets/testo-510.jpg/extra"]:
            with self.subTest(path=path), self.request(path) as r:
                self.assertEqual(r.code, 404)
                self.assertEqual(json.load(r)["error"]["code"], "not_found")

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

    def test_local_argon_retries_and_reports_cost_without_exposing_rejected_draft(self):
        bad = draft()
        bad["commercial"]["text"] = "Работает с Bluetooth."
        transport = Mock()
        transport.open.side_effect = [Response(envelope(bad)), Response(envelope(bad))]
        raw = json.dumps(product().as_dict(), ensure_ascii=False).encode("utf-8")
        with create_server(0, Config("fake", ARGON_MODEL)) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/generate",
                data=raw, headers={"Content-Type": "application/json"})
            try:
                with patch("pribor.server.generate", side_effect=lambda product, config, repair:
                           generate(product, config, transport, repair=repair)):
                    with self.assertRaises(urllib.error.HTTPError) as response:
                        urllib.request.urlopen(request, timeout=3)
                    with response.exception as error:
                        self.assertEqual(error.code, 422)
                        payload = json.load(error)["error"]
            finally:
                server.shutdown()
                worker.join(timeout=3)
        self.assertEqual(transport.open.call_count, 2)
        self.assertEqual(payload["attempt_count"], 2)
        self.assertEqual(payload["reported_cost_usd"], 0.00246)
        self.assertNotIn("rejected_draft", payload)


if __name__ == "__main__":
    unittest.main()
