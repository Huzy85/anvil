import os
import json
import tempfile
import uuid
import shutil
from datetime import datetime
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any


class StateError(Exception):
    pass


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    REVIEWING = "reviewing"
    ACCEPTED = "accepted"
    FAILED = "failed"
    ESCALATED = "escalated"


@dataclass
class ReviewResult:
    approved: bool
    feedback: str
    attempt: int


@dataclass
class Task:
    id: int
    title: str
    description: str
    status: TaskStatus
    branch: str
    retries: int = 0
    review_results: List[ReviewResult] = field(default_factory=list)
    worker_name: str = ""
    created_at: str = ""
    completed_at: Optional[str] = None
    error: Optional[str] = None


@dataclass
class ForgeSession:
    id: str
    project_name: str
    session_branch: str
    base_branch: str
    tasks: List[Task]
    created_at: str
    completed_at: Optional[str] = None
    status: str = "active"


def _task_to_dict(task: Task) -> Dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status.value,
        "branch": task.branch,
        "retries": task.retries,
        "review_results": [
            {"approved": r.approved, "feedback": r.feedback, "attempt": r.attempt}
            for r in task.review_results
        ],
        "worker_name": task.worker_name,
        "created_at": task.created_at,
        "completed_at": task.completed_at,
        "error": task.error,
    }


def _dict_to_task(data: Dict[str, Any]) -> Task:
    return Task(
        id=data["id"],
        title=data["title"],
        description=data["description"],
        status=TaskStatus(data.get("status", "pending")),
        branch=data["branch"],
        retries=data.get("retries", 0),
        review_results=[
            ReviewResult(
                approved=r["approved"],
                feedback=r["feedback"],
                attempt=r["attempt"],
            )
            for r in data.get("review_results", [])
        ],
        worker_name=data.get("worker_name", ""),
        created_at=data.get("created_at", ""),
        completed_at=data.get("completed_at"),
        error=data.get("error"),
    )


def _session_to_dict(session: ForgeSession) -> Dict[str, Any]:
    return {
        "id": session.id,
        "project_name": session.project_name,
        "session_branch": session.session_branch,
        "base_branch": session.base_branch,
        "tasks": [_task_to_dict(t) for t in session.tasks],
        "created_at": session.created_at,
        "completed_at": session.completed_at,
        "status": session.status,
    }


def _dict_to_session(data: Dict[str, Any]) -> ForgeSession:
    return ForgeSession(
        id=data["id"],
        project_name=data["project_name"],
        session_branch=data["session_branch"],
        base_branch=data["base_branch"],
        tasks=[_dict_to_task(t) for t in data.get("tasks", [])],
        created_at=data["created_at"],
        completed_at=data.get("completed_at"),
        status=data.get("status", "active"),
    )


class StateManager:
    def __init__(self, repo_path: str):
        self.state_dir = os.path.join(repo_path, ".forge")
        self.state_file = os.path.join(self.state_dir, "state.json")
        self._ensure_dir()

    def _ensure_dir(self):
        os.makedirs(self.state_dir, exist_ok=True)
        gitignore = os.path.join(self.state_dir, ".gitignore")
        if not os.path.exists(gitignore):
            with open(gitignore, "w") as f:
                f.write("state.json\n")

    def load(self) -> ForgeSession:
        if not os.path.exists(self.state_file):
            raise StateError("No active session found. Run 'forge new' first.")
        try:
            with open(self.state_file, "r") as f:
                return _dict_to_session(json.load(f))
        except (json.JSONDecodeError, KeyError) as e:
            raise StateError(f"Corrupted state file: {e}")

    def save(self, session: ForgeSession):
        fd, tmp = tempfile.mkstemp(dir=self.state_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(_session_to_dict(session), f, indent=2)
            shutil.move(tmp, self.state_file)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def create(
        self, project_name: str, base_branch: str, tasks_data: List[Dict[str, Any]]
    ) -> ForgeSession:
        session_id = uuid.uuid4().hex[:6]
        now = datetime.utcnow().isoformat() + "Z"
        tasks = [
            Task(
                id=i + 1,
                title=td["title"],
                description=td["description"],
                status=TaskStatus.PENDING,
                branch=f"forge/task-{i + 1}",
                created_at=now,
            )
            for i, td in enumerate(tasks_data)
        ]
        session = ForgeSession(
            id=session_id,
            project_name=project_name,
            session_branch=f"forge/session-{session_id}",
            base_branch=base_branch,
            tasks=tasks,
            created_at=now,
        )
        self.save(session)
        return session

    def update_task(self, session: ForgeSession, task_id: int, **kwargs) -> ForgeSession:
        for task in session.tasks:
            if task.id == task_id:
                for k, v in kwargs.items():
                    setattr(task, k, v)
                break
        self.save(session)
        return session

    def add_review(
        self, session: ForgeSession, task_id: int, review: ReviewResult
    ) -> ForgeSession:
        for task in session.tasks:
            if task.id == task_id:
                task.review_results.append(review)
                break
        self.save(session)
        return session
