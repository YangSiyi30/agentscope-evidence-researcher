"""Real stdio fixture: all HTTP and search data are synthetic, never network."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

# Direct file entry point; make the project-owned scripts namespace importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ddgs.exceptions import TimeoutException  # noqa: E402
from ddgs.http_client import HttpClient  # noqa: E402

from as_researcher import mcp_server  # noqa: E402
from as_researcher.core import Source  # noqa: E402
from as_researcher.search import SearchBackendError  # noqa: E402
from scripts.search_diagnostic_server import main  # noqa: E402

SECRET = "fixture-secret-never-persist"


def fake_http(client, method, url, **kwargs):
    if "timeout" in url:
        raise TimeoutException(SECRET)
    return SimpleNamespace(status_code=200, content=SECRET.encode())


async def fake_search(query, limit):
    try:
        await asyncio.to_thread(HttpClient.request, object(), "GET", "https://example.org/" + query)
    except TimeoutException:
        # Mimic the installed provider's ambiguous error after a worker failure.
        raise SearchBackendError("search_no_results_or_unavailable") from None
    return [Source(SECRET, "https://example.org/?secret=" + SECRET, SECRET)]


HttpClient.request = fake_http
mcp_server.make_search = lambda provider: fake_search

if __name__ == "__main__":
    main()
