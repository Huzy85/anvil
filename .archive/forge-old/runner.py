import asyncio
from datetime import datetime
from typing import Optional, List, Tuple, Any

from .config import ForgeConfig
from .state import StateManager, ForgeSession, Task, TaskStatus, ReviewResult
from .git_ops import (
    get_repo,
    create_task_branch,
    commit_all,
    get_diff,
    merge_task_branch,
    checkout,
    GitError,
)
from .worker import run_task as worker_run_task, WorkerResult
from .reviewer import review_task, escalate_and_fix, ReviewDecision


class RunnerError(Exception):
    pass


class ForgeRunner:
    def __init__(
        self,
        config: ForgeConfig,
        state_manager: StateManager,
        repo_path: str,
        verbose: bool = False,
    ):
        self.config = config
        self.sm = state_manager
        self.repo_path = repo_path
        self.repo = get_repo(repo_path)
        self.verbose = verbose
        self._git_lock = asyncio.Lock()  # serializes git ops — GitPython blocks the event loop

    def log(self, msg: str) -> None:
        print(msg)

    def debug(self, msg: str) -> None:
        if self.verbose:
            print(f"  [debug] {msg}")

    # ------------------------------------------------------------------ #
    # Public entry point                                                   #
    # ------------------------------------------------------------------ #

    async def run_session(self, session: ForgeSession) -> ForgeSession:
        self.log(f"\nForge session {session.id} — {len(session.tasks)} tasks")
        self.log(f"Branch: {session.session_branch}\n")
        try:
            if self.config.session.concurrent:
                session = await self._run_concurrent(session)
            else:
                session = await self._run_sequential(session)
        except Exception as e:
            self.log(f"[runner] fatal: {e}")
            session.status = "failed"
            self.sm.save(session)
            raise

        all_done = all(
            t.status in (TaskStatus.ACCEPTED, TaskStatus.ESCALATED)
            for t in session.tasks
        )
        session.status = "completed" if all_done else "failed"
        session.completed_at = datetime.utcnow().isoformat() + "Z"
        self.sm.save(session)
        return session

    # ------------------------------------------------------------------ #
    # Sequential mode                                                      #
    # ------------------------------------------------------------------ #

    async def _run_sequential(self, session: ForgeSession) -> ForgeSession:
        for task in session.tasks:
            if task.status != TaskStatus.PENDING:
                continue
            ok = await self._execute_task(session, task)
            if not ok:
                self.log(f"[task {task.id}] failed — stopping session.")
                break
        return session

    # ------------------------------------------------------------------ #
    # Concurrent mode (pipeline: code N+1 while reviewing N)              #
    # ------------------------------------------------------------------ #

    async def _run_concurrent(self, session: ForgeSession) -> ForgeSession:
        """Pipeline: LLM call for task N+1 overlaps with Claude review of task N.
        Git ops are serialized via _git_lock (GitPython blocks the event loop).
        If a review fails the pipeline stops — incomplete next-task coding is
        reset to PENDING so 'forge resume' can retry cleanly."""
        pending = [t for t in session.tasks if t.status == TaskStatus.PENDING]
        if not pending:
            return session

        review_future: Optional[asyncio.Task] = None
        pending_review_task: Optional[Task] = None

        for task in pending:
            code_future = asyncio.create_task(self._code_task(session, task))

            if review_future is not None and pending_review_task is not None:
                # Run LLM coding + Claude review concurrently
                results = await asyncio.gather(
                    code_future, review_future, return_exceptions=True
                )
                code_result, approved = results[0], results[1]
                review_future = None

                if isinstance(approved, Exception):
                    self.log(f"[task {pending_review_task.id}] review error: {approved}")
                    # Reset the just-coded task to PENDING for resume
                    if not isinstance(code_result, Exception) and code_result is not None:
                        task_obj, _ = code_result
                        self.sm.update_task(session, task_obj.id, status=TaskStatus.PENDING)
                    return session

                if not approved:
                    self.log(f"[task {pending_review_task.id}] rejected — stopping pipeline.")
                    # Reset the just-coded task to PENDING for resume
                    if not isinstance(code_result, Exception) and code_result is not None:
                        task_obj, _ = code_result
                        self.sm.update_task(session, task_obj.id, status=TaskStatus.PENDING)
                    return session

                pending_review_task = None
                if isinstance(code_result, Exception) or code_result is None:
                    return session
                code_result_for_review = code_result
            else:
                code_result_for_review = await code_future

            if code_result_for_review is None:
                return session

            task_obj, diff = code_result_for_review
            pending_review_task = task_obj
            review_future = asyncio.create_task(
                self._review_loop(session, task_obj, diff)
            )

        # Wait for the final review
        if review_future is not None:
            await review_future

        return session

    # ------------------------------------------------------------------ #
    # Core: code one task (worker agent loop + commit)                     #
    # ------------------------------------------------------------------ #

    async def _code_task(
        self, session: ForgeSession, task: Task
    ) -> Optional[Tuple[Task, str]]:
        """Run worker on task, commit result. Returns (task, diff) or None on failure."""
        session = self.sm.update_task(
            session, task.id, status=TaskStatus.RUNNING
        )
        task = next(t for t in session.tasks if t.id == task.id)

        self.log(f"\n[task {task.id}/{len(session.tasks)}] {task.title}")
        self.log(f"  branch: {task.branch}")

        # Create task branch from current session branch tip
        try:
            async with self._git_lock:
                create_task_branch(self.repo, task.branch, session.session_branch)
        except GitError as e:
            self.log(f"  [error] git: {e}")
            session = self.sm.update_task(
                session, task.id, status=TaskStatus.FAILED, error=str(e)
            )
            return None

        context = self._build_context(session, task.id)
        worker = self.config.default_worker

        def on_iter(n: int, label: str) -> None:
            self.debug(f"  iter {n}: {label}")

        self.log(f"  coding with {worker.name}...")
        result: WorkerResult = await worker_run_task(
            task_description=task.description,
            task_title=task.title,
            repo_path=self.repo_path,
            worker=worker,
            context=context,
            on_iteration=on_iter,
        )

        if not result.success:
            self.log(f"  [error] worker: {result.error}")
            session = self.sm.update_task(
                session, task.id, status=TaskStatus.FAILED, error=result.error
            )
            return None

        self.log(f"  worker done ({result.iterations} iters). Committing...")
        try:
            async with self._git_lock:
                sha = commit_all(self.repo, f"forge: task {task.id} — {task.title}")
                diff = get_diff(self.repo, session.session_branch, task.branch)
        except GitError as e:
            self.log(f"  [error] commit/diff: {e}")
            session = self.sm.update_task(
                session, task.id, status=TaskStatus.FAILED, error=str(e)
            )
            return None

        self.debug(f"  commit {sha[:8]}, diff {len(diff)} chars")
        return task, diff

    # ------------------------------------------------------------------ #
    # Core: review loop with retry + escalation                           #
    # ------------------------------------------------------------------ #

    async def _review_loop(self, session: ForgeSession, task: Task, diff: str) -> bool:
        """Review task diff. Retry up to max_retries, then escalate. Returns True if accepted."""
        max_retries = self.config.session.max_retries
        rejection_feedback: List[str] = []

        for attempt in range(1, max_retries + 2):  # +1 for final escalation attempt
            is_retry = attempt > 1
            is_escalation_attempt = attempt > max_retries

            if is_escalation_attempt:
                self.log(f"  [task {task.id}] escalating to Claude for direct fix...")
                decision = await escalate_and_fix(
                    task_title=task.title,
                    task_description=task.description,
                    diff=diff,
                    feedback_history=rejection_feedback,
                    repo_path=self.repo_path,
                    reviewer_config=self.config.reviewer,
                )
                session = self.sm.add_review(
                    session,
                    task.id,
                    ReviewResult(
                        approved=decision.approved,
                        feedback=decision.feedback,
                        attempt=attempt,
                    ),
                )
                if decision.approved:
                    await self._merge_and_accept(session, task, escalated=True)
                    self.log(f"  [task {task.id}] escalation OK — {decision.feedback[:80]}")
                    return True
                else:
                    self.log(f"  [task {task.id}] escalation failed: {decision.feedback[:120]}")
                    session = self.sm.update_task(
                        session, task.id, status=TaskStatus.FAILED, error=decision.feedback
                    )
                    return False

            # Regular review
            status_label = f"attempt {attempt}/{max_retries}" if is_retry else "reviewing"
            self.log(f"  [task {task.id}] {status_label}...")
            session = self.sm.update_task(session, task.id, status=TaskStatus.REVIEWING)

            decision = await review_task(
                task_title=task.title,
                task_description=task.description,
                diff=diff,
                reviewer_config=self.config.reviewer,
                attempt=attempt,
            )
            session = self.sm.add_review(
                session,
                task.id,
                ReviewResult(
                    approved=decision.approved,
                    feedback=decision.feedback,
                    attempt=attempt,
                ),
            )

            if decision.approved:
                await self._merge_and_accept(session, task, escalated=False)
                self.log(f"  [task {task.id}] APPROVED ✓")
                return True

            # Rejected — retry if attempts remain
            self.log(f"  [task {task.id}] REJECTED: {decision.feedback[:120]}")
            rejection_feedback.append(decision.feedback)

            if attempt <= max_retries:
                self.log(f"  [task {task.id}] retrying ({attempt}/{max_retries})...")
                session = self.sm.update_task(
                    session, task.id, status=TaskStatus.RUNNING, retries=attempt
                )
                # Ensure we're on the task branch before the worker writes anything
                try:
                    async with self._git_lock:
                        checkout(self.repo, task.branch)
                except GitError as e:
                    session = self.sm.update_task(
                        session, task.id, status=TaskStatus.FAILED, error=str(e)
                    )
                    return False

                retry_context = (
                    self._build_context(session, task.id)
                    + f"\n\nYour previous attempt was rejected. Feedback:\n{decision.feedback}\n"
                    "Fix the issues and call task_complete when done."
                )
                worker = self.config.default_worker
                result: WorkerResult = await worker_run_task(
                    task_description=task.description,
                    task_title=task.title,
                    repo_path=self.repo_path,
                    worker=worker,
                    context=retry_context,
                )
                if not result.success:
                    self.log(f"  [task {task.id}] retry worker failed: {result.error}")
                    session = self.sm.update_task(
                        session, task.id, status=TaskStatus.FAILED, error=result.error
                    )
                    return False

                try:
                    async with self._git_lock:
                        commit_all(self.repo, f"forge: task {task.id} retry {attempt}")
                        diff = get_diff(self.repo, session.session_branch, task.branch)
                except GitError as e:
                    session = self.sm.update_task(
                        session, task.id, status=TaskStatus.FAILED, error=str(e)
                    )
                    return False

        return False  # unreachable but satisfies type checker

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    async def _merge_and_accept(self, session: ForgeSession, task: Task, escalated: bool) -> None:
        status = TaskStatus.ESCALATED if escalated else TaskStatus.ACCEPTED
        try:
            async with self._git_lock:
                merge_task_branch(self.repo, session.session_branch, task.branch)
        except GitError as e:
            raise RunnerError(f"Merge failed for task {task.id}: {e}") from e
        self.sm.update_task(
            session,
            task.id,
            status=status,
            completed_at=datetime.utcnow().isoformat() + "Z",
        )

    def _build_context(self, session: ForgeSession, up_to_task_id: int) -> str:
        done = [
            t for t in session.tasks
            if t.id < up_to_task_id
            and t.status in (TaskStatus.ACCEPTED, TaskStatus.ESCALATED)
        ]
        if not done:
            return ""
        lines = ["Previously completed tasks:"]
        for t in done:
            summary = ""
            if t.review_results:
                # Use the last approved review's feedback as summary
                approved = [r for r in t.review_results if r.approved]
                if approved:
                    summary = f" — {approved[-1].feedback[:100]}"
            lines.append(f"  Task {t.id}: {t.title}{summary}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # resume: skip already-done tasks                                     #
    # ------------------------------------------------------------------ #

    async def resume_session(self, session: ForgeSession) -> ForgeSession:
        pending = [t for t in session.tasks if t.status == TaskStatus.PENDING]
        running = [t for t in session.tasks if t.status == TaskStatus.RUNNING]
        # Reset anything stuck in RUNNING/REVIEWING back to PENDING
        for t in running:
            session = self.sm.update_task(session, t.id, status=TaskStatus.PENDING)
        reviewing = [t for t in session.tasks if t.status == TaskStatus.REVIEWING]
        for t in reviewing:
            session = self.sm.update_task(session, t.id, status=TaskStatus.PENDING)

        self.log(f"Resuming session {session.id} — {len(pending)} tasks remaining")
        return await self.run_session(session)
