# Maintenance

Pod GC CronJob and RBAC for cleaning up stuck pods (Unknown, Failed, Error).

## Apply

```bash
kubectl apply -k k8s/maintenance
```

Or apply everything from the k8s root (includes maintenance):

```bash
kubectl apply -k k8s
```
