#!/usr/bin/env bash
# Hermes dispatch program for Kubernetes: one pod per Hermes session, files on a PVC so pause keeps them.
set -euo pipefail
op=$1 id=$2
shift 2
ns=${HERMES_K8S_NAMESPACE:-hermes}

case "$op" in
	spawn)
		kubectl -n "$ns" get pvc "$id" >/dev/null 2>&1 || kubectl -n "$ns" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: $id}
spec: {accessModes: [ReadWriteOnce], resources: {requests: {storage: 10Gi}}}
EOF
		kubectl -n "$ns" get pod "$id" >/dev/null 2>&1 || kubectl -n "$ns" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata: {name: $id, labels: {hermes.dispatch: "1"}}
spec:
  containers:
  - name: sandbox
    image: $HERMES_SANDBOX_IMAGE
    command: [sleep, infinity]
    volumeMounts: [{name: home, mountPath: /root}]
  volumes: [{name: home, persistentVolumeClaim: {claimName: $id}}]
EOF
		kubectl -n "$ns" wait --for=condition=Ready "pod/$id" --timeout=240s >/dev/null
		;;
	exec)
		exec kubectl -n "$ns" exec -i "$id" -- "$@"
		;;
	pause)
		kubectl -n "$ns" delete pod "$id" --wait=false >/dev/null
		;;
	delete)
		kubectl -n "$ns" delete pod "$id" --ignore-not-found >/dev/null
		kubectl -n "$ns" delete pvc "$id" --ignore-not-found >/dev/null
		;;
	*)
		echo "unknown operation: $op" >&2
		exit 2
		;;
esac
