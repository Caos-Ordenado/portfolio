import copy
import io
import json
import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import releaser


SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
IMAGE = "ghcr.io/caos-ordenado/openwebui-tools@" + DIGEST


def deployment(image="old:tag", policy="Never", marker=None):
    meta = {"name": "openwebui-tools", "namespace": "default", "generation": 1,
            "resourceVersion": "100"}
    if marker is not None:
        meta["annotations"] = {releaser.ATTEMPT_KEY: str(marker), "other": "preserved"}
    return {"metadata": meta, "spec": {"replicas": 1, "template": {"spec": {"containers": [
        {"name": "sidecar", "image": "side:tag", "imagePullPolicy": "Always"},
        {"name": "openwebui-tools", "image": image, "imagePullPolicy": policy},
    ]}}}, "status": {"observedGeneration": 1, "updatedReplicas": 1,
                       "readyReplicas": 1, "availableReplicas": 1}}


class FakeTransport:
    def __init__(self):
        self.head = SHA
        self.runs = [{"id": 12, "head_sha": SHA, "head_branch": "main", "event": "push",
                      "status": "completed", "conclusion": "success"}]
        self.jobs = [{"name": "build", "status": "completed", "conclusion": "success"}]
        self.statuses = [{"context": "openwebui-tools-image", "state": "success",
                          "description": DIGEST, "target_url": releaser.RUN_PAGE + "12",
                          "creator": {"login": "github-actions[bot]"}}]
        self.digest = DIGEST
        self.manifest = True
        self.headers_seen = []

    def request(self, method, url, headers=None, data=None):
        self.headers_seen.append(headers or {})
        if url.endswith("/git/ref/heads/main"):
            body = {"ref": "refs/heads/main", "object": {"type": "commit", "sha": self.head}}
        elif url.endswith("/runs?branch=main&event=push&per_page=1"):
            body = {"total_count": len(self.runs), "workflow_runs": self.runs}
        elif "/jobs?" in url:
            body = {"total_count": len(self.jobs), "jobs": self.jobs}
        elif "/statuses?" in url:
            body = self.statuses
        elif "/token?" in url:
            body = {"token": "private-test-token"}
        elif "/manifests/" in url:
            if not self.manifest:
                raise releaser.ReleaseError("http_401")
            return 200, {"Docker-Content-Digest": self.digest,
                         "Content-Type": "application/vnd.oci.image.index.v1+json"}, b"{}"
        else:
            raise AssertionError(url)
        return 200, {}, json.dumps(body).encode()


class FakeKube:
    url = "deployment"
    service = "service"

    def __init__(self, failure=None, marker=None):
        self.current = deployment(marker=marker)
        self.failure = failure
        self.patches = []

    def get(self, url):
        if url == self.service:
            return {"metadata": {"name": "openwebui-tools", "namespace": "default"},
                    "spec": {"type": "ClusterIP"}}
        return copy.deepcopy(self.current)

    def patch(self, ops):
        self.patches.append(ops)
        item = self.current["spec"]["template"]["spec"]["containers"][1]
        for op in ops:
            if op["op"] != "test":
                continue
            path = op["path"]
            actual = (self.current["metadata"]["resourceVersion"] if path == "/metadata/resourceVersion"
                      else self.current["metadata"].get("annotations", {}).get(releaser.ATTEMPT_KEY)
                      if path == releaser.ATTEMPT_PATH else item[path.split("/")[-1]])
            if actual != op["value"]:
                raise releaser.ReleaseError("http_409")
        for op in ops:
            if op["op"] not in ("add", "replace"):
                continue
            path = op["path"]
            if path == "/metadata/annotations":
                self.current["metadata"]["annotations"] = op["value"]
            elif path == releaser.ATTEMPT_PATH:
                self.current["metadata"].setdefault("annotations", {})[releaser.ATTEMPT_KEY] = op["value"]
            else:
                item[path.split("/")[-1]] = op["value"]
        self.current["metadata"]["resourceVersion"] = str(int(self.current["metadata"]["resourceVersion"]) + 1)
        self.current["metadata"]["generation"] += 1
        self.current["status"]["observedGeneration"] += 1
        if self.failure == "lost_response" and len(self.patches) == 1:
            raise releaser.ReleaseError("network_failure")
        if self.failure == "race" and len(self.patches) == 1:
            item["image"] = "other:tag"
            raise releaser.ReleaseError("network_failure")
        return self.get(self.url)


class ReleaserTests(unittest.TestCase):
    def test_pending_failed_skipped_or_newer_head_never_promotes_prior_run(self):
        for state, conclusion in [("queued", None), ("completed", "failure"),
                                  ("completed", "skipped")]:
            gh, kube = FakeTransport(), FakeKube()
            gh.runs[0].update(status=state, conclusion=conclusion)
            releaser.reconcile(gh, kube)
            self.assertEqual(kube.patches, [])
        gh, kube = FakeTransport(), FakeKube()
        gh.head = "c" * 40
        releaser.reconcile(gh, kube)
        self.assertEqual(kube.patches, [])

    def test_main_advances_during_registry_lookup(self):
        class Advancing(FakeTransport):
            def request(self, method, url, headers=None, data=None):
                if "/manifests/" in url:
                    self.head = "c" * 40
                return super().request(method, url, headers, data)
        kube = FakeKube()
        releaser.reconcile(Advancing(), kube)
        self.assertEqual(kube.patches, [])

    def test_no_build_and_malformed_sha_fail_closed(self):
        gh, kube = FakeTransport(), FakeKube()
        gh.jobs = []
        releaser.reconcile(gh, kube)
        self.assertEqual(kube.patches, [])
        gh.runs[0]["head_sha"] = "bad"
        with self.assertRaises(releaser.ReleaseError):
            releaser.reconcile(gh, kube)

    def test_status_forgery_missing_and_newer_revocation(self):
        for change in ({"creator": {"login": "attacker"}}, {"target_url": "https://evil.example"},
                       {"description": "sha256:wrong"}, {"state": "failure"}):
            gh, kube = FakeTransport(), FakeKube()
            gh.statuses[0].update(change)
            with self.assertRaises(releaser.ReleaseError):
                releaser.reconcile(gh, kube)
            self.assertEqual(kube.patches, [])
        gh = FakeTransport()
        gh.statuses.insert(0, {**gh.statuses[0], "state": "failure"})
        with self.assertRaises(releaser.ReleaseError):
            releaser.reconcile(gh, FakeKube())
        gh.statuses = []
        with self.assertRaises(releaser.ReleaseError):
            releaser.reconcile(gh, FakeKube())

    def test_registry_digest_mismatch_private_registry(self):
        for digest, public in [("sha256:" + "c" * 64, True), (DIGEST, False)]:
            gh, kube = FakeTransport(), FakeKube()
            gh.digest, gh.manifest = digest, public
            with self.assertRaises(releaser.ReleaseError):
                releaser.reconcile(gh, kube)
            self.assertEqual(kube.patches, [])

    def test_patch_atomically_marks_attempt_and_checks_resource_version(self):
        kube = FakeKube()
        releaser.reconcile(FakeTransport(), kube)
        ops = kube.patches[0]
        self.assertEqual(ops[0], {"op": "test", "path": "/metadata/resourceVersion", "value": "100"})
        self.assertIn({"op": "add", "path": "/metadata/annotations",
                       "value": {releaser.ATTEMPT_KEY: "12"}}, ops)
        self.assertEqual(kube.current["spec"]["template"]["spec"]["containers"][0]["image"], "side:tag")
        self.assertEqual(releaser.metadata(kube.current)[2], "12")
        with patch.object(releaser.time, "sleep"), patch.object(releaser.time, "monotonic", side_effect=[0, 1, 2]):
            kube.current["status"]["observedGeneration"] = 0
            with self.assertRaises(releaser.ReleaseError):
                releaser.wait_ready(kube, IMAGE, "IfNotPresent", 2)

    def test_resource_version_race_rejects_patch_without_marker(self):
        kube = FakeKube()
        original = kube.get(kube.url)
        rv, annotations, marker = releaser.metadata(original)
        kube.current["metadata"]["resourceVersion"] = "101"
        ops = releaser.patch_ops(1, "old:tag", "Never", IMAGE, "IfNotPresent",
                                 rv, marker, 12, annotations)
        with self.assertRaises(releaser.ReleaseError):
            kube.patch(ops)
        self.assertIsNone(releaser.metadata(kube.current)[2])

    def test_same_older_run_and_manual_rollback_not_repromoted(self):
        for marker in ("12", "13"):
            kube = FakeKube(marker=marker)
            releaser.reconcile(FakeTransport(), kube)
            self.assertEqual(kube.patches, [])
        kube = FakeKube(marker="11")
        releaser.reconcile(FakeTransport(), kube)
        self.assertEqual(releaser.metadata(kube.current)[2], "12")
        self.assertEqual(kube.current["metadata"]["annotations"]["other"], "preserved")
        kube.current["spec"]["template"]["spec"]["containers"][1].update(image="old:tag", imagePullPolicy="Never")
        releaser.reconcile(FakeTransport(), kube)
        self.assertEqual(len(kube.patches), 1)

    def test_uncertain_patch_restores_but_keeps_attempt(self):
        kube = FakeKube("lost_response")
        with self.assertRaises(releaser.ReleaseError):
            releaser.reconcile(FakeTransport(), kube)
        self.assertEqual(len(kube.patches), 2)
        self.assertEqual(releaser.container(kube.current)[1:], ("old:tag", "Never"))
        self.assertEqual(releaser.metadata(kube.current)[2], "12")
        releaser.reconcile(FakeTransport(), kube)
        self.assertEqual(len(kube.patches), 2)

    def test_mixed_race_does_not_restore_other_actor(self):
        kube = FakeKube("race")
        with self.assertRaises(releaser.ReleaseError):
            releaser.reconcile(FakeTransport(), kube)
        self.assertEqual(len(kube.patches), 1)
        self.assertEqual(releaser.container(kube.current)[1], "other:tag")

    def test_no_token_or_response_body_in_logs(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        releaser.LOG.addHandler(handler)
        try:
            gh = FakeTransport()
            gh.manifest = False
            with self.assertRaises(releaser.ReleaseError):
                releaser.reconcile(gh, FakeKube())
        finally:
            releaser.LOG.removeHandler(handler)
        self.assertNotIn("private-test-token", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
