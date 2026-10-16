# Cópia das fotos do Immich (host pve)

Cópia diária de `/mnt/pve/media/data/photos` (HD de mídia, 24 TB) para
`/mnt/pve/scratch_disk/immich-backup/atual` (SSD/scratch de 440 GB), porque a pasta de fotos é bind mount
do CT 102 e não entra no vzdump.

- Copia `library`, `upload`, `profile` e `backups` (dumps diários do banco do Immich, 02:00).
  `thumbs` e `encoded-video` ficam de fora (o Immich regenera).
- Apagados/alterados no Immich vão para `_removidos/<data>` e ficam 30 dias.
- Não copia com menos de 20 GB livres no scratch disk (o serviço falha; veja `systemctl status immich-photos-backup`).
- Última cópia bem-sucedida: `/mnt/pve/scratch_disk/immich-backup/ULTIMA_COPIA_OK`.

Instalação no pve:

    install -m 755 immich-photos-backup.sh /usr/local/sbin/
    install -m 644 immich-photos-backup.service immich-photos-backup.timer /etc/systemd/system/
    systemctl daemon-reload && systemctl enable --now immich-photos-backup.timer

Restaurar: parar o stack `media-photos`, `rsync -a /mnt/pve/scratch_disk/immich-backup/atual/ /mnt/pve/media/data/photos/`,
e se o banco também se perdeu, restaurar o dump mais recente de `backups/` pela tela de restauração do Immich.
