#!/usr/bin/env bash
# Hermes dispatch program for Docker: one container per Hermes session.
#   hermes config set plugins.entries.dispatch.settings.program /path/to/docker.sh
set -euo pipefail
op=$1 id=$2
shift 2

case "$op" in
	spawn)
		docker start "$id" >/dev/null 2>&1 ||
			docker run -d --init --name "$id" --label hermes.dispatch=1 \
				"$HERMES_SANDBOX_IMAGE" sleep infinity >/dev/null
		;;
	exec)
		# Forward the skill/env-passthrough values Hermes put in our environment (names only on argv).
		env_args=()
		for name in ${HERMES_SANDBOX_ENV:-}; do env_args+=(-e "$name"); done
		exec docker exec -i ${env_args[@]+"${env_args[@]}"} "$id" "$@"
		;;
	pause)
		docker stop "$id" >/dev/null
		;;
	delete)
		docker rm -f "$id" >/dev/null
		;;
	*)
		echo "unknown operation: $op" >&2
		exit 2
		;;
esac
