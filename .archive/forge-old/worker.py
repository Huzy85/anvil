import os
import re
import json
import fnmatch
import subprocess
from dataclasses import dataclass
from typing import Optional, Callable, List, Dict, Any

import httpx

from .config import Worker


class WorkerError(Exception):
    pass


@dataclass
class WorkerResult:
    success: bool
    summary: str
    files_changed: List[str]
    iterations: int
    error: Optional[str] = None


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file's contents from the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to repo root"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write or overwrite a file in the repository. Creates parent directories automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to repo root"},
                    "content": {"type": "string", "description": "File content"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command. Timeout 60s. Returns stdout, stderr, exit_code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                    "cwd": {
                        "type": "string",
                        "description": "Working directory (default: repo root)",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in a directory matching a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path relative to repo root (default: .)",
                    },
                    "pattern": {"type": "string", "description": "Glob pattern (default: *)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search for a regex pattern across files in the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {
                        "type": "string",
                        "description": "Directory to search (default: repo root)",
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "Glob to filter files (default: *)",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_complete",
            "description": "Call this when the task is fully done.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "What was done and why",
                    },
                    "files_changed": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of files created or modified",
                    },
                },
                "required": ["summary", "files_changed"],
            },
        },
    },
]

_DANGEROUS = [
    r"rm\s+-rf\s+/",
    r"rm\s+-rf\s+~",
    r"mkfs",
    r"dd\s+of=/dev/[a-z]",
    r":\(\)\s*\{",  # fork bomb
]


def _is_safe_command(command: str) -> bool:
    for pattern in _DANGEROUS:
        if re.search(pattern, command, re.IGNORECASE):
            return False
    return True


def _resolve(repo_path: str, path: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.join(repo_path, path)


async def _execute_tool(tool_call: Dict[str, Any], repo_path: str) -> Dict[str, Any]:
    name = tool_call["function"]["name"]
    tool_id = tool_call["id"]

    # Arguments come as a JSON string from the API
    raw_args = tool_call["function"].get("arguments", "{}")
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    except json.JSONDecodeError:
        args = {}

    def result(content: str) -> Dict[str, Any]:
        return {"role": "tool", "tool_call_id": tool_id, "content": content}

    try:
        if name == "read_file":
            path = _resolve(repo_path, args["path"])
            with open(path, "r", encoding="utf-8") as f:
                return result(f.read())

        elif name == "write_file":
            path = _resolve(repo_path, args["path"])
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(args["content"])
            return result(f"Written: {args['path']}")

        elif name == "run_command":
            command = args["command"]
            if not _is_safe_command(command):
                return result(f"Blocked: command '{command}' matched a safety filter.")
            cwd = args.get("cwd") or repo_path
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=cwd,
            )
            return result(
                json.dumps(
                    {
                        "stdout": proc.stdout,
                        "stderr": proc.stderr,
                        "exit_code": proc.returncode,
                    }
                )
            )

        elif name == "list_files":
            base = _resolve(repo_path, args.get("path", "."))
            pattern = args.get("pattern", "*")
            files = []
            for root, _, filenames in os.walk(base):
                for fn in filenames:
                    if fnmatch.fnmatch(fn, pattern):
                        files.append(os.path.relpath(os.path.join(root, fn), repo_path))
            return result(json.dumps(files))

        elif name == "search_code":
            pattern = args["pattern"]
            base = _resolve(repo_path, args.get("path", "."))
            file_pattern = args.get("file_pattern", "*")
            matches = []
            for root, _, filenames in os.walk(base):
                for fn in filenames:
                    if fnmatch.fnmatch(fn, file_pattern):
                        fpath = os.path.join(root, fn)
                        try:
                            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                                for lineno, line in enumerate(f, 1):
                                    if re.search(pattern, line):
                                        matches.append(
                                            {
                                                "file": os.path.relpath(fpath, repo_path),
                                                "line": lineno,
                                                "content": line.rstrip(),
                                            }
                                        )
                        except OSError:
                            continue
            return result(json.dumps(matches))

        elif name == "task_complete":
            # Handled in the main loop — just ack here
            return result("acknowledged")

        else:
            return result(f"Unknown tool: {name}")

    except subprocess.TimeoutExpired:
        return result("Command timed out after 60s.")
    except Exception as e:
        return result(f"Tool error ({name}): {e}")


async def run_task(
    task_description: str,
    task_title: str,
    repo_path: str,
    worker: Worker,
    context: str = "",
    on_iteration: Optional[Callable[[int, str], None]] = None,
) -> WorkerResult:
    system_prompt = (
        f"You are a coding agent working on a software project.\n"
        f"Repository root: {repo_path}\n\n"
        f"Use your tools to read files, write code, and run commands. "
        f"Work carefully and verify your changes with run_command where appropriate. "
        f"When the task is fully complete, call task_complete with a clear summary and the list of files you changed.\n\n"
        f"Task: {task_title}\n\n"
        f"{task_description}"
    )
    if context:
        system_prompt += f"\n\nContext from previous tasks:\n{context}"

    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    iterations = 0
    files_changed: List[str] = []  # accumulates across all iterations

    base_url = worker.url.rstrip("/")
    endpoint = f"{base_url}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if worker.api_key:
        headers["Authorization"] = f"Bearer {worker.api_key}"

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        while iterations < worker.max_iterations:
            iterations += 1

            try:
                resp = await client.post(
                    endpoint,
                    headers=headers,
                    json={
                        "model": worker.model,
                        "messages": messages,
                        "tools": TOOLS,
                        "tool_choice": "auto",
                    },
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                return WorkerResult(
                    success=False,
                    summary="",
                    files_changed=[],
                    iterations=iterations,
                    error=f"API error {e.response.status_code}: {e.response.text[:200]}",
                )
            except httpx.RequestError as e:
                return WorkerResult(
                    success=False,
                    summary="",
                    files_changed=[],
                    iterations=iterations,
                    error=f"Connection error: {e}",
                )

            data = resp.json()
            choice = data["choices"][0]
            message = choice["message"]
            finish_reason = choice.get("finish_reason", "")

            messages.append(message)

            tool_calls = message.get("tool_calls") or []

            if on_iteration:
                label = f"{len(tool_calls)} tool call(s)" if tool_calls else (message.get("content") or "")
                on_iteration(iterations, label[:120])

            if not tool_calls:
                # No tool calls — model finished with text
                summary = message.get("content") or "Task complete."
                return WorkerResult(
                    success=True,
                    summary=summary,
                    files_changed=[],
                    iterations=iterations,
                )

            # Execute tool calls sequentially
            for tc in tool_calls:
                if tc["function"]["name"] == "task_complete":
                    raw = tc["function"].get("arguments", "{}")
                    tc_args = json.loads(raw) if isinstance(raw, str) else raw
                    return WorkerResult(
                        success=True,
                        summary=tc_args.get("summary", ""),
                        files_changed=tc_args.get("files_changed", []),
                        iterations=iterations,
                    )

                tool_result = await _execute_tool(tc, repo_path)
                messages.append(tool_result)

                if tc["function"]["name"] == "write_file":
                    raw = tc["function"].get("arguments", "{}")
                    tc_args = json.loads(raw) if isinstance(raw, str) else raw
                    p = tc_args.get("path", "")
                    if p:
                        files_changed.append(p)

    return WorkerResult(
        success=False,
        summary="",
        files_changed=files_changed,
        iterations=iterations,
        error=f"Max iterations ({worker.max_iterations}) reached without task_complete.",
    )
