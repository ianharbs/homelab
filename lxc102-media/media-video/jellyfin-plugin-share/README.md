# Compartilhar — plugin do Jellyfin

Link público (sem conta) para **série** (todas as temporadas), **temporada**, **episódio** ou **filme**.
Quem recebe assiste no navegador (HLS, legenda, troca de áudio/qualidade, continua de onde parou) e pode baixar
(arquivo único ou `.zip` da temporada/série, sem compressão, com as legendas externas).

## Uso
- Botão **Compartilhar** (ícone share) na página do item e opção **Compartilhar** no menu ⋮ / clique direito
  dos cards. Escolhe a validade em dias ou "Sem validade".
- Painel › Plugins › **Compartilhar**: URL pública (`https://video.ihome.dev.br`), validade sugerida, só-admins,
  limite de reproduções por link, lista de links com **Revogar**/Copiar e "Remover expirados".
- Link: `https://video.ihome.dev.br/Share/v/<token>` (também funciona em jf.ihome.dev.br).
- Automação: `POST /Share/Links {"itemId": "...", "days": 7}` aceita API key (age como o 1º admin).

## Domínio video.ihome.dev.br
Desde 16/11 servido pelo CT 109 (srv-public, túnel Cloudflare próprio "ihome_viewer" → Caddy → Jellyfin),
ver `lxc109-public/README.md`. O antigo NPM host 91 (whitelist só de /Share) foi apagado.

## Build / deploy (rodar no host pve)
```
./build.sh          # compila no CT 102 (docker mcr.microsoft.com/dotnet/sdk:10.0) contra as DLLs do Jellyfin rodando
./build.sh deploy   # + copia para /config/data/plugins/Compartilhar_<versão>, gera meta.json e reinicia o Jellyfin
```
Quando o Jellyfin mudar de versão principal (ABI), basta rodar `./build.sh deploy` de novo.

## Como funciona
- O `client.js` é injetado no `index.html` pelo plugin **File Transformation** (dependência; sem ele não há botão,
  mas os links continuam funcionando).
- Links ficam em `/config/data/plugins/configurations/Jellyfin.Plugin.Share/links.json`.
- O streaming é o HLS do próprio Jellyfin (h264/aac, copia o vídeo quando dá, QSV quando precisa transcodificar),
  pedido pelo plugin a `127.0.0.1` com uma API key interna ("Compartilhar (plugin)") que nunca vai para o cliente.

## Segurança
- Token de 192 bits, comparação em tempo constante; expirado/revogado → 410 imediatamente (vale também para
  segmentos já em reprodução).
- Endpoints anônimos só servem os vídeos cobertos pelo link (checa série/temporada do episódio); caminho HLS
  validado por regex, parâmetros repassados por whitelist, `MediaSourceId`/`DeviceId` forçados pelo servidor.
- Playlists filtradas (removida a linha de trickplay que trazia `ApiKey=`).
- Máximo de reproduções simultâneas por link (padrão 3) contra abuso de transcodificação.
- Página com CSP restrita, `noindex`, `no-referrer`, `X-Frame-Options: DENY`; hls.js 1.5.17 embutido (sem CDN).
- Nenhuma porta nova: passa pelo mesmo NPM/host do Jellyfin.
