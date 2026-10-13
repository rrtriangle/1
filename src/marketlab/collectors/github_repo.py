"""GitHub 저장소의 파일 목록(경로·blob sha·크기)을 조회한다.

전체 git clone 대신 필요한 파일만 raw URL 로 받는다 (중복 저장 없음, 변경 파일만 재다운로드).
- 1순위: GitHub REST API trees (인증 불필요, 시간당 60회 제한 → 실행당 1회만 호출)
- 2순위: 깊이 1 · blob 제외 git clone 으로 트리만 받아 `git ls-tree` (용량 수 MB)
blob sha 는 다운로드한 파일의 git_blob_sha1 과 대조해 무결성을 검증하는 데 쓴다.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import get_settings
from ..logging_setup import get_logger, log_event
from ..net.http import HttpClient

log = get_logger("github")


@dataclass(frozen=True)
class RemoteFile:
    path: str
    sha: str
    size: int | None


def _via_api(client: HttpClient, api_base: str, repo: str, branch: str) -> tuple[str, list[RemoteFile]]:
    r = client.get(f"{api_base}/repos/{repo}/git/trees/{branch}?recursive=1",
                   headers={"Accept": "application/vnd.github+json"})
    if r.status_code != 200:
        raise RuntimeError(f"GitHub API HTTP {r.status_code}")
    js = r.json()
    if js.get("truncated"):
        raise RuntimeError("GitHub API tree truncated")
    files = [RemoteFile(e["path"], e["sha"], e.get("size")) for e in js.get("tree", []) if e.get("type") == "blob"]
    return js.get("sha", ""), files


def _via_git(repo: str, branch: str, workdir: Path) -> tuple[str, list[RemoteFile]]:
    if not shutil.which("git"):
        raise RuntimeError("git 미설치")
    target = workdir / repo.replace("/", "__")
    url = f"https://github.com/{repo}.git"
    if target.exists():
        subprocess.run(["git", "-C", str(target), "fetch", "-q", "--depth", "1", "--filter=blob:none",
                        "origin", branch], check=True, timeout=600)
        ref = "FETCH_HEAD"
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--no-checkout",
                        "--branch", branch, url, str(target)], check=True, timeout=900)
        ref = "HEAD"
    head = subprocess.run(["git", "-C", str(target), "rev-parse", ref], check=True, capture_output=True,
                          text=True).stdout.strip()
    out = subprocess.run(["git", "-C", str(target), "ls-tree", "-r", ref], check=True, capture_output=True,
                         text=True, timeout=300).stdout
    files = []
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        _mode, typ, sha = meta.split()
        if typ == "blob":
            files.append(RemoteFile(path, sha, None))  # 크기는 blob 을 받아야 알 수 있어 생략
    return head, files


def list_repo_files(repo: str, branch: str, client: HttpClient, api_base: str = "https://api.github.com"
                    ) -> tuple[str, list[RemoteFile], str]:
    """반환: (commit/tree sha, 파일 목록, 사용한 방법)"""
    try:
        sha, files = _via_api(client, api_base, repo, branch)
        return sha, files, "github_api"
    except Exception as e:
        log_event(log, "GitHub API 실패, git 트리 조회로 대체", repo=repo, error=str(e))
    workdir = get_settings().paths.tmp / "git_trees"
    sha, files = _via_git(repo, branch, workdir)
    return sha, files, "git_ls_tree"


def remote_size(client: HttpClient, url: str) -> int | None:
    try:
        r = client.head(url)
        if r.status_code == 200 and r.headers.get("Content-Length"):
            return int(r.headers["Content-Length"])
    except Exception:
        pass
    return None
