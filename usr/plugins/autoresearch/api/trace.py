"""A0 ApiHandler shim → GET /api/plugins/autoresearch/trace?run_id=…&exp=…

Streams SSE — must return a Flask Response directly.
"""
from __future__ import annotations

from helpers.api import ApiHandler, Request

from usr.plugins.autoresearch.api import routes


class Trace(ApiHandler):
    @classmethod
    def get_methods(cls) -> list[str]:
        return ["GET"]

    @classmethod
    def requires_csrf(cls) -> bool:
        return False  # SSE GET — clients use EventSource which can't send CSRF

    async def process(self, input: dict, request: Request):
        run_id = request.args.get("run_id") or ""
        exp = request.args.get("exp") or ""
        with self.app.test_request_context(
            f"/autoresearch/trace?run_id={run_id}&exp={exp}", method="GET"
        ):
            response = routes.trace()
        return response
