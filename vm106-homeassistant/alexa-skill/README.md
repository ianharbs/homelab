# Skill Alexa Smart Home própria (grátis)

A Alexa comanda o Home Assistant por uma skill Smart Home nossa, sem Nabu Casa:

```
Echo → nuvem Alexa → AWS Lambda (us-east-1) → https://ha.ihome.dev.br/api/alexa/smart_home
                                               (Cloudflare Tunnel → NPM → HA 192.168.10.60:8123)
```

- O que vai para a Alexa é definido no bloco `alexa: smart_home:` do `/config/configuration.yaml` do HA
  (cópia em [`alexa.yaml`](alexa.yaml)). Mudou o filtro → `ha core restart` → "Alexa, descobrir dispositivos".
- `lambda_function.py` é o código oficial da documentação do HA
  (gist matt2005/744b5ef548cc13d88d0569eea65f5e5b), sem alterações.
- pt-BR usa a região Norte-América: Lambda em **US East (N. Virginia) / us-east-1**, client ID
  `https://pitangui.amazon.com/`, eventos em `https://api.amazonalexa.com/v3/events`.

## Passo a passo

### 1. Criar a skill (developer.amazon.com → Alexa Skills Kit)
Use a **mesma conta Amazon das Echos**.
1. *Create Skill* → nome "Home Assistant", idioma **Portuguese (BR)**, tipo **Smart Home**, *Provision your own*.
2. Copie o **Skill ID** (`amzn1.ask.skill....`).

### 2. Criar a Lambda (console.aws.amazon.com, região **N. Virginia / us-east-1**)
1. IAM → *Create role* → serviço *Lambda* → política `AWSLambdaBasicExecutionRole` → nome `AWSLambdaBasicExecutionRole-SmartHome`.
2. Lambda → *Create function* → *Author from scratch* → nome `HomeAssistant`, runtime **Python 3.13**,
   role existente acima.
3. *Add trigger* → **Alexa** → *Alexa Smart Home* → cole o Skill ID.
4. Cole `lambda_function.py` no editor → *Deploy*.
5. *Configuration → Environment variables* → `BASE_URL` = `https://ha.ihome.dev.br` (sem barra no fim).
6. Copie o **ARN** da função (canto superior direito).

### 3. Voltar à skill
1. *Smart Home → Default endpoint* = ARN da Lambda → *Save*.
2. *Account Linking*:
   - Authorization URI: `https://ha.ihome.dev.br/auth/authorize`
   - Access Token URI: `https://ha.ihome.dev.br/auth/token`
   - Client ID: `https://pitangui.amazon.com/` (com a barra final)
   - Client Secret: qualquer texto (o HA não confere)
   - Authentication Scheme: **Credentials in request body**
   - Scope: `smart_home` → *Save*.
3. (Opcional, recomendado) *Permissions* → ligar **Send Alexa Events** → copiar *Alexa Client Id* e
   *Alexa Client Secret* para o `secrets.yaml` do HA (`alexa_client_id`, `alexa_client_secret`) e
   descomentar `endpoint/client_id/client_secret` no bloco `alexa:`. Sem isso tudo funciona, mas o app
   Alexa não vê mudanças de estado feitas fora dela.

### 4. Ativar
App Alexa → *Mais → Skills e jogos → Suas skills → Dev* → "Home Assistant" → *Ativar* → login no HA →
"Alexa, descobrir dispositivos".

## Testar sem a Alexa
```bash
. /root/homelab/.env
curl -s -X POST https://ha.ihome.dev.br/api/alexa/smart_home \
  -H "Authorization: Bearer $HA_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"directive\":{\"header\":{\"namespace\":\"Alexa.Discovery\",\"name\":\"Discover\",\"payloadVersion\":\"3\",\"messageId\":\"t\"},\"payload\":{\"scope\":{\"type\":\"BearerToken\",\"token\":\"$HA_TOKEN\"}}}}"
```
