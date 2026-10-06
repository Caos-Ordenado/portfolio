import importlib.util
import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("releaser", Path(__file__).resolve().parents[1] / "releaser.py")
releaser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(releaser)


def deployment(name="open-terminal", container="open-terminal", image="old:tag", marker=None):
    return {"metadata": {"name": name, "namespace": "code-agent", "resourceVersion": "12", "generation": 2,
                         "annotations": {releaser.MARKER: marker} if marker else {}},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": container, "image": image, "imagePullPolicy": "Never"}]}}},
            "status": {"observedGeneration": 2, "updatedReplicas": 1, "readyReplicas": 1, "availableReplicas": 1}}


class Gh:
    def __init__(self, responses):
        self.responses = responses

    def request(self, method, url, headers=None, data=None):
        for suffix, response in self.responses.items():
            if url.endswith(suffix):
                return 200, {}, __import__("json").dumps(response).encode()
        raise AssertionError(url)


def test_snapshot_rejects_wrong_namespace_and_containers():
    obj = deployment()
    obj["metadata"]["namespace"] = "default"
    with pytest.raises(releaser.ReleaseError):
        releaser.snapshot(obj, "open-terminal", "open-terminal")
    obj = deployment()
    obj["spec"]["template"]["spec"]["containers"].append(obj["spec"]["template"]["spec"]["containers"][0])
    with pytest.raises(releaser.ReleaseError):
        releaser.snapshot(obj, "open-terminal", "open-terminal")


def test_patch_limited_to_named_container_image_policy_and_marker():
    ops = releaser.patch_ops("open-terminal", 0, ("old", "Never"), ("new", "IfNotPresent"), "12", None, {}, 123)
    assert [op["path"] for op in ops if op["op"] == "replace"] == [
        "/spec/template/spec/containers/0/image", "/spec/template/spec/containers/0/imagePullPolicy"]
    assert any(op["path"] == releaser.MARKER_PATH and op["value"] == "123" for op in ops)
    assert ops[0] == {"op": "test", "path": "/metadata/resourceVersion", "value": "12"}


def test_stale_and_failed_runs_do_not_release():
    sha = "a" * 40
    base = {"/git/ref/heads/main": {"ref": "refs/heads/main", "object": {"type": "commit", "sha": sha}}}
    run = {"id": 17, "head_sha": sha, "head_branch": "main", "event": "push", "status": "completed", "conclusion": "failure"}
    assert releaser.candidate(Gh({**base, "per_page=1": {"total_count": 1, "workflow_runs": [run]}})) is None
    run["conclusion"] = "success"
    run["head_sha"] = "b" * 40
    with pytest.raises(releaser.ReleaseError, match="stale"):
        releaser.candidate(Gh({**base, "per_page=1": {"total_count": 1, "workflow_runs": [run]}}))


def test_status_requires_matching_run_and_bot():
    sha = "a" * 40
    item = {"context": "code-agent-cluster-diagnostics-image", "state": "success", "creator": {"login": "github-actions[bot]"},
            "description": "sha256:" + "f" * 64, "target_url": releaser.RUN_PAGE + "17"}
    assert releaser.digest_status(Gh({"per_page=100": [item]}), 17, sha, "cluster-diagnostics") == item["description"]
    item["target_url"] = releaser.RUN_PAGE + "16"
    with pytest.raises(releaser.ReleaseError, match="invalid_image_status"):
        releaser.digest_status(Gh({"per_page=100": [item]}), 17, sha, "cluster-diagnostics")


def test_restore_previous_image_and_policy_after_failed_rollout(monkeypatch):
    class Kube:
        def __init__(self):
            self.current = deployment(marker="27")
            self.current["spec"]["template"]["spec"]["containers"][0].update(image="new", imagePullPolicy="IfNotPresent")
            self.current["spec"]["template"]["metadata"] = {"annotations": {releaser.RESTART: "restart"}}
            self.ops = []

        def read(self, name):
            return self.current

        def patch(self, name, ops):
            self.ops.append(ops)
            return self.current

    kube = Kube()
    monkeypatch.setattr(releaser, "wait_ready", lambda *args: None)
    releaser.restore(kube, [("deployment", "open-terminal", "open-terminal", 0, ("old:tag", "Never"), ("new", "IfNotPresent"), None, None, "restart")], 27)
    assert [op["value"] for op in kube.ops[0] if op["op"] == "replace"] == ["old:tag", "Never"]


def test_restore_refuses_concurrent_changes(monkeypatch, caplog):
    class Kube:
        def read(self, name):
            return deployment(image="third-party", marker="27")

        def patch(self, name, ops):
            raise AssertionError("must not overwrite concurrent edit")

    releaser.restore(Kube(), [("deployment", "open-terminal", "open-terminal", 0, ("old:tag", "Never"), ("new", "IfNotPresent"), None, None, "restart")], 27)
    assert "restore_failed" in caplog.text


def test_instruction_validates_blob_and_size():
    path = releaser.INSTRUCTIONS[0][1]
    raw = b"# instructions\n"
    obj = {"type": "file", "path": path, "encoding": "base64", "size": len(raw),
           "sha": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(),
           "content": base64.b64encode(raw).decode()}
    sha = "a" * 40
    assert releaser.instruction(Gh({f"?ref={sha}": obj}), path, sha) == raw.decode()
    for change in ({"sha": "0" * 40}, {"size": len(raw) + 1}, {"content": "!!!!"}, {"path": "other"}):
        with pytest.raises(releaser.ReleaseError):
            releaser.instruction(Gh({f"?ref={sha}": {**obj, **change}}), path, sha)


def test_config_read_and_patch_are_constrained():
    class Transport:
        def request(self, method, url, headers=None, data=None):
            assert url.endswith("/api/v1/namespaces/code-agent/configmaps/open-terminal-agents")
            if method == "GET":
                return 200, {}, json.dumps({"metadata": {"name": "open-terminal-agents", "namespace": "code-agent", "resourceVersion": "3"}, "data": {"AGENTS.md": "old"}}).encode()
            assert headers["Content-Type"] == "application/json-patch+json"
            assert json.loads(data) == releaser.config_ops("3", "old", "new")
            return 200, {}, json.dumps({"metadata": {"name": "open-terminal-agents", "namespace": "code-agent", "resourceVersion": "4"}, "data": {"AGENTS.md": "new"}}).encode()

    kube = releaser.Kube(Transport(), "kubernetes.default.svc", "443", "fake")
    assert releaser.config_snapshot(kube.read_config("open-terminal-agents"), "open-terminal-agents") == ("3", "old")
    assert releaser.config_snapshot(kube.patch_config("open-terminal-agents", releaser.config_ops("3", "old", "new")), "open-terminal-agents") == ("4", "new")
    for bad in ({"metadata": {"name": "wrong", "namespace": "code-agent", "resourceVersion": "3"}, "data": {"AGENTS.md": "old"}},
                {"metadata": {"name": "open-terminal-agents", "namespace": "other", "resourceVersion": "3"}, "data": {"AGENTS.md": "old"}}):
        with pytest.raises(releaser.ReleaseError):
            releaser.config_snapshot(bad, "open-terminal-agents")


class FakeKube:
    def __init__(self):
        self.deployments = {name: deployment(name, container) for name, container, _ in releaser.TARGETS}
        self.configs = {name: {"metadata": {"name": name, "namespace": "code-agent", "resourceVersion": "1"},
                               "data": {"AGENTS.md": "old"}} for name, _ in releaser.INSTRUCTIONS}
        self.patches = []
        self.fail = False

    def read(self, name):
        return copy.deepcopy(self.deployments[name])

    def read_config(self, name):
        return copy.deepcopy(self.configs[name])

    def patch(self, name, ops):
        return self._patch(self.deployments[name], ops, name)

    def patch_config(self, name, ops):
        return self._patch(self.configs[name], ops, name)

    def _patch(self, obj, ops, name):
        self.patches.append((name, copy.deepcopy(ops)))
        if self.fail and name == "infra-terminal":
            raise releaser.ReleaseError("rollout_failed")
        work = copy.deepcopy(obj)
        for op in ops:
            parts = [p.replace("~1", "/").replace("~0", "~") for p in op["path"].split("/")[1:]]
            node = work
            for part in parts[:-1]:
                node = node[int(part)] if isinstance(node, list) else node[part]
            key = parts[-1]
            if op["op"] == "test":
                assert node[key] == op["value"]
            elif op["op"] == "remove":
                del node[key]
            else:
                node[key] = op["value"]
        work["metadata"]["resourceVersion"] = str(int(obj["metadata"]["resourceVersion"]) + 1)
        if "spec" in work:
            work["metadata"]["generation"] += 1
            work["status"]["observedGeneration"] += 1
        obj.clear()
        obj.update(work)
        return copy.deepcopy(obj)


def test_sync_restarts_both_terminals_even_with_unchanged_images(monkeypatch):
    kube = FakeKube()
    monkeypatch.setattr(releaser, "candidate", lambda gh: (27, "a" * 40))
    monkeypatch.setattr(releaser, "instruction", lambda gh, path, sha: "new")
    monkeypatch.setattr(releaser, "digest_status", lambda *args: "sha256:" + "f" * 64)
    monkeypatch.setattr(releaser, "registry_digest", lambda *args: "sha256:" + "f" * 64)
    monkeypatch.setattr(releaser, "head", lambda gh: "a" * 40)
    monkeypatch.setattr(releaser, "wait_ready", lambda *args: None)
    releaser.reconcile(None, kube)
    assert all(c["data"]["AGENTS.md"] == "new" for c in kube.configs.values())
    for name in ("open-terminal", "infra-terminal"):
        ops = next(ops for target, ops in kube.patches if target == name)
        assert any(op["path"] in (releaser.RESTART_PATH, "/spec/template/metadata") and op["op"] == "add" for op in ops)
        assert kube.deployments[name]["spec"]["template"]["metadata"]["annotations"][releaser.RESTART] == "a" * 40 + "-27"


def test_failed_rollout_restores_config_and_restart(monkeypatch):
    kube = FakeKube()
    kube.deployments["open-terminal"]["spec"]["template"]["metadata"] = {"annotations": {releaser.RESTART: "prior"}}
    kube.fail = True
    monkeypatch.setattr(releaser, "candidate", lambda gh: (27, "a" * 40))
    monkeypatch.setattr(releaser, "instruction", lambda *args: "new")
    monkeypatch.setattr(releaser, "digest_status", lambda *args: "sha256:" + "f" * 64)
    monkeypatch.setattr(releaser, "registry_digest", lambda *args: "sha256:" + "f" * 64)
    monkeypatch.setattr(releaser, "head", lambda gh: "a" * 40)
    monkeypatch.setattr(releaser, "wait_ready", lambda *args: None)
    with pytest.raises(releaser.ReleaseError, match="rollout_failed"):
        releaser.reconcile(None, kube)
    assert [c["data"]["AGENTS.md"] for c in kube.configs.values()] == ["old", "old"]
    assert kube.deployments["open-terminal"]["spec"]["template"]["metadata"]["annotations"][releaser.RESTART] == "prior"
    assert kube.deployments["open-terminal"]["spec"]["template"]["spec"]["containers"][0]["image"] == "old:tag"
    restored_config = next(i for i, (name, ops) in enumerate(kube.patches)
                           if name == "open-terminal-agents" and any(op.get("value") == "old" for op in ops if op["op"] == "replace"))
    restored_terminal = next(i for i, (name, ops) in enumerate(kube.patches)
                             if name == "open-terminal" and any(op.get("value") == "old:tag" for op in ops if op["op"] == "replace"))
    assert restored_config < restored_terminal  # subPath refreshes on terminal restart


def test_restore_removes_new_restart_and_refuses_concurrent_config(monkeypatch, caplog):
    kube = FakeKube()
    kube.deployments["open-terminal"]["metadata"]["annotations"][releaser.MARKER] = "27"
    kube.deployments["open-terminal"]["spec"]["template"]["metadata"] = {"annotations": {releaser.RESTART: "a" * 40 + "-27"}}
    kube.deployments["open-terminal"]["spec"]["template"]["spec"]["containers"][0].update(image="new", imagePullPolicy="IfNotPresent")
    kube.configs["open-terminal-agents"]["data"]["AGENTS.md"] = "third-party"
    monkeypatch.setattr(releaser, "wait_ready", lambda *args: None)
    releaser.restore(kube, [("config", "open-terminal-agents", "old", "new", "1", "2"),
                            ("deployment", "open-terminal", "open-terminal", 0, ("old:tag", "Never"),
                             ("new", "IfNotPresent"), None, None, "a" * 40 + "-27")], 27)
    assert releaser.RESTART not in kube.deployments["open-terminal"]["spec"]["template"]["metadata"]["annotations"]
    assert kube.configs["open-terminal-agents"]["data"]["AGENTS.md"] == "third-party"
    assert "restore_failed names=open-terminal-agents" in caplog.text


def test_zero_terminal_replicas_fail_closed(monkeypatch):
    kube = FakeKube()
    kube.deployments["open-terminal"]["spec"]["replicas"] = 0
    with pytest.raises(releaser.ReleaseError, match="terminal_zero_replicas"):
        releaser.wait_ready(kube, "open-terminal", "open-terminal", ("old:tag", "Never"), __import__("time").monotonic() + 1)
