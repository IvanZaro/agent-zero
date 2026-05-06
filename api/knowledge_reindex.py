import os

from helpers.api import ApiHandler, Request, Response
from helpers.print_style import PrintStyle
from plugins._memory.helpers.memory import Memory


class KnowledgeReindex(ApiHandler):
    """
    POST endpoint that triggers an incremental knowledge reindex for a given
    memory_subdir. Wired for the reindex_watcher sidecar (see plan §6) so
    Obsidian edits propagate to the FAISS index within ~30-60s instead of
    waiting for memory init / chat creation.

    Auth model: shared secret via X-Reindex-Secret header, matched against the
    REINDEX_SECRET env var. Loopback isn't usable because the watcher runs in
    a sibling container (Docker bridge IP, not 127.0.0.1) — verified via
    helpers/network.py:is_loopback_address. Fail-closed: if REINDEX_SECRET is
    unset, every request is rejected (no accidental open endpoint).

    Internally uses Memory.get_by_subdir(..., preload_knowledge=True), which
    mirrors the call at plugins/_memory/helpers/memory.py:88
    (preload_knowledge(log_item, knowledge_subdirs, memory_subdir)) and
    handles knowledge_subdirs resolution via
    get_knowledge_subdirs_by_memory_subdir at memory.py:629.
    """

    @classmethod
    def requires_auth(cls) -> bool:
        return False

    @classmethod
    def requires_loopback(cls) -> bool:
        return False

    @classmethod
    def requires_csrf(cls) -> bool:
        return False

    @classmethod
    def requires_api_key(cls) -> bool:
        return False

    @classmethod
    def get_methods(cls) -> list[str]:
        return ["POST"]

    async def process(self, input: dict, request: Request) -> dict | Response:
        expected = os.environ.get("REINDEX_SECRET", "")
        provided = request.headers.get("X-Reindex-Secret", "")
        if not expected or provided != expected:
            return Response("Forbidden", 403)

        memory_subdir: str = input.get("memory_subdir", "default")

        try:
            # Drop any cached in-memory FAISS handle so reindex starts from
            # a fresh load + diff against knowledge_import.json on disk.
            if memory_subdir in Memory.index:
                del Memory.index[memory_subdir]

            # log_item=None is supported (signature: LogItem | None at memory.py:259-261).
            wrap = await Memory.get_by_subdir(
                memory_subdir=memory_subdir,
                log_item=None,
                preload_knowledge=True,
            )

            indexed_count = len(wrap.db.get_all_docs())
            return {
                "ok": True,
                "reindexed": memory_subdir,
                "indexed_count": indexed_count,
            }
        except Exception as e:
            PrintStyle.error(f"knowledge_reindex failed: {e}")
            return Response(
                response=f'{{"ok": false, "error": "{str(e)}"}}',
                status=500,
                mimetype="application/json",
            )
