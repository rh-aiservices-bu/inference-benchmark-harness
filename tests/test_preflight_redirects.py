"""Preflight must not follow gateway/login redirects or forward bearer credentials."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest

from bench.preflight import fetch


class PreflightRedirectTests(unittest.TestCase):
    def setUp(self):
        self.observed = []
        observed = self.observed

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                observed.append((self.server.server_port, self.path,
                                 self.headers.get('Authorization')))
                if self.path.startswith('/redirect/'):
                    code = int(self.path.rsplit('/', 1)[1])
                    self.send_response(code)
                    self.send_header('Location', self.server.redirect_target)
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"data":[{"id":"fixture-model"}]}')

            def log_message(self, *args):
                pass

        self.servers = []
        for _ in range(2):
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.servers.append(server)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.origin, self.other = self.servers
        self.url = f'http://127.0.0.1:{self.origin.server_port}'
        self.headers = {'Authorization': 'Bearer fixture-secret'}

    def test_direct_model_read_preserves_authentication(self):
        body, attempts = fetch(self.url + '/models', self.headers)
        self.assertIn('fixture-model', body)
        self.assertEqual(attempts, 1)
        self.assertEqual(self.observed, [
            (self.origin.server_port, '/models', 'Bearer fixture-secret')])

    def test_cross_origin_redirect_never_receives_bearer(self):
        self.origin.redirect_target = f'http://127.0.0.1:{self.other.server_port}/models'
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                self.observed.clear()
                with self.assertRaisesRegex(ValueError, f'HTTP {code}'):
                    fetch(self.url + f'/redirect/{code}', self.headers)
                self.assertEqual(self.observed, [
                    (self.origin.server_port, f'/redirect/{code}', 'Bearer fixture-secret')])

    def test_same_origin_login_redirect_fails_instead_of_looking_ready(self):
        self.origin.redirect_target = self.url + '/login'
        with self.assertRaisesRegex(ValueError, 'HTTP 302'):
            fetch(self.url + '/redirect/302', self.headers)
        self.assertEqual(self.observed, [
            (self.origin.server_port, '/redirect/302', 'Bearer fixture-secret')])


if __name__ == '__main__':
    unittest.main()
