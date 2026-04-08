"""
LLM provider abstraction. Supports Anthropic Claude, OpenAI, and Azure OpenAI.
Returns a callable that takes (system_prompt, user_message, tools) and returns text.
"""

from __future__ import annotations
import os
from typing import Any, Callable

import anthropic
import openai


def build_llm(config: dict) -> Callable:
    """
    Build an LLM callable from config.

    Returns a function with signature:
        call(system: str, user: str, tools: list[dict] | None = None) -> str
    """
    provider = config.get("provider", "anthropic").lower()

    if provider == "anthropic":
        return _anthropic_client(config)
    elif provider in ("openai", "azure", "custom"):
        return _openai_client(config)
    else:
        raise ValueError(f"Unknown LLM provider: {provider}. Supported: anthropic, openai, azure, custom")


def _anthropic_client(config: dict) -> Callable:
    api_key = config.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)
    model = config.get("model", "claude-opus-4-5")
    max_tokens = config.get("max_tokens", 32768)
    temperature = config.get("temperature", 0.7)

    def call(system: str, user: str, tools: list[dict] | None = None) -> str:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if tools:
            kwargs["tools"] = tools

        # Agentic loop: handle tool_use responses
        messages = [{"role": "user", "content": user}]
        while True:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=messages,
                tools=tools or [],
            )

            if response.stop_reason == "max_tokens":
                raise RuntimeError(
                    f"LLM output was truncated (max_tokens={max_tokens} reached). "
                    "Increase max_tokens in config or reduce template complexity."
                )

            if response.stop_reason == "end_turn" or not tools:
                # Extract text from response
                for block in response.content:
                    if hasattr(block, "text"):
                        return block.text
                return ""

            if response.stop_reason == "tool_use":
                # Process tool calls and continue
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = _dispatch_tool(block.name, block.input,
                                                kwargs.get("_tool_handlers", {}))
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(result),
                        })

                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
            else:
                # Unexpected stop reason
                break

        return ""

    return call


def _openai_client(config: dict) -> Callable:
    provider = config.get("provider", "openai").lower()

    if provider == "custom":
        api_key = config.get("api_key") or os.environ.get("LLM_API_KEY", "none")
        base_url = config.get("base_url")
        if not base_url:
            raise ValueError("base_url is required for provider 'custom'")
        client = openai.OpenAI(api_key=api_key, base_url=base_url)
        model = config.get("model")
        if not model:
            raise ValueError("model is required for provider 'custom'")
    elif provider == "azure":
        api_key = config.get("api_key") or os.environ.get("AZURE_OPENAI_KEY")
        client = openai.AzureOpenAI(
            api_key=api_key,
            azure_endpoint=config["base_url"],
            api_version="2024-08-01-preview",
        )
        model = config.get("deployment") or config.get("model")
    else:
        api_key = config.get("api_key") or os.environ.get("OPENAI_API_KEY")
        base_url = config.get("base_url") or None
        client = openai.OpenAI(api_key=api_key, base_url=base_url)
        model = config.get("model", "gpt-4o")

    max_tokens = config.get("max_tokens", 32768)
    context_window = config.get("context_window", 32768)
    temperature = config.get("temperature", 0.7)

    def call(system: str, user: str, tools: list[dict] | None = None, handlers: dict | None = None) -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        # Estimate input token usage (~4 chars/token) and leave room within the context window.
        # This prevents silent empty responses when input + max_tokens overflows the model's context.
        input_chars = len(system) + len(user)
        if tools:
            input_chars += sum(len(str(t)) for t in tools)
        estimated_input_tokens = input_chars // 4
        dynamic_max = context_window - estimated_input_tokens - 512  # 512 token safety buffer
        effective_max = max(min(max_tokens, dynamic_max), 1024)  # always allow at least 1024 output

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": effective_max,
            "temperature": temperature,
            "messages": messages,
        }
        if tools:
            # Convert Anthropic-style tool defs to OpenAI format
            kwargs["tools"] = [_to_openai_tool(t) for t in tools]
            kwargs["tool_choice"] = "auto"

        tool_calls_made = False

        while True:
            response = client.chat.completions.create(**kwargs)
            choice = response.choices[0]

            if choice.finish_reason == "length":
                raise RuntimeError(
                    f"LLM output was truncated (max_tokens={effective_max} reached). "
                    "Reduce template complexity or increase context_window in config."
                )

            if choice.finish_reason == "stop":
                content = choice.message.content
                if content and content.strip():
                    return content
                # Model finished tool calls but returned no text — ask explicitly for the JSON
                if tool_calls_made:
                    kwargs["messages"].append({"role": "assistant", "content": ""})
                    kwargs["messages"].append({"role": "user", "content": "Now output the final JSON."})
                    kwargs.pop("tools", None)
                    kwargs.pop("tool_choice", None)
                    final = client.chat.completions.create(**kwargs)
                    return final.choices[0].message.content or ""
                return ""

            if choice.finish_reason == "tool_calls" and tools:
                tool_calls_made = True
                tool_calls = choice.message.tool_calls
                kwargs["messages"].append(choice.message)
                for tc in tool_calls:
                    import json
                    result = _dispatch_tool(tc.function.name,
                                           json.loads(tc.function.arguments),
                                           handlers or {})
                    kwargs["messages"].append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": str(result),
                    })
            else:
                raise RuntimeError(
                    f"[llm_client] unexpected finish_reason={choice.finish_reason!r}, "
                    f"tool_calls_made={tool_calls_made}, "
                    f"content={repr((choice.message.content or '')[:300])}"
                )

    return call


def _dispatch_tool(name: str, inputs: dict, handlers: dict) -> str:
    handler = handlers.get(name)
    if handler is None:
        return f"Error: tool '{name}' not found"
    try:
        return str(handler(**inputs))
    except Exception as e:
        return f"Error calling tool '{name}': {e}"


def _to_openai_tool(anthropic_tool: dict) -> dict:
    """Convert Anthropic tool definition format to OpenAI function format."""
    return {
        "type": "function",
        "function": {
            "name": anthropic_tool["name"],
            "description": anthropic_tool.get("description", ""),
            "parameters": anthropic_tool.get("input_schema", {"type": "object", "properties": {}}),
        },
    }
