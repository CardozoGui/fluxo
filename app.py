import json
import os
import sqlite3
from collections import Counter
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pandas as pd
import streamlit as st

DB_PATH = Path(
    os.environ.get(
        "FLUXO_CAIXA_DB_PATH",
        str(Path(__file__).resolve().with_name("fluxo_caixa.db")),
    )
)
CATEGORIAS_PADRAO = ["Vendas", "Serviços", "Aluguel", "Salários", "Marketing", "Outros"]
TIPOS = ["Receita", "Despesa"]
STATUS = ["Realizada", "Prevista"]
FREQUENCIAS = ["Semanal", "Mensal", "Anual"]

st.set_page_config(
    page_title="Fluxo de Caixa | Visão geral",
    page_icon=":material/account_balance_wallet:",
    layout="wide",
)


def exigir_login() -> None:
    """Protege o app com login OIDC e uma lista explícita de e-mails autorizados."""
    if "auth" not in st.secrets:
        st.title("Acesso ao fluxo de caixa")
        st.error(
            "A autenticação ainda não foi configurada. Defina as credenciais OIDC "
            "em .streamlit/secrets.toml (ou nos Secrets do serviço de hospedagem)."
        )
        st.stop()

    emails_autorizados = {
        email.strip().casefold()
        for email in st.secrets.get("access", {}).get("allowed_emails", [])
        if email.strip()
    }
    if not emails_autorizados:
        st.title("Acesso ao fluxo de caixa")
        st.error("Nenhum e-mail autorizado foi configurado. O acesso está bloqueado.")
        st.stop()

    if not st.user.is_logged_in:
        st.title("Acesso ao fluxo de caixa")
        st.caption("Entre com a conta autorizada para acessar os dados financeiros.")
        if st.button("Entrar com Google", type="primary", icon=":material/login:"):
            st.login()
        st.stop()

    email = str(st.user.email).strip().casefold()
    if email not in emails_autorizados:
        st.title("Acesso não autorizado")
        st.error("Esta conta não está autorizada a acessar o fluxo de caixa.")
        st.button("Sair", on_click=st.logout)
        st.stop()


def criar_tabela_transacoes(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE transacoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL,
            descricao TEXT NOT NULL,
            categoria TEXT NOT NULL,
            valor_centavos INTEGER NOT NULL CHECK (valor_centavos > 0),
            tipo TEXT NOT NULL CHECK (tipo IN ('Receita', 'Despesa')),
            status TEXT NOT NULL DEFAULT 'Realizada'
                CHECK (status IN ('Realizada', 'Prevista'))
        )
        """
    )


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("BEGIN IMMEDIATE")
        tabelas = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "transacoes" not in tabelas:
            criar_tabela_transacoes(conn)
        else:
            colunas = {
                row[1] for row in conn.execute("PRAGMA table_info(transacoes)")
            }
            if "valor_centavos" not in colunas:
                conn.execute("ALTER TABLE transacoes RENAME TO transacoes_legado")
                criar_tabela_transacoes(conn)
                linhas = conn.execute(
                    """
                    SELECT id, data, descricao, categoria, valor, tipo
                    FROM transacoes_legado
                    """
                ).fetchall()
                for linha in linhas:
                    centavos = int(
                        (Decimal(str(linha[4])) * 100).quantize(
                            Decimal("1"), rounding=ROUND_HALF_UP
                        )
                    )
                    conn.execute(
                        """
                        INSERT INTO transacoes
                            (id, data, descricao, categoria, valor_centavos, tipo)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (*linha[:4], centavos, linha[5]),
                    )
                conn.execute("DROP TABLE transacoes_legado")
            elif "status" not in colunas:
                conn.execute(
                    "ALTER TABLE transacoes ADD COLUMN status TEXT NOT NULL "
                    "DEFAULT 'Realizada'"
                )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS categorias (
                nome TEXT PRIMARY KEY COLLATE NOCASE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recorrencias (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inicio TEXT NOT NULL,
                fim TEXT,
                descricao TEXT NOT NULL,
                categoria TEXT NOT NULL,
                valor_centavos INTEGER NOT NULL CHECK (valor_centavos > 0),
                tipo TEXT NOT NULL CHECK (tipo IN ('Receita', 'Despesa')),
                frequencia TEXT NOT NULL CHECK (
                    frequencia IN ('Semanal', 'Mensal', 'Anual')
                )
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS configuracoes (
                chave TEXT PRIMARY KEY,
                valor TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            "INSERT OR IGNORE INTO categorias (nome) VALUES (?)",
            [(categoria,) for categoria in CATEGORIAS_PADRAO],
        )
        categorias_existentes = conn.execute(
            "SELECT DISTINCT categoria FROM transacoes"
        ).fetchall()
        categorias_recorrentes = conn.execute(
            "SELECT DISTINCT categoria FROM recorrencias"
        ).fetchall()
        conn.executemany(
            "INSERT OR IGNORE INTO categorias (nome) VALUES (?)",
            categorias_existentes + categorias_recorrentes,
        )
        conn.execute(
            "INSERT OR IGNORE INTO configuracoes (chave, valor) VALUES "
            "('saldo_inicial_centavos', '0')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO configuracoes (chave, valor) VALUES (?, ?)",
            ("saldo_inicial_data", date.today().isoformat()),
        )


def centavos_de(valor: float | Decimal) -> int:
    return int(
        (Decimal(str(valor)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def formatar_moeda(centavos: int) -> str:
    valor = abs(Decimal(centavos)) / 100
    formatado = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    sinal = "-" if centavos < 0 else ""
    return f"{sinal}R$ {formatado}"


def carregar_categorias() -> list[str]:
    with sqlite3.connect(DB_PATH) as conn:
        return [
            row[0]
            for row in conn.execute("SELECT nome FROM categorias ORDER BY nome COLLATE NOCASE")
        ]


@st.cache_data(ttl=60, max_entries=1)
def carregar_dados() -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query(
            """
            SELECT id, data, descricao, categoria, valor_centavos, tipo, status
            FROM transacoes
            ORDER BY data DESC, id DESC
            """,
            conn,
        )
    df["data"] = pd.to_datetime(df["data"], errors="raise")
    df["valor"] = df["valor_centavos"] / 100
    return df


def carregar_recorrencias() -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query(
            """
            SELECT id, inicio, fim, descricao, categoria, valor_centavos, tipo, frequencia
            FROM recorrencias
            ORDER BY inicio, id
            """,
            conn,
        )
    if not df.empty:
        df["inicio"] = pd.to_datetime(df["inicio"], errors="raise")
        df["fim"] = pd.to_datetime(df["fim"], errors="coerce")
        df["valor"] = df["valor_centavos"] / 100
    return df


def carregar_configuracoes() -> tuple[int, date]:
    with sqlite3.connect(DB_PATH) as conn:
        configuracoes = dict(
            conn.execute("SELECT chave, valor FROM configuracoes").fetchall()
        )
    return (
        int(configuracoes["saldo_inicial_centavos"]),
        date.fromisoformat(configuracoes["saldo_inicial_data"]),
    )


def salvar_transacao(
    data_transacao: date,
    descricao: str,
    categoria: str,
    valor_centavos: int,
    tipo: str,
    status: str,
    transacao_id: int | None = None,
) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        if transacao_id is None:
            conn.execute(
                """
                INSERT INTO transacoes
                    (data, descricao, categoria, valor_centavos, tipo, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    data_transacao.isoformat(),
                    descricao.strip(),
                    categoria,
                    valor_centavos,
                    tipo,
                    status,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE transacoes
                SET data = ?, descricao = ?, categoria = ?, valor_centavos = ?,
                    tipo = ?, status = ?
                WHERE id = ?
                """,
                (
                    data_transacao.isoformat(),
                    descricao.strip(),
                    categoria,
                    valor_centavos,
                    tipo,
                    status,
                    transacao_id,
                ),
            )


def deletar_transacao(transacao_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("DELETE FROM transacoes WHERE id = ?", (transacao_id,))
        return cursor.rowcount


def salvar_saldo_inicial(centavos: int, data_referencia: date) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES (?, ?)",
            [
                ("saldo_inicial_centavos", str(centavos)),
                ("saldo_inicial_data", data_referencia.isoformat()),
            ],
        )


def gerar_datas_recorrentes(
    inicio: date, fim: date, frequencia: str, limite: date
) -> list[date]:
    data_base = max(inicio, limite)
    if data_base > fim:
        return []

    ocorrencias: list[date] = []
    if frequencia == "Semanal":
        indice = max(0, (data_base - inicio).days // 7)
        data_ocorrencia = inicio + timedelta(weeks=indice)
        while data_ocorrencia <= fim:
            if data_ocorrencia >= data_base:
                ocorrencias.append(data_ocorrencia)
            data_ocorrencia += timedelta(weeks=1)
        return ocorrencias

    meses_por_ciclo = 1 if frequencia == "Mensal" else 12
    diferenca_meses = (data_base.year - inicio.year) * 12 + data_base.month - inicio.month
    indice = max(0, diferenca_meses // meses_por_ciclo)
    while True:
        meses_desde_inicio = indice * meses_por_ciclo
        ano = inicio.year + (inicio.month - 1 + meses_desde_inicio) // 12
        mes = (inicio.month - 1 + meses_desde_inicio) % 12 + 1
        dia = min(inicio.day, monthrange(ano, mes)[1])
        data_ocorrencia = date(ano, mes, dia)
        if data_ocorrencia > fim:
            break
        if data_ocorrencia >= data_base:
            ocorrencias.append(data_ocorrencia)
        indice += 1
    return ocorrencias


def montar_ocorrencias(
    recorrencias: pd.DataFrame,
    inicio: date,
    fim: date,
    limite: date,
    transacoes: pd.DataFrame | None = None,
) -> pd.DataFrame:
    ocorrencias: list[dict[str, object]] = []
    if recorrencias.empty:
        return pd.DataFrame(
            columns=["data", "descricao", "categoria", "valor_centavos", "valor", "tipo", "status"]
        )

    registradas: Counter[tuple[date, str, str, int, str]] = Counter()
    if transacoes is not None and not transacoes.empty:
        registradas.update(
            (
                linha.data.date(),
                linha.descricao,
                linha.categoria,
                int(linha.valor_centavos),
                linha.tipo,
            )
            for linha in transacoes.itertuples(index=False)
        )

    for regra in recorrencias.itertuples(index=False):
        data_inicio = regra.inicio.date()
        data_fim = regra.fim.date() if pd.notna(regra.fim) else fim
        data_limite = max(inicio, limite)
        datas = gerar_datas_recorrentes(
            data_inicio,
            min(fim, data_fim),
            regra.frequencia,
            data_limite,
        )
        for data_ocorrencia in datas:
            chave = (
                data_ocorrencia,
                regra.descricao,
                regra.categoria,
                int(regra.valor_centavos),
                regra.tipo,
            )
            if registradas[chave]:
                registradas[chave] -= 1
                continue
            ocorrencias.append(
                {
                    "data": pd.Timestamp(data_ocorrencia),
                    "descricao": regra.descricao,
                    "categoria": regra.categoria,
                    "valor_centavos": int(regra.valor_centavos),
                    "valor": int(regra.valor_centavos) / 100,
                    "tipo": regra.tipo,
                    "status": "Prevista",
                }
            )
    return pd.DataFrame(
        ocorrencias,
        columns=[
            "data",
            "descricao",
            "categoria",
            "valor_centavos",
            "valor",
            "tipo",
            "status",
        ],
    )


def saldo_projetado(
    df: pd.DataFrame,
    recorrencias: pd.DataFrame,
    saldo_base: int,
    data_base: date,
    data_fim: date,
) -> int | None:
    if data_fim < data_base:
        return None

    movimentacoes = df[
        (
            (df["status"] == "Realizada")
            & (df["data"].dt.date >= data_base)
            & (df["data"].dt.date <= data_fim)
        )
        | (
            (df["status"] == "Prevista")
            & (df["data"].dt.date >= max(data_base, date.today()))
            & (df["data"].dt.date <= data_fim)
        )
    ]
    saldo = saldo_base
    for linha in movimentacoes.itertuples(index=False):
        sinal = 1 if linha.tipo == "Receita" else -1
        saldo += sinal * int(linha.valor_centavos)

    ocorrencias = montar_ocorrencias(
        recorrencias,
        data_base,
        data_fim,
        max(data_base, date.today()),
        df,
    )
    for linha in ocorrencias.itertuples(index=False):
        sinal = 1 if linha.tipo == "Receita" else -1
        saldo += sinal * int(linha.valor_centavos)
    return saldo


def movimentacoes_no_periodo(
    transacoes: pd.DataFrame,
    recorrencias: pd.DataFrame,
    inicio: date,
    fim: date,
    categorias: list[str] | None = None,
) -> pd.DataFrame:
    transacoes_periodo = transacoes[
        transacoes["data"].notna()
        & (transacoes["data"].dt.date >= inicio)
        & (transacoes["data"].dt.date <= fim)
    ].copy()
    if categorias is not None:
        transacoes_periodo = transacoes_periodo[
            transacoes_periodo["categoria"].isin(categorias)
        ]
    transacoes_periodo = transacoes_periodo[
        ["data", "descricao", "categoria", "valor_centavos", "tipo", "status"]
    ]

    ocorrencias = montar_ocorrencias(
        recorrencias,
        inicio,
        fim,
        max(inicio, date.today()),
        transacoes,
    )
    if categorias is not None:
        ocorrencias = ocorrencias[ocorrencias["categoria"].isin(categorias)]
    movimentos = pd.concat(
        [transacoes_periodo, ocorrencias], ignore_index=True
    )
    movimentos["data"] = pd.to_datetime(movimentos["data"], errors="raise")
    return movimentos


def periodo_inicial(df: pd.DataFrame) -> tuple[date, date]:
    datas_validas = df["data"].dropna() if not df.empty else pd.Series(dtype="datetime64[ns]")
    if datas_validas.empty:
        hoje = date.today()
        return hoje, hoje
    return datas_validas.min().date(), max(datas_validas.max().date(), date.today())


def proximo_periodo(data_inicio: date, data_fim: date) -> tuple[date, date]:
    dias = (data_fim - data_inicio).days + 1
    return data_inicio - timedelta(days=dias), data_inicio - timedelta(days=1)


def formatar_variacao(atual: int, anterior: int) -> str | None:
    if anterior == 0:
        return None
    variacao = (atual - anterior) / abs(anterior) * 100
    return f"{variacao:+.1f}%"


def criar_backup() -> bytes:
    with sqlite3.connect(DB_PATH) as conn:
        transacoes = [
            dict(zip(("id", "data", "descricao", "categoria", "valor_centavos", "tipo", "status"), row))
            for row in conn.execute(
                """
                SELECT id, data, descricao, categoria, valor_centavos, tipo, status
                FROM transacoes ORDER BY id
                """
            ).fetchall()
        ]
        recorrencias = [
            dict(zip(("id", "inicio", "fim", "descricao", "categoria", "valor_centavos", "tipo", "frequencia"), row))
            for row in conn.execute(
                """
                SELECT id, inicio, fim, descricao, categoria, valor_centavos, tipo, frequencia
                FROM recorrencias ORDER BY id
                """
            ).fetchall()
        ]
        categorias = [
            row[0] for row in conn.execute("SELECT nome FROM categorias ORDER BY nome")
        ]
        configuracoes = dict(
            conn.execute("SELECT chave, valor FROM configuracoes").fetchall()
        )
    return json.dumps(
        {
            "versao": 1,
            "moeda": "BRL",
            "transacoes": transacoes,
            "recorrencias": recorrencias,
            "categorias": categorias,
            "configuracoes": configuracoes,
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")


def invalidar_dados() -> None:
    carregar_dados.clear()


exigir_login()

init_db()
df = carregar_dados()
df_recorrencias = carregar_recorrencias()
categorias_disponiveis = carregar_categorias()
saldo_base, data_base = carregar_configuracoes()
periodo_padrao = periodo_inicial(df)

aba_visao, aba_movimentacoes, aba_cadastros, aba_relatorios = st.tabs(
    ["Visão geral", "Movimentações", "Cadastros", "Relatórios"]
)

with st.sidebar:
    st.title("Fluxo de caixa", anchor=False)
    st.caption("Controle financeiro")
    st.caption(f"Acesso: {st.user.email}")
    st.button("Sair", on_click=st.logout, width="stretch")

    st.subheader("Filtros", divider="gray")
    if "filtro_periodo" not in st.session_state:
        st.session_state["filtro_periodo"] = periodo_padrao
    periodo = st.date_input(
        "Período",
        key="filtro_periodo",
        help="Os indicadores e gráficos consideram o período selecionado.",
    )
    categorias_selecionadas = st.multiselect(
        "Categorias",
        options=categorias_disponiveis,
        default=categorias_disponiveis,
    )
    granularidade = st.selectbox(
        "Agrupar gráfico por", ["Dia", "Semana", "Mês"], index=2
    )
    st.caption("Os filtros atualizam o painel automaticamente.")

with aba_cadastros:
    st.subheader("Nova transação", divider="gray")
    with st.form("form_transacao", clear_on_submit=True):
        data_transacao = st.date_input("Data", value=date.today())
        descricao = st.text_input("Descrição", placeholder="Ex.: venda de serviço")
        categoria = st.selectbox("Categoria", categorias_disponiveis)
        valor = st.number_input(
            "Valor (R$)", min_value=0.0, value=0.0, step=10.0, format="%.2f"
        )
        tipo = st.selectbox("Tipo", TIPOS)
        status = st.selectbox("Situação", STATUS, index=0)
        submit = st.form_submit_button(
            "Adicionar transação",
            type="primary",
            width="stretch",
            icon=":material/add:",
        )

    if submit:
        descricao_limpa = descricao.strip()
        valor_em_centavos = centavos_de(valor)
        if not descricao_limpa or valor_em_centavos <= 0:
            st.error("Informe uma descrição e um valor de pelo menos R$ 0,01.")
        elif status == "Realizada" and data_transacao > date.today():
            st.error("Uma transação futura precisa ser marcada como prevista.")
        else:
            salvar_transacao(
                data_transacao,
                descricao_limpa,
                categoria,
                valor_em_centavos,
                tipo,
                status,
            )
            invalidar_dados()
            st.success("Transação registrada.")
            st.rerun()

    with st.expander("Saldo inicial", icon=":material/account_balance:"):
        with st.form("form_saldo_inicial"):
            data_saldo = st.date_input(
                "Saldo válido desde",
                value=data_base,
                help="Informe o saldo no início desse dia; as movimentações posteriores "
                "serão somadas à projeção.",
            )
            valor_saldo = st.number_input(
                "Saldo de abertura (R$)",
                min_value=-1_000_000_000.0,
                value=float(Decimal(saldo_base) / 100),
                step=100.0,
                format="%.2f",
            )
            salvar_saldo = st.form_submit_button(
                "Salvar saldo inicial", width="stretch"
            )
        if salvar_saldo:
            salvar_saldo_inicial(centavos_de(valor_saldo), data_saldo)
            st.toast("Saldo inicial atualizado.", icon=":material/check:")
            st.rerun()

    with st.expander("Gerenciar categorias", icon=":material/category:"):
        with st.form("form_nova_categoria", clear_on_submit=True):
            nova_categoria = st.text_input("Nova categoria")
            adicionar_categoria = st.form_submit_button("Adicionar categoria")
        if adicionar_categoria:
            nome = nova_categoria.strip()
            if not nome:
                st.error("Informe o nome da categoria.")
            elif any(nome.casefold() == item.casefold() for item in categorias_disponiveis):
                st.error("Essa categoria já existe.")
            else:
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute("INSERT INTO categorias (nome) VALUES (?)", (nome,))
                st.rerun()

        if categorias_disponiveis:
            categoria_gerenciar = st.selectbox(
                "Categoria para alterar", categorias_disponiveis, key="categoria_gerenciar"
            )
            with st.form(f"form_renomear_categoria_{categoria_gerenciar}"):
                novo_nome = st.text_input(
                    "Novo nome",
                    value=categoria_gerenciar,
                    key=f"novo_nome_categoria_{categoria_gerenciar}",
                )
                renomear_categoria = st.form_submit_button("Renomear categoria")
            if renomear_categoria:
                nome = novo_nome.strip()
                if not nome:
                    st.error("Informe o novo nome.")
                elif nome.casefold() != categoria_gerenciar.casefold() and any(
                    nome.casefold() == item.casefold()
                    for item in categorias_disponiveis
                ):
                    st.error("Já existe uma categoria com esse nome.")
                else:
                    with sqlite3.connect(DB_PATH) as conn:
                        conn.execute(
                            "UPDATE categorias SET nome = ? WHERE nome = ?",
                            (nome, categoria_gerenciar),
                        )
                        conn.execute(
                            "UPDATE transacoes SET categoria = ? WHERE categoria = ?",
                            (nome, categoria_gerenciar),
                        )
                        conn.execute(
                            "UPDATE recorrencias SET categoria = ? WHERE categoria = ?",
                            (nome, categoria_gerenciar),
                        )
                    invalidar_dados()
                    st.rerun()

            confirmar_exclusao_categoria = st.checkbox(
                f"Confirmo a exclusão de “{categoria_gerenciar}”"
            )
            if st.button(
                "Excluir categoria",
                disabled=not confirmar_exclusao_categoria,
                icon=":material/delete:",
            ):
                with sqlite3.connect(DB_PATH) as conn:
                    uso = conn.execute(
                        """
                        SELECT
                            (SELECT COUNT(*) FROM transacoes WHERE categoria = ?) +
                            (SELECT COUNT(*) FROM recorrencias WHERE categoria = ?)
                        """,
                        (categoria_gerenciar, categoria_gerenciar),
                    ).fetchone()[0]
                    if uso:
                        st.error(
                            "A categoria está em uso. Renomeie-a ou reclassifique "
                            "os lançamentos antes de excluí-la."
                        )
                    else:
                        conn.execute(
                            "DELETE FROM categorias WHERE nome = ?",
                            (categoria_gerenciar,),
                        )
                        st.rerun()

    with st.expander("Nova recorrência", icon=":material/event_repeat:"):
        with st.form("form_recorrencia", clear_on_submit=True):
            descricao_recorrencia = st.text_input(
                "Descrição da recorrência", placeholder="Ex.: aluguel"
            )
            categoria_recorrencia = st.selectbox(
                "Categoria da recorrência",
                categorias_disponiveis,
                key="categoria_recorrencia",
            )
            valor_recorrencia = st.number_input(
                "Valor recorrente (R$)",
                min_value=0.0,
                value=0.0,
                step=10.0,
                format="%.2f",
            )
            tipo_recorrencia = st.selectbox(
                "Tipo da recorrência", TIPOS, key="tipo_recorrencia"
            )
            frequencia = st.selectbox("Frequência", FREQUENCIAS)
            inicio_recorrencia = st.date_input(
                "Primeira ocorrência", value=date.today(), key="inicio_recorrencia"
            )
            sem_fim = st.checkbox("Sem data final", value=True)
            fim_recorrencia = st.date_input(
                "Última ocorrência",
                value=date.today() + timedelta(days=365),
                disabled=sem_fim,
                key="fim_recorrencia",
            )
            salvar_recorrencia = st.form_submit_button(
                "Criar recorrência", width="stretch"
            )
        if salvar_recorrencia:
            descricao_limpa = descricao_recorrencia.strip()
            valor_recorrente_em_centavos = centavos_de(valor_recorrencia)
            if not descricao_limpa or valor_recorrente_em_centavos <= 0:
                st.error("Informe uma descrição e um valor de pelo menos R$ 0,01.")
            elif not sem_fim and fim_recorrencia < inicio_recorrencia:
                st.error("A data final deve ser igual ou posterior à inicial.")
            else:
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute(
                        """
                        INSERT INTO recorrencias
                            (inicio, fim, descricao, categoria, valor_centavos, tipo, frequencia)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            inicio_recorrencia.isoformat(),
                            None if sem_fim else fim_recorrencia.isoformat(),
                            descricao_limpa,
                            categoria_recorrencia,
                            valor_recorrente_em_centavos,
                            tipo_recorrencia,
                            frequencia,
                        ),
                    )
                st.rerun()

    if not df_recorrencias.empty:
        with st.expander("Gerenciar recorrências", icon=":material/event_repeat:"):
            recorrencia_por_id = {
                int(linha.id): linha
                for linha in df_recorrencias.itertuples(index=False)
            }
            id_recorrencia = st.selectbox(
                "Recorrência",
                options=list(recorrencia_por_id),
                format_func=lambda identificador: (
                    f"#{identificador} · "
                    f"{recorrencia_por_id[identificador].descricao} · "
                    f"{recorrencia_por_id[identificador].frequencia}"
                ),
            )
            confirmar_exclusao_recorrencia = st.checkbox(
                "Confirmo a exclusão desta recorrência"
            )
            if st.button(
                "Excluir recorrência",
                disabled=not confirmar_exclusao_recorrencia,
                key="excluir_recorrencia",
            ):
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute(
                        "DELETE FROM recorrencias WHERE id = ?", (id_recorrencia,)
                    )
                st.rerun()


with aba_relatorios:
    st.title("Relatórios", icon=":material/assessment:")
    st.caption(
        "Consulte o desempenho mensal e anual e acompanhe projeções de caixa."
    )
    tipo_relatorio = st.segmented_control(
        "Tipo de relatório",
        options=["Mensal", "Anual", "Projeções"],
        default="Mensal",
        key="tipo_relatorio",
    )

    anos_disponiveis = {date.today().year}
    if not df.empty:
        anos_disponiveis.update(int(ano) for ano in df["data"].dt.year.dropna().unique())
    anos = sorted(anos_disponiveis, reverse=True)

    if tipo_relatorio == "Mensal":
        ano_coluna, mes_coluna = st.columns(2)
        with ano_coluna:
            ano_relatorio = st.selectbox(
                "Ano", anos, key="relatorio_mensal_ano"
            )
        with mes_coluna:
            numero_mes = st.selectbox(
                "Mês",
                options=list(range(1, 13)),
                format_func=lambda mes: [
                    "Janeiro",
                    "Fevereiro",
                    "Março",
                    "Abril",
                    "Maio",
                    "Junho",
                    "Julho",
                    "Agosto",
                    "Setembro",
                    "Outubro",
                    "Novembro",
                    "Dezembro",
                ][mes - 1],
                index=date.today().month - 1,
                key="relatorio_mensal_mes",
            )
        inicio_relatorio = date(ano_relatorio, numero_mes, 1)
        fim_relatorio = date(
            ano_relatorio,
            numero_mes,
            monthrange(ano_relatorio, numero_mes)[1],
        )
        movimentos_relatorio = movimentacoes_no_periodo(
            df,
            df_recorrencias,
            inicio_relatorio,
            fim_relatorio,
            categorias_selecionadas,
        )
        realizadas_relatorio = movimentos_relatorio[
            movimentos_relatorio["status"] == "Realizada"
        ]
        previstas_relatorio = movimentos_relatorio[
            movimentos_relatorio["status"] == "Prevista"
        ]
        receitas_realizadas_relatorio = int(
            realizadas_relatorio.loc[
                realizadas_relatorio["tipo"] == "Receita", "valor_centavos"
            ].sum()
        )
        despesas_realizadas_relatorio = int(
            realizadas_relatorio.loc[
                realizadas_relatorio["tipo"] == "Despesa", "valor_centavos"
            ].sum()
        )
        receitas_previstas_relatorio = int(
            previstas_relatorio.loc[
                previstas_relatorio["tipo"] == "Receita", "valor_centavos"
            ].sum()
        )
        despesas_previstas_relatorio = int(
            previstas_relatorio.loc[
                previstas_relatorio["tipo"] == "Despesa", "valor_centavos"
            ].sum()
        )

        metricas_relatorio = st.columns(4)
        metricas_relatorio[0].metric(
            "Receitas realizadas",
            formatar_moeda(receitas_realizadas_relatorio),
            border=True,
        )
        metricas_relatorio[1].metric(
            "Despesas realizadas",
            formatar_moeda(despesas_realizadas_relatorio),
            border=True,
        )
        metricas_relatorio[2].metric(
            "Saldo realizado",
            formatar_moeda(
                receitas_realizadas_relatorio - despesas_realizadas_relatorio
            ),
            border=True,
        )
        metricas_relatorio[3].metric(
            "Fluxo previsto",
            formatar_moeda(
                receitas_previstas_relatorio - despesas_previstas_relatorio
            ),
            border=True,
        )

        saldo_fim_mes = saldo_projetado(
            df, df_recorrencias, saldo_base, data_base, fim_relatorio
        )
        if saldo_fim_mes is None:
            st.info(
                f"Defina o saldo inicial com data-base até {fim_relatorio:%d/%m/%Y} "
                "para calcular o saldo projetado no fim do mês."
            )
        else:
            st.metric(
                "Saldo projetado no fim do mês",
                formatar_moeda(saldo_fim_mes),
                border=True,
            )

        st.subheader("Movimentação por categoria")
        if movimentos_relatorio.empty:
            st.info("Não há movimentações neste mês para as categorias selecionadas.")
        else:
            resumo_categoria = (
                movimentos_relatorio.assign(
                    valor_assinado=movimentos_relatorio.apply(
                        lambda linha: int(linha.valor_centavos)
                        * (1 if linha.tipo == "Receita" else -1),
                        axis=1,
                    )
                )
                .groupby(["categoria", "status"], as_index=False)[
                    "valor_assinado"
                ]
                .sum()
            )
            resumo_categoria["Valor (R$)"] = resumo_categoria[
                "valor_assinado"
            ].map(lambda valor: formatar_moeda(int(valor)))
            resumo_categoria = resumo_categoria.rename(
                columns={"categoria": "Categoria", "status": "Situação"}
            )
            st.dataframe(
                resumo_categoria[["Categoria", "Situação", "Valor (R$)"]],
                hide_index=True,
                width="stretch",
                alt="Fluxo de caixa mensal por categoria e situação",
            )
            por_dia = (
                movimentos_relatorio.assign(
                    dia=movimentos_relatorio["data"].dt.strftime("%d/%m/%Y"),
                    valor=movimentos_relatorio["valor_centavos"] / 100,
                )
                .groupby(["dia", "tipo", "status"], as_index=False)["valor"]
                .sum()
                .pivot(
                    index="dia",
                    columns=["tipo", "status"],
                    values="valor",
                )
                .fillna(0)
            )
            por_dia.columns = [
                f"{tipo} ({situacao.lower()})"
                for tipo, situacao in por_dia.columns
            ]
            st.bar_chart(
                por_dia,
                alt="Receitas e despesas realizadas e previstas por dia no mês",
            )

    elif tipo_relatorio == "Anual":
        ano_relatorio = st.selectbox(
            "Ano do relatório", anos, key="relatorio_anual_ano"
        )
        inicio_relatorio = date(ano_relatorio, 1, 1)
        fim_relatorio = date(ano_relatorio, 12, 31)
        movimentos_relatorio = movimentacoes_no_periodo(
            df,
            df_recorrencias,
            inicio_relatorio,
            fim_relatorio,
            categorias_selecionadas,
        )
        realizadas_relatorio = movimentos_relatorio[
            movimentos_relatorio["status"] == "Realizada"
        ]
        previstas_relatorio = movimentos_relatorio[
            movimentos_relatorio["status"] == "Prevista"
        ]
        receitas_realizadas_relatorio = int(
            realizadas_relatorio.loc[
                realizadas_relatorio["tipo"] == "Receita", "valor_centavos"
            ].sum()
        )
        despesas_realizadas_relatorio = int(
            realizadas_relatorio.loc[
                realizadas_relatorio["tipo"] == "Despesa", "valor_centavos"
            ].sum()
        )
        receitas_previstas_relatorio = int(
            previstas_relatorio.loc[
                previstas_relatorio["tipo"] == "Receita", "valor_centavos"
            ].sum()
        )
        despesas_previstas_relatorio = int(
            previstas_relatorio.loc[
                previstas_relatorio["tipo"] == "Despesa", "valor_centavos"
            ].sum()
        )

        metricas_relatorio = st.columns(4)
        metricas_relatorio[0].metric(
            "Receitas realizadas",
            formatar_moeda(receitas_realizadas_relatorio),
            border=True,
        )
        metricas_relatorio[1].metric(
            "Despesas realizadas",
            formatar_moeda(despesas_realizadas_relatorio),
            border=True,
        )
        metricas_relatorio[2].metric(
            "Saldo realizado",
            formatar_moeda(
                receitas_realizadas_relatorio - despesas_realizadas_relatorio
            ),
            border=True,
        )
        metricas_relatorio[3].metric(
            "Fluxo previsto",
            formatar_moeda(
                receitas_previstas_relatorio - despesas_previstas_relatorio
            ),
            border=True,
        )
        saldo_fim_ano = saldo_projetado(
            df, df_recorrencias, saldo_base, data_base, fim_relatorio
        )
        if saldo_fim_ano is None:
            st.info(
                f"Defina o saldo inicial com data-base até {fim_relatorio:%d/%m/%Y} "
                "para calcular o saldo projetado no fim do ano."
            )
        else:
            st.metric(
                "Saldo projetado no fim do ano",
                formatar_moeda(saldo_fim_ano),
                border=True,
            )

        st.subheader("Evolução mensal")
        meses_ano = pd.period_range(
            f"{ano_relatorio}-01", f"{ano_relatorio}-12", freq="M"
        )
        if movimentos_relatorio.empty:
            resumo_mensal = pd.DataFrame(
                0,
                index=meses_ano,
                columns=pd.MultiIndex.from_product(
                    [TIPOS, STATUS], names=["tipo", "status"]
                ),
            )
        else:
            movimentos_relatorio = movimentos_relatorio.copy()
            movimentos_relatorio["mes"] = movimentos_relatorio["data"].dt.to_period(
                "M"
            )
            resumo_mensal = (
                movimentos_relatorio.groupby(["mes", "tipo", "status"])[
                    "valor_centavos"
                ]
                .sum()
                .unstack(["tipo", "status"], fill_value=0)
                .reindex(
                    index=meses_ano,
                    columns=pd.MultiIndex.from_product(
                        [TIPOS, STATUS], names=["tipo", "status"]
                    ),
                    fill_value=0,
                )
            )
        resumo_mensal.columns = [
            f"{tipo} ({situacao.lower()})"
            for tipo, situacao in resumo_mensal.columns
        ]
        resumo_mensal = resumo_mensal / 100
        resumo_mensal.index = resumo_mensal.index.strftime("%b")
        resumo_mensal.index.name = "Mês"
        st.bar_chart(
            resumo_mensal,
            alt="Receitas e despesas mensais, realizadas e previstas no ano selecionado",
        )

        st.subheader("Resumo por categoria")
        if movimentos_relatorio.empty:
            st.info("Não há movimentações neste ano para as categorias selecionadas.")
        else:
            resumo_anual_categoria = (
                movimentos_relatorio.assign(
                    valor_assinado=movimentos_relatorio.apply(
                        lambda linha: int(linha.valor_centavos)
                        * (1 if linha.tipo == "Receita" else -1),
                        axis=1,
                    )
                )
                .groupby(["categoria", "status"], as_index=False)[
                    "valor_assinado"
                ]
                .sum()
            )
            resumo_anual_categoria["Valor (R$)"] = resumo_anual_categoria[
                "valor_assinado"
            ].map(lambda valor: formatar_moeda(int(valor)))
            resumo_anual_categoria = resumo_anual_categoria.rename(
                columns={"categoria": "Categoria", "status": "Situação"}
            )
            st.dataframe(
                resumo_anual_categoria[
                    ["Categoria", "Situação", "Valor (R$)"]
                ],
                hide_index=True,
                width="stretch",
                alt="Fluxo de caixa anual por categoria e situação",
            )

    else:
        meses_projecao = st.selectbox(
            "Horizonte da projeção",
            options=[3, 6, 12],
            format_func=lambda meses: f"Próximos {meses} meses",
            index=1,
            key="horizonte_projecao",
        )
        inicio_projecao = date.today().replace(day=1)
        periodos_projecao = pd.period_range(
            inicio_projecao,
            periods=meses_projecao,
            freq="M",
        )
        fim_projecao = periodos_projecao[-1].end_time.date()
        movimentos_projecao = movimentacoes_no_periodo(
            df,
            df_recorrencias,
            inicio_projecao,
            fim_projecao,
        )
        linhas_projecao: list[dict[str, int | str]] = []
        saldo_anterior_projecao = saldo_projetado(
            df,
            df_recorrencias,
            saldo_base,
            data_base,
            inicio_projecao - timedelta(days=1),
        )
        for periodo_mes in periodos_projecao:
            inicio_mes = periodo_mes.start_time.date()
            fim_mes = periodo_mes.end_time.date()
            movimentos_mes = movimentos_projecao[
                movimentos_projecao["data"].dt.to_period("M") == periodo_mes
            ]
            receitas_realizadas_mes = int(
                movimentos_mes.loc[
                    (movimentos_mes["tipo"] == "Receita")
                    & (movimentos_mes["status"] == "Realizada"),
                    "valor_centavos",
                ].sum()
            )
            despesas_realizadas_mes = int(
                movimentos_mes.loc[
                    (movimentos_mes["tipo"] == "Despesa")
                    & (movimentos_mes["status"] == "Realizada"),
                    "valor_centavos",
                ].sum()
            )
            receitas_previstas_mes = int(
                movimentos_mes.loc[
                    (movimentos_mes["tipo"] == "Receita")
                    & (movimentos_mes["status"] == "Prevista"),
                    "valor_centavos",
                ].sum()
            )
            despesas_previstas_mes = int(
                movimentos_mes.loc[
                    (movimentos_mes["tipo"] == "Despesa")
                    & (movimentos_mes["status"] == "Prevista"),
                    "valor_centavos",
                ].sum()
            )
            saldo_fim_mes = saldo_projetado(
                df,
                df_recorrencias,
                saldo_base,
                data_base,
                fim_mes,
            )
            linhas_projecao.append(
                {
                    "Mês": periodo_mes.start_time.strftime("%b/%Y"),
                    "Receitas realizadas": receitas_realizadas_mes,
                    "Despesas realizadas": despesas_realizadas_mes,
                    "Receitas previstas": receitas_previstas_mes,
                    "Despesas previstas": despesas_previstas_mes,
                    "Saldo inicial": (
                        saldo_anterior_projecao
                        if saldo_anterior_projecao is not None
                        else 0
                    ),
                    "Saldo final projetado": (
                        saldo_fim_mes if saldo_fim_mes is not None else 0
                    ),
                }
            )
            saldo_anterior_projecao = saldo_fim_mes

        tabela_projecao = pd.DataFrame(linhas_projecao)
        if saldo_projetado(
            df,
            df_recorrencias,
            saldo_base,
            data_base,
            inicio_projecao - timedelta(days=1),
        ) is None:
            st.warning(
                f"A data-base do saldo inicial ({data_base:%d/%m/%Y}) é posterior "
                "ao início da projeção. Atualize o saldo inicial para calcular "
                "os saldos finais projetados corretamente."
            )
        st.caption(
            "A projeção considera todas as categorias, o saldo inicial, "
            "lançamentos previstos e regras recorrentes."
        )
        st.subheader("Saldo projetado por mês")
        st.line_chart(
            tabela_projecao.set_index("Mês")[["Saldo final projetado"]] / 100,
            alt="Evolução do saldo de caixa projetado nos próximos meses",
        )

        colunas_metricas_projecao = st.columns(3)
        total_receitas_previstas = int(
            tabela_projecao["Receitas previstas"].sum()
        )
        total_despesas_previstas = int(
            tabela_projecao["Despesas previstas"].sum()
        )
        colunas_metricas_projecao[0].metric(
            "Entradas previstas",
            formatar_moeda(total_receitas_previstas),
            border=True,
        )
        colunas_metricas_projecao[1].metric(
            "Saídas previstas",
            formatar_moeda(total_despesas_previstas),
            border=True,
        )
        colunas_metricas_projecao[2].metric(
            "Saldo no fim do horizonte",
            formatar_moeda(
                int(tabela_projecao["Saldo final projetado"].iloc[-1])
            ),
            border=True,
        )
        tabela_projecao_exibicao = tabela_projecao.copy()
        for coluna_valor in tabela_projecao_exibicao.columns[1:]:
            tabela_projecao_exibicao[coluna_valor] = tabela_projecao_exibicao[
                coluna_valor
            ].map(lambda valor: formatar_moeda(int(valor)))
        st.dataframe(
            tabela_projecao_exibicao,
            hide_index=True,
            width="stretch",
            alt="Fluxo mensal previsto e saldos projetados",
        )

    if tipo_relatorio == "Projeções":
        dados_exportacao = tabela_projecao_exibicao.to_csv(index=False).encode(
            "utf-8-sig"
        )
        nome_exportacao = "projecao_fluxo_de_caixa.csv"
    else:
        dados_exportacao = movimentos_relatorio[
            ["data", "descricao", "categoria", "valor_centavos", "tipo", "status"]
        ].copy()
        if not dados_exportacao.empty:
            dados_exportacao["data"] = dados_exportacao["data"].dt.strftime(
                "%d/%m/%Y"
            )
            dados_exportacao["valor_centavos"] = dados_exportacao[
                "valor_centavos"
            ].map(lambda valor: f"{valor / 100:.2f}")
        dados_exportacao = dados_exportacao.rename(
            columns={
                "data": "Data",
                "descricao": "Descrição",
                "categoria": "Categoria",
                "valor_centavos": "Valor (R$)",
                "tipo": "Tipo",
                "status": "Situação",
            }
        ).to_csv(index=False).encode("utf-8-sig")
        nome_exportacao = (
            f"relatorio_{tipo_relatorio.lower()}_{ano_relatorio}_{numero_mes:02d}.csv"
            if tipo_relatorio == "Mensal"
            else f"relatorio_anual_{ano_relatorio}.csv"
        )
    st.download_button(
        "Exportar relatório CSV",
        data=dados_exportacao,
        file_name=nome_exportacao,
        mime="text/csv",
        icon=":material/download:",
    )
    st.caption(
        "Os relatórios se baseiam nas categorias selecionadas na barra lateral. "
        "As projeções consideram todas as categorias."
    )

with aba_visao:
    if isinstance(periodo, (tuple, list)) and len(periodo) == 2:
        data_inicio, data_fim = periodo
    else:
        data_inicio = data_fim = periodo

    if data_inicio > data_fim:
        data_inicio, data_fim = data_fim, data_inicio

    df_periodo = df[
        df["data"].notna()
        & (df["data"].dt.date >= data_inicio)
        & (df["data"].dt.date <= data_fim)
        & df["categoria"].isin(categorias_selecionadas)
    ].copy()
    ocorrencias_periodo = montar_ocorrencias(
        df_recorrencias,
        data_inicio,
        data_fim,
        max(data_inicio, date.today()),
        df,
    )
    ocorrencias_periodo = ocorrencias_periodo[
        ocorrencias_periodo["categoria"].isin(categorias_selecionadas)
    ]

    realizadas = df_periodo[df_periodo["status"] == "Realizada"]
    receitas_realizadas = int(
        realizadas.loc[realizadas["tipo"] == "Receita", "valor_centavos"].sum()
    )
    despesas_realizadas = int(
        realizadas.loc[realizadas["tipo"] == "Despesa", "valor_centavos"].sum()
    )
    saldo_realizado = receitas_realizadas - despesas_realizadas

    inicio_anterior, fim_anterior = proximo_periodo(data_inicio, data_fim)
    anteriores = df[
        (df["data"].dt.date >= inicio_anterior)
        & (df["data"].dt.date <= fim_anterior)
        & (df["status"] == "Realizada")
        & df["categoria"].isin(categorias_selecionadas)
    ]
    receitas_anteriores = int(
        anteriores.loc[anteriores["tipo"] == "Receita", "valor_centavos"].sum()
    )
    despesas_anteriores = int(
        anteriores.loc[anteriores["tipo"] == "Despesa", "valor_centavos"].sum()
    )
    saldo_anterior = receitas_anteriores - despesas_anteriores
    saldo_fim_projetado = saldo_projetado(
        df, df_recorrencias, saldo_base, data_base, data_fim
    )

    st.title("Visão geral", icon=":material/analytics:")
    st.caption(
        "Acompanhe o realizado, as previsões e a evolução do caixa. "
        "O saldo projetado considera todas as categorias."
    )

    metricas = st.columns(5)
    metricas[0].metric(
        "Receitas realizadas",
        formatar_moeda(receitas_realizadas),
        formatar_variacao(receitas_realizadas, receitas_anteriores),
        delta_description=(
            "vs. período anterior"
            if receitas_anteriores
            else "sem base no período anterior"
        ),
        border=True,
    )
    metricas[1].metric(
        "Despesas realizadas",
        formatar_moeda(despesas_realizadas),
        formatar_variacao(despesas_realizadas, despesas_anteriores),
        delta_description=(
            "vs. período anterior"
            if despesas_anteriores
            else "sem base no período anterior"
        ),
        delta_color="inverse",
        border=True,
    )
    metricas[2].metric(
        "Saldo realizado",
        formatar_moeda(saldo_realizado),
        formatar_variacao(saldo_realizado, saldo_anterior),
        delta_description=(
            "vs. período anterior" if saldo_anterior else "sem base no período anterior"
        ),
        border=True,
    )
    metricas[3].metric(
        "Saldo projetado no fim",
        formatar_moeda(saldo_fim_projetado) if saldo_fim_projetado is not None else "Sem base",
        border=True,
    )
    metricas[4].metric(
        "Lançamentos registrados",
        f"{len(df_periodo):,}".replace(",", "."),
        border=True,
    )
    if saldo_fim_projetado is None:
        st.caption(
            f"Saldo projetado indisponível: o período termina antes da data-base "
            f"({data_base:%d/%m/%Y}). Ajuste o saldo inicial para consultar períodos anteriores."
        )
    else:
        st.caption(
            f"Projeção baseada no saldo de {formatar_moeda(saldo_base)} em "
            f"{data_base:%d/%m/%Y}, considerando todas as categorias até "
            f"{data_fim:%d/%m/%Y}."
        )
        if receitas_anteriores == 0 or despesas_anteriores == 0:
            st.caption(
                "A comparação usa transações realizadas no período anterior de mesma duração."
            )

    movimentos_grafico = pd.concat(
        [
            df_periodo[["data", "categoria", "valor_centavos", "valor", "tipo", "status"]],
            ocorrencias_periodo[["data", "categoria", "valor_centavos", "valor", "tipo", "status"]],
        ],
        ignore_index=True,
    )
    movimentos_grafico["data"] = pd.to_datetime(movimentos_grafico["data"], errors="raise")

    st.space("small")
    graf_coluna, categoria_coluna = st.columns(2)
    with graf_coluna:
        with st.container(border=True):
            st.subheader("Receitas e despesas", anchor=False)
            if movimentos_grafico.empty:
                st.caption("Não há movimentações para o período e as categorias selecionadas.")
            else:
                chave_frequencia = {"Dia": "D", "Semana": "W-SUN", "Mês": "MS"}[
                    granularidade
                ]
                inicio_grafico = pd.Timestamp(data_inicio)
                fim_grafico = pd.Timestamp(data_fim)
                if granularidade in {"Semana", "Mês"}:
                    datas_grafico = pd.period_range(
                        inicio_grafico,
                        fim_grafico,
                        freq={"Semana": "W-SUN", "Mês": "M"}[granularidade],
                    ).start_time
                else:
                    datas_grafico = pd.date_range(
                        inicio_grafico, fim_grafico, freq=chave_frequencia
                    )
                agrupado = (
                    movimentos_grafico.assign(
                        periodo=movimentos_grafico["data"].dt.to_period(
                            {"Dia": "D", "Semana": "W-SUN", "Mês": "M"}[granularidade]
                        )
                    )
                    .groupby(["periodo", "tipo", "status"])["valor_centavos"]
                    .sum()
                    .unstack(["tipo", "status"], fill_value=0)
                )
                periodos = datas_grafico.to_period(
                    {"Dia": "D", "Semana": "W-SUN", "Mês": "M"}[granularidade]
                )
                colunas_grafico = pd.MultiIndex.from_product(
                    [TIPOS, STATUS], names=["tipo", "status"]
                )
                agrupado = agrupado.reindex(index=periodos, columns=colunas_grafico, fill_value=0)
                agrupado.columns = [
                    f"{tipo} ({situacao.lower()})" for tipo, situacao in agrupado.columns
                ]
                agrupado = agrupado / 100
                rotulos = {
                    "Dia": "%d/%m",
                    "Semana": "sem. %d/%m",
                    "Mês": "%m/%Y",
                }
                agrupado.index = datas_grafico.strftime(rotulos[granularidade])
                agrupado.index.name = granularidade
                st.bar_chart(
                    agrupado.reset_index(),
                    x=granularidade,
                    y=list(agrupado.columns),
                    stack=False,
                    alt="Receitas e despesas realizadas e previstas no período",
                )

    with categoria_coluna:
        with st.container(border=True):
            st.subheader("Despesas por categoria", anchor=False)
            st.caption("Inclui movimentações previstas e ocorrências recorrentes.")
            despesas_categoria = (
                movimentos_grafico[movimentos_grafico["tipo"] == "Despesa"]
                .groupby("categoria", as_index=False)["valor_centavos"]
                .sum()
                .sort_values("valor_centavos", ascending=False)
            )
            if despesas_categoria.empty:
                st.caption("Nenhuma despesa no período selecionado.")
            else:
                despesas_categoria["valor"] = despesas_categoria["valor_centavos"] / 100
                st.bar_chart(
                    despesas_categoria,
                    x="categoria",
                    y="valor",
                    horizontal=True,
                    alt="Despesas realizadas e previstas agrupadas por categoria",
                )

with aba_movimentacoes:
    st.subheader("Movimentações", icon=":material/receipt_long:")
    filtros_coluna, exportar_coluna, backup_coluna = st.columns(
        [2, 1, 1], vertical_alignment="bottom"
    )
    with filtros_coluna:
        tipos_selecionados = st.pills(
            "Tipos exibidos",
            options=TIPOS,
            default=TIPOS,
            selection_mode="multi",
        )

    df_tabela = df_periodo[
        df_periodo["tipo"].isin(tipos_selecionados or [])
    ].copy()
    df_tabela = df_tabela.sort_values(["data", "id"], ascending=[False, False])
    df_exportacao = pd.concat(
        [
            df_tabela[
                ["data", "descricao", "categoria", "valor_centavos", "tipo", "status"]
            ],
            ocorrencias_periodo[
                ocorrencias_periodo["tipo"].isin(tipos_selecionados or [])
            ][["data", "descricao", "categoria", "valor_centavos", "tipo", "status"]],
        ],
        ignore_index=True,
    ).sort_values("data", ascending=False).rename(
        columns={
            "data": "Data",
            "descricao": "Descrição",
            "categoria": "Categoria",
            "valor_centavos": "Valor (R$)",
            "tipo": "Tipo",
            "status": "Situação",
        }
    )
    if not df_exportacao.empty:
        df_exportacao["Data"] = pd.to_datetime(df_exportacao["Data"]).dt.strftime("%d/%m/%Y")
        df_exportacao["Valor (R$)"] = df_exportacao["Valor (R$)"] / 100

    with exportar_coluna:
        st.download_button(
            "Exportar período",
            data=df_exportacao.to_csv(index=False).encode("utf-8-sig"),
            file_name="movimentacoes_fluxo_de_caixa.csv",
            mime="text/csv",
            icon=":material/download:",
            width="stretch",
            disabled=df_exportacao.empty,
        )
    with backup_coluna:
        st.download_button(
            "Backup completo",
            data=criar_backup(),
            file_name=f"backup_fluxo_de_caixa_{date.today():%Y%m%d}.json",
            mime="application/json",
            icon=":material/backup:",
            help="Inclui lançamentos, recorrências, categorias e saldo inicial.",
            width="stretch",
        )

    if df_tabela.empty:
        st.info("Nenhuma movimentação encontrada com os filtros atuais.")
    else:
        df_exibicao = df_tabela[
            ["id", "data", "descricao", "categoria", "valor_centavos", "tipo", "status"]
        ].copy()
        df_exibicao["data"] = df_exibicao["data"].dt.strftime("%d/%m/%Y")
        df_exibicao["valor_centavos"] = df_exibicao["valor_centavos"].map(
            lambda valor_centavos: formatar_moeda(int(valor_centavos))
        )
        st.dataframe(
            df_exibicao,
            width="stretch",
            hide_index=True,
            column_config={
                "id": st.column_config.NumberColumn("ID", help="Identificador da transação"),
                "data": "Data",
                "descricao": "Descrição",
                "categoria": "Categoria",
                "valor_centavos": "Valor",
                "tipo": "Tipo",
                "status": "Situação",
            },
            alt="Movimentações financeiras no período selecionado",
        )

    if not ocorrencias_periodo.empty:
        with st.expander("Previsões das recorrências", icon=":material/event_repeat:"):
            exibicao_recorrencias = ocorrencias_periodo[
                ["data", "descricao", "categoria", "valor_centavos", "tipo"]
            ].copy()
            exibicao_recorrencias["data"] = exibicao_recorrencias["data"].dt.strftime(
                "%d/%m/%Y"
            )
            exibicao_recorrencias["valor_centavos"] = exibicao_recorrencias[
                "valor_centavos"
            ].map(lambda valor_centavos: formatar_moeda(int(valor_centavos)))
            exibicao_recorrencias = exibicao_recorrencias.rename(
                columns={
                    "data": "Data",
                    "descricao": "Descrição",
                    "categoria": "Categoria",
                    "valor_centavos": "Valor",
                    "tipo": "Tipo",
                }
            )
            st.dataframe(
                exibicao_recorrencias,
                hide_index=True,
                width="stretch",
                alt="Próximas ocorrências das regras recorrentes",
            )

    if not df.empty:
        with st.expander("Editar ou excluir lançamentos", icon=":material/edit:"):
            transacoes_por_id = {
                int(linha.id): linha for linha in df.itertuples(index=False)
            }
            id_edicao = st.selectbox(
                "Lançamento",
                options=list(transacoes_por_id),
                format_func=lambda identificador: (
                    f"#{identificador} · "
                    f"{transacoes_por_id[identificador].data:%d/%m/%Y} · "
                    f"{transacoes_por_id[identificador].descricao} · "
                    f"{formatar_moeda(int(transacoes_por_id[identificador].valor_centavos))}"
                ),
            )
            transacao_edicao = transacoes_por_id[id_edicao]
            with st.form(f"form_editar_transacao_{id_edicao}"):
                data_edicao = st.date_input(
                    "Data do lançamento",
                    value=transacao_edicao.data.date(),
                    key=f"data_edicao_{id_edicao}",
                )
                descricao_edicao = st.text_input(
                    "Descrição do lançamento",
                    value=transacao_edicao.descricao,
                    key=f"descricao_edicao_{id_edicao}",
                )
                categoria_edicao = st.selectbox(
                    "Categoria do lançamento",
                    categorias_disponiveis,
                    index=categorias_disponiveis.index(transacao_edicao.categoria),
                    key=f"categoria_edicao_{id_edicao}",
                )
                valor_edicao = st.number_input(
                    "Valor do lançamento (R$)",
                    min_value=0.0,
                    value=float(Decimal(int(transacao_edicao.valor_centavos)) / 100),
                    step=10.0,
                    format="%.2f",
                    key=f"valor_edicao_{id_edicao}",
                )
                tipo_edicao = st.selectbox(
                    "Tipo do lançamento",
                    TIPOS,
                    index=TIPOS.index(transacao_edicao.tipo),
                    key=f"tipo_edicao_{id_edicao}",
                )
                status_edicao = st.selectbox(
                    "Situação do lançamento",
                    STATUS,
                    index=STATUS.index(transacao_edicao.status),
                    key=f"status_edicao_{id_edicao}",
                )
                salvar_edicao = st.form_submit_button("Salvar alterações")
            if salvar_edicao:
                descricao_limpa = descricao_edicao.strip()
                valor_edicao_em_centavos = centavos_de(valor_edicao)
                if not descricao_limpa or valor_edicao_em_centavos <= 0:
                    st.error("Informe uma descrição e um valor de pelo menos R$ 0,01.")
                elif status_edicao == "Realizada" and data_edicao > date.today():
                    st.error("Uma transação futura precisa ser marcada como prevista.")
                else:
                    salvar_transacao(
                        data_edicao,
                        descricao_limpa,
                        categoria_edicao,
                        valor_edicao_em_centavos,
                        tipo_edicao,
                        status_edicao,
                        id_edicao,
                    )
                    invalidar_dados()
                    st.toast("Lançamento atualizado.", icon=":material/check:")
                    st.rerun()

            confirmar_exclusao = st.checkbox(
                f"Confirmo a exclusão do lançamento #{id_edicao}"
            )
            if st.button(
                "Excluir lançamento",
                disabled=not confirmar_exclusao,
                icon=":material/delete:",
            ):
                removidas = deletar_transacao(id_edicao)
                if removidas:
                    invalidar_dados()
                    st.toast("Lançamento excluído.", icon=":material/check:")
                    st.rerun()
                else:
                    st.error("O lançamento selecionado não foi encontrado.")

    st.caption("Valores apresentados em reais (BRL).")
