"""The agent loop, and the interface every model sits behind.

One loop serves both agents. The model decides which tools to call; tool
errors go back to it as results so it can correct itself; every call is kept
in a trace the reviewer can read.
"""
import json
from dataclasses import dataclass
from typing import Any, Callable, Protocol


@dataclass
class Tool:
    name: str
    description: str
    schema: dict  # JSON Schema for the tool's arguments
    fn: Callable[..., Any]


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Turn:
    content: Any  # the assistant message content, replayed verbatim on the next request
    calls: list[ToolCall]
    text: str = ""


class Model(Protocol):
    """A text and vision model that can call tools.

    Messages use the Anthropic Messages shape (text, image, tool_use and
    tool_result content blocks). A second provider would translate inside its
    own adapter.
    """

    name: str

    def turn(self, system: str, messages: list[dict], tools: list[Tool]) -> Turn: ...


class ModelError(RuntimeError):
    """The model could not produce a usable turn (API failure, refusal, truncation)."""


def run_agent(model: Model, system: str, content: list[dict], tools: list[Tool], max_steps: int = 8) -> tuple[list[dict], str]:
    """Run the model until it answers without calling a tool.

    Returns (trace, summary): every tool call with its result, and the model's
    closing text.
    """
    by_name = {tool.name: tool for tool in tools}
    messages = [{"role": "user", "content": content}]
    trace = []
    for _ in range(max_steps):
        turn = model.turn(system, messages, tools)
        if not turn.calls:
            return trace, turn.text
        messages.append({"role": "assistant", "content": turn.content})
        results = []
        for call in turn.calls:
            output, failed = _execute(by_name, call)
            trace.append({"tool": call.name, "args": call.args, "result": output, "error": failed})
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": output, "is_error": failed})
        messages.append({"role": "user", "content": results})
    return trace, "Processing stopped at its step limit before finishing. Treat this result as incomplete."


def _execute(by_name: dict[str, Tool], call: ToolCall) -> tuple[str, bool]:
    tool = by_name.get(call.name)
    if tool is None:
        return f"Unknown tool '{call.name}'.", True
    try:
        result = tool.fn(**call.args)
    except (ValueError, TypeError) as exc:  # bad arguments from the model, not a bug in the tool
        return f"Error: {exc}", True
    return (result if isinstance(result, str) else json.dumps(result)), False
