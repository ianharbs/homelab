# Deezer plugin for DroppedNeedle

[English](#english) · [Português (BR)](#português-br)

---

## English

> ⚠️ **Disclaimer: read before using**
>
> This plugin downloads and **decrypts audio streams from Deezer** using your own
> account's session cookie (ARL). Doing so **violates Deezer's Terms of Service**
> and, depending on where you live, may infringe copyright law or rules against
> circumventing technical protection measures. Deezer may **suspend or terminate
> the account** whose ARL you use.
>
> It is provided **as is, for personal and educational use only**, with no warranty
> of any kind. You are solely responsible for how you use it and for complying with
> the laws of your country and with Deezer's terms. Do not use it to distribute
> music. If you like an artist, buy their music or support them directly.

### What it does

A DroppedNeedle plugin that acts as both an **indexer** (search) and a
**download client** for Deezer, with no proxy (Octo Fiesta, Deemix, etc.) in between.

- **Exact edition:** DroppedNeedle only hands a plugin the artist, album, year
  and track count. The plugin therefore resolves the target edition itself on
  MusicBrainz, using the same `select_edition` rule as DroppedNeedle. It then
  assembles exactly that tracklist from Deezer:
  1. match by ISRC/UPC;
  2. then by title + duration;
  3. fill any missing tracks from other Deezer albums (compilations, remasters).

  This avoids the partial imports you get when Deezer only carries a different
  edition. Without a MusicBrainz match, it falls back to plain Deezer album matching.
- **Real quality and size before downloading:** it reports `lossless` only when
  every track is available in FLAC, so there is no "unknown quality".
- **Importer-aligned matching:** tracks whose length differs from MusicBrainz by
  more than max(15 s, 10 %) are skipped, because DroppedNeedle would reject them
  on import anyway.
- **Scores:**

  | Result | Score |
  |---|---|
  | Full tracklist from one album | 0.95 |
  | Full tracklist assembled from several albums | 0.90 |
  | ≥ 75 % of the tracks | 0.72 (auto-accepted) |
  | Fewer tracks | 0.60 (manual review) |

- **Files:** decrypted FLAC/MP3 tagged with MusicBrainz IDs, ISRC and cover art.
  The staging folder is removed after import.

### Requirements

- DroppedNeedle with plugin API v1. Tested on **v2.15.0**. The plugin uses a few
  DroppedNeedle internals (with fallbacks), so a future DroppedNeedle update may
  break it.
- A Deezer **Premium/HiFi** account for FLAC. A free account will not work.

### Installation

1. Copy the `deezer/` folder into DroppedNeedle's plugins folder, which is
   `/app/plugins` inside the container. Map it as a volume, for example
   `./droppedneedle/plugins:/app/plugins`.
2. Make sure the folder is owned by the container user (`PUID`/`PGID`), e.g.
   `chown -R 1000:1000 plugins/deezer`.
3. Restart DroppedNeedle, or save any plugin, so it rescans the folder.
4. In **Settings → Plugins → Deezer (direct)**, paste your ARL and enable the plugin.
5. Optional: in **Downloads → source priority**, put `plugin:deezer` first.

To update the plugin later, replace the files and save the plugin settings again.
Saving reloads the code.

### Getting your ARL

1. Log in at [deezer.com](https://www.deezer.com) in your browser.
2. Open the developer tools (F12) and go to **Application** (Chrome/Edge) or
   **Storage** (Firefox). Open **Cookies → https://www.deezer.com**.
3. Copy the value of the **`arl`** cookie (about 192 characters).
4. Close the tab without logging out, because logging out may invalidate the ARL.

**Treat the ARL like a password:** anyone holding it is logged in as you. Never
commit it to Git or share it. The plugin health check shows your plan, and reports
an error if the ARL is invalid or expired or if the account has no FLAC.

### Settings

| Key | Description | Default |
|---|---|---|
| `arl` | Deezer ARL cookie (secret) | — |
| `quality` | `flac` or `mp3_320` | `flac` |
| `allow_fallback` | Accept MP3 320 when FLAC is missing (`yes`/`no`) | `no` |
| `download_dir` | Staging folder inside the container | `/data/downloads/deezer` |
| `max_parallel` | Tracks downloaded at once (1–6). Keep it low | `3` |
| `musicbrainz_url` | MusicBrainz WS/2 base URL. Use your mirror if DroppedNeedle uses one | `https://musicbrainz.org/ws/2` |

### Known limitations

- **What was tested:** full album requests, including editions assembled from
  several Deezer albums and albums with shared artist credits.
- **What was not tested much yet:**
  - MP3 fallback;
  - single-track requests;
  - multi-disc releases.
- **Albums already partly in the library:** DroppedNeedle will not overwrite files
  it does not own (`target_occupied`). To re-download such an album, move the
  partial folder out, rescan the library, and request the album again.
- **Catalog gaps:** if Deezer lacks a track, or only has a version whose length
  differs from MusicBrainz, the album stays incomplete.
- **Maintenance:** Deezer can change its private API at any time, which would break
  downloads until the plugin is updated.

---

## Português (BR)

> ⚠️ **Aviso: leia antes de usar**
>
> Este plugin baixa e **descriptografa streams de áudio do Deezer** usando o cookie
> de sessão (ARL) da sua própria conta. Isso **viola os Termos de Uso do Deezer**
> e, dependendo do país, pode infringir a lei de direitos autorais ou regras contra
> contornar medidas técnicas de proteção. O Deezer pode **suspender ou encerrar a
> conta** cujo ARL você usar.
>
> Ele é fornecido **"no estado em que se encontra", apenas para uso pessoal e
> educacional**, sem garantia de nenhum tipo. Você é o único responsável pelo uso
> que fizer e por cumprir as leis do seu país e os termos do Deezer. Não use para
> distribuir música. Se você gosta de um artista, compre a música ou apoie-o
> diretamente.

### O que ele faz

Plugin do DroppedNeedle que funciona como **indexador** (busca) e **cliente de
download** do Deezer, sem proxy no meio (Octo Fiesta, Deemix etc.).

- **Edição exata:** o DroppedNeedle só passa ao plugin artista, álbum, ano e número
  de faixas. Por isso o plugin descobre sozinho a edição-alvo no MusicBrainz, com a
  mesma regra `select_edition` do DroppedNeedle. Depois monta exatamente aquele
  tracklist no Deezer:
  1. casa por ISRC/UPC;
  2. depois por título + duração;
  3. completa faixas faltantes com outros álbuns do Deezer (coletâneas, remasters).

  Isso evita importações pela metade quando o Deezer só tem outra edição. Sem
  correspondência no MusicBrainz, ele usa a busca simples de álbuns do Deezer.
- **Qualidade e tamanho reais antes de baixar:** só informa `lossless` quando todas
  as faixas têm FLAC, então não aparece mais "qualidade desconhecida".
- **Alinhado ao importador:** faixas cuja duração difere do MusicBrainz em mais de
  máx(15 s, 10%) são ignoradas, porque o DroppedNeedle as recusaria na importação.
- **Notas:**

  | Resultado | Nota |
  |---|---|
  | Tracklist completo de um álbum | 0,95 |
  | Tracklist completo montado de vários álbuns | 0,90 |
  | ≥ 75% das faixas | 0,72 (aceite automático) |
  | Menos faixas | 0,60 (revisão manual) |

- **Arquivos:** FLAC/MP3 descriptografados e etiquetados com IDs do MusicBrainz,
  ISRC e capa. A pasta temporária é apagada após a importação.

### Requisitos

- DroppedNeedle com API de plugins v1. Testado na **v2.15.0**. O plugin usa algumas
  funções internas do DroppedNeedle (com alternativas), então uma atualização futura
  dele pode quebrar o plugin.
- Conta **Premium/HiFi** do Deezer para FLAC. Conta gratuita não funciona.

### Instalação

1. Copie a pasta `deezer/` para a pasta de plugins do DroppedNeedle, que é
   `/app/plugins` dentro do container. Mapeie-a como volume, por exemplo
   `./droppedneedle/plugins:/app/plugins`.
2. Garanta que a pasta pertença ao usuário do container (`PUID`/`PGID`), por exemplo
   `chown -R 1000:1000 plugins/deezer`.
3. Reinicie o DroppedNeedle, ou salve qualquer plugin, para ele reescanear a pasta.
4. Em **Configurações → Plugins → Deezer (direct)**, cole o seu ARL e ative o plugin.
5. Opcional: em **Downloads → prioridade das fontes**, coloque `plugin:deezer` em
   primeiro.

Para atualizar o plugin depois, substitua os arquivos e salve as configurações do
plugin de novo. Salvar recarrega o código.

### Como pegar o ARL

1. Entre em [deezer.com](https://www.deezer.com) no navegador.
2. Abra as ferramentas de desenvolvedor (F12) e vá em **Application** (Chrome/Edge)
   ou **Armazenamento** (Firefox). Abra **Cookies → https://www.deezer.com**.
3. Copie o valor do cookie **`arl`** (cerca de 192 caracteres).
4. Feche a aba sem sair da conta, porque o logout pode invalidar o ARL.

**Trate o ARL como uma senha:** quem tiver ele entra na sua conta. Nunca coloque no
Git nem compartilhe. A verificação de saúde do plugin mostra o seu plano e dá erro se
o ARL estiver inválido ou expirado, ou se a conta não tiver FLAC.

### Configurações

| Chave | Descrição | Padrão |
|---|---|---|
| `arl` | Cookie ARL do Deezer (secreto) | — |
| `quality` | `flac` ou `mp3_320` | `flac` |
| `allow_fallback` | Aceitar MP3 320 quando não houver FLAC (`sim`/`não`) | `não` |
| `download_dir` | Pasta temporária dentro do container | `/data/downloads/deezer` |
| `max_parallel` | Faixas baixadas ao mesmo tempo (1–6). Mantenha baixo | `3` |
| `musicbrainz_url` | URL base do MusicBrainz WS/2. Use o seu espelho se o DroppedNeedle usar um | `https://musicbrainz.org/ws/2` |

### Limitações conhecidas

- **O que foi testado:** pedidos de álbum completo, incluindo edições montadas de
  vários álbuns do Deezer e álbuns com crédito de artista compartilhado.
- **O que ainda foi pouco testado:**
  - o fallback para MP3;
  - pedidos de faixa avulsa;
  - lançamentos com vários discos.
- **Álbum que já está em parte na biblioteca:** o DroppedNeedle não sobrescreve
  arquivos que não são dele (`target_occupied`). Para baixar de novo, tire a pasta
  parcial da biblioteca, reescaneie e peça o álbum outra vez.
- **Lacunas no catálogo:** se o Deezer não tiver uma faixa, ou só tiver uma versão
  com duração diferente da do MusicBrainz, o álbum fica incompleto.
- **Manutenção:** o Deezer pode mudar a API privada a qualquer momento, e os
  downloads param até o plugin ser atualizado.
