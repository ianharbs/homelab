#!/bin/bash
# Backup das fotos (Immich) e dos documentos (Paperless) — roda no host pve via backup-gdrive.timer (05:00).
# 1. Paperless: document_exporter no CT 103 (pve3) e cópia do export para o HD de 24 TB
#    (/mnt/pve/media/data/backup/paperless/atual; alterados/apagados ficam 30 dias em _removidos/<data>).
# 2. Google Drive (remote rclone "gdrive", escopo drive.file, pasta homelab-backup/):
#    - immich/atual/{library,upload,profile,backups}  (originais + dumps do banco; thumbs e vídeos convertidos o Immich refaz)
#    - paperless/atual                                 (o export do passo 1)
#    Apagado/alterado vai para <app>/_removidos/<data> no Drive e é apagado de vez depois de RETENCAO_DIAS.
# Antes de subir fotos confere o espaço livre no Drive (os 400 GB são divididos com Gmail e Google Fotos).
# Falha ou pouco espaço → aviso pelas notificações do Proxmox (ntfy, tópico homelab).
set -uo pipefail

FOTOS=/mnt/pve/media/data/photos
PASTAS_IMMICH=(library upload profile backups)
PAPERLESS_LOCAL=/mnt/pve/media/data/backup/paperless
PVE3=192.168.10.4
PAPERLESS_EXPORT_PVE3=/zfs-ha/subvol-103-disk-0/opt/docker/paperless/export
REMOTE=gdrive:homelab-backup
RETENCAO_DIAS=30
MARGEM_GB=10          # deixa pelo menos isso livre no Drive
AVISO_LIVRE_GB=40     # avisa quando o Drive ficar com menos que isso

hoje=$(date +%F)
erros=()

avisar() {  # avisar <notice|warning|error> <título> <mensagem>
  perl -MPVE::Notify -e 'PVE::Notify::notify($ARGV[0], "homelab-backup",
    {titulo => $ARGV[1], mensagem => $ARGV[2]}, {type => "homelab-backup", hostname => "pve"})' "$@" || true
}

KH=$(mktemp); trap 'rm -f "$KH"' EXIT
{ printf '%s ' "$PVE3"; cut -d' ' -f2- /etc/pve/nodes/pve3/ssh_known_hosts; echo; } > "$KH"
SSH=(ssh -o BatchMode=yes -o HostKeyAlgorithms=rsa-sha2-512,rsa-sha2-256 -o UserKnownHostsFile="$KH")

RCLONE=(rclone --config /root/.config/rclone/rclone.conf --fast-list --transfers 4 --checkers 8
        --drive-stop-on-upload-limit --drive-use-trash=false --bwlimit "07:00,4M 23:00,off"
        --stats 10m --stats-one-line --log-level NOTICE)

mountpoint -q /mnt/pve/media || { avisar error "Backup: HD de mídia não montado" "/mnt/pve/media não está montado no pve; nada foi copiado."; exit 1; }

# --- 1. Paperless → HD de 24 TB -------------------------------------------------------------
echo "== Paperless: exportando no CT 103"
if "${SSH[@]}" root@$PVE3 pct exec 103 -- docker exec paperless \
     document_exporter /usr/src/paperless/export --delete --no-progress-bar; then
  mkdir -p "$PAPERLESS_LOCAL/atual" "$PAPERLESS_LOCAL/_removidos"
  if rsync -a --delete --backup --backup-dir="$PAPERLESS_LOCAL/_removidos/$hoje" \
       -e "${SSH[*]}" root@$PVE3:$PAPERLESS_EXPORT_PVE3/ "$PAPERLESS_LOCAL/atual/"; then
    find "$PAPERLESS_LOCAL/_removidos" -mindepth 1 -maxdepth 1 -type d -mtime +"$RETENCAO_DIAS" -exec rm -rf {} +
    date '+%F %T' > "$PAPERLESS_LOCAL/ULTIMA_COPIA_OK"
    echo "ok: $(du -sh "$PAPERLESS_LOCAL/atual" | cut -f1) em $PAPERLESS_LOCAL/atual"
  else
    erros+=("cópia do export do Paperless para o HD de 24 TB")
  fi
else
  erros+=("document_exporter do Paperless (CT 103)")
fi

# --- 2. Google Drive ------------------------------------------------------------------------
subir() {  # subir <origem local> <destino> <onde guardar o que sair do destino>  (os dois relativos a $REMOTE)
  "${RCLONE[@]}" sync "$1/" "$REMOTE/$2" --backup-dir "$REMOTE/$3"
}

livre_gb=$(rclone --config /root/.config/rclone/rclone.conf about gdrive: --json 2>/dev/null \
           | python3 -c 'import json,sys; print(int(json.load(sys.stdin).get("free", 0)) // 2**30)' 2>/dev/null)
if [ -z "$livre_gb" ]; then
  erros+=("acesso ao Google Drive (rclone about falhou — token expirado? veja o README)")
else
  echo "== Google Drive: ${livre_gb} GB livres"

  # Paperless (pequeno) sobe sempre que houver o export local
  if [ -f "$PAPERLESS_LOCAL/ULTIMA_COPIA_OK" ]; then
    subir "$PAPERLESS_LOCAL/atual" paperless/atual "paperless/_removidos/$hoje" || erros+=("envio do Paperless para o Drive")
  fi

  # Immich: só se o que falta subir couber com folga
  local_kb=$(du -sk "${PASTAS_IMMICH[@]/#/$FOTOS/}" | awk '{s+=$1} END {print s}')
  remoto_b=$(rclone --config /root/.config/rclone/rclone.conf size "$REMOTE/immich/atual" --json 2>/dev/null \
             | python3 -c 'import json,sys; print(json.load(sys.stdin)["bytes"])' 2>/dev/null || echo 0)
  falta_gb=$(( (local_kb * 1024 - remoto_b) / 2**30 ))
  [ "$falta_gb" -lt 0 ] && falta_gb=0
  echo "== Immich: ~${falta_gb} GB a mais que a cópia no Drive"
  if [ $(( livre_gb - falta_gb )) -lt "$MARGEM_GB" ]; then
    erros+=("fotos do Immich NÃO enviadas: faltam ~${falta_gb} GB e o Drive só tem ${livre_gb} GB livres")
  else
    for p in "${PASTAS_IMMICH[@]}"; do
      subir "$FOTOS/$p" "immich/atual/$p" "immich/_removidos/$hoje/$p" || erros+=("envio de immich/$p para o Drive")
    done
  fi

  # Limpa _removidos antigos pela data no nome da pasta (o arquivo movido mantém a data original,
  # então --min-age apagaria foto antiga na hora). Apaga de vez, sem passar pela lixeira do Drive.
  limite=$(date -d "-$RETENCAO_DIAS days" +%F)
  for app in immich paperless; do
    for d in $("${RCLONE[@]}" lsf --dirs-only "$REMOTE/$app/_removidos" 2>/dev/null); do
      d=${d%/}
      [[ $d =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ && $d < $limite ]] && "${RCLONE[@]}" purge "$REMOTE/$app/_removidos/$d"
    done
  done
  date '+%F %T' | "${RCLONE[@]}" rcat "$REMOTE/ULTIMA_COPIA_OK" || true

  livre_gb=$(rclone --config /root/.config/rclone/rclone.conf about gdrive: --json 2>/dev/null \
             | python3 -c 'import json,sys; print(int(json.load(sys.stdin).get("free", 0)) // 2**30)' 2>/dev/null || echo 0)
  if [ "${#erros[@]}" -eq 0 ] && [ "$livre_gb" -lt "$AVISO_LIVRE_GB" ]; then
    avisar warning "Google Drive quase cheio" "Backup ok, mas o Drive está com só ${livre_gb} GB livres. Apague fotos já importadas do Google Fotos ou aumente o plano."
  fi
fi

if [ "${#erros[@]}" -gt 0 ]; then
  msg=$(printf -- '- %s\n' "${erros[@]}")
  echo "ERROS:"; echo "$msg"
  avisar error "Backup fotos/documentos com erro" "$msg
Detalhes: journalctl -u backup-gdrive no pve"
  exit 1
fi
echo "backup completo"
