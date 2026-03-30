import yaml
from dataclasses import dataclass, field
from typing import List, Optional


class ConfigError(Exception):
    pass


@dataclass
class ChatModel:
    url: str = "http://localhost:8082/v1"
    model: str = "auto"
    api_key: str = ""
    name: str = "Hermes"


@dataclass
class Worker:
    name: str
    url: str
    model: str = "auto"
    api_key: str = ""
    max_iterations: int = 30


@dataclass
class Reviewer:
    max_turns: int = 5
    allowed_tools: List[str] = field(default_factory=lambda: ["Read", "Grep", "Glob"])
    escalation_tools: List[str] = field(
        default_factory=lambda: ["Read", "Grep", "Glob", "Write", "Edit", "Bash"]
    )


@dataclass
class Planner:
    max_turns: int = 10


@dataclass
class Session:
    max_retries: int = 2
    concurrent: bool = True


@dataclass
class Project:
    name: str
    repo: str = "."


@dataclass
class ForgeConfig:
    project: Project
    workers: List[Worker]
    reviewer: Reviewer
    planner: Planner
    session: Session
    chat_model: ChatModel = field(default_factory=ChatModel)

    def get_worker(self, name: str) -> Optional[Worker]:
        return next((w for w in self.workers if w.name == name), None)

    @property
    def default_worker(self) -> Worker:
        return self.workers[0]


def load_config(path: str = "forge.yaml") -> ForgeConfig:
    import os
    global_path = os.path.expanduser("~/.forge/forge.yaml")
    resolved = path if os.path.exists(path) else (global_path if os.path.exists(global_path) else path)
    try:
        with open(resolved, "r") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        raise ConfigError(f"No forge.yaml found locally or at {global_path}. Run 'forge init' to create one.")
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in '{resolved}': {e}")

    if "project" not in data:
        raise ConfigError("Missing required 'project' section")
    project_data = data["project"]
    if "name" not in project_data:
        raise ConfigError("Missing required 'project.name'")
    project = Project(
        name=str(project_data["name"]),
        repo=str(project_data.get("repo", ".")),
    )

    if "workers" not in data:
        raise ConfigError("Missing required 'workers' section")
    workers_data = data["workers"]
    if not isinstance(workers_data, list) or len(workers_data) == 0:
        raise ConfigError("'workers' must be a non-empty list")

    workers = []
    for i, wd in enumerate(workers_data):
        if not isinstance(wd, dict):
            raise ConfigError(f"Worker #{i} must be a mapping")
        if "name" not in wd:
            raise ConfigError(f"Worker #{i} missing required 'name'")
        if "url" not in wd:
            raise ConfigError(f"Worker #{i} missing required 'url'")
        workers.append(
            Worker(
                name=str(wd["name"]),
                url=str(wd["url"]),
                model=str(wd.get("model", "auto")),
                api_key=str(wd.get("api_key", "")),
                max_iterations=int(wd.get("max_iterations", 30)),
            )
        )

    rd = data.get("reviewer", {})
    reviewer = Reviewer(
        max_turns=int(rd.get("max_turns", 5)),
        allowed_tools=list(rd.get("allowed_tools", ["Read", "Grep", "Glob"])),
        escalation_tools=list(
            rd.get("escalation_tools", ["Read", "Grep", "Glob", "Write", "Edit", "Bash"])
        ),
    )

    pd = data.get("planner", {})
    planner = Planner(max_turns=int(pd.get("max_turns", 10)))

    sd = data.get("session", {})
    session = Session(
        max_retries=int(sd.get("max_retries", 2)),
        concurrent=bool(sd.get("concurrent", True)),
    )

    cd = data.get("chat_model", {})
    chat_model = ChatModel(
        url=str(cd.get("url", "http://localhost:8082/v1")),
        model=str(cd.get("model", "auto")),
        api_key=str(cd.get("api_key", "")),
        name=str(cd.get("name", "Hermes")),
    )

    return ForgeConfig(
        project=project,
        workers=workers,
        reviewer=reviewer,
        planner=planner,
        session=session,
        chat_model=chat_model,
    )


def generate_template(project_name: str = "my-project") -> str:
    return f"""project:
  name: {project_name}
  repo: "."

# Chat model: local LLM for the initial idea conversation
chat_model:
  url: "http://localhost:8082/v1"   # Hermes — fast, conversational
  model: "auto"
  name: "Hermes"

# Worker: local LLM that does the actual coding
workers:
  - name: hercules
    url: "http://localhost:8081/v1"  # Hercules — coding model
    model: "auto"
    api_key: ""
    max_iterations: 30

# Reviewer: Claude checks every task result
reviewer:
  max_turns: 5
  allowed_tools:
    - Read
    - Grep
    - Glob
  escalation_tools:
    - Read
    - Grep
    - Glob
    - Write
    - Edit
    - Bash

planner:
  max_turns: 10

session:
  max_retries: 2
  concurrent: true
"""
