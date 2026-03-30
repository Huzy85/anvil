import asyncio
import json
import os
import re
from typing import Any, List, Dict


class PlannerError(Exception):
    pass


def get_repo_context(repo_path: str, max_files: int = 60) -> str:
    """Return a compact file tree of the repo for context injection."""
    lines = []
    for root, dirs, files in os.walk(repo_path):
        # Skip hidden and common noise dirs
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".git", "dist", "build", "venv", ".venv")]
        rel = os.path.relpath(root, repo_path)
        prefix = "" if rel == "." else rel + "/"
        for f in sorted(files):
            lines.append(prefix + f)
            if len(lines) >= max_files:
                lines.append(f"... ({max_files}+ files, truncated)")
                return "\n".join(lines)
    return "\n".join(lines)


def _build_prompt(rough_plan: str, project_name: str, context: str) -> str:
    context_section = f"\nRepository contents:\n{context}\n" if context else ""
    return (
        f"You are a software planning assistant. Break this project plan into a numbered list of discrete coding tasks.\n\n"
        f"Project: {project_name}"
        f"{context_section}\n"
        f"Plan:\n{rough_plan}\n\n"
        "Return a JSON array only — no explanation before or after. Each item must have:\n"
        '- title: short task name (max 60 chars)\n'
        '- description: detailed instructions for a coding agent (what to build, what files to create/edit, acceptance criteria)\n\n'
        'Example format:\n'
        '[\n'
        '  {\n'
        '    "title": "Set up project structure",\n'
        '    "description": "Create the directory layout with src/, tests/, and a pyproject.toml..."\n'
        '  }\n'
        ']'
    )


def _extract_task_list(claude_stdout: str) -> List[Dict]:
    """Extract and validate a task list from claude --output-format json output."""
    raw = claude_stdout.strip()

    # claude --output-format json wraps output: {"result": "..."}
    try:
        wrapper = json.loads(raw)
        result_text = wrapper.get("result", raw) if isinstance(wrapper, dict) else raw
    except json.JSONDecodeError:
        result_text = raw

    # Now parse the task array from result_text
    try:
        tasks = json.loads(result_text)
    except json.JSONDecodeError:
        # Try to fish out a JSON array from mixed text
        match = re.search(r"(\[.*\])", result_text, re.DOTALL)
        if not match:
            raise PlannerError(
                f"Claude did not return a JSON array. Got:\n{result_text[:500]}"
            )
        try:
            tasks = json.loads(match.group(1))
        except json.JSONDecodeError as e:
            raise PlannerError(f"Could not parse extracted JSON array: {e}")

    if not isinstance(tasks, list):
        raise PlannerError(f"Expected a JSON array, got {type(tasks).__name__}")
    if not tasks:
        raise PlannerError("Claude returned an empty task list.")

    validated = []
    for i, item in enumerate(tasks):
        if not isinstance(item, dict):
            raise PlannerError(f"Task {i+1} is not a JSON object")
        title = item.get("title", "").strip()
        description = item.get("description", "").strip()
        if not title:
            raise PlannerError(f"Task {i+1} is missing a non-empty 'title'")
        if not description:
            raise PlannerError(f"Task {i+1} is missing a non-empty 'description'")
        validated.append({"title": title, "description": description})

    return validated


async def generate_plan(
    rough_plan: str,
    project_name: str,
    planner_config: Any,
    existing_context: str = "",
) -> List[Dict]:
    prompt = _build_prompt(rough_plan, project_name, existing_context)

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt,
            "--output-format", "json",
            "--allowedTools", "Read,Glob,Grep",
            "--permission-mode", "acceptEdits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise PlannerError("'claude' not found in PATH. Install Claude Code CLI first.")

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise PlannerError("Claude timed out after 180s while generating plan.")

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        raise PlannerError(f"Claude exited {proc.returncode}: {err}")

    return _extract_task_list(stdout.decode("utf-8", errors="replace"))
