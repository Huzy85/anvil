"""Interactive idea chat with a local LLM, leading into a Forge session."""

import sys
from typing import List, Dict, Optional

import httpx
import click

CHAT_SYSTEM = (
    "You are a project planning assistant. Help the user think through and refine their idea. "
    "Ask clarifying questions, explore approaches, and help them land on a clear, concrete direction. "
    "Be concise and practical. When the user is happy with the idea, they type '!plan' to hand "
    "it to Claude for detailed task planning."
)


def _call_llm(url: str, model: str, messages: List[Dict], api_key: str = "") -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 2048,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    endpoint = f"{url.rstrip('/')}/chat/completions"
    with httpx.Client(timeout=httpx.Timeout(90.0)) as client:
        resp = client.post(endpoint, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def run_idea_chat(
    url: str,
    model: str = "auto",
    api_key: str = "",
    chat_name: str = "Local LLM",
) -> Optional[str]:
    """
    Interactive chat loop with the local LLM.
    Returns a conversation transcript when user types !plan, or None if aborted.
    """
    messages: List[Dict] = [{"role": "system", "content": CHAT_SYSTEM}]

    click.echo(f"\nForge — idea chat ({chat_name})")
    click.echo("Describe your project idea. Type '!plan' when ready to hand off to Claude.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            click.echo("\nAborted.")
            return None

        if not user_input:
            continue

        if user_input.lower() in ("!plan", "!done", "!go", "!build"):
            exchanges = [m for m in messages if m["role"] in ("user", "assistant")]
            if len(exchanges) < 2:
                click.echo("Describe the idea first, then type !plan.\n")
                continue
            click.echo("\nHanding off to Claude for detailed planning...\n")
            return _build_transcript(messages)

        messages.append({"role": "user", "content": user_input})
        click.echo()

        try:
            response = _call_llm(url, model, messages, api_key)
        except httpx.HTTPStatusError as e:
            click.echo(f"[error] API {e.response.status_code}: {e.response.text[:200]}", err=True)
            messages.pop()
            continue
        except httpx.RequestError as e:
            click.echo(f"[error] Cannot reach {url}: {e}", err=True)
            messages.pop()
            continue

        click.echo(f"{chat_name}: {response}\n")
        messages.append({"role": "assistant", "content": response})


def _build_transcript(messages: List[Dict]) -> str:
    """Build a rough plan string from the conversation for Claude's planner."""
    lines = ["Project idea — from planning conversation:\n"]
    for m in messages:
        if m["role"] == "user":
            lines.append(f"User: {m['content']}")
        elif m["role"] == "assistant":
            lines.append(f"Assistant: {m['content']}")
    return "\n".join(lines)
