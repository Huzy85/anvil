import asyncio
import os
import sys

import click

from .config import load_config, generate_template, ConfigError
from .state import StateManager, TaskStatus, StateError
from .git_ops import find_repo_root, get_repo, get_current_branch, create_session_branch, GitError
from .planner import generate_plan, get_repo_context, PlannerError
from .runner import ForgeRunner
from .chat import run_idea_chat


def _repo_path() -> str:
    try:
        return find_repo_root(".")
    except GitError as e:
        raise click.ClickException(str(e))


def _optional_repo_path() -> str:
    """Return repo root, or cwd if not in a git repo."""
    try:
        return find_repo_root(".")
    except GitError:
        return os.getcwd()


def _task_color(status: TaskStatus) -> str:
    return {
        TaskStatus.ACCEPTED: "green",
        TaskStatus.ESCALATED: "cyan",
        TaskStatus.REVIEWING: "yellow",
        TaskStatus.RUNNING: "yellow",
        TaskStatus.FAILED: "red",
        TaskStatus.PENDING: "white",
    }.get(status, "white")


@click.group(invoke_without_command=True)
@click.version_option("0.1.0", prog_name="forge")
@click.pass_context
def cli(ctx):
    """Forge — Hercules builds, Claude reviews. Type your idea and go."""
    if ctx.invoked_subcommand is None:
        # bare `forge` → start chat immediately
        ctx.invoke(chat)


# ------------------------------------------------------------------ #
# forge chat                                                          #
# ------------------------------------------------------------------ #

@cli.command()
@click.option("--config", "-c", default="forge.yaml", show_default=True, help="Config file")
@click.option("--worker", "-w", default=None, help="Worker name (default: first in config)")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
def chat(config, worker, verbose):
    """Chat with local LLM to explore the idea, then plan and build with Hercules + Claude."""
    try:
        cfg = load_config(config)
    except ConfigError as e:
        raise click.ClickException(str(e))

    if worker:
        w = cfg.get_worker(worker)
        if w is None:
            raise click.ClickException(f"Worker '{worker}' not found in config.")
        cfg.workers = [w] + [x for x in cfg.workers if x.name != worker]

    # Phase 1: chat with local LLM
    transcript = run_idea_chat(
        url=cfg.chat_model.url,
        model=cfg.chat_model.model,
        api_key=cfg.chat_model.api_key,
        chat_name=cfg.chat_model.name,
    )
    if transcript is None:
        return  # user aborted

    # Phase 2: Claude generates detailed task plan
    # Try to find a git repo; if not found, offer to init one
    try:
        repo_path = find_repo_root(".")
    except GitError:
        cwd = os.getcwd()
        click.echo(f"\nNo git repo found in {cwd}.")
        if click.confirm("Initialise a git repo here?", default=True):
            import subprocess
            subprocess.run(["git", "init", cwd], check=True)
            repo_path = cwd
        else:
            click.echo("Cannot run tasks without a git repo. Exiting.")
            return

    repo = get_repo(repo_path)
    base_branch = get_current_branch(repo)

    click.echo("Claude is breaking the idea into tasks...")
    repo_context = get_repo_context(repo_path)
    try:
        tasks = asyncio.run(
            generate_plan(
                rough_plan=transcript,
                project_name=cfg.project.name,
                planner_config=cfg.planner,
                existing_context=repo_context,
            )
        )
    except PlannerError as e:
        raise click.ClickException(str(e))

    click.echo(f"\n{len(tasks)} tasks:\n")
    for i, t in enumerate(tasks, 1):
        click.echo(f"  {i}. {t['title']}")
        if verbose:
            click.echo(f"     {t['description'][:120]}...")

    click.echo()
    if not click.confirm(f"Proceed with these {len(tasks)} tasks?", default=True):
        click.echo("Aborted.")
        return

    # Phase 3: create session + Hercules implements, Claude reviews
    import uuid
    from .state import ForgeSession, Task, TaskStatus as TS
    from datetime import datetime

    sm = StateManager(repo_path)
    session_id = uuid.uuid4().hex[:6]
    session_branch = f"forge/session-{session_id}"
    try:
        create_session_branch(repo, session_branch, base_branch)
    except GitError as e:
        raise click.ClickException(f"Git error: {e}")

    now = datetime.utcnow().isoformat() + "Z"
    task_objs = [
        Task(
            id=i + 1,
            title=t["title"],
            description=t["description"],
            status=TS.PENDING,
            branch=f"forge/task-{i + 1}",
            created_at=now,
        )
        for i, t in enumerate(tasks)
    ]
    session = ForgeSession(
        id=session_id,
        project_name=cfg.project.name,
        session_branch=session_branch,
        base_branch=base_branch,
        tasks=task_objs,
        created_at=now,
    )
    sm.save(session)

    click.echo(f"\nSession {session_id} — branch {session_branch}\n")

    runner = ForgeRunner(cfg, sm, repo_path, verbose=verbose)
    try:
        session = asyncio.run(runner.run_session(session))
    except KeyboardInterrupt:
        click.echo("\nInterrupted. Run 'forge resume' to continue.")
        sys.exit(1)

    _print_summary(session)


# ------------------------------------------------------------------ #
# forge new                                                           #
# ------------------------------------------------------------------ #

@cli.command()
@click.argument("plan", required=False)
@click.option("--config", "-c", default="forge.yaml", show_default=True, help="Config file")
@click.option("--worker", "-w", default=None, help="Worker name (default: first in config)")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
def new(plan, config, worker, verbose):
    """Start a new Forge session from a rough plan."""
    # Load config
    try:
        cfg = load_config(config)
    except ConfigError as e:
        raise click.ClickException(str(e))

    # Select worker
    if worker:
        w = cfg.get_worker(worker)
        if w is None:
            raise click.ClickException(f"Worker '{worker}' not found in config.")
        cfg.workers = [w] + [x for x in cfg.workers if x.name != worker]

    # Find repo
    repo_path = _repo_path()
    repo = get_repo(repo_path)
    base_branch = get_current_branch(repo)

    # Get plan text
    if not plan:
        click.echo("No plan provided — reading from stdin (Ctrl+D when done):\n")
        plan = sys.stdin.read().strip()
    if not plan:
        raise click.ClickException("Plan cannot be empty.")

    # Generate task list
    click.echo(f"\nGenerating task plan for '{cfg.project.name}'...")
    repo_context = get_repo_context(repo_path)
    try:
        tasks = asyncio.run(
            generate_plan(
                rough_plan=plan,
                project_name=cfg.project.name,
                planner_config=cfg.planner,
                existing_context=repo_context,
            )
        )
    except PlannerError as e:
        raise click.ClickException(str(e))

    # Show tasks
    click.echo(f"\n{len(tasks)} tasks generated:\n")
    for i, t in enumerate(tasks, 1):
        click.echo(f"  {i}. {t['title']}")
        if verbose:
            click.echo(f"     {t['description'][:120]}...")

    click.echo()
    if not click.confirm(f"Proceed with these {len(tasks)} tasks?", default=True):
        click.echo("Aborted.")
        return

    # Create session branch
    sm = StateManager(repo_path)
    # Derive the session id that will be used
    import uuid
    session_id = uuid.uuid4().hex[:6]
    session_branch = f"forge/session-{session_id}"
    try:
        create_session_branch(repo, session_branch, base_branch)
    except GitError as e:
        raise click.ClickException(f"Git error: {e}")

    # Create session state (re-use session_id so branch matches)
    # We patch create() to accept an id override by building manually
    from .state import ForgeSession, Task, TaskStatus as TS
    from datetime import datetime
    now = datetime.utcnow().isoformat() + "Z"
    task_objs = [
        Task(
            id=i + 1,
            title=t["title"],
            description=t["description"],
            status=TS.PENDING,
            branch=f"forge/task-{i + 1}",
            created_at=now,
        )
        for i, t in enumerate(tasks)
    ]
    session = ForgeSession(
        id=session_id,
        project_name=cfg.project.name,
        session_branch=session_branch,
        base_branch=base_branch,
        tasks=task_objs,
        created_at=now,
    )
    sm.save(session)

    click.echo(f"\nSession {session_id} started on branch {session_branch}\n")

    # Run
    runner = ForgeRunner(cfg, sm, repo_path, verbose=verbose)
    try:
        session = asyncio.run(runner.run_session(session))
    except KeyboardInterrupt:
        click.echo("\nInterrupted. Run 'forge resume' to continue.")
        sys.exit(1)

    _print_summary(session)


# ------------------------------------------------------------------ #
# forge resume                                                        #
# ------------------------------------------------------------------ #

@cli.command()
@click.option("--config", "-c", default="forge.yaml", show_default=True)
@click.option("--verbose", "-v", is_flag=True)
def resume(config, verbose):
    """Resume an interrupted session."""
    try:
        cfg = load_config(config)
    except ConfigError as e:
        raise click.ClickException(str(e))

    repo_path = _repo_path()
    sm = StateManager(repo_path)

    try:
        session = sm.load()
    except StateError as e:
        raise click.ClickException(str(e))

    if session.status == "completed":
        click.echo(f"Session {session.id} is already completed.")
        return

    click.echo(f"Resuming session {session.id}...")
    runner = ForgeRunner(cfg, sm, repo_path, verbose=verbose)
    try:
        session = asyncio.run(runner.resume_session(session))
    except KeyboardInterrupt:
        click.echo("\nInterrupted. Run 'forge resume' again to continue.")
        sys.exit(1)

    _print_summary(session)


# ------------------------------------------------------------------ #
# forge status                                                        #
# ------------------------------------------------------------------ #

@cli.command()
def status():
    """Show current session status."""
    repo_path = _repo_path()
    sm = StateManager(repo_path)

    try:
        session = sm.load()
    except StateError as e:
        raise click.ClickException(str(e))

    # Header
    status_color = "green" if session.status == "completed" else (
        "red" if session.status == "failed" else "yellow"
    )
    click.echo(f"\nSession  : {session.id}")
    click.echo(f"Project  : {session.project_name}")
    click.echo(f"Branch   : {session.base_branch} → {session.session_branch}")
    click.echo(f"Status   : {click.style(session.status, fg=status_color)}")
    click.echo(f"Created  : {session.created_at}")

    # Task table
    click.echo(f"\n{'#':<4} {'Title':<40} {'Status':<12} {'Retries':<8} {'Reviews'}")
    click.echo("─" * 72)
    for t in session.tasks:
        color = _task_color(t.status)
        status_str = click.style(f"{t.status.value:<12}", fg=color)
        title = t.title[:38] + ".." if len(t.title) > 40 else t.title
        click.echo(f"{t.id:<4} {title:<40} {status_str} {t.retries:<8} {len(t.review_results)}")

    # Counts
    accepted = sum(1 for t in session.tasks if t.status == TaskStatus.ACCEPTED)
    escalated = sum(1 for t in session.tasks if t.status == TaskStatus.ESCALATED)
    failed = sum(1 for t in session.tasks if t.status == TaskStatus.FAILED)
    pending = sum(1 for t in session.tasks if t.status == TaskStatus.PENDING)
    click.echo(f"\n  {click.style(str(accepted), fg='green')} accepted  "
               f"{click.style(str(escalated), fg='cyan')} escalated  "
               f"{click.style(str(failed), fg='red')} failed  "
               f"{click.style(str(pending), fg='white', dim=True)} pending")


# ------------------------------------------------------------------ #
# forge report                                                        #
# ------------------------------------------------------------------ #

@cli.command()
def report():
    """Print and save a full session report."""
    repo_path = _repo_path()
    sm = StateManager(repo_path)

    try:
        session = sm.load()
    except StateError as e:
        raise click.ClickException(str(e))

    lines = []

    def out(s: str = "") -> None:
        click.echo(s)
        lines.append(s)

    out("=" * 60)
    out(f"Forge Report — Session {session.id}")
    out("=" * 60)
    out(f"Project  : {session.project_name}")
    out(f"Branch   : {session.base_branch} → {session.session_branch}")
    out(f"Status   : {session.status}")
    out(f"Started  : {session.created_at}")
    out(f"Finished : {session.completed_at or 'in progress'}")
    out()

    accepted = escalated = failed = 0

    for t in session.tasks:
        out(f"Task {t.id}: {t.title}")
        out(f"  Status  : {t.status.value}")
        out(f"  Branch  : {t.branch}")
        if t.error:
            out(f"  Error   : {t.error}")
        if t.review_results:
            out(f"  Reviews :")
            for r in t.review_results:
                verdict = "APPROVED" if r.approved else "REJECTED"
                out(f"    [{r.attempt}] {verdict}: {r.feedback[:200]}")
        out()

        if t.status == TaskStatus.ACCEPTED:
            accepted += 1
        elif t.status == TaskStatus.ESCALATED:
            escalated += 1
        elif t.status == TaskStatus.FAILED:
            failed += 1

    out("─" * 60)
    out(f"Accepted : {accepted}")
    out(f"Escalated: {escalated}")
    out(f"Failed   : {failed}")
    total_retries = sum(t.retries for t in session.tasks)
    out(f"Retries  : {total_retries}")
    out("=" * 60)

    report_path = os.path.join(repo_path, ".forge", "report.txt")
    with open(report_path, "w") as f:
        f.write("\n".join(lines))
    click.echo(f"\nSaved to {report_path}")


# ------------------------------------------------------------------ #
# forge init (bonus: creates forge.yaml)                             #
# ------------------------------------------------------------------ #

@cli.command()
@click.argument("name", required=False)
def init(name):
    """Create a forge.yaml config file in the current directory."""
    if os.path.exists("forge.yaml"):
        if not click.confirm("forge.yaml already exists. Overwrite?", default=False):
            return
    project_name = name or os.path.basename(os.getcwd())
    with open("forge.yaml", "w") as f:
        f.write(generate_template(project_name))
    click.echo(f"Created forge.yaml for project '{project_name}'.")


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

def _print_summary(session) -> None:
    accepted = sum(1 for t in session.tasks if t.status == TaskStatus.ACCEPTED)
    escalated = sum(1 for t in session.tasks if t.status == TaskStatus.ESCALATED)
    failed = sum(1 for t in session.tasks if t.status == TaskStatus.FAILED)
    total = len(session.tasks)
    click.echo(f"\n{'─'*40}")
    click.echo(f"Session {session.id} {click.style(session.status, fg='green' if session.status == 'completed' else 'red')}")
    click.echo(f"{accepted}/{total} accepted  {escalated} escalated  {failed} failed")
    click.echo(f"Run 'forge report' for details.")
