# Limite de potência da CPU (host pve)

O pve (i5-14600K, MSI PRO B760M-E) usa o cooler PCYes Nótus ST17, de 65 W de TDP. A BIOS libera
PL1/PL2 de 135/150 W, e a CPU ficava em 97-100 °C mesmo quase ociosa (~60 W no package).

- PL1 = 55 W, PL2 = 90 W via RAPL (era 45/65 W até 22/10/2026) (`/sys/class/powercap/intel-rapl:0`). O MSR 0x610 não vem travado pela BIOS.
  Medido em 2026-10-18 com carga total em 20 threads: PL1 65 W → 99-100 °C; PL1 45 W → 92 °C (~2,45 GHz).
  Ocioso: de 97 °C / 50 W para 73 °C / 20 W (efeito principal do EPP).
  Teste de 45 s em 22/10/2026: 65/90 W → 81 W, 94 °C, ~3,6 GHz; 90/120 W → 99 °C em segundos. Ficou 55/90 W (picos rápidos, sustentado abaixo dos 100 °C).
- Governor `powersave` + EPP `balance_performance` (o turbo continua funcionando).
- Na BIOS, ativar também *C-States*, *C1E* e *Package C-State Limit* = Auto/C10 (vinham limitados a C0/C1,
  o que mantém o consumo ocioso alto).
- Ao trocar por um cooler maior (torre ≥150 W), ajustar `PL1_W`/`PL2_W` no script ou desativar o serviço.

Instalação no pve:

    install -m 755 cpu-power-limit.sh /usr/local/sbin/
    install -m 644 cpu-power-limit.service /etc/systemd/system/
    systemctl daemon-reload && systemctl enable --now cpu-power-limit.service

Conferir: `systemctl status cpu-power-limit` e `turbostat --quiet --Summary --show PkgWatt,PkgTmp,Busy% --interval 5`.
Reverter na hora: `systemctl disable cpu-power-limit` e reiniciar (a BIOS volta a 135/150 W e governor `performance`).
