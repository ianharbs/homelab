# lxc200 · srv-print3d · 192.168.10.200 (pve3)

CT do negócio de impressão 3D. Ubuntu 24.04, 4 vCPU, 6 GB RAM, 64 GB em `local-lvm`,
unprivileged + nesting, mesmo `lxc.idmap` dos outros CTs (uid/gid 1000 mapeado).
Docker + Komodo Periphery (`connect_as = "srv-print3d"`), repo em `/root/homelab`, dados em `/opt/docker`.
Backup diário 03:00 no job do pve3 (`backup-media`, keep-last=5).

Stacks:
- `print3d-filament`  → spoolman (:7912) — estoque de filamento, R$/g por carretel
- `print3d-models`    → manyfold (:3214) — biblioteca STL/3MF (`/opt/docker/manyfold/libraries`)
- `print3d-inventory` → inventree (:8090; server, worker, postgres 17, redis, caddy) — insumos, reposição, produtos com BOM.
  Segredos em `/opt/docker/inventree/inventree.env` (chmod 600, inclui a senha inicial do admin).
- `print3d-calc`      → nginx (:8095) — calculadora de preço/orçamento; `/spoolman/` é proxy para o Spoolman.
- `print3d-agents`   → print3d-agents (Python puro, sem porta) — agentes no Telegram (bot e grupo próprios do negócio):
  Curador (Printables, filtro de licença comercial + IA), Custo (malha STL/3MF ou 3MF fatiado/G-code), Anunciante
  (rascunho ML/Shopee), Estoque (Spoolman+InvenTree → Vikunja Compras), Pedidos (texto livre → Vikunja), Gerente
  (relatório semanal), Atendente/pedidos do ML (ligam com ML_CLIENT_ID/SECRET/REFRESH_TOKEN). Tudo que sai para fora
  é rascunho com botões. Estado e segredos em `/opt/docker/print3d-agents` (secrets.env 600, config.json editável
  ou via `/config`). IA: Gemini (flash-lite p/ tarefas, 3.5-flash p/ textos). Depois de mudar código: `docker restart print3d-agents`.
- planejado: bridge da Kobra X (farolprint/Kobralink) quando a impressora chegar; `stack-agents`.

Pedidos: Vikunja do CT 103 (tasks.ihome.dev.br), projeto "Impressão 3D" → Pedidos (kanban), Compras, Ideias de produtos.

Por enquanto fora do Komodo (onboarding do Periphery pendente): deploy com
`docker compose -p <stack> --env-file .env -f lxc200-print3d/<stack>/docker-compose.yml up -d`.
