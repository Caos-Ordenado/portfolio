"""One-shot, outbound-only promotion of three trusted images to four named Deployments."""
import base64
import binascii
import hashlib
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

REPO = "https://api.github.com/repos/Caos-Ordenado/portfolio"
WORKFLOW = "code-agent-build.yml"
RUN_PAGE = "https://github.com/Caos-Ordenado/portfolio/actions/runs/"
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
TARGETS = (
    ("code-agent-pr-broker", "pr-broker", "code-agent-pr-broker"),
    ("cluster-diagnostics", "diagnostics", "cluster-diagnostics"),
    ("open-terminal", "open-terminal", "open-terminal-hardened"),
    ("infra-terminal", "infra-terminal", "open-terminal-hardened"),
)
MARKER = "automation.reyops.com/code-agent-attempted-run-id"
MARKER_PATH = "/metadata/annotations/automation.reyops.com~1code-agent-attempted-run-id"
RESTART = "automation.reyops.com/agents-restart"
RESTART_PATH = "/spec/template/metadata/annotations/automation.reyops.com~1agents-restart"
INSTRUCTIONS = (
    ("open-terminal-agents", "k8s/code-agent/terminal-AGENTS.md"),
    ("infra-terminal-agents", "k8s/code-agent/infra-review/infra-terminal-AGENTS.md"),
)
MAX_INSTRUCTIONS = 65536
LOG = logging.getLogger("code-agent-releaser")


class ReleaseError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Transport:
    def __init__(self, ca=None):
        context = ssl.create_default_context(cafile=ca)
        if ca:
            context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context), NoRedirect())

    def request(self, method, url, headers=None, data=None):
        try:
            with self.opener.open(urllib.request.Request(url, headers=headers or {}, data=data, method=method), timeout=10) as response:
                body = response.read(2_000_001)
                if len(body) > 2_000_000:
                    raise ReleaseError("oversize_response")
                return response.status, response.headers, body
        except urllib.error.HTTPError as exc:
            raise ReleaseError(f"http_{exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ReleaseError("network_failure") from None


def get(transport, url, headers=None):
    status, _, body = transport.request("GET", url, headers)
    if status != 200:
        raise ReleaseError("bad_http_status")
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise ReleaseError("invalid_json") from None


def head(transport):
    ref = get(transport, REPO + "/git/ref/heads/main")
    if not isinstance(ref, dict) or ref.get("ref") != "refs/heads/main" or not isinstance(ref.get("object"), dict) or ref["object"].get("type") != "commit":
        raise ReleaseError("invalid_main_ref")
    sha = ref["object"].get("sha")
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ReleaseError("invalid_main_sha")
    return sha


def candidate(transport):
    sha = head(transport)
    runs = get(transport, REPO + f"/actions/workflows/{WORKFLOW}/runs?branch=main&event=push&per_page=1")
    if not isinstance(runs, dict) or type(runs.get("total_count")) is not int or not isinstance(runs.get("workflow_runs"), list) or len(runs["workflow_runs"]) > 1 or runs["total_count"] < len(runs["workflow_runs"]):
        raise ReleaseError("invalid_runs")
    if not runs["workflow_runs"]:
        if runs["total_count"]:
            raise ReleaseError("missing_latest_run")
        return None
    run = runs["workflow_runs"][0]
    if not isinstance(run, dict) or run.get("event") != "push" or run.get("head_branch") != "main" or type(run.get("id")) is not int or run["id"] <= 0 or run.get("head_sha") != sha:
        raise ReleaseError("stale_or_invalid_run")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        return None
    jobs = get(transport, REPO + f"/actions/runs/{run['id']}/jobs?per_page=100")
    if not isinstance(jobs, dict) or type(jobs.get("total_count")) is not int or not isinstance(jobs.get("jobs"), list) or jobs["total_count"] != len(jobs["jobs"]):
        raise ReleaseError("invalid_jobs")
    builds = [j for j in jobs["jobs"] if isinstance(j, dict) and j.get("name") == "build"]
    if len(builds) != 1 or builds[0].get("status") != "completed" or builds[0].get("conclusion") != "success":
        raise ReleaseError("missing_successful_build")
    return run["id"], sha


def digest_status(transport, run_id, sha, image):
    statuses = get(transport, REPO + f"/commits/{sha}/statuses?per_page=100")
    if not isinstance(statuses, list) or len(statuses) > 100:
        raise ReleaseError("invalid_statuses")
    for item in statuses:
        if not isinstance(item, dict):
            raise ReleaseError("invalid_status")
        if item.get("context") == f"code-agent-{image}-image":
            creator = item.get("creator")
            digest = item.get("description")
            if (item.get("state") != "success" or not isinstance(creator, dict) or creator.get("login") != "github-actions[bot]"
                    or not isinstance(digest, str) or not DIGEST.fullmatch(digest) or item.get("target_url") != RUN_PAGE + str(run_id)):
                raise ReleaseError("invalid_image_status")
            return digest
    raise ReleaseError("status_window_exhausted" if len(statuses) == 100 else "missing_image_status")


def registry_digest(transport, sha, image):
    repo = f"caos-ordenado/{image}"
    token = get(transport, "https://ghcr.io/token?" + urllib.parse.urlencode({"scope": f"repository:{repo}:pull", "service": "ghcr.io"}))
    if not isinstance(token, dict) or not isinstance(token.get("token"), str) or not token["token"]:
        raise ReleaseError("anonymous_registry_unavailable")
    status, headers, _ = transport.request("GET", f"https://ghcr.io/v2/{repo}/manifests/git-{sha}", {
        "Authorization": "Bearer " + token["token"],
        "Accept": ", ".join(("application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json", "application/vnd.docker.distribution.manifest.v2+json")),
    })
    digest = headers.get("Docker-Content-Digest", "")
    if status != 200 or not DIGEST.fullmatch(digest) or headers.get("Content-Type", "").split(";", 1)[0] not in (
        "application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json", "application/vnd.docker.distribution.manifest.v2+json"
    ):
        raise ReleaseError("invalid_registry_manifest")
    return digest


def instruction(transport, path, sha):
    obj = get(transport, REPO + "/contents/" + path + "?ref=" + sha)
    if not isinstance(obj, dict) or obj.get("type") != "file" or obj.get("path") != path or obj.get("encoding") != "base64":
        raise ReleaseError("invalid_instruction_metadata")
    if type(obj.get("size")) is not int or not 0 < obj["size"] <= MAX_INSTRUCTIONS or not isinstance(obj.get("content"), str) or not isinstance(obj.get("sha"), str):
        raise ReleaseError("invalid_instruction_metadata")
    try:
        raw = base64.b64decode("".join(obj["content"].splitlines()), validate=True)
        text = raw.decode("utf-8")
    except (ValueError, UnicodeError, binascii.Error):
        raise ReleaseError("invalid_instruction_content") from None
    blob_sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    if len(raw) != obj["size"] or len(raw) > MAX_INSTRUCTIONS or blob_sha != obj["sha"] or "\0" in text:
        raise ReleaseError("instruction_hash_mismatch")
    return text


class Kube:
    def __init__(self, transport, host, port, token):
        if not host or not re.fullmatch(r"[a-zA-Z0-9.:-]+", host) or not port.isdigit() or not token:
            raise ReleaseError("invalid_kubernetes_endpoint")
        self.transport = transport
        self.base = f"https://{host}:{port}/apis/apps/v1/namespaces/code-agent/deployments/"
        self.config_base = f"https://{host}:{port}/api/v1/namespaces/code-agent/configmaps/"
        self.headers = {"Authorization": "Bearer " + token}

    def read(self, name):
        return get(self.transport, self.base + name, self.headers)

    def patch(self, name, ops):
        status, _, body = self.transport.request("PATCH", self.base + name, {**self.headers, "Content-Type": "application/json-patch+json"}, json.dumps(ops).encode())
        if status != 200:
            raise ReleaseError("patch_status")
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise ReleaseError("invalid_patch_response") from None

    def read_config(self, name):
        return get(self.transport, self.config_base + name, self.headers)

    def patch_config(self, name, ops):
        status, _, body = self.transport.request("PATCH", self.config_base + name, {**self.headers, "Content-Type": "application/json-patch+json"}, json.dumps(ops).encode())
        if status != 200:
            raise ReleaseError("config_patch_status")
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise ReleaseError("invalid_config_patch_response") from None


def config_snapshot(obj, name):
    try:
        meta = obj["metadata"]
        value = obj["data"]["AGENTS.md"]
        if (meta["name"] != name or meta["namespace"] != "code-agent"
                or not isinstance(meta["resourceVersion"], str) or not meta["resourceVersion"]
                or not isinstance(value, str)):
            raise ReleaseError("invalid_config_map")
        return meta["resourceVersion"], value
    except (KeyError, TypeError):
        raise ReleaseError("invalid_config_map") from None


def config_ops(rv, old, new):
    return [{"op": "test", "path": "/metadata/resourceVersion", "value": rv},
            {"op": "test", "path": "/data/AGENTS.md", "value": old},
            {"op": "replace", "path": "/data/AGENTS.md", "value": new}]


def restart_state(deployment):
    try:
        template = deployment["spec"]["template"]
        metadata = template.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ReleaseError("invalid_template_annotations")
        annotations = metadata.get("annotations")
        if annotations is not None and not isinstance(annotations, dict):
            raise ReleaseError("invalid_template_annotations")
        old = (annotations or {}).get(RESTART)
        if old is not None and not isinstance(old, str):
            raise ReleaseError("invalid_template_annotations")
        return metadata, annotations, old
    except (KeyError, TypeError):
        raise ReleaseError("invalid_template_annotations") from None


def restart_ops(metadata, annotations, previous, value):
    if previous is not None:
        return [{"op": "test", "path": RESTART_PATH, "value": previous},
                {"op": "replace", "path": RESTART_PATH, "value": value}] if value is not None else [
                {"op": "test", "path": RESTART_PATH, "value": previous}, {"op": "remove", "path": RESTART_PATH}]
    if value is None:
        return []
    if not metadata:
        return [{"op": "add", "path": "/spec/template/metadata", "value": {"annotations": {RESTART: value}}}]
    if annotations is None:
        return [{"op": "add", "path": "/spec/template/metadata/annotations", "value": {RESTART: value}}]
    return [{"op": "add", "path": RESTART_PATH, "value": value}]


def snapshot(deployment, name, container):
    try:
        meta = deployment["metadata"]
        if meta["name"] != name or meta["namespace"] != "code-agent" or not isinstance(meta["resourceVersion"], str):
            raise ReleaseError("wrong_deployment")
        containers = deployment["spec"]["template"]["spec"]["containers"]
        matches = [(i, c) for i, c in enumerate(containers) if c["name"] == container]
        if len(matches) != 1:
            raise ReleaseError("invalid_containers")
        index, item = matches[0]
        image, policy = item["image"], item["imagePullPolicy"]
        if not isinstance(image, str) or not image or policy not in ("Never", "Always", "IfNotPresent"):
            raise ReleaseError("invalid_image")
        annotations = meta.get("annotations")
        if annotations is not None and not isinstance(annotations, dict):
            raise ReleaseError("invalid_annotations")
        marker = (annotations or {}).get(MARKER)
        if marker is not None and (not isinstance(marker, str) or not marker.isdecimal() or int(marker) <= 0):
            raise ReleaseError("invalid_marker")
        return index, image, policy, meta["resourceVersion"], annotations, marker
    except (KeyError, TypeError, IndexError):
        raise ReleaseError("invalid_deployment") from None


def patch_ops(name, index, old, new, rv, marker, annotations, run_id=None):
    path = f"/spec/template/spec/containers/{index}"
    ops = [{"op": "test", "path": "/metadata/resourceVersion", "value": rv},
           {"op": "test", "path": path + "/name", "value": name},
           {"op": "test", "path": path + "/image", "value": old[0]},
           {"op": "test", "path": path + "/imagePullPolicy", "value": old[1]}]
    if marker is not None:
        ops.append({"op": "test", "path": MARKER_PATH, "value": marker})
    if run_id is not None:
        ops.append({"op": "add", "path": MARKER_PATH if annotations is not None else "/metadata/annotations", "value": str(run_id) if annotations is not None else {MARKER: str(run_id)}})
    return ops + [{"op": "replace", "path": path + "/image", "value": new[0]}, {"op": "replace", "path": path + "/imagePullPolicy", "value": new[1]}]


def wait_ready(kube, name, container, desired, deadline, restart=None):
    while time.monotonic() < deadline:
        current = kube.read(name)
        if snapshot(current, name, container)[1:3] != desired:
            raise ReleaseError("concurrent_rollout_change")
        if restart is not None and restart_state(current)[2] != restart:
            raise ReleaseError("concurrent_rollout_change")
        try:
            generation = current["metadata"]["generation"]
            replicas = current["spec"]["replicas"]
            status = current.get("status", {})
            if name in ("open-terminal", "infra-terminal") and type(replicas) is int and replicas == 0:
                raise ReleaseError("terminal_zero_replicas")
            if (type(generation) is int and type(replicas) is int and replicas >= 0
                    and type(status.get("observedGeneration")) is int and status["observedGeneration"] >= generation
                    and all(status.get(k, 0) == replicas for k in ("updatedReplicas", "readyReplicas", "availableReplicas"))):
                return
        except (KeyError, TypeError):
            raise ReleaseError("invalid_rollout_status") from None
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    raise ReleaseError("rollout_timeout")


def restore(kube, applied, run_id):
    failures = []
    # Restore ConfigMaps before restarting terminal Pods: subPath snapshots
    # instruction data only when a new Pod is created.
    ordered = [item for item in reversed(applied) if item[0] == "config"]
    ordered += [item for item in reversed(applied) if item[0] == "deployment"]
    for item in ordered:
        kind, name, *details = item
        try:
            if kind == "config":
                old, new, original_rv, committed_rv = details
                rv, value = config_snapshot(kube.read_config(name), name)
                if value == old:
                    if rv != original_rv:
                        raise ReleaseError("concurrent_rollback_change")
                    continue
                if value != new or (committed_rv is not None and rv != committed_rv):
                    raise ReleaseError("concurrent_rollback_change")
                if config_snapshot(kube.patch_config(name, config_ops(rv, new, old)), name)[1] != old:
                    raise ReleaseError("unexpected_restore_result")
                continue
            container, index, old, new, previous_marker, previous_restart, restart = details
            live = kube.read(name)
            i, image, policy, rv, annotations, marker = snapshot(live, name, container)
            metadata, template_annotations, current_restart = restart_state(live)
            if (i, image, policy, current_restart) == (index, old[0], old[1], previous_restart) and marker in (previous_marker, str(run_id)):
                continue  # PATCH did not commit.
            if i != index or marker != str(run_id) or (image, policy) != new or current_restart != restart:
                raise ReleaseError("concurrent_rollback_change")
            ops = patch_ops(container, i, new, old, rv, marker, annotations)
            ops += restart_ops(metadata, template_annotations, restart, previous_restart)
            kube.patch(name, ops)
            wait_ready(kube, name, container, old, time.monotonic() + 300)
        except ReleaseError:
            failures.append(name)
    if failures:
        LOG.error("event=restore_failed names=%s", ",".join(failures))


def reconcile(gh, kube):
    found = candidate(gh)
    if found is None:
        LOG.info("event=no_eligible_build")
        return
    run_id, sha = found
    contents = {name: instruction(gh, path, sha) for name, path in INSTRUCTIONS}
    images = {}
    for _, _, image in TARGETS:
        if image not in images:
            expected = digest_status(gh, run_id, sha, image)
            if registry_digest(gh, sha, image) != expected:
                raise ReleaseError("registry_digest_mismatch")
            images[image] = f"ghcr.io/caos-ordenado/{image}@{expected}"
    originals = []
    configs = []
    for name, _ in INSTRUCTIONS:
        rv, old = config_snapshot(kube.read_config(name), name)
        configs.append((name, rv, old, contents[name]))
    for name, container, image in TARGETS:
        live = kube.read(name)
        state = snapshot(live, name, container)
        restart = restart_state(live) if name in ("open-terminal", "infra-terminal") else None
        if restart is not None and live["spec"].get("replicas") == 0:
            raise ReleaseError("terminal_zero_replicas")
        if state[5] is not None and int(state[5]) >= run_id:
            LOG.info("event=run_already_attempted")
            return
        originals.append((name, container, images[image], state, restart))
    if head(gh) != sha:
        raise ReleaseError("main_advanced")
    applied = []
    try:
        config_versions = {}
        for name, rv, old, new in configs:
            if old != new:
                record = ["config", name, old, new, rv, None]
                applied.append(record)
                patched_rv, patched_value = config_snapshot(kube.patch_config(name, config_ops(rv, old, new)), name)
                record[5] = patched_rv
                if patched_value != new:
                    raise ReleaseError("unexpected_config_patch_result")
                config_versions[name] = patched_rv
            else:
                config_versions[name] = rv
        for name, container, image, state, restart in originals:
            index, old_image, old_policy, rv, annotations, marker = state
            old, new = (old_image, old_policy), (image, "IfNotPresent")
            restart_value = f"{sha}-{run_id}" if restart is not None else None
            # Record intent before the PATCH: a lost API response may still have committed.
            applied.append(("deployment", name, container, index, old, new, marker,
                            restart[2] if restart is not None else None, restart_value))
            ops = patch_ops(container, index, old, new, rv, marker, annotations, run_id)
            if restart is not None:
                ops += restart_ops(*restart, restart_value)
            patched = kube.patch(name, ops)
            if snapshot(patched, name, container)[1:3] != new or snapshot(patched, name, container)[5] != str(run_id):
                raise ReleaseError("unexpected_patch_result")
            if restart is not None and restart_state(patched)[2] != restart_value:
                raise ReleaseError("unexpected_patch_result")
            wait_ready(kube, name, container, new, time.monotonic() + 300, restart_value)
        for name, _, _, new in configs:
            if config_snapshot(kube.read_config(name), name) != (config_versions[name], new):
                raise ReleaseError("concurrent_config_change")
    except ReleaseError:
        restore(kube, applied, run_id)
        raise
    LOG.info("event=rollout_ready sha=%s", sha)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text(encoding="utf-8").strip()
        kube = Kube(Transport("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"), os.getenv("KUBERNETES_SERVICE_HOST", ""), os.getenv("KUBERNETES_SERVICE_PORT", "443"), token)
        reconcile(Transport(), kube)
        return 0
    except (OSError, ssl.SSLError):
        LOG.error("event=service_account_unavailable")
    except ReleaseError as exc:
        LOG.error("event=release_failed reason=%s", exc)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
