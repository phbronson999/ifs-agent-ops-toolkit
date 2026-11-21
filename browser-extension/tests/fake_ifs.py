"""A stand-in for the IFS web client: one page plus projection endpoints (local browser test only)."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = b"""<!doctype html><html><head><title>Fake IFS</title></head><body><h1>Fake IFS</h1>
<script>
const svc = '/main/ifsapplications/projection/v1/OrderHandling.svc';
window.header = () => fetch(svc + '/OrderSet', {method: 'POST', headers: {'Content-Type': 'application/json',
  'Authorization': 'Bearer secret-token'}, body: JSON.stringify({CustomerNo: '5832'})}).then(r => r.json());
window.line = () => fetch(svc + "/OrderSet(OrderNo='ORD-1')/LinesArray", {method: 'POST',
  headers: {'Content-Type': 'application/json'}, body: JSON.stringify({OrderNo: 'ORD-1', LineNo: '1'})}).then(r => r.json());
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, status, body, ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Set-Cookie", "session=abc")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/main/ifsapplications/web"):
            return self._send(200, PAGE, "text/html")
        return self._send(404, b"{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path.endswith("/OrderSet"):
            return self._send(201, json.dumps({"@odata.etag": 'W/"1"', "OrderNo": "ORD-1", **body}).encode())
        return self._send(201, json.dumps(body).encode())


ThreadingHTTPServer(("127.0.0.1", 8766), H).serve_forever()
