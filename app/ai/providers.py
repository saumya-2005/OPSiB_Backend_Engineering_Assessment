"""Provider implementations. Only the Protocol in summary.py is known to the rest of the app."""
import os
import time

from app.ai.summary import EvidencePackage, ProviderUnavailable

SYSTEM_PROMPT = """You help an operations analyst investigate a retail receipt shortfall.
You receive JSON: facts already computed by a rule engine, what is known/unknown, and evidence records.
Rules:
- Explain the evidence, point out missing context, suggest investigation steps.
- Do NOT calculate or restate quantities differently from the facts given. Do NOT invent records.
- Cite only evidence 'ref' values from the input.
- The data cannot establish cause; never claim supplier fault, carrier loss or theft.
- You cannot decide permissions, change systems, or mark anything verified.
Reply with ONLY JSON: {"explanation": str, "missing_context": [str], "suggested_steps":
[{"text": str, "cites": [ref]}], "cited_refs": [ref]}"""


class DisabledProvider:
    """Default. The app is fully functional with no AI."""
    name = "disabled"

    def summarize(self, package: EvidencePackage):
        raise ProviderUnavailable("AI provider disabled (AI_PROVIDER=none)")


class ScriptedProvider:
    """Deterministic stand-in for tests and demos (no network, no cost)."""
    name = "scripted"

    def __init__(self, response, delay: float = 0.0, error: Exception | None = None):
        self.response, self.delay, self.error = response, delay, error

    def summarize(self, package: EvidencePackage):
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.response


class ClaudeProvider:
    """Anthropic Messages API. `client` is injected so tests use a fake and the code never
    reads secrets itself. NOT exercised against the live API in this exercise."""
    name = "claude"

    def __init__(self, client, model: str, max_tokens: int = 800):
        self.client, self.model, self.max_tokens = client, model, max_tokens

    def summarize(self, package: EvidencePackage):
        msg = self.client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": package.model_dump_json()}])
        return "".join(getattr(b, "text", "") for b in msg.content)


def get_provider(settings):
    if settings.ai_provider == "claude":
        key = os.environ.get("ANTHROPIC_API_KEY")       # secret comes from the environment only
        if not key:
            return DisabledProvider()
        import anthropic                                  # optional dependency, imported lazily
        return ClaudeProvider(anthropic.Anthropic(api_key=key), settings.ai_model)
    return DisabledProvider()