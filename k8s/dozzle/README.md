# Dozzle on home MicroK8s

Dozzle uses the `observability/dozzle` ServiceAccount to read pod logs and pod
metrics across namespaces. The `metrics.k8s.io/pods` list permission is required
by the running v8 image; without it Dozzle logs `failed to get pod metrics`,
panics, and restarts even though `/logs/` can briefly return HTTP 200.

After a reviewed PR and confirmation that `kubectl config current-context` is
home `microk8s`, apply only `kubectl apply -f k8s/dozzle/rbac.yaml` from the
`portfolio/` root. Verify with
`kubectl auth can-i list pods.metrics.k8s.io --as=system:serviceaccount:observability:dozzle --all-namespaces`,
`kubectl -n observability get pods -l app=dozzle`, recent Dozzle logs, and
`curl -I http://home.server:30080/logs/`. Wait longer than the previous crash
interval and confirm the restart count stays constant and the UI loads logs.

If unrelated access is observed, restore the prior `k8s/dozzle/rbac.yaml` from
the reviewed revision and apply that file, then recheck the ServiceAccount's
effective permissions. Do not grant write access or give the coding terminal
Kubernetes credentials.
