"""A0 ApiHandler shim → POST /api/plugins/autoresearch/stop."""
from __future__ import annotations

from helpers.api import ApiHandler, Request

from usr.plugins.autoresearch.api import routes


class Stop(ApiHandler):
    @classmethod
    def get_methods(cls) -> list[str]:
        return ["POST"]

    async def process(self, input: dict, request: Request):
        with self.app.test_request_context(
            "/autoresearch/stop", method="POST", json=input
        ):
            response = routes.stop()
        return response
