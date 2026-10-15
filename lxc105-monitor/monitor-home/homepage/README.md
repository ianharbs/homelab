# Homepage (srv-monitor, CT 105)

Config em `/opt/docker/homepage/config` no CT 105 (pve3). Publicado em https://home.ihome.dev.br.

- `services.template.yaml` é o `services.yaml` real **sem segredos**: chaves e senhas aparecem como
  `__NOME__`. O arquivo de verdade só existe no servidor.
- `widgets.yaml`: `__LATITUDE__`/`__LONGITUDE__` vêm do Home Assistant (`/api/config`).
- `proxmox.yaml` (token do Proxmox) não é versionado.
- Links (`href`) usam o endereço externo `*.ihome.dev.br`; os widgets usam o IP interno.
  Serviços sem proxy no NPM (Komodo, Prometheus, slskd, Omada, Frigate, AdGuard secundário) ficam com link LAN.
- Jellyfin 12 removeu as rotas `/emby/...` que o widget nativo do Homepage 1.13 usa, por isso o card
  dele é um `customapi` em `/Items/Counts`.
- O Homepage relê os arquivos sozinho; para trocar `services.yaml`, grave num `.new` e faça `mv`
  (se o arquivo some por um instante, o container tenta recriar o padrão e reinicia).
