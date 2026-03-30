import asyncio
import json
import re
from dataclasses import dataclass
from typing import List, Optional

from .config import Reviewer

MAX_DIFF_CHARS = 40_000  # truncate huge diffs before sending to Claude


class ReviewError(Exception):
    pass


@dataclass
class ReviewDecision:
    approved: bool
    feedback: str
    escalated: bool = False


def _strip_ansi(text: str) -> str:
    return re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])").sub("", text)


def _truncate_diff(diff: str) -> str:
    if len(diff) <= MAX_DIFF_CHARS:
        return diff
    return diff[:MAX_DIFF_CHARS] + f"\n\n[diff truncated — {len(diff) - MAX_DIFF_CHARS} chars omitted]"


def _review_prompt(title: str, description: str, diff: str) -> str:
    return (
        f"You are reviewing code for task: {title}\n\n"
        f"Task description:\n{description}\n\n"
        f"Git diff:\n{_truncate_diff(diff)}\n\n"
        "Review this diff carefully. Respond with:\n"
        "- APPROVED if the implementation is correct and complete\n"
        "- REJECTED if there are issues\n\n"
        "If REJECTED, explain exactly what needs to be fixed.\n"
        "If APPROVED, briefly summarize what was done well."
    )


def _escalation_prompt(title: str, description: str, diff: str, feedback_history: List[str]) -> str:
    history = "\n".join(f"Attempt {i+1}: {f}" for i, f in enumerate(feedback_history))
    return (
        f"You are a senior developer. The worker LLM failed to implement this task correctly after "
        f"{len(feedback_history)} attempt(s). Fix it directly using your tools.\n\n"
        f"Task: {title}\n\n"
        f"Description:\n{description}\n\n"
        f"Current diff (what the worker produced):\n{_truncate_diff(diff)}\n\n"
        f"Rejection history:\n{history}\n\n"
        "Read the relevant files, fix the issues, and confirm what you changed."
    )


async def _run_claude(
    prompt: str,
    tools: List[str],
    cwd: Optional[str] = None,
    timeout: int = 300,
) -> tuple[str, str, int]:
    cmd = [
        "claude",
        "-p", prompt,
        "--output-format", "json",
        "--allowedTools", ",".join(tools),
        "--permission-mode", "acceptEdits",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise ReviewError(f"Claude timed out after {timeout}s")
        return (
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            proc.returncode,
        )
    except FileNotFoundError:
        raise ReviewError("'claude' not found in PATH. Install Claude Code CLI first.")


def _parse_output(raw: str) -> ReviewDecision:
    # claude --output-format json wraps output in {result: "..."}
    text = raw.strip()
    try:
        data = json.loads(text)
        result = data.get("result", text)
    except (json.JSONDecodeError, AttributeError):
        result = text

    result = _strip_ansi(result).strip()

    if "APPROVED" in result:
        feedback = re.sub(r"\bAPPROVED\b", "", result).strip(" :-\n")
        return ReviewDecision(approved=True, feedback=feedback or "Looks good.")
    if "REJECTED" in result:
        feedback = re.sub(r"\bREJECTED\b", "", result).strip(" :-\n")
        return ReviewDecision(approved=False, feedback=feedback or "Issues found — no details provided.")
    # No clear verdict — treat as rejection with full text
    return ReviewDecision(approved=False, feedback=result or "No review output received.")


async def review_task(
    task_title: str,
    task_description: str,
    diff: str,
    reviewer_config: Reviewer,
    attempt: int = 1,
) -> ReviewDecision:
    prompt = _review_prompt(task_title, task_description, diff)
    try:
        stdout, stderr, code = await _run_claude(
            prompt, reviewer_config.allowed_tools
        )
    except ReviewError as e:
        return ReviewDecision(approved=False, feedback=str(e))

    if code != 0:
        msg = stderr.strip() or f"claude exited {code}"
        return ReviewDecision(approved=False, feedback=f"Claude error: {msg}")

    return _parse_output(stdout)


async def escalate_and_fix(
    task_title: str,
    task_description: str,
    diff: str,
    feedback_history: List[str],
    repo_path: str,
    reviewer_config: Reviewer,
) -> ReviewDecision:
    prompt = _escalation_prompt(task_title, task_description, diff, feedback_history)
    try:
        stdout, stderr, code = await _run_claude(
            prompt,
            reviewer_config.escalation_tools,
            cwd=repo_path,
            timeout=600,  # more time — Claude may be writing code
        )
    except ReviewError as e:
        return ReviewDecision(approved=False, feedback=str(e), escalated=True)

    if code != 0:
        msg = stderr.strip() or f"claude exited {code}"
        return ReviewDecision(approved=False, feedback=f"Escalation failed: {msg}", escalated=True)

    # Parse for summary — escalation is always "approved" if exit 0
    text = stdout.strip()
    try:
        data = json.loads(text)
        summary = data.get("result", text)
    except (json.JSONDecodeError, AttributeError):
        summary = text

    return ReviewDecision(
        approved=True,
        feedback=_strip_ansi(summary).strip() or "Fixed by Claude.",
        escalated=True,
    )
