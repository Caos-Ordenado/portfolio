import json

import httpx
import pytest

from broker.main import (GitHub, Proposal, REPO, ReviewProposal, StaleBaseError,
                         UpstreamError, allowed_review_path, app, git_blob_sha)

ROOT = f"/repos/{REPO}"
BASE = "a" * 40
TREE = "b" * 40
NEW_TREE = "c" * 40
NEW_COMMIT = "d" * 40


@pytest.fixture(autouse=True)
def portfolio_runtime(monkeypatch):
    monkeypatch.setenv("REVIEW_REPOSITORY", "portfolio")


def test_missing_review_repository_fails_closed(monkeypatch):
    from broker.main import review_repository
    monkeypatch.delenv("REVIEW_REPOSITORY")
    with pytest.raises(RuntimeError, match="invalid review repository"):
        review_repository()
BLOB = "e" * 40
PATH = "services/openwebui_tools/src/example.py"


def proposal(files=None, **kwargs):
    return Proposal(title="Change", body="Description", message="Update code",
                    files=files if files is not None else [{"path": PATH, "content": "print(1)\n"}], **kwargs)


@pytest.mark.parametrize("path", [
    "../services/openwebui_tools/src/a.py", "services/openwebui_tools/src/../a.py",
    "services/openwebui_tools/src/.hidden.py", "services/openwebui_tools/src/x/.dir/a.py",
    "services/openwebui_tools/src/a.txt", "services/openwebui_tools/src/a\\b.py",
    "services/openwebui_tools/src/a/./b.py", "services/openwebui_tools/src/a//b.py",
])
def test_reject_path(path):
    with pytest.raises(ValueError):
        proposal([{"path": path, "content": "x"}])


def test_reject_caps_and_extra_fields():
    with pytest.raises(ValueError):
        proposal([{"path": PATH, "content": "x" * 65537}])
    with pytest.raises(ValueError):
        proposal([{"path": PATH, "content": "a"}] * 2)
    with pytest.raises(ValueError):
        proposal([{"path": f"services/openwebui_tools/src/file{i}.py", "content": "x"} for i in range(21)])
    with pytest.raises(ValueError):
        Proposal.model_validate({"title": "a", "body": "", "message": "b", "files": [{"path": PATH, "content": "a"}], "repo": "evil/repo"})
    with pytest.raises(ValueError):
        proposal([{"path": PATH, "content": "\ud800"}])


@pytest.mark.asyncio
async def test_installation_token_is_repo_and_permission_scoped(monkeypatch):
    from broker import main

    monkeypatch.setenv("GITHUB_INSTALLATION_ID", "123")
    monkeypatch.setattr(main, "app_jwt", lambda: "test-jwt")

    def respond(request):
        assert request.url.path == "/app/installations/123/access_tokens"
        assert request.headers["authorization"] == "Bearer test-jwt"
        assert json.loads(request.content) == {
            "repositories": ["portfolio"], "permissions": {"contents": "write", "pull_requests": "write"}}
        return httpx.Response(201, json={"token": "installation-token"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await GitHub(client).installation_token() == "installation-token"


def mock_api(entries, *, fail_pulls=False, fail_auto_merge=False, listed_path=PATH):
    calls = []
    listed_pr = {
        "state": "open", "draft": False, "number": 1, "node_id": "PR_kwTest",
        "user": {"login": "home-lab-terminal-app[bot]"},
        "head": {"ref": "agent/openwebui-tools/test", "sha": "f" * 40,
                 "repo": {"id": 1, "full_name": REPO}},
        "base": {"ref": "main", "sha": BASE, "repo": {"id": 1, "full_name": REPO}},
    }

    def respond(request):
        assert request.url.host == "api.github.com"
        assert request.headers["authorization"] == "Bearer test-token"
        path = request.url.path
        calls.append((request.method, path, json.loads(request.content) if request.content else None))
        if path.endswith("/git/ref/heads/main"):
            data = {"object": {"sha": BASE}}
        elif path.endswith(f"/git/commits/{BASE}"):
            data = {"tree": {"sha": TREE}}
        elif path.endswith(f"/git/trees/{TREE}"):
            data = {"truncated": False, "tree": entries}
        elif path.endswith("/git/blobs"):
            data = {"sha": BLOB}
        elif path.endswith("/git/trees"):
            data = {"sha": NEW_TREE}
        elif path.endswith("/git/commits"):
            data = {"sha": NEW_COMMIT}
        elif path.endswith("/git/refs"):
            data = {}
        elif path.endswith("/pulls") and request.method == "GET":
            data = [listed_pr]
        elif path.endswith("/pulls/1/files"):
            data = [{"filename": listed_path, "status": "modified"}]
        elif path.endswith("/pulls/1"):
            data = {**listed_pr, "mergeable_state": "clean"}
        elif path.endswith("/pulls"):
            if fail_pulls:
                return httpx.Response(500, json={"error": "private upstream content"})
            data = {"html_url": f"https://github.com/{REPO}/pull/1", "number": 1, "node_id": "PR_kwTest"}
        elif path == "/graphql":
            if fail_auto_merge:
                return httpx.Response(403, json={"error": "private upstream content"})
            assert json.loads(request.content)["variables"] == {"id": "PR_kwTest"}
            data = {"data": {"enablePullRequestAutoMerge": {"pullRequest": {"autoMergeRequest": {"enabledAt": "2026-09-30T00:00:00Z"}}}}}
        elif "/git/refs/heads/agent/" in path and request.method == "DELETE":
            data = {}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=data)

    return httpx.MockTransport(respond), calls


@pytest.mark.asyncio
async def test_creates_pr_only_for_fixed_repo_preserving_mode(monkeypatch):
    entries = [{"path": PATH, "type": "blob", "mode": "100755", "sha": "f" * 40}]
    transport, calls = mock_api(entries)
    async with httpx.AsyncClient(transport=transport) as client:
        github = GitHub(client)

        async def token():
            return "test-token"

        monkeypatch.setattr(github, "installation_token", token)
        result = await github.create_proposal(proposal())
    assert result["number"] == 1
    assert result["auto_merge_queued"] is True
    assert result["branch"].startswith("agent/openwebui-tools/")
    assert all(path.startswith(ROOT + "/") or path == "/graphql" for _, path, _ in calls)
    tree_payload = next(payload for method, path, payload in calls if method == "POST" and path.endswith("/git/trees"))
    assert tree_payload == {"base_tree": TREE, "tree": [{"path": PATH, "mode": "100755", "type": "blob", "sha": BLOB}]}
    assert next(payload for method, path, payload in calls if path.endswith("/pulls")) == {
        "title": "Change", "body": "Description", "head": result["branch"], "base": "main"}


@pytest.mark.asyncio
@pytest.mark.parametrize("entries,content", [
    ([{"path": PATH, "type": "blob", "mode": "120000", "sha": "f" * 40}], "new"),
    ([{"path": "services/openwebui_tools/src", "type": "blob", "mode": "120000"}], "new"),
    ([{"path": PATH, "type": "blob", "mode": "100644", "sha": git_blob_sha(b"same")}], "same"),
])
async def test_no_write_for_symlink_or_noop(monkeypatch, entries, content):
    transport, calls = mock_api(entries)
    async with httpx.AsyncClient(transport=transport) as client:
        github = GitHub(client)

        async def token():
            return "test-token"

        monkeypatch.setattr(github, "installation_token", token)
        with pytest.raises((ValueError, UpstreamError)):
            await github.create_proposal(proposal([{"path": PATH, "content": content}]))
    assert all(method == "GET" for method, _, _ in calls)


@pytest.mark.asyncio
async def test_failed_pr_cleans_branch_without_upstream_payload(monkeypatch):
    transport, calls = mock_api([], fail_pulls=True)
    async with httpx.AsyncClient(transport=transport) as client:
        github = GitHub(client)

        async def token():
            return "test-token"

        monkeypatch.setattr(github, "installation_token", token)
        with pytest.raises(UpstreamError) as exc:
            await github.create_proposal(proposal())
    assert "private upstream content" not in str(exc.value)
    assert calls[-1][0] == "DELETE"


@pytest.mark.asyncio
async def test_failed_auto_merge_keeps_published_pr(monkeypatch):
    transport, calls = mock_api([], fail_auto_merge=True)
    async with httpx.AsyncClient(transport=transport) as client:
        github = GitHub(client)

        async def token():
            return "test-token"

        monkeypatch.setattr(github, "installation_token", token)
        result = await github.create_proposal(proposal())
    assert result["number"] == 1
    assert result["auto_merge_queued"] is False
    assert calls[-1][0:2] == ("POST", "/graphql")
    assert not any(method == "DELETE" for method, _, _ in calls)


@pytest.mark.asyncio
async def test_ready_bot_pr_requeues_only_allowlisted_source(monkeypatch):
    for path, expected in [(PATH, True), ("k8s/code-agent/deployment.yaml", False)]:
        transport, calls = mock_api([], listed_path=path)
        async with httpx.AsyncClient(transport=transport) as client:
            github = GitHub(client)

            async def token():
                return "test-token"

            monkeypatch.setattr(github, "installation_token", token)
            await github.retry_ready_auto_merges()
        assert any(method == "POST" and url == "/graphql" for method, url, _ in calls) is expected


@pytest.mark.asyncio
async def test_retry_finds_bot_pr_after_unrelated_full_page(monkeypatch):
    bot = {
        "state": "open", "draft": False, "number": 2, "node_id": "PR_kwTest",
        "user": {"login": "home-lab-terminal-app[bot]"},
        "head": {"ref": "agent/openwebui-tools/allowed", "sha": "f" * 40,
                 "repo": {"id": 1, "full_name": REPO}},
        "base": {"ref": "main", "sha": BASE, "repo": {"id": 1, "full_name": REPO}},
    }
    called = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={}))) as client:
        github = GitHub(client)

        async def token():
            return "test-token"

        async def listing(path, _token):
            if path.endswith("&page=1"):
                return [{"state": "open", "user": {"login": "other"}} for _ in range(100)]
            if path.endswith("&page=2"):
                return [bot]
            return [{"filename": PATH, "status": "modified"}]

        async def detail(method, path, _token, payload=None):
            assert method == "GET" and path.endswith("/pulls/2")
            return {**bot, "mergeable_state": "clean"}

        async def queue(_token, node_id):
            called.append(node_id)
            return True

        monkeypatch.setattr(github, "installation_token", token)
        monkeypatch.setattr(github, "request_list", listing)
        monkeypatch.setattr(github, "request", detail)
        monkeypatch.setattr(github, "request_auto_merge", queue)
        await github.retry_ready_auto_merges()
    assert called == ["PR_kwTest"]


@pytest.mark.asyncio
async def test_retry_never_uses_incomplete_pr_pages(monkeypatch):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={}))) as client:
        github = GitHub(client)

        async def token():
            return "test-token"

        async def listing(path, _token):
            if path.endswith("&page=1"):
                return [{"state": "open", "user": {"login": "other"}} for _ in range(100)]
            raise UpstreamError("page unavailable")

        async def unexpected(*_args):
            raise AssertionError("incomplete page must not request auto-merge")

        monkeypatch.setattr(github, "installation_token", token)
        monkeypatch.setattr(github, "request_list", listing)
        monkeypatch.setattr(github, "request_auto_merge", unexpected)
        with pytest.raises(UpstreamError):
            await github.retry_ready_auto_merges()


@pytest.mark.asyncio
async def test_endpoint_rejects_external_repo_and_large_body():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/health")).json() == {"status": "ok"}
        payload = proposal().model_dump()
        payload["repository"] = "other/repo"
        response = await client.post("/proposals", json=payload)
        assert response.status_code == 422
        assert "other/repo" not in response.text
        response = await client.post("/proposals", content=b"x" * (256 * 1024 + 1))
        assert response.status_code == 413


@pytest.mark.parametrize("repo,path,allowed", [
    ("portfolio", "services/renderer/src/main.py", True),
    ("portfolio", "services/web_crawler/tests/test_api.py", True),
    ("portfolio", "services/openwebui_tools/src/main.py", True),
    ("portfolio", "agents/product_search_agent/src/core/agent.py", True),
    ("portfolio", "shared/shared/shared/llm_client.py", True),
    ("portfolio", "k8s/renderer/deployment.yaml", True),
    ("portfolio", "k8s/web_crawler/ingress.yaml", True),
    ("portfolio", "k8s/llama/configmap.yaml", True),
    ("portfolio", "k8s/openwebui_tools/releaser.yaml", False),
    ("portfolio", "k8s/openwebui/deployment.yaml", False),
    ("portfolio", "k8s/argocd/rbac.yaml", False),
    ("portfolio", "k8s/traefik/values.yaml", False),
    ("portfolio", "k8s/unknown/deployment.yaml", False),
    ("portfolio", "services/code_agent_pr_broker/broker/main.py", False),
    ("portfolio", "services/openwebui_tools_releaser/src/main.py", False),
    ("portfolio", "k8s/code-agent/deployment.yaml", False),
    ("portfolio", "k8s/secrets/README.md", False),
    ("portfolio", "k8s/renderer/Secrets.yaml", False),
    ("portfolio", "k8s/renderer/GENERATED/deployment.yaml", False),
    ("portfolio", "k8s/renderer/certs/ca.yaml", False),
    ("portfolio", "k8s/renderer/.env.yaml", False),
    ("portfolio", "k8s/renderer/WEBHOOK.yaml", False),
    ("portfolio", "k8s/renderer/auth-token.yaml", False),
    ("portfolio", "services/renderer/src/AGENTS.md", False),
    ("portfolio", "services/renderer/deploy.sh", False),
    ("infra", "services/web/pages/index.vue", True),
    ("infra", "services/web_espacioraizconsciente/server/routes/health.get.ts", True),
    ("infra", "k8s/web/deployment.yaml", True),
    ("infra", "hosting/k3s/traefik/helmchart-config.yaml", True),
    ("infra", "hosting/01-create-infrastructure.sh", True),
    ("infra", "hosting/certs/cert.pem", False),
    ("infra", "k8s/secrets/README.md", False),
    ("infra", ".github/workflows/release.yaml", False),
    ("infra", "services/web/deploy-hetzner.sh", False),
    ("infra", "services/web/.env", False),
    ("infra", "AGENTS.md", False),
    ("infra", "services/web/pages/../../hosting/README.md", False),
    ("infra", "services/web/pages/.env.local.ts", False),
    ("infra", "services/web/pages/Generated/index.vue", False),
    ("infra", "services/web/pages/AGENTS.md", False),
])
def test_review_allowlist(repo, path, allowed):
    assert allowed_review_path(repo, path) is allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("repo", ["portfolio", "infra"])
async def test_review_token_is_split_by_repo(monkeypatch, repo):
    from broker import main
    monkeypatch.setenv("GITHUB_INSTALLATION_ID", "123")
    monkeypatch.setattr(main, "app_jwt", lambda: "test-jwt")

    def respond(req):
        assert json.loads(req.content)["repositories"] == [repo]
        return httpx.Response(201, json={"token": "test-token"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await GitHub(client, repo).installation_token() == "test-token"


@pytest.mark.asyncio
async def test_review_pr_never_auto_merges(monkeypatch):
    monkeypatch.setenv("REVIEW_REPOSITORY", "infra")
    monkeypatch.setenv("REVIEW_SNAPSHOT_SHA", BASE)
    path = "services/web/pages/index.vue"
    transport, calls = mock_api([])
    # mock_api advertises portfolio pull URLs; use a fixed infra URL instead.
    def respond(req):
        response = transport.handle_request(req)
        if req.url.path.endswith("/pulls") and req.method == "POST":
            return httpx.Response(201, json={"html_url": "https://github.com/Caos-Ordenado/infra/pull/1", "number": 1})
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        github = GitHub(client, "infra")

        async def token():
            return "test-token"

        monkeypatch.setattr(github, "installation_token", token)
        result = await github.create_review_proposal(ReviewProposal(
            title="Review", body="Body", message="Change", expected_base_sha=BASE,
            files=[{"path": path, "content": "<template>OK</template>"}],
        ))
    assert result["auto_merge_queued"] is False
    assert result["branch"].startswith("agent/review/")
    assert not any(url == "/graphql" for _, url, _ in calls)
    assert all(url.startswith("/repos/Caos-Ordenado/infra/") for _, url, _ in calls)


@pytest.mark.asyncio
async def test_stale_review_base_does_not_write(monkeypatch):
    monkeypatch.setenv("REVIEW_REPOSITORY", "portfolio")
    transport, calls = mock_api([])
    async with httpx.AsyncClient(transport=transport) as client:
        github = GitHub(client)

        async def token():
            return "test-token"

        monkeypatch.setattr(github, "installation_token", token)
        with pytest.raises(StaleBaseError):
            await github.create_review_proposal(ReviewProposal(
                title="Review", body="", message="Change", expected_base_sha="f" * 40,
                files=[{"path": PATH, "content": "print(1)"}],
            ))
    assert len(calls) == 1 and calls[0][0] == "GET"


@pytest.mark.asyncio
async def test_infra_runtime_disables_legacy_and_poller(monkeypatch):
    monkeypatch.setenv("REVIEW_REPOSITORY", "infra")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/proposals", json=proposal().model_dump())).status_code == 404
        assert (await client.post("/review-proposals", json={**proposal().model_dump(), "expected_base_sha": BASE})).status_code == 422
    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError):
            await GitHub(client, "infra").retry_ready_auto_merges()


@pytest.mark.asyncio
async def test_review_endpoint_stale_base_returns_conflict(monkeypatch):
    monkeypatch.setenv("REVIEW_REPOSITORY", "portfolio")
    transport, calls = mock_api([])
    async with httpx.AsyncClient(transport=transport) as upstream:
        github = GitHub(upstream)

        async def token():
            return "test-token"

        monkeypatch.setattr(github, "installation_token", token)
        app.state.github = github
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                payload = {**proposal().model_dump(), "expected_base_sha": "f" * 40}
                assert (await client.post("/review-proposals", json=payload)).status_code == 409
                assert (await client.post("/review-proposals", json=proposal().model_dump())).status_code == 422
        finally:
            del app.state.github
    assert len(calls) == 1 and calls[0][0] == "GET"


@pytest.mark.asyncio
async def test_review_rejects_nested_symlink_before_writes(monkeypatch):
    monkeypatch.setenv("REVIEW_REPOSITORY", "infra")
    monkeypatch.setenv("REVIEW_SNAPSHOT_SHA", BASE)
    transport, calls = mock_api([{"path": "services/web/pages/nested", "type": "blob", "mode": "120000"}])
    async with httpx.AsyncClient(transport=transport) as client:
        github = GitHub(client, "infra")

        async def token():
            return "test-token"

        monkeypatch.setattr(github, "installation_token", token)
        with pytest.raises(UpstreamError):
            await github.create_review_proposal(ReviewProposal(
                title="Review", body="", message="Change", expected_base_sha=BASE,
                files=[{"path": "services/web/pages/nested/index.vue", "content": "<template />"}],
            ))
    assert all(method == "GET" for method, _, _ in calls)


@pytest.mark.parametrize("content", ["\x00", "\ud800", "-----BEGIN PRIVATE KEY-----\nkey"])
def test_review_rejects_nontext_and_private_key(content):
    with pytest.raises(ValueError):
        ReviewProposal(title="Review", body="", message="Change", expected_base_sha=BASE,
                       files=[{"path": PATH, "content": content}])


def review_proposal(path="services/web/pages/index.vue", expected_base_sha=BASE):
    return ReviewProposal(title="Review", body="", message="Change", expected_base_sha=expected_base_sha,
                          files=[{"path": path, "content": "<template />"}])


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot,expected,error", [
    (None, BASE, RuntimeError),
    ("wrong", BASE, RuntimeError),
    ("f" * 40, BASE, StaleBaseError),
])
async def test_infra_snapshot_fails_before_api(monkeypatch, snapshot, expected, error):
    monkeypatch.setenv("REVIEW_REPOSITORY", "infra")
    if snapshot is None:
        monkeypatch.delenv("REVIEW_SNAPSHOT_SHA", raising=False)
    else:
        monkeypatch.setenv("REVIEW_SNAPSHOT_SHA", snapshot)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _req: pytest.fail("API called"))) as client:
        with pytest.raises(error):
            await GitHub(client, "infra").create_review_proposal(review_proposal(expected_base_sha=expected))


@pytest.mark.asyncio
@pytest.mark.parametrize("ref_shas", [("f" * 40,), (BASE, "f" * 40)])
async def test_infra_live_main_stale_on_either_check(monkeypatch, ref_shas):
    monkeypatch.setenv("REVIEW_REPOSITORY", "infra")
    monkeypatch.setenv("REVIEW_SNAPSHOT_SHA", BASE)
    calls = []
    ref_reads = 0

    def respond(req):
        nonlocal ref_reads
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("/git/ref/heads/main"):
            sha = ref_shas[min(ref_reads, len(ref_shas) - 1)]
            ref_reads += 1
            return httpx.Response(200, json={"object": {"sha": sha}})
        if req.url.path.endswith(f"/git/commits/{BASE}"):
            return httpx.Response(200, json={"tree": {"sha": TREE}})
        if req.url.path.endswith(f"/git/trees/{TREE}"):
            return httpx.Response(200, json={"truncated": False, "tree": []})
        pytest.fail("unexpected request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        github = GitHub(client, "infra")

        async def token():
            return "test-token"

        monkeypatch.setattr(github, "installation_token", token)
        with pytest.raises(StaleBaseError):
            await github.create_review_proposal(review_proposal())
    assert all(method == "GET" for method, _ in calls)


@pytest.mark.asyncio
async def test_infra_snapshot_changed_between_checks_blocks_writes(monkeypatch):
    monkeypatch.setenv("REVIEW_REPOSITORY", "infra")
    monkeypatch.setenv("REVIEW_SNAPSHOT_SHA", BASE)
    calls = []

    def respond(req):
        calls.append(req.method)
        if req.url.path.endswith("/git/ref/heads/main"):
            if calls.count("GET") == 4:
                monkeypatch.setenv("REVIEW_SNAPSHOT_SHA", "f" * 40)
            return httpx.Response(200, json={"object": {"sha": BASE}})
        if req.url.path.endswith(f"/git/commits/{BASE}"):
            return httpx.Response(200, json={"tree": {"sha": TREE}})
        if req.url.path.endswith(f"/git/trees/{TREE}"):
            return httpx.Response(200, json={"truncated": False, "tree": []})
        pytest.fail("unexpected request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        github = GitHub(client, "infra")

        async def token():
            return "test-token"

        monkeypatch.setattr(github, "installation_token", token)
        with pytest.raises(StaleBaseError):
            await github.create_review_proposal(review_proposal())
    assert calls == ["GET"] * 4


@pytest.mark.asyncio
async def test_infra_endpoint_missing_snapshot_and_caller_override(monkeypatch):
    monkeypatch.setenv("REVIEW_REPOSITORY", "infra")
    monkeypatch.delenv("REVIEW_SNAPSHOT_SHA", raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        payload = review_proposal().model_dump()
        assert (await client.post("/review-proposals", json=payload)).status_code == 502
        payload["review_snapshot_sha"] = BASE
        assert (await client.post("/review-proposals", json=payload)).status_code == 422


@pytest.mark.asyncio
async def test_private_files_cannot_be_posted_to_public_broker(monkeypatch):
    monkeypatch.setenv("REVIEW_REPOSITORY", "portfolio")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        payload = review_proposal().model_dump()
        assert (await client.post("/review-proposals", json=payload)).status_code == 422
        payload["repository"] = "infra"
        assert (await client.post("/review-proposals", json=payload)).status_code == 422
        payload.pop("repository")
        payload["files"] = [{"path": "k8s/openwebui_tools/releaser.yaml", "content": "x"}]
        assert (await client.post("/review-proposals", json=payload)).status_code == 422
