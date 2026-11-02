"""The token-bearing page must not be cacheable or frameable.

`/?key=` is the URL a browser loads. no-store, no-referrer, and the exact
page CSP are what keep that URL and the inline page from being cached,
leaked, or framed. Stdlib only.
"""

import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

import server


TOKEN = "unit-test-key"
PAGE = b"<html>queue marker</html>\n"
PAGE_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; connect-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def http_request(host, port, method, path):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        payload = resp.read()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, payload, hdrs
    finally:
        conn.close()


class PageCacheAndCspTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.httpd = start_server()
        cls.host, cls.port = cls.httpd.server_address

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=self.tmp.name)
        for name in (
            "QUESTIONS_FILE", "ANSWERS_FILE", "LOG_FILE", "LOCK_FILE",
            "INDEX_FILE", "TOKEN",
        ):
            self.addCleanup(setattr, server, name, getattr(server, name))
        server.QUESTIONS_FILE = os.path.join(self.dir, "questions.json")
        server.ANSWERS_FILE = os.path.join(self.dir, "answers.jsonl")
        server.LOG_FILE = os.path.join(self.dir, "server.log")
        server.LOCK_FILE = os.path.join(self.dir, ".queue.lock")
        server.INDEX_FILE = os.path.join(self.dir, "index.html")
        server.TOKEN = TOKEN
        with open(server.INDEX_FILE, "wb") as fh:
            fh.write(PAGE)
        with open(server.QUESTIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump([{"id": "q1", "question": "SENTINEL-QUESTION", "status": "open"}], fh)

    def request(self, method, path):
        return http_request(self.host, self.port, method, path)

    def assert_page_headers(self, hdrs, body_len):
        self.assertEqual(hdrs["cache-control"], "no-store")
        self.assertEqual(hdrs["referrer-policy"], "no-referrer")
        self.assertEqual(hdrs["x-frame-options"], "DENY")
        self.assertEqual(hdrs["x-content-type-options"], "nosniff")
        self.assertEqual(hdrs["content-security-policy"], PAGE_CSP)
        self.assertNotIn("unsafe-eval", hdrs["content-security-policy"])
        self.assertTrue(hdrs["content-type"].startswith("text/html"))
        self.assertEqual(hdrs["content-length"], str(body_len))
        self.assertNotIn("connection", hdrs)

    def test_the_token_page_is_uncacheable_and_locked_to_its_csp(self):
        for path in ("/", "/index.html"):
            authed = path + "?key=" + TOKEN
            status, body, hdrs = self.request("GET", authed)
            self.assertEqual(status, 200, path)
            self.assertEqual(body, PAGE)
            self.assertNotIn(TOKEN.encode(), body)
            self.assert_page_headers(hdrs, len(PAGE))

            head_status, head_body, head_hdrs = self.request("HEAD", authed)
            self.assertEqual(head_status, 200, path)
            self.assertEqual(head_body, b"")
            self.assert_page_headers(head_hdrs, len(PAGE))

        status, body, hdrs = self.request("GET", "/?key=wrong")
        self.assertEqual(status, 403)
        self.assertEqual(hdrs.get("connection"), "close")
        self.assertNotIn(b"queue marker", body)
        self.assertNotIn("content-security-policy", hdrs)

        status, body, hdrs = self.request("GET", "/api/questions?key=" + TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["cache-control"], "no-store")
        self.assertNotIn("content-security-policy", hdrs)
        self.assertNotIn(b"queue marker", body)

        with open(server.LOG_FILE, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("key=REDACTED", log)
        self.assertNotIn(TOKEN, log)
