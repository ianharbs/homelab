"""Hook PreToolUse do telegram-agent: o que muda alguma coisa pede ✅ no Telegram antes de rodar.

Só age quando TG_AGENT_JOB está no ambiente (sessões abertas pelo agent.py); nas sessões do
terminal não faz nada. A detecção é heurística, por padrões no comando: o que não bate passa
direto (consultas, logs, docker restart/start) e continua sob o classificador do modo auto.
Saída: exit 0 sem nada = segue o fluxo normal; JSON "allow" = o Ian autorizou; exit 2 = bloqueado.
"""
import json
import os
import re
import sys
import urllib.request

# (padrão, motivo) — aplicados ao comando inteiro, inclusive o que vai dentro de ssh/pct exec/docker exec
DANGER = [
    (r"\brm\s|\brmdir\b|\bshred\b|\bwipefs\b|\bmkfs|\bdd\s+if=|\btruncate\s", "apaga/sobrescreve"),
    (r"\bmv\s|\bcp\s|\bln\s|\binstall\s+-|\brsync\b.*--delete|\bfind\b.*\s-(delete|exec\s+rm)", "move/copia arquivos"),
    (r"\bsed\s+(-[a-zA-Z]*i|--in-place)|\btee\b|\bpatch\s", "edita arquivo"),
    (r">>?\s*['\"]?(?!/dev/null|/dev/std|&|/tmp/claude)(/|~|\$|\.{1,2}/|[\w.-]+\.\w{1,5}\b)", "grava em arquivo"),
    (r"\bchmod\b|\bchown\b|\bchattr\b|\buseradd\b|\buserdel\b|\bpasswd\b|\bcrontab\s", "permissões/usuários"),
    (r"\bgit\s+(\S+\s+)*?(commit|push|reset|checkout|switch|rebase|merge|stash|clean|rm|mv|tag|cherry-pick|revert|am|apply|restore)\b", "git que muda o repo"),
    (r"\bdocker\s+(rm|rmi|stop|kill|pause|update|prune|system\s+prune|volume\s+(rm|prune)|network\s+(rm|prune|disconnect|connect)|image\s+(rm|prune)|container\s+(rm|prune|stop|kill))\b", "docker"),
    (r"\bdocker[\s-]compose\b.*\b(up|down|rm|stop|kill|pull|create|restart)\b", "docker compose"),
    (r"\b(pct|qm)\s+(stop|shutdown|destroy|set|rollback|snapshot|delsnapshot|restore|create|clone|migrate|resize|move[_-]volume|unlock|push|reboot|suspend|start)\b", "Proxmox"),
    (r"\bha-manager\s+(set|remove|add|migrate|relocate)|\bpve(sh\s+(create|set|delete)|sm\s+(set|remove|add|free|alloc)|cm\s)", "Proxmox"),
    (r"\b(zfs|zpool)\s+(destroy|rollback|set|create|send|recv|receive|import|export|detach|offline|online|replace|clear|labelclear|attach|rename|promote|split|upgrade)\b", "ZFS"),
    (r"\b(lv|vg|pv)(remove|create|extend|reduce|rename|change)\b|\b(sgdisk|parted|fdisk|sfdisk)\b|\bu?mount\s", "discos"),
    (r"\bsystemctl\s+(\S+\s+)*?(stop|disable|mask|restart|reload|enable|start|kill|daemon-reload|isolate)\b", "serviço do sistema"),
    (r"\b(reboot|poweroff|shutdown|halt)\b|\b(kill|pkill|killall)\s", "desliga/mata processo"),
    (r"\b(iptables|ip6tables)(-restore)?\s+.*-(A|I|D|F|X|R|P|N|Z)\b|\bnft\s+(add|delete|flush|insert|replace)|\bip\s+(link|addr|route|rule)\s+(add|del|set|flush|change|replace)", "rede/firewall"),
    (r"\b(apt|apt-get|dpkg)\s+(\S+\s+)*?(install|remove|purge|upgrade|dist-upgrade|full-upgrade|autoremove|-i)\b|\bpip3?\s+install|\bnpm\s+(i|install|uninstall)\b", "pacotes"),
    (r"\bcurl\b.*(-X\s*(POST|PUT|DELETE|PATCH)|\s-d\s|--data|\s-F\s|--form|\s-T\s|--upload)", "requisição que altera"),
    (r"\b(DROP|DELETE\s+FROM|UPDATE\s+\w+\s+SET|INSERT\s+INTO|ALTER|TRUNCATE|CREATE\s+(TABLE|DATABASE|USER|INDEX))\b", "SQL que altera"),
    (r"\brclone\s+(delete|purge|move|sync|deletefile|rmdir|moveto|copy|copyto|config)\b", "rclone"),
    (r"open\([^)]*,\s*['\"][wax]|\.write_(text|bytes)\(|\.write\(|os\.(remove|unlink|rename|replace|rmdir|system)|shutil\.|method\s*=\s*['\"](POST|PUT|DELETE|PATCH)", "script que grava/altera"),
]
# a API de leitura do Komodo (/read) usa POST, mas só lê: esse trecho do comando sai da checagem
SAFE_CURL = r"curl\b[^|;&\n]*:9120/read\b[^|;&\n]*"

FREE_PATHS = ("/root/.claude/projects/", "/tmp/claude-")  # memória e scratchpad


def classify(tool, inp):
    if tool == "Bash":
        cmd = inp.get("command", "")
        check = re.sub(SAFE_CURL, "", cmd)
        for pat, why in DANGER:
            if re.search(pat, check):
                return why, cmd
        return None, cmd
    path = inp.get("file_path") or inp.get("notebook_path") or ""
    if path.startswith(FREE_PATHS):
        return None, path
    if tool == "Write":
        preview = inp.get("content", "")[:1500]
        return "cria/sobrescreve arquivo", f"{path}\n---\n{preview}"
    if tool in ("Edit", "MultiEdit"):
        edits = inp.get("edits") or [inp]
        diff = "\n…\n".join(f"- {e.get('old_string', '')[:600]}\n+ {e.get('new_string', '')[:600]}" for e in edits)
        return "edita arquivo", f"{path}\n---\n{diff}"
    return "edita arquivo", path


def main():
    job = os.environ.get("TG_AGENT_JOB")
    if not job:
        return 0
    ev = json.load(sys.stdin)
    tool, inp = ev.get("tool_name", ""), ev.get("tool_input") or {}
    why, summary = classify(tool, inp)
    if not why:
        return 0
    req = urllib.request.Request(
        os.environ.get("TG_AGENT_CONFIRM_URL", "http://127.0.0.1:8098/confirm"),
        data=json.dumps({"job": job, "tool": tool, "reason": why, "summary": summary,
                         "description": inp.get("description", "")}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            ok = json.load(r).get("ok")
    except Exception as e:
        print(f"Não consegui pedir confirmação ao Ian no Telegram ({e}); não rode isso.", file=sys.stderr)
        return 2
    if ok:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                                 "permissionDecisionReason": "Ian autorizou no Telegram"}}))
        return 0
    print("O Ian NÃO autorizou isso no Telegram (negou ou não respondeu). Não tente de outro jeito; "
          "diga o que ia fazer e pare.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
