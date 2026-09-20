"""Test helpers: a fake web and a fake GitHub API, so tests never touch the network.

Both are built on httpx.MockTransport, documented at
https://www.python-httpx.org/advanced/transports/#mock-transports
"""

import httpx

PUBLIC_IP = "93.184.216.34"  # a public (globally routable) address used as a stand-in
FAKE_TOKEN = "test-token-not-a-real-secret"  # obviously fake; tests never use a real token

PDF_URL = "https://example.org/papers/study.pdf"
PDF_BYTES = b"%PDF-1.7 fake research paper"


class FakeWeb:
    """Maps URL -> (status, headers, body) and records every URL requested.

    A route may also be an exception instance, which is raised (e.g. a timeout).
    A callable body is called to produce a generator, which httpx streams
    without a Content-Length header.
    """

    def __init__(self, routes: dict | None = None) -> None:
        self.routes = routes or {}
        self.requests: list[str] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        route = self.routes.get(url, (404, {}, b"not found"))
        if isinstance(route, Exception):
            raise route
        status, headers, body = route
        if callable(body):
            body = body()
        return httpx.Response(status, headers=headers, content=body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)


class FakeGitHub:
    """Answers every GitHub API call with one status code and records the requests."""

    def __init__(
        self, status: int = 201, error: Exception | None = None, message: str | None = None
    ) -> None:
        self.status = status
        self.error = error
        self.message = message or f"fake GitHub response {status}"
        self.requests: list[httpx.Request] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, json={"message": self.message})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)
