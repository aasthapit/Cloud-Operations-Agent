#!/usr/bin/env bash
# Prepare the local kind fleet so live mode has real state to read.
#
# Idempotent by construction: every object is server-side applied, so a second
# run is a no-op apart from re-hashing the Prometheus config. Scope is pinned
# to the six kind-acm-* contexts; the script refuses to touch anything else on
# the machine.
#
# Usage: make live-prep  (or ./deploy/live/live-prep.sh [cluster ...])
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLEET=(acm-hub-1 acm-hub-2 acm-spoke-1a acm-spoke-1b acm-spoke-2a acm-spoke-2b)
CLUSTERS=("${@:-}")
[[ -z "${CLUSTERS[*]:-}" ]] && CLUSTERS=("${FLEET[@]}")

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
sub() { printf '    %s\n' "$*"; }
die() { printf '\033[1;31mfail:\033[0m %s\n' "$*" >&2; exit 1; }

in_fleet() {
  local c
  for c in "${FLEET[@]}"; do [[ "$c" == "$1" ]] && return 0; done
  return 1
}

kc() { kubectl --context "kind-$CLUSTER" "$@"; }

# Prometheus config carries the cluster's own name as the `cluster` external
# label, and a hash of the rendered config so a config change actually rolls
# the pod (a ConfigMap edit alone would not).
render_prometheus() {
  local rendered
  rendered="$(sed "s/__CLUSTER_NAME__/$CLUSTER/g" "$DIR/20-prometheus.yaml")"
  local hash
  hash="$(printf '%s' "$rendered" | shasum -a 256 | cut -c1-12)"
  printf '%s' "$rendered" | sed "s/__CONFIG_HASH__/$hash/g"
}

# Which demo workloads belong on which cluster. Placement is DISCOVERED at
# runtime from kube_pod_labels; this map is only what live-prep deploys.
demo_manifests() {
  case "$CLUSTER" in
    acm-spoke-1a) echo "$DIR/30-payments-prod-healthy.yaml" ;;
    acm-spoke-2a) echo "$DIR/31-payments-prod-degraded.yaml" ;;
    acm-spoke-1b) echo "$DIR/32-logistics-dev.yaml" ;;
    *) echo "" ;;
  esac
}

for CLUSTER in "${CLUSTERS[@]}"; do
  in_fleet "$CLUSTER" || die "$CLUSTER is not part of the cloud-ops kind fleet; refusing to touch it"
  kubectl config get-contexts -o name | grep -qx "kind-$CLUSTER" \
    || die "kubeconfig has no context kind-$CLUSTER"

  log "$CLUSTER: monitoring stack"
  kc apply --server-side --force-conflicts -f "$DIR/00-monitoring.yaml" >/dev/null
  kc apply --server-side --force-conflicts -f "$DIR/10-kube-state-metrics.yaml" >/dev/null
  render_prometheus | kc apply --server-side --force-conflicts -f - >/dev/null
  sub "applied kube-state-metrics + prometheus (external label cluster=$CLUSTER)"

  for manifest in $(demo_manifests); do
    log "$CLUSTER: demo workloads $(basename "$manifest")"
    kc apply --server-side --force-conflicts -f "$manifest" >/dev/null
  done

  log "$CLUSTER: waiting for rollouts"
  kc -n monitoring rollout status deploy/kube-state-metrics --timeout=180s | sed 's/^/    /'
  kc -n monitoring rollout status deploy/prometheus --timeout=180s | sed 's/^/    /'
  case "$CLUSTER" in
    acm-spoke-1a)
      kc -n payments-prod rollout status deploy/payments-api --timeout=180s | sed 's/^/    /' ;;
    acm-spoke-1b)
      kc -n logistics-dev rollout status deploy/inventory-sync --timeout=180s | sed 's/^/    /' ;;
    acm-spoke-2a)
      # Deliberately crash-looping: this deployment never becomes available, so
      # wait for the pods to exist rather than for a rollout that cannot finish.
      kc -n payments-prod wait --for=condition=PodScheduled pod \
        -l app=payments-api --timeout=180s | sed 's/^/    /' ;;
  esac

  sub "nodes: $(kc get nodes --no-headers | wc -l | tr -d ' ') | $(kc get nodes --no-headers | awk '{print $1"="$2}' | tr '\n' ' ')"
done

log "fleet ready. Verify with: make live-smoke"
