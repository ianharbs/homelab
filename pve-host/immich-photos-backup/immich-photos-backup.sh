#!/bin/bash
# Cópia de segurança das fotos do Immich (HD de mídia) para o scratch disk do pve.
# Roda no host pve via immich-photos-backup.timer (02:30, depois do dump do banco do Immich às 02:00).
# - Copia só o que não se recria: originais (library, upload), fotos de perfil e os dumps do banco (backups).
#   thumbs/ e encoded-video/ o Immich regenera.
# - Arquivo apagado/alterado no Immich não some da cópia na hora: vai para _removidos/<data> e fica RETENCAO_DIAS.
# - Não roda com menos de MIN_LIVRE_GB livres no destino (sai com erro, aparece no journal/status do timer).
set -euo pipefail

ORIGEM=/mnt/pve/media/data/photos
DESTINO=/mnt/pve/scratch_disk/immich-backup
RETENCAO_DIAS=30
MIN_LIVRE_GB=20
PASTAS=(library upload profile backups)

mountpoint -q /mnt/pve/media || { echo "ERRO: /mnt/pve/media não está montado"; exit 1; }
mountpoint -q /mnt/pve/scratch_disk || { echo "ERRO: /mnt/pve/scratch_disk não está montado"; exit 1; }
[ -d "$ORIGEM/library" ] || { echo "ERRO: $ORIGEM não parece a pasta do Immich"; exit 1; }

livre_gb=$(( $(df --output=avail -k /mnt/pve/scratch_disk | tail -1) / 1024 / 1024 ))
precisa_gb=$(( $(du -sk "${PASTAS[@]/#/$ORIGEM/}" | awk '{s+=$1} END {print s}') / 1024 / 1024 ))
echo "origem: ${precisa_gb} GB; livre no scratch disk: ${livre_gb} GB"
if [ "$livre_gb" -lt "$MIN_LIVRE_GB" ]; then
  echo "ERRO: menos de ${MIN_LIVRE_GB} GB livres no scratch disk; cópia não feita"; exit 1
fi

hoje=$(date +%F)
mkdir -p "$DESTINO/atual" "$DESTINO/_removidos"
for p in "${PASTAS[@]}"; do
  rsync -a --delete --backup --backup-dir="$DESTINO/_removidos/$hoje/$p" \
    "$ORIGEM/$p/" "$DESTINO/atual/$p/"
done

# Limpa removidos antigos
find "$DESTINO/_removidos" -mindepth 1 -maxdepth 1 -type d -mtime +"$RETENCAO_DIAS" -exec rm -rf {} +
date '+%F %T' > "$DESTINO/ULTIMA_COPIA_OK"
echo "ok: $(du -sh "$DESTINO/atual" | cut -f1) em $DESTINO/atual"
