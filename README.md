# Fluxo de Caixa

Aplicativo web de fluxo de caixa feito com Streamlit.

## Executar localmente

```bash
python -m pip install -r requirements.txt
streamlit run app.py
```

O banco SQLite local (`fluxo_caixa.db`) é criado automaticamente ao iniciar o app. Para escolher outro local, configure `FLUXO_CAIXA_DB_PATH`.

## Login simples

O app usa uma senha compartilhada. Configure-a localmente em `.streamlit/secrets.toml`, copiando o arquivo `.streamlit/secrets.toml.example`, ou defina a variável de ambiente `FLUXO_CAIXA_SENHA`:

```toml
[login]
senha = "escolha-uma-senha"
```

No Streamlit Community Cloud, adicione o mesmo bloco na área **Settings → Secrets**. A senha vale para todas as pessoas e não identifica usuários individualmente. A sessão termina ao clicar em **Sair** ou fechar/reiniciar a sessão do app. Não use essa proteção para dados financeiros reais ou em um app público.

## Publicar na web

1. Crie um repositório no GitHub e envie `app.py`, `requirements.txt`, `.gitignore` e este README.
2. Não envie `fluxo_caixa.db` nem backups: eles podem conter informações financeiras.
3. No Streamlit Community Cloud, crie um app apontando para esse repositório, branch e `app.py`.

## Dados e privacidade

O SQLite é adequado para execução local, mas o armazenamento local de uma instância hospedada pode ser temporário e não é compartilhado entre instâncias. Os dados adicionados no app publicado podem ser apagados quando o serviço reiniciar. Para uso contínuo, conecte o app a um banco persistente (por exemplo, PostgreSQL gerenciado) antes de cadastrar dados reais.

O login simples funciona como uma barreira básica de acesso, não como autenticação forte. O banco SQLite hospedado pode ser temporário; não cadastre dados financeiros reais sem configurar armazenamento persistente e privado.
