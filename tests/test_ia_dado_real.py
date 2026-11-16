"""Testes da Frente 2 — IA validada em dado real (unittest da biblioteca padrão).

Cobrem os critérios de aceite de:

    1. referência climática real offline (ERA5-Land) e avaliação em dado real;
    2. prognóstico retreinado com o rótulo da regra fisiológica (rotulador
       carregado por importlib, com módulo FALSO — nunca o da Frente 1);
    3. aviso de geada recalibrado (limiar fisiológico, coeficientes por região,
       margem conformal por estação);
    4. portão de segurança local (Page-Hinkley, Brier contra a climatologia,
       Platt e mínima local);
    5. certeza da antecipação falada em dois níveis.

Sempre com banco SQLite TEMPORÁRIO e sem rede. Execução (raiz do projeto):
``python -m unittest tests.test_ia_dado_real -v``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import logging
import math
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from unittest import mock

RAIZ_PROJETO: Path = Path(__file__).resolve().parent.parent
PASTA_SRC: Path = RAIZ_PROJETO / "src"
if str(PASTA_SRC) not in sys.path:
    sys.path.insert(0, str(PASTA_SRC))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import acoes_orientacao  # noqa: E402
import desempenho_local  # noqa: E402
import prognostico  # noqa: E402
import referencia_climatica  # noqa: E402
import treinamento_ia  # noqa: E402
from database import (  # noqa: E402
    ParametrosCultura,
    RegistroLeitura,
    buscar_parametros_cultura,
    definir_configuracao,
    inicializar_banco,
    inserir_leituras_em_lote,
    obter_configuracao,
)
from gerador_dataset import (  # noqa: E402
    calcular_features,
    calcular_features_dataframe,
    calcular_status_regra,
    gerar_mensagem_orientacao,
    simular_serie_temporal,
    umidade_relativa_magnus,
)
from prognostico import aplicar_antecipacao, gerar_frase_antecipacao  # noqa: E402

FORMATO: str = "%Y-%m-%d %H:%M:%S"


def setUpModule() -> None:
    """Silencia os logs informativos e os avisos durante os testes (o teste do aviso reativa o que precisa)."""
    logging.disable(logging.WARNING)


def tearDownModule() -> None:
    """Reativa os logs."""
    logging.disable(logging.NOTSET)


# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------
class ModeloFalso:
    """Estimador falso com ``predict_proba`` constante (classes 0 e 1)."""

    classes_ = np.array([0, 1])

    def __init__(self, probabilidade: float) -> None:
        """Guarda a probabilidade de "vira Vermelho" devolvida para qualquer linha."""
        self.probabilidade = float(probabilidade)

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """Devolve ``[1 − p, p]`` para cada linha."""
        return np.tile([1.0 - self.probabilidade, self.probabilidade], (len(X), 1))


def pacote_falso(probabilidade: float, limiar: float = 0.5) -> Dict[str, Any]:
    """Pacote de prognóstico mínimo (9 chaves) com o modelo falso."""
    return {
        "modelo": ModeloFalso(probabilidade), "features": list(prognostico.COLUNAS_FEATURES_PROGNOSTICO),
        "limiar": limiar, "horizonte_horas": 12, "culturas": ["cafe_arabica", "cafe_conilon"],
        "coef_tmin": None, "metricas": {}, "versao": "FALSO-1", "treinado_em": "2026-01-01T00:00:00",
    }


def montar_registro(
    parametros: ParametrosCultura, instante: datetime, temperatura: float, umidade_ar: float, solo: float,
    origem: str = "CAMPO_USB", status_regra: Optional[int] = None,
) -> RegistroLeitura:
    """Registro coerente como o coletor montaria; ``status_regra`` força a cor da regra (ex.: regra fisiológica)."""
    deficit, desvio = calcular_features(temperatura, solo, parametros)
    status = calcular_status_regra(temperatura, solo, parametros) if status_regra is None else int(status_regra)
    return RegistroLeitura(
        data_hora=instante.strftime(FORMATO), codigo_cultura=parametros.codigo, origem=origem,
        temperatura_ar_bruta=temperatura, umidade_ar_bruta=umidade_ar, umidade_solo_bruta=solo,
        temperatura_ar=temperatura, umidade_ar=umidade_ar, umidade_solo=solo,
        deficit_hidrico_solo=deficit, desvio_termico=desvio,
        status_regra=status, status_predito=status, status_alerta=status, probabilidade_predicao=1.0,
        flag_anomalia=0, descricao_anomalia="",
        mensagem_voz=gerar_mensagem_orientacao(min(status, 2), deficit, desvio, parametros, temperatura, solo),
        versao_modelo="REGRAS",
    )


def rotulo_fisiologico_falso(df_horario: pd.DataFrame, parametros: ParametrosCultura,
                             configuracao: Optional[object] = None) -> pd.Series:
    """Dublê da ``regras_agronomicas.rotular_status_horario`` (Frente 1) com o mesmo contrato.

    Conforto pela média de 24 h (≥ 18 h) limitado a Amarelo, Vermelho térmico só com ≤ 4 °C e solo
    instantâneo. Serve só para provar que a Frente 2 usa a função carregada por importlib.
    """
    faixa_t = parametros.faixa("temperatura_ar")
    faixa_s = parametros.faixa("umidade_solo")
    temperatura = pd.to_numeric(df_horario["temperatura_ar"], errors="coerce").astype(float).reset_index(drop=True)
    media = temperatura.rolling(24, min_periods=18).mean()
    base = media.where(media.notna(), temperatura).to_numpy()
    conforto = np.minimum(np.nan_to_num(prognostico.status_vetorizado(base, faixa_t)), 1)
    termico = np.where(temperatura.to_numpy() <= 4.0, 2, conforto)
    solo = np.nan_to_num(prognostico.status_vetorizado(
        pd.to_numeric(df_horario["umidade_solo"], errors="coerce").to_numpy(dtype=float), faixa_s))
    return pd.Series(np.maximum(termico, solo).astype(int), index=df_horario.index)


def modulo_regras_falso(funcao: Any = rotulo_fisiologico_falso) -> types.ModuleType:
    """Módulo ``regras_agronomicas`` falso, para pôr em ``sys.modules``."""
    modulo = types.ModuleType("regras_agronomicas")
    modulo.rotular_status_horario = funcao  # type: ignore[attr-defined]
    return modulo


class BaseBanco(unittest.TestCase):
    """Banco temporário isolado, sem complemento real e com o cache do prognóstico limpo."""

    def setUp(self) -> None:
        """Cria o banco temporário e aponta o complemento para um caminho inexistente."""
        self._diretorio = tempfile.mkdtemp(prefix="agroanalise_dado_real_")
        self.pasta = Path(self._diretorio)
        self.banco = self.pasta / "teste.db"
        inicializar_banco(self.banco)
        self.arabica = buscar_parametros_cultura("cafe_arabica", caminho_banco=self.banco)
        prognostico.limpar_cache_serie_horaria()
        self._sem_complemento = mock.patch.object(prognostico, "CAMINHO_COMPLEMENTO_PROGNOSTICO",
                                                  self.pasta / "sem_complemento.json")
        self._sem_complemento.start()

    def tearDown(self) -> None:
        """Desfaz o patch e remove o diretório temporário."""
        self._sem_complemento.stop()
        prognostico.limpar_cache_serie_horaria()
        shutil.rmtree(self._diretorio, ignore_errors=True)

    def inserir_historico(self, agora: datetime, valores: Sequence[Tuple[float, float, float]]) -> List[int]:
        """Insere leituras horárias de campo que TERMINAM uma hora antes de ``agora``."""
        total = len(valores)
        registros = [montar_registro(self.arabica, agora - timedelta(hours=total - posicao), t, ur, solo)
                     for posicao, (t, ur, solo) in enumerate(valores)]
        return inserir_leituras_em_lote(registros, caminho_banco=self.banco)


# ---------------------------------------------------------------------------
# 1. Referência climática real
# ---------------------------------------------------------------------------
class TesteReferenciaClimatica(unittest.TestCase):
    """carregar_referencia, tabela_noites, climatologia_mes_hora e regiao_da_cultura."""

    def test_35064_linhas_sem_nan(self) -> None:
        """Cada região tem 35.064 horas (2018–2021) e nenhuma temperatura ausente."""
        for regiao in referencia_climatica.REGIOES_REFERENCIA:
            df = referencia_climatica.carregar_referencia(regiao)
            self.assertEqual(len(df), 35064, regiao)
            self.assertEqual(int(df["temperatura_ar"].isna().sum()), 0, regiao)
            self.assertEqual(list(df.columns), list(referencia_climatica.COLUNAS_REFERENCIA))
            self.assertTrue(df["umidade_ar"].between(0.0, 100.0).all())
            self.assertIsNone(df["data_hora"].dt.tz)

    def test_hora_local_de_sao_paulo(self) -> None:
        """A primeira hora UTC de 2018 (00:00) vira 22:00 de 31/12/2017 no horário de verão (UTC−2)."""
        df = referencia_climatica.carregar_referencia("varginha_mg")
        self.assertEqual(df["data_hora"].iloc[0], pd.Timestamp("2017-12-31 22:00:00"))

    def test_media_anual_de_varginha(self) -> None:
        """Média anual de Varginha entre 19 e 22 °C (local apto ao Arábica pela Portaria 58/2005)."""
        df = referencia_climatica.carregar_referencia("varginha_mg")
        medias = df.loc[df["data_hora"].dt.year.between(2018, 2021)].groupby(df["data_hora"].dt.year)[
            "temperatura_ar"].mean()
        for ano, media in medias.items():
            self.assertTrue(19.0 <= media <= 22.0, (ano, media))

    def test_climatologia_288_chaves(self) -> None:
        """288 chaves 'MM-HH' com valores em (0, 1), Laplace (1, 2) e 0,5 nas células vazias."""
        df = referencia_climatica.carregar_referencia("varginha_mg")
        alvo = (df["temperatura_ar"] < 15.0).astype(float)
        clima = referencia_climatica.climatologia_mes_hora(df["data_hora"], alvo)
        self.assertEqual(len(clima), 288)
        self.assertTrue(all(re.fullmatch(r"\d\d-\d\d", chave) for chave in clima))
        self.assertTrue(all(0.0 < valor < 1.0 for valor in clima.values()))
        self.assertGreater(clima["07-06"], clima["01-15"])  # madrugada de julho é mais fria que tarde de janeiro
        pequeno = referencia_climatica.climatologia_mes_hora(
            pd.Series(pd.to_datetime(["2021-01-01 05:00", "2021-01-02 05:00"])), pd.Series([1.0, 0.0]))
        self.assertAlmostEqual(pequeno["01-05"], 2.0 / 4.0)
        self.assertAlmostEqual(pequeno["02-05"], 0.5)

    def test_tabela_noites(self) -> None:
        """Uma linha por noite, mínima da noite anterior = persistência, colunas do contrato."""
        noites = referencia_climatica.tabela_noites(referencia_climatica.carregar_referencia("varginha_mg"))
        self.assertEqual(list(noites.columns), list(referencia_climatica.COLUNAS_NOITES))
        self.assertGreater(len(noites), 1400)
        seguidas = noites["data"].diff() == pd.Timedelta(days=1)
        self.assertTrue(np.allclose(noites["tmin_noite_anterior"][seguidas], noites["tmin_noite"].shift(1)[seguidas]))
        self.assertTrue((noites["tmin_noite"] <= noites["t_tarde"] + 5.0).all())

    def test_regiao_da_cultura(self) -> None:
        """Padrão da cultura, chave regiao_referencia do banco e banco ausente."""
        pasta = Path(tempfile.mkdtemp(prefix="agroanalise_regiao_"))
        try:
            banco = pasta / "teste.db"
            self.assertEqual(referencia_climatica.regiao_da_cultura("cafe_arabica", banco), "varginha_mg")
            self.assertFalse(banco.exists())  # nunca cria o banco
            inicializar_banco(banco)
            self.assertEqual(referencia_climatica.regiao_da_cultura("cafe_conilon", banco), "sao_gabriel_da_palha_es")
            self.assertIsNone(referencia_climatica.regiao_da_cultura("tomate", banco))
            definir_configuracao("regiao_referencia", "sao_gabriel_da_palha_es", banco)
            self.assertEqual(referencia_climatica.regiao_da_cultura("cafe_arabica", banco), "sao_gabriel_da_palha_es")
            definir_configuracao("regiao_referencia", "inexistente", banco)
            self.assertEqual(referencia_climatica.regiao_da_cultura("cafe_arabica", banco), "varginha_mg")
        finally:
            shutil.rmtree(pasta, ignore_errors=True)

    def test_licenca_e_fonte(self) -> None:
        """O arquivo de licença traz a atribuição CC BY 4.0 e a ressalva da geada radiativa."""
        texto = (referencia_climatica.DIRETORIO_REFERENCIA / "LICENCA_E_FONTE.txt").read_text(encoding="utf-8")
        for trecho in ("Weather data by Open-Meteo.com", "Copernicus ERA5-Land, CC BY 4.0", "−21,6", "−19,0",
                       "GEADA RADIATIVA"):
            self.assertIn(trecho, texto)


# ---------------------------------------------------------------------------
# 2. Rotulador e retreino
# ---------------------------------------------------------------------------
class TesteRotulador(unittest.TestCase):
    """carregar_rotulador com módulo falso, sem módulo e com modo inválido."""

    def setUp(self) -> None:
        """Faixas do Arábica num banco temporário."""
        self._diretorio = tempfile.mkdtemp(prefix="agroanalise_rotulador_")
        banco = Path(self._diretorio) / "teste.db"
        inicializar_banco(banco)
        self.arabica = buscar_parametros_cultura("cafe_arabica", caminho_banco=banco)
        self.entrada = pd.DataFrame({
            "data_hora": pd.date_range("2021-07-01", periods=48, freq="h"),
            "temperatura_ar": [12.0] * 24 + [3.0] + [12.0] * 23, "umidade_ar": 70.0, "umidade_solo": 70.0})

    def tearDown(self) -> None:
        """Remove o diretório temporário."""
        shutil.rmtree(self._diretorio, ignore_errors=True)

    def test_fisiologica_usa_a_funcao_do_modulo(self) -> None:
        """Com 'regras_agronomicas' falso em sys.modules, o rótulo vem da função falsa."""
        chamadas: List[int] = []

        def falsa(df: pd.DataFrame, parametros: ParametrosCultura, configuracao: Optional[object] = None) -> pd.Series:
            """Rótulo constante 1 que registra a chamada."""
            chamadas.append(len(df))
            return pd.Series(1, index=df.index)

        with mock.patch.dict(sys.modules, {"regras_agronomicas": modulo_regras_falso(falsa)}):
            rotulador = treinamento_ia.carregar_rotulador("fisiologica")
            rotulo = rotulador(self.entrada, self.arabica)
        self.assertEqual(chamadas, [48])
        self.assertEqual(treinamento_ia.modo_do_rotulador(rotulador), "fisiologica")
        self.assertTrue((rotulo == 1).all())
        self.assertTrue(rotulo.index.equals(self.entrada.index))

    def test_sem_modulo_cai_no_instantaneo_com_aviso(self) -> None:
        """Sem o módulo: WARNING no log e rótulo instantâneo (12 °C = Vermelho no Arábica)."""
        logging.disable(logging.NOTSET)
        try:
            with mock.patch.dict(sys.modules, {"regras_agronomicas": None}), \
                    self.assertLogs("agroanalise.treinamento_ia", level="WARNING") as registro:
                rotulador = treinamento_ia.carregar_rotulador("fisiologica")
        finally:
            logging.disable(logging.WARNING)
        self.assertTrue(any("instantânea" in linha for linha in registro.output))
        self.assertEqual(treinamento_ia.modo_do_rotulador(rotulador), "instantanea")
        self.assertEqual(int(rotulador(self.entrada, self.arabica).iloc[0]), 2)
        with mock.patch.dict(sys.modules, {"regras_agronomicas": types.ModuleType("regras_agronomicas")}):
            sem_funcao = treinamento_ia.carregar_rotulador("fisiologica")
        self.assertEqual(treinamento_ia.modo_do_rotulador(sem_funcao), "instantanea")

    def test_instantaneo_e_modo_invalido(self) -> None:
        """'instantanea' = max(status da temperatura, status do solo); modo desconhecido levanta ValueError."""
        rotulador = treinamento_ia.carregar_rotulador("instantanea")
        rotulo = rotulador(self.entrada.assign(temperatura_ar=20.0, umidade_solo=[70.0] * 47 + [52.0]), self.arabica)
        self.assertEqual(rotulo.tolist(), [0] * 47 + [1])
        with self.assertRaises(ValueError):
            treinamento_ia.carregar_rotulador("media_mensal")
        self.assertEqual(treinamento_ia.modo_do_rotulador(None), "instantanea")

    def test_linha_de_comando(self) -> None:
        """--regra-temperatura aceita só as duas regras."""
        parser = treinamento_ia._criar_parser()
        self.assertEqual(parser.parse_args(["--regra-temperatura", "fisiologica"]).regra_temperatura, "fisiologica")
        self.assertIsNone(parser.parse_args([]).regra_temperatura)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["--regra-temperatura", "anual"])


class TesteTreinoRapidoFisiologico(unittest.TestCase):
    """Treino rápido (2 × 2.000 h) com o rotulador fisiológico falso: pacote, complemento e dado real."""

    @classmethod
    def setUpClass(cls) -> None:
        """Simula 2 × 2.000 h, treina com o rótulo fisiológico falso e monta o complemento."""
        cls._diretorio = tempfile.mkdtemp(prefix="agroanalise_treino_rapido_")
        cls.pasta = Path(cls._diretorio)
        banco = cls.pasta / "teste.db"
        inicializar_banco(banco)
        cls.parametros = {codigo: buscar_parametros_cultura(codigo, caminho_banco=banco)
                          for codigo in ("cafe_arabica", "cafe_conilon")}
        series = [simular_serie_temporal(cls.parametros["cafe_arabica"], 2000, semente=7,
                                         data_inicial="2026-05-01 00:00:00"),
                  simular_serie_temporal(cls.parametros["cafe_conilon"], 2000, semente=8,
                                         data_inicial="2026-05-01 00:00:00")]
        cls.limpo = calcular_features_dataframe(pd.concat(series, ignore_index=True), cls.parametros)
        with mock.patch.dict(sys.modules, {"regras_agronomicas": modulo_regras_falso()}):
            rotulador = treinamento_ia.carregar_rotulador("fisiologica")
        cls.rotulador = staticmethod(rotulador)  # função guardada na classe: sem virar método ligado
        cls.pacote, cls.avaliacao = treinamento_ia.avaliar_prognostico_completo(
            cls.limpo, cls.parametros, rotulador=rotulador)
        cls.pacote["versao"] = f"{cls.pacote['versao']}-fisiologica"

    @classmethod
    def tearDownClass(cls) -> None:
        """Remove o diretório temporário."""
        shutil.rmtree(cls._diretorio, ignore_errors=True)

    def test_pacote_com_nove_chaves(self) -> None:
        """O pacote fisiológico continua com EXATAMENTE as 9 chaves e recarrega."""
        self.assertEqual(set(self.pacote), set(prognostico.CHAVES_PACOTE_PROGNOSTICO))
        caminho = treinamento_ia.salvar_modelo_prognostico(self.pacote, self.pasta / "modelo_prognostico.pkl")
        carregado = prognostico.carregar_modelo_prognostico(caminho)
        assert carregado is not None
        self.assertTrue(carregado["versao"].endswith("-fisiologica"))

    def test_rotulo_fisiologico_no_dataset(self) -> None:
        """O rótulo do dataset do prognóstico é o do rotulador (não o status do dataset)."""
        df_prog = treinamento_ia.preparar_dataset_prognostico(self.limpo, self.parametros, rotulador=self.rotulador)
        df_v2 = treinamento_ia.preparar_dataset_prognostico(self.limpo, self.parametros)
        self.assertTrue((df_prog["status_atual"] < 2).all())
        self.assertNotEqual(len(df_prog), len(df_v2))  # o frio do inverno deixou de ser Vermelho

    def test_complemento_json_valido(self) -> None:
        """Complemento com as chaves do contrato, 288 células por cultura e coeficientes por região."""
        complemento = treinamento_ia.montar_complemento_prognostico(
            self.limpo, self.parametros, self.rotulador, self.pacote["versao"])
        caminho = treinamento_ia.salvar_complemento_prognostico(complemento, self.pasta / "complemento.json")
        conteudo = json.loads(caminho.read_text(encoding="utf-8"))
        self.assertEqual(set(conteudo), set(treinamento_ia.CHAVES_COMPLEMENTO))
        self.assertEqual(conteudo["regra_temperatura"], "fisiologica")
        self.assertEqual(conteudo["versao"], self.pacote["versao"])
        for cultura in ("cafe_arabica", "cafe_conilon"):
            tabela = conteudo["climatologia_por_cultura"][cultura]
            self.assertEqual(len(tabela), 288)
            self.assertTrue(all(0.0 < valor < 1.0 for valor in tabela.values()))
        for regiao in referencia_climatica.REGIOES_REFERENCIA:
            self.assertEqual(len(conteudo["coef_tmin_por_regiao"][regiao]), 3)
            self.assertEqual(set(conteudo["quantis_tmin_por_regiao"][regiao]), {"seca", "aguas"})
        carregado = prognostico.carregar_complemento_prognostico(caminho)
        self.assertEqual(carregado, conteudo)
        with self.assertRaises(ValueError):
            treinamento_ia.salvar_complemento_prognostico({"versao": "x"}, self.pasta / "ruim.json")

    def test_sem_referencia_nao_quebra(self) -> None:
        """Instalação sem a pasta da referência: complemento só com o simulador e avaliação vazia, sem exceção."""
        vazia = self.pasta / "sem_referencia"
        vazia.mkdir(exist_ok=True)
        self.assertFalse(referencia_climatica.referencia_disponivel(None, vazia))
        complemento = treinamento_ia.montar_complemento_prognostico(
            self.limpo, self.parametros, self.rotulador, self.pacote["versao"], diretorio=vazia)
        self.assertEqual(complemento["coef_tmin_por_regiao"], {})
        self.assertEqual(len(complemento["climatologia_por_cultura"]["cafe_arabica"]), 288)
        resultado = treinamento_ia.avaliar_em_referencia(self.pacote, self.parametros, self.rotulador, diretorio=vazia)
        self.assertEqual(resultado["por_cultura"], {})
        self.assertTrue(math.isnan(resultado["brier"]))
        self.assertEqual(treinamento_ia.avaliar_geada_referencia(None, diretorio=vazia), {})

    def test_avaliar_em_referencia(self) -> None:
        """avaliar_em_referencia devolve AUC, Brier e os Brier da climatologia e da persistência."""
        resultado = treinamento_ia.avaliar_em_referencia(
            self.pacote, {"cafe_arabica": self.parametros["cafe_arabica"]}, self.rotulador)
        for chave in ("auc", "brier", "brier_climatologia", "brier_persistencia", "falso_amarelo_h_semana",
                      "recall"):
            self.assertIn(chave, resultado)
        self.assertTrue(0.0 <= resultado["brier"] <= 1.0)
        self.assertTrue(0.0 <= resultado["brier_climatologia"] <= 1.0)
        self.assertEqual(resultado["ano_teste"], 2021)
        self.assertEqual(resultado["regra"], "fisiologica")
        arabica = resultado["por_cultura"]["cafe_arabica"]
        self.assertEqual(arabica["regiao"], "varginha_mg")
        self.assertGreater(arabica["horas"], 8000)  # horas não vermelhas de 2021
        texto = treinamento_ia.montar_relatorio_referencia(
            resultado, treinamento_ia.avaliar_geada_referencia(tuple(self.pacote["coef_tmin"]),
                                                               regioes=["varginha_mg"]))
        self.assertIn(treinamento_ia.TITULO_SECAO_REFERENCIA, texto)
        self.assertIn("[escopo:", texto)
        self.assertRegex(texto, r"(DENTRO|ACIMA) do orçamento")


class TesteArtefatosDadoReal(unittest.TestCase):
    """Artefatos de ``python src/treinamento_ia.py`` (pulados se ainda não houver treino)."""

    @unittest.skipUnless(treinamento_ia.CAMINHO_RELATORIO_TREINO.is_file()
                         and prognostico.CAMINHO_COMPLEMENTO_PROGNOSTICO.is_file(), "treino real ainda não executado")
    def test_relatorio_secao_7_e_complemento(self) -> None:
        """Relatório com a seção 7 e complemento JSON coerente com o pacote salvo."""
        texto = treinamento_ia.CAMINHO_RELATORIO_TREINO.read_text(encoding="utf-8")
        self.assertIn("SEÇÃO 7 — Dado real (reanálise ERA5-Land)", texto)
        complemento = prognostico.carregar_complemento_prognostico(prognostico.CAMINHO_COMPLEMENTO_PROGNOSTICO)
        assert complemento is not None
        pacote = prognostico.carregar_modelo_prognostico(prognostico.CAMINHO_MODELO_PROGNOSTICO)
        assert pacote is not None
        self.assertEqual(complemento["versao"], pacote["versao"])
        self.assertTrue(pacote["versao"].endswith("-" + complemento["regra_temperatura"]))


# ---------------------------------------------------------------------------
# 3. Aviso de geada recalibrado
# ---------------------------------------------------------------------------
class TesteGeadaEmDadoReal(unittest.TestCase):
    """Coeficientes por região e margem conformal, medidos em 2021 (fora da amostra)."""

    @classmethod
    def setUpClass(cls) -> None:
        """Coeficientes do simulador (ajuste da v2 num ano simulado) e avaliação das duas regiões."""
        cls._diretorio = tempfile.mkdtemp(prefix="agroanalise_geada_real_")
        banco = Path(cls._diretorio) / "teste.db"
        inicializar_banco(banco)
        parametros = {codigo: buscar_parametros_cultura(codigo, caminho_banco=banco)
                      for codigo in ("cafe_arabica", "cafe_conilon")}
        series = [simular_serie_temporal(parametros["cafe_arabica"], 8760, semente=42),
                  simular_serie_temporal(parametros["cafe_conilon"], 8760, semente=43)]
        horario = pd.concat(series, ignore_index=True)
        cls.coef_simulador, _, _ = treinamento_ia.ajustar_coef_tmin(horario, parametros)
        cls.avaliacao = treinamento_ia.avaliar_geada_referencia(cls.coef_simulador)

    @classmethod
    def tearDownClass(cls) -> None:
        """Remove o diretório temporário."""
        shutil.rmtree(cls._diretorio, ignore_errors=True)

    def test_coeficientes_de_varginha(self) -> None:
        """Varginha 2021: MAE ≤ 1,5 °C, |viés| ≤ 0,5 °C e MAE menor que o dos coeficientes do simulador."""
        metodos = self.avaliacao["varginha_mg"]["metodos"]
        self.assertLessEqual(metodos["regiao"]["mae"], 1.5)
        self.assertLessEqual(abs(metodos["regiao"]["vies"]), 0.5)
        self.assertLess(metodos["regiao"]["mae"], metodos["simulador"]["mae"])
        self.assertLessEqual(metodos["regiao"]["alarmes_falsos"], metodos["simulador"]["alarmes_falsos"])

    def test_cobertura_q90(self) -> None:
        """Cobertura empírica do intervalo q90 ≥ 0,85 em 2021 nas duas estações (e nas duas regiões)."""
        for regiao, dados in self.avaliacao.items():
            self.assertEqual(set(dados["cobertura_q90"]), {"seca", "aguas"}, regiao)
            for estacao, cobertura in dados["cobertura_q90"].items():
                self.assertGreaterEqual(cobertura, 0.85, (regiao, estacao))

    def test_ajuste_so_com_anos_anteriores(self) -> None:
        """ajustar_coef_tmin_referencia ignora 2021: estragar 2021 não muda os coeficientes."""
        noites = referencia_climatica.tabela_noites(referencia_climatica.carregar_referencia("varginha_mg"))
        original = treinamento_ia.ajustar_coef_tmin_referencia(noites)
        estragado = noites.copy()
        estragado.loc[estragado["data"].dt.year == 2021, "tmin_noite"] = 99.0
        self.assertEqual(original, treinamento_ia.ajustar_coef_tmin_referencia(estragado))

    def test_quantil_com_correcao_de_amostra_finita(self) -> None:
        """⌈(n+1)q⌉/n: 9 resíduos 1..9 com q = 0,9 → 9; estação com amostra pequena demais fica de fora."""
        residuos = pd.Series(list(range(1, 10)) + [0.5, -0.5, 0.2])
        meses = pd.Series([7] * 9 + [1, 1, 2])
        quantis = treinamento_ia.quantis_conformal_tmin(residuos, meses, 0.9)
        self.assertEqual(quantis["seca"], 9.0)
        self.assertNotIn("aguas", quantis)  # ⌈4·0,9⌉ = 4 > 3 resíduos
        self.assertEqual(referencia_climatica.quantil_conformal(np.arange(1, 20), 0.9), 18.0)


class TesteGeadaNoTempoReal(BaseBanco):
    """aplicar_antecipacao na regra fisiológica (limiar no abrigo) e na instantânea (v2 intacta)."""

    TARDE = datetime(2026, 7, 10, 18, 0, 0)

    def historico_tarde(self, agora: datetime, temperatura: float, umidade: float) -> None:
        """24 h de campo terminando 1 h antes de ``agora``; as últimas horas com o ar do fim da tarde."""
        self.inserir_historico(agora, [(temperatura + 4.0, umidade + 10.0, 70.0)] * 22 + [(temperatura, umidade, 70.0)] * 2)

    def configurar(self, **valores: Any) -> None:
        """Grava chaves de configuracao_sistema no banco temporário."""
        for chave, valor in valores.items():
            definir_configuracao(chave, valor, self.banco)

    def test_mininma_de_9_graus_nao_e_geada(self) -> None:
        """Fisiológica, 18 h, mínima prevista de 9 °C → sem GEADA_NOITE, motivo FRIO/NENHUM, sem 'Cubra as mudas'."""
        self.configurar(regra_temperatura="fisiologica", coef_tmin_local="9,0,0")
        self.historico_tarde(self.TARDE, 14.0, 60.0)
        # Amarelo informativo do dia frio, com a voz da regra fisiológica (Frente 1: DIA_FRIO).
        registro = dataclasses.replace(
            montar_registro(self.arabica, self.TARDE, 14.0, 60.0, 70.0, status_regra=1),
            mensagem_voz="Atenção com a plantação de café arábica. O dia está frio, mas sem perigo. "
                         "Não precisa fazer nada.")
        novo, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico={})
        assert resultado is not None
        self.assertAlmostEqual(float(resultado.temperatura_minima_prevista), 9.0)
        self.assertIn(resultado.motivo_principal, ("FRIO", "NENHUM"))
        self.assertEqual(resultado.status_antecipado, 0)
        self.assertNotIn("cubra as mudas", novo.mensagem_voz.lower())
        self.assertNotIn("pode gear", novo.mensagem_voz)

    def test_minima_de_3_graus_e_geada(self) -> None:
        """Fisiológica, mínima prevista de 3 °C → GEADA_NOITE e status 1 (ainda Verde pela regra)."""
        self.configurar(regra_temperatura="fisiologica", coef_tmin_local="3,0,0")
        self.historico_tarde(self.TARDE, 14.0, 60.0)
        registro = montar_registro(self.arabica, self.TARDE, 14.0, 60.0, 70.0, status_regra=0)
        novo, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico={})
        assert resultado is not None
        self.assertEqual((resultado.motivo_principal, resultado.status_antecipado, novo.status_alerta),
                         ("GEADA_NOITE", 1, 1))
        self.assertIn("cubra as mudas novas", novo.mensagem_voz)

    def test_margem_conformal_local_e_do_complemento(self) -> None:
        """Mínima de 5 °C: com margem padrão (2 °C) avisa; com q90 local de 0,5 °C (julho = seca) não avisa."""
        self.configurar(regra_temperatura="fisiologica", coef_tmin_local="5,0,0")
        self.historico_tarde(self.TARDE, 14.0, 60.0)
        registro = montar_registro(self.arabica, self.TARDE, 14.0, 60.0, 70.0, status_regra=0)
        _, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico={})
        assert resultado is not None
        self.assertEqual(resultado.motivo_principal, "GEADA_NOITE")
        self.configurar(quantis_tmin_local=json.dumps({"seca": 0.5, "aguas": 3.0}))
        _, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico={})
        assert resultado is not None
        self.assertNotEqual(resultado.motivo_principal, "GEADA_NOITE")
        # limiar e incerteza vêm do banco: um SHT3x (0,2 °C) com mínima de 5 °C e margem 0,5 não avisa;
        # um sensor ruim (3 °C de incerteza) avisa.
        self.configurar(incerteza_sensor_ar_c=3.0)
        _, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico={})
        assert resultado is not None
        self.assertEqual(resultado.motivo_principal, "GEADA_NOITE")

    def test_prioridade_dos_coeficientes(self) -> None:
        """local > complemento (região) > pacote > padrão."""
        configuracao = prognostico.ConfiguracaoPrognostico(regra_temperatura="fisiologica")
        complemento = {"coef_tmin_por_regiao": {"varginha_mg": [1.0, 0.0, 0.0]},
                       "quantis_tmin_por_regiao": {"varginha_mg": {"seca": 1.5, "aguas": 0.7}}}
        pacote = pacote_falso(0.1)
        pacote["coef_tmin"] = (2.0, 0.0, 0.0)
        self.assertEqual(prognostico.escolher_coef_tmin(configuracao, complemento, "varginha_mg", pacote),
                         ((1.0, 0.0, 0.0), "regiao"))
        self.assertEqual(prognostico.escolher_coef_tmin(configuracao, None, "varginha_mg", pacote),
                         ((2.0, 0.0, 0.0), "pacote"))
        self.assertEqual(prognostico.escolher_coef_tmin(configuracao, None, None, None),
                         (prognostico.COEF_TMIN_PADRAO, "padrao"))
        local = prognostico.ConfiguracaoPrognostico(regra_temperatura="fisiologica", coef_tmin_local=(3.0, 0.0, 0.0))
        self.assertEqual(prognostico.escolher_coef_tmin(local, complemento, "varginha_mg", pacote)[1], "local")
        self.assertEqual(prognostico.margem_geada(configuracao, complemento, "varginha_mg", 7), 1.5)
        self.assertEqual(prognostico.margem_geada(configuracao, complemento, "varginha_mg", 12), 0.7)
        self.assertEqual(prognostico.margem_geada(configuracao, None, "varginha_mg", 7), prognostico.MARGEM_GEADA_C)

    def test_instantanea_continua_como_na_v2(self) -> None:
        """Modo instantâneo explícito: T 16 °C / UR 40 % às 18 h continua dando GEADA_NOITE."""
        self.configurar(regra_temperatura="instantanea", coef_tmin_local="30,0,0")  # local só vale na fisiológica
        self.historico_tarde(self.TARDE, 16.0, 40.0)
        registro = montar_registro(self.arabica, self.TARDE, 16.0, 40.0, 70.0)
        novo, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico={})
        assert resultado is not None
        self.assertEqual((resultado.motivo_principal, resultado.status_antecipado, novo.status_alerta),
                         ("GEADA_NOITE", 1, 1))

    def test_motivo_regra_no_vermelho(self) -> None:
        """Vermelho da regra fisiológica com motivo_regra GEADA_AGORA → GEADA_NOITE; CALOR_FLORADA → CALOR_TARDE."""
        self.configurar(regra_temperatura="fisiologica")
        self.historico_tarde(self.TARDE, 14.0, 60.0)
        for motivo_regra, esperado in (("GEADA_AGORA", "GEADA_NOITE"), ("CALOR_FLORADA", "CALOR_TARDE"),
                                       ("CALOR_SEGUIDO", "CALOR_TARDE")):
            registro = montar_registro(self.arabica, self.TARDE, 14.0, 60.0, 70.0, status_regra=2)
            registro.motivo_regra = motivo_regra  # type: ignore[attr-defined]
            novo, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico={})
            assert resultado is not None
            self.assertEqual(resultado.motivo_principal, esperado)
            self.assertIs(novo, registro)
        sem_motivo = montar_registro(self.arabica, self.TARDE, 14.0, 60.0, 70.0, status_regra=2)
        _, resultado = aplicar_antecipacao(sem_motivo, self.arabica, self.banco, pacote_prognostico={})
        assert resultado is not None
        self.assertEqual(resultado.motivo_principal, "FRIO")  # sem motivo_regra: classificação da v2

    def test_configuracao_ilegivel_vira_padrao(self) -> None:
        """Valores inválidos ou banco ausente → padrões da v2 (nunca exceção)."""
        self.configurar(regra_temperatura="qualquer", limiar_geada_abrigo_c="abc", platt_local="1",
                        coef_tmin_local="a,b,c", quantis_tmin_local="{ruim")
        configuracao = prognostico.ler_configuracao_prognostico(self.banco)
        self.assertEqual(configuracao, prognostico.ConfiguracaoPrognostico())
        self.assertEqual(prognostico.ler_configuracao_prognostico(self.pasta / "nao_existe.db"),
                         prognostico.ConfiguracaoPrognostico())


# ---------------------------------------------------------------------------
# 4. Portão de segurança local
# ---------------------------------------------------------------------------
class TestePageHinkley(unittest.TestCase):
    """Detector de piora do log-loss."""

    def test_estacionario_nao_dispara_e_degrau_dispara(self) -> None:
        """500 valores estacionários não disparam; um degrau de +0,5 dispara logo depois.

        Com os padrões (delta 0,005; limiar 50) um degrau de altura h soma no máximo h − delta por passo,
        então nenhum Page-Hinkley dispara antes de 50 / 0,495 ≈ 101 passos. O teste exige ≤ 120 passos
        (a média corrente ainda absorve parte do degrau) e mostra que um degrau de +1,0 dispara em ≤ 60.
        """
        gerador = np.random.default_rng(11)
        estacionario = 0.3 + gerador.normal(0.0, 0.05, 500)
        for altura, maximo in ((0.5, 120), (1.0, 60)):
            detector = desempenho_local.PageHinkley()
            self.assertFalse(any(detector.atualizar(valor) for valor in estacionario))
            passos = next((passo for passo in range(1, 400)
                           if detector.atualizar(0.3 + altura + gerador.normal(0.0, 0.05))), None)
            assert passos is not None
            self.assertGreaterEqual(passos, int(50.0 / (altura - 0.005)))
            self.assertLessEqual(passos, maximo, altura)
        detector.reiniciar()
        self.assertEqual((detector.n, detector.soma, detector.minimo), (0, 0.0, 0.0))
        self.assertFalse(detector.atualizar(float("nan")))


class TestePortaoLocal(BaseBanco):
    """avaliar_e_decidir, Platt, suspensão em aplicar_antecipacao e CLI."""

    INICIO = datetime(2026, 6, 1, 0, 0, 0)
    DIAS = 30

    def popular(self, probabilidade_boa: bool) -> datetime:
        """30 dias de leituras horárias de campo com analise_antecipacao; devolve o 'agora' da avaliação.

        Vermelho (solo seco) em blocos de 6 h a cada 3 dias. Previsões BOAS: 0,9 nas 12 h antes do
        Vermelho e 0,02 no resto; previsões RUINS: 0,9 sempre.
        """
        horas = self.DIAS * 24
        vermelho = np.zeros(horas, dtype=bool)
        for inicio in range(40, horas, 72):
            vermelho[inicio:inicio + 6] = True
        registros = []
        for posicao in range(horas):
            solo = 40.0 if vermelho[posicao] else 70.0
            registros.append(montar_registro(self.arabica, self.INICIO + timedelta(hours=posicao), 20.0, 70.0, solo))
        ids = inserir_leituras_em_lote(registros, caminho_banco=self.banco)
        futuro = np.array([vermelho[posicao + 1:posicao + 13].any() for posicao in range(horas)])
        conexao = sqlite3.connect(str(self.banco))
        try:
            conexao.execute(prognostico._DDL_ANALISE)
            linhas = []
            for posicao, id_leitura in enumerate(ids):
                p = (0.9 if futuro[posicao] else 0.02) if probabilidade_boa else 0.9
                linhas.append((id_leitura, p, "NENHUM", 0, "FALSO-1"))
            conexao.executemany("INSERT INTO analise_antecipacao (id_leitura, probabilidade_risco_futuro, "
                                "motivo_principal, status_antecipado, versao_prognostico) VALUES (?, ?, ?, ?, ?)",
                                linhas)
            conexao.commit()
        finally:
            conexao.close()
        return self.INICIO + timedelta(hours=horas)

    def avaliar(self, agora: datetime) -> Dict[str, Any]:
        """avaliar_e_decidir sem complemento (climatologia = taxa da janela)."""
        return desempenho_local.avaliar_e_decidir(self.banco, agora,
                                                  caminho_complemento=self.pasta / "sem_complemento.json")

    def test_previsoes_resolvidas(self) -> None:
        """Uma previsão por hora não vermelha, alvo do maior status das 12 h seguintes, só de campo."""
        agora = self.popular(probabilidade_boa=True)
        resolvidas = desempenho_local.previsoes_resolvidas(self.banco, agora)
        self.assertEqual(list(resolvidas.columns), list(desempenho_local.COLUNAS_RESOLVIDAS))
        self.assertGreaterEqual(len(resolvidas), 168)
        self.assertTrue(((resolvidas["probabilidade"] > 0.5) == (resolvidas["alvo"] == 1)).all())
        self.assertTrue((resolvidas["hora"] <= agora - timedelta(hours=12)).all())

    def test_previsoes_ruins_suspendem(self) -> None:
        """p = 0,9 sem Vermelho → prognostico_suspenso = true; o modelo deixa de elevar, a geada não."""
        agora = self.popular(probabilidade_boa=False)
        resultado = self.avaliar(agora)
        self.assertTrue(resultado["avaliado"])
        self.assertTrue(resultado["suspenso"])
        self.assertGreaterEqual(resultado["brier_local"], resultado["brier_climatologia"])
        self.assertEqual(obter_configuracao("prognostico_suspenso", caminho_banco=self.banco), "true")
        self.assertIsNotNone(obter_configuracao("prognostico_brier_local_28d", caminho_banco=self.banco))
        self.assertTrue(str(obter_configuracao("prognostico_avaliado_em", caminho_banco=self.banco)).startswith("2026-07-01"))

        # O modelo (p = 0,95) não eleva mais; a versão registrada ganha '-suspenso'.
        momento = datetime(2026, 7, 2, 14, 0, 0)
        self.inserir_historico(momento, [(20.0, 70.0, 70.0)] * 24)
        registro = montar_registro(self.arabica, momento, 20.0, 70.0, 70.0)
        novo, resultado_antecipacao = aplicar_antecipacao(registro, self.arabica, self.banco,
                                                          pacote_prognostico=pacote_falso(0.95))
        assert resultado_antecipacao is not None
        self.assertEqual(novo.status_alerta, 0)
        self.assertEqual(resultado_antecipacao.versao_prognostico, "FALSO-1-suspenso")
        self.assertAlmostEqual(float(resultado_antecipacao.probabilidade_risco_futuro), 0.95)
        definir_configuracao("prognostico_suspenso", False, self.banco)
        novo, _ = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico=pacote_falso(0.95))
        self.assertEqual(novo.status_alerta, 1)  # sem o portão, o mesmo modelo elevaria
        definir_configuracao("prognostico_suspenso", True, self.banco)

        # A geada prevista de 2 °C continua elevando com o prognóstico suspenso.
        definir_configuracao("regra_temperatura", "fisiologica", self.banco)
        definir_configuracao("coef_tmin_local", "2,0,0", self.banco)
        tarde = datetime(2026, 7, 3, 18, 0, 0)
        prognostico.limpar_cache_serie_horaria()
        self.inserir_historico(tarde, [(18.0, 60.0, 70.0)] * 24)
        registro = montar_registro(self.arabica, tarde, 18.0, 60.0, 70.0)
        novo, resultado_antecipacao = aplicar_antecipacao(registro, self.arabica, self.banco,
                                                          pacote_prognostico=pacote_falso(0.95))
        assert resultado_antecipacao is not None
        self.assertEqual((resultado_antecipacao.motivo_principal, novo.status_alerta), ("GEADA_NOITE", 1))

    def test_suspenso_com_pacote_treinado(self) -> None:
        """Com um pacote REAL (Random Forest treinado), suspenso: nada sobe por 'modelo' e a versão marca '-suspenso'."""
        banco_dataset = self.pasta / "dataset.db"
        inicializar_banco(banco_dataset)
        parametros = {codigo: buscar_parametros_cultura(codigo, caminho_banco=banco_dataset)
                      for codigo in ("cafe_arabica", "cafe_conilon")}
        series = [simular_serie_temporal(parametros["cafe_arabica"], 1500, semente=3),
                  simular_serie_temporal(parametros["cafe_conilon"], 1500, semente=4)]
        limpo = calcular_features_dataframe(pd.concat(series, ignore_index=True), parametros)
        df_prog = treinamento_ia.preparar_dataset_prognostico(limpo, parametros)
        pacote = treinamento_ia.treinar_modelo_prognostico(df_prog, parametros_por_cultura=parametros)
        pacote["limiar"] = 0.0  # qualquer probabilidade dispararia o gatilho do modelo sem o portão
        definir_configuracao("prognostico_suspenso", True, self.banco)
        momento = datetime(2026, 3, 10, 14, 0)
        self.inserir_historico(momento, [(20.0, 70.0, 70.0)] * 24)
        registro = montar_registro(self.arabica, momento, 20.0, 70.0, 70.0)
        novo, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico=pacote)
        assert resultado is not None
        self.assertEqual(novo.status_alerta, 0)
        self.assertTrue(resultado.versao_prognostico.endswith("-suspenso"))
        self.assertIsNotNone(resultado.probabilidade_risco_futuro)

    def test_previsoes_boas_nao_suspendem_e_ajustam_platt(self) -> None:
        """Previsões boas → prognostico_suspenso = false e Platt gravado (≥ 200 resolvidas, ≥ 20 positivas)."""
        agora = self.popular(probabilidade_boa=True)
        resultado = self.avaliar(agora)
        self.assertTrue(resultado["avaliado"])
        self.assertFalse(resultado["suspenso"])
        self.assertFalse(resultado["page_hinkley_disparou"])
        self.assertLess(resultado["brier_local"], resultado["brier_climatologia"])
        self.assertEqual(obter_configuracao("prognostico_suspenso", caminho_banco=self.banco), "false")
        self.assertGreaterEqual(resultado["n_positivas"], 20)
        platt = obter_configuracao("platt_local", caminho_banco=self.banco)
        assert platt is not None
        a, b = (float(valor) for valor in platt.split(","))
        self.assertGreater(a, 0.0)
        configuracao = prognostico.ler_configuracao_prognostico(self.banco)
        self.assertEqual(configuracao.platt_local, (a, b))

    def test_poucos_dados_nao_decidem(self) -> None:
        """Menos de 168 previsões resolvidas: nada é gravado e a decisão anterior continua."""
        definir_configuracao("prognostico_suspenso", True, self.banco)
        resultado = self.avaliar(datetime(2026, 6, 1))
        self.assertFalse(resultado["avaliado"])
        self.assertEqual(obter_configuracao("prognostico_suspenso", caminho_banco=self.banco), "true")
        self.assertFalse(desempenho_local.avaliar_e_decidir(self.pasta / "nao_existe.db")["avaliado"])

    def test_platt_antes_do_limiar(self) -> None:
        """platt_local '1,-3' leva p = 0,9 a ≈ 0,31: abaixo do limiar 0,5, o modelo não eleva."""
        self.assertAlmostEqual(prognostico.calibrar_platt(0.9, (1.0, -3.0)), 1 / (1 + math.exp(-(math.log(9) - 3))))
        self.assertEqual(prognostico.calibrar_platt(0.42, None), 0.42)
        momento = datetime(2026, 3, 10, 14, 0)
        self.inserir_historico(momento, [(20.0, 70.0, 70.0)] * 24)
        registro = montar_registro(self.arabica, momento, 20.0, 70.0, 70.0)
        novo, _ = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico=pacote_falso(0.9))
        self.assertEqual(novo.status_alerta, 1)
        definir_configuracao("platt_local", "1,-3", self.banco)
        novo, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico=pacote_falso(0.9))
        assert resultado is not None
        self.assertEqual(novo.status_alerta, 0)
        self.assertAlmostEqual(float(resultado.probabilidade_risco_futuro), 0.9)  # gravada a do modelo

    def test_linha_de_comando(self) -> None:
        """python src/desempenho_local.py --banco <banco de teste> sai com código 0."""
        agora_real = datetime.now().replace(minute=0, second=0, microsecond=0)
        self.INICIO = agora_real - timedelta(days=self.DIAS)  # dados dentro da janela de 28 dias de "agora"
        self.popular(probabilidade_boa=True)
        processo = subprocess.run([sys.executable, str(PASTA_SRC / "desempenho_local.py"), "--banco", str(self.banco)],
                                  capture_output=True, text=True, timeout=180, cwd=str(RAIZ_PROJETO))
        self.assertEqual(processo.returncode, 0, processo.stderr)
        resumo = json.loads(processo.stdout)
        self.assertTrue(resumo["avaliado"])
        self.assertIn(obter_configuracao("prognostico_suspenso", caminho_banco=self.banco), ("true", "false"))


class TesteMinimaLocal(BaseBanco):
    """ajustar_coef_tmin_local em noites sintéticas sem ruído."""

    def test_recupera_coeficientes(self) -> None:
        """30 noites de Tmin = −4 + 0,55·T_tarde + 0,5·Td_tarde → coeficientes recuperados ± 0,1."""
        gerador = np.random.default_rng(5)
        inicio = datetime(2026, 5, 1, 0, 0, 0)
        registros: List[RegistroLeitura] = []
        tardes = [(float(t), float(t - gerador.uniform(2.0, 14.0))) for t in gerador.uniform(14.0, 26.0, 31)]
        minimas = [-4.0 + 0.55 * t_tarde + 0.5 * td_tarde for t_tarde, td_tarde in tardes]
        for dia in range(31):
            t_tarde, td_tarde = tardes[dia]
            ur_tarde = float(umidade_relativa_magnus(t_tarde, td_tarde))
            for hora in range(24):
                # A madrugada (0 h–6 h) pertence à noite que começou na tarde ANTERIOR.
                minima = minimas[dia] if hora >= 7 else minimas[max(0, dia - 1)]
                if 17 <= hora <= 19:
                    temperatura, umidade = t_tarde, ur_tarde
                elif hora >= 20:
                    temperatura, umidade = minima + 0.5 * (24 - hora), 80.0
                elif hora <= 6:
                    temperatura, umidade = minima + (0.0 if hora == 5 else 0.3 + 0.1 * abs(hora - 5)), 85.0
                else:
                    temperatura, umidade = t_tarde + 3.0, 60.0
                registros.append(montar_registro(self.arabica, inicio + timedelta(days=dia, hours=hora),
                                                 round(temperatura, 4), round(umidade, 6), 70.0))
        inserir_leituras_em_lote(registros, caminho_banco=self.banco)
        coef = desempenho_local.ajustar_coef_tmin_local(self.banco)
        assert coef is not None
        for obtido, esperado in zip(coef, (-4.0, 0.55, 0.5)):
            self.assertAlmostEqual(obtido, esperado, delta=0.1)
        configuracao = prognostico.ler_configuracao_prognostico(self.banco)
        assert configuracao.coef_tmin_local is not None
        self.assertAlmostEqual(configuracao.coef_tmin_local[1], 0.55, delta=0.1)
        self.assertIn("seca", configuracao.quantis_tmin_local)
        self.assertLess(configuracao.quantis_tmin_local["seca"], 0.1)

    def test_poucas_noites(self) -> None:
        """Sem noites suficientes: None e nada gravado."""
        self.assertIsNone(desempenho_local.ajustar_coef_tmin_local(self.banco))
        self.assertIsNone(obter_configuracao("coef_tmin_local", caminho_banco=self.banco))


class TesteUnidadesSystemd(unittest.TestCase):
    """deploy/agroanalise-desempenho.service e .timer."""

    def test_timer_de_hora_em_hora(self) -> None:
        """Timer horário, persistente, apontando para o serviço oneshot que roda desempenho_local.py."""
        timer = (RAIZ_PROJETO / "deploy" / "agroanalise-desempenho.timer").read_text(encoding="utf-8")
        servico = (RAIZ_PROJETO / "deploy" / "agroanalise-desempenho.service").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=hourly", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("Unit=agroanalise-desempenho.service", timer)
        self.assertIn("Type=oneshot", servico)
        self.assertIn("src/desempenho_local.py", servico)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze indisponível")
    def test_systemd_analyze_verify(self) -> None:
        """systemd-analyze verify não aponta erro nem aviso nas duas unidades."""
        processo = subprocess.run(["systemd-analyze", "verify",
                                   str(RAIZ_PROJETO / "deploy" / "agroanalise-desempenho.service"),
                                   str(RAIZ_PROJETO / "deploy" / "agroanalise-desempenho.timer")],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(processo.returncode, 0, processo.stderr)
        self.assertNotIn("agroanalise-desempenho", processo.stderr)


# ---------------------------------------------------------------------------
# 5. Certeza falada em dois níveis
# ---------------------------------------------------------------------------
FRASES_V2: List[Tuple[str, Optional[float], Optional[float], int, str]] = [
    ("SECA_CHEGANDO", 14.0, 0.0, 18, "Ainda está tudo bem, mas a terra está secando e deve ficar seca amanhã de manhã. "
                                     "Regue hoje no fim da tarde."),
    ("SECA_CHEGANDO", None, None, 10, "A terra está seca há mais de um dia e deve ficar muito seca nas próximas horas. "
                                      "Regue hoje no fim da tarde."),
    ("SECA_CHEGANDO", 5.0, 9.0, 3, "A terra está seca há 9 horas e deve ficar muito seca hoje de manhã. "
                                   "Regue agora, devagar, até a terra ficar úmida."),
    ("SECA_CHEGANDO", 2.0, 1.0, 13, "A terra começou a secar e deve ficar muito seca daqui a pouco. "
                                    "Regue agora, devagar, até a terra ficar úmida."),
    ("SECA_AGORA", 14.0, 0.0, 18, "A terra está seca. Regue agora."),
    ("SECA_AGORA", None, None, 10, "A terra está seca há mais de um dia. Regue agora."),
    ("SECA_AGORA", 5.0, 9.0, 3, "A terra está seca há 9 horas. Regue agora."),
    ("ENCHARCAMENTO", 14.0, 0.0, 18, "A terra está ficando molhada demais. Não regue hoje e abra valetas se a água empoçar."),
    ("ENCHARCAMENTO", 5.0, 9.0, 3, "A terra está molhada demais há 9 horas. Não regue hoje e abra valetas se a água "
                                   "empoçar."),
    ("CALOR_TARDE", 14.0, 0.0, 18, "Deve fazer muito calor amanhã de manhã. Faça sombra nas mudas novas e regue no fim "
                                   "da tarde para refrescar."),
    ("CALOR_TARDE", 5.0, 9.0, 3, "Está quente há 9 horas e deve esquentar mais hoje de manhã. Faça sombra nas mudas "
                                 "novas e regue no fim da tarde para refrescar."),
    ("GEADA_NOITE", 14.0, 0.0, 18, "Esta noite pode gear. Antes de escurecer, cubra as mudas novas e chegue terra no pé "
                                   "das plantas."),
    ("GEADA_NOITE", None, None, 10, "Esta noite deve fazer muito frio. Proteja as mudas novas e chegue terra no pé das "
                                    "plantas."),
    ("GEADA_NOITE", 5.0, 9.0, 3, "Pode gear ainda nesta madrugada. Cubra as mudas novas agora e chegue terra no pé das "
                                 "plantas."),
    ("FRIO", 14.0, 0.0, 18, "Esta noite vai fazer frio. Proteja as mudas novas."),
    ("FRIO", 5.0, 9.0, 3, "Vai fazer mais frio nesta madrugada. Proteja as mudas novas."),
    ("NENHUM", 14.0, 0.0, 18, ""),
]


class TesteCertezaFalada(BaseBanco):
    """gerar_frase_antecipacao(..., probabilidade=...) e a integração em aplicar_antecipacao."""

    AGORA = datetime(2026, 7, 10, 18, 0, 0)

    def test_quase_certo_e_pode_ser(self) -> None:
        """p = 0,85 → 'É quase certo que'; p = 0,5 → 'Pode ser que' (com SECA_CHEGANDO)."""
        alta = gerar_frase_antecipacao("SECA_CHEGANDO", 14.0, 0.0, self.AGORA, self.arabica, probabilidade=0.85)
        media = gerar_frase_antecipacao("SECA_CHEGANDO", 14.0, 0.0, self.AGORA, self.arabica, probabilidade=0.5)
        self.assertIn("É quase certo que", alta)
        self.assertIn("Pode ser que", media)
        self.assertNotIn("Pode ser que", alta)
        self.assertNotIn("É quase certo", media)
        self.assertIn("Regue hoje no fim da tarde.", alta)
        self.assertEqual(alta, "É quase certo que a terra fique muito seca amanhã de manhã. Regue hoje no fim da tarde.")

    def test_sem_porcentagem_nem_numero_de_probabilidade(self) -> None:
        """Nenhuma variante tem '%' nem os dígitos da probabilidade."""
        for motivo in prognostico.MOTIVOS:
            for probabilidade in (0.55, 0.67, 0.85, 0.93):
                frase = gerar_frase_antecipacao(motivo, 14.0, 0.0, self.AGORA, self.arabica, probabilidade=probabilidade)
                self.assertNotIn("%", frase)
                self.assertIsNone(re.search(r"\d", frase), (motivo, frase))
                self.assertNotIn(str(int(round(probabilidade * 100))), frase)
        for niveis in prognostico.FRASES_CERTEZA.values():
            self.assertEqual(set(niveis), set(prognostico.NIVEIS_CERTEZA))
            for texto in niveis.values():
                self.assertIsNone(re.search(r"\d|%", texto.replace("{prazo}", "")))

    def test_sem_probabilidade_identica_a_v2(self) -> None:
        """probabilidade=None → a mesma string da v2 para os 7 motivos."""
        self.assertEqual({caso[0] for caso in FRASES_V2}, set(prognostico.MOTIVOS))
        for motivo, prazo, estresse, hora, esperado in FRASES_V2:
            agora = datetime(2026, 7, 10, hora, 0)
            self.assertEqual(gerar_frase_antecipacao(motivo, prazo, estresse, agora, self.arabica), esperado)
            self.assertEqual(gerar_frase_antecipacao(motivo, prazo, estresse, agora, self.arabica, probabilidade=None),
                             esperado)

    def test_acoes_coerentes_com_a_rega(self) -> None:
        """derivar_acoes acha a mesma ação de rega, com as mesmas palavras-chave, nas frases com certeza."""
        for prazo, estresse, hora in ((14.0, 0.0, 18), (2.0, 1.0, 13), (5.0, 9.0, 3)):
            agora = datetime(2026, 7, 10, hora, 0)
            v2 = gerar_frase_antecipacao("SECA_CHEGANDO", prazo, estresse, agora, self.arabica)
            for probabilidade in (0.85, 0.55):
                frase = gerar_frase_antecipacao("SECA_CHEGANDO", prazo, estresse, agora, self.arabica,
                                                probabilidade=probabilidade)
                acoes = acoes_orientacao.derivar_acoes(1, 20.0, 62.0, self.arabica, motivo_antecipacao="SECA_CHEGANDO",
                                                       mensagem_voz=frase)
                acoes_v2 = acoes_orientacao.derivar_acoes(1, 20.0, 62.0, self.arabica,
                                                          motivo_antecipacao="SECA_CHEGANDO", mensagem_voz=v2)
                rega = [acao for acao in acoes if acao in acoes_orientacao.ACOES_DE_REGA]
                self.assertEqual(rega, [acao for acao in acoes_v2 if acao in acoes_orientacao.ACOES_DE_REGA])
                self.assertTrue(rega)
                self.assertTrue(acoes_orientacao.acoes_coerentes_com_mensagem(rega, frase))
        geada = gerar_frase_antecipacao("GEADA_NOITE", 11.0, 0.0, self.AGORA, self.arabica, probabilidade=0.9)
        self.assertIn("chegue terra", geada)
        self.assertIn("cubra as mudas", geada)

    def test_certeza_so_no_gatilho_do_modelo(self) -> None:
        """Amarelo pelo modelo: certeza na voz (quase certo com 0,9; pode ser com 0,6); secagem: frase da v2."""
        momento = datetime(2026, 3, 10, 14, 0)
        self.inserir_historico(momento, [(20.0, 70.0, 70.0)] * 24)
        registro = montar_registro(self.arabica, momento, 20.0, 70.0, 70.0)
        novo, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico=pacote_falso(0.9))
        assert resultado is not None
        self.assertEqual(novo.status_alerta, 1)
        self.assertIn("É quase certo que", novo.mensagem_voz)
        self.assertNotIn("%", novo.mensagem_voz)
        novo, _ = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico=pacote_falso(0.6))
        self.assertIn("Pode ser que", novo.mensagem_voz)
        # Secagem (gatilho físico, sem modelo): nenhuma frase de certeza.
        prognostico.limpar_cache_serie_horaria()
        outro = momento + timedelta(days=3)
        self.inserir_historico(outro, [(20.0, 70.0, 62.0 + (23 - hora)) for hora in range(24)])
        registro = montar_registro(self.arabica, outro, 20.0, 70.0, 61.0)
        novo, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico={})
        assert resultado is not None
        self.assertEqual(resultado.motivo_principal, "SECA_CHEGANDO")
        self.assertNotIn("quase certo", novo.mensagem_voz)
        self.assertNotIn("Pode ser que", novo.mensagem_voz)


if __name__ == "__main__":
    unittest.main()
