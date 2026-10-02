"""Restricted GitHub App PR broker. Do not expose this service to the public edge."""

import asyncio
import base64
import hashlib
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import jwt
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)
API = "https://api.github.com"
REPO = "Caos-Ordenado/portfolio"
REVIEW_REPOS = {"portfolio": REPO, "infra": "Caos-Ordenado/infra"}
PREFIX = "services/openwebui_tools/src/"
BRANCH_PREFIX = "agent/openwebui-tools/"
BOT_LOGIN = "home-lab-terminal-app[bot]"
MAX_BODY = 256 * 1024
MAX_BYTES = 64 * 1024
SHA = re.compile(r"[a-fA-F0-9]{40}")
REVIEW_BRANCH_PREFIX = "agent/review/"
TEXT_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".vue", ".css", ".scss", ".html", ".json", ".md", ".yaml", ".yml", ".sh", ".svg"}
PORTFOLIO_SOURCE = (
    "services/renderer/", "services/web_crawler/", "services/openwebui_tools/",
    "agents/product_search_agent/", "shared/shared/shared/",
)
PORTFOLIO_K8S_WORKLOADS = {"renderer", "web_crawler", "llama"}
INFRA_SITES = ("services/web/", "services/web_espacioraizconsciente/")
SITE_DIRS = ("pages/", "components/", "server/", "assets/")
DENIED_PARTS = {".github", ".git", ".nuxt", ".output", "node_modules", "dist", "build",
                "__pycache__", "secrets", "certs", "credentials", "generated", "vendor"}
DENIED_NAMES = {"agents.md", ".gitmodules", "dockerfile", "package-lock.json", "pnpm-lock.yaml"}
SENSITIVE = re.compile(r"(?i)(secret|credential|password|private[_-]?key|token|webhook|auth|\.env|\.pem|\.key|\.p12|\.pfx)")
KEY_MATERIAL = re.compile(r"-----BEGIN (?:[A-Z ]*PRIVATE KEY|OPENSSH PRIVATE KEY)-----")


def review_repository() -> str:
    name = os.environ.get("REVIEW_REPOSITORY", "")
    if name not in REVIEW_REPOS:
        raise RuntimeError("invalid review repository configuration")
    return name


def allowed_review_path(repo: str, path: str) -> bool:
    if repo not in REVIEW_REPOS or not path or "\\" in path or not path.isascii() or any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        return False
    parts = path.split("/")
    if any(not part or part in (".", "..") or part.startswith(".") or part.casefold() in DENIED_PARTS or SENSITIVE.search(part) for part in parts):
        return False
    name = parts[-1]
    if name.casefold() in DENIED_NAMES or name.casefold().startswith("dockerfile") or (name.casefold().startswith("deploy") and name.casefold().endswith(".sh")) or Path(name).suffix.lower() not in TEXT_EXTENSIONS:
        return False
    if repo == "portfolio":
        if path.startswith("k8s/"):
            return len(parts) >= 3 and parts[1] in PORTFOLIO_K8S_WORKLOADS and Path(name).suffix.lower() in {".yaml", ".yml", ".md"}
        return Path(name).suffix == ".py" and (
            any(path.startswith(root + directory) for root in PORTFOLIO_SOURCE[:-1] for directory in ("src/", "tests/"))
            or path.startswith("shared/shared/shared/") or path.startswith("shared/shared/tests/")
        )
    if path.startswith("k8s/") or path.startswith("hosting/k3s/"):
        return Path(name).suffix.lower() in {".yaml", ".yml", ".md"}
    if path.startswith("hosting/"):
        return len(parts) == 2 and Path(name).suffix.lower() in {".sh", ".md", ".yaml", ".yml"}
    return any(path.startswith(site + directory) for site in INFRA_SITES for directory in SITE_DIRS) and Path(name).suffix.lower() in TEXT_EXTENSIONS - {".py", ".sh", ".md", ".yaml", ".yml"}


class ReviewFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    content: str


class ReviewProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(max_length=16000)
    message: str = Field(min_length=1, max_length=200)
    expected_base_sha: str
    files: list[ReviewFile] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def check_files(self) -> "ReviewProposal":
        if not SHA.fullmatch(self.expected_base_sha):
            raise ValueError("invalid base SHA")
        if len({f.path for f in self.files}) != len(self.files):
            raise ValueError("duplicate paths")
        if not self.title.strip() or not self.message.strip():
            raise ValueError("blank title or message")
        try:
            total = sum(len(f.content.encode("utf-8")) for f in self.files)
        except UnicodeEncodeError:
            raise ValueError("contents must be UTF-8 encodable") from None
        if total > MAX_BYTES or any(
            KEY_MATERIAL.search(f.content) or any(ord(ch) < 32 and ch not in "\n\r\t" or ord(ch) == 127 for ch in f.content)
            for f in self.files
        ):
            raise ValueError("invalid file content")
        return self


class ProposalFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    content: str

    @field_validator("path")
    @classmethod
    def valid_path(cls, path: str) -> str:
        if not path.startswith(PREFIX) or "\\" in path or "\x00" in path:
            raise ValueError("path outside allowed source directory")
        parts = path.split("/")
        if any(part in ("", ".", "..") or part.startswith(".") for part in parts):
            raise ValueError("invalid path component")
        if not parts[-1].endswith(".py") or len(parts[-1]) <= 3:
            raise ValueError("only Python source files are allowed")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
            raise ValueError("invalid path character")
        return path


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(max_length=16000)
    message: str = Field(min_length=1, max_length=200)
    files: list[ProposalFile] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def check_files(self) -> "Proposal":
        if len({f.path for f in self.files}) != len(self.files):
            raise ValueError("duplicate paths")
        try:
            total = sum(len(f.content.encode("utf-8")) for f in self.files)
        except UnicodeEncodeError:
            raise ValueError("contents must be UTF-8 encodable") from None
        if total > MAX_BYTES:
            raise ValueError("file contents exceed 64 KiB")
        if not self.title.strip() or not self.message.strip():
            raise ValueError("title and message must not be blank")
        return self


class UpstreamError(Exception):
    pass


class StaleBaseError(Exception):
    pass


def check_review_base(proposal: ReviewProposal, repository: str, live_sha: str) -> None:
    expected = proposal.expected_base_sha.lower()
    if repository == "infra":
        snapshot = os.environ.get("REVIEW_SNAPSHOT_SHA", "")
        if not SHA.fullmatch(snapshot):
            raise RuntimeError("infra review snapshot unavailable")
        if snapshot.lower() != expected:
            raise StaleBaseError("snapshot does not match proposal")
    if live_sha.lower() != expected:
        raise StaleBaseError("main has changed")


def require_sha(value: Any) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise UpstreamError("invalid GitHub SHA")
    return value


def git_blob_sha(content: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()


def app_jwt() -> str:
    app_id = os.environ.get("GITHUB_APP_ID", "")
    pem_path = os.environ.get("GITHUB_APP_PEM_PATH", "")
    if not app_id.isdecimal() or not pem_path:
        raise RuntimeError("GitHub App configuration unavailable")
    key = Path(pem_path).read_bytes()
    now = int(time.time())
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": app_id}, key, algorithm="RS256")


class GitHub:
    def __init__(self, client: httpx.AsyncClient, repository: str = "portfolio"):
        if repository not in REVIEW_REPOS:
            raise ValueError("invalid repository")
        self.client = client
        self.repository = repository

    async def request(self, method: str, path: str, token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = await self.client.request(
                method, API + path,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28"}, json=payload,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise UpstreamError("invalid GitHub response")
            return data
        except (httpx.HTTPError, ValueError):
            # Never include response bodies, request objects, or headers (contain credentials).
            raise UpstreamError("GitHub request failed") from None

    async def request_list(self, path: str, token: str) -> list[dict[str, Any]]:
        try:
            response = await self.client.get(
                API + path,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                raise UpstreamError("invalid GitHub list")
            return data
        except (httpx.HTTPError, ValueError):
            raise UpstreamError("GitHub list request failed") from None

    async def installation_token(self) -> str:
        installation = os.environ.get("GITHUB_INSTALLATION_ID", "")
        if not installation.isdecimal():
            raise RuntimeError("GitHub installation configuration unavailable")
        data = await self.request("POST", f"/app/installations/{installation}/access_tokens", app_jwt(),
                                   {"repositories": [self.repository], "permissions": {"contents": "write", "pull_requests": "write"}})
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise UpstreamError("invalid installation token response")
        return token

    async def request_auto_merge(self, token: str, node_id: str) -> bool:
        if not isinstance(node_id, str) or not node_id:
            return False
        try:
            result = await self.request("POST", "/graphql", token, {
                "query": "mutation($id: ID!) { enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: SQUASH}) { pullRequest { autoMergeRequest { enabledAt } } } }",
                "variables": {"id": node_id},
            })
            return bool(
                not result.get("errors")
                and (result.get("data") or {}).get("enablePullRequestAutoMerge", {})
                .get("pullRequest", {}).get("autoMergeRequest", {}).get("enabledAt")
            )
        except (UpstreamError, TypeError, AttributeError):
            logger.warning("auto_merge_not_queued")
            return False

    async def retry_ready_auto_merges(self) -> None:
        """Re-request auto-merge once GitHub reports all protected gates clean."""
        if self.repository != "portfolio" or review_repository() != "portfolio":
            raise RuntimeError("auto-merge unavailable in this deployment")
        token = await self.installation_token()
        root = f"/repos/{REPO}"
        prs: list[dict[str, Any]] = []
        for page in range(1, 31):
            batch = await self.request_list(
                f"{root}/pulls?state=open&base=main&per_page=100&page={page}", token
            )
            if len(batch) > 100:
                raise UpstreamError("invalid PR page")
            prs.extend(batch)
            if len(prs) >= 3000:  # GitHub caps PR file listings at 3000 too.
                logger.warning("auto_merge_pr_window_full")
                return
            if len(batch) < 100:
                break
        # Finish pagination before evaluating any candidate. A failed page
        # must not turn a partial result into an authorization decision.
        for pr in prs:
            head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
            base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
            head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
            base_repo = base.get("repo") if isinstance(base.get("repo"), dict) else {}
            number = pr.get("number")
            if not (
                pr.get("state") == "open" and not pr.get("draft")
                and isinstance(pr.get("user"), dict) and pr["user"].get("login") == BOT_LOGIN
                and type(number) is int and number > 0
                and isinstance(head.get("ref"), str) and head["ref"].startswith(BRANCH_PREFIX)
                and base.get("ref") == "main"
                and head_repo.get("full_name") == REPO == base_repo.get("full_name")
                and head_repo.get("id") == base_repo.get("id")
            ):
                continue
            files = await self.request_list(f"{root}/pulls/{number}/files?per_page=100", token)
            if not files or len(files) >= 100:
                continue
            if any(
                not isinstance(path, str) or not path.startswith(PREFIX) or len(path) == len(PREFIX)
                for item in files
                for path in ([item.get("filename")] + ([item["previous_filename"]] if "previous_filename" in item else []))
            ):
                continue
            latest = await self.request("GET", f"{root}/pulls/{number}", token)
            latest_head = latest.get("head") if isinstance(latest.get("head"), dict) else {}
            latest_base = latest.get("base") if isinstance(latest.get("base"), dict) else {}
            latest_head_repo = latest_head.get("repo") if isinstance(latest_head.get("repo"), dict) else {}
            latest_base_repo = latest_base.get("repo") if isinstance(latest_base.get("repo"), dict) else {}
            if not (
                latest.get("state") == "open" and latest.get("mergeable_state") == "clean"
                and isinstance(latest.get("user"), dict) and latest["user"].get("login") == BOT_LOGIN
                and latest_head.get("sha") == head.get("sha")
                and latest_base.get("sha") == base.get("sha")
                and latest_head.get("ref") == head.get("ref")
                and latest_base.get("ref") == "main"
                and latest_head_repo.get("full_name") == REPO == latest_base_repo.get("full_name")
                and latest_head_repo.get("id") == latest_base_repo.get("id")
            ):
                continue
            if await self.request_auto_merge(token, latest.get("node_id")):
                logger.info("auto_merge_ready_pr=%s", number)

    async def create_proposal(self, proposal: Proposal) -> dict[str, Any]:
        if self.repository != "portfolio" or review_repository() != "portfolio":
            raise RuntimeError("legacy proposals unavailable in this deployment")
        return await self._create_pr(proposal, REPO, BRANCH_PREFIX, auto_merge=True)

    async def create_review_proposal(self, proposal: ReviewProposal) -> dict[str, Any]:
        if self.repository != review_repository():
            raise RuntimeError("review repository mismatch")
        if any(not allowed_review_path(self.repository, file.path) for file in proposal.files):
            raise ValueError("review path not allowed")
        if self.repository == "infra":
            # Reject missing/invalid operator configuration before requesting a token.
            check_review_base(proposal, self.repository, proposal.expected_base_sha)
        return await self._create_pr(proposal, REVIEW_REPOS[self.repository], REVIEW_BRANCH_PREFIX, auto_merge=False)

    async def _create_pr(self, proposal: Proposal | ReviewProposal, repo: str, branch_prefix: str, *, auto_merge: bool) -> dict[str, Any]:
        token = await self.installation_token()
        root = f"/repos/{repo}"
        ref = await self.request("GET", f"{root}/git/ref/heads/main", token)
        base_sha = require_sha(ref.get("object", {}).get("sha") if isinstance(ref.get("object"), dict) else None)
        if isinstance(proposal, ReviewProposal):
            check_review_base(proposal, self.repository, base_sha)
        commit = await self.request("GET", f"{root}/git/commits/{base_sha}", token)
        tree_sha = require_sha(commit.get("tree", {}).get("sha") if isinstance(commit.get("tree"), dict) else None)
        tree = await self.request("GET", f"{root}/git/trees/{tree_sha}?recursive=1", token)
        if tree.get("truncated") is not False or not isinstance(tree.get("tree"), list):
            raise UpstreamError("incomplete GitHub tree")
        entries: dict[str, dict[str, Any]] = {}
        for entry in tree["tree"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise UpstreamError("invalid tree entry")
            entries[entry["path"]] = entry
        changes: list[tuple[ProposalFile, str]] = []
        for file in proposal.files:
            parts = file.path.split("/")
            for i in range(1, len(parts)):
                parent = entries.get("/".join(parts[:i]))
                if parent is not None and (parent.get("type"), parent.get("mode")) != ("tree", "040000"):
                    raise UpstreamError("non-directory path component")
            existing = entries.get(file.path)
            mode = "100644"
            if existing is not None:
                if existing.get("type") != "blob" or existing.get("mode") not in ("100644", "100755"):
                    raise UpstreamError("non-regular target")
                mode = existing["mode"]
                if require_sha(existing.get("sha")) == git_blob_sha(file.content.encode("utf-8")):
                    continue
            changes.append((file, mode))
        if not changes:
            raise ValueError("proposal contains no changes")
        if isinstance(proposal, ReviewProposal):
            current = await self.request("GET", f"{root}/git/ref/heads/main", token)
            current_sha = require_sha(current.get("object", {}).get("sha") if isinstance(current.get("object"), dict) else None)
            check_review_base(proposal, self.repository, current_sha)
            if current_sha.lower() != base_sha.lower():
                raise StaleBaseError("main has changed")
        new_entries = []
        for file, mode in changes:
            blob = await self.request("POST", f"{root}/git/blobs", token,
                                      {"content": base64.b64encode(file.content.encode("utf-8")).decode("ascii"),
                                       "encoding": "base64"})
            new_entries.append({"path": file.path, "mode": mode, "type": "blob", "sha": require_sha(blob.get("sha"))})
        new_tree = await self.request("POST", f"{root}/git/trees", token,
                                      {"base_tree": tree_sha, "tree": new_entries})
        new_commit = await self.request("POST", f"{root}/git/commits", token,
                                        {"message": proposal.message, "tree": require_sha(new_tree.get("sha")),
                                         "parents": [base_sha]})
        branch = branch_prefix + secrets.token_hex(12)
        await self.request("POST", f"{root}/git/refs", token,
                           {"ref": f"refs/heads/{branch}", "sha": require_sha(new_commit.get("sha"))})
        # If PR creation fails, best-effort remove the orphan branch; never mask the original error.
        try:
            pr = await self.request("POST", f"{root}/pulls", token,
                                    {"title": proposal.title, "body": proposal.body, "head": branch, "base": "main"})
            url = pr.get("html_url")
            number = pr.get("number")
            if not isinstance(url, str) or not url.startswith(f"https://github.com/{repo}/pull/") or type(number) is not int:
                raise UpstreamError("invalid pull request response")
        except UpstreamError:
            try:
                await self.request("DELETE", f"{root}/git/refs/heads/{branch}", token)
            except UpstreamError:
                logger.warning("orphan_branch_cleanup_failed")
            raise

        # The App token (unlike GITHUB_TOKEN in pull_request_target) may request
        # auto-merge. GitHub still requires protected-branch checks and review.
        # An auto-merge error must never delete an already published PR/branch.
        queued = False
        if auto_merge:
            queued = await self.request_auto_merge(token, pr.get("node_id"))
            if not queued:
                logger.warning("auto_merge_not_queued")
        return {"url": url, "number": number, "branch": branch, "auto_merge_queued": queued}


async def poll_auto_merges() -> None:
    while True:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), follow_redirects=False) as client:
                await GitHub(client).retry_ready_auto_merges()
        except (UpstreamError, RuntimeError, OSError, jwt.PyJWTError, asyncio.TimeoutError, ValueError, TypeError):
            logger.warning("auto_merge_poller_failed")
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(poll_auto_merges()) if review_repository() == "portfolio" else None
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Code agent PR broker", lifespan=lifespan)


@app.middleware("http")
async def limit_body(request: Request, call_next: Any) -> Any:
    if request.url.path in ("/proposals", "/review-proposals"):
        size = 0
        chunks = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY:
                return JSONResponse({"detail": "request too large"}, status_code=413)
            chunks.append(chunk)
        request._body = b"".join(chunks)
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
    return JSONResponse({"detail": "invalid proposal"}, status_code=422)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/proposals")
async def proposals(proposal: Proposal, request: Request) -> dict[str, Any]:
    if review_repository() != "portfolio":
        raise HTTPException(status_code=404, detail="not found")
    try:
        # No caller-controlled URL, repository or ref reaches the client.
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), follow_redirects=False) as client:
            github = getattr(request.app.state, "github", None) or GitHub(client)
            return await github.create_proposal(proposal)
    except ValueError:
        raise HTTPException(status_code=422, detail="proposal contains no changes") from None
    except (UpstreamError, RuntimeError, OSError, jwt.PyJWTError, asyncio.TimeoutError):
        logger.warning("proposal_creation_failed", exc_info=False)
        raise HTTPException(status_code=502, detail="proposal could not be created") from None


@app.post("/review-proposals")
async def review_proposals(proposal: ReviewProposal, request: Request) -> dict[str, Any]:
    repo = review_repository()
    if any(not allowed_review_path(repo, file.path) for file in proposal.files):
        raise HTTPException(status_code=422, detail="invalid proposal")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), follow_redirects=False) as client:
            github = getattr(request.app.state, "github", None) or GitHub(client, repo)
            return await github.create_review_proposal(proposal)
    except StaleBaseError:
        raise HTTPException(status_code=409, detail="main has changed") from None
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid proposal") from None
    except (UpstreamError, RuntimeError, OSError, jwt.PyJWTError, asyncio.TimeoutError):
        logger.warning("review_proposal_creation_failed", exc_info=False)
        raise HTTPException(status_code=502, detail="proposal could not be created") from None
