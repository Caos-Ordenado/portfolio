"""Read-only Kubernetes summaries and bounded pod logs, without terminal API credentials."""

import json
import hmac
import os
import re
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
CA_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
KIND_PATHS = {
    "namespaces": ("/api/v1", False),
    "nodes": ("/api/v1", False),
    "persistentvolumes": ("/api/v1", False),
    "pods": ("/api/v1", True),
    "events": ("/api/v1", True),
    "services": ("/api/v1", True),
    "endpoints": ("/api/v1", True),
    "persistentvolumeclaims": ("/api/v1", True),
    "serviceaccounts": ("/api/v1", True),
    "deployments": ("/apis/apps/v1", True),
    "replicasets": ("/apis/apps/v1", True),
    "daemonsets": ("/apis/apps/v1", True),
    "statefulsets": ("/apis/apps/v1", True),
    "jobs": ("/apis/batch/v1", True),
    "cronjobs": ("/apis/batch/v1", True),
    "networkpolicies": ("/apis/networking.k8s.io/v1", True),
    "ingresses": ("/apis/networking.k8s.io/v1", True),
    "ingressroutes": ("/apis/traefik.io/v1alpha1", True),
    "podmetrics": ("/apis/metrics.k8s.io/v1beta1", True),
    "nodemetrics": ("/apis/metrics.k8s.io/v1beta1", False),
}
API_NAMES = {"podmetrics": "pods", "nodemetrics": "nodes"}
NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")
CONTAINER = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\Z")
SLOTS = threading.BoundedSemaphore(8)


def required_name(value: str, *, container: bool = False) -> str:
    if not (CONTAINER if container else NAME).fullmatch(value):
        raise ValueError("invalid Kubernetes name")
    return value


def params(query: str, permitted: set[str]) -> dict[str, str]:
    data = urllib.parse.parse_qs(query, keep_blank_values=True, strict_parsing=True)
    if not data.keys() <= permitted or any(len(values) != 1 for values in data.values()):
        raise ValueError("invalid query")
    return {key: values[0] for key, values in data.items()}


def kubernetes_tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(CA_PATH))
    # Home MicroK8s CA lacks X.509 keyUsage; Python 3.13 enables STRICT by
    # default. Keep CA-chain and IP/hostname validation, only relax that flag.
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


def kube_get(path: str) -> bytes:
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if not re.fullmatch(r"[0-9.]+", host) or port != "443":
        raise ValueError("invalid Kubernetes API configuration")
    url = f"https://{host}:{port}{path}"
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + TOKEN_PATH.read_text().strip(),
        "Accept": "application/json",
    })
    with urllib.request.urlopen(request, context=kubernetes_tls_context(), timeout=10) as response:
        data = response.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("Kubernetes response too large")
    return data


def summary(kind: str, item: dict) -> dict:
    metadata = item.get("metadata") or {}
    status = item.get("status") or {}
    result = {"name": metadata.get("name"), "namespace": metadata.get("namespace"),
              "created": metadata.get("creationTimestamp")}
    if kind == "pods":
        result.update({"phase": status.get("phase"), "reason": status.get("reason"),
                       "node": (item.get("spec") or {}).get("nodeName"),
                       "containers": [{"name": c.get("name"), "ready": c.get("ready"),
                                       "restarts": c.get("restartCount"),
                                       "state": next(iter((c.get("state") or {}).keys()), None)}
                                      for c in status.get("containerStatuses") or []]})
    elif kind == "events":
        result.update({"reason": item.get("reason"), "type": item.get("type"),
                       "involved": (item.get("involvedObject") or {}).get("name")})
    elif kind in ("podmetrics", "nodemetrics"):
        result["usage"] = [{"name": c.get("name"), "usage": c.get("usage")}
                           for c in (item.get("containers") or [])] if kind == "podmetrics" else item.get("usage")
    else:
        result["status"] = {key: status[key] for key in (
            "phase", "replicas", "readyReplicas", "availableReplicas", "unavailableReplicas",
            "currentNumberScheduled", "numberReady", "succeeded", "failed", "active") if key in status}
        result["conditions"] = [{key: condition.get(key) for key in ("type", "status", "reason")}
                                for condition in (status.get("conditions") or [])[:10]]
    return result


def resources(query: str) -> dict:
    arguments = params(query, {"kind", "namespace", "limit", "continue"})
    kind = arguments.get("kind", "")
    if kind not in KIND_PATHS:
        raise ValueError("unsupported resource kind")
    prefix, namespaced = KIND_PATHS[kind]
    namespace = arguments.get("namespace", "")
    if namespace:
        if not namespaced:
            raise ValueError("resource is cluster-scoped")
        required_name(namespace)
        prefix += f"/namespaces/{namespace}"
    limit = int(arguments.get("limit", "50"))
    if not 1 <= limit <= 100:
        raise ValueError("invalid limit")
    continuation = arguments.get("continue", "")
    if len(continuation) > 1024:
        raise ValueError("invalid continuation")
    resource = API_NAMES.get(kind, kind)
    path = prefix + "/" + resource + "?" + urllib.parse.urlencode({"limit": limit, "continue": continuation})
    response = json.loads(kube_get(path))
    if (not isinstance(response, dict) or not isinstance(response.get("items"), list)
            or any(not isinstance(item, dict) for item in response["items"])):
        raise ValueError("invalid Kubernetes list response")
    return {"kind": kind, "items": [summary(kind, item) for item in response["items"][:limit]],
            "continue": (response.get("metadata") or {}).get("continue", "")}


def pod_logs(query: str) -> dict:
    arguments = params(query, {"namespace", "pod", "container", "tail", "previous"})
    namespace = required_name(arguments.get("namespace", ""))
    pod = required_name(arguments.get("pod", ""))
    container = arguments.get("container", "")
    if container:
        required_name(container, container=True)
    tail = int(arguments.get("tail", "100"))
    if not 1 <= tail <= 300 or arguments.get("previous", "false") not in ("true", "false"):
        raise ValueError("invalid log bounds")
    path = f"/api/v1/namespaces/{namespace}/pods/{pod}/log?" + urllib.parse.urlencode({
        "container": container, "tailLines": tail, "limitBytes": 65536,
        "timestamps": "true", "previous": arguments.get("previous", "false"),
    })
    data = kube_get(path)
    if len(data) > 65536:
        raise ValueError("log response exceeds limit")
    return {"namespace": namespace, "pod": pod, "logs": data.decode("utf-8", "replace")}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        pass

    def respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not SLOTS.acquire(blocking=False):
            self.respond(503, {"error": "busy"})
            return
        try:
            if len(self.path) > 4096:
                raise ValueError("request too large")
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path not in ("/health", "/ready"):
                supplied = self.headers.get("Authorization", "")
                keys = (os.environ["DIAGNOSTICS_PORTFOLIO_KEY"], os.environ["DIAGNOSTICS_INFRA_KEY"])
                if not any(hmac.compare_digest(supplied, "Bearer " + key) for key in keys):
                    self.respond(401, {"error": "unauthorized"})
                    return
            if parsed.path == "/health":
                result = {"status": "ok"}
            elif parsed.path == "/ready":
                kube_get("/api/v1/namespaces?limit=1")
                result = {"status": "ready"}
            elif parsed.path == "/kinds":
                result = {"kinds": sorted(KIND_PATHS)}
            elif parsed.path == "/resources":
                result = resources(parsed.query)
            elif parsed.path == "/logs":
                result = pod_logs(parsed.query)
            else:
                self.respond(404, {"error": "not found"})
                return
            self.respond(200, result)
        except (ValueError, KeyError, TypeError, AttributeError, json.JSONDecodeError):
            self.respond(400, {"error": "invalid request or upstream response"})
        except urllib.error.HTTPError as error:
            self.respond(502, {"error": f"Kubernetes API returned {error.code}"})
        except (OSError, urllib.error.URLError):
            self.respond(503, {"error": "Kubernetes API unavailable"})
        finally:
            SLOTS.release()


if __name__ == "__main__":
    for key_name in ("DIAGNOSTICS_PORTFOLIO_KEY", "DIAGNOSTICS_INFRA_KEY"):
        key = os.environ[key_name]
        if len(key) != 64 or not re.fullmatch(r"[0-9a-f]{64}", key):
            raise RuntimeError("diagnostics key configuration unavailable")
    ThreadingHTTPServer(("0.0.0.0", 8002), Handler).serve_forever()
