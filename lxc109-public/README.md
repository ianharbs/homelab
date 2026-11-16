# CT 109 — srv-public (entrada pública isolada)

`video.ihome.dev.br` para quem **não** é da casa: Jellyfin dos convidados, links do plugin Compartilhar,
pedido de cadastro com aprovação e o convite do Wizarr. Não passa pelo NPM nem pelo túnel principal.

```
Cloudflare ── túnel próprio "ihome_viewer" (d570db09-…) ── cloudflared ─┐  CT 109 (192.168.10.90, pve)
                                                          ├─ caddy :80 ─┬─ /cadastro*      → cadastro:8080
                                                          │             ├─ /j/* /join /wizard/* /static/* … → wizarr:5690
                                                          │             └─ resto            → Jellyfin 192.168.10.20:8096
                                                          └─ wizarr :5690 (admin só pela LAN)
```

## Isolamento
- **Firewall do Proxmox** (`/etc/pve/firewall/109.fw`, ligado em 16/11 com `cluster.fw` em política ACCEPT
  para não afetar mais nada): saída só para `192.168.10.20:8096` e internet (bloqueia o ipset `privadas`);
  entrada só `5690/tcp` e ping vindos da LAN; anti-spoofing de IP/MAC.
- **Banda**: `net0 rate=60` (60 MB/s ≈ 480 Mbps de 803 de upload) — sobra banda para a casa.
- Wizarr admin/settings/API **não** são encaminhados pelo Caddy (lista de permitidos tirada do url_map
  da v2026.10.1; ao atualizar o Wizarr, conferir se surgiram rotas públicas novas).
- Jellyfin: `KnownProxies` inclui 192.168.10.90 (vale após reiniciar o Jellyfin); o Caddy repassa o
  `CF-Connecting-IP` como X-Forwarded-For.

## Cadastro (app próprio, `public/cadastro/app.py`, só stdlib)
1. Visitante preenche `video.ihome.dev.br/cadastro` (nome, e-mail opcional, recado; honeypot + 3 pedidos/IP/dia)
   e recebe uma página de status privada.
2. DM do @iHomeSCbot para o Ian (chat 1199930908) com **Aprovar/Rejeitar** = links assinados (HMAC) que abrem
   uma confirmação (POST). Não usa getUpdates porque o statusbot (CT 102) já faz polling desse bot.
3. Aprovado → convite de uso único no Wizarr (7 dias, downloads) e o botão aparece na página de status.
4. Conta criada no Wizarr → o app aplica no Jellyfin **só nessa conta**: Filmes+Séries+Coleções, download,
   2 telas, sem Live TV/exclusão/compartilhamento, oculta no login. Avisa no Telegram.
   O Wizarr importa todos os usuários do Jellyfin; o app guarda um *baseline* (Anapgo, ianharbs, jeffejh,
   robsonjfelicio) e só age em contas novas casadas com o `used_at` do convite.
5. **Guardião de prioridade** (a cada 15 s): se ianharbs/Anapgo estão assistindo, convidados ficam com no
   máximo 2 transcodificações simultâneas; as mais novas recebem aviso e são paradas.

## Segredos (fora do git)
- `/opt/docker/cloudflared/tunnel.env` → `TUNNEL_TOKEN`
- `/opt/docker/cadastro/secrets.env` → APP_SECRET, TG_TOKEN, TG_ADMIN_CHAT, JELLYFIN_API_KEY
  (key "Cadastro (CT 109)"), WIZARR_API_KEY, WIZARR_SERVER_ID
- Wizarr: admin `ianharbs`, senha em `/root/wizarr-admin-senha.txt` (CT 109); Jellyfin key "Wizarr (CT 109)".

## Túnel
Rota publicada (Cloudflare One › Networking › Tunnels › ihome_viewer): `video.ihome.dev.br` → `http://caddy:80`
(criada 16/11; CNAME `video` → `d570db09-….cfargotunnel.com`, tem prioridade sobre o curinga do túnel principal).

## Operação
- Stack `public` no Komodo (servidor `srv-public`, Periphery systemd com saída liberada só para o Core 192.168.10.50:9120).
  Mudou algo em `public/`? commit + push → o Komodo faz o deploy ("Deploy on git change"); o cadastro é buildado no deploy (`pull_policy: build`).
- Admin do Wizarr: http://192.168.10.90:5690 (só LAN).
- Fechar cadastros: `ACCEPT_REQUESTS=false` no environment do serviço `cadastro`.
