"""Retorno do agricultor e ponte com o técnico de ATER (assistência técnica e extensão rural).

O semáforo da v2 só falava: o agricultor não tinha como dizer "já fiz". Este
módulo guarda o RETORNO dele e prepara o resumo para o técnico:

    * ``registro_acoes``: cada toque em "Feito" (ação, causa, leitura, instante);
      um "Feito" recente SILENCIA a repetição do Vermelho daquela causa por
      60 minutos (``causa_silenciada``), na página e no alarme da estação base.
      A repetição sem fim de um alerta já atendido gasta a confiança no alarme
      (efeito "lobo"): https://econpapers.repec.org/RePEc:wly:riskan:v:35:y:2015:i:3:p:385-395
      Conselho digital com retorno do agricultor aumentou produtividade e adoção
      (Fabregas, Kremer & Schilbach, Science 2019; resumo em
      https://precisiondev.org/customized-digital-advice-can-help-farmers-reduce-crop-loss-and-manage-weather-shocks-a-summary-or-as-much-as-we-can-summarize/).
    * ``eventos_interface``: o que acontece na tela (ouviu, tocou num
      pictograma, pediu ajuda, ficou sem voz, sem rede), com retenção de 90 dias.
    * ``pedido_ajuda``: o toque em "Chamar o técnico", com a foto do momento
      (cor, causa, temperatura e água na terra). Só 18,2 % dos estabelecimentos
      da agricultura familiar recebem assistência técnica (Censo Agro 2017):
      https://www.ibge.gov.br/apps/atlasrural/pdfs/05_00_Texto.pdf
    * ``montar_resumo_tecnico``: o resumo de N dias da página ``/tecnico``
      (horas por cor e por causa, avisos antecipados confirmados ou não, ações
      feitas, problemas de sensor, pedidos de ajuda, estado do prognóstico e da
      calibração) e o texto pronto para o WhatsApp (≤ 700 caracteres, sem dado
      pessoal além do nome da propriedade). Ferramenta separada para o
      agricultor e para o consultor, como recomendam Rose et al. (2016):
      https://econpapers.repec.org/RePEc:eee:agisys:v:149:y:2016:i:c:p:165-174

Domínio do retorno (v4):
    * ``registrar_acao_sem_repetir``: o mesmo "Feito" (mesma ação, causa e
      leitura) tocado de novo em menos de ``JANELA_REPETICAO_SEGUNDOS`` não vira
      outro registro (toque duplo, celular compartilhado, reenvio sem rede);
    * ``desfazer_acao``: o "Feito" tocado sem querer pode ser desfeito por
      ``JANELA_DESFAZER_SEGUNDOS``; apagar o registro devolve o alerta (a pausa
      de ``causa_silenciada`` é calculada a partir dos registros);
    * pedidos de ajuda e eventos gravados durante a DEMONSTRAÇÃO levam
      ``demonstracao = 1`` e ficam fora do resumo do técnico.

As tabelas são criadas aqui (``CREATE TABLE IF NOT EXISTS``); a coluna
``demonstracao`` de bancos antigos é acrescentada por
``database.migrar_colunas_demonstracao``. Tudo funciona offline: o link ``https://wa.me/...``
só é aberto pelo WhatsApp do próprio celular.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import statistics
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

from acoes_orientacao import (
    ACOES_VALIDAS,
    CAUSAS_VALIDAS,
    DESCRICOES_ACAO,
    LEGENDAS_CAUSA,
    derivar_causa,
)
from database import (
    CAMINHO_BANCO,
    COLUNA_DEMONSTRACAO,
    CORES_STATUS,
    CULTURA_PADRAO,
    MODO_CAMPO_USB,
    MODO_DEMONSTRACAO,
    MODO_SIMULACAO,
    buscar_parametros_cultura,
    migrar_colunas_demonstracao,
    obter_conexao,
    obter_configuracao,
)

registrador: logging.Logger = logging.getLogger("agroanalise.retorno")

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
TIPOS_EVENTO: Tuple[str, ...] = (
    "ouviu", "tocou_pictograma", "feito", "ajuda", "sem_voz", "rede_caida", "chamou_tecnico",
)
DIAS_RETENCAO_EVENTOS: int = 90
MINUTOS_SILENCIO_FEITO: int = 60
# v4: o "Feito" pode ser desfeito até 2 min depois (a tela mostra "Desfazer" por 15 s;
# a folga cobre a rede fraca), e o mesmo "Feito" repetido em menos de 1 min não duplica.
JANELA_DESFAZER_SEGUNDOS: int = 120
JANELA_REPETICAO_SEGUNDOS: int = 60
# Tolerância de relógio entre o celular e o computador ao desfazer um toque "do futuro".
FOLGA_RELOGIO_SEGUNDOS: int = 120
TAMANHO_MAXIMO_TEXTO_WHATSAPP: int = 700
TAMANHO_MAXIMO_DETALHE: int = 200
TAMANHO_MAXIMO_ORIGEM: int = 64
# Avisos antecipados: leituras com aviso a menos de 60 min uma da outra formam um só aviso;
# o aviso é "confirmado" se vier Vermelho até 12 h depois (horizonte do prognóstico).
MINUTOS_ENTRE_AVISOS: int = 60
HORIZONTE_CONFIRMACAO_HORAS: int = 12
DIAS_RESUMO_PADRAO: int = 7
DIAS_RESUMO_MAXIMO: int = 90
LIMITE_LISTAS_RESUMO: int = 20
# Telefone no formato internacional E.164 (até 15 dígitos), como pede o "click to chat" do WhatsApp.
DIGITOS_MINIMOS_TELEFONE: int = 10
DIGITOS_MAXIMOS_TELEFONE: int = 15
CODIGO_PAIS_PADRAO: str = "55"  # Brasil: número só com DDD (10–11 dígitos) ganha o 55 na frente.
URL_WHATSAPP: str = "https://wa.me/"

# Passado de cada ação, para a confirmação falada "Anotado: você regou às 14 horas."
ACOES_NO_PASSADO: Dict[str, str] = {
    "REGAR_AGORA": "regou",
    "REGAR_FIM_DA_TARDE": "regou",
    "REGAR_MANHA": "regou",
    "NAO_REGAR": "parou de regar",
    "ABRIR_VALETAS": "abriu as valetas",
    "FAZER_SOMBRA": "fez sombra",
    "COBRIR_MUDAS": "cobriu as mudas",
    "CHEGAR_TERRA": "chegou terra no pé",
    "NADA_A_FAZER": "olhou a lavoura",
    "VERIFICAR_APARELHO": "conferiu o aparelho",
}
NOMES_COR_TEXTO: Dict[int, str] = {0: "Verde", 1: "Amarelo", 2: "Vermelho"}
DESCRICAO_CAUSA_CURTA: Dict[str, str] = {
    "TERRA_SECA": "terra seca",
    "ENCHARCADO": "terra encharcada",
    "FRIO_GEADA": "frio ou geada",
    "CALOR": "calor",
    "APARELHO": "problema no aparelho",
    "NENHUMA": "sem problema",
}

class RegistroInexistente(LookupError):
    """O "Feito" que se quer desfazer não existe (id errado ou já desfeito)."""


class JanelaDesfazerExpirada(ValueError):
    """O "Feito" é mais velho que a janela de desfazer (``JANELA_DESFAZER_SEGUNDOS``)."""


_DDL_RETORNO: str = """
CREATE TABLE IF NOT EXISTS registro_acoes (
    id_acao         INTEGER PRIMARY KEY AUTOINCREMENT,
    id_leitura      INTEGER REFERENCES historico_leituras(id_leitura) ON DELETE SET NULL,
    codigo_acao     TEXT    NOT NULL,
    causa           TEXT    NOT NULL,
    instante_utc    TEXT    NOT NULL,
    origem_aparelho TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_registro_acoes_causa_instante ON registro_acoes(causa, instante_utc);

CREATE TABLE IF NOT EXISTS eventos_interface (
    id_evento    INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo         TEXT    NOT NULL
                 CHECK (tipo IN ('ouviu','tocou_pictograma','feito','ajuda','sem_voz','rede_caida','chamou_tecnico')),
    instante_utc TEXT    NOT NULL,
    detalhe      TEXT    NOT NULL DEFAULT '',
    demonstracao INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_eventos_interface_instante ON eventos_interface(instante_utc);

CREATE TABLE IF NOT EXISTS pedido_ajuda (
    id_pedido      INTEGER PRIMARY KEY AUTOINCREMENT,
    instante_utc   TEXT    NOT NULL,
    status         INTEGER CHECK (status IS NULL OR status IN (0, 1, 2)),
    causa          TEXT    NOT NULL DEFAULT '',
    temperatura_ar REAL,
    umidade_solo   REAL,
    id_leitura     INTEGER REFERENCES historico_leituras(id_leitura) ON DELETE SET NULL,
    demonstracao   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pedido_ajuda_instante ON pedido_ajuda(instante_utc);
"""

_TABELAS_GARANTIDAS: Set[Tuple[str, int, int]] = set()
_TRAVA_TABELAS: threading.Lock = threading.Lock()
# Serializa "procura o Feito igual + grava" (o servidor atende em threads).
_TRAVA_REGISTRO: threading.Lock = threading.Lock()


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def instante_iso_utc(instante: datetime) -> str:
    """Instante em ISO UTC com ``+00:00`` e segundos (mesmo formato de ``historico_leituras.instante_utc``).

    Um ``datetime`` sem fuso é tratado como hora local.
    """
    return instante.astimezone(timezone.utc).isoformat(timespec="seconds")


def _interpretar_iso(texto: Any) -> Optional[datetime]:
    """Converte ISO (com ou sem fuso) em ``datetime`` UTC *aware*; ``None`` se inválido."""
    conteudo = str(texto or "").strip()
    if not conteudo:
        return None
    try:
        instante = datetime.fromisoformat(conteudo)
    except ValueError:
        return None
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=timezone.utc)
    return instante.astimezone(timezone.utc)


def _chave_banco(caminho_banco: Path) -> Tuple[str, int, int]:
    """Identidade do arquivo do banco (caminho, dispositivo, inode) para lembrar as tabelas já criadas."""
    caminho = Path(caminho_banco).resolve()
    try:
        info = caminho.stat()
        return str(caminho), int(info.st_dev), int(info.st_ino)
    except OSError:
        return str(caminho), -1, -1


def _tabela_existe(conexao: sqlite3.Connection, nome: str) -> bool:
    """``True`` se a tabela existir no banco."""
    linha = conexao.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (nome,)).fetchone()
    return linha is not None


def _colunas(conexao: sqlite3.Connection, tabela: str) -> Set[str]:
    """Nomes das colunas da tabela (conjunto vazio se ela não existir)."""
    return {str(linha[1]) for linha in conexao.execute(f"PRAGMA table_info({tabela})")}


def garantir_tabelas(caminho_banco: Path = CAMINHO_BANCO) -> None:
    """Cria ``registro_acoes``, ``eventos_interface`` e ``pedido_ajuda`` se faltarem (idempotente).

    A criação é lembrada por arquivo (caminho + inode): chamadas seguidas não
    repetem o DDL.

    Raises:
        sqlite3.Error: banco inacessível.
    """
    chave = _chave_banco(caminho_banco)
    with _TRAVA_TABELAS:
        if chave in _TABELAS_GARANTIDAS:
            return
    conexao = obter_conexao(caminho_banco)
    try:
        conexao.executescript(_DDL_RETORNO)
        conexao.commit()
        migrar_colunas_demonstracao(conexao)
    finally:
        conexao.close()
    with _TRAVA_TABELAS:
        _TABELAS_GARANTIDAS.add(_chave_banco(caminho_banco))


def _leitura_existe(conexao: sqlite3.Connection, id_leitura: Optional[int]) -> bool:
    """``True`` se ``id_leitura`` existir em ``historico_leituras``."""
    if id_leitura is None:
        return False
    linha = conexao.execute("SELECT 1 FROM historico_leituras WHERE id_leitura = ?", (int(id_leitura),)).fetchone()
    return linha is not None


def _modo_demonstracao_ativo(conexao: sqlite3.Connection) -> bool:
    """``True`` se a configuração ``modo_operacao`` do banco for ``DEMONSTRACAO`` (lida na mesma conexão)."""
    if not _tabela_existe(conexao, "configuracao_sistema"):
        return False
    linha = conexao.execute("SELECT valor FROM configuracao_sistema WHERE chave = 'modo_operacao'").fetchone()
    return linha is not None and str(linha[0] or "").strip().upper() == MODO_DEMONSTRACAO


def _leitura_de_demonstracao(conexao: sqlite3.Connection, id_leitura: Optional[int]) -> bool:
    """``True`` se a leitura existir e tiver sido gravada pela banca (``origem = DEMONSTRACAO``)."""
    if id_leitura is None:
        return False
    linha = conexao.execute("SELECT origem FROM historico_leituras WHERE id_leitura = ?", (int(id_leitura),)).fetchone()
    return linha is not None and str(linha[0] or "") == MODO_DEMONSTRACAO


def _filtro_demonstracao(conexao: sqlite3.Connection, tabela: str, incluir_demonstracao: bool,
                         prefixo: str = "") -> str:
    """Trecho SQL `` AND demonstracao = 0`` (vazio se incluir a demonstração ou se a coluna não existir)."""
    if incluir_demonstracao or COLUNA_DEMONSTRACAO not in _colunas(conexao, tabela):
        return ""
    return f" AND {prefixo}{COLUNA_DEMONSTRACAO} = 0"


def falar_hora(instante_local: datetime) -> str:
    """Hora cheia falada: "à 1 hora", "às 14 horas", "à meia-noite", "ao meio-dia"."""
    hora = instante_local.hour
    if hora == 0:
        return "à meia-noite"
    if hora == 12:
        return "ao meio-dia"
    if hora == 1:
        return "à 1 hora"
    return f"às {hora} horas"


def frase_confirmacao_acao(codigo_acao: str, instante_utc: datetime) -> str:
    """Confirmação falada do "Feito": "Anotado: você regou às 14 horas."."""
    passado = ACOES_NO_PASSADO.get(codigo_acao, "fez o que foi pedido")
    return f"Anotado: você {passado} {falar_hora(instante_utc.astimezone())}."


# ---------------------------------------------------------------------------
# Registro de ações ("Feito")
# ---------------------------------------------------------------------------
def registrar_acao(
    caminho_banco: Path,
    codigo_acao: str,
    causa: str,
    id_leitura: Optional[int],
    origem_aparelho: str,
    instante_utc: Optional[datetime] = None,
) -> int:
    """Grava um toque em "Feito" e devolve o ``id_acao``.

    Args:
        caminho_banco: banco SQLite.
        codigo_acao: código de ``ACOES_VALIDAS``.
        causa: código de ``CAUSAS_VALIDAS``.
        id_leitura: leitura exibida quando o agricultor tocou (opcional; se não
            existir mais, é gravado ``NULL``).
        origem_aparelho: identificação curta do aparelho (endereço na rede local),
            cortada em 64 caracteres.
        instante_utc: instante do toque (padrão: agora). Um toque guardado sem
            rede e reenviado depois traz o instante original.

    Raises:
        ValueError: ação ou causa fora do catálogo.
        sqlite3.Error: falha do banco.
    """
    acao, codigo_causa = _normalizar_acao_e_causa(codigo_acao, causa)
    instante = instante_iso_utc(instante_utc or datetime.now(timezone.utc))
    garantir_tabelas(caminho_banco)
    conexao = obter_conexao(caminho_banco)
    try:
        leitura = int(id_leitura) if id_leitura is not None and _leitura_existe(conexao, id_leitura) else None
        cursor = conexao.execute(
            "INSERT INTO registro_acoes (id_leitura, codigo_acao, causa, instante_utc, origem_aparelho) "
            "VALUES (?, ?, ?, ?, ?)",
            (leitura, acao, codigo_causa, instante, str(origem_aparelho or "")[:TAMANHO_MAXIMO_ORIGEM]),
        )
        # Integração v4 (meta 11: nada da demonstração no resumo do técnico): o evento "feito" de um
        # toque sobre leitura da banca (ou sem leitura, com a demonstração ligada) leva a marca de
        # demonstração, como o próprio "Feito" já fica fora de ``acoes_feitas`` pela origem da leitura.
        demonstracao = _leitura_de_demonstracao(conexao, leitura) or (leitura is None
                                                                      and _modo_demonstracao_ativo(conexao))
        conexao.execute("INSERT INTO eventos_interface (tipo, instante_utc, detalhe, demonstracao) "
                        "VALUES ('feito', ?, ?, ?)", (instante, f"{acao}|{codigo_causa}", int(demonstracao)))
        conexao.commit()
        return int(cursor.lastrowid or 0)
    finally:
        conexao.close()


def _normalizar_acao_e_causa(codigo_acao: str, causa: str) -> Tuple[str, str]:
    """Códigos em maiúsculas, validados contra ``ACOES_VALIDAS`` e ``CAUSAS_VALIDAS``.

    Raises:
        ValueError: ação ou causa fora do catálogo.
    """
    acao = str(codigo_acao or "").strip().upper()
    codigo_causa = str(causa or "").strip().upper()
    if acao not in ACOES_VALIDAS:
        raise ValueError(f"Ação desconhecida: {codigo_acao!r}.")
    if codigo_causa not in CAUSAS_VALIDAS:
        raise ValueError(f"Causa desconhecida: {causa!r}.")
    return acao, codigo_causa


def registrar_acao_sem_repetir(
    caminho_banco: Path,
    codigo_acao: str,
    causa: str,
    id_leitura: Optional[int],
    origem_aparelho: str,
    instante_utc: Optional[datetime] = None,
    janela_segundos: int = JANELA_REPETICAO_SEGUNDOS,
) -> Tuple[int, bool]:
    """Grava o "Feito" só se o mesmo toque não foi gravado há menos de ``janela_segundos``.

    "Mesmo toque" = mesma causa, mesmo ``id_leitura`` (a leitura que ainda não
    existe ou já saiu do banco vale ``NULL``, como em ``registrar_acao``) e
    mesmo ``codigo_acao``. A janela conta a partir do registro gravado: três
    toques em 50 s dão um registro; um toque 61 s depois do primeiro dá outro.

    Args:
        caminho_banco: banco SQLite.
        codigo_acao: código de ``ACOES_VALIDAS``.
        causa: código de ``CAUSAS_VALIDAS``.
        id_leitura: leitura exibida quando o agricultor tocou (opcional).
        origem_aparelho: identificação curta do aparelho.
        instante_utc: instante do toque (padrão: agora).
        janela_segundos: janela de repetição (padrão ``JANELA_REPETICAO_SEGUNDOS``).

    Returns:
        ``(id_acao, repetido)``: o id do registro existente e ``True`` quando o
        toque é repetição; o id do novo registro e ``False`` quando gravou.

    Raises:
        ValueError: ação ou causa fora do catálogo.
        sqlite3.Error: falha do banco.
    """
    acao, codigo_causa = _normalizar_acao_e_causa(codigo_acao, causa)
    instante = (instante_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    janela = max(0, int(janela_segundos))
    garantir_tabelas(caminho_banco)
    with _TRAVA_REGISTRO:
        conexao = obter_conexao(caminho_banco)
        try:
            leitura = int(id_leitura) if id_leitura is not None and _leitura_existe(conexao, id_leitura) else None
            linhas = conexao.execute(
                "SELECT id_acao, instante_utc FROM registro_acoes "
                "WHERE causa = ? AND codigo_acao = ? AND id_leitura IS ? AND instante_utc >= ? AND instante_utc <= ? "
                "ORDER BY instante_utc DESC, id_acao DESC",
                (codigo_causa, acao, leitura, instante_iso_utc(instante - timedelta(seconds=janela)),
                 instante_iso_utc(instante + timedelta(seconds=janela))),
            ).fetchall()
        finally:
            conexao.close()
        for id_existente, texto_instante in linhas:
            gravado = _interpretar_iso(texto_instante)
            if gravado is not None and abs((instante - gravado).total_seconds()) < janela:
                return int(id_existente), True
        return registrar_acao(caminho_banco, acao, codigo_causa, id_leitura, origem_aparelho, instante), False


def desfazer_acao(caminho_banco: Path, id_acao: int, agora_utc: datetime,
                  janela_segundos: int = JANELA_DESFAZER_SEGUNDOS) -> None:
    """Desfaz um "Feito": apaga a linha de ``registro_acoes`` (e o evento ``feito`` que ela gerou).

    Sem o registro, ``causa_silenciada`` deixa de ver a pausa: o alerta daquela
    causa volta a tocar, como antes do toque.

    Args:
        caminho_banco: banco SQLite.
        id_acao: id devolvido por ``registrar_acao``/``registrar_acao_sem_repetir``.
        agora_utc: instante do pedido de desfazer.
        janela_segundos: idade máxima do registro (padrão ``JANELA_DESFAZER_SEGUNDOS``).

    Raises:
        RegistroInexistente: não há "Feito" com esse id (ou ele já foi desfeito).
        JanelaDesfazerExpirada: o registro é mais velho que ``janela_segundos``.
        sqlite3.Error: falha do banco.
    """
    try:
        identificador = int(id_acao)
    except (TypeError, ValueError) as erro:
        raise RegistroInexistente(f"Registro de ação inválido: {id_acao!r}.") from erro
    agora = agora_utc.astimezone(timezone.utc)
    garantir_tabelas(caminho_banco)
    with _TRAVA_REGISTRO:
        conexao = obter_conexao(caminho_banco)
        try:
            conexao.execute("BEGIN IMMEDIATE")
            linha = conexao.execute(
                "SELECT instante_utc, codigo_acao, causa FROM registro_acoes WHERE id_acao = ?", (identificador,)
            ).fetchone()
            if linha is None:
                conexao.rollback()
                raise RegistroInexistente(f"Não há registro de ação {identificador}.")
            gravado = _interpretar_iso(linha[0])
            idade = (agora - gravado).total_seconds() if gravado is not None else float("inf")
            if idade > max(0, int(janela_segundos)) or idade < -FOLGA_RELOGIO_SEGUNDOS:
                conexao.rollback()
                raise JanelaDesfazerExpirada(
                    f"O registro {identificador} tem {idade:.0f} s; só dá para desfazer até {int(janela_segundos)} s.")
            conexao.execute("DELETE FROM registro_acoes WHERE id_acao = ?", (identificador,))
            conexao.execute(
                "DELETE FROM eventos_interface WHERE id_evento = (SELECT id_evento FROM eventos_interface "
                "WHERE tipo = 'feito' AND instante_utc = ? AND detalhe = ? ORDER BY id_evento DESC LIMIT 1)",
                (str(linha[0]), f"{linha[1]}|{linha[2]}"),
            )
            conexao.commit()
        except Exception:
            if conexao.in_transaction:
                conexao.rollback()
            raise
        finally:
            conexao.close()


def causa_silenciada(caminho_banco: Path, causa: str, agora_utc: datetime,
                     minutos: int = MINUTOS_SILENCIO_FEITO, incluir_demonstracao: bool = False) -> bool:
    """``True`` se houve "Feito" para esta causa nos últimos ``minutos`` (padrão 60).

    Um "Feito" tocado sobre uma leitura de DEMONSTRAÇÃO (banca) só conta com
    ``incluir_demonstracao=True``: a apresentação não cala um alerta real.
    Nunca lança exceção: tabela ausente ou banco com erro → ``False`` (o alerta
    continua tocando, que é o lado seguro).
    """
    codigo = str(causa or "").strip().upper()
    if not codigo or codigo == "NENHUMA":
        return False
    limite_inferior = instante_iso_utc(agora_utc - timedelta(minutes=max(0, int(minutos))))
    limite_superior = instante_iso_utc(agora_utc + timedelta(minutes=2))
    try:
        conexao = obter_conexao(caminho_banco)
        try:
            if not _tabela_existe(conexao, "registro_acoes"):
                return False
            filtro_demo = "" if incluir_demonstracao else " AND COALESCE(h.origem, '') <> ?"
            parametros: Tuple[Any, ...] = (codigo, limite_inferior, limite_superior) + (
                () if incluir_demonstracao else (MODO_DEMONSTRACAO,))
            linha = conexao.execute(
                "SELECT 1 FROM registro_acoes r LEFT JOIN historico_leituras h ON h.id_leitura = r.id_leitura "
                "WHERE r.causa = ? AND r.instante_utc >= ? AND r.instante_utc <= ?" + filtro_demo + " LIMIT 1",
                parametros,
            ).fetchone()
        finally:
            conexao.close()
    except (sqlite3.Error, OSError) as erro:
        registrador.warning("Não foi possível consultar o silêncio da causa %s: %s", codigo, erro)
        return False
    return linha is not None


# ---------------------------------------------------------------------------
# Eventos da interface
# ---------------------------------------------------------------------------
def registrar_evento(caminho_banco: Path, tipo: str, detalhe: str = "",
                     instante_utc: Optional[datetime] = None) -> int:
    """Grava um evento da interface (``TIPOS_EVENTO``) e devolve o ``id_evento``.

    Com a configuração ``modo_operacao = DEMONSTRACAO`` o evento leva
    ``demonstracao = 1`` e fica fora do resumo do técnico (v4).

    Raises:
        ValueError: tipo fora do catálogo.
        sqlite3.Error: falha do banco.
    """
    codigo = str(tipo or "").strip().lower()
    if codigo not in TIPOS_EVENTO:
        raise ValueError(f"Tipo de evento desconhecido: {tipo!r}.")
    garantir_tabelas(caminho_banco)
    conexao = obter_conexao(caminho_banco)
    try:
        cursor = conexao.execute(
            "INSERT INTO eventos_interface (tipo, instante_utc, detalhe, demonstracao) VALUES (?, ?, ?, ?)",
            (codigo, instante_iso_utc(instante_utc or datetime.now(timezone.utc)),
             str(detalhe or "")[:TAMANHO_MAXIMO_DETALHE], int(_modo_demonstracao_ativo(conexao))),
        )
        conexao.commit()
        return int(cursor.lastrowid or 0)
    finally:
        conexao.close()


def limpar_eventos_antigos(caminho_banco: Path, dias: int = DIAS_RETENCAO_EVENTOS,
                           agora_utc: Optional[datetime] = None) -> int:
    """Apaga eventos da interface mais velhos que ``dias`` (padrão 90) e devolve quantos saíram.

    Raises:
        sqlite3.Error: falha do banco.
    """
    garantir_tabelas(caminho_banco)
    limite = instante_iso_utc((agora_utc or datetime.now(timezone.utc)) - timedelta(days=max(1, int(dias))))
    conexao = obter_conexao(caminho_banco)
    try:
        cursor = conexao.execute("DELETE FROM eventos_interface WHERE instante_utc < ?", (limite,))
        conexao.commit()
        return int(cursor.rowcount or 0)
    finally:
        conexao.close()


# ---------------------------------------------------------------------------
# Pedido de ajuda e WhatsApp
# ---------------------------------------------------------------------------
def normalizar_numero_whatsapp(numero: Any) -> str:
    """Número só com dígitos no formato internacional exigido por ``https://wa.me/<número>``.

    Tira "+", espaços, traços e parênteses; tira o "00" de discagem
    internacional; um número brasileiro só com DDD (10 ou 11 dígitos) ganha o
    55. Devolve ``""`` se não sobrar um número plausível (10 a 15 dígitos, E.164).
    """
    digitos = re.sub(r"\D", "", str(numero or ""))
    if digitos.startswith("00"):
        digitos = digitos[2:]
    digitos = digitos.lstrip("0")
    if len(digitos) in (10, 11) and not digitos.startswith(CODIGO_PAIS_PADRAO):
        digitos = CODIGO_PAIS_PADRAO + digitos
    if not DIGITOS_MINIMOS_TELEFONE <= len(digitos) <= DIGITOS_MAXIMOS_TELEFONE:
        return ""
    return digitos


def montar_link_whatsapp(numero: Any, texto: str) -> str:
    """``https://wa.me/<número>?text=<texto url-encoded>``; ``""`` sem número válido."""
    digitos = normalizar_numero_whatsapp(numero)
    if not digitos:
        return ""
    return f"{URL_WHATSAPP}{digitos}?text={quote(texto, safe='')}"


def _numero_curto(valor: Any) -> str:
    """Número com no máximo uma casa decimal e vírgula ("31,5"); "?" se ausente."""
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return "?"
    if numero != numero:
        return "?"
    texto = f"{numero:.1f}".rstrip("0").rstrip(".")
    return texto.replace(".", ",")


def limitar_texto(texto: str, limite: int = TAMANHO_MAXIMO_TEXTO_WHATSAPP) -> str:
    """Corta o texto em ``limite`` caracteres, terminando com reticências quando cortar."""
    if len(texto) <= limite:
        return texto
    return texto[:max(0, limite - 1)].rstrip() + "…"


def montar_texto_pedido(nome_propriedade: str, status: Optional[Dict[str, Any]], instante_utc: datetime,
                        resumo_curto: str = "") -> str:
    """Texto do pedido de ajuda para o WhatsApp do técnico (≤ 700 caracteres).

    Só leva o nome da propriedade (nenhum outro dado pessoal), a cor, a causa,
    a temperatura, a água na terra e, se houver, o resumo curto dos últimos dias.
    """
    hora = instante_utc.astimezone().strftime("%d/%m %H:%M")
    partes = [f"AgroAnálise – {nome_propriedade or 'propriedade'}: pedido de ajuda em {hora}."]
    if status:
        try:
            nivel = int(status.get("status_alerta", 1))
        except (TypeError, ValueError):
            nivel = 1
        causa = DESCRICAO_CAUSA_CURTA.get(str(status.get("causa") or ""), "")
        cultura = str(status.get("nome_cultura") or "")
        partes.append(f"{cultura}: sinal {NOMES_COR_TEXTO.get(nivel, 'Amarelo')}"
                      + (f" ({causa})" if causa else "") + ".")
        partes.append(f"Ar {_numero_curto(status.get('temperatura_ar'))} °C, água na terra "
                      f"{_numero_curto(status.get('umidade_solo'))} %.")
    if resumo_curto:
        partes.append(resumo_curto)
    return limitar_texto(" ".join(partes))


def registrar_pedido_ajuda(caminho_banco: Path, status: Optional[Dict[str, Any]],
                           instante_utc: Optional[datetime] = None) -> int:
    """Grava o pedido de ajuda (cor, causa, ar, terra e leitura do momento) e o evento ``chamou_tecnico``.

    Pedido feito durante a demonstração (``status["modo_operacao"] ==
    "DEMONSTRACAO"``, ou leitura da banca) leva ``demonstracao = 1`` no pedido e
    no evento, e fica fora do resumo do técnico (v4).

    Raises:
        sqlite3.Error: falha do banco.
    """
    garantir_tabelas(caminho_banco)
    instante = instante_iso_utc(instante_utc or datetime.now(timezone.utc))
    dados = status or {}
    try:
        nivel: Optional[int] = int(dados["status_alerta"]) if dados.get("status_alerta") is not None else None
    except (TypeError, ValueError):
        nivel = None
    if nivel not in (0, 1, 2):
        nivel = None
    causa = str(dados.get("causa") or "")
    conexao = obter_conexao(caminho_banco)
    try:
        id_leitura = dados.get("id_leitura")
        try:
            leitura = int(id_leitura) if id_leitura is not None and _leitura_existe(conexao, id_leitura) else None
        except (TypeError, ValueError):
            leitura = None
        demonstracao = int(str(dados.get("modo_operacao") or "").strip().upper() == MODO_DEMONSTRACAO
                           or _leitura_de_demonstracao(conexao, leitura))
        cursor = conexao.execute(
            "INSERT INTO pedido_ajuda (instante_utc, status, causa, temperatura_ar, umidade_solo, id_leitura, "
            "demonstracao) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (instante, nivel, causa, _real_ou_none(dados.get("temperatura_ar")),
             _real_ou_none(dados.get("umidade_solo")), leitura, demonstracao),
        )
        conexao.execute("INSERT INTO eventos_interface (tipo, instante_utc, detalhe, demonstracao) "
                        "VALUES ('chamou_tecnico', ?, ?, ?)", (instante, causa, demonstracao))
        conexao.commit()
        return int(cursor.lastrowid or 0)
    finally:
        conexao.close()


def _real_ou_none(valor: Any) -> Optional[float]:
    """``float`` finito ou ``None``."""
    if valor is None or isinstance(valor, bool):
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    return numero if numero == numero and abs(numero) != float("inf") else None


# ---------------------------------------------------------------------------
# Resumo do técnico
# ---------------------------------------------------------------------------
def _cultura_ativa(caminho_banco: Path) -> str:
    """Código da cultura monitorada (configuração ``cultura_ativa``)."""
    return str(obter_configuracao("cultura_ativa", CULTURA_PADRAO, caminho_banco=caminho_banco) or CULTURA_PADRAO)


def _horas_por_cor_e_causa(conexao: sqlite3.Connection, caminho_banco: Path, codigo_cultura: str,
                           inicio_iso: str, inicio_local: str) -> Dict[str, Any]:
    """Horas (frações pelo número de leituras de cada hora) por cor e por causa, só de leituras de campo.

    As leituras brutas dão cor e causa; as horas já agregadas pela retenção
    (``historico_agregado_hora``) dão só a cor (a causa fica ``SEM_DETALHE``).
    """
    colunas = _colunas(conexao, "historico_leituras")
    expr_motivo = "COALESCE(h.motivo_regra, '')" if "motivo_regra" in colunas else "''"
    expr_estado = "COALESCE(h.estado_sensor, 'OK')" if "estado_sensor" in colunas else "'OK'"
    filtro_tempo = "h.instante_utc >= ?" if "instante_utc" in colunas else "h.data_hora >= ?"
    parametro_tempo = inicio_iso if "instante_utc" in colunas else inicio_local
    consulta = f"""
        SELECT strftime('%Y-%m-%d %H', h.data_hora) AS hora, h.origem, h.status_alerta,
               {expr_estado} AS estado, {expr_motivo} AS motivo,
               CASE WHEN h.deficit_hidrico_solo > 0 THEN 1 WHEN h.deficit_hidrico_solo < 0 THEN -1 ELSE 0 END,
               CASE WHEN h.desvio_termico > 0 THEN 1 WHEN h.desvio_termico < 0 THEN -1 ELSE 0 END,
               COUNT(*) AS n, AVG(h.temperatura_ar) AS t, AVG(h.umidade_solo) AS s
          FROM historico_leituras h JOIN culturas c ON c.id_cultura = h.id_cultura
         WHERE c.codigo = ? AND h.origem IN (?, ?) AND {filtro_tempo}
         GROUP BY 1, 2, 3, 4, 5, 6, 7
    """
    linhas = conexao.execute(consulta, (codigo_cultura, MODO_CAMPO_USB, MODO_SIMULACAO, parametro_tempo)).fetchall()
    totais_hora: Dict[Tuple[str, str], int] = {}
    for linha in linhas:
        chave = (str(linha[0]), str(linha[1]))
        totais_hora[chave] = totais_hora.get(chave, 0) + int(linha[7])
    try:
        parametros = buscar_parametros_cultura(codigo_cultura, caminho_banco=caminho_banco)
    except (KeyError, ValueError, sqlite3.Error):
        parametros = None
    horas_cor = {cor: 0.0 for cor in CORES_STATUS.values()}
    horas_causa: Dict[str, float] = {causa: 0.0 for causa in CAUSAS_VALIDAS}
    horas_simulacao = 0.0
    for linha in linhas:
        hora, origem, status, estado, motivo = str(linha[0]), str(linha[1]), int(linha[2]), str(linha[3]), str(linha[4])
        fracao = int(linha[7]) / max(1, totais_hora[(hora, origem)])
        if origem != MODO_CAMPO_USB:
            horas_simulacao += fracao
            continue
        horas_cor[CORES_STATUS.get(status, "amarelo")] += fracao
        if parametros is None:
            continue
        leitura = {"temperatura_ar": linha[8], "umidade_solo": linha[9], "motivo_regra": motivo}
        causa = derivar_causa(status, leitura, parametros, "NENHUM", estado)
        horas_causa[causa] = horas_causa.get(causa, 0.0) + fracao
    horas_ja_contadas = {hora for (hora, origem) in totais_hora if origem == MODO_CAMPO_USB}
    if _tabela_existe(conexao, "historico_agregado_hora"):
        agregadas = conexao.execute(
            "SELECT strftime('%Y-%m-%d %H', a.hora), a.n, a.n_verde, a.n_amarelo, a.n_vermelho "
            "FROM historico_agregado_hora a JOIN culturas c ON c.id_cultura = a.id_cultura "
            "WHERE c.codigo = ? AND a.origem = ? AND a.hora >= ?",
            (codigo_cultura, MODO_CAMPO_USB, inicio_local),
        ).fetchall()
        for hora, n, verde, amarelo, vermelho in agregadas:
            if str(hora) in horas_ja_contadas or not n:
                continue
            horas_cor["verde"] += (verde or 0) / n
            horas_cor["amarelo"] += (amarelo or 0) / n
            horas_cor["vermelho"] += (vermelho or 0) / n
            horas_causa["SEM_DETALHE"] = horas_causa.get("SEM_DETALHE", 0.0) + 1.0
    return {
        "horas_por_cor": {cor: round(valor, 1) for cor, valor in horas_cor.items()},
        "horas_por_causa": {causa: round(valor, 1) for causa, valor in horas_causa.items()},
        "horas_simulacao": round(horas_simulacao, 1),
    }


def _avisos_antecipados(conexao: sqlite3.Connection, codigo_cultura: str, inicio_iso: str,
                        agora_utc: datetime) -> Dict[str, Any]:
    """Avisos antecipados (Amarelo vindo do prognóstico) confirmados ou não por Vermelho em até 12 h.

    Aviso = leitura com ``analise_antecipacao.status_antecipado = 1``,
    ``status_alerta = 1`` e ``status_regra < 2``. Leituras de aviso a menos de
    ``MINUTOS_ENTRE_AVISOS`` uma da outra são o MESMO aviso. Confirmado: houve
    Vermelho (mesma cultura, fora da demonstração) entre o início do aviso e
    12 h depois da última leitura dele. Avisos cuja janela de 12 h ainda não
    fechou e sem Vermelho ficam como ``pendentes``.
    """
    vazio = {"confirmados": 0, "nao_confirmados": 0, "pendentes": 0, "antecedencia_mediana_h": None}
    if not _tabela_existe(conexao, "analise_antecipacao"):
        return vazio
    linhas = conexao.execute(
        """
        SELECT h.instante_utc FROM analise_antecipacao a
          JOIN historico_leituras h ON h.id_leitura = a.id_leitura
          JOIN culturas c ON c.id_cultura = h.id_cultura
         WHERE a.status_antecipado = 1 AND h.status_alerta = 1 AND h.status_regra < 2
           AND c.codigo = ? AND h.origem <> ? AND h.instante_utc >= ?
         ORDER BY h.instante_utc
        """,
        (codigo_cultura, MODO_DEMONSTRACAO, inicio_iso),
    ).fetchall()
    episodios: List[List[datetime]] = []
    for (texto,) in linhas:
        instante = _interpretar_iso(texto)
        if instante is None:
            continue
        if episodios and (instante - episodios[-1][1]) <= timedelta(minutes=MINUTOS_ENTRE_AVISOS):
            episodios[-1][1] = instante
        else:
            episodios.append([instante, instante])
    resultado = dict(vazio)
    antecedencias: List[float] = []
    for inicio, fim in episodios:
        limite = fim + timedelta(hours=HORIZONTE_CONFIRMACAO_HORAS)
        vermelho = conexao.execute(
            """
            SELECT MIN(h.instante_utc) FROM historico_leituras h JOIN culturas c ON c.id_cultura = h.id_cultura
             WHERE c.codigo = ? AND h.origem <> ? AND h.status_alerta = 2
               AND h.instante_utc > ? AND h.instante_utc <= ?
            """,
            (codigo_cultura, MODO_DEMONSTRACAO, instante_iso_utc(inicio), instante_iso_utc(limite)),
        ).fetchone()
        primeiro_vermelho = _interpretar_iso(vermelho[0]) if vermelho is not None else None
        if primeiro_vermelho is not None:
            resultado["confirmados"] += 1
            antecedencias.append((primeiro_vermelho - inicio).total_seconds() / 3600.0)
        elif limite > agora_utc:
            resultado["pendentes"] += 1
        else:
            resultado["nao_confirmados"] += 1
    if antecedencias:
        resultado["antecedencia_mediana_h"] = round(statistics.median(antecedencias), 1)
    return resultado


def _acoes_feitas(conexao: sqlite3.Connection, inicio_iso: str, incluir_demonstracao: bool = False) -> Dict[str, Any]:
    """Toques em "Feito" no período: total, por ação e os últimos ``LIMITE_LISTAS_RESUMO``."""
    if not _tabela_existe(conexao, "registro_acoes"):
        return {"total": 0, "por_acao": {}, "ultimas": []}
    # Toques sobre leituras da banca (demonstração) não entram no resumo do técnico.
    marca = "-" if incluir_demonstracao else MODO_DEMONSTRACAO
    origem = ("FROM registro_acoes r LEFT JOIN historico_leituras h ON h.id_leitura = r.id_leitura "
              "WHERE r.instante_utc >= ? AND COALESCE(h.origem, '') <> ?")
    por_acao = {str(codigo): int(n) for codigo, n in conexao.execute(
        f"SELECT r.codigo_acao, COUNT(*) {origem} GROUP BY r.codigo_acao", (inicio_iso, marca))}
    ultimas = [
        {"instante_utc": str(instante), "codigo_acao": str(codigo), "descricao": DESCRICOES_ACAO.get(str(codigo), ""),
         "causa": str(causa)}
        for instante, codigo, causa in conexao.execute(
            f"SELECT r.instante_utc, r.codigo_acao, r.causa {origem} ORDER BY r.instante_utc DESC LIMIT ?",
            (inicio_iso, marca, LIMITE_LISTAS_RESUMO))
    ]
    return {"total": sum(por_acao.values()), "por_acao": por_acao, "ultimas": ultimas}


def _problemas_sensor(conexao: sqlite3.Connection, codigo_cultura: str, inicio_iso: str) -> Dict[str, Any]:
    """Leituras de campo com ``estado_sensor`` diferente de OK: total, horas distintas e por diagnóstico."""
    colunas = _colunas(conexao, "historico_leituras")
    if "estado_sensor" not in colunas or "instante_utc" not in colunas:
        return {"leituras": 0, "horas": 0, "por_estado": {}, "ultimos": []}
    linhas = conexao.execute(
        """
        SELECT h.estado_sensor, COALESCE(h.diagnostico_sensor, ''), COUNT(*),
               COUNT(DISTINCT strftime('%Y-%m-%d %H', h.data_hora)), MIN(h.instante_utc), MAX(h.instante_utc)
          FROM historico_leituras h JOIN culturas c ON c.id_cultura = h.id_cultura
         WHERE c.codigo = ? AND h.origem = ? AND h.estado_sensor <> 'OK' AND h.instante_utc >= ?
         GROUP BY 1, 2 ORDER BY 6 DESC
        """,
        (codigo_cultura, MODO_CAMPO_USB, inicio_iso),
    ).fetchall()
    horas = conexao.execute(
        """
        SELECT COUNT(DISTINCT strftime('%Y-%m-%d %H', h.data_hora))
          FROM historico_leituras h JOIN culturas c ON c.id_cultura = h.id_cultura
         WHERE c.codigo = ? AND h.origem = ? AND h.estado_sensor <> 'OK' AND h.instante_utc >= ?
        """,
        (codigo_cultura, MODO_CAMPO_USB, inicio_iso),
    ).fetchone()
    por_estado: Dict[str, int] = {}
    for estado, _, quantidade, _, _, _ in linhas:
        por_estado[str(estado)] = por_estado.get(str(estado), 0) + int(quantidade)
    ultimos = [
        {"estado": str(estado), "diagnostico": str(diagnostico)[:TAMANHO_MAXIMO_DETALHE], "leituras": int(quantidade),
         "horas": int(horas_distintas), "primeiro_utc": str(primeiro), "ultimo_utc": str(ultimo)}
        for estado, diagnostico, quantidade, horas_distintas, primeiro, ultimo in linhas[:LIMITE_LISTAS_RESUMO]
    ]
    return {"leituras": sum(por_estado.values()), "horas": int(horas[0] or 0) if horas else 0,
            "por_estado": por_estado, "ultimos": ultimos}


def _pedidos_ajuda(conexao: sqlite3.Connection, inicio_iso: str, incluir_demonstracao: bool = False) -> Dict[str, Any]:
    """Pedidos de ajuda do período (total e os últimos); os da demonstração só com ``incluir_demonstracao``."""
    if not _tabela_existe(conexao, "pedido_ajuda"):
        return {"total": 0, "ultimos": []}
    filtro = _filtro_demonstracao(conexao, "pedido_ajuda", incluir_demonstracao)
    total = conexao.execute(f"SELECT COUNT(*) FROM pedido_ajuda WHERE instante_utc >= ?{filtro}",
                            (inicio_iso,)).fetchone()
    ultimos = [
        {"instante_utc": str(instante), "status": status, "causa": str(causa or ""),
         "temperatura_ar": temperatura, "umidade_solo": solo}
        for instante, status, causa, temperatura, solo in conexao.execute(
            "SELECT instante_utc, status, causa, temperatura_ar, umidade_solo FROM pedido_ajuda "
            f"WHERE instante_utc >= ?{filtro} ORDER BY instante_utc DESC LIMIT ?", (inicio_iso, LIMITE_LISTAS_RESUMO))
    ]
    return {"total": int(total[0] or 0) if total else 0, "ultimos": ultimos}


def _eventos_por_tipo(conexao: sqlite3.Connection, inicio_iso: str, incluir_demonstracao: bool = False) -> Dict[str, int]:
    """Contagem dos eventos da interface por tipo no período (todos os tipos, com zero).

    Os eventos gravados durante a demonstração só contam com ``incluir_demonstracao``.
    """
    contagem = {tipo: 0 for tipo in TIPOS_EVENTO}
    if _tabela_existe(conexao, "eventos_interface"):
        filtro = _filtro_demonstracao(conexao, "eventos_interface", incluir_demonstracao)
        for tipo, quantidade in conexao.execute(
                f"SELECT tipo, COUNT(*) FROM eventos_interface WHERE instante_utc >= ?{filtro} GROUP BY tipo",
                (inicio_iso,)):
            contagem[str(tipo)] = int(quantidade)
    return contagem


def _registros_de_demonstracao(conexao: sqlite3.Connection, inicio_iso: str) -> Dict[str, int]:
    """Quantos pedidos de ajuda e eventos do período são da demonstração (o que o resumo deixou de fora)."""
    contagem = {"pedidos_ajuda": 0, "eventos_interface": 0}
    for chave, tabela in (("pedidos_ajuda", "pedido_ajuda"), ("eventos_interface", "eventos_interface")):
        if _tabela_existe(conexao, tabela) and COLUNA_DEMONSTRACAO in _colunas(conexao, tabela):
            linha = conexao.execute(f"SELECT COUNT(*) FROM {tabela} WHERE instante_utc >= ? AND "
                                    f"{COLUNA_DEMONSTRACAO} = 1", (inicio_iso,)).fetchone()
            contagem[chave] = int(linha[0] or 0) if linha else 0
    return contagem


def resumo_curto_whatsapp(resumo: Dict[str, Any]) -> str:
    """Uma frase com as horas por cor, os avisos confirmados e as ações feitas do período."""
    cores = resumo.get("horas_por_cor", {})
    avisos = resumo.get("avisos_antecipados", {})
    dias = int(resumo.get("periodo", {}).get("dias", DIAS_RESUMO_PADRAO))
    return (f"Últimos {dias} dias: {_numero_curto(cores.get('vermelho', 0))} h vermelho, "
            f"{_numero_curto(cores.get('amarelo', 0))} h amarelo, {_numero_curto(cores.get('verde', 0))} h verde; "
            f"avisos confirmados {avisos.get('confirmados', 0)} de "
            f"{int(avisos.get('confirmados', 0)) + int(avisos.get('nao_confirmados', 0))}; "
            f"{resumo.get('acoes_feitas', {}).get('total', 0)} ações feitas; "
            f"{resumo.get('problemas_sensor', {}).get('horas', 0)} h com sensor suspeito.")


def montar_texto_whatsapp(resumo: Dict[str, Any], incluir_demonstracao: Optional[bool] = None) -> str:
    """Texto do resumo para o técnico no WhatsApp (≤ 700 caracteres; só o nome da propriedade como dado pessoal).

    Os números vêm do ``resumo``, que por padrão já deixa a demonstração de fora.
    Com ``incluir_demonstracao=True`` (ou um resumo montado assim), o texto avisa
    que os números incluem a demonstração da banca. ``None`` = o que o resumo diz.
    """
    if incluir_demonstracao is None:
        incluir_demonstracao = bool(resumo.get("incluir_demonstracao", False))
    causas = {causa: horas for causa, horas in resumo.get("horas_por_causa", {}).items()
              if causa not in ("NENHUMA", "SEM_DETALHE") and horas}
    principais = sorted(causas.items(), key=lambda item: -float(item[1]))[:3]
    partes = [f"AgroAnálise – {resumo.get('nome_propriedade') or 'propriedade'} – {resumo.get('nome_cultura', '')}.",
              resumo_curto_whatsapp(resumo)]
    if principais:
        partes.append("Causas: " + ", ".join(f"{DESCRICAO_CAUSA_CURTA.get(causa, causa.lower())} "
                                             f"{_numero_curto(horas)} h" for causa, horas in principais) + ".")
    pedidos = resumo.get("pedidos_ajuda", {}).get("total", 0)
    if pedidos:
        partes.append(f"Pedidos de ajuda: {pedidos}.")
    prognostico = resumo.get("prognostico", {})
    if prognostico.get("suspenso"):
        partes.append("Previsão de 12 h suspensa (erro alto no local).")
    calibracao = resumo.get("calibracao", {})
    if calibracao.get("dias_desde_calibracao") is None:
        partes.append("Sonda de solo sem calibração no local.")
    else:
        partes.append(f"Sonda calibrada há {calibracao['dias_desde_calibracao']} dias.")
    if incluir_demonstracao:
        partes.append("Inclui a demonstração da banca.")
    return limitar_texto(" ".join(partes))


def _texto_ou_vazio(valor: Any) -> str:
    """Texto sem espaços nas pontas; ``""`` para ``None``."""
    return "" if valor is None else str(valor).strip()


def montar_resumo_tecnico(
    caminho_banco: Path,
    dias: int = DIAS_RESUMO_PADRAO,
    agora_utc: Optional[datetime] = None,
    configuracoes: Optional[Dict[str, Any]] = None,
    prognostico_disponivel: bool = False,
    calibracao: Optional[Dict[str, Any]] = None,
    incluir_demonstracao: bool = False,
) -> Dict[str, Any]:
    """Resumo dos últimos ``dias`` para a página do técnico (``/api/tecnico/resumo``).

    Por padrão (v4) os pedidos de ajuda e os eventos gravados durante a
    demonstração da banca ficam de fora, como já ficavam os "Feito" da banca;
    ``demonstracao_excluida`` diz quantos ficaram de fora.

    Args:
        caminho_banco: banco SQLite.
        dias: tamanho do período (1 a 90).
        agora_utc: fim do período (padrão: agora; injetável nos testes).
        configuracoes: configurações já lidas (``obter_todas_configuracoes``).
        prognostico_disponivel: se o módulo de prognóstico foi carregado.
        calibracao: estado da calibração da sonda (``calibracao_sonda.estado_calibracao``).
        incluir_demonstracao: ``True`` conta também o que foi gravado na demonstração.

    Returns:
        Dicionário com ``periodo``, ``nome_propriedade``, ``codigo_cultura``,
        ``nome_cultura``, ``horas_por_cor``, ``horas_por_causa``,
        ``horas_simulacao``, ``avisos_antecipados``, ``acoes_feitas``,
        ``problemas_sensor``, ``pedidos_ajuda``, ``eventos_interface``,
        ``prognostico``, ``calibracao``, ``configuracao``, ``incluir_demonstracao``,
        ``demonstracao_excluida``, ``texto_whatsapp`` e ``link_whatsapp``.

    Raises:
        sqlite3.Error: banco inacessível.
    """
    agora = (agora_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    periodo = max(1, min(DIAS_RESUMO_MAXIMO, int(dias)))
    inicio = agora - timedelta(days=periodo)
    inicio_iso = instante_iso_utc(inicio)
    inicio_local = inicio.astimezone().replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    config = dict(configuracoes or {})
    codigo_cultura = str(config.get("cultura_ativa") or _cultura_ativa(caminho_banco))
    garantir_tabelas(caminho_banco)
    conexao = obter_conexao(caminho_banco)
    try:
        linha_cultura = conexao.execute("SELECT nome_comum FROM culturas WHERE codigo = ?", (codigo_cultura,)).fetchone()
        horas = _horas_por_cor_e_causa(conexao, caminho_banco, codigo_cultura, inicio_iso, inicio_local)
        avisos = _avisos_antecipados(conexao, codigo_cultura, inicio_iso, agora)
        acoes = _acoes_feitas(conexao, inicio_iso, incluir_demonstracao)
        sensor = _problemas_sensor(conexao, codigo_cultura, inicio_iso)
        pedidos = _pedidos_ajuda(conexao, inicio_iso, incluir_demonstracao)
        eventos = _eventos_por_tipo(conexao, inicio_iso, incluir_demonstracao)
        excluidos = ({"pedidos_ajuda": 0, "eventos_interface": 0} if incluir_demonstracao
                     else _registros_de_demonstracao(conexao, inicio_iso))
    finally:
        conexao.close()
    brier = _real_ou_none(config.get("prognostico_brier_local_28d"))
    suspenso_bruto = config.get("prognostico_suspenso", False)
    suspenso = suspenso_bruto if isinstance(suspenso_bruto, bool) else str(suspenso_bruto).strip().lower() in (
        "true", "1", "sim", "yes", "verdadeiro")
    numero = normalizar_numero_whatsapp(config.get("numero_whatsapp_tecnico"))
    resumo: Dict[str, Any] = {
        "periodo": {"dias": periodo, "inicio_utc": inicio_iso, "fim_utc": instante_iso_utc(agora)},
        "nome_propriedade": _texto_ou_vazio(config.get("nome_propriedade")),
        "codigo_cultura": codigo_cultura,
        "nome_cultura": str(linha_cultura[0]) if linha_cultura else codigo_cultura,
        "horas_por_cor": horas["horas_por_cor"],
        "horas_por_causa": horas["horas_por_causa"],
        "legendas_causa": dict(LEGENDAS_CAUSA),
        "horas_simulacao": horas["horas_simulacao"],
        "avisos_antecipados": avisos,
        "acoes_feitas": acoes,
        "problemas_sensor": sensor,
        "pedidos_ajuda": pedidos,
        "eventos_interface": eventos,
        "prognostico": {"disponivel": bool(prognostico_disponivel), "suspenso": bool(suspenso),
                        "brier_local_28d": brier},
        "calibracao": dict(calibracao or {"dias_desde_calibracao": None, "metodo": "FABRICA"}),
        "configuracao": {
            "data_florada_principal": _texto_ou_vazio(config.get("data_florada_principal")),
            "seca_programada_ativa": bool(config.get("seca_programada_ativa") is True
                                          or str(config.get("seca_programada_ativa")).lower() == "true"),
            "seca_programada_inicio": _texto_ou_vazio(config.get("seca_programada_inicio")),
            "seca_programada_fim": _texto_ou_vazio(config.get("seca_programada_fim")),
            "numero_whatsapp_cadastrado": bool(numero),
            "regra_temperatura": _texto_ou_vazio(config.get("regra_temperatura")) or "instantanea",
        },
        "incluir_demonstracao": bool(incluir_demonstracao),
        "demonstracao_excluida": excluidos,
    }
    texto = montar_texto_whatsapp(resumo, incluir_demonstracao)
    resumo["texto_whatsapp"] = texto
    resumo["link_whatsapp"] = montar_link_whatsapp(numero, texto) if numero else f"{URL_WHATSAPP}?text={quote(texto, safe='')}"
    return resumo

