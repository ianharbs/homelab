#!/bin/bash
# Compila o plugin dentro do CT 102 (rodar no host pve). Saída: out/Jellyfin.Plugin.Share.dll
# Uso: ./build.sh [deploy]   — "deploy" copia para /config/data/plugins e reinicia o Jellyfin.
set -euo pipefail
cd "$(dirname "$0")"
CT=102
W=/root/jfshare-build
VERSION=$(grep -oP '(?<=<Version>)[^<]+' src/Jellyfin.Plugin.Share.csproj)

pct exec $CT -- bash -c "mkdir -p $W/refs && rm -rf $W/src $W/out"
tar -C . -czf - src | pct exec $CT -- tar -C $W -xzf -
# DLLs de referência = as do Jellyfin que está rodando (garante ABI certa)
pct exec $CT -- bash -c "docker cp jellyfin:/usr/lib/jellyfin/bin/. $W/refs/ >/dev/null"
pct exec $CT -- docker run --rm -v $W:/w -w /w/src mcr.microsoft.com/dotnet/sdk:10.0 \
  bash -c "dotnet build -c Release -o /w/out -nologo -v q 2>&1 | grep -E 'error|warn CS|Build succeeded|Compilação' || true; test -f /w/out/Jellyfin.Plugin.Share.dll"
mkdir -p out
pct exec $CT -- cat $W/out/Jellyfin.Plugin.Share.dll > out/Jellyfin.Plugin.Share.dll
echo "ok: out/Jellyfin.Plugin.Share.dll ($VERSION)"

if [ "${1:-}" = "deploy" ]; then
  JV=$(pct exec $CT -- curl -s localhost:8096/System/Info/Public | python3 -c 'import json,sys;print(json.load(sys.stdin)["Version"])')
  ABI="${JV%.*}.0.0"; ABI="${ABI%%.*}.0.0.0"
  D="/config/data/plugins/Compartilhar_$VERSION"
  pct exec $CT -- bash -c "docker exec jellyfin sh -c 'rm -rf /config/data/plugins/Compartilhar_*; mkdir -p \"$D\"' && docker cp $W/out/Jellyfin.Plugin.Share.dll jellyfin:\"$D/\""
  pct exec $CT -- docker exec -i jellyfin sh -c "cat > \"$D/meta.json\"; chown -R abc:abc \"$D\"" <<META
{
  "category": "General",
  "changelog": "",
  "description": "Links públicos (sem conta) para séries, temporadas, episódios e filmes.",
  "guid": "8f3c2a61-5d4e-4b7a-9c1e-2a6f0d9b7e41",
  "name": "Compartilhar",
  "overview": "Compartilhe séries, temporadas e episódios com quem não tem conta",
  "owner": "ihome",
  "targetAbi": "$ABI",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "version": "$VERSION",
  "status": "Active",
  "autoUpdate": false,
  "assemblies": []
}
META
  pct exec $CT -- docker restart jellyfin >/dev/null
  echo "deploy: $D (targetAbi $ABI), jellyfin reiniciado"
fi
