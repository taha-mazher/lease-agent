"""Model selection, and the Anthropic adapter.

MODEL_PROVIDER=stub|anthropic picks the model. When unset, the Anthropic
adapter is used if ANTHROPIC_API_KEY is present, and the stub otherwise.
"""
import os
from functools import lru_cache

from .agent import Model, ModelError, Tool, ToolCall, Turn


class AnthropicModel:
    def __init__(self):
        import anthropic  # imported here so the stub runs without the SDK installed

        self._anthropic = anthropic
        self._client = anthropic.Anthropic()
        self.name = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")

    def turn(self, system: str, messages: list[dict], tools: list[Tool]) -> Turn:
        try:
            response = self._client.messages.create(
                model=self.name,
                max_tokens=16000,
                system=system,
                messages=messages,
                tools=[{"name": t.name, "description": t.description, "input_schema": t.schema} for t in tools],
                output_config={"effort": "high"},
            )
        except self._anthropic.APIError as exc:
            raise ModelError(f"Anthropic API error: {exc}") from exc
        if response.stop_reason in ("refusal", "max_tokens"):
            raise ModelError(f"The model stopped early ({response.stop_reason}).")
        calls = [ToolCall(block.id, block.name, block.input) for block in response.content if block.type == "tool_use"]
        text = "".join(block.text for block in response.content if block.type == "text")
        # response.content goes back unchanged: it carries thinking blocks the API expects to see again.
        return Turn(response.content, calls, text)


@lru_cache
def get_model() -> Model:
    provider = os.environ.get("MODEL_PROVIDER") or ("anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "stub")
    if provider == "anthropic":
        return AnthropicModel()
    if provider == "stub":
        from .stub import StubModel

        return StubModel()
    raise ValueError(f"Unknown MODEL_PROVIDER '{provider}'. Use 'stub' or 'anthropic'.")
