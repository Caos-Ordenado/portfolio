"""One-shot, outbound-only deployment reconciler for trusted source-only builds."""

import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


API = "https://api.github.com/repos/Caos-Ordenado/portfolio/actions/workflows/openwebui-tools-deploy.yml/runs"
GITHUB = "https://api.github.com/repos/Caos-Ordenado/portfolio"
RUN_PAGE = "https://github.com/Caos-Ordenado/portfolio/actions/runs/"
ATTEMPT_KEY = "automation.reyops.com/openwebui-tools-attempted-run-id"
ATTEMPT_PATH = "/metadata/annotations/automation.reyops.com~1openwebui-tools-attempted-run-id"
REGISTRY = "https://ghcr.io"
REPOSITORY = "caos-ordenado/openwebui-tools"
DEPLOYMENT = "openwebui-tools"
NAMESPACE = "default"
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
CA_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
LOG = logging.getLogger("releaser")


class ReleaseError(Exception):
    """Safe-to-log failure (no upstream response bodies or credentials)."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Transport:
    def __init__(self, ca_file=None):
        context = ssl.create_default_context(cafile=ca_file)
        if ca_file:
            # The current MicroK8s CA lacks a key-usage extension required by
            # Python 3.13's strict mode. Only relax that check for the mounted
            # cluster CA; still validate its chain and the server hostname.
            context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context), NoRedirect()
        )

    def request(self, method, url, headers=None, data=None):
        request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with self.opener.open(request, timeout=10) as response:
                body = response.read(2_000_001)
                if len(body) > 2_000_000:
                    raise ReleaseError("response_too_large")
                return response.status, response.headers, body
        except urllib.error.HTTPError as exc:
            # Never log headers, body, or URL: these may contain credentials.
            raise ReleaseError(f"http_{exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ReleaseError("network_failure") from None


def fetch_json(transport, url, headers=None):
    status, _, body = transport.request("GET", url, headers)
    if status != 200:
        raise ReleaseError("unexpected_http_status")
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise ReleaseError("invalid_json") from None


def main_head(transport):
    ref = fetch_json(transport, GITHUB + "/git/ref/heads/main", {"Accept": "application/vnd.github+json"})
    try:
        head = ref["object"]["sha"]
        if ref["ref"] != "refs/heads/main" or ref["object"]["type"] != "commit" or not SHA.fullmatch(head):
            raise ReleaseError("invalid_main_ref")
    except (KeyError, TypeError):
        raise ReleaseError("invalid_main_ref") from None
    return head


def latest_build(transport):
    head = main_head(transport)
    # Never look past the newest run: a pending, failed, or skipped run blocks
    # any earlier successful build from being re-promoted.
    query = "?branch=main&event=push&per_page=1"
    runs = fetch_json(transport, API + query, {"Accept": "application/vnd.github+json"})
    if not isinstance(runs, dict) or not isinstance(runs.get("workflow_runs"), list):
        raise ReleaseError("invalid_runs")
    items = runs["workflow_runs"]
    if not isinstance(runs.get("total_count"), int) or runs["total_count"] < len(items) or len(items) > 1:
        raise ReleaseError("invalid_runs_count")
    if not items:
        if runs["total_count"]:
            raise ReleaseError("invalid_runs_count")
        return None
    run = items[0]
    if not isinstance(run, dict) or run.get("head_branch") != "main" or run.get("event") != "push":
        raise ReleaseError("invalid_run")
    run_id, sha = run.get("id"), run.get("head_sha")
    if type(run_id) is not int or run_id <= 0 or not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ReleaseError("invalid_run_identity")
    if sha != head or run.get("status") != "completed" or run.get("conclusion") != "success":
        return None
    jobs = fetch_json(
        transport, f"{GITHUB}/actions/runs/{run_id}/jobs?per_page=100",
        {"Accept": "application/vnd.github+json"},
    )
    if not isinstance(jobs, dict) or not isinstance(jobs.get("jobs"), list) or type(jobs.get("total_count")) is not int or jobs["total_count"] != len(jobs["jobs"]):
        raise ReleaseError("invalid_jobs")
    builds = [job for job in jobs["jobs"] if isinstance(job, dict) and job.get("name") == "build"]
    if len(builds) != 1 or builds[0].get("status") != "completed" or builds[0].get("conclusion") != "success":
        return None
    return run_id, sha


def published_digest(transport, run_id, sha):
    statuses = fetch_json(transport, f"{GITHUB}/commits/{sha}/statuses?per_page=100",
                          {"Accept": "application/vnd.github+json"})
    if not isinstance(statuses, list) or len(statuses) > 100:
        raise ReleaseError("invalid_statuses")
    # GitHub returns newest first. Never fall back to an older matching success
    # after a newer status has revoked or replaced the context.
    for status in statuses:
        if not isinstance(status, dict):
            raise ReleaseError("invalid_statuses")
        if status.get("context") == "openwebui-tools-image":
            try:
                digest = status["description"]
                creator = status["creator"]["login"]
            except (KeyError, TypeError):
                raise ReleaseError("invalid_image_status") from None
            if (status.get("state") != "success" or creator != "github-actions[bot]"
                    or not isinstance(digest, str) or not DIGEST.fullmatch(digest)
                    or status.get("target_url") != RUN_PAGE + str(run_id)):
                raise ReleaseError("invalid_image_status")
            return digest
    if len(statuses) == 100:
        raise ReleaseError("status_window_exhausted")
    raise ReleaseError("missing_image_status")


def image_digest(transport, sha):
    if not SHA.fullmatch(sha):
        raise ReleaseError("invalid_sha")
    token = fetch_json(
        transport,
        REGISTRY + "/token?" + urllib.parse.urlencode({"scope": f"repository:{REPOSITORY}:pull", "service": "ghcr.io"}),
    )
    if not isinstance(token, dict) or not isinstance(token.get("token"), str) or not token["token"]:
        raise ReleaseError("anonymous_registry_unavailable")
    status, headers, _ = transport.request(
        "GET",
        f"{REGISTRY}/v2/{REPOSITORY}/manifests/git-{sha}",
        {
            "Authorization": "Bearer " + token["token"],
            "Accept": ", ".join([
                "application/vnd.oci.image.index.v1+json",
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            ]),
        },
    )
    digest = headers.get("Docker-Content-Digest", "")
    media_type = headers.get("Content-Type", "").split(";", 1)[0].strip()
    if status != 200 or not DIGEST.fullmatch(digest) or media_type not in (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ):
        raise ReleaseError("invalid_registry_manifest")
    return f"ghcr.io/{REPOSITORY}@{digest}"


class Kube:
    def __init__(self, transport, host, port, token):
        if not host or not re.fullmatch(r"[a-zA-Z0-9.:-]+", host) or not port.isdigit():
            raise ReleaseError("invalid_kubernetes_endpoint")
        self.transport = transport
        self.url = f"https://{host}:{port}/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{DEPLOYMENT}"
        self.service = f"https://{host}:{port}/api/v1/namespaces/{NAMESPACE}/services/{DEPLOYMENT}"
        self.headers = {"Authorization": "Bearer " + token}

    def get(self, url):
        return fetch_json(self.transport, url, self.headers)

    def patch(self, ops):
        status, _, body = self.transport.request(
            "PATCH", self.url,
            {**self.headers, "Content-Type": "application/json-patch+json"},
            json.dumps(ops).encode("utf-8"),
        )
        if status != 200:
            raise ReleaseError("patch_status")
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise ReleaseError("invalid_patch_response") from None


def container(deployment):
    try:
        if deployment["metadata"]["name"] != DEPLOYMENT or deployment["metadata"]["namespace"] != NAMESPACE:
            raise ReleaseError("wrong_deployment")
        containers = deployment["spec"]["template"]["spec"]["containers"]
        matches = [(i, item) for i, item in enumerate(containers) if item["name"] == DEPLOYMENT]
        if len(matches) != 1:
            raise ReleaseError("invalid_containers")
        index, item = matches[0]
        if not isinstance(item["image"], str) or not isinstance(item["imagePullPolicy"], str):
            raise ReleaseError("invalid_container")
        return index, item["image"], item["imagePullPolicy"]
    except (KeyError, TypeError, IndexError):
        raise ReleaseError("invalid_deployment") from None


def metadata(deployment):
    try:
        meta = deployment["metadata"]
        rv = meta["resourceVersion"]
        annotations = meta.get("annotations")
        if not isinstance(rv, str) or not rv or (annotations is not None and not isinstance(annotations, dict)):
            raise ReleaseError("invalid_deployment_metadata")
        value = (annotations or {}).get(ATTEMPT_KEY)
        if value is not None and (not isinstance(value, str) or not value.isdecimal() or int(value) <= 0):
            raise ReleaseError("invalid_attempt_marker")
        return rv, annotations, value
    except (KeyError, TypeError):
        raise ReleaseError("invalid_deployment_metadata") from None


def patch_ops(index, old_image, old_policy, new_image, new_policy, rv, marker=None, run_id=None, annotations=None):
    path = f"/spec/template/spec/containers/{index}"
    ops = [
        {"op": "test", "path": "/metadata/resourceVersion", "value": rv},
        {"op": "test", "path": path + "/name", "value": DEPLOYMENT},
        {"op": "test", "path": path + "/image", "value": old_image},
        {"op": "test", "path": path + "/imagePullPolicy", "value": old_policy},
    ]
    if marker is not None:
        ops.append({"op": "test", "path": ATTEMPT_PATH, "value": marker})
    if run_id is not None:
        if annotations is None:
            ops.append({"op": "add", "path": "/metadata/annotations", "value": {ATTEMPT_KEY: str(run_id)}})
        else:
            ops.append({"op": "add", "path": ATTEMPT_PATH, "value": str(run_id)})
    ops.extend([
        {"op": "replace", "path": path + "/image", "value": new_image},
        {"op": "replace", "path": path + "/imagePullPolicy", "value": new_policy},
    ])
    return ops


def check_service(kube):
    service = kube.get(kube.service)
    try:
        if service["metadata"]["name"] != DEPLOYMENT or service["metadata"]["namespace"] != NAMESPACE or service["spec"]["type"] != "ClusterIP":
            raise ReleaseError("service_not_clusterip")
    except (KeyError, TypeError):
        raise ReleaseError("invalid_service") from None


def wait_ready(kube, image, policy, deadline):
    while time.monotonic() < deadline:
        current = kube.get(kube.url)
        _, live_image, live_policy = container(current)
        if (live_image, live_policy) != (image, policy):
            raise ReleaseError("deployment_changed_during_rollout")
        try:
            generation = current["metadata"]["generation"]
            desired = current["spec"]["replicas"]
            status = current.get("status", {})
            if (isinstance(generation, int) and isinstance(desired, int) and desired > 0
                    and status.get("observedGeneration", 0) >= generation
                    and status.get("updatedReplicas", 0) == desired
                    and status.get("readyReplicas", 0) == desired
                    and status.get("availableReplicas", 0) == desired):
                return
        except (KeyError, TypeError, ValueError):
            raise ReleaseError("invalid_rollout_status") from None
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    raise ReleaseError("rollout_timeout")


def reconcile(gh, kube):
    candidate = latest_build(gh)
    if candidate is None:
        LOG.info("event=no_eligible_build")
        return
    run_id, sha = candidate
    expected_digest = published_digest(gh, run_id, sha)
    image = image_digest(gh, sha)
    if image != f"ghcr.io/{REPOSITORY}@{expected_digest}":
        raise ReleaseError("registry_digest_mismatch")
    check_service(kube)
    original = kube.get(kube.url)
    index, old_image, old_policy = container(original)
    rv, annotations, marker = metadata(original)
    if marker is not None and run_id <= int(marker):
        LOG.info("event=run_already_attempted")
        return
    if main_head(gh) != sha:
        LOG.info("event=main_advanced")
        return
    # Failure may happen after the API commits a patch but before its response arrives.
    attempted = False
    try:
        attempted = True
        patched = kube.patch(patch_ops(index, old_image, old_policy, image, "IfNotPresent",
                                     rv, marker, run_id, annotations))
        _, actual_image, actual_policy = container(patched)
        if (actual_image, actual_policy) != (image, "IfNotPresent") or metadata(patched)[2] != str(run_id):
            raise ReleaseError("unexpected_patch_result")
        wait_ready(kube, image, "IfNotPresent", time.monotonic() + 300)
        check_service(kube)
    except ReleaseError:
        if attempted:
            try:
                live = kube.get(kube.url)
                live_index, live_image, live_policy = container(live)
                live_rv, _, live_marker = metadata(live)
                if (live_index == index and live_marker == str(run_id)
                        and (live_image, live_policy) == (image, "IfNotPresent")):
                    kube.patch(patch_ops(index, image, "IfNotPresent", old_image, old_policy,
                                         live_rv, str(run_id)))
                    wait_ready(kube, old_image, old_policy, time.monotonic() + 300)
                    LOG.warning("event=restored_previous_image")
                elif (live_index, live_image, live_policy, live_marker) != (index, old_image, old_policy, marker):
                    raise ReleaseError("concurrent_deployment_change")
            except ReleaseError:
                LOG.error("event=restore_failed_manual_inspection_required")
        raise
    LOG.info("event=rollout_ready sha=%s", sha)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if not token:
            raise ReleaseError("missing_service_account_token")
        kube = Kube(Transport(str(CA_PATH)), os.environ.get("KUBERNETES_SERVICE_HOST", ""),
                    os.environ.get("KUBERNETES_SERVICE_PORT", "443"), token)
        reconcile(Transport(), kube)
    except (OSError, ssl.SSLError):
        LOG.error("event=service_account_unavailable")
        return 1
    except ReleaseError as exc:
        LOG.error("event=release_failed reason=%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
