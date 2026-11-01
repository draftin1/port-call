import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from destinations import Attachment


class HttpTransportTests(unittest.TestCase):
    def test_attach_exchange_through_edge_requiring_application_user_agent(self):
        received = []

        class Edge(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.headers.get('User-Agent', '').startswith('Python-urllib/'):
                    self.send_error(403)
                    return
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append((self.path, self.headers.get('User-Agent'), body))
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(dict(state=state,
                    plugin_attach_token='synthetic-ticket',
                    resource_id='dataset:owner:data:node')).encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Edge)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            auth = Attachment({})
            # Only the local test fixture uses HTTP; production requires HTTPS.
            auth.base = f'http://127.0.0.1:{server.server_port}'
            auth.start('http://localhost:8090/saving/callback')
            state, verifier, browser, _ = auth.pending
            auth.finish('synthetic-code', state, browser)
            self.assertTrue(auth.status()['connected'])
            self.assertEqual(received, [('/connect/exchange', 'Roost/1.0',
                dict(code='synthetic-code', app_id='roost', code_verifier=verifier))])
            self.assertNotIn('synthetic-ticket', json.dumps(auth.status()))
        finally:
            server.shutdown()
            server.server_close()
            worker.join()
