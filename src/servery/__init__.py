"""servery — a zero-dependency, pure-Python HTTP file server.

A batteries-included ``python -m http.server``: rich sortable directory
listings, range/resumable downloads, conditional requests, basic auth,
upload, and HTTPS — built with nothing but the Python standard library.

Public API::

    from servery import Config, serve
    serve(Config.create("./public", port=8000))
"""

from servery._version import __version__
from servery.config import Config
from servery.handler import ServeryHandler
from servery.server import ServeryHTTPServer, make_server, serve, server_url

__all__ = [
    "Config",
    "ServeryHTTPServer",
    "ServeryHandler",
    "__version__",
    "make_server",
    "serve",
    "server_url",
]
