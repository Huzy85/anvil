import os
import git
from git.exc import GitCommandError


class GitError(Exception):
    pass


def _clean(exc) -> str:
    msg = str(exc)
    for noise in ("fatal: ", "error: ", "hint: "):
        msg = msg.replace(noise, "")
    return msg.strip()


def get_repo(repo_path: str) -> git.Repo:
    try:
        return git.Repo(repo_path, search_parent_directories=False)
    except git.exc.InvalidGitRepositoryError:
        raise GitError(f"Not a git repository: {repo_path}")
    except Exception as e:
        raise GitError(f"Failed to open repo: {_clean(e)}")


def find_repo_root(path: str = ".") -> str:
    current = os.path.abspath(path)
    while True:
        if os.path.isdir(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            raise GitError(f"No git repository found at or above: {os.path.abspath(path)}")
        current = parent


def get_current_branch(repo: git.Repo) -> str:
    try:
        return repo.active_branch.name
    except TypeError:
        raise GitError("Repository is in detached HEAD state")
    except Exception as e:
        raise GitError(f"Failed to get current branch: {_clean(e)}")


def branch_exists(repo: git.Repo, branch: str) -> bool:
    return branch in [b.name for b in repo.branches]


def checkout(repo: git.Repo, branch: str) -> None:
    try:
        repo.git.checkout(branch)
    except GitCommandError as e:
        raise GitError(f"Failed to checkout '{branch}': {_clean(e)}")


def create_session_branch(repo: git.Repo, session_branch: str, base_branch: str) -> None:
    try:
        checkout(repo, base_branch)
        try:
            repo.remotes.origin.pull(base_branch)
        except Exception:
            pass  # no remote or offline — fine
        repo.create_head(session_branch, base_branch)
        repo.git.checkout(session_branch)
    except GitError:
        raise
    except Exception as e:
        raise GitError(f"Failed to create session branch: {_clean(e)}")


def create_task_branch(repo: git.Repo, task_branch: str, from_branch: str) -> None:
    try:
        checkout(repo, from_branch)
        repo.create_head(task_branch, from_branch)
        repo.git.checkout(task_branch)
    except GitError:
        raise
    except Exception as e:
        raise GitError(f"Failed to create task branch: {_clean(e)}")


def commit_all(repo: git.Repo, message: str) -> str:
    try:
        repo.git.add("-A")
        commit = repo.index.commit(message)
        return commit.hexsha
    except GitCommandError as e:
        raise GitError(f"Failed to commit: {_clean(e)}")


def get_diff(repo: git.Repo, base_branch: str, task_branch: str) -> str:
    try:
        return repo.git.diff(base_branch, task_branch, unified=3)
    except GitCommandError as e:
        raise GitError(f"Failed to get diff: {_clean(e)}")


def merge_task_branch(repo: git.Repo, session_branch: str, task_branch: str) -> None:
    try:
        checkout(repo, session_branch)
        repo.git.merge(task_branch, no_ff=True, m=f"Merge task: {task_branch}")
    except GitCommandError as e:
        raise GitError(f"Failed to merge '{task_branch}': {_clean(e)}")
