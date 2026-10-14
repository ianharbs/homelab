# homelab
Configurações Docker Compose do homelab rodando em Proxmox VE (cluster `iHomeCluster`), distribuídos em 3 nós, com HA (ZFS replicado) para os LXCs de gateway e apps.


## Cluster
```
Proxmox VE 9.1.x · cluster iHomeCluster (3 nós)
│
├── pve  · 192.168.10.2 · Intel i5-14600K · 32 GB · iGPU Intel
├── pve2 · 192.168.10.3 · Xeon E5-2680 v4 · 32 GB · Intel Arc A310
└── pve3 · 192.168.10.4 · Xeon E5-2680 v4 · 32 GB · sem GPU
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
pve · 192.168.10.2 · Intel i5-14600K · 32 GB · iGPU Intel
  ├── lxc100 · srv-omada · 192.168.10.5
  │    ├── omada-controller   → omada-controller
  │    └── stack-agents       → cadvisor, docker-socket-proxy, promtail
  │
  ├── lxc101 · srv-gateway · 192.168.10.10
  │    ├── gateway-db         → npm-db
  │    ├── gateway-dns        → adguard, adguard-sync
  │    ├── gateway-network    → wg-easy, netalertx
  │    ├── gateway-proxy      → cloudflared, npm
  │    └── stack-agents       → cadvisor, docker-socket-proxy, promtail
  │
  └── lxc102 · srv-media · 192.168.10.20
       ├── media-download     → qbittorrent, sabnzbd, prowlarr, cleanuparr, flaresolverr, torrent-indexer (+ redis)   [gluetun: a instalar]
       ├── media-editing      → bazarr, lingarr, profilarr, seerr
       ├── media-music        → navidrome, octo-fiesta, audiomuse-ai (flask, worker, redis), droppedneedle, slskd
       ├── media-others       → stash, whisparr
       ├── media-reading      → audiobookshelf, kavita, suwayomi, shelfarr, shelfarr-libation
       ├── media-tv           → dispatcharr_web, dispatcharr_celery
       ├── media-video        → jellyfin, radarr, sonarr
       └── stack-agents       → cadvisor, docker-socket-proxy, promtail

pve2 · 192.168.10.3 · Xeon E5-2680 v4 · 32 GB · Intel Arc A310
  │
  ├── lxc104 · srv-security · 192.168.10.40
  │    ├── security-nvr       → frigate
  │    ├── security-pwd       → vaultwarden
  │    └── stack-agents       → cadvisor, docker-socket-proxy, promtail
  │
  ├── lxc107 · srv-games · 192.168.10.70
  │    ├── games-library      → gameyfin
  │    └── stack-agents       → cadvisor, docker-socket-proxy, promtail
  │
  └── lxc108 · srv-ai · 192.168.10.80
      ├── ai-llm             → ollama, open-webui
      └── stack-agents       → cadvisor, docker-socket-proxy, promtail

pve3 · 192.168.10.4 · Xeon E5-2680 v4 · 32 GB · sem GPU
  ├── lxc103 · srv-apps · 192.168.10.30
  │    ├── apps-automation    → n8n, n8n-runner
  │    ├── apps-finances      → actual_budget
  │    ├── apps-habits        → wingfit, wger (web, nginx, celery-worker, celery-beat)
  │    ├── apps-housing       → mealie, bar-assistant, salt-rim
  │    ├── apps-travel        → adventurelog-backend, adventurelog-frontend, adventurelog-db
  │    ├── apps-utilities     → bentopdf, marreta, sftpgo
  │    ├── stack-agents       → cadvisor, docker-socket-proxy, promtail
  │    ├── stack-meilisearch  → meilisearch
  │    ├── stack-postgres     → postgres, pgadmin
  │    └── stack-redis        → redis
  │
  ├── lxc105 · srv-monitor · 192.168.10.50
  │    ├── adguard-secundary  → adguard-secondary
  │    ├── komodo             → komodo-core, komodo-ferretdb, komodo-postgres
  │    ├── monitor-home       → homepage
  │    ├── monitor-media      → jellystat
  │    ├── monitor-server     → grafana, loki, prometheus, blackbox, pve-exporter, scrutiny (+ influxdb)
  │    └── stack-agents       → cadvisor, docker-socket-proxy, promtail, speedtest-exporter
  │
  └── vm106 · Home Assistant OS · 192.168.10.60
       └── Home Assistant
```


## Setup inicial
```bash
cp .env.example .env
# Preencha o .env com os valores reais (nunca commitar o .env)
# Gere secrets com: openssl rand -hex 32
```

### Deploy pelo Komodo (padrão)
O **Komodo** (`lxc105-monitor/komodo`, UI em `http://192.168.10.50:9120`) gerencia todas as stacks a partir deste repositório:
- Cada LXC roda o agente **Periphery** (systemd, `/etc/komodo/periphery.config.toml`) conectando de saída no Core; o servidor no Komodo tem o nome da hostname (`srv-apps`, ...).
- As stacks são declaradas em [`komodo/stacks.toml`](komodo/stacks.toml) e aplicadas por um **Resource Sync** apontando para a pasta `komodo/`. O `project_name` de cada stack é o nome da pasta, igual ao `deploy.sh`.
- **Deploy por git:** o procedimento `Deploy on git change` roda a cada 5 min e reimplanta só as stacks cujo compose mudou (`BatchDeployStackIfChanged`). Não precisa de webhook nem de expor o Komodo.
- **Segredos:** ficam no `[secrets]` de `${CONFIG_PATH}/komodo/core.config.toml` no srv-monitor (fora do git). As stacks referenciam `CHAVE=[[CHAVE]]` no `environment`. Variável nova → adicionar no `core.config.toml`, reiniciar o `komodo-core` e incluir a linha na stack em `komodo/stacks.toml`.
- **Atualização automática:** o procedimento `Global Auto Update` roda todo dia às **04:00** (após o backup das 00:00). Stacks com `auto_update = true` são recriadas quando sai uma imagem nova na mesma tag; as marcadas `critico` (bancos, NPM, Vaultwarden, Omada, monitor-server, o próprio Komodo) têm só `poll_for_updates` e aparecem como "update disponível" para aplicar manualmente.

### Deploy manual por LXC (fallback, sem Komodo)
Os comandos são iguais em todos os LXCs: o `deploy.sh` descobre a pasta pela hostname do LXC (`srv-apps` → `lxc103-apps`, `srv-gateway` → `lxc101-gateway`, etc.).
```bash
git clone https://github.com/ianharbs/homelab.git /root/homelab
nano /root/homelab/.env
chmod +x /root/homelab/deploy.sh
/root/homelab/deploy.sh
```
Se a hostname não seguir o padrão `srv-<nome>`, passe a pasta: `/root/homelab/deploy.sh lxc103-apps`.

O script cria as redes externas que faltarem (`gateway_net` com a sub-rede fixa), sobe as stacks na ordem (bancos primeiro, `stack-agents` por último) com `docker compose up -d` e avisa quais falharam. Use `RECREATE=1 ./deploy.sh` para forçar a recriação dos containers.

Para atualizar depois de uma mudança: `cd /root/homelab && git pull && ./deploy.sh`. O `up -d` só recria o que mudou.

> O deploy é feito direto por `docker compose`; as stacks deste repositório são a fonte da verdade.


## Variáveis de ambiente
Todas as stacks consomem o mesmo `.env`. Veja `.env.example` para a lista completa de variáveis.


## Backups
Backups automáticos via **Proxmox VE** (vzdump, `zstd`, modo snapshot) rodam diariamente às **03:00**, um job por nó, com retenção de 2 cópias:

| Nó | Guests | Storage de destino |
|---|---|---|
| `pve` | 101, 102 | `scratch_disk` |
| `pve2` | 104, 107, 108 | `frigate_store` (HD USB do pve2) |
| `pve3` | 103, 105, 106 | `media` → no pve3 é uma pasta no **disco do sistema** (o HD de mídia fica no pve) |

Excludes configurados (`/etc/vzdump.conf`): `var/lib/containerd`, `var/lib/docker/overlay2`, `var/lib/docker/volumes`, `data/compose`.

> `lxc101` e `lxc103` também têm replicação ZFS entre nós (ver HA acima), que **não** substitui o backup.

> Os dados reais dos containers ficam em `/opt/docker` via bind mount e são incluídos no backup normalmente.


## Notas importantes
- **`stack-agents`** usa o mesmo template em todos os LXCs: `cadvisor`, `docker-socket-proxy` (expõe a API Docker de forma restrita na porta 2375) e `promtail`. Ajustes por LXC:
  - **lxc105:** descomentar a linha `name: monitor_net` e o `speedtest-exporter`.
- **`apps-travel`** usa banco próprio (`adventurelog-db` com postgis) — não depende do PostgreSQL centralizado.
- **Hardware transcoding** (iGPU Intel i5-14600K/Intel Arc A310) habilitado em: `jellyfin`, `dispatcharr_web`, `frigate`.
- **SFTPGo** acessa `/opt/docker` de todos os LXCs via SFTP com virtual folders. Permissões gerenciadas via ACL (`setfacl`) em cada LXC.
- **Secrets** são únicos por serviço — não reutilizar entre serviços distintos. Gere com `openssl rand -hex 32`.
- **Acesso externo:** os serviços passam pelo Nginx Proxy Manager (lxc101) + Cloudflare Tunnel. A lista de domínios e destinos está em `.ignore/Proxies.txt`.


## Operação e recuperação
- **Desligar com cuidado:** desligamentos sujos do Docker já causaram `docker0 ... networks have same bridge name` (entrada duplicada em `/var/lib/docker/network/files/local-kv.db`). Antes de desligar um nó, pare os guests. No Proxmox, defina *shutdown timeout* de 120 s nos LXCs `101` e `103` e `TimeoutStopSec=120` no serviço do Docker.
- **Correção do `docker0`** (console do LXC): parar o Docker, apagar `local-kv.db`, subir o Docker, recriar as redes externas (`gateway_net` 172.20.0.0/24 gw 172.20.0.1 no lxc101; `apps_net`, `db_net`, `monitoring_net` no lxc103) e rodar o `deploy.sh` com `--force-recreate`. Se containers na mesma sub-rede passarem a dar `ETIMEDOUT`, reiniciar o Docker (e, se preciso, o LXC) para limpar pontes antigas.
- **Variáveis novas:** o `.env` não vem pelo Git; ao adicionar uma variável, atualize o `core.config.toml` do Komodo (e o `.env` dos LXCs afetados, se usar o `deploy.sh`).
- **Monitoramento versionado:** cópias de referência em `lxc105-monitor/monitor-server/` — `prometheus/prometheus.yml`, `blackbox/blackbox.yml` e os dashboards do Grafana em `grafana/dashboards/*.json`. A config ativa continua em `/opt/docker/prometheus/config` e `/opt/docker/blackbox` no srv-monitor: ao mudar uma, atualize a outra. Para restaurar: copiar o arquivo para `/opt/docker/...` e `curl -XPOST localhost:9090/-/reload` (ou `:9115/-/reload` no blackbox); dashboards via *Dashboards → New → Import* (o `uid` é preservado, então links entre eles continuam funcionando).
- **Scrutiny:** cada nó Proxmox roda o coletor (`/opt/scrutiny`, cron em `/etc/cron.d/scrutiny`, a cada 6 h, log em `/var/log/scrutiny-collector.log`). Os HDs USB Seagate precisam do UAS desativado (`/etc/modprobe.d/usb-storage-quirks.conf`) para o SMART funcionar.
