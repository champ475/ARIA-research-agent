"""agents/base_agent.py"""
from agents.llm_client import LLMClient
from config import TOKENS_AGENT


class BaseAgent:
    def __init__(self, system_prompt: str = ""):
        self.client = LLMClient(system_prompt=system_prompt or self._system())

    def _system(self) -> str:
        return (
            "You are ARIA, an expert institutional equity analyst for Indian markets. "
            "Provide precise, data-grounded analysis in clean Markdown."
        )

    def ask(self, prompt: str, temperature: float = 0.3) -> str:
        return self.client.ask(prompt, temperature=temperature, max_tokens=TOKENS_AGENT)

    def chat(self, prompt: str, temperature: float = 0.3) -> str:
        return self.client.chat(prompt, temperature=temperature, max_tokens=TOKENS_AGENT)

    @property
    def provider(self) -> str:
        return self.client.provider
