# Skill: Prometheus query crafting

How to use the observability tools well during interactive investigation.

- Prefer the purpose-built tools (obs__get_golden_signals, obs__get_workload_usage, obs__get_firing_alerts) over raw PromQL; they return pre-summarized, fleet-labeled results.
- When you do need raw PromQL via obs__query_instant, always scope by the fleet label and namespace: `{cluster="<cluster>", namespace="<ns>"}`. Unscoped queries over hundreds of clusters are wrong and slow.
- App discovery uses kube-state-metrics label conventions: Kubernetes label `app.kubernetes.io/name=foo` appears as `label_app_kubernetes_io_name="foo"` on `kube_pod_labels` (non-alphanumerics sanitized to `_`); legacy apps may use `label_app`.
- Useful patterns: restart churn `increase(kube_pod_container_status_restarts_total[1h])`; memory vs limit `container_memory_working_set_bytes` against `kube_pod_container_resource_limits{resource="memory"}`; CPU throttling ratio from `container_cpu_cfs_throttled_periods_total` over `container_cpu_cfs_periods_total`.
- Quote the query you ran when presenting a number, so the user can reproduce it.
