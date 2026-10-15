# Homepage (srv-monitor, CT 105)

Config em `/opt/docker/homepage/config` no CT 105 (pve3). Publicado em https://home.ihome.dev.br.

- O arquivo no servidor é a fonte da verdade (ele também é editado à mão); a cópia aqui é a mesma,
  porque a config não tem mais segredos. Antes de mexer, puxe a versão atual do servidor.
- Segredos: `/opt/docker/homepage/secrets.env` (fora do git, chmod 600), carregado pelo `env_file`
  do compose. Na config eles aparecem como `{{HOMEPAGE_VAR_NOME}}`. Para trocar uma senha, edite o
  `secrets.env` e recrie o container (deploy do stack `monitor-home` no Komodo).
- O grupo `Mídia — XXX` fica oculto: o `custom.js` marca o grupo pelo nome e o `custom.css` esconde;
  clicar no logo mostra/esconde.
- `widgets.yaml`: latitude/longitude também vêm do `secrets.env`.
- Links (`href`) usam o endereço externo `*.ihome.dev.br`; os widgets usam o IP interno.
  Serviços sem proxy no NPM (Prometheus, slskd, Frigate, AdGuard secundário) ficam com link LAN.
- Jellyfin 12 removeu as rotas `/emby/...` que o widget nativo do Homepage 1.13 usa, por isso o card
  dele é um `customapi` em `/Items/Counts`.
- O Homepage relê os arquivos sozinho; para trocar `services.yaml`, grave num `.new` e faça `mv`
  (se o arquivo some por um instante, o container tenta recriar o padrão e reinicia).
