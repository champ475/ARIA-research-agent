"""
evals/answers/replay.py — the record/replay LLM provider for the answer eval
(EVAL-01).

Installed through agents.llm_client.set_provider_override, so every client the
graph builds (analyzer, planner, reflection, judge, the streamed answer, the
extractors) answers from evals/fixtures/answers.jsonl, keyed like the golden
set's fixtures by sha256(node, system prompt, prompt). In record mode a miss is
sent to the real provider chain (LLMClient.live) and saved; in replay mode a
miss is noted on the store and raised, and the eval fails that case.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator

from evals.runner import FixtureStore, MissingFixture

_CHUNK = 40


class ReplayStore(FixtureStore):
    def __init__(self, path, mode: str):
        super().__init__(path)
        self.mode = mode
        self.misses: list[str] = []
        self._lock = threading.Lock()

    def put(self, key: str, node: str, response: str) -> None:
        with self._lock:
            super().put(key, node, response)


class ReplayProvider:
    name = "replay"
    model = "replay"

    def __init__(self, system: str, node: str | None, store: ReplayStore):
        self.system, self.node, self.store = system, node or "other", store
        self.last_usage: tuple[int, int] | None = None
        self._live = None

    def _key(self, prompt: str) -> str:
        return hashlib.sha256(f"{self.node}\n{self.system}\n{prompt}".encode()).hexdigest()

    def ask(
        self, prompt: str, temperature: float = 0.0, max_tokens: int = 0, model: str | None = None
    ) -> str:
        key = self._key(prompt)
        out = self.store.data.get(key)
        if out is None:
            if self.store.mode != "record":
                from utils.llm_json import REPAIR_MARKER

                if REPAIR_MARKER in prompt:
                    return ""  # LLM-05's repair retry after an unusable reply: the node's fallback
                self.store.misses.append(f"{self.node}:{key[:12]}")
                raise MissingFixture(key)
            if self._live is None:
                from agents.llm_client import LLMClient

                self._live = LLMClient.live(self.system, node=self.node)
            out = self._live.ask(prompt, temperature=temperature, max_tokens=max_tokens)
            self.store.put(key, self.node, out)
        self.last_usage = (max(1, len(prompt) // 4), max(1, len(out) // 4))
        return out

    chat = ask

    def stream(
        self, prompt: str, temperature: float = 0.0, max_tokens: int = 0, model: str | None = None
    ) -> Iterator[str]:
        text = self.ask(prompt, temperature, max_tokens, model)
        for i in range(0, len(text), _CHUNK):
            yield text[i : i + _CHUNK]

    def reset(self) -> None:
        pass

    def load_history(self, messages: list[dict]) -> None:
        pass
