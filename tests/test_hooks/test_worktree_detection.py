"""Tests for worktree handling across locate and destructive command guard (#2424).

Covers:
- `locate` pruning custom nested worktrees from `scope="repo"` results while including them in `scope="worktrees"`.
- `locate` searching correctly when `GENESIS_REPO_ROOT` points directly to a linked worktree.
- `destructive_command_guard` applying uniform path depth checks to linked worktrees without exemptions.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from genesis.mcp.memory.locate import _impl_locate, _list_worktrees
from scripts.hooks.destructive_command_guard import _check_target


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "-C", str(path), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test User"], check=True)
    readme = path / "README.md"
    readme.write_text("# Main Repo\n")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "initial commit"], check=True)


def test_destructive_guard_does_not_exempt_worktrees():
    """Verify shallow linked worktrees (depth < 4) are refused like plain directories."""
    tmp_dir = Path(tempfile.mkdtemp(dir="/tmp"))
    repo = None
    wt_path = None
    try:
        repo = tmp_dir / "repo"
        repo.mkdir()
        _init_repo(repo)

        wt_path = tmp_dir / "wt"
        parts = [p for p in str(wt_path).split("/") if p]
        if len(parts) >= 4:
            pytest.skip(f"cannot create a shallow path (< 4 components): {wt_path} has depth {len(parts)}")

        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-b", "wt-branch", str(wt_path)],
            check=True, capture_output=True,
        )

        plain_dir = tmp_dir / "plain"
        plain_dir.mkdir()

        plain_reason = _check_target(str(plain_dir))
        assert plain_reason is not None and "too broad" in plain_reason

        wt_reason = _check_target(str(wt_path))
        assert wt_reason is not None and "too broad" in wt_reason
    finally:
        if repo and wt_path and wt_path.exists():
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt_path)], capture_output=True)
        shutil.rmtree(tmp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_locate_repo_root_that_is_a_linked_worktree(tmp_path, monkeypatch):
    """When GENESIS_REPO_ROOT points to a linked worktree, scope="repo" must still scan it."""
    main_repo = tmp_path / "main-repo"
    main_repo.mkdir()
    _init_repo(main_repo)

    wt_repo = tmp_path / "wt-repo"
    subprocess.run(
        ["git", "-C", str(main_repo), "worktree", "add", "-b", "wt-branch", str(wt_repo)],
        check=True, capture_output=True,
    )

    wt_file = wt_repo / "file_in_wt.md"
    wt_file.write_text("# File in WT\n")

    monkeypatch.setenv("GENESIS_REPO_ROOT", str(wt_repo))

    res_repo = await _impl_locate(scope="repo", within="0")
    names_repo = {r["name"] for r in res_repo["results"]}
    assert "file_in_wt.md" in names_repo


@pytest.mark.asyncio
async def test_locate_prunes_custom_nested_worktree(tmp_path, monkeypatch):
    """Create linked worktree inside main repo at a path outside prescribed directories.

    For example <repo>/elsewhere/feature-1, so scope="repo" would walk into it
    without the fix. Assert worktree_file.md is absent in repo scope and present in worktrees scope.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    wt_path = repo / "elsewhere" / "feature-1"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "feature-1", str(wt_path)],
        check=True, capture_output=True,
    )

    wt_file = wt_path / "worktree_file.md"
    wt_file.write_text("# Worktree File\n")

    monkeypatch.setenv("GENESIS_REPO_ROOT", str(repo))

    wts = _list_worktrees(repo)
    assert any(wt.resolve() == wt_path.resolve() for wt in wts)

    res_wts = await _impl_locate(scope="worktrees", within="0")
    names_wts = {r["name"] for r in res_wts["results"]}
    assert "worktree_file.md" in names_wts

    res_repo = await _impl_locate(scope="repo", within="0")
    names_repo = {r["name"] for r in res_repo["results"]}
    assert "worktree_file.md" not in names_repo
    assert "README.md" in names_repo
