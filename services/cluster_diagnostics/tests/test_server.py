"""Verify API mapping, input bounds and omission of raw Kubernetes specs."""

import json
import ssl
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import pytest

from diagnostics import server


def test_pod_summary_does_not_return_env_or_secret_references():
    item = {
        "metadata": {"name": "worker", "namespace": "default"},
        "spec": {"nodeName": "caos", "containers": [{"name": "worker", "env": [
            {"name": "TOKEN", "value": "private-test-value"}]}],
            "volumes": [{"name": "credentials", "secret": {"secretName": "private-secret"}}]},
        "status": {"phase": "Running", "containerStatuses": [{
            "name": "worker", "ready": True, "restartCount": 2, "state": {"running": {}}}]},
    }
    result = json.dumps(server.summary("pods", item))
    assert "worker" in result and "Running" in result
    assert "private-test-value" not in result and "private-secret" not in result


def test_events_exclude_untrusted_message_text():
    value = server.summary("events", {"metadata": {"name": "warning"}, "reason": "Failed",
                                      "message": "leaked-credential", "type": "Warning"})
    assert "leaked-credential" not in json.dumps(value)


@pytest.mark.parametrize("query", [
    "kind=secrets", "kind=configmaps", "kind=pods&namespace=../default",
    "kind=pods&namespace=default%2Fother", "kind=pods&limit=1000", "kind=pods&limit=oops",
    "kind=pods&kind=nodes", "kind=pods&extra=value",
])
def test_resource_path_rejects_unsafe_queries(query):
    with pytest.raises(ValueError):
        server.resources(query)


def test_namespaced_resource_uses_fixed_api_path_and_pagination():
    response = {"items": [{"metadata": {"name": "worker", "namespace": "default"},
                           "status": {"phase": "Running"}}], "metadata": {"continue": "next+page"}}
    with patch.object(server, "kube_get", return_value=json.dumps(response).encode()) as upstream:
        result = server.resources("kind=pods&namespace=default&limit=10")
    assert upstream.call_args.args[0].startswith("/api/v1/namespaces/default/pods?")
    assert result["continue"] == "next+page" and result["items"][0]["phase"] == "Running"


def test_bad_kubernetes_list_fails_closed():
    with patch.object(server, "kube_get", return_value=b'{"items":["bad item"]}'):
        with pytest.raises(ValueError, match="invalid Kubernetes list"):
            server.resources("kind=pods")


def test_microk8s_tls_relaxation_preserves_identity_and_ca_validation(monkeypatch):
    original = ssl.create_default_context

    def context_without_local_ca(*, cafile):
        assert str(cafile) == str(server.CA_PATH)
        context = original()
        context.verify_flags |= ssl.VERIFY_X509_STRICT
        return context

    monkeypatch.setattr(server.ssl, "create_default_context", context_without_local_ca)
    context = server.kubernetes_tls_context()
    assert not context.verify_flags & ssl.VERIFY_X509_STRICT
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


@pytest.mark.parametrize("query", [
    "namespace=default&pod=../secrets", "namespace=default&pod=ok&tail=301",
    "namespace=default&pod=ok&tail=0", "namespace=default&pod=ok&previous=maybe",
    "namespace=default&pod=ok&container=../x", "namespace=default&pod=ok&follow=true",
])
def test_log_query_rejects_traversal_unbounded_or_streaming(query):
    with pytest.raises(ValueError):
        server.pod_logs(query)


def test_pod_logs_are_bounded_and_only_get_named_pod():
    with patch.object(server, "kube_get", return_value=b"one line\n") as upstream:
        result = server.pod_logs("namespace=observability&pod=dozzle-abc&tail=12")
    assert result["logs"] == "one line\n"
    assert upstream.call_args.args[0].startswith("/api/v1/namespaces/observability/pods/dozzle-abc/log?")
    assert "limitBytes=65536" in upstream.call_args.args[0]
    with patch.object(server, "kube_get", return_value=b"x" * 65537):
        with pytest.raises(ValueError):
            server.pod_logs("namespace=default&pod=worker")


def test_http_authentication_before_cluster_reads(monkeypatch):
    monkeypatch.setenv("DIAGNOSTICS_PORTFOLIO_KEY", "a" * 64)
    monkeypatch.setenv("DIAGNOSTICS_INFRA_KEY", "b" * 64)
    http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{http.server_port}/kinds"
        with patch.object(server, "kube_get", side_effect=AssertionError("unexpected API call")):
            for token in (None, "wrong"):
                request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
                with pytest.raises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request, timeout=2)
                assert error.value.code == 401
            for token in ("a" * 64, "b" * 64):
                request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
                with urllib.request.urlopen(request, timeout=2) as response:
                    assert response.status == 200 and "pods" in json.load(response)["kinds"]
        with patch.object(server, "kube_get", side_effect=urllib.error.HTTPError(
                "https://10.152.183.1", 403, "forbidden", {}, None)):
            request = urllib.request.Request(url.replace("/kinds", "/resources?kind=pods"),
                                             headers={"Authorization": "Bearer " + "a" * 64})
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=2)
            assert error.value.code == 502
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)
