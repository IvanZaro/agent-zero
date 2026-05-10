"""A0 ApiHandler shim → GET /api/plugins/autoresearch/status?run_id=…"""
from __future__ import annotations

from helpers.api import ApiHandler, Request

from usr.plugins.autoresearch.api import routes


class Status(ApiHandler):
    @classmethod
    def get_methods(cls) -> list[str]:
        return ["GET", "POST"]

    @classmethod
    def requires_csrf(cls) -> bool:
        return False  # GET endpoint

    async def process(self, input: dict, request: Request):
        run_id = request.args.get("run_id") or input.get("run_id", "")
        with self.app.test_request_context(
            f"/autoresearch/status?run_id={run_id}", method="GET"
        ):
            response = routes.status()
        return response
