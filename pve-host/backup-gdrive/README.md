# Backup de fotos e documentos (host pve)

Roda todo dia às 05:00 (`backup-gdrive.timer`), depois do vzdump (03:00) e das atualizações do Komodo (04:00).

| O quê | Principal | Cópia no HD de 24 TB | Google Drive (`homelab-backup/`) |
|---|---|---|---|
| Fotos (Immich) | `/mnt/pve/media/data/photos` (já é o HD de 24 TB) | — (cópia extra no SSD scratch: `immich-photos-backup`) | `immich/atual/{library,upload,profile,backups}` |
| Documentos (Paperless) | CT 103 `/opt/docker/paperless` (SSD do pve3, também no vzdump) | `/mnt/pve/media/data/backup/paperless/atual` (export) | `paperless/atual` |

- O Paperless é exportado com `document_exporter` (arquivos originais + `manifest.json`), o formato que o
  `document_importer` aceita para restaurar em uma instalação nova.
- Arquivo apagado ou alterado não some na hora: vai para `_removidos/<data>` (no HD e no Drive) e fica 30 dias.
- Os arquivos ficam **sem criptografia** no Drive (escolha do usuário em 28/10/2026), legíveis pelo site do Drive.
- O remote do rclone usa o escopo `drive.file`, então o rclone só enxerga o que ele mesmo criou, não o resto do Drive.
- Antes de subir fotos o script compara o que falta enviar com o espaço livre do Drive. Os 400 GB do Google One
  são divididos com Gmail e Google Fotos, e foto importada do Google Fotos conta duas vezes até ser apagada de lá.
  Se não couber, as fotos não sobem e chega um aviso.
- Avisos (erro e Drive com menos de 40 GB livres) chegam pelas notificações do Proxmox → ntfy (tópico `homelab`),
  com o template `/etc/pve/notification-templates/default/homelab-backup-*.txt.hbs`.
- Durante o dia (07:00–23:00) o envio é limitado a 4 MB/s para não ocupar a internet.

## Instalação no pve

    # rclone oficial (o do Debian é antigo), confira o SHA256 em downloads.rclone.org/<versão>/SHA256SUMS
    curl -fLO https://downloads.rclone.org/rclone-current-linux-amd64.deb && dpkg -i rclone-current-linux-amd64.deb

    # template do aviso
    mkdir -p /etc/pve/notification-templates/default
    echo '{{ titulo }}'   > /etc/pve/notification-templates/default/homelab-backup-subject.txt.hbs
    echo '{{ mensagem }}' > /etc/pve/notification-templates/default/homelab-backup-body.txt.hbs

    install -m 755 backup-gdrive.sh /usr/local/sbin/
    install -m 644 backup-gdrive.service backup-gdrive.timer /etc/systemd/system/
    systemctl daemon-reload && systemctl enable --now backup-gdrive.timer

### Conectar o Google Drive (uma vez, ou quando o token expirar)

Num computador com navegador e o rclone instalado:

    rclone authorize "drive" "eyJzY29wZSI6ImRyaXZlLmZpbGUifQ"

(o texto em base64 é `{"scope":"drive.file"}`). Faça login com a conta Google, copie o JSON do token que aparece e,
no pve:

    rclone config create gdrive drive scope=drive.file token='<JSON colado aqui>'
    rclone about gdrive:          # deve mostrar o espaço do Google One

Primeira cópia na mão: `systemctl start backup-gdrive` e acompanhe com `journalctl -fu backup-gdrive`.

## Restaurar

- **Fotos:** pare o stack `media-photos`, `rclone copy gdrive:homelab-backup/immich/atual /mnt/pve/media/data/photos`
  (ou use a cópia do SSD scratch, que é mais rápida) e, se o banco também se perdeu, restaure o dump mais recente de
  `backups/` pela tela de restauração do Immich.
- **Documentos:** com o Paperless novo rodando e vazio, copie `paperless/atual` (do HD ou do Drive) para a pasta
  `export` dele e rode `docker exec paperless document_importer /usr/src/paperless/export`.
