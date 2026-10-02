#!/usr/bin/env bash
# Sobe as stacks do LXC em que está rodando.
# Uso: ./deploy.sh [pasta-do-lxc]
#   Sem argumento: detecta pela hostname (srv-apps -> lxc*-apps, srv-gateway -> lxc*-gateway, ...).
#   Com argumento: ./deploy.sh lxc103-apps
# Variáveis: ENVF=/caminho/.env (padrão: .env na raiz do repositório)
#            RECREATE=1 para usar --force-recreate
DIR="$(cd "$(dirname "$0")" && pwd)"
ENVF="${ENVF:-$DIR/.env}"
LXC="$1"

if [ -z "$LXC" ]; then
  NAME="$(hostname -s)"; NAME="${NAME#srv-}"
  LXC="$(cd "$DIR" && ls -d lxc*-"$NAME" 2>/dev/null | head -n1)"
  [ -n "$LXC" ] || { echo "Não achei pasta lxc*-$NAME para o hostname $(hostname -s). Passe a pasta como argumento."; exit 1; }
  echo ">> Detectado pela hostname: $LXC"
fi

[ -d "$DIR/$LXC" ] || { echo "Pasta $LXC não existe"; exit 1; }
[ -f "$ENVF" ] || { echo ".env não encontrado em $ENVF"; exit 1; }

# redes externas declaradas nos compose desta pasta
NETS=$(grep -rh -B1 'external: true' "$DIR/$LXC" | grep -E '^\s+[a-z_]+:\s*$' | tr -d ' :' | sort -u)
for n in $NETS; do
  docker network inspect "$n" >/dev/null 2>&1 && continue
  if [ "$n" = "gateway_net" ]; then   # AdGuard usa IP fixo 172.20.0.10
    docker network create --driver bridge --subnet 172.20.0.0/24 --gateway 172.20.0.1 "$n"
  else
    docker network create "$n"
  fi
done

# ordem: bancos primeiro, stack-agents por último
ORDER="stack-postgres stack-redis stack-meilisearch gateway-db"
LAST="stack-agents"
STACKS=""
for s in $ORDER; do [ -d "$DIR/$LXC/$s" ] && STACKS="$STACKS $s"; done
for s in $(ls -d "$DIR/$LXC"/*/ | xargs -n1 basename); do
  case " $ORDER $LAST " in *" $s "*) ;; *) STACKS="$STACKS $s";; esac
done
[ -d "$DIR/$LXC/$LAST" ] && STACKS="$STACKS $LAST"

FLAGS="-d"; [ -n "$RECREATE" ] && FLAGS="-d --force-recreate"
FALHAS=""
for s in $STACKS; do
  f="$DIR/$LXC/$s/docker-compose.yml"
  [ -f "$f" ] || { echo "!!! sem docker-compose.yml em $s"; continue; }
  echo "=== $s"
  docker compose --env-file "$ENVF" -f "$f" -p "$s" up $FLAGS || FALHAS="$FALHAS $s"
done
echo
docker ps --format 'table {{.Names}}\t{{.Status}}'
[ -z "$FALHAS" ] || { echo "!!! Falharam:$FALHAS"; exit 1; }
