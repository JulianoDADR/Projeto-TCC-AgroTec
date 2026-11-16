"""Testes da Frente 1 da v3 — regras fisiológicas e medição de campo (unittest da biblioteca padrão).

Cobrem os critérios de aceite das quatro melhorias, sempre com banco SQLite
TEMPORÁRIO e sem rede:

    1. Motor térmico em camadas (modo ``fisiologica``): conforto na média de
       24 h (no máximo Amarelo), geada e calor fisiológicos, IA do instante
       neutralizada, demonstração na regra instantânea, série de inverno,
       propriedade "vetorizado = tempo real" e migração do banco v2;
    2. Banda morta no status gravado;
    3. Janela de florada e seca programada;
    4. Perfil do sensor de ar (firmware, banner, saneador, geada com DHT22),
       alimentação pulsada da sonda e calibração da sonda no host.

Execução (a partir da raiz do projeto): ``python -m unittest tests.test_regras_fisiologicas -v``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest import mock

RAIZ_PROJETO: Path = Path(__file__).resolve().parent.parent
PASTA_SRC: Path = RAIZ_PROJETO / "src"
if str(PASTA_SRC) not in sys.path:
    sys.path.insert(0, str(PASTA_SRC))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import coletor_serial  # noqa: E402
import database  # noqa: E402
import regras_agronomicas as ra  # noqa: E402
from coletor_serial import (  # noqa: E402
    ColetorAgroAnalise,
    MotorRegrasCampo,
    SaneadorTempoReal,
    calcular_checksum_nmea,
    calibrar_umidade_solo,
    carregar_modulo_calibracao,
    interpretar_linha_serial_detalhada,
    interpretar_perfil_firmware,
    montar_registro_leitura,
    processar_leitura,
)
from database import (  # noqa: E402
    ParametrosCultura,
    RegistroLeitura,
    buscar_historico,
    buscar_parametros_cultura,
    buscar_ultima_leitura,
    definir_configuracao,
    inicializar_banco,
    inserir_leitura,
    inserir_leituras_em_lote,
    migrar_esquema,
    obter_configuracao,
    obter_todas_configuracoes,
    semear_configuracoes_v3,
)
from gerador_dataset import (  # noqa: E402
    COLUNAS_FEATURES_MODELO,
    calcular_status_regra,
    classificar_variavel,
    gerar_mensagem_fisiologica,
)
from qualidade_sensores import EstadoSensores, FiltroMedianaMovel  # noqa: E402
from regras_agronomicas import (  # noqa: E402
    AvaliacaoRegra,
    ConfiguracaoTermica,
    ResumoTermico,
    avaliar_status_campo,
    banda_morta_padrao,
    buscar_resumo_termico,
    classificar_com_banda_morta,
    em_janela_florada,
    em_seca_programada,
    fase_florada,
    limiar_geada_vermelho,
    resumir_serie_termica,
    rotular_status_horario,
)

FORMATO: str = "%Y-%m-%d %H:%M:%S"
CFG_FISIOLOGICA: ConfiguracaoTermica = ConfiguracaoTermica(modo=ra.MODO_FISIOLOGICO)
RESUMO_MEDIA_19: ResumoTermico = ResumoTermico(media_24h=19.0, horas_com_dado=24, maxima_hoje=None, maxima_ontem=None)
OUTUBRO: datetime = datetime(2026, 10, 15, 14, 0)
FEVEREIRO: datetime = datetime(2027, 2, 10, 14, 0)
JULHO: datetime = datetime(2026, 7, 15, 10, 0)


def setUpModule() -> None:
    """Silencia logs informativos/avisos (erros continuam visíveis)."""
    logging.disable(logging.WARNING)


def tearDownModule() -> None:
    """Reativa os logs."""
    logging.disable(logging.NOTSET)


def trocas_de_cor(status: List[int]) -> int:
    """Quantas vezes a cor muda numa sequência de status."""
    return sum(1 for anterior, atual in zip(status, status[1:]) if anterior != atual)


def linha_v2(temperatura: str, umidade_ar: str, solo: str) -> str:
    """Linha do protocolo 2 com checksum correto (como o firmware envia)."""
    conteudo = f"2,{temperatura},{umidade_ar},{solo}"
    return f"${conteudo}*{calcular_checksum_nmea(conteudo)}"


def registro_exemplo(**alteracoes: Any) -> RegistroLeitura:
    """``RegistroLeitura`` válido (Verde, Café Arábica, agora) com campos alteráveis (construtor v1)."""
    dados: Dict[str, Any] = dict(
        data_hora=datetime.now().strftime(FORMATO), codigo_cultura="cafe_arabica", origem="CAMPO_USB",
        temperatura_ar_bruta=20.0, umidade_ar_bruta=70.0, umidade_solo_bruta=70.0, temperatura_ar=20.0,
        umidade_ar=70.0, umidade_solo=70.0, deficit_hidrico_solo=0.0, desvio_termico=0.0, status_regra=0,
        status_predito=0, status_alerta=0, probabilidade_predicao=1.0, flag_anomalia=0, descricao_anomalia="",
        mensagem_voz="Tudo bem.", versao_modelo="REGRAS",
    )
    dados.update(alteracoes)
    return RegistroLeitura(**dados)


class ModeloInstantaneoFalso:
    """IA do instante falsa: Vermelho (2) quando a temperatura recebida é < 15 °C, senão Verde."""

    classes_ = np.array([0, 1, 2])

    def predict(self, entrada: pd.DataFrame) -> np.ndarray:
        """Classe 2 abaixo de 15 °C."""
        return np.where(entrada["temperatura_ar"].to_numpy() < 15.0, 2, 0)

    def predict_proba(self, entrada: pd.DataFrame) -> np.ndarray:
        """Probabilidade 1 na classe prevista."""
        previsto = self.predict(entrada)
        return np.eye(3)[previsto]


def pacote_falso() -> Dict[str, Any]:
    """Pacote ``.pkl`` em memória com a IA falsa, treinada (de mentira) nos dois cafés."""
    return {"modelo": ModeloInstantaneoFalso(), "features": list(COLUNAS_FEATURES_MODELO), "versao": "FALSA_1",
            "culturas": ["cafe_arabica", "cafe_conilon"]}


class BaseBanco(unittest.TestCase):
    """Banco temporário e parâmetros dos dois cafés."""

    def setUp(self) -> None:
        """Cria e inicializa o banco temporário (sem as chaves da v3, como um banco v2)."""
        self._diretorio = tempfile.mkdtemp(prefix="agro_fisio_")
        self.pasta = Path(self._diretorio)
        self.banco: Path = self.pasta / "teste.db"
        inicializar_banco(self.banco)
        self.arabica: ParametrosCultura = buscar_parametros_cultura("cafe_arabica", caminho_banco=self.banco)
        self.conilon: ParametrosCultura = buscar_parametros_cultura("cafe_conilon", caminho_banco=self.banco)

    def tearDown(self) -> None:
        """Remove o diretório temporário."""
        shutil.rmtree(self._diretorio, ignore_errors=True)

    def leituras(self) -> List[Dict[str, Any]]:
        """Todas as leituras, da mais antiga para a mais recente."""
        return list(reversed(buscar_historico(limite=100000, caminho_banco=self.banco)))

    def ligar_fisiologica(self, **chaves: Any) -> None:
        """Semeia as chaves da v3 (regra fisiológica) e grava as chaves pedidas."""
        semear_configuracoes_v3(self.banco)
        for chave, valor in chaves.items():
            definir_configuracao(chave, valor, caminho_banco=self.banco)

    def avaliar(self, temperatura: float, solo: float = 70.0, resumo: ResumoTermico = ra.RESUMO_VAZIO,
                agora: datetime = JULHO, cfg: ConfiguracaoTermica = CFG_FISIOLOGICA,
                parametros: Optional[ParametrosCultura] = None,
                anterior: Optional[Tuple[int, int]] = None) -> AvaliacaoRegra:
        """Atalho para ``avaliar_status_campo`` no Arábica."""
        return avaliar_status_campo(temperatura, solo, parametros or self.arabica, resumo, cfg, agora,
                                    estado_anterior=anterior)


# ---------------------------------------------------------------------------
# 1. Motor térmico em camadas
# ---------------------------------------------------------------------------
class TesteMotorTermico(BaseBanco):
    """Critério 2 da melhoria 1: casos do Arábica com DHT11 no modo fisiológico."""

    def test_limiar_de_geada_dht11(self) -> None:
        """2,0 °C (Epamig/Embrapa) + 2,0 °C (DHT11) = 4,0 °C."""
        self.assertEqual(limiar_geada_vermelho(CFG_FISIOLOGICA), 4.0)

    def test_3_9_graus_e_geada_agora(self) -> None:
        """3,9 °C → 2 / GEADA_AGORA, mesmo com a média de 24 h ideal."""
        avaliacao = self.avaliar(3.9, resumo=RESUMO_MEDIA_19)
        self.assertEqual((avaliacao.status, avaliacao.motivo), (2, "GEADA_AGORA"))

    def test_4_1_graus_com_media_19_e_verde(self) -> None:
        """4,1 °C com média 24 h de 19 °C → 0 (madrugada fria sem geada num dia bom)."""
        avaliacao = self.avaliar(4.1, resumo=RESUMO_MEDIA_19)
        self.assertEqual((avaliacao.status, avaliacao.motivo, avaliacao.base_temperatura), (0, "NENHUM", "media_24h"))

    def test_media_14_e_dia_frio_nunca_vermelho(self) -> None:
        """Média 24 h de 14 °C → 1 / DIA_FRIO; nenhuma combinação sem geada chega a 2."""
        avaliacao = self.avaliar(14.0, resumo=ResumoTermico(14.0, 24, None, None))
        self.assertEqual((avaliacao.status, avaliacao.motivo), (1, "DIA_FRIO"))
        for media in np.arange(0.0, 18.0, 0.5):
            for leitura in np.arange(4.1, 20.0, 0.7):
                resultado = self.avaliar(float(leitura), resumo=ResumoTermico(float(media), 24, None, None))
                self.assertEqual(resultado.status, 1, (media, leitura))
                self.assertEqual(resultado.motivo, "DIA_FRIO")

    def test_12_graus_instantaneo_com_media_19_e_verde(self) -> None:
        """12 °C instantâneo com média 24 h de 19 °C → 0."""
        self.assertEqual(self.avaliar(12.0, resumo=RESUMO_MEDIA_19).status, 0)

    def test_critico_min_nao_gera_vermelho(self) -> None:
        """6 °C (abaixo do antigo crítico de 8 °C, acima da geada) sem histórico → 1 / DIA_FRIO."""
        avaliacao = self.avaliar(6.0)
        self.assertEqual((avaliacao.status, avaliacao.motivo, avaliacao.base_temperatura), (1, "DIA_FRIO", "instantanea"))
        self.assertEqual(calcular_status_regra(6.0, 70.0, self.arabica), 2)  # a regra v2 dava Vermelho

    def test_outubro_maxima_34_1_com_leitura_33_e_calor_florada(self) -> None:
        """Outubro, máxima de 34,1 °C e leitura de 33 °C → 2 / CALOR_FLORADA."""
        avaliacao = self.avaliar(33.0, resumo=ResumoTermico(24.0, 24, 34.1, 30.0), agora=OUTUBRO)
        self.assertEqual((avaliacao.status, avaliacao.motivo), (2, "CALOR_FLORADA"))

    def test_outubro_33_9_e_calor_dia(self) -> None:
        """Outubro, 33,9 °C (máxima do dia) → 1 / CALOR_DIA."""
        avaliacao = self.avaliar(33.9, resumo=ResumoTermico(24.0, 24, 33.0, 30.0), agora=OUTUBRO)
        self.assertEqual((avaliacao.status, avaliacao.motivo), (1, "CALOR_DIA"))

    def test_fevereiro_calor_seguido_no_segundo_dia(self) -> None:
        """Fevereiro, 34,1 °C no 1º dia → 1 / CALOR_DIA; no 2º dia seguido → 2 / CALOR_SEGUIDO."""
        primeiro = self.avaliar(34.1, resumo=ResumoTermico(24.0, 24, 31.0, 30.0), agora=FEVEREIRO)
        self.assertEqual((primeiro.status, primeiro.motivo), (1, "CALOR_DIA"))
        segundo = self.avaliar(34.1, resumo=ResumoTermico(24.0, 24, 31.0, 34.1), agora=FEVEREIRO + timedelta(days=1))
        self.assertEqual((segundo.status, segundo.motivo), (2, "CALOR_SEGUIDO"))

    def test_calor_vermelho_exige_leitura_acima_do_critico(self) -> None:
        """Depois do pico (máxima 34,5 °C), a leitura de 25 °C à noite volta a Amarelo (CALOR_DIA)."""
        avaliacao = self.avaliar(25.0, resumo=ResumoTermico(24.0, 24, 34.5, 34.5), agora=FEVEREIRO)
        self.assertEqual((avaliacao.status, avaliacao.motivo), (1, "CALOR_DIA"))

    def test_conilon_usa_o_proprio_critico(self) -> None:
        """Conilon (crítico 34 °C): 33 °C na florada sem dia de 34 °C → 0 pelo calor; 34,0 °C → 2."""
        conilon = self.avaliar(33.0, resumo=ResumoTermico(24.0, 24, 33.0, 30.0), agora=OUTUBRO,
                               parametros=self.conilon)
        self.assertEqual(conilon.status, 0)
        quente = self.avaliar(34.0, resumo=ResumoTermico(24.0, 24, 33.0, 30.0), agora=OUTUBRO, parametros=self.conilon)
        self.assertEqual((quente.status, quente.motivo), (2, "CALOR_FLORADA"))

    def test_menos_de_18_horas_usa_instantanea_e_no_maximo_amarelo(self) -> None:
        """Com 17 horas de histórico: base 'instantanea' e, sem geada nem calor, no máximo Amarelo."""
        agora = datetime(2026, 7, 15, 12, 0)
        instantes = [agora - timedelta(hours=hora) for hora in range(1, 18)]
        serie = pd.DataFrame({"data_hora": instantes, "temperatura_ar": [19.0] * len(instantes)})
        resumo = resumir_serie_termica(serie, agora)
        self.assertEqual((resumo.horas_com_dado, resumo.media_24h), (17, None))
        for leitura in (4.5, 10.0, 14.9, 25.5, 31.9):
            avaliacao = self.avaliar(leitura, resumo=resumo, agora=agora)
            self.assertEqual(avaliacao.base_temperatura, "instantanea")
            self.assertLessEqual(avaliacao.status, 1, leitura)
        serie_completa = pd.DataFrame({"data_hora": instantes + [agora - timedelta(hours=18)],
                                       "temperatura_ar": [19.0] * 18})
        self.assertEqual(resumir_serie_termica(serie_completa, agora).media_24h, 19.0)

    def test_modo_instantaneo_igual_a_regra_v2(self) -> None:
        """``cfg.modo='instantanea'`` devolve exatamente ``calcular_status_regra`` (as duas culturas)."""
        cfg = ConfiguracaoTermica()
        for parametros in (self.arabica, self.conilon):
            for temperatura in np.arange(0.0, 45.0, 0.7):
                for solo in np.arange(0.0, 100.0, 3.1):
                    avaliacao = self.avaliar(float(temperatura), float(solo), resumo=RESUMO_MEDIA_19, cfg=cfg,
                                             parametros=parametros)
                    self.assertEqual(avaliacao.status, calcular_status_regra(temperatura, solo, parametros))
                    self.assertIn(avaliacao.motivo, ra.MOTIVOS_REGRA)

    def test_mensagens_tem_palavras_chave_do_contrato(self) -> None:
        """Voz fisiológica: palavras-chave combinadas com os pictogramas da Frente 3."""
        casos: List[Tuple[AvaliacaoRegra, Tuple[str, ...]]] = [
            (self.avaliar(3.0, resumo=RESUMO_MEDIA_19), ("Cubra as mudas", "chegue terra")),
            (self.avaliar(33.0, resumo=ResumoTermico(24.0, 24, 34.1, 30.0), agora=OUTUBRO), ("fim da tarde", "sombra")),
            (self.avaliar(34.1, resumo=ResumoTermico(24.0, 24, 31.0, 34.1), agora=FEVEREIRO), ("fim da tarde",)),
            (self.avaliar(14.0, resumo=ResumoTermico(14.0, 24, None, None)), ("Não precisa fazer nada", "frio")),
            (self.avaliar(23.0, resumo=ResumoTermico(23.0, 24, None, None)), ("Não precisa fazer nada", "quente")),
            (self.avaliar(33.0, resumo=ResumoTermico(24.0, 24, 33.0, 30.0), agora=FEVEREIRO),
             ("Não precisa fazer nada",)),
        ]
        motivos = ["GEADA_AGORA", "CALOR_FLORADA", "CALOR_SEGUIDO", "DIA_FRIO", "DIA_QUENTE", "CALOR_DIA"]
        for (avaliacao, palavras), motivo in zip(casos, motivos):
            self.assertEqual(avaliacao.motivo, motivo)
            mensagem = gerar_mensagem_fisiologica(avaliacao, 0.0, self.arabica, avaliacao.temperatura_referencia, 70.0)
            for palavra in palavras:
                self.assertIn(palavra, mensagem, (motivo, mensagem))
        geada = gerar_mensagem_fisiologica(casos[0][0], 0.0, self.arabica, 3.0, 70.0)
        self.assertIn("Está gelando agora. Cubra as mudas e chegue terra no pé das plantas assim que puder.", geada)
        self.assertTrue(geada.startswith("Urgente!"))
        frio = gerar_mensagem_fisiologica(casos[3][0], 0.0, self.arabica, 14.0, 70.0)
        self.assertIn("O dia está frio para o café. Não precisa fazer nada agora.", frio)
        self.assertNotIn("geada", frio.lower())
        verde = gerar_mensagem_fisiologica(self.avaliar(20.0, resumo=RESUMO_MEDIA_19), 0.0, self.arabica, 20.0, 70.0)
        self.assertTrue(verde.startswith("Tudo bem"))

    def test_mensagem_combina_seca_e_frio_sem_contradicao(self) -> None:
        """Solo seco + dia frio: manda regar de manhã e NÃO diz 'Não precisa fazer nada'."""
        avaliacao = self.avaliar(14.0, 55.0, resumo=ResumoTermico(14.0, 24, None, None))
        self.assertEqual((avaliacao.status, avaliacao.motivo), (1, "SECA_AGORA"))
        mensagem = gerar_mensagem_fisiologica(avaliacao, 5.0, self.arabica, 14.0, 55.0)
        self.assertIn("de manhã", mensagem)
        self.assertNotIn("Não precisa fazer nada", mensagem)


class TesteResumoTermicoNoBanco(BaseBanco):
    """``buscar_resumo_termico`` (SQL) = ``resumir_serie_termica`` (pandas), com fallback no agregado."""

    def _gravar_serie(self, instantes: List[datetime], temperaturas: List[float], origem: str = "CAMPO_USB") -> None:
        """Grava leituras sintéticas (só temperatura varia)."""
        registros = [registro_exemplo(data_hora=instante.strftime(FORMATO), temperatura_ar=float(temperatura),
                                      origem=origem)
                     for instante, temperatura in zip(instantes, temperaturas)]
        inserir_leituras_em_lote(registros, caminho_banco=self.banco)

    def test_sql_igual_ao_pandas_com_dados_irregulares(self) -> None:
        """Leituras a cada 7 min com lacunas, por 50 h: os dois caminhos dão o mesmo resumo."""
        gerador = np.random.default_rng(7)
        agora = datetime(2026, 9, 3, 15, 37, 12)
        instantes = [agora - timedelta(minutes=7 * passo) for passo in range(1, 430)]
        instantes = [instante for indice, instante in enumerate(instantes) if not 60 <= indice < 90]
        temperaturas = np.round(20 + 8 * np.sin(np.arange(len(instantes)) / 30.0) + gerador.normal(0, 1, len(instantes)), 1)
        self._gravar_serie(instantes, list(temperaturas))
        self._gravar_serie(instantes[:50], [40.0] * 50, origem="SIMULACAO")  # outra origem não entra
        sql = buscar_resumo_termico("cafe_arabica", self.banco, agora, "CAMPO_USB")
        serie = pd.DataFrame({"data_hora": [instante.strftime(FORMATO) for instante in instantes],
                              "temperatura_ar": temperaturas})
        memoria = resumir_serie_termica(serie, agora)
        self.assertEqual(sql.horas_com_dado, memoria.horas_com_dado)
        self.assertGreaterEqual(sql.horas_com_dado, 18)
        for campo in ("media_24h", "maxima_hoje", "maxima_ontem"):
            self.assertAlmostEqual(getattr(sql, campo), getattr(memoria, campo), places=5, msg=campo)

    def test_fallback_no_agregado_horario(self) -> None:
        """Sem leituras brutas, as médias de ``historico_agregado_hora`` completam as 24 h."""
        agora = datetime(2026, 7, 15, 6, 0, 0)
        conexao = database.obter_conexao(self.banco)
        try:
            for horas in range(1, 30):
                hora = (agora - timedelta(hours=horas)).strftime("%Y-%m-%d %H:00:00")
                conexao.execute(
                    "INSERT INTO historico_agregado_hora (id_cultura, hora, origem, n, temperatura_min, temperatura_med, "
                    "temperatura_max, status_alerta_max, n_verde, n_amarelo, n_vermelho, n_anomalias) "
                    "VALUES (?, ?, 'CAMPO_USB', 720, 16.0, 17.0, 18.0, 1, 0, 720, 0, 0)",
                    (self.arabica.id_cultura, hora))
            conexao.commit()
        finally:
            conexao.close()
        resumo = buscar_resumo_termico("cafe_arabica", self.banco, agora, "CAMPO_USB")
        self.assertGreaterEqual(resumo.horas_com_dado, 18)
        self.assertAlmostEqual(resumo.media_24h or 0.0, 17.0)
        self.assertEqual(resumo.maxima_ontem, 17.0)

    def test_banco_sem_tabelas_devolve_resumo_vazio(self) -> None:
        """Banco inexistente/sem tabelas: resumo vazio, sem exceção (a regra usa a leitura instantânea)."""
        resumo = buscar_resumo_termico("cafe_arabica", self.pasta / "vazio.db", datetime.now(), "CAMPO_USB")
        self.assertEqual(resumo, ra.RESUMO_VAZIO)


class TestePipelineFisiologico(BaseBanco):
    """``processar_leitura`` com a regra fisiológica: IA neutralizada, demonstração instantânea, colunas novas."""

    def setUp(self) -> None:
        """Banco com as chaves da v3."""
        super().setUp()
        self.ligar_fisiologica()
        coletor_serial._FALHAS_MODULO_CALIBRACAO.clear()

    def _processar(self, temperatura: float, origem: str = "CAMPO_USB", **argumentos: Any) -> RegistroLeitura:
        """Uma leitura pelo pipeline completo com a IA falsa (sem calibração)."""
        with mock.patch.dict(sys.modules, {"calibracao_sonda": None}):
            _, registro = processar_leitura(temperatura, 70.0, 70.0, origem, "cafe_arabica",
                                            SaneadorTempoReal(self.arabica), pacote_falso(), caminho_banco=self.banco,
                                            **argumentos)
        return registro

    def test_ia_instantanea_neutralizada(self) -> None:
        """12 °C com IA falsa que daria 2: status_alerta 1 (regra DIA_FRIO, IA neutralizada em 20 °C)."""
        registro = self._processar(12.0)
        self.assertEqual((registro.status_regra, registro.status_predito, registro.status_alerta), (1, 0, 1))
        self.assertEqual((registro.motivo_regra, registro.base_temperatura), ("DIA_FRIO", "instantanea"))
        self.assertIn("Não precisa fazer nada", registro.mensagem_voz)
        gravada = buscar_ultima_leitura(caminho_banco=self.banco)
        assert gravada is not None
        self.assertEqual((gravada["status_alerta"], gravada["motivo_regra"], gravada["base_temperatura"]),
                         (1, "DIA_FRIO", "instantanea"))

    def test_demonstracao_usa_regra_instantanea(self) -> None:
        """Mesma leitura com origem DEMONSTRACAO: regra instantânea (2) e IA com a temperatura real (2)."""
        registro = self._processar(12.0, origem="DEMONSTRACAO")
        self.assertEqual((registro.status_regra, registro.status_predito, registro.status_alerta), (2, 2, 2))
        self.assertEqual(registro.base_temperatura, "instantanea")
        dataset = self._processar(12.0, origem="DATASET")
        self.assertEqual(dataset.status_regra, 2)

    def test_sem_chave_continua_instantanea(self) -> None:
        """Banco sem ``regra_temperatura`` (v2): 12 °C no campo continua Vermelho, como na v2."""
        outro = self.pasta / "v2.db"
        inicializar_banco(outro)
        with mock.patch.dict(sys.modules, {"calibracao_sonda": None}):
            _, registro = processar_leitura(12.0, 70.0, 70.0, "CAMPO_USB", "cafe_arabica",
                                            SaneadorTempoReal(self.arabica), None, caminho_banco=outro)
        self.assertEqual((registro.status_regra, registro.status_alerta), (2, 2))
        self.assertEqual(registro.motivo_regra, "DIA_FRIO")

    def test_media_24h_do_historico_gravado(self) -> None:
        """25 h de leituras a 19 °C no banco: 12 °C agora → Verde pela média de 24 h (gravada)."""
        agora = datetime(2026, 7, 15, 6, 0)
        instantes = [agora - timedelta(minutes=30 * passo) for passo in range(1, 51)]
        inserir_leituras_em_lote([registro_exemplo(data_hora=instante.strftime(FORMATO), temperatura_ar=19.0)
                                  for instante in instantes], caminho_banco=self.banco)
        registro = self._processar(12.0, agora=agora)
        self.assertEqual((registro.status_regra, registro.status_alerta), (0, 0))
        self.assertEqual((registro.base_temperatura, registro.temperatura_media_24h), ("media_24h", 19.0))
        self.assertEqual(registro.data_hora, agora.strftime(FORMATO))
        gravada = buscar_ultima_leitura(caminho_banco=self.banco)
        assert gravada is not None
        self.assertEqual((gravada["temperatura_media_24h"], gravada["base_temperatura"]), (19.0, "media_24h"))
        # A geada continua instantânea mesmo com a média ideal.
        geada = self._processar(3.5, agora=agora + timedelta(minutes=1))
        self.assertEqual((geada.status_alerta, geada.motivo_regra), (2, "GEADA_AGORA"))
        self.assertIn("Cubra as mudas", geada.mensagem_voz)

    def test_sensor_de_solo_suspeito_na_regra_fisiologica(self) -> None:
        """Solo suspeito + geada: Vermelho de geada (sensor de ar confiável) e aviso do sensor; solo não pesa."""
        estado = EstadoSensores(estado_solo="SUSPEITO", estado_ar="OK", descricao="solo:travado_60min")
        with mock.patch.dict(sys.modules, {"calibracao_sonda": None}):
            _, registro = processar_leitura(20.0, 70.0, 5.0, "CAMPO_USB", "cafe_arabica",
                                            SaneadorTempoReal(self.arabica), pacote_falso(), caminho_banco=self.banco,
                                            estado_sensores=estado)
        self.assertEqual((registro.status_alerta, registro.motivo_regra), (1, "NENHUM"))
        self.assertIn("sensor da terra", registro.mensagem_voz)
        with mock.patch.dict(sys.modules, {"calibracao_sonda": None}):
            _, frio = processar_leitura(3.0, 70.0, 5.0, "CAMPO_USB", "cafe_arabica", SaneadorTempoReal(self.arabica),
                                        pacote_falso(), caminho_banco=self.banco, estado_sensores=estado)
        self.assertEqual((frio.status_alerta, frio.motivo_regra), (2, "GEADA_AGORA"))
        self.assertIn("Cubra as mudas", frio.mensagem_voz)


class TesteSerieDeInverno(BaseBanco):
    """Critério 3: 60 dias de inverno (senoide de média 17 °C e amplitude 13 °C)."""

    def _serie(self) -> pd.DataFrame:
        """Série horária com a mínima às 5h30 (nenhuma hora cheia toca os 4,0 °C)."""
        instantes = pd.date_range("2026-06-15 00:00:00", periods=60 * 24, freq="h")
        horas = instantes.hour.to_numpy(dtype=float)
        temperatura = 17.0 + 13.0 * np.sin(2 * np.pi * (horas - 11.5) / 24.0)
        return pd.DataFrame({"data_hora": instantes, "temperatura_ar": np.round(temperatura, 2),
                             "umidade_solo": 70.0})

    def test_instantanea_pisca_e_fisiologica_nao(self) -> None:
        """Instantânea: > 4 trocas/dia e > 20 % Vermelho; fisiológica: 0 % Vermelho e < 2 trocas/dia."""
        serie = self._serie()
        dias = len(serie) / 24.0
        instantanea = rotular_status_horario(serie, self.arabica, ConfiguracaoTermica()).tolist()
        fisiologica = rotular_status_horario(serie, self.arabica).tolist()
        self.assertGreater(trocas_de_cor(instantanea) / dias, 4.0)
        self.assertGreater(instantanea.count(2) / len(instantanea), 0.20)
        self.assertEqual(fisiologica.count(2), 0)
        self.assertLess(trocas_de_cor(fisiologica) / dias, 2.0)
        self.assertEqual(rotular_status_horario(serie, self.arabica).index.tolist(), serie.index.tolist())


def serie_aleatoria(semente: int, inicio: str, media_inicio: float, media_fim: float, horas: int = 2000) -> pd.DataFrame:
    """Série horária realista com frentes frias (geada), ondas de calor e solo em passeio aleatório."""
    gerador = np.random.default_rng(semente)
    dias = horas // 24 + 2
    media_dia = np.linspace(media_inicio, media_fim, dias) + gerador.normal(0.0, 1.5, dias)
    amplitude = gerador.uniform(4.0, 9.0, dias)
    for _ in range(6):  # frentes frias de 2 dias
        dia = int(gerador.integers(0, dias - 2))
        media_dia[dia:dia + 2] -= gerador.uniform(7.0, 11.0)
    for _ in range(6):  # ondas de calor de 2 a 3 dias
        dia = int(gerador.integers(0, dias - 3))
        media_dia[dia:dia + 3] += gerador.uniform(6.0, 9.0)
    instantes = pd.date_range(inicio, periods=horas, freq="h")
    indice_dia = (np.arange(horas) // 24).astype(int)
    hora = instantes.hour.to_numpy(dtype=float)
    temperatura = media_dia[indice_dia] + amplitude[indice_dia] * np.sin(2 * np.pi * (hora - 9.0) / 24.0)
    temperatura = np.clip(np.round(temperatura + gerador.normal(0.0, 0.5, horas), 1), 0.0, 50.0)
    solo = np.clip(70.0 + np.cumsum(gerador.normal(0.0, 1.6, horas)), 20.0, 98.0).round(1)
    return pd.DataFrame({"data_hora": instantes, "temperatura_ar": temperatura, "umidade_solo": solo})


class TestePropriedadeVetorizadoTempoReal(BaseBanco):
    """Critério 4: ``rotular_status_horario`` e ``avaliar_status_campo`` concordam em 100 % das horas."""

    def _comparar(self, serie: pd.DataFrame, cfg: Optional[ConfiguracaoTermica]) -> Dict[str, int]:
        """Compara hora a hora e devolve a contagem dos motivos do caminho em tempo real."""
        rotulos = rotular_status_horario(serie, self.arabica, cfg)
        cfg_tempo_real = cfg or CFG_FISIOLOGICA
        motivos: Dict[str, int] = {}
        for posicao in range(len(serie)):
            linha = serie.iloc[posicao]
            agora = pd.Timestamp(linha["data_hora"]).to_pydatetime()
            resumo = resumir_serie_termica(serie.iloc[max(0, posicao - 48):posicao], agora)
            avaliacao = avaliar_status_campo(float(linha["temperatura_ar"]), float(linha["umidade_solo"]),
                                             self.arabica, resumo, cfg_tempo_real, agora)
            self.assertEqual(int(rotulos.iloc[posicao]), avaliacao.status, (posicao, agora, avaliacao))
            motivos[avaliacao.motivo] = motivos.get(avaliacao.motivo, 0) + 1
        return motivos

    def test_serie_florada_por_calendario(self) -> None:
        """Ago.–nov., padrão (florada por calendário, seca desligada): todos os motivos térmicos aparecem."""
        serie = serie_aleatoria(11, "2026-08-10 00:00:00", 17.0, 24.0)
        motivos = self._comparar(serie, None)
        for motivo in ("GEADA_AGORA", "CALOR_FLORADA", "CALOR_DIA", "DIA_FRIO", "NENHUM", "SECA_AGORA"):
            self.assertIn(motivo, motivos)

    def test_serie_verao_com_data_de_florada_e_seca_programada(self) -> None:
        """Dez.–mar. com florada em 01/12 e seca programada 01-20→02-20: CALOR_SEGUIDO e SECA_PROGRAMADA."""
        serie = serie_aleatoria(23, "2026-12-10 00:00:00", 24.0, 23.0)
        serie.loc[serie.index[600:1500], "umidade_solo"] = np.clip(serie["umidade_solo"].iloc[600:1500] - 25, 20, 98)
        cfg = ConfiguracaoTermica(modo=ra.MODO_FISIOLOGICO, data_florada_principal=date(2026, 12, 1),
                                  seca_programada_ativa=True, seca_programada_inicio="01-20",
                                  seca_programada_fim="02-20")
        motivos = self._comparar(serie, cfg)
        for motivo in ("CALOR_FLORADA", "CALOR_SEGUIDO", "SECA_PROGRAMADA"):
            self.assertIn(motivo, motivos)

    def test_rejeita_entrada_invalida(self) -> None:
        """Colunas faltando, fora de ordem ou NaN → ValueError."""
        serie = serie_aleatoria(1, "2026-08-10 00:00:00", 17.0, 24.0, horas=72)
        with self.assertRaises(ValueError):
            rotular_status_horario(serie.drop(columns=["umidade_solo"]), self.arabica)
        with self.assertRaises(ValueError):
            rotular_status_horario(serie.iloc[::-1], self.arabica)
        com_nan = serie.copy()
        com_nan.loc[com_nan.index[3], "temperatura_ar"] = np.nan
        with self.assertRaises(ValueError):
            rotular_status_horario(com_nan, self.arabica)


class TesteMigracaoV3(BaseBanco):
    """Critério 5 + compatibilidade: banco v2, construtores antigos e chaves da v3."""

    DDL_V2: str = """
    CREATE TABLE culturas (
        id_cultura INTEGER PRIMARY KEY AUTOINCREMENT, codigo TEXT NOT NULL UNIQUE, nome_comum TEXT NOT NULL,
        nome_cientifico TEXT, categoria TEXT NOT NULL, descricao TEXT, ativo INTEGER NOT NULL DEFAULT 1,
        criado_em TEXT NOT NULL DEFAULT (datetime('now','localtime')));
    CREATE TABLE historico_leituras (
        id_leitura INTEGER PRIMARY KEY AUTOINCREMENT, data_hora TEXT NOT NULL, id_cultura INTEGER NOT NULL,
        origem TEXT NOT NULL, temperatura_ar_bruta REAL, umidade_ar_bruta REAL, umidade_solo_bruta REAL,
        temperatura_ar REAL NOT NULL, umidade_ar REAL NOT NULL, umidade_solo REAL NOT NULL,
        deficit_hidrico_solo REAL NOT NULL, desvio_termico REAL NOT NULL, status_regra INTEGER NOT NULL,
        status_predito INTEGER NOT NULL, status_alerta INTEGER NOT NULL, probabilidade_predicao REAL NOT NULL,
        flag_anomalia INTEGER NOT NULL DEFAULT 0, descricao_anomalia TEXT NOT NULL DEFAULT '',
        mensagem_voz TEXT NOT NULL, versao_modelo TEXT NOT NULL,
        estado_sensor TEXT NOT NULL DEFAULT 'OK', diagnostico_sensor TEXT NOT NULL DEFAULT '', instante_utc TEXT);
    CREATE TABLE configuracao_sistema (
        chave TEXT PRIMARY KEY, valor TEXT NOT NULL, tipo TEXT NOT NULL, descricao TEXT NOT NULL,
        atualizado_em TEXT NOT NULL DEFAULT (datetime('now','localtime')));
    INSERT INTO culturas (codigo, nome_comum, nome_cientifico, categoria) VALUES
        ('cafe_arabica', 'Café Arábica', 'Coffea arabica', 'cafe');
    """

    def test_banco_v2_migra_e_e_idempotente(self) -> None:
        """Depois de migrar_esquema (2×): colunas novas existem, linhas antigas com padrão, nada duplicado."""
        caminho = self.pasta / "banco_v2.db"
        conexao = sqlite3.connect(caminho)
        try:
            conexao.executescript(self.DDL_V2)
            for hora in range(3):
                conexao.execute(
                    "INSERT INTO historico_leituras (data_hora, id_cultura, origem, temperatura_ar, umidade_ar, "
                    "umidade_solo, deficit_hidrico_solo, desvio_termico, status_regra, status_predito, status_alerta, "
                    "probabilidade_predicao, mensagem_voz, versao_modelo, instante_utc) "
                    "VALUES (?, 1, 'CAMPO_USB', 20, 70, 70, 0, 0, 0, 0, 0, 1.0, 'Tudo bem.', 'REGRAS', ?)",
                    (f"2026-01-01 0{hora}:00:00", f"2026-01-01T0{hora}:00:00+00:00"))
            conexao.commit()
        finally:
            conexao.close()
        conexao = database.obter_conexao(caminho)
        try:
            migrar_esquema(conexao)
            migrar_esquema(conexao)
            colunas = [linha[1] for linha in conexao.execute("PRAGMA table_info(historico_leituras)")]
            linhas = [dict(linha) for linha in conexao.execute("SELECT * FROM historico_leituras")]
        finally:
            conexao.close()
        novas = ["motivo_regra", "base_temperatura", "temperatura_media_24h", "umidade_solo_firmware"]
        self.assertEqual(colunas[-4:], novas)
        for coluna in novas:
            self.assertEqual(colunas.count(coluna), 1)
        self.assertEqual(len(linhas), 3)
        for linha in linhas:
            self.assertEqual((linha["motivo_regra"], linha["base_temperatura"], linha["temperatura_media_24h"],
                              linha["umidade_solo_firmware"]), ("", "", None, None))

    def test_banco_novo_mantem_ordem_das_colunas_v2(self) -> None:
        """Banco novo: as colunas da v3 entram ANTES das três da v2 (que continuam as últimas)."""
        conexao = database.obter_conexao(self.banco)
        try:
            colunas = [linha[1] for linha in conexao.execute("PRAGMA table_info(historico_leituras)")]
        finally:
            conexao.close()
        self.assertEqual(colunas[-3:], ["estado_sensor", "diagnostico_sensor", "instante_utc"])
        self.assertTrue({"motivo_regra", "base_temperatura", "temperatura_media_24h", "umidade_solo_firmware"}
                        <= set(colunas))

    def test_registro_v1_valido_e_campos_novos_gravados(self) -> None:
        """Construtor v1 continua valendo; os campos novos são gravados e validados."""
        antigo = registro_exemplo()
        self.assertEqual((antigo.motivo_regra, antigo.base_temperatura, antigo.temperatura_media_24h,
                          antigo.umidade_solo_firmware), ("", "", None, None))
        inserir_leitura(antigo, caminho_banco=self.banco)
        novo = registro_exemplo(motivo_regra="GEADA_AGORA", base_temperatura="media_24h", temperatura_media_24h=17.25,
                                umidade_solo_firmware=float("nan"))
        inserir_leitura(novo, caminho_banco=self.banco)
        gravada = buscar_ultima_leitura(caminho_banco=self.banco)
        assert gravada is not None
        self.assertEqual((gravada["motivo_regra"], gravada["base_temperatura"], gravada["temperatura_media_24h"],
                          gravada["umidade_solo_firmware"]), ("GEADA_AGORA", "media_24h", 17.25, None))
        for invalido in (dict(motivo_regra="geada; DROP"), dict(base_temperatura="semanal"),
                         dict(temperatura_media_24h=float("inf"))):
            with self.assertRaises(ValueError):
                inserir_leitura(registro_exemplo(**invalido), caminho_banco=self.banco)

    def test_chaves_v3_semeadas_sem_sobrescrever(self) -> None:
        """inicializar_banco não cria as chaves da v3; semear_configuracoes_v3 cria e não sobrescreve."""
        self.assertIsNone(obter_configuracao("regra_temperatura", caminho_banco=self.banco))
        self.assertEqual(len(obter_todas_configuracoes(caminho_banco=self.banco)), 12)
        definir_configuracao("regra_temperatura", "instantanea", caminho_banco=self.banco)
        semear_configuracoes_v3(self.banco)
        semear_configuracoes_v3(self.banco)
        configuracoes = obter_todas_configuracoes(caminho_banco=self.banco)
        self.assertEqual(configuracoes["regra_temperatura"], "instantanea")
        esperado = {"limiar_geada_abrigo_c": 2.0, "incerteza_sensor_ar_c": 2.0, "limiar_calor_dano_c": 34.0,
                    "dias_janela_florada": 45, "data_florada_principal": "", "seca_programada_ativa": False,
                    "seca_programada_inicio": "06-24", "seca_programada_fim": "09-04", "banda_morta_ativa": True,
                    "banda_morta_fracao": 0.03, "banda_morta_atraso_s": 60.0, "sensor_ar_modelo": "DHT11", "prognostico_suspenso": False,
                    "coef_tmin_local": "", "platt_local": "", "regiao_referencia": "", "alarme_local_ativo": True,
                    "alarme_silencio_inicio": "21:30", "alarme_silencio_fim": "05:00", "numero_whatsapp_tecnico": ""}
        for chave, valor in esperado.items():
            self.assertEqual(configuracoes[chave], valor, chave)

    def test_coletor_semeia_fisiologica(self) -> None:
        """O coletor, ao iniciar, semeia ``regra_temperatura='fisiologica'`` e grava motivo e base."""
        coletor = ColetorAgroAnalise(caminho_banco=self.banco, forcar_simulacao=True, intervalo_segundos=0.2,
                                     max_iteracoes=3)
        coletor.caminho_modelo = self.pasta / "sem_modelo.pkl"
        coletor.modulo_antecipacao = None
        coletor.executar()
        self.assertEqual(obter_configuracao("regra_temperatura", caminho_banco=self.banco), "fisiologica")
        self.assertEqual(coletor.motor_regras.configuracao.modo, "fisiologica")
        self.assertTrue(coletor.motor_regras.configuracao.banda_morta_ativa)
        leituras = self.leituras()
        self.assertEqual(len(leituras), 3)
        for leitura in leituras:
            self.assertIn(leitura["motivo_regra"], ra.MOTIVOS_REGRA)
            self.assertIn(leitura["base_temperatura"], ra.BASES_TEMPERATURA)
        # O técnico pode voltar à regra instantânea.
        definir_configuracao("regra_temperatura", "instantanea", caminho_banco=self.banco)
        coletor_v2 = ColetorAgroAnalise(caminho_banco=self.banco, forcar_simulacao=True, intervalo_segundos=0.2,
                                        max_iteracoes=1)
        coletor_v2.caminho_modelo = self.pasta / "sem_modelo.pkl"
        coletor_v2.modulo_antecipacao = None
        coletor_v2.executar()
        self.assertEqual(coletor_v2.motor_regras.configuracao.modo, "instantanea")


# ---------------------------------------------------------------------------
# 2. Banda morta
# ---------------------------------------------------------------------------
class TesteBandaMorta(BaseBanco):
    """Banda morta no status gravado: piora imediata, melhora com folga, geada sem banda."""

    def motor(self, banda: bool = True) -> MotorRegrasCampo:
        """Motor fisiológico com (ou sem) banda morta."""
        return MotorRegrasCampo(ConfiguracaoTermica(modo=ra.MODO_FISIOLOGICO, banda_morta_ativa=banda))

    def test_bandas_padrao(self) -> None:
        """3 % da faixa crítica: 0,72 °C no Arábica e 1,8 pp no solo."""
        self.assertAlmostEqual(banda_morta_padrao(self.arabica.faixa("temperatura_ar"), 0.03), 0.72)
        self.assertAlmostEqual(banda_morta_padrao(self.arabica.faixa("umidade_solo"), 0.03), 1.8)

    def test_piora_imediata_e_melhora_com_folga(self) -> None:
        """61 → 59: Amarelo na mesma leitura; 59 → 60,5 continua Amarelo; 59 → 62 volta a Verde.

        Na função de classificação a volta é imediata; no motor do coletor ela
        ainda espera o off-delay de 60 s de melhora contínua (ISA-18.2).
        """
        faixa = self.arabica.faixa("umidade_solo")
        self.assertEqual(classificar_com_banda_morta(59.0, faixa, 0, 1.8), 1)
        self.assertEqual(classificar_com_banda_morta(60.5, faixa, 1, 1.8), 1)
        self.assertEqual(classificar_com_banda_morta(61.7, faixa, 1, 1.8), 1)
        self.assertEqual(classificar_com_banda_morta(62.0, faixa, 1, 1.8), 0)
        self.assertEqual(classificar_com_banda_morta(62.0, faixa, 2, 1.8), 0)
        self.assertEqual(classificar_com_banda_morta(48.5, faixa, 2, 1.8), 2)  # 50 − ... precisa de 51,8 para Amarelo

        motor = self.motor()

        def avaliar(solo: float, segundos: float) -> int:
            """Uma leitura de solo a 20 °C pelo motor, ``segundos`` depois de JULHO."""
            return motor.avaliar(20.0, solo, self.arabica, "CAMPO_USB", self.banco,
                                 JULHO + timedelta(seconds=segundos)).status
        self.assertEqual(avaliar(61.0, 0), 0)
        self.assertEqual(avaliar(59.0, 5), 1)      # piora imediata
        self.assertEqual(avaliar(60.5, 10), 1)     # dentro da banda: continua Amarelo
        self.assertEqual(avaliar(62.0, 15), 1)     # melhora com folga começa a contar
        self.assertEqual(avaliar(62.0, 74), 1)     # 59 s de melhora: ainda Amarelo
        self.assertEqual(avaliar(62.0, 75), 0)     # 60 s: volta a Verde
        self.assertEqual(avaliar(59.0, 80), 1)
        self.assertEqual(avaliar(62.0, 85), 1)
        self.assertEqual(avaliar(61.0, 100), 1)    # perdeu a folga: a contagem recomeça
        self.assertEqual(avaliar(62.0, 110), 1)
        self.assertEqual(avaliar(62.0, 169), 1)
        self.assertEqual(avaliar(62.0, 170), 0)
        imediato = MotorRegrasCampo(ConfiguracaoTermica(modo=ra.MODO_FISIOLOGICO, banda_morta_ativa=True,
                                                        banda_morta_atraso_s=0.0))
        self.assertEqual(imediato.avaliar(20.0, 59.0, self.arabica, "CAMPO_USB", self.banco, JULHO).status, 1)
        self.assertEqual(imediato.avaliar(20.0, 62.0, self.arabica, "CAMPO_USB", self.banco, JULHO).status, 0)

    def test_geada_nao_espera_a_banda(self) -> None:
        """Anterior Verde com média ideal: 3,9 °C dá Vermelho de geada na hora."""
        motor = self.motor()
        self.assertEqual(motor.avaliar(20.0, 70.0, self.arabica, "CAMPO_USB", self.banco, JULHO).status, 0)
        avaliacao = motor.avaliar(3.9, 70.0, self.arabica, "CAMPO_USB", self.banco, JULHO)
        self.assertEqual((avaliacao.status, avaliacao.motivo), (2, "GEADA_AGORA"))

    def test_sem_banda_igual_a_classificar_variavel(self) -> None:
        """``banda_morta_ativa=false``: resultado idêntico a ``classificar_variavel`` (sem histerese)."""
        gerador = np.random.default_rng(3)
        faixa = self.arabica.faixa("umidade_solo")
        motor = self.motor(banda=False)
        for valor in np.round(gerador.uniform(20.0, 99.0, 400), 1):
            self.assertEqual(motor.avaliar(20.0, float(valor), self.arabica, "CAMPO_USB", self.banco, JULHO).status_solo,
                             classificar_variavel(float(valor), faixa))
            self.assertEqual(classificar_com_banda_morta(float(valor), faixa, None, 1.8),
                             classificar_variavel(float(valor), faixa))
        self.assertEqual(motor.estados_anteriores, {})

    def test_propriedade_banda_nunca_atrasa_a_piora(self) -> None:
        """Para qualquer anterior: resultado ≥ classificação crua e ≤ max(anterior, crua)."""
        gerador = np.random.default_rng(5)
        faixa = self.arabica.faixa("umidade_solo")
        for valor in np.round(gerador.uniform(0.0, 100.0, 2000), 1):
            for anterior in (0, 1, 2):
                cru = classificar_variavel(float(valor), faixa)
                resultado = classificar_com_banda_morta(float(valor), faixa, anterior, 1.8)
                self.assertGreaterEqual(resultado, cru)
                self.assertLessEqual(resultado, max(anterior, cru))

    def test_mediana_e_banda_20_sementes(self) -> None:
        """Solo a 1 pp da borda (60 %) com σ = 1 pp, mediana 12 e banda: ≤ 1 troca gravada em 720, 20 sementes.

        Controle: sem o off-delay (só a banda de 1,8 pp) ao menos uma semente troca 2 vezes.
        """
        piores: Dict[float, int] = {}
        for solo_verdadeiro in (61.0, 59.0):
            for semente in range(20):
                gerador = np.random.default_rng(semente)
                valores = np.round(solo_verdadeiro + gerador.normal(0.0, 1.0, 720), 1)
                saneador = SaneadorTempoReal(self.arabica, plausibilidade_fisica=True, limite_hold_last=30)
                filtro = FiltroMedianaMovel(janela=12)
                motor = self.motor()
                estado = EstadoSensores()
                status: List[int] = []
                with mock.patch.dict(sys.modules, {"calibracao_sonda": None}):
                    for indice, valor in enumerate(valores):  # uma leitura a cada 5 s, como no campo
                        registro, _ = montar_registro_leitura(20.0, 70.0, float(valor), "CAMPO_USB", "cafe_arabica",
                                                              saneador, None, self.banco, filtro=filtro,
                                                              estado_sensores=estado, motor_regras=motor,
                                                              agora=JULHO + timedelta(seconds=5 * indice))
                        status.append(int(registro.status_alerta))
                piores[solo_verdadeiro] = max(piores.get(solo_verdadeiro, 0), trocas_de_cor(status))
                self.assertLessEqual(trocas_de_cor(status), 1, (solo_verdadeiro, semente))
        self.assertLessEqual(max(piores.values()), 1)
        # Controle: só a banda (sem off-delay), semente 5 a 61 %: o semáforo gravado troca 2 vezes.
        gerador = np.random.default_rng(5)
        valores = np.round(61.0 + gerador.normal(0.0, 1.0, 720), 1)
        saneador = SaneadorTempoReal(self.arabica, plausibilidade_fisica=True, limite_hold_last=30)
        filtro = FiltroMedianaMovel(janela=12)
        so_banda = MotorRegrasCampo(ConfiguracaoTermica(modo=ra.MODO_FISIOLOGICO, banda_morta_ativa=True,
                                                        banda_morta_atraso_s=0.0))
        status_controle: List[int] = []
        with mock.patch.dict(sys.modules, {"calibracao_sonda": None}):
            for indice, valor in enumerate(valores):
                registro, _ = montar_registro_leitura(20.0, 70.0, float(valor), "CAMPO_USB", "cafe_arabica", saneador,
                                                      None, self.banco, filtro=filtro, estado_sensores=EstadoSensores(),
                                                      motor_regras=so_banda,
                                                      agora=JULHO + timedelta(seconds=5 * indice))
                status_controle.append(int(registro.status_alerta))
        self.assertGreaterEqual(trocas_de_cor(status_controle), 2)

    def test_gravado_no_banco_com_banda(self) -> None:
        """Série gravada por processar_leitura (semente 5, 61 %, a pior só com a banda): no máximo 1 troca."""
        gerador = np.random.default_rng(5)
        valores = np.round(61.0 + gerador.normal(0.0, 1.0, 720), 1)
        saneador = SaneadorTempoReal(self.arabica, plausibilidade_fisica=True, limite_hold_last=30)
        filtro = FiltroMedianaMovel(janela=12)
        motor = self.motor()
        with mock.patch.dict(sys.modules, {"calibracao_sonda": None}):
            for indice, valor in enumerate(valores):
                processar_leitura(20.0, 70.0, float(valor), "CAMPO_USB", "cafe_arabica", saneador, None,
                                  caminho_banco=self.banco, filtro=filtro, estado_sensores=EstadoSensores(),
                                  motor_regras=motor, agora=JULHO + timedelta(seconds=5 * indice))
        status = [int(leitura["status_alerta"]) for leitura in self.leituras()]
        self.assertEqual(len(status), 720)
        self.assertLessEqual(trocas_de_cor(status), 1)

    def test_memoria_semeada_pelo_banco_e_zerada_na_troca_de_cultura(self) -> None:
        """Motor novo parte da última leitura gravada (solo 59 % → Amarelo); trocar de cultura zera."""
        inserir_leitura(registro_exemplo(umidade_solo=59.0, status_regra=1, status_alerta=1),
                        caminho_banco=self.banco)
        motor = self.motor()
        agora = datetime.now()
        self.assertEqual(motor.avaliar(20.0, 60.5, self.arabica, "CAMPO_USB", self.banco, agora).status_solo, 1)
        self.assertEqual(motor.estados_anteriores, {("cafe_arabica", "CAMPO_USB"): (0, 1)})
        motor.avaliar(24.0, 60.5, self.conilon, "CAMPO_USB", self.banco, agora)
        self.assertNotIn(("cafe_arabica", "CAMPO_USB"), motor.estados_anteriores)

    def test_demonstracao_sem_banda(self) -> None:
        """Demonstração: regra instantânea sem banda, mesmo com o motor com banda."""
        motor = self.motor()
        self.assertEqual(motor.avaliar(20.0, 59.0, self.arabica, "DEMONSTRACAO", self.banco, JULHO).status, 1)
        self.assertEqual(motor.avaliar(20.0, 60.5, self.arabica, "DEMONSTRACAO", self.banco, JULHO).status, 0)
        self.assertEqual(motor.avaliar(12.0, 70.0, self.arabica, "DEMONSTRACAO", self.banco, JULHO).status, 2)


# ---------------------------------------------------------------------------
# 3. Florada e seca programada
# ---------------------------------------------------------------------------
class TesteFloradaESecaProgramada(BaseBanco):
    """Janela de florada (data ou calendário) e seca programada opcional."""

    SECA: ConfiguracaoTermica = ConfiguracaoTermica(modo=ra.MODO_FISIOLOGICO, seca_programada_ativa=True)

    def test_seca_programada_em_julho(self) -> None:
        """15/07, solo 45 % → 1 / SECA_PROGRAMADA com 'Não regue'; 30 % → 2; em 15/10 → 2 / SECA_AGORA."""
        avaliacao = self.avaliar(20.0, 45.0, resumo=RESUMO_MEDIA_19, agora=datetime(2026, 7, 15, 10), cfg=self.SECA)
        self.assertEqual((avaliacao.status, avaliacao.motivo), (1, "SECA_PROGRAMADA"))
        mensagem = gerar_mensagem_fisiologica(avaliacao, 15.0, self.arabica, 20.0, 45.0)
        self.assertIn("Não regue", mensagem)
        self.assertIn("A terra está seca de propósito para a florada.", mensagem)
        self.assertNotIn("Regue agora", mensagem)
        muito_seca = self.avaliar(20.0, 30.0, resumo=RESUMO_MEDIA_19, agora=datetime(2026, 7, 15, 10), cfg=self.SECA)
        self.assertEqual((muito_seca.status, muito_seca.motivo), (2, "SECA_AGORA"))
        fora = self.avaliar(20.0, 45.0, resumo=RESUMO_MEDIA_19, agora=datetime(2026, 10, 15, 10), cfg=self.SECA)
        self.assertEqual((fora.status, fora.motivo), (2, "SECA_AGORA"))
        desligada = self.avaliar(20.0, 45.0, resumo=RESUMO_MEDIA_19, agora=datetime(2026, 7, 15, 10))
        self.assertEqual((desligada.status, desligada.motivo), (2, "SECA_AGORA"))
        encharcada = self.avaliar(20.0, 96.0, resumo=RESUMO_MEDIA_19, agora=datetime(2026, 7, 15, 10), cfg=self.SECA)
        self.assertEqual((encharcada.status, encharcada.motivo), (2, "ENCHARCAMENTO"))

    def test_data_de_florada_principal(self) -> None:
        """Florada em 2026-12-01: 34,1 °C em 20/12 → CALOR_FLORADA; em 05/10 → 1 no 1º dia."""
        cfg = ConfiguracaoTermica(modo=ra.MODO_FISIOLOGICO, data_florada_principal=date(2026, 12, 1))
        dezembro = self.avaliar(34.1, resumo=ResumoTermico(25.0, 24, 30.0, 30.0), agora=datetime(2026, 12, 20, 14),
                                cfg=cfg)
        self.assertEqual((dezembro.status, dezembro.motivo), (2, "CALOR_FLORADA"))
        outubro = self.avaliar(34.1, resumo=ResumoTermico(25.0, 24, 30.0, 30.0), agora=datetime(2026, 10, 5, 14),
                               cfg=cfg)
        self.assertEqual((outubro.status, outubro.motivo), (1, "CALOR_DIA"))
        self.assertTrue(em_janela_florada(date(2027, 1, 15), cfg))   # 01/12 + 45 dias
        self.assertFalse(em_janela_florada(date(2027, 1, 16), cfg))
        self.assertTrue(em_janela_florada(date(2027, 12, 3), cfg))   # repete no ano seguinte
        self.assertEqual(fase_florada(date(2026, 12, 20), cfg), "FLORADA")
        self.assertEqual(fase_florada(date(2026, 10, 5), cfg), "FORA_FLORADA")
        self.assertEqual(fase_florada(date(2026, 10, 5), CFG_FISIOLOGICA), "FLORADA")  # sem data: set.–nov.

    def test_janela_que_cruza_o_ano(self) -> None:
        """Seca programada 11-15 → 02-10 é reconhecida nos dois anos."""
        cfg = ConfiguracaoTermica(modo=ra.MODO_FISIOLOGICO, seca_programada_ativa=True,
                                  seca_programada_inicio="11-15", seca_programada_fim="02-10")
        dentro = [date(2026, 11, 15), date(2026, 12, 25), date(2027, 1, 31), date(2027, 2, 10)]
        fora = [date(2026, 11, 14), date(2027, 2, 11), date(2026, 7, 1)]
        self.assertTrue(all(em_seca_programada(dia, cfg) for dia in dentro))
        self.assertFalse(any(em_seca_programada(dia, cfg) for dia in fora))
        self.assertFalse(em_seca_programada(date(2026, 12, 25), replace(cfg, seca_programada_ativa=False)))

    def test_configuracao_escrita_pela_banca(self) -> None:
        """Chaves escritas por definir_configuracao são lidas; valor inválido → padrão com aviso no log."""
        self.ligar_fisiologica(data_florada_principal="2026-12-01", seca_programada_ativa=True,
                               seca_programada_inicio="07-01", seca_programada_fim="08-31")
        cfg = ra.carregar_configuracao_termica(self.banco)
        self.assertEqual((cfg.modo, cfg.data_florada_principal, cfg.seca_programada_ativa, cfg.seca_programada_inicio,
                          cfg.seca_programada_fim, cfg.banda_morta_ativa),
                         ("fisiologica", date(2026, 12, 1), True, "07-01", "08-31", True))
        definir_configuracao("data_florada_principal", "31/12/2026", caminho_banco=self.banco)
        definir_configuracao("seca_programada_fim", "13-40", caminho_banco=self.banco)
        definir_configuracao("regra_temperatura", "semanal", caminho_banco=self.banco)
        logging.disable(logging.NOTSET)
        try:
            with self.assertLogs("agroanalise.regras_agronomicas", level="WARNING") as capturado:
                cfg_invalida = ra.carregar_configuracao_termica(self.banco)
        finally:
            logging.disable(logging.WARNING)
        self.assertEqual((cfg_invalida.data_florada_principal, cfg_invalida.seca_programada_fim, cfg_invalida.modo),
                         (None, "09-04", "instantanea"))
        self.assertGreaterEqual(len(capturado.output), 3)
        self.assertEqual(ra.carregar_configuracao_termica(self.pasta / "sem_chaves.db"), ConfiguracaoTermica())

    def test_coletor_recarrega_a_cada_ciclo(self) -> None:
        """Banca grava a florada e liga a seca: o motor do coletor muda na próxima leitura da configuração."""
        self.ligar_fisiologica()
        coletor = ColetorAgroAnalise(caminho_banco=self.banco, forcar_simulacao=True, intervalo_segundos=0.2,
                                     max_iteracoes=1)
        coletor._atualizar_configuracoes()
        self.assertFalse(coletor.motor_regras.configuracao.seca_programada_ativa)
        definir_configuracao("seca_programada_ativa", True, caminho_banco=self.banco)
        definir_configuracao("data_florada_principal", "2026-12-01", caminho_banco=self.banco)
        coletor._atualizar_configuracoes()
        self.assertTrue(coletor.motor_regras.configuracao.seca_programada_ativa)
        self.assertEqual(coletor.motor_regras.configuracao.data_florada_principal, date(2026, 12, 1))


# ---------------------------------------------------------------------------
# 4. Perfil do sensor de ar, sonda pulsada e calibração
# ---------------------------------------------------------------------------
@unittest.skipUnless(shutil.which("g++"), "g++ não instalado")
class TesteFirmwareV3(unittest.TestCase):
    """Firmware: DHT11/DHT22, alimentação pulsada e banner com o perfil dos sensores."""

    def _sintaxe(self, *extras: str) -> subprocess.CompletedProcess:
        """``g++ -fsyntax-only`` com os stubs (mesmo comando dos testes da v2)."""
        return subprocess.run(["g++", "-fsyntax-only", "-Wall", "-Wextra", *extras, "-x", "c++",
                               "-I", str(RAIZ_PROJETO / "tests" / "stubs_arduino"),
                               str(RAIZ_PROJETO / "arduino" / "coleta_sensores.ino")],
                              capture_output=True, text=True, timeout=120, check=False)

    def test_sintaxe_nas_tres_combinacoes(self) -> None:
        """Padrão, DHT22 e sonda pulsada no D7 (e as duas juntas com -D__AVR__) compilam."""
        for extras in ((), ("-DAGRO_SENSOR_AR=AGRO_AR_DHT22",), ("-DAGRO_PINO_ALIMENTACAO_SOLO=7",),
                       ("-D__AVR__", "-DAGRO_SENSOR_AR=AGRO_AR_DHT22", "-DAGRO_PINO_ALIMENTACAO_SOLO=7")):
            resultado = self._sintaxe(*extras)
            self.assertEqual(resultado.returncode, 0, (extras, resultado.stderr))

    def _executar_no_host(self, *extras: str) -> Tuple[List[str], str]:
        """Compila e roda o firmware no computador; devolve (linhas da serial, saída de erro)."""
        with tempfile.TemporaryDirectory(prefix="agro_fw3_") as pasta:
            executavel = Path(pasta) / "firmware_host"
            compilacao = subprocess.run(
                ["g++", *extras, "-I", str(RAIZ_PROJETO / "tests" / "stubs_arduino"), "-I", str(RAIZ_PROJETO / "arduino"),
                 "-o", str(executavel), str(RAIZ_PROJETO / "tests" / "stubs_arduino" / "execucao_host.cpp")],
                capture_output=True, text=True, timeout=180, check=False)
            self.assertEqual(compilacao.returncode, 0, compilacao.stderr)
            execucao = subprocess.run([str(executavel)], capture_output=True, text=True, timeout=30, check=True)
        return [linha.strip() for linha in execucao.stdout.splitlines() if linha.strip()], execucao.stderr

    def test_banner_padrao_e_pulsado(self) -> None:
        """Banner v2 intacto + perfil; com pulso, toda leitura do ADC acontece com a sonda LIGADA."""
        linhas, erro = self._executar_no_host()
        self.assertEqual(linhas[:2], ["#AGROANALISE_PRONTO;PROTOCOLO=2",
                                      "#AGROANALISE_PERFIL;PROTOCOLO=2;AR=DHT11;SOLO=RESISTIVO;PULSO=0"])
        self.assertIn("PULSOS=0", erro)
        linhas_pulso, erro_pulso = self._executar_no_host("-DAGRO_PINO_ALIMENTACAO_SOLO=7",
                                                          "-DAGRO_SENSOR_AR=AGRO_AR_DHT22")
        self.assertEqual(linhas_pulso[1], "#AGROANALISE_PERFIL;PROTOCOLO=2;AR=DHT22;SOLO=RESISTIVO;PULSO=1")
        self.assertIn("#ERRO:DHT22_LEITURA_INVALIDA", linhas_pulso)
        campos = dict(parte.split("=") for parte in erro_pulso.strip().split(":", 1)[1].split(";"))
        self.assertEqual(int(campos["LEITURAS_SOLO_DESLIGADA"]), 0)
        self.assertEqual(int(campos["LEITURAS_SOLO_LIGADA"]), 10 * int(campos["PULSOS"]))
        self.assertGreaterEqual(int(campos["PULSOS"]), 11)  # 10 no pré-preenchimento + 1 por envio
        dados = [interpretar_linha_serial_detalhada(linha) for linha in linhas_pulso if linha.startswith("$")]
        self.assertGreaterEqual(len(dados), 2)
        self.assertTrue(all(linha.tipo == "DADOS" and linha.checksum_valido for linha in dados))
        self.assertEqual(interpretar_perfil_firmware(linhas_pulso[1][1:])["AR"], "DHT22")


class TestePerfilSensorAr(BaseBanco):
    """Perfil do sensor de ar no host: saneador, limiar de geada e banner."""

    def test_saneador_com_perfil(self) -> None:
        """−1,5 °C: anomalia 'negativo' com DHT11, medição válida com DHT22."""
        dht11 = SaneadorTempoReal(self.arabica)
        _, flag, descricao = dht11.sanear(-1.5, 70.0, 70.0)
        self.assertEqual((flag, descricao), (1, "temperatura_ar:negativo"))
        dht22 = SaneadorTempoReal(self.arabica, limites_fisicos=ra.limites_fisicos_sensor_ar("DHT22"))
        valores, flag22, _ = dht22.sanear(-1.5, 70.0, 70.0)
        self.assertEqual((flag22, valores["temperatura_ar"]), (0, -1.5))
        self.assertEqual(ra.limites_fisicos_sensor_ar("XPTO")["temperatura_ar"], (0.0, 50.0))
        self.assertEqual(ra.limites_fisicos_sensor_ar("sht31")["temperatura_ar"], (-40.0, 125.0))

    def test_geada_com_dht22(self) -> None:
        """Incerteza 0,5 °C: limiar 2,5 °C; 2,4 °C → Vermelho e 2,6 °C → não é geada."""
        cfg = ConfiguracaoTermica(modo=ra.MODO_FISIOLOGICO, incerteza_sensor_ar_c=0.5)
        self.assertEqual(limiar_geada_vermelho(cfg), 2.5)
        self.assertEqual(self.avaliar(2.4, resumo=RESUMO_MEDIA_19, cfg=cfg).motivo, "GEADA_AGORA")
        self.assertEqual(self.avaliar(2.6, resumo=RESUMO_MEDIA_19, cfg=cfg).status, 0)

    def test_banner_em_duas_linhas(self) -> None:
        """'#AGROANALISE_PRONTO' (v2) + '#AGROANALISE_PERFIL;...;AR=DHT22' gravam o perfil e mantêm o descarte."""
        self.ligar_fisiologica()
        coletor = ColetorAgroAnalise(caminho_banco=self.banco, forcar_simulacao=True)
        coletor._atualizar_configuracoes()
        coletor._verificar_troca_cultura()
        coletor._ao_receber_linha(interpretar_linha_serial_detalhada("#AGROANALISE_PRONTO;PROTOCOLO=2"))
        self.assertEqual(obter_configuracao("sensor_ar_modelo", caminho_banco=self.banco), "DHT11")
        coletor._ao_receber_linha(interpretar_linha_serial_detalhada(
            "#AGROANALISE_PERFIL;PROTOCOLO=2;AR=DHT22;SOLO=RESISTIVO;PULSO=1"))
        self.assertEqual(obter_configuracao("sensor_ar_modelo", caminho_banco=self.banco), "DHT22")
        self.assertEqual(float(obter_configuracao("incerteza_sensor_ar_c", caminho_banco=self.banco)), 0.5)
        self.assertEqual(coletor.motor_regras.configuracao.incerteza_sensor_ar_c, 0.5)
        assert coletor._saneador_campo is not None
        self.assertEqual(coletor._saneador_campo.limites_fisicos["temperatura_ar"], (-40.0, 80.0))
        self.assertFalse(coletor._ao_receber_linha(interpretar_linha_serial_detalhada(linha_v2("1.0", "65.0", "70.0"))))
        coletor._ao_receber_linha(interpretar_linha_serial_detalhada("#AGROANALISE_PERFIL;AR=BME280"))
        self.assertEqual(obter_configuracao("sensor_ar_modelo", caminho_banco=self.banco), "DHT11")
        self.assertEqual(float(obter_configuracao("incerteza_sensor_ar_c", caminho_banco=self.banco)), 2.0)


@unittest.skipUnless(sys.platform.startswith("linux") and hasattr(os, "openpty"), "emulador pty requer Linux")
class TesteBannerPty(BaseBanco):
    """Arduino emulado em pseudo-terminal com o banner de uma linha só ``AR=DHT22``."""

    def setUp(self) -> None:
        """Par pty e espera de reset zerada."""
        super().setUp()
        import tty
        self.mestre, self.escravo = os.openpty()
        tty.setraw(self.escravo)
        self.nome_porta = os.ttyname(self.escravo)
        self._espera_original = coletor_serial.ESPERA_RESET_ARDUINO_SEGUNDOS
        coletor_serial.ESPERA_RESET_ARDUINO_SEGUNDOS = 0.0
        self._parar = threading.Event()

    def tearDown(self) -> None:
        """Restaura a constante e fecha os descritores."""
        coletor_serial.ESPERA_RESET_ARDUINO_SEGUNDOS = self._espera_original
        self._parar.set()
        for descritor in (self.mestre, self.escravo):
            with contextlib.suppress(OSError):
                os.close(descritor)
        super().tearDown()

    def _escrever(self, linha: str) -> bool:
        """Escreve uma linha no lado do 'Arduino'."""
        try:
            os.write(self.mestre, (linha + "\r\n").encode("ascii"))
            return True
        except OSError:
            return False

    def test_banner_dht22_e_geada_abaixo_de_zero(self) -> None:
        """Banner AR=DHT22 → perfil gravado; −1,5 °C sem anomalia e 2,4 °C com Vermelho de geada."""
        coletor = ColetorAgroAnalise(caminho_banco=self.banco, porta=self.nome_porta, intervalo_segundos=0.2,
                                     max_iteracoes=10)
        coletor.caminho_modelo = self.pasta / "sem_modelo.pkl"
        coletor.modulo_antecipacao = None

        def firmware() -> None:
            """Banner, depois −1,5 °C por 4 ciclos e 2,4 °C em seguida."""
            prazo = time.monotonic() + 15
            while not coletor.conectado and time.monotonic() < prazo and not self._parar.is_set():
                time.sleep(0.01)
            if not self._escrever("#AGROANALISE_PRONTO;PROTOCOLO=2;AR=DHT22;SOLO=RESISTIVO;PULSO=1"):
                return
            passo = 0
            while not self._parar.is_set():
                temperatura = "-1.5" if passo < 20 else "2.4"
                if not self._escrever(linha_v2(temperatura, "90.0", "70.0")):
                    return
                passo += 1
                self._parar.wait(0.05)
        tarefa = threading.Thread(target=firmware, daemon=True)
        tarefa.start()
        execucao = threading.Thread(target=coletor.executar, daemon=True)
        execucao.start()
        execucao.join(timeout=90)
        self._parar.set()
        self.assertFalse(execucao.is_alive(), "o coletor não terminou")
        self.assertEqual(obter_configuracao("sensor_ar_modelo", caminho_banco=self.banco), "DHT22")
        self.assertEqual(float(obter_configuracao("incerteza_sensor_ar_c", caminho_banco=self.banco)), 0.5)
        campo = [leitura for leitura in self.leituras() if leitura["origem"] == "CAMPO_USB"]
        self.assertGreaterEqual(len(campo), 4)
        negativos = [leitura for leitura in campo if leitura["temperatura_ar_bruta"] == -1.5]
        self.assertTrue(negativos)
        for leitura in negativos:
            self.assertEqual((leitura["flag_anomalia"], leitura["temperatura_ar"]), (0, -1.5))
            self.assertEqual((leitura["status_alerta"], leitura["motivo_regra"]), (2, "GEADA_AGORA"))
        quase = [leitura for leitura in campo if leitura["temperatura_ar_bruta"] == 2.4]
        self.assertTrue(quase)
        for leitura in quase:
            self.assertEqual((leitura["status_alerta"], leitura["motivo_regra"]), (2, "GEADA_AGORA"))


class TesteCalibracaoSonda(BaseBanco):
    """Calibração da sonda aplicada no host (módulo opcional ``calibracao_sonda`` da Frente 3)."""

    def setUp(self) -> None:
        """Esquece falhas anteriores do carregamento."""
        super().setUp()
        coletor_serial._FALHAS_MODULO_CALIBRACAO.clear()

    def tearDown(self) -> None:
        """Esquece as falhas provocadas pelo teste."""
        coletor_serial._FALHAS_MODULO_CALIBRACAO.clear()
        super().tearDown()

    @staticmethod
    def _modulo(funcao: Callable[..., Any]) -> types.ModuleType:
        """Módulo falso com ``converter_umidade_solo``."""
        modulo = types.ModuleType("calibracao_sonda")
        modulo.converter_umidade_solo = funcao  # type: ignore[attr-defined]
        return modulo

    def test_modulo_falso_soma_5(self) -> None:
        """Calibração +5: umidade_solo gravada = firmware + 5; umidade_solo_firmware preservado; só no campo."""
        chamadas: List[Tuple[Any, ...]] = []

        def somar_5(valor: Optional[float], caminho: Path, id_no: str = "principal") -> Optional[float]:
            """Soma 5 pontos."""
            chamadas.append((valor, caminho, id_no))
            return None if valor is None else valor + 5.0
        with mock.patch.dict(sys.modules, {"calibracao_sonda": self._modulo(somar_5)}):
            _, registro = processar_leitura(20.0, 70.0, 60.0, "CAMPO_USB", "cafe_arabica",
                                            SaneadorTempoReal(self.arabica), None, caminho_banco=self.banco)
            _, simulado = processar_leitura(20.0, 70.0, 60.0, "SIMULACAO", "cafe_arabica",
                                            SaneadorTempoReal(self.arabica), None, caminho_banco=self.banco)
        self.assertEqual((registro.umidade_solo, registro.umidade_solo_firmware, registro.umidade_solo_bruta),
                         (65.0, 60.0, 65.0))
        self.assertEqual(chamadas, [(60.0, self.banco, "principal")])
        self.assertEqual((simulado.umidade_solo, simulado.umidade_solo_firmware), (60.0, None))
        gravadas = self.leituras()
        self.assertEqual((gravadas[0]["umidade_solo"], gravadas[0]["umidade_solo_firmware"]), (65.0, 60.0))

    def test_modulo_ausente_e_identidade(self) -> None:
        """Sem o módulo: identidade, sem exceção; a falha é lembrada por 60 s (sem novo import)."""
        with mock.patch.dict(sys.modules, {"calibracao_sonda": None}):
            _, registro = processar_leitura(20.0, 70.0, 60.0, "CAMPO_USB", "cafe_arabica",
                                            SaneadorTempoReal(self.arabica), None, caminho_banco=self.banco)
        self.assertEqual((registro.umidade_solo, registro.umidade_solo_firmware), (60.0, 60.0))
        self.assertIsNone(carregar_modulo_calibracao("modulo_inexistente_agro_xyz"))
        with mock.patch.object(coletor_serial.importlib, "import_module") as importar:
            self.assertIsNone(carregar_modulo_calibracao("modulo_inexistente_agro_xyz"))
            importar.assert_not_called()
        coletor_serial._FALHAS_MODULO_CALIBRACAO["modulo_inexistente_agro_xyz"] -= 61.0
        with mock.patch.object(coletor_serial.importlib, "import_module", side_effect=ImportError("x")) as importar:
            self.assertIsNone(carregar_modulo_calibracao("modulo_inexistente_agro_xyz"))
            importar.assert_called_once()

    def test_modulo_com_defeito_e_identidade(self) -> None:
        """Exceção, retorno None ou NaN na calibração: identidade; entrada ausente continua ausente."""
        def explodir(valor: Optional[float], caminho: Path, id_no: str = "principal") -> Optional[float]:
            """Falha sempre."""
            raise RuntimeError("tabela corrompida")
        for funcao in (explodir, lambda valor, caminho, id_no="principal": None,
                       lambda valor, caminho, id_no="principal": float("nan")):
            with mock.patch.dict(sys.modules, {"calibracao_sonda": self._modulo(funcao)}):
                self.assertEqual(calibrar_umidade_solo(42.0, self.banco), 42.0)
                self.assertIsNone(calibrar_umidade_solo(None, self.banco))
        with mock.patch.dict(sys.modules, {"calibracao_sonda": types.ModuleType("calibracao_sonda")}):
            self.assertIsNone(carregar_modulo_calibracao())
            self.assertEqual(calibrar_umidade_solo(42.0, self.banco), 42.0)


if __name__ == "__main__":
    unittest.main()
