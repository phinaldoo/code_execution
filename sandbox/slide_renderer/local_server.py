from __future__ import annotations

import threading
from contextlib import AbstractContextManager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class _QuietHandler(SimpleHTTPRequestHandler):
    """HTTP request handler that suppresses access logs."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return


class LocalStaticServer(AbstractContextManager["LocalStaticServer"]):
    """Context manager for serving static files from a directory."""

    def __init__(self, root_dir: Path) -> None:
        self._root_dir = root_dir
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.base_url: str = ""

    def __enter__(self) -> "LocalStaticServer":
        """Start the local static server."""
        handler = partial(_QuietHandler, directory=str(self._root_dir))
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self._server.request_queue_size = 16
        host, port = self._server.server_address
        self.base_url = f"http://{host}:{port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Stop the local static server."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)

