import json

import httpx
import pytest

from broker.main import GitHub, Proposal, REPO, UpstreamError, app, git_blob_sha

ROOT = f"/repos/{REPO}"
BASE = "a" * 40
TREE = "b" * 40
NEW_TREE = "c" * 40
NEW_COMMIT = "d" * 40
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


def mock_api(entries, *, fail_pulls=False, fail_auto_merge=False):
    calls = []

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
