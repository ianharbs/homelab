# homelab
Configurações Docker Compose do homelab rodando em Proxmox VE (cluster `iHomeCluster`), distribuídos em 3 nós, com HA (ZFS replicado) para os LXCs de gateway e apps.


## Cluster
```
Proxmox VE 9.1.x · https://pve.ihome.dev.br/ · cluster iHomeCluster (3 nós)
│
├── pve  · 192.168.10.1 · Intel i5-14600K · 32 GB · iGPU Intel
├── pve2 · 192.168.10.2 · Xeon E5-2680 v4 · 32 GB · Intel Arc A310
└── pve3 · 192.168.10.3 · sem GPU
```

- **GPU:** transcodificação por hardware via `/dev/dri` (iGPU do `pve` e Arc A310 do `pve2`) nos serviços que precisam (`jellyfin`, `dispatcharr_web`, `frigate`).
- Os LXCs usam IPs fixos em `192.168.10.x` e podem rodar em nós diferentes; quem usa GPU ou disco local fica no nó que o possui.


## Alta disponibilidade e replicação
- Storage **ZFS** `zfs-ha` (mesmo nome em todos os nós) guarda o disco raiz de `lxc101` e `lxc103`.
- **Replicação** (pvesr) dos dois LXCs entre os nós; os dados dos containers ficam em `/opt/docker` dentro do próprio disco do LXC.
- **Proxmox HA** com os recursos `ct:101` e `ct:103` e regras de afinidade de nó para definir o nó preferido de cada um.
- Migração de LXC é por **reinício** (não é live). Cada nó precisa ter as entradas `root:<id>:1` em `/etc/subuid` e `/etc/subgid` para os ids usados nos `lxc.idmap` (uid/gid 1000 e gid do `render`), e os snippets/hookscripts devem existir em todos os nós.


## Guests e stacks
```
├── lxc101 · srv-gateway · 192.168.10.10
│    ├── gateway-db         → npm-db
│    ├── gateway-dns        → adguard, adguard-sync
│    ├── gateway-network    → wg-easy, netalertx
│    ├── gateway-proxy      → cloudflared, npm
│    └── stack-agents       → cadvisor, docker-socket-proxy, promtail
│
├── lxc102 · srv-media · 192.168.10.20
│    ├── media-download     → qbittorrent, sabnzbd, prowlarr, cleanuparr, flaresolverr, torrent-indexer (+ redis)   [gluetun: a instalar]
│    ├── media-editing      → bazarr, lingarr, profilarr, seerr
│    ├── media-music        → navidrome, octo-fiesta, audiomuse-ai (flask, worker, redis), droppedneedle, slskd, musicbrainz-picard
│    ├── media-others       → stash, whisparr
│    ├── media-reading      → audiobookshelf, kavita, suwayomi, shelfarr, shelfarr-libation
│    ├── media-tv           → dispatcharr_web, dispatcharr_celery
│    ├── media-video        → jellyfin, radarr, sonarr
│    └── stack-agents       → cadvisor, docker-socket-proxy, promtail
│
├── lxc103 · srv-apps · 192.168.10.30
│    ├── apps-automation    → n8n, n8n-runner
│    ├── apps-finances      → actual_budget
│    ├── apps-habits        → wingfit, wger (web, nginx, celery-worker, celery-beat)
│    ├── apps-housing       → mealie, bar-assistant, salt-rim
│    ├── apps-travel        → adventurelog-backend, adventurelog-frontend, adventurelog-db
│    ├── apps-utilities     → bentopdf, marreta, sftpgo
│    ├── stack-agents       → cadvisor, docker-socket-proxy, promtail, wud
│    ├── stack-meilisearch  → meilisearch
│    ├── stack-postgres     → postgres, pgadmin
│    ├── stack-redis        → redis
│    └── tools-hub          → tools-nginx, tools-proxy
│
├── lxc104 · srv-security · 192.168.10.40
│    ├── security-nvr       → frigate
│    ├── security-pwd       → vaultwarden
│    └── stack-agents       → cadvisor, docker-socket-proxy, promtail
│
├── lxc105 · srv-monitor · 192.168.10.50
│    ├── adguard-secundary  → adguard-secondary
│    ├── monitor-home       → homepage
│    ├── monitor-media      → jellystat
│    ├── monitor-server     → grafana, loki, prometheus, blackbox, pve-exporter, scrutiny (+ influxdb)
│    └── stack-agents       → cadvisor, docker-socket-proxy, promtail, speedtest-exporter
│
├── vm106 · Home Assistant OS · 192.168.10.60
│    └── Home Assistant
│
├── lxc107 · srv-games · 192.168.10.70
│    ├── games-library      → gameyfin
│    └── stack-agents       → cadvisor, docker-socket-proxy, promtail
│
└── lxc108 · srv-ai · 192.168.10.80
     ├── ai-llm             → ollama, open-webui
     └── stack-agents       → cadvisor, docker-socket-proxy, promtail
```


## Setup inicial
```bash
cp .env.example .env
# Preencha o .env com os valores reais (nunca commitar o .env)
# Gere secrets com: openssl rand -hex 32
```

### Deploy por LXC (padrão)
Em cada LXC, clone o repositório, crie o `.env` e rode o script com a pasta daquele LXC:
```bash
git clone <URL-DO-REPO> /root/homelab
nano /root/homelab/.env
chmod +x /root/homelab/deploy.sh
/root/homelab/deploy.sh lxc103-apps      # ou lxc101-gateway, lxc102-media, ...
```
O script cria as redes externas que faltarem, sobe as stacks na ordem (bancos primeiro, `stack-agents` por último) com `docker compose up -d`.

Para atualizar depois de uma mudança: `git pull` e `./deploy.sh <pasta>` de novo. O `up -d` só recria o que mudou.

> O deploy é feito direto por `docker compose`; as stacks deste repositório são a fonte da verdade.


## Variáveis de ambiente
Todas as stacks consomem o mesmo `.env`. Veja `.env.example` para a lista completa de variáveis.


## Backups
Backups automáticos via **Proxmox VE** rodam diariamente às 00:00, cobrindo todos os LXCs e VMs com retenção de 2 cópias. 
Excludes configurados: `var/lib/containerd`, `var/lib/docker/overlay2`, `var/lib/docker/volumes`, `data/compose`.

> `lxc101` e `lxc103` também têm replicação ZFS entre nós (ver HA acima), que **não** substitui o backup.

> Os dados reais dos containers ficam em `/opt/docker` via bind mount e são incluídos no backup normalmente.


## Notas importantes
- **`stack-agents`** usa o mesmo template em todos os LXCs: `cadvisor`, `docker-socket-proxy` (expõe a API Docker de forma restrita na porta 2375) e `promtail`. Ajustes por LXC:
  - **lxc103:** descomentar o `wud` (What's Up Docker), que atualiza os containers dos demais LXCs via `docker-socket-proxy`
  - **lxc105:** descomentar a linha `name: monitor_net` e o `speedtest-exporter`.
- **`apps-travel`** usa banco próprio (`adventurelog-db` com postgis) — não depende do PostgreSQL centralizado.
- **Hardware transcoding** (iGPU Intel i5-14600K) habilitado em: `jellyfin`, `dispatcharr_web`, `frigate`.
- **SFTPGo** acessa `/opt/docker` de todos os LXCs via SFTP com virtual folders. Permissões gerenciadas via ACL (`setfacl`) em cada LXC.
- **Secrets** são únicos por serviço — não reutilizar entre serviços distintos. Gere com `openssl rand -hex 32`.
- **Acesso externo:** os serviços passam pelo Nginx Proxy Manager (lxc101) + Cloudflare Tunnel. A lista de domínios e destinos está em `.ignore/Proxies.txt`.


## Operação e recuperação
- **Desligar com cuidado:** desligamentos sujos do Docker já causaram `docker0 ... networks have same bridge name` (entrada duplicada em `/var/lib/docker/network/files/local-kv.db`). Antes de desligar um nó, pare os guests. No Proxmox, defina *shutdown timeout* de 120 s nos LXCs `101` e `103` e `TimeoutStopSec=120` no serviço do Docker.
- **Correção do `docker0`** (console do LXC): parar o Docker, apagar `local-kv.db`, subir o Docker, recriar as redes externas (`gateway_net` 172.20.0.0/24 gw 172.20.0.1 no lxc101; `apps_net`, `db_net`, `monitoring_net` no lxc103) e rodar o `deploy.sh` com `--force-recreate`. Se containers na mesma sub-rede passarem a dar `ETIMEDOUT`, reiniciar o Docker (e, se preciso, o LXC) para limpar pontes antigas.
- **Variáveis novas:** o `.env` não vem pelo Git; ao adicionar uma variável, atualize o `.env` em cada LXC afetado antes do `deploy.sh`.
