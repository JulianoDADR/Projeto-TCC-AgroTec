"""Testes da Frente B (v4) — domínio do retorno do agricultor e PIN com código.

Cobrem, sempre com banco SQLite TEMPORÁRIO, relógio injetado e sem rede:

    * ``desfazer_acao``: apaga o "Feito" (a pausa do alerta some), recusa
      registro inexistente e registro mais velho que a janela de 120 s;
    * ``registrar_acao_sem_repetir``: três toques iguais em menos de 60 s viram
      um registro; 61 s depois, outro; toques diferentes não se juntam;
    * marca ``demonstracao`` em pedidos de ajuda e eventos: fora do resumo do
      técnico (e do texto do WhatsApp), a não ser com ``incluir_demonstracao``;
    * migração idempotente de um banco da v3 (sem a coluna ``demonstracao``);
    * ``seguranca_banca``: códigos e mensagem do PIN e a contagem do bloqueio.

Execução (a partir da raiz do projeto): ``python -m unittest tests.test_retorno_v4 -v``.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

RAIZ_PROJETO: Path = Path(__file__).resolve().parent.parent
PASTA_SRC: Path = RAIZ_PROJETO / "src"
if str(PASTA_SRC) not in sys.path:
    sys.path.insert(0, str(PASTA_SRC))

import retorno_agricultor as ret  # noqa: E402
import seguranca_banca  # noqa: E402
from database import (  # noqa: E402
    MODO_DEMONSTRACAO,
    MODO_SIMULACAO,
    RegistroLeitura,
    buscar_parametros_cultura,
    definir_configuracao,
    inicializar_banco,
    inserir_leitura,
    obter_conexao,
)
from gerador_dataset import FORMATO_DATA_HORA, calcular_features  # noqa: E402

AGORA: datetime = datetime(2026, 7, 10, 17, 0, 0, tzinfo=timezone.utc)
ORIGEM_TESTE: str = "192.168.0.10"

# Esquema das tabelas do retorno na v3 (antes da coluna ``demonstracao``), para a migração.
DDL_RETORNO_V3: str = """
CREATE TABLE IF NOT EXISTS registro_acoes (
    id_acao         INTEGER PRIMARY KEY AUTOINCREMENT,
    id_leitura      INTEGER REFERENCES historico_leituras(id_leitura) ON DELETE SET NULL,
    codigo_acao     TEXT    NOT NULL,
    causa           TEXT    NOT NULL,
    instante_utc    TEXT    NOT NULL,
    origem_aparelho TEXT    NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS eventos_interface (
    id_evento    INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo         TEXT    NOT NULL
                 CHECK (tipo IN ('ouviu','tocou_pictograma','feito','ajuda','sem_voz','rede_caida','chamou_tecnico')),
    instante_utc TEXT    NOT NULL,
    detalhe      TEXT    NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pedido_ajuda (
    id_pedido      INTEGER PRIMARY KEY AUTOINCREMENT,
    instante_utc   TEXT    NOT NULL,
    status         INTEGER CHECK (status IS NULL OR status IN (0, 1, 2)),
    causa          TEXT    NOT NULL DEFAULT '',
    temperatura_ar REAL,
    umidade_solo   REAL,
    id_leitura     INTEGER REFERENCES historico_leituras(id_leitura) ON DELETE SET NULL
);
"""


class RelogioFalso:
    """Relógio monotônico controlado pelo teste."""

    def __init__(self) -> None:
        """Começa no instante 1000 s."""
        self.agora: float = 1000.0

    def __call__(self) -> float:
        """Instante atual (segundos)."""
        return self.agora


class BaseRetorno(unittest.TestCase):
    """Banco temporário inicializado para cada teste."""

    def setUp(self) -> None:
        """Cria o banco temporário com as culturas e as tabelas do retorno."""
        self._diretorio = tempfile.mkdtemp(prefix="agro_retorno_v4_")
        self.banco = Path(self._diretorio) / "agro.db"
        inicializar_banco(self.banco)
        ret.garantir_tabelas(self.banco)
        self.arabica = buscar_parametros_cultura("cafe_arabica", caminho_banco=self.banco)

    def tearDown(self) -> None:
        """Apaga o banco temporário."""
        shutil.rmtree(self._diretorio, ignore_errors=True)

    def inserir(self, instante: datetime, origem: str = "CAMPO_USB", status: int = 2,
                temperatura: float = 20.0, solo: float = 30.0) -> int:
        """Grava uma leitura do Arábica no instante dado e devolve o id."""
        deficit, desvio = calcular_features(temperatura, solo, self.arabica)
        return inserir_leitura(RegistroLeitura(
            data_hora=instante.astimezone().replace(tzinfo=None).strftime(FORMATO_DATA_HORA),
            codigo_cultura="cafe_arabica", origem=origem, temperatura_ar_bruta=temperatura, umidade_ar_bruta=70.0,
            umidade_solo_bruta=solo, temperatura_ar=temperatura, umidade_ar=70.0, umidade_solo=solo,
            deficit_hidrico_solo=deficit, desvio_termico=desvio, status_regra=status, status_predito=status,
            status_alerta=status, probabilidade_predicao=1.0, flag_anomalia=0, descricao_anomalia="",
            mensagem_voz="Perigo.", versao_modelo="REGRAS", instante_utc=instante.isoformat(timespec="seconds")),
            caminho_banco=self.banco)

    def consultar(self, sql: str, parametros: Tuple[Any, ...] = ()) -> List[Tuple[Any, ...]]:
        """Linhas de uma consulta no banco do teste."""
        conexao = obter_conexao(self.banco)
        try:
            return [tuple(linha) for linha in conexao.execute(sql, parametros).fetchall()]
        finally:
            conexao.close()

    def contar(self, tabela: str) -> int:
        """Número de linhas da tabela."""
        return int(self.consultar(f"SELECT COUNT(*) FROM {tabela}")[0][0])


# ---------------------------------------------------------------------------
# Desfazer o "Feito"
# ---------------------------------------------------------------------------
class TesteDesfazer(BaseRetorno):
    """``desfazer_acao``: o toque acidental em "Feito" não cala o alerta."""

    def test_constantes_e_excecoes(self) -> None:
        """Janelas de 120 s e 60 s; exceções com as bases pedidas."""
        self.assertEqual(ret.JANELA_DESFAZER_SEGUNDOS, 120)
        self.assertEqual(ret.JANELA_REPETICAO_SEGUNDOS, 60)
        self.assertTrue(issubclass(ret.RegistroInexistente, LookupError))
        self.assertTrue(issubclass(ret.JanelaDesfazerExpirada, ValueError))

    def test_desfazer_devolve_o_alerta(self) -> None:
        """Feito + desfazer: a linha sai de ``registro_acoes`` e a causa deixa de estar silenciada."""
        leitura = self.inserir(AGORA - timedelta(seconds=5))
        id_acao = ret.registrar_acao(self.banco, "REGAR_AGORA", "TERRA_SECA", leitura, ORIGEM_TESTE, AGORA)
        self.assertTrue(ret.causa_silenciada(self.banco, "TERRA_SECA", AGORA + timedelta(seconds=10)))
        self.assertEqual(self.contar("registro_acoes"), 1)
        ret.desfazer_acao(self.banco, id_acao, AGORA + timedelta(seconds=10))
        self.assertEqual(self.contar("registro_acoes"), 0)
        self.assertFalse(ret.causa_silenciada(self.banco, "TERRA_SECA", AGORA + timedelta(seconds=10)))
        # O evento "feito" que o toque gerou também sai (o resumo não conta um Feito desfeito).
        self.assertEqual(self.consultar("SELECT COUNT(*) FROM eventos_interface WHERE tipo = 'feito'")[0][0], 0)
        resumo = ret.montar_resumo_tecnico(self.banco, agora_utc=AGORA + timedelta(minutes=1))
        self.assertEqual(resumo["acoes_feitas"]["total"], 0)
        self.assertEqual(resumo["eventos_interface"]["feito"], 0)

    def test_desfazer_no_limite_e_depois_da_janela(self) -> None:
        """Aos 120 s ainda desfaz; aos 121 s levanta ``JanelaDesfazerExpirada`` e nada é apagado."""
        primeiro = ret.registrar_acao(self.banco, "COBRIR_MUDAS", "FRIO_GEADA", None, ORIGEM_TESTE, AGORA)
        segundo = ret.registrar_acao(self.banco, "CHEGAR_TERRA", "FRIO_GEADA", None, ORIGEM_TESTE, AGORA)
        with self.assertRaises(ret.JanelaDesfazerExpirada):
            ret.desfazer_acao(self.banco, primeiro, AGORA + timedelta(seconds=121))
        self.assertEqual(self.contar("registro_acoes"), 2)
        self.assertTrue(ret.causa_silenciada(self.banco, "FRIO_GEADA", AGORA + timedelta(seconds=121)))
        ret.desfazer_acao(self.banco, segundo, AGORA + timedelta(seconds=120))
        self.assertEqual(self.contar("registro_acoes"), 1)
        # Janela própria (por exemplo, a tela pede 15 s).
        with self.assertRaises(ret.JanelaDesfazerExpirada):
            ret.desfazer_acao(self.banco, primeiro, AGORA + timedelta(seconds=16), janela_segundos=15)

    def test_desfazer_registro_inexistente(self) -> None:
        """Id que não existe, id inválido e desfazer duas vezes levantam ``RegistroInexistente``."""
        with self.assertRaises(ret.RegistroInexistente):
            ret.desfazer_acao(self.banco, 999, AGORA)
        with self.assertRaises(LookupError):
            ret.desfazer_acao(self.banco, "abc", AGORA)  # type: ignore[arg-type]
        id_acao = ret.registrar_acao(self.banco, "NAO_REGAR", "ENCHARCADO", None, ORIGEM_TESTE, AGORA)
        ret.desfazer_acao(self.banco, id_acao, AGORA + timedelta(seconds=3))
        with self.assertRaises(ret.RegistroInexistente):
            ret.desfazer_acao(self.banco, id_acao, AGORA + timedelta(seconds=4))

    def test_desfazer_so_apaga_o_proprio_registro(self) -> None:
        """Dois "Feito" da mesma causa: desfazer um mantém o outro (e a pausa continua)."""
        id_a = ret.registrar_acao(self.banco, "REGAR_AGORA", "TERRA_SECA", None, ORIGEM_TESTE, AGORA)
        id_b = ret.registrar_acao(self.banco, "REGAR_AGORA", "TERRA_SECA", None, "outro", AGORA + timedelta(seconds=1))
        ret.desfazer_acao(self.banco, id_a, AGORA + timedelta(seconds=30))
        self.assertEqual(self.consultar("SELECT id_acao FROM registro_acoes"), [(id_b,)])
        self.assertTrue(ret.causa_silenciada(self.banco, "TERRA_SECA", AGORA + timedelta(seconds=30)))
        self.assertEqual(self.consultar("SELECT COUNT(*) FROM eventos_interface WHERE tipo = 'feito'")[0][0], 1)


# ---------------------------------------------------------------------------
# Feito sem duplicata
# ---------------------------------------------------------------------------
class TesteSemRepetir(BaseRetorno):
    """``registrar_acao_sem_repetir``: um toque repetido não vira outro registro."""

    def test_tres_toques_um_registro(self) -> None:
        """Três chamadas iguais em menos de 60 s: uma linha; retornos (id, False), (id, True), (id, True)."""
        leitura = self.inserir(AGORA - timedelta(seconds=5))
        retornos = [ret.registrar_acao_sem_repetir(self.banco, "REGAR_AGORA", "TERRA_SECA", leitura, ORIGEM_TESTE,
                                                   AGORA + timedelta(seconds=segundos)) for segundos in (0, 20, 59)]
        id_acao = retornos[0][0]
        self.assertGreater(id_acao, 0)
        self.assertEqual(retornos, [(id_acao, False), (id_acao, True), (id_acao, True)])
        self.assertEqual(self.contar("registro_acoes"), 1)
        # 61 s depois do registro gravado: novo toque, nova linha.
        novo, repetido = ret.registrar_acao_sem_repetir(self.banco, "REGAR_AGORA", "TERRA_SECA", leitura,
                                                        ORIGEM_TESTE, AGORA + timedelta(seconds=61))
        self.assertFalse(repetido)
        self.assertNotEqual(novo, id_acao)
        self.assertEqual(self.contar("registro_acoes"), 2)

    def test_toques_diferentes_nao_se_juntam(self) -> None:
        """Outra ação, outra causa ou outra leitura gravam linha nova; leitura ausente vale NULL."""
        leitura_1 = self.inserir(AGORA - timedelta(seconds=5))
        leitura_2 = self.inserir(AGORA - timedelta(seconds=2))
        chamadas = [("REGAR_AGORA", "TERRA_SECA", leitura_1), ("COBRIR_MUDAS", "TERRA_SECA", leitura_1),
                    ("REGAR_AGORA", "CALOR", leitura_1), ("REGAR_AGORA", "TERRA_SECA", leitura_2),
                    ("REGAR_AGORA", "TERRA_SECA", None)]
        for codigo, causa, leitura in chamadas:
            _, repetido = ret.registrar_acao_sem_repetir(self.banco, codigo, causa, leitura, ORIGEM_TESTE, AGORA)
            self.assertFalse(repetido, (codigo, causa, leitura))
        self.assertEqual(self.contar("registro_acoes"), len(chamadas))
        # Leitura que não existe é gravada como NULL e se junta ao toque sem leitura.
        _, repetido = ret.registrar_acao_sem_repetir(self.banco, "regar_agora", "terra_seca", 987654, ORIGEM_TESTE,
                                                     AGORA + timedelta(seconds=10))
        self.assertTrue(repetido)

    def test_validacao_e_janela_propria(self) -> None:
        """Ação ou causa fora do catálogo: ValueError; janela de 0 s grava sempre."""
        with self.assertRaises(ValueError):
            ret.registrar_acao_sem_repetir(self.banco, "VOAR", "TERRA_SECA", None, ORIGEM_TESTE, AGORA)
        with self.assertRaises(ValueError):
            ret.registrar_acao_sem_repetir(self.banco, "REGAR_AGORA", "CHUVA", None, ORIGEM_TESTE, AGORA)
        for _ in range(2):
            _, repetido = ret.registrar_acao_sem_repetir(self.banco, "REGAR_AGORA", "TERRA_SECA", None, ORIGEM_TESTE,
                                                         AGORA, janela_segundos=0)
            self.assertFalse(repetido)
        self.assertEqual(self.contar("registro_acoes"), 2)

    def test_toques_simultaneos(self) -> None:
        """Oito threads tocando ao mesmo tempo gravam um registro só."""
        resultados: List[Tuple[int, bool]] = []
        trava = threading.Lock()

        def tocar() -> None:
            """Um toque em "Feito"."""
            resultado = ret.registrar_acao_sem_repetir(self.banco, "FAZER_SOMBRA", "CALOR", None, ORIGEM_TESTE, AGORA)
            with trava:
                resultados.append(resultado)

        threads = [threading.Thread(target=tocar) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.contar("registro_acoes"), 1)
        self.assertEqual(sum(1 for _, repetido in resultados if not repetido), 1)
        self.assertEqual(len({id_acao for id_acao, _ in resultados}), 1)

    def test_registrar_acao_continua_igual(self) -> None:
        """``registrar_acao`` (assinatura e comportamento da v3) continua gravando cada toque."""
        for _ in range(3):
            ret.registrar_acao(self.banco, "REGAR_AGORA", "TERRA_SECA", None, ORIGEM_TESTE, AGORA)
        self.assertEqual(self.contar("registro_acoes"), 3)


# ---------------------------------------------------------------------------
# Demonstração fora do resumo do técnico
# ---------------------------------------------------------------------------
class TesteDemonstracaoForaDoResumo(BaseRetorno):
    """Pedidos e eventos gravados na demonstração não contaminam o resumo do técnico."""

    def _gravar_ajuda_e_ouviu(self, modo: str, quantidade: int = 2) -> None:
        """Com ``modo_operacao`` = ``modo``, grava pedidos de ajuda e eventos 'ouviu'."""
        definir_configuracao("modo_operacao", modo, caminho_banco=self.banco)
        for indice in range(quantidade):
            instante = AGORA - timedelta(hours=1, minutes=indice)
            ret.registrar_pedido_ajuda(self.banco, {"status_alerta": 2, "causa": "TERRA_SECA", "temperatura_ar": 20.0,
                                                    "umidade_solo": 30.0, "modo_operacao": modo}, instante)
            ret.registrar_evento(self.banco, "ouviu", "voz", instante)

    def test_demonstracao_nao_conta(self) -> None:
        """Na DEMONSTRACAO: pedidos 0 e 'ouviu' 0 no resumo; o texto do WhatsApp não fala em pedidos."""
        self._gravar_ajuda_e_ouviu(MODO_DEMONSTRACAO)
        resumo = ret.montar_resumo_tecnico(self.banco, agora_utc=AGORA)
        self.assertEqual(resumo["pedidos_ajuda"]["total"], 0)
        self.assertEqual(resumo["pedidos_ajuda"]["ultimos"], [])
        self.assertEqual(resumo["eventos_interface"]["ouviu"], 0)
        self.assertEqual(resumo["eventos_interface"]["chamou_tecnico"], 0)
        self.assertEqual(resumo["demonstracao_excluida"], {"pedidos_ajuda": 2, "eventos_interface": 4})
        self.assertNotIn("Pedidos de ajuda", resumo["texto_whatsapp"])
        self.assertNotIn("demonstração", resumo["texto_whatsapp"])
        self.assertEqual(self.consultar("SELECT DISTINCT demonstracao FROM pedido_ajuda"), [(1,)])
        self.assertEqual(self.consultar("SELECT DISTINCT demonstracao FROM eventos_interface"), [(1,)])
        # Com incluir_demonstracao=True os números aparecem e o texto avisa.
        completo = ret.montar_resumo_tecnico(self.banco, agora_utc=AGORA, incluir_demonstracao=True)
        self.assertEqual(completo["pedidos_ajuda"]["total"], 2)
        self.assertEqual(completo["eventos_interface"]["ouviu"], 2)
        self.assertIn("Pedidos de ajuda: 2.", completo["texto_whatsapp"])
        self.assertIn("Inclui a demonstração da banca.", completo["texto_whatsapp"])
        self.assertLessEqual(len(completo["texto_whatsapp"]), ret.TAMANHO_MAXIMO_TEXTO_WHATSAPP)

    def test_fora_da_demonstracao_continua_contando(self) -> None:
        """Em SIMULACAO (ou CAMPO_USB) os pedidos e eventos contam como antes."""
        self._gravar_ajuda_e_ouviu(MODO_SIMULACAO, quantidade=3)
        resumo = ret.montar_resumo_tecnico(self.banco, agora_utc=AGORA)
        self.assertEqual(resumo["pedidos_ajuda"]["total"], 3)
        self.assertEqual(resumo["eventos_interface"]["ouviu"], 3)
        self.assertEqual(resumo["eventos_interface"]["chamou_tecnico"], 3)
        self.assertEqual(resumo["demonstracao_excluida"], {"pedidos_ajuda": 0, "eventos_interface": 0})
        self.assertIn("Pedidos de ajuda: 3.", resumo["texto_whatsapp"])

    def test_mistura_conta_so_o_real(self) -> None:
        """Pedidos reais e da demonstração no mesmo período: só os reais contam."""
        self._gravar_ajuda_e_ouviu(MODO_SIMULACAO, quantidade=1)
        self._gravar_ajuda_e_ouviu(MODO_DEMONSTRACAO, quantidade=2)
        resumo = ret.montar_resumo_tecnico(self.banco, agora_utc=AGORA)
        self.assertEqual(resumo["pedidos_ajuda"]["total"], 1)
        self.assertEqual(resumo["eventos_interface"]["ouviu"], 1)

    def test_pedido_sobre_leitura_da_banca_e_feito_da_banca(self) -> None:
        """Leitura de origem DEMONSTRACAO marca o pedido, mesmo fora do modo; o "Feito" segue a regra da v3.

        O "Feito" da banca sai de ``acoes_feitas`` (pela origem da leitura). Integração v4 (ajuste
        justificado, meta 11 "0 eventos da demonstração no resumo"): o evento "feito" desse toque
        também sai e passa a ser contado em ``demonstracao_excluida``.
        """
        definir_configuracao("modo_operacao", MODO_SIMULACAO, caminho_banco=self.banco)
        leitura = self.inserir(AGORA - timedelta(minutes=5), origem=MODO_DEMONSTRACAO)
        ret.registrar_pedido_ajuda(self.banco, {"status_alerta": 2, "causa": "TERRA_SECA", "id_leitura": leitura},
                                   AGORA - timedelta(minutes=4))
        ret.registrar_acao(self.banco, "REGAR_AGORA", "TERRA_SECA", leitura, ORIGEM_TESTE, AGORA - timedelta(minutes=3))
        resumo = ret.montar_resumo_tecnico(self.banco, agora_utc=AGORA)
        self.assertEqual(resumo["pedidos_ajuda"]["total"], 0)
        self.assertEqual(resumo["eventos_interface"]["chamou_tecnico"], 0)
        self.assertEqual(resumo["acoes_feitas"]["total"], 0)
        self.assertEqual(resumo["eventos_interface"]["feito"], 0)
        self.assertEqual(resumo["demonstracao_excluida"]["eventos_interface"], 2)
        completo = ret.montar_resumo_tecnico(self.banco, agora_utc=AGORA, incluir_demonstracao=True)
        self.assertEqual(completo["acoes_feitas"]["total"], 1)
        self.assertEqual(completo["pedidos_ajuda"]["total"], 1)


# ---------------------------------------------------------------------------
# Migração de um banco da v3
# ---------------------------------------------------------------------------
class TesteMigracaoV3(unittest.TestCase):
    """Banco da v3 (tabelas do retorno sem ``demonstracao``) migra sem erro e sem perder linhas."""

    def setUp(self) -> None:
        """Cria um banco da v3 com um pedido e um evento antigos."""
        self._diretorio = tempfile.mkdtemp(prefix="agro_migracao_v4_")
        self.banco = Path(self._diretorio) / "v3.db"
        inicializar_banco(self.banco)
        conexao = sqlite3.connect(str(self.banco))
        try:
            conexao.executescript(DDL_RETORNO_V3)
            conexao.execute("INSERT INTO pedido_ajuda (instante_utc, status, causa) VALUES (?, 2, 'TERRA_SECA')",
                            ((AGORA - timedelta(hours=2)).isoformat(timespec="seconds"),))
            conexao.execute("INSERT INTO eventos_interface (tipo, instante_utc) VALUES ('ouviu', ?)",
                            ((AGORA - timedelta(hours=2)).isoformat(timespec="seconds"),))
            conexao.commit()
        finally:
            conexao.close()

    def tearDown(self) -> None:
        """Apaga o banco temporário."""
        shutil.rmtree(self._diretorio, ignore_errors=True)

    def _colunas(self, tabela: str) -> Dict[str, Tuple[Any, ...]]:
        """Colunas da tabela (nome → linha do PRAGMA)."""
        conexao = sqlite3.connect(str(self.banco))
        try:
            return {str(linha[1]): tuple(linha) for linha in conexao.execute(f"PRAGMA table_info({tabela})")}
        finally:
            conexao.close()

    def test_inicializar_duas_vezes(self) -> None:
        """``inicializar_banco`` duas vezes: a coluna nasce uma vez, com 0 nas linhas antigas, que contam no resumo."""
        self.assertNotIn("demonstracao", self._colunas("pedido_ajuda"))
        inicializar_banco(self.banco)
        inicializar_banco(self.banco)
        for tabela in ("pedido_ajuda", "eventos_interface"):
            coluna = self._colunas(tabela)["demonstracao"]
            self.assertEqual(coluna[2].upper(), "INTEGER")
            self.assertEqual(coluna[3], 1)  # NOT NULL
            self.assertEqual(str(coluna[4]), "0")  # DEFAULT 0
        ret.garantir_tabelas(self.banco)
        resumo = ret.montar_resumo_tecnico(self.banco, agora_utc=AGORA)
        self.assertEqual(resumo["pedidos_ajuda"]["total"], 1)
        self.assertEqual(resumo["eventos_interface"]["ouviu"], 1)

    def test_garantir_tabelas_migra_sem_inicializar(self) -> None:
        """O servidor que só chama ``garantir_tabelas`` também ganha a coluna (sem erro de coluna duplicada)."""
        ret._TABELAS_GARANTIDAS.clear()
        ret.garantir_tabelas(self.banco)
        self.assertIn("demonstracao", self._colunas("eventos_interface"))
        definir_configuracao("modo_operacao", MODO_DEMONSTRACAO, caminho_banco=self.banco)
        ret.registrar_evento(self.banco, "ouviu", "", AGORA - timedelta(minutes=1))
        resumo = ret.montar_resumo_tecnico(self.banco, agora_utc=AGORA)
        self.assertEqual(resumo["eventos_interface"]["ouviu"], 1)  # só o antigo, da v3


# ---------------------------------------------------------------------------
# PIN com código e contagem do bloqueio
# ---------------------------------------------------------------------------
class TestePinComCodigo(unittest.TestCase):
    """Constantes do PIN e ``LimitadorTentativas.segundos_restantes``."""

    def test_constantes(self) -> None:
        """Códigos distintos para PIN errado e PIN ausente; mensagem curta e sem 'Sim/Não'."""
        self.assertEqual(seguranca_banca.CODIGO_PIN_ERRADO, "pin_errado")
        self.assertEqual(seguranca_banca.CODIGO_PIN_NECESSARIO, "pin_necessario")
        self.assertEqual(seguranca_banca.MENSAGEM_PIN_ERRADO, "Senha errada. Tente de novo.")

    def test_segundos_restantes(self) -> None:
        """Antes de 5 erros: 0; depois: inteiro em [1, 60] que diminui; ao vencer: 0 e contagem zerada."""
        relogio = RelogioFalso()
        limitador = seguranca_banca.LimitadorTentativas(max_erros=5, bloqueio_s=60, relogio=relogio)
        ip = "10.0.0.7"
        for _ in range(4):
            limitador.registrar_erro(ip)
            self.assertEqual(limitador.segundos_restantes(ip), 0)
        limitador.registrar_erro(ip)
        self.assertTrue(limitador.bloqueado(ip))
        restante = limitador.segundos_restantes(ip)
        self.assertGreaterEqual(restante, 1)
        self.assertLessEqual(restante, 60)
        self.assertEqual(restante, 60)
        relogio.agora += 59.5
        self.assertEqual(limitador.segundos_restantes(ip), 1)
        relogio.agora += 0.5
        self.assertEqual(limitador.segundos_restantes(ip), 0)
        self.assertFalse(limitador.bloqueado(ip))
        limitador.registrar_erro(ip)  # a contagem recomeçou: um erro não bloqueia
        self.assertEqual(limitador.segundos_restantes(ip), 0)
        self.assertEqual(limitador.segundos_restantes("outro"), 0)

    def test_segundos_restantes_depois_de_limpar(self) -> None:
        """PIN certo (``limpar``) zera o bloqueio."""
        relogio = RelogioFalso()
        limitador = seguranca_banca.LimitadorTentativas(max_erros=2, bloqueio_s=30, relogio=relogio)
        limitador.registrar_erro("ip")
        limitador.registrar_erro("ip")
        self.assertEqual(limitador.segundos_restantes("ip"), 30)
        limitador.limpar("ip")
        self.assertEqual(limitador.segundos_restantes("ip"), 0)


if __name__ == "__main__":
    unittest.main()
