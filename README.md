# Fluxo de Caixa

Aplicativo web de fluxo de caixa feito com Streamlit.

## Executar localmente

```bash
python -m pip install -r requirements.txt
streamlit run app.py
```

O banco SQLite local (`fluxo_caixa.db`) é criado automaticamente ao iniciar o app. Para escolher outro local, configure `FLUXO_CAIXA_DB_PATH`.

## Configurar acesso

O app exige login Google via OpenID Connect e libera o acesso somente aos e-mails listados em `access.allowed_emails`.

1. Crie credenciais OAuth 2.0 do tipo aplicativo web no Google Cloud e adicione `http://localhost:8501/oauth2callback` como URI de redirecionamento autorizada.
2. Copie `.streamlit/secrets.toml.example` para `.streamlit/secrets.toml` e substitua os valores e e-mails:

```toml
[auth]
redirect_uri = "http://localhost:8501/oauth2callback"
cookie_secret = "gere-um-segredo-aleatorio-longo"
client_id = "SEU_CLIENT_ID"
client_secret = "SEU_CLIENT_SECRET"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"

[access]
allowed_emails = ["voce@exemplo.com"]
```

3. Para publicar, configure os mesmos valores nos Secrets do serviço de hospedagem e atualize `redirect_uri` para `https://SEU-DOMINIO/oauth2callback`. Cadastre essa URL também nas URIs autorizadas do cliente OAuth.

O arquivo `.streamlit/secrets.toml` é local e não deve ser enviado ao Git. Adicione somente os e-mails que devem ter acesso.

## Publicar na web

1. Crie um repositório no GitHub e envie `app.py`, `requirements.txt`, `.gitignore` e este README.
2. Não envie `fluxo_caixa.db` nem backups: eles podem conter informações financeiras.
3. No Streamlit Community Cloud, crie um app apontando para esse repositório, branch e `app.py`.

## Dados e privacidade

O SQLite é adequado para execução local, mas o armazenamento local de uma instância hospedada pode ser temporário e não é compartilhado entre instâncias. Os dados adicionados no app publicado podem ser apagados quando o serviço reiniciar. Para uso contínuo, conecte o app a um banco persistente (por exemplo, PostgreSQL gerenciado) antes de cadastrar dados reais.

O login restringe a entrada por identidade Google e lista de e-mails autorizados. Para uso contínuo na web, conecte o app também a um banco persistente e privado.
