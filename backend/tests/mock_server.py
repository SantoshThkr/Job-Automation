"""Local mock job portals (Naukri, LinkedIn, Instahyre) and ATS pages used by tests and the demo.

    python -m tests.mock_server --port 8765
"""

from __future__ import annotations

import argparse
import re
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SITES = Path(__file__).parent / "fixtures" / "sites"

# URL shapes of the real portals mapped onto static mock files
ROUTES: list[tuple[str, str]] = [
    (r"^/naukri/nlogin/login", "/naukri/login.html"),
    (r"^/naukri/mnjuser/homepage", "/naukri/homepage.html"),
    (r"^/naukri/job-listings-.*-(\d+)$", "/naukri/job-{0}.html"),
    (r"^/naukri/[a-z0-9-]+-jobs(?:-in-[a-z0-9-]+?)?(?:-(\d+))?$", "/naukri/search-{0}.html"),
    (r"^/linkedin/login", "/linkedin/login.html"),
    (r"^/linkedin/feed/?$", "/linkedin/feed.html"),
    (r"^/linkedin/jobs/search/?$", "/linkedin/search.html"),
    (r"^/linkedin/jobs/view/(\d+)/?$", "/linkedin/view-{0}.html"),
    (r"^/instahyre/login/?$", "/instahyre/login.html"),
    (r"^/instahyre/candidate/opportunities/?$", "/instahyre/opportunities.html"),
    (r"^/instahyre/search-jobs/?$", "/instahyre/opportunities.html"),
    (r"^/instahyre/job-(\d+)-[a-z0-9-]+/?$", "/instahyre/job-{0}.html"),
]


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401
        pass

    def do_GET(self):
        path, _, query = self.path.partition("?")
        for rx, target in ROUTES:
            m = re.match(rx, path)
            if m:
                groups = [g for g in m.groups()]
                if "search-{0}" in target:
                    page = groups[0] if groups and groups[0] else "1"
                    candidate = target.format(page)
                    if not (SITES / candidate.lstrip("/")).is_file():
                        candidate = "/naukri/search-empty.html"
                    self.path = candidate
                else:
                    self.path = target.format(*(g or "" for g in groups))
                self.path += ("?" + query) if query else ""
                break
        return super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_POST(self):  # forms in mock sites post here; reply with a confirmation page
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        target = self.path.split("?")[0]
        thanks = SITES / target.lstrip("/")
        if thanks.is_file():
            body = thanks.read_bytes()
        else:
            body = b"<html><body><h1>Thank you for applying!</h1><p>Your application has been submitted.</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)




def serve(port: int = 0, host: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer((host, port), partial(_QuietHandler, directory=str(SITES)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://{host}:{server.server_address[1]}"


def main() -> None:
    ap = argparse.ArgumentParser(description="Serve the mock portals")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    server, url = serve(a.port)
    print(f"Mock portals at {url}/naukri  {url}/linkedin  {url}/instahyre  (Ctrl+C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
