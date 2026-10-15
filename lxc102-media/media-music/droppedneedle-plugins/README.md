# Plugins do DroppedNeedle

Plugins usados no DroppedNeedle do CT 102 (srv-media). A cópia em produção fica em
`/opt/docker/droppedneedle/plugins/`, montada em `/app/plugins` no container.

| Plugin | Descrição |
|---|---|
| [`deezer/`](deezer/) | Busca e download direto do Deezer, guiado pela edição do MusicBrainz. Veja o [README do plugin](deezer/README.md), com o aviso de uso em pt-BR e inglês. |

## Instalar ou atualizar neste homelab

```sh
cp -r deezer /opt/docker/droppedneedle/plugins/ && chown -R 1000:1000 /opt/docker/droppedneedle/plugins/deezer
```

Depois, salve o plugin em **Configurações → Plugins** (o salvamento recarrega o código).

Neste ambiente:
- `musicbrainz_url` aponta para o mesmo espelho usado pelo DroppedNeedle;
- a prioridade das fontes é `plugin:deezer` → usenet → soulseek;
- o ARL fica só na configuração do DroppedNeedle, nunca no git.
