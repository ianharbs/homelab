#!/bin/bash
# Monta no host pve as pastas de config (sshfs) que o Samba do CT 102 expõe em
# \\192.168.10.20\ihome\configs. O CT 102 recebe /mnt/pve/configs via mp3 (bind, propagação shared).
# FUSE fica no HOST de propósito: sshfs dentro de CT com backup snapshot congela (incidente 29/10).
# Requisitos: sshfs; chave /root/.ssh/samba_configs autorizada no ianharbs dos CTs;
# id_rsa do pve autorizado no add-on Advanced SSH do HA. Idempotente.
set -euo pipefail
U=/etc/systemd/system
mkdir -p /etc/sshfs-samba /mnt/pve/configs
# HA conecta como root: mapeia root remoto -> ianharbs (1000) p/ o Samba poder gravar
echo "ianharbs:0" > /etc/sshfs-samba/ha-uidmap
echo "ianharbs:0" > /etc/sshfs-samba/ha-gidmap

cat > $U/mnt-pve-configs.mount <<'EOF'
[Unit]
Description=Pasta das configs (bind shared) p/ o Samba do CT 102
Before=pve-guests.service

[Mount]
What=/mnt/pve/configs
Where=/mnt/pve/configs
Type=none
Options=bind,shared

[Install]
WantedBy=local-fs.target
EOF

COMMON="UserKnownHostsFile=/root/.ssh/known_hosts_samba,BatchMode=yes,allow_other,reconnect,ServerAliveInterval=15,ServerAliveCountMax=3,ConnectTimeout=10,_netdev"
mk() { # nome origem opções
  mkdir -p /mnt/pve/configs/$1
  cat > $U/mnt-pve-configs-$1.mount <<EOF
[Unit]
Description=sshfs $2 (Samba CT 102 ihome/configs/$1)
Requires=mnt-pve-configs.mount
After=mnt-pve-configs.mount network-online.target
Wants=network-online.target

[Mount]
What=$2
Where=/mnt/pve/configs/$1
Type=fuse.sshfs
Options=$3,$COMMON
TimeoutSec=30

[Install]
WantedBy=remote-fs.target
EOF
}
K=IdentityFile=/root/.ssh/samba_configs
mk lxc101 ianharbs@192.168.10.10:/opt/docker $K
mk lxc102 ianharbs@192.168.10.20:/opt/docker $K
mk lxc103 ianharbs@192.168.10.30:/opt/docker $K
mk lxc104 ianharbs@192.168.10.40:/opt/docker $K
mk lxc105 ianharbs@192.168.10.50:/opt/docker $K
mk lxc107 ianharbs@192.168.10.70:/opt/docker $K
mk vm106 root@192.168.10.60:/homeassistant IdentityFile=/root/.ssh/id_rsa,idmap=file,uidfile=/etc/sshfs-samba/ha-uidmap,gidfile=/etc/sshfs-samba/ha-gidmap,nomap=ignore

systemctl daemon-reload
systemctl enable mnt-pve-configs.mount mnt-pve-configs-{lxc101,lxc102,lxc103,lxc104,lxc105,lxc107,vm106}.mount
# já montado à mão? systemd adota; senão monta agora
systemctl start mnt-pve-configs.mount mnt-pve-configs-{lxc101,lxc102,lxc103,lxc104,lxc105,lxc107,vm106}.mount || true
findmnt -R /mnt/pve/configs -o TARGET,SOURCE,PROPAGATION
