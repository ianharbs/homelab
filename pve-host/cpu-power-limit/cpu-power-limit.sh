#!/bin/bash
# Limita a potência do i5-14600K do pve enquanto o cooler for o PCYes Nótus ST17 (65 W de TDP).
# A BIOS deixa PL1/PL2 em 135/150 W, muito acima do que o cooler dissipa (CPU vivia a 97-100 °C).
# - PL1 (média longa, janela de ~56 s) = PL1_W; PL2 (picos curtos, ~2,4 ms) = PL2_W.
# - Governor powersave + EPP balance_performance: o turbo continua, mas os núcleos não ficam presos em clock alto.
# Valores somem no reboot; roda no boot via cpu-power-limit.service.
set -euo pipefail

PL1_W=55
PL2_W=90
EPP=balance_performance

ZONA=/sys/class/powercap/intel-rapl:0
[ "$(cat $ZONA/name)" = package-0 ] || { echo "ERRO: $ZONA não é package-0"; exit 1; }
echo $(( PL1_W * 1000000 )) > $ZONA/constraint_0_power_limit_uw
echo $(( PL2_W * 1000000 )) > $ZONA/constraint_1_power_limit_uw

for cpu in /sys/devices/system/cpu/cpu[0-9]*/cpufreq; do
  echo powersave > $cpu/scaling_governor
  echo $EPP > $cpu/energy_performance_preference
done

echo "PL1=$(( $(cat $ZONA/constraint_0_power_limit_uw) / 1000000 )) W" \
     "PL2=$(( $(cat $ZONA/constraint_1_power_limit_uw) / 1000000 )) W" \
     "governor=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)" \
     "epp=$(cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference)"
