#!/usr/bin/env bash
# Uso: ./deploy.sh <pasta-do-lxc>   ex.: ./deploy.sh lxc103-apps
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
LXC="$1"
ENVF="${ENVF:-$DIR/.env}"
[ -d "$DIR/$LXC" ] || { echo "Pasta $LXC não existe"; exit 1; }
[ -f "$ENVF" ] || { echo ".env não encontrado em $ENVF"; exit 1; }

# redes externas usadas pelas stacks desta pasta
for n in $(grep -rhE '^\s+name:\s' "$DIR/$LXC" 2>/dev/null | awk '{print $2}' | sort -u); do :; done
for n in $(grep -rh -B1 'external: true' "$DIR/$LXC" | grep -E '^\s+[a-z_]+:$' | tr -d ' :' | sort -u); do
  docker network inspect "$n" >/dev/null 2>&1 || docker network create "$n"
done

# ordem: bancos primeiro, agents por último
ORDER="stack-postgres stack-redis stack-meilisearch gateway-db"
ALL=$(ls -d "$DIR/$LXC"/*/ | xargs -n1 basename)
LAST="stack-agents"
for s in $ORDER; do [ -d "$DIR/$LXC/$s" ] && STACKS="$STACKS $s"; done
for s in $ALL; do
  case " $ORDER $LAST " in *" $s "*) ;; *) STACKS="$STACKS $s";; esac
done
[ -d "$DIR/$LXC/$LAST" ] && STACKS="$STACKS $LAST"

for s in $STACKS; do
  f="$DIR/$LXC/$s/docker-compose.yml"
  [ -f "$f" ] || continue
  echo "=== $s"
  docker compose --env-file "$ENVF" -f "$f" -p "$s" up -d
done
docker ps