# Plugins do DroppedNeedle

Cópia versionada dos plugins em `/opt/docker/droppedneedle/plugins/` no CT 102
(montado em `/app/plugins` no container). Para instalar ou atualizar:

```sh
cp -r deezer /opt/docker/droppedneedle/plugins/ && chown -R 1000:1000 /opt/docker/droppedneedle/plugins/deezer
```

Depois disso, salve o plugin em **Configurações → Plugins** (o salvamento recarrega o código).

## deezer

Fonte de busca e download direto do Deezer, sem o octo-fiesta no meio.

- **Edição certa:** o DroppedNeedle só passa artista, álbum, ano e número de faixas.
  O plugin descobre a edição-alvo no MusicBrainz (mesma regra `select_edition`
  do DroppedNeedle) e monta exatamente aquele tracklist no Deezer:
  1. busca por ISRC/UPC;
  2. depois por título + duração;
  3. completa faixas faltantes com outros álbuns do Deezer.
- **Duração:** segue a tolerância do importador do DroppedNeedle
  (máx(15 s, 10%) em relação ao MusicBrainz).
  - Tracklist completo: score 0,95 (ou 0,90 se montado de vários álbuns).
  - ≥ 75% das faixas: 0,72 (aceite automático).
  - Menos que isso: 0,60 (revisão manual).
- **Qualidade:** tamanho e qualidade vêm do Deezer antes do download.
  O plugin informa `lossless` só quando todas as faixas têm FLAC.
- **Arquivos:** stream descriptografado (Blowfish CBC) e etiquetado com os IDs
  do MusicBrainz, ISRC e capa.

**Configurações:**

| Chave | Valor |
|---|---|
| `arl` | Cookie `arl` de uma conta Premium/HiFi (fica só na config do DroppedNeedle) |
| `quality` | `flac` ou `mp3_320` |
| `allow_fallback` | Aceitar MP3 320 quando não houver FLAC |
| `download_dir` | Padrão: `/data/downloads/deezer` |
| `max_parallel` | Downloads simultâneos (padrão 3) |
| `musicbrainz_url` | Aqui: `https://api.brainzmash.cc/ws/2`, a mesma fonte do DroppedNeedle |

A ordem das fontes fica em **Downloads → prioridade**: `plugin:deezer`, usenet, soulseek.
