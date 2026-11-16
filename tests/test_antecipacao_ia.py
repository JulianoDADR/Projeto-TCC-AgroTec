"""Testes da frente "Antecipação de estresse" (unittest da biblioteca padrão).

Cobrem os critérios de aceite de:

    * simulador agronomicamente realista (estações, geada radiativa, veranico,
      escaldadura, ano completo) e ``ponto_orvalho_magnus``;
    * IA de prognóstico de 12 h em tempo real (``aplicar_antecipacao``):
      Amarelo antecipado por secagem e por modelo, segurança (nunca cria
      Vermelho, nunca rebaixa, só leituras de campo com histórico), ETA de
      secagem, prazo falado, memória de estresse e persistência própria;
    * aviso de geada ainda à tarde e a nova frase de geada acontecendo;
    * avaliação honesta: divisão temporal, robustez a ruído, calibração e
      antecipação, além das metas do treino no teste temporal do simulador.

Sempre com banco SQLite TEMPORÁRIO. Execução (raiz do projeto):
``python -m unittest tests.test_antecipacao_ia -v``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple
from unittest import mock

RAIZ_PROJETO: Path = Path(__file__).resolve().parent.parent
PASTA_SRC: Path = RAIZ_PROJETO / "src"
if str(PASTA_SRC) not in sys.path:
    sys.path.insert(0, str(PASTA_SRC))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import database  # noqa: E402
import gerador_dataset  # noqa: E402
import prognostico  # noqa: E402
import treinamento_ia  # noqa: E402
from database import (  # noqa: E402
    ParametrosCultura,
    RegistroLeitura,
    buscar_parametros_cultura,
    inicializar_banco,
    inserir_leitura,
)
from gerador_dataset import (  # noqa: E402
    calcular_features,
    calcular_features_dataframe,
    calcular_status_regra,
    gerar_mensagem_orientacao,
    ponto_orvalho_magnus,
    simular_serie_temporal,
)
from prognostico import (  # noqa: E402
    EXPRESSOES_PRAZO,
    MOTIVOS,
    ResultadoAntecipacao,
    aplicar_antecipacao,
    estimar_horas_ate_secar,
    falar_prazo,
    gravar_analise_antecipacao,
    obter_analise_antecipacao,
    prever_temperatura_minima_noite,
)

FORMATO: str = "%Y-%m-%d %H:%M:%S"


def setUpModule() -> None:
    """Silencia os logs informativos/avisos durante os testes."""
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
    """Pacote de prognóstico mínimo com o modelo falso."""
    return {
        "modelo": ModeloFalso(probabilidade), "features": list(prognostico.COLUNAS_FEATURES_PROGNOSTICO),
        "limiar": limiar, "horizonte_horas": 12, "culturas": ["cafe_arabica", "cafe_conilon"],
        "coef_tmin": None, "metricas": {}, "versao": "FALSO-1", "treinado_em": "2026-01-01T00:00:00",
    }


def montar_registro(
    parametros: ParametrosCultura, instante: datetime, temperatura: float, umidade_ar: float, solo: float,
    origem: str = "CAMPO_USB",
) -> RegistroLeitura:
    """Registro coerente (features, status da regra e mensagem) como o coletor montaria."""
    deficit, desvio = calcular_features(temperatura, solo, parametros)
    status = calcular_status_regra(temperatura, solo, parametros)
    return RegistroLeitura(
        data_hora=instante.strftime(FORMATO), codigo_cultura=parametros.codigo, origem=origem,
        temperatura_ar_bruta=temperatura, umidade_ar_bruta=umidade_ar, umidade_solo_bruta=solo,
        temperatura_ar=temperatura, umidade_ar=umidade_ar, umidade_solo=solo,
        deficit_hidrico_solo=deficit, desvio_termico=desvio,
        status_regra=status, status_predito=status, status_alerta=status, probabilidade_predicao=1.0,
        flag_anomalia=0, descricao_anomalia="",
        mensagem_voz=gerar_mensagem_orientacao(status, deficit, desvio, parametros, temperatura, solo),
        versao_modelo="REGRAS",
    )


class BaseBanco(unittest.TestCase):
    """Banco temporário isolado e cache do prognóstico limpo a cada teste."""

    def setUp(self) -> None:
        """Cria o banco temporário com as culturas semeadas."""
        self._diretorio = tempfile.mkdtemp(prefix="agroanalise_antecipacao_")
        self.pasta = Path(self._diretorio)
        self.banco = self.pasta / "teste.db"
        inicializar_banco(self.banco)
        self.arabica = buscar_parametros_cultura("cafe_arabica", caminho_banco=self.banco)
        prognostico.limpar_cache_serie_horaria()

    def tearDown(self) -> None:
        """Remove o diretório temporário."""
        prognostico.limpar_cache_serie_horaria()
        shutil.rmtree(self._diretorio, ignore_errors=True)

    def inserir_historico(
        self, agora: datetime, valores: Sequence[Tuple[float, float, float]], origem: str = "CAMPO_USB"
    ) -> List[int]:
        """Insere leituras horárias que TERMINAM uma hora antes de ``agora`` (a última tupla é a mais recente)."""
        ids: List[int] = []
        total = len(valores)
        for posicao, (temperatura, umidade_ar, solo) in enumerate(valores):
            instante = agora - timedelta(hours=total - posicao)
            ids.append(inserir_leitura(montar_registro(self.arabica, instante, temperatura, umidade_ar, solo, origem),
                                       caminho_banco=self.banco))
        return ids


def campos_diferentes(original: RegistroLeitura, novo: RegistroLeitura) -> List[str]:
    """Nomes dos campos que mudaram entre dois registros."""
    return [campo.name for campo in dataclasses.fields(RegistroLeitura)
            if getattr(original, campo.name) != getattr(novo, campo.name)]


def contar_frases(texto: str) -> int:
    """Quantidade de frases (terminadas por . ! ?)."""
    return len([parte for parte in re.split(r"(?<=[.!?])\s+", texto.strip()) if parte])


# ---------------------------------------------------------------------------
# 1. Simulador realista
# ---------------------------------------------------------------------------
class TesteSimuladorRealista(unittest.TestCase):
    """simular_serie_temporal(arabica, 8760, semente=42): estações, geada, veranico, chuva, solo."""

    @classmethod
    def setUpClass(cls) -> None:
        """Simula um ano do Arábica uma única vez para todos os testes da classe."""
        cls._diretorio = tempfile.mkdtemp(prefix="agroanalise_simulador_")
        cls.banco = Path(cls._diretorio) / "teste.db"
        inicializar_banco(cls.banco)
        cls.arabica = buscar_parametros_cultura("cafe_arabica", caminho_banco=cls.banco)
        cls.conilon = buscar_parametros_cultura("cafe_conilon", caminho_banco=cls.banco)
        cls.serie = simular_serie_temporal(cls.arabica, 8760, semente=42)
        cls.momentos = pd.to_datetime(cls.serie["data_hora"])
        cls.subidas = cls.serie["umidade_solo"].diff()

    @classmethod
    def tearDownClass(cls) -> None:
        """Remove o diretório temporário."""
        shutil.rmtree(cls._diretorio, ignore_errors=True)

    def test_ano_completo_e_colunas(self) -> None:
        """HORAS_SIMULACAO = 8760 e as colunas de saída não mudaram."""
        self.assertEqual(gerador_dataset.HORAS_SIMULACAO, 8760)
        self.assertEqual(list(self.serie.columns), ["data_hora", "cultura", "temperatura_ar", "umidade_ar", "umidade_solo"])
        self.assertEqual(len(self.serie), 8760)
        self.assertEqual(self.serie["data_hora"].iloc[0], "2026-01-01 00:00:00")

    def test_a_geada_so_em_madrugadas_de_inverno(self) -> None:
        """(a) Há horas abaixo de critico_min, todas de maio a agosto e entre 0 h e 8 h."""
        abaixo = self.serie["temperatura_ar"] < self.arabica.faixa("temperatura_ar").critico_min
        self.assertGreaterEqual(int(abaixo.sum()), 1)
        self.assertTrue(set(self.momentos[abaixo].dt.month) <= {5, 6, 7, 8})
        self.assertTrue(set(self.momentos[abaixo].dt.hour) <= set(range(0, 9)))

    def test_b_inverno_mais_frio_que_verao(self) -> None:
        """(b) Média de jun./jul. pelo menos 3 °C abaixo da média de jan./fev."""
        inverno = self.serie.loc[self.momentos.dt.month.isin([6, 7]).to_numpy(), "temperatura_ar"].mean()
        verao = self.serie.loc[self.momentos.dt.month.isin([1, 2]).to_numpy(), "temperatura_ar"].mean()
        self.assertLessEqual(inverno, verao - 3.0)

    def test_c_chuva_concentrada_nas_aguas(self) -> None:
        """(c) Subidas de solo (> 2 pp/h, proxy de chuva) de mai.–set. < 30 % das de out.–mar."""
        chuva = self.subidas.where(self.subidas > 2.0, 0.0).fillna(0.0)
        seca = chuva[self.momentos.dt.month.isin([5, 6, 7, 8, 9]).to_numpy()].sum()
        aguas = chuva[self.momentos.dt.month.isin([10, 11, 12, 1, 2, 3]).to_numpy()].sum()
        self.assertGreater(aguas, 0.0)
        self.assertLess(seca, 0.3 * aguas)

    def test_d_veranico_de_dez_dias(self) -> None:
        """(d) Algum período de 10 dias seguidos, entre jan. e mar., sem subida de solo acima de 2 pp/h."""
        no_periodo = self.momentos.dt.month.isin([1, 2, 3]).to_numpy()
        chuvoso = (self.subidas > 2.0).fillna(False).to_numpy()
        maior, corrente = 0, 0
        for posicao in range(len(self.serie)):
            corrente = corrente + 1 if (no_periodo[posicao] and not chuvoso[posicao]) else 0
            maior = max(maior, corrente)
        self.assertGreaterEqual(maior, 240)

    def test_e_classes_equilibradas(self) -> None:
        """(e) Cada cor com pelo menos 10 % e o dataset completo sem aviso de PROPORCAO_MINIMA_CLASSE."""
        rotulado = calcular_features_dataframe(self.serie, {"cafe_arabica": self.arabica})
        proporcoes = rotulado["status_alerta"].value_counts(normalize=True)
        for classe in (0, 1, 2):
            self.assertGreaterEqual(float(proporcoes.get(classe, 0.0)), 0.10, f"classe {classe}")
        pasta = Path(self._diretorio)
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            _, limpo = gerador_dataset.gerar_dataset_completo(
                caminho_bruto=pasta / "bruto.csv", caminho_limpo=pasta / "limpo.csv", caminho_banco=self.banco)
        self.assertNotIn("AVISO", saida.getvalue())
        geral = limpo["status_alerta"].value_counts(normalize=True)
        for classe in (0, 1, 2):
            self.assertGreaterEqual(float(geral.get(classe, 0.0)), gerador_dataset.PROPORCAO_MINIMA_CLASSE)

    def test_f_solo_sem_quedas_impossiveis(self) -> None:
        """(f) A maior queda horária da umidade do solo fica abaixo de 4 pp/h."""
        self.assertLess(float((-self.subidas).max()), 4.0)

    def test_g_ponto_de_orvalho_magnus(self) -> None:
        """(g) ponto_orvalho_magnus(12, 40) entre −2,0 e −0,5; aceita vetores e satura em 100 %."""
        valor = ponto_orvalho_magnus(12, 40)
        self.assertIsInstance(valor, float)
        self.assertTrue(-2.0 <= valor <= -0.5, valor)
        vetor = ponto_orvalho_magnus(np.array([20.0, 25.0]), np.array([100.0, 50.0]))
        self.assertAlmostEqual(float(vetor[0]), 20.0, places=6)
        self.assertLess(float(vetor[1]), 25.0)

    def test_escaldadura_rara(self) -> None:
        """(e da especificação) Escaldadura acima de critico_max existe, mas é rara (< 1 % das horas)."""
        acima = (self.serie["temperatura_ar"] > self.arabica.faixa("temperatura_ar").critico_max).sum()
        self.assertGreaterEqual(int(acima), 1)
        self.assertLess(int(acima), 88)

    def test_relatorio_realismo(self) -> None:
        """gerar_relatorio_realismo traz geada por mês, veranico, chuva e médias mensais."""
        texto = gerador_dataset.gerar_relatorio_realismo(self.serie, self.arabica)
        for trecho in ("T média", "h<crít.mín", "Chuva", "veranico", "Horas abaixo de critico_min"):
            self.assertIn(trecho, texto)

    def test_qualquer_cultura_funciona(self) -> None:
        """Cultura nova (tomate) e série curta também funcionam, dentro dos limites físicos."""
        tomate = ParametrosCultura(
            id_cultura=99, codigo="tomate", nome_comum="Tomate", nome_cientifico="", categoria="hortalica",
            faixas={
                "temperatura_ar": database.FaixaParametro("temperatura_ar", "°C", 20.0, 26.0, 16.0, 30.0, 10.0, 35.0),
                "umidade_ar": database.FaixaParametro("umidade_ar", "%", 60.0, 80.0, 50.0, 90.0, 40.0, 95.0),
                "umidade_solo": database.FaixaParametro("umidade_solo", "%", 60.0, 80.0, 50.0, 88.0, 35.0, 95.0),
            })
        for horas in (30, 2000):
            serie = simular_serie_temporal(tomate, horas, semente=5, data_inicial="2026-05-10 13:00:00")
            self.assertEqual(len(serie), horas)
            for coluna, (minimo, maximo) in gerador_dataset.LIMITES_FISICOS_SENSORES.items():
                self.assertTrue(serie[coluna].between(minimo, maximo).all(), coluna)


# ---------------------------------------------------------------------------
# 2. Features temporais, alvo e utilidades de linguagem
# ---------------------------------------------------------------------------
class TesteFeaturesEPrazo(BaseBanco):
    """Alvo do prognóstico, features relativas, ETA de secagem e prazo falado."""

    def test_alvo_risco_futuro(self) -> None:
        """Alvo = 1 se algum Vermelho em t+1..t+h; NaN nas últimas h horas."""
        alvo = prognostico.calcular_alvo_risco_futuro(pd.Series([0, 1, 0, 2, 0, 0]), horizonte=2)
        self.assertEqual(alvo.iloc[:4].tolist(), [0.0, 1.0, 1.0, 0.0])
        self.assertTrue(alvo.iloc[4:].isna().all())

    def test_features_temporais(self) -> None:
        """Todas as colunas existem; as 24 primeiras horas ficam incompletas; o resto é finito."""
        serie = simular_serie_temporal(self.arabica, 200, semente=3)
        features = prognostico.calcular_features_temporais(serie, self.arabica)
        self.assertEqual(list(features.columns), prognostico.COLUNAS_FEATURES_PROGNOSTICO)
        self.assertEqual(len(features), 200)
        self.assertTrue(features.iloc[:23].isna().any(axis=1).all())
        self.assertTrue(np.isfinite(features.iloc[30:].to_numpy()).all())
        self.assertTrue(((features["horas_ate_secar"].iloc[30:] >= 0) & (features["horas_ate_secar"].iloc[30:] <= 72)).all())

    def test_f_estimar_horas_ate_secar(self) -> None:
        """(f) 48 h perdendo 0,5 pp/h e 10 pp acima da borda → 18–22 h; rega de +15 pp → None."""
        faixa = self.arabica.faixa("umidade_solo")
        borda = faixa.atencao_min
        perdendo = pd.Series([borda + 10.0 + 0.5 * (47 - hora) for hora in range(48)])
        eta = estimar_horas_ate_secar(perdendo, faixa)
        self.assertIsNotNone(eta)
        self.assertTrue(18.0 <= float(eta) <= 22.0, eta)
        regado = perdendo.copy()
        regado.iloc[40:] += 15.0
        self.assertIsNone(estimar_horas_ate_secar(regado, faixa))
        self.assertIsNone(estimar_horas_ate_secar(pd.Series([70.0] * 30), faixa))
        self.assertEqual(estimar_horas_ate_secar(pd.Series([borda - 1.0] * 30), faixa), 0.0)

    def test_g_falar_prazo(self) -> None:
        """(g) falar_prazo(14, 18 h) = 'amanhã de manhã'; nunca fala números (só 'uns 2/3 dias')."""
        self.assertEqual(falar_prazo(14, datetime(2026, 7, 10, 18, 0)), "amanhã de manhã")
        self.assertEqual(falar_prazo(2, datetime(2026, 7, 10, 9, 0)), "daqui a pouco")
        self.assertEqual(falar_prazo(5, datetime(2026, 7, 10, 10, 0)), "hoje à tarde")
        self.assertEqual(falar_prazo(11, datetime(2026, 7, 10, 14, 0)), "esta noite")
        for hora in range(24):
            for prazo in (0.5, 4, 7, 10, 13, 17, 22, 30, 45, 70):
                texto = falar_prazo(prazo, datetime(2026, 7, 10, hora, 15))
                self.assertIn(texto, EXPRESSOES_PRAZO)
                self.assertIsNone(re.search(r"\d", texto.replace("uns 2", "").replace("uns 3", "")), texto)

    def test_gerar_frase_antecipacao(self) -> None:
        """Frases curtas para todos os motivos; motivo inválido é recusado."""
        agora = datetime(2026, 7, 10, 14, 0)
        for motivo in MOTIVOS:
            frase = prognostico.gerar_frase_antecipacao(motivo, 6.0, 0.0, agora, self.arabica)
            if motivo == "NENHUM":
                self.assertEqual(frase, "")
            else:
                self.assertTrue(1 <= contar_frases(frase) <= 3, frase)
                self.assertNotIn("°", frase)
                self.assertNotIn("%", frase)
        self.assertEqual(prognostico.gerar_frase_antecipacao("SECA_AGORA", 0.0, 9.0, agora, self.arabica),
                         "A terra está seca há 9 horas. Regue agora.")
        with self.assertRaises(ValueError):
            prognostico.gerar_frase_antecipacao("CHUVA", None, None, agora, self.arabica)


# ---------------------------------------------------------------------------
# 3. Antecipação em tempo real
# ---------------------------------------------------------------------------
class TesteAntecipacaoTempoReal(BaseBanco):
    """aplicar_antecipacao com leituras CAMPO_USB horárias sintéticas no banco temporário."""

    AGORA = datetime(2026, 3, 10, 14, 0, 0)

    def serie_secando(self) -> List[Tuple[float, float, float]]:
        """24 h de secagem a 1 pp/h que terminam com o solo em 62 % (a leitura atual será 61 %)."""
        return [(20.0, 70.0, 62.0 + (23 - hora)) for hora in range(24)]

    def test_a_secagem_sem_modelo_vira_amarelo(self) -> None:
        """(a) Solo ainda Verde, ETA ≤ 12 h, sem modelo → Amarelo antecipado por SECA_CHEGANDO."""
        self.inserir_historico(self.AGORA, self.serie_secando())
        registro = montar_registro(self.arabica, self.AGORA, 20.0, 70.0, 61.0)
        self.assertEqual(registro.status_regra, 0)
        novo, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico={})
        assert resultado is not None
        self.assertEqual(novo.status_regra, 0)
        self.assertEqual(novo.status_alerta, 1)
        self.assertEqual(novo.status_predito, 1)
        self.assertEqual(resultado.motivo_principal, "SECA_CHEGANDO")
        self.assertEqual(resultado.status_antecipado, 1)
        self.assertIsNone(resultado.probabilidade_risco_futuro)
        self.assertEqual(resultado.versao_prognostico, prognostico.VERSAO_SEM_PROGNOSTICO)
        self.assertTrue(resultado.horas_ate_secar is not None and resultado.horas_ate_secar <= 12.0)
        self.assertTrue(novo.mensagem_voz.startswith("Atenção com a plantação de café arábica"))
        self.assertTrue(any(expressao in novo.mensagem_voz for expressao in EXPRESSOES_PRAZO), novo.mensagem_voz)
        self.assertLessEqual(contar_frases(novo.mensagem_voz), 4)
        self.assertEqual(set(campos_diferentes(registro, novo)), {"status_predito", "status_alerta", "mensagem_voz"})

    def test_b_modelo_falso_eleva_com_solo_estavel(self) -> None:
        """(b) Solo estável e pacote falso com P = 0,9 (limiar 0,5) → status_alerta = 1."""
        self.inserir_historico(self.AGORA, [(20.0, 70.0, 70.0)] * 24)
        registro = montar_registro(self.arabica, self.AGORA, 20.0, 70.0, 70.0)
        novo, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico=pacote_falso(0.9))
        assert resultado is not None
        self.assertEqual(novo.status_alerta, 1)
        self.assertAlmostEqual(float(resultado.probabilidade_risco_futuro), 0.9)
        self.assertIsNone(resultado.horas_ate_secar)
        self.assertIn(resultado.motivo_principal, MOTIVOS)
        self.assertEqual(resultado.versao_prognostico, "FALSO-1")
        novo_baixo, resultado_baixo = aplicar_antecipacao(registro, self.arabica, self.banco,
                                                          pacote_prognostico=pacote_falso(0.2))
        self.assertEqual(novo_baixo, registro)
        assert resultado_baixo is not None
        self.assertEqual((resultado_baixo.status_antecipado, resultado_baixo.motivo_principal), (0, "NENHUM"))

    def test_c_historico_curto_nao_faz_nada(self) -> None:
        """(c) Só 10 h de histórico → registro idêntico e resultado None."""
        self.inserir_historico(self.AGORA, self.serie_secando()[-10:])
        registro = montar_registro(self.arabica, self.AGORA, 20.0, 70.0, 61.0)
        novo, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico=pacote_falso(0.9))
        self.assertIsNone(resultado)
        self.assertEqual(novo, registro)

    def test_d_vermelho_nunca_muda_e_nada_vira_vermelho(self) -> None:
        """(d) status_regra = 2 nunca muda; de < 2 nunca sai 2; só 3 campos podem mudar; cor nunca cai."""
        self.inserir_historico(self.AGORA, self.serie_secando())
        vermelho = montar_registro(self.arabica, self.AGORA, 20.0, 70.0, 40.0)
        self.assertEqual(vermelho.status_regra, 2)
        for pacote in ({}, pacote_falso(0.99, 0.01)):
            novo, _ = aplicar_antecipacao(vermelho, self.arabica, self.banco, pacote_prognostico=pacote)
            self.assertEqual(novo, vermelho)
        gerador = np.random.default_rng(7)
        for caso in range(40):
            temperatura = float(np.round(gerador.uniform(9.0, 31.0), 1))
            umidade_ar = float(np.round(gerador.uniform(25.0, 95.0), 1))
            solo = float(np.round(gerador.uniform(40.0, 92.0), 1))
            hora = int(gerador.integers(0, 24))
            agora = self.AGORA.replace(hour=hora) + timedelta(days=caso + 1)
            prognostico.limpar_cache_serie_horaria()
            self.inserir_historico(agora, [(temperatura + 0.3 * (k % 3), umidade_ar, solo + 0.6 * (24 - k))
                                           for k in range(24)])
            registro = montar_registro(self.arabica, agora, temperatura, umidade_ar, solo)
            for pacote in ({}, pacote_falso(float(gerador.uniform(0.0, 1.0)))):
                novo, _ = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico=pacote)
                self.assertTrue(set(campos_diferentes(registro, novo)) <= {"status_predito", "status_alerta", "mensagem_voz"})
                if registro.status_regra == 2:
                    self.assertEqual(novo.status_alerta, registro.status_alerta)
                else:
                    self.assertLessEqual(novo.status_alerta, max(registro.status_alerta, 1))
                self.assertGreaterEqual(novo.status_alerta, registro.status_alerta)
                self.assertGreaterEqual(novo.status_predito, registro.status_predito)

    def test_e_origens_que_nao_sao_de_campo(self) -> None:
        """(e) SIMULACAO e DEMONSTRACAO → (registro, None), mesmo com histórico de campo completo."""
        self.inserir_historico(self.AGORA, self.serie_secando())
        for origem in ("SIMULACAO", "DEMONSTRACAO"):
            registro = montar_registro(self.arabica, self.AGORA, 20.0, 70.0, 61.0, origem=origem)
            novo, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico={})
            self.assertIsNone(resultado)
            self.assertEqual(novo, registro)

    def test_so_historico_de_campo_conta(self) -> None:
        """Histórico de SIMULACAO não serve de base para antecipar uma leitura de campo."""
        self.inserir_historico(self.AGORA, self.serie_secando(), origem="SIMULACAO")
        registro = montar_registro(self.arabica, self.AGORA, 20.0, 70.0, 61.0)
        self.assertEqual(aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico={}), (registro, None))

    def test_i_memoria_de_estresse(self) -> None:
        """(i) 9 h seguidas de solo abaixo de ideal_min → a mensagem diz 'há 9 horas'."""
        # 16 h na faixa ideal e 8 h a 53 % (abaixo de 60 %); a leitura atual (55 %, depois de uma rega
        # fraca) é a 9ª hora seca. A rega na janela impede o ETA: o motivo é a seca de agora.
        self.inserir_historico(self.AGORA, [(20.0, 70.0, 66.0)] * 16 + [(20.0, 70.0, 53.0)] * 8)
        registro = montar_registro(self.arabica, self.AGORA, 20.0, 70.0, 55.0)
        novo, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico=pacote_falso(0.9))
        assert resultado is not None
        self.assertEqual(resultado.horas_em_estresse, 9.0)
        self.assertEqual(resultado.motivo_principal, "SECA_AGORA")
        self.assertIn("há 9 horas", novo.mensagem_voz)
        self.assertIn("Regue agora", novo.mensagem_voz)
        # Variante com secagem: o gatilho é o ETA e a memória continua na frase.
        prognostico.limpar_cache_serie_horaria()
        outro = self.AGORA + timedelta(days=3)
        self.inserir_historico(outro, [(20.0, 70.0, 66.0)] * 16 + [(20.0, 70.0, 59.0 - k) for k in range(8)])
        registro = montar_registro(self.arabica, outro, 20.0, 70.0, 51.0)
        novo, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico={})
        assert resultado is not None
        self.assertEqual(resultado.motivo_principal, "SECA_CHEGANDO")
        self.assertIn("há 9 horas", novo.mensagem_voz)
        self.assertLessEqual(contar_frases(novo.mensagem_voz), 4)

    def test_frase_condicional_de_seca(self) -> None:
        """ETA entre 12 e 24 h → uma frase condicional, sem mudar a cor."""
        self.inserir_historico(self.AGORA, [(20.0, 70.0, 75.0 - 0.7 * k) for k in range(24)])
        registro = montar_registro(self.arabica, self.AGORA, 20.0, 70.0, 58.5)
        novo, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico={})
        assert resultado is not None
        self.assertTrue(12.0 < float(resultado.horas_ate_secar) <= 24.0, resultado.horas_ate_secar)
        self.assertEqual(novo.status_alerta, registro.status_alerta)
        self.assertEqual(novo.mensagem_voz.count("Se não chover nem regar, a terra deve ficar muito seca"), 1)
        self.assertTrue(novo.mensagem_voz.startswith(registro.mensagem_voz))

    def test_nunca_lanca_excecao(self) -> None:
        """Parâmetros inválidos ou banco inexistente → (registro, None), sem exceção."""
        registro = montar_registro(self.arabica, self.AGORA, 20.0, 70.0, 61.0)
        self.assertEqual(aplicar_antecipacao(registro, None, self.banco), (registro, None))  # type: ignore[arg-type]
        self.assertEqual(aplicar_antecipacao(registro, self.arabica, self.pasta / "nao_existe.db",
                                             pacote_prognostico={}), (registro, None))

    def test_pacote_padrao_ausente_ou_corrompido(self) -> None:
        """pacote_prognostico=None carrega o arquivo padrão; ausente/corrompido → segue sem modelo."""
        self.inserir_historico(self.AGORA, self.serie_secando())
        registro = montar_registro(self.arabica, self.AGORA, 20.0, 70.0, 61.0)
        corrompido = self.pasta / "corrompido.pkl"
        corrompido.write_bytes(b"isto nao e um pickle")
        self.assertIsNone(prognostico.carregar_modelo_prognostico(corrompido))
        self.assertIsNone(prognostico.carregar_modelo_prognostico(self.pasta / "ausente.pkl"))
        with mock.patch.object(prognostico, "CAMINHO_MODELO_PROGNOSTICO", self.pasta / "ausente.pkl"):
            novo, resultado = aplicar_antecipacao(registro, self.arabica, self.banco)
        assert resultado is not None
        self.assertEqual(resultado.versao_prognostico, prognostico.VERSAO_SEM_PROGNOSTICO)
        self.assertEqual(novo.status_alerta, 1)

    def test_modulo_nao_importa_coletor_nem_app(self) -> None:
        """prognostico importa só database e gerador_dataset (nunca coletor_serial nem app)."""
        codigo = ("import sys; sys.path.insert(0, %r); import prognostico; "
                  "print('coletor_serial' in sys.modules, 'app' in sys.modules)" % str(PASTA_SRC))
        saida = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True, check=True).stdout
        self.assertEqual(saida.strip(), "False False")


class TestePersistenciaAnalise(BaseBanco):
    """gravar_analise_antecipacao / obter_analise_antecipacao e ON DELETE CASCADE."""

    def test_h_ida_e_volta_e_cascata(self) -> None:
        """(h) Gravar → obter devolve os mesmos valores; apagar a leitura apaga a análise."""
        agora = datetime(2026, 7, 10, 18, 0)
        self.assertIsNone(obter_analise_antecipacao(1, self.banco))  # tabela ainda não existe
        id_leitura = inserir_leitura(montar_registro(self.arabica, agora, 20.0, 70.0, 70.0), caminho_banco=self.banco)
        resultado = ResultadoAntecipacao(
            status_antecipado=1, probabilidade_risco_futuro=0.73, horas_ate_secar=None,
            temperatura_minima_prevista=4.25, motivo_principal="GEADA_NOITE", prazo_horas=11.0,
            horas_em_estresse=0.0, versao_prognostico="Prognostico12h-teste")
        gravar_analise_antecipacao(id_leitura, resultado, self.banco)
        gravar_analise_antecipacao(id_leitura, resultado, self.banco)  # idempotente (INSERT OR REPLACE)
        lida = obter_analise_antecipacao(id_leitura, self.banco)
        self.assertEqual(lida, {
            "probabilidade_risco_futuro": 0.73, "horas_ate_secar": None, "temperatura_minima_prevista": 4.25,
            "motivo_principal": "GEADA_NOITE", "prazo_horas": 11.0, "horas_em_estresse": 0.0,
            "status_antecipado": 1, "versao_prognostico": "Prognostico12h-teste",
        })
        self.assertIsNone(obter_analise_antecipacao(id_leitura + 100, self.banco))
        conexao = sqlite3.connect(str(self.banco))
        try:
            conexao.execute("PRAGMA foreign_keys = ON")
            conexao.execute("DELETE FROM historico_leituras WHERE id_leitura = ?", (id_leitura,))
            conexao.commit()
        finally:
            conexao.close()
        self.assertIsNone(obter_analise_antecipacao(id_leitura, self.banco))

    def test_recusa_valores_invalidos(self) -> None:
        """Motivo fora de MOTIVOS, status_antecipado 2 ou leitura inexistente são recusados."""
        id_leitura = inserir_leitura(montar_registro(self.arabica, datetime(2026, 7, 10, 18), 20.0, 70.0, 70.0),
                                     caminho_banco=self.banco)
        base = ResultadoAntecipacao(0, None, None, None, "NENHUM", None, None, "SEM_PROGNOSTICO")
        with self.assertRaises(ValueError):
            gravar_analise_antecipacao(id_leitura, dataclasses.replace(base, motivo_principal="CHUVA"), self.banco)
        with self.assertRaises(ValueError):
            gravar_analise_antecipacao(id_leitura, dataclasses.replace(base, status_antecipado=2), self.banco)
        with self.assertRaises(sqlite3.IntegrityError):
            gravar_analise_antecipacao(id_leitura + 50, base, self.banco)


# ---------------------------------------------------------------------------
# 4. Geada ainda à tarde
# ---------------------------------------------------------------------------
class TesteGeada(BaseBanco):
    """prever_temperatura_minima_noite, integração em aplicar_antecipacao e frase de geada acontecendo."""

    TARDE = datetime(2026, 7, 10, 18, 0, 0)

    def historico_tarde(self, agora: datetime, temperatura: float, umidade: float) -> None:
        """24 h de campo terminando 1 h antes de ``agora``; as últimas horas com o ar do fim da tarde."""
        valores = [(temperatura + 4.0, umidade + 10.0, 70.0)] * 22 + [(temperatura, umidade, 70.0)] * 2
        self.inserir_historico(agora, valores)

    def test_previsao_dispara_com_ar_seco_e_frio(self) -> None:
        """T_tarde = 12 °C e UR = 40 % às 18 h → mínima prevista abaixo de critico_min + 2 (Arábica)."""
        tarde = pd.DataFrame({"data_hora": pd.to_datetime(["2026-07-10 17:00:00", "2026-07-10 18:00:00"]),
                              "temperatura_ar": [12.0, 12.0], "umidade_ar": [40.0, 40.0]})
        previsao = prever_temperatura_minima_noite(tarde, self.TARDE)
        assert previsao is not None
        self.assertLess(previsao, self.arabica.faixa("temperatura_ar").critico_min + prognostico.MARGEM_GEADA_C)
        quente = tarde.assign(temperatura_ar=[20.0, 20.0], umidade_ar=[80.0, 80.0])
        self.assertGreaterEqual(prever_temperatura_minima_noite(quente, self.TARDE),
                                self.arabica.faixa("temperatura_ar").critico_min + prognostico.MARGEM_GEADA_C)
        self.assertIsNone(prever_temperatura_minima_noite(tarde, datetime(2026, 7, 10, 10, 0)))
        self.assertIsNotNone(prever_temperatura_minima_noite(tarde, datetime(2026, 7, 11, 3, 0)))
        self.assertIsNone(prever_temperatura_minima_noite(tarde.iloc[[0]], datetime(2026, 7, 10, 17, 20)))

    def test_aviso_de_geada_a_tarde_eleva_para_amarelo(self) -> None:
        """Às 18 h, ainda Verde (18,5 °C) mas ar seco (UR 30 %) → GEADA_NOITE e status_alerta = 1."""
        self.historico_tarde(self.TARDE, 18.5, 30.0)
        registro = montar_registro(self.arabica, self.TARDE, 18.5, 30.0, 70.0)
        self.assertEqual(registro.status_regra, 0)
        novo, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico={})
        assert resultado is not None
        self.assertEqual(resultado.motivo_principal, "GEADA_NOITE")
        self.assertEqual(novo.status_alerta, 1)
        self.assertIn("Esta noite pode gear. Antes de escurecer, cubra as mudas novas e chegue terra no pé das plantas.",
                      novo.mensagem_voz)
        self.assertTrue(novo.mensagem_voz.startswith("Atenção com a plantação de café arábica"))

    def test_cenario_12_graus_40_por_cento(self) -> None:
        """T_tarde = 12 °C e UR = 40 %: a geada é prevista, mas 12 °C já é Vermelho no Arábica e continua Vermelho."""
        self.historico_tarde(self.TARDE, 12.0, 40.0)
        registro = montar_registro(self.arabica, self.TARDE, 12.0, 40.0, 70.0)
        self.assertEqual(registro.status_regra, 2)
        novo, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico={})
        assert resultado is not None
        self.assertEqual(novo, registro)
        self.assertLess(float(resultado.temperatura_minima_prevista), 10.0)
        # Ar seco e frio, mas ainda no Amarelo (16 °C): o aviso de geada sai e a cor fica Amarela.
        prognostico.limpar_cache_serie_horaria()
        outro = self.TARDE + timedelta(days=1)
        self.historico_tarde(outro, 16.0, 40.0)
        registro = montar_registro(self.arabica, outro, 16.0, 40.0, 70.0)
        novo, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico={})
        assert resultado is not None
        self.assertEqual((resultado.motivo_principal, resultado.status_antecipado, novo.status_alerta),
                         ("GEADA_NOITE", 1, 1))

    def test_tarde_quente_e_umida_sem_aviso(self) -> None:
        """T = 20 °C e UR = 80 % às 18 h → sem aviso (cor e mensagem intactas)."""
        self.historico_tarde(self.TARDE, 20.0, 80.0)
        registro = montar_registro(self.arabica, self.TARDE, 20.0, 80.0, 70.0)
        novo, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico={})
        assert resultado is not None
        self.assertEqual(novo, registro)
        self.assertEqual(resultado.motivo_principal, "NENHUM")

    def test_nunca_pode_gear_de_dia(self) -> None:
        """O mesmo ar de geada às 10 h → sem previsão e sem 'pode gear' (das 6 h às 16 h)."""
        for hora in range(6, 17):
            prognostico.limpar_cache_serie_horaria()
            agora = datetime(2026, 7, 1, hora, 0) + timedelta(days=hora)
            self.historico_tarde(agora, 18.5, 30.0)
            registro = montar_registro(self.arabica, agora, 18.5, 30.0, 70.0)
            novo, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico={})
            assert resultado is not None
            self.assertIsNone(resultado.temperatura_minima_prevista)
            self.assertNotIn("pode gear", novo.mensagem_voz)

    def test_frio_leve_so_acrescenta_frase(self) -> None:
        """Mínima prevista entre critico_min + 2 e atencao_min → frase de frio leve, cor intacta."""
        self.historico_tarde(self.TARDE, 19.0, 60.0)
        registro = montar_registro(self.arabica, self.TARDE, 19.0, 60.0, 70.0)
        novo, resultado = aplicar_antecipacao(registro, self.arabica, self.banco, pacote_prognostico={})
        assert resultado is not None
        self.assertEqual(resultado.motivo_principal, "FRIO")
        self.assertEqual(novo.status_alerta, registro.status_alerta)
        self.assertIn("Esta noite vai fazer frio. Proteja as mudas novas.", novo.mensagem_voz)

    def test_frase_de_geada_acontecendo(self) -> None:
        """gerar_mensagem_orientacao com 5 °C: 'Está gelando agora', 'Cubra as mudas', 'chegue terra', sem 'pode gear'."""
        deficit, desvio = calcular_features(5.0, 70.0, self.arabica)
        mensagem = gerar_mensagem_orientacao(2, deficit, desvio, self.arabica, 5.0, 70.0)
        for trecho in ("Está gelando agora", "Cubra as mudas", "chegue terra"):
            self.assertIn(trecho, mensagem)
        self.assertNotIn("pode gear", mensagem)

    def test_palavras_chave_de_acao_preservadas(self) -> None:
        """As palavras-chave de ação da interface-campo continuam nas mesmas condições."""
        casos = {
            (20.0, 30.0): ["Regue agora"], (20.0, 55.0): ["fim da tarde"], (16.0, 55.0): ["de manhã"],
            (20.0, 85.0): ["Não regue"], (20.0, 93.0): ["Pare de regar", "valetas"], (29.0, 70.0): ["sombra"],
            (5.0, 70.0): ["Cubra as mudas", "chegue terra"], (12.0, 70.0): ["Cubra as mudas", "chegue terra"],
            (16.0, 70.0): ["Proteja as mudas"], (20.0, 70.0): ["Não precisa fazer nada"],
        }
        for (temperatura, solo), palavras in casos.items():
            deficit, desvio = calcular_features(temperatura, solo, self.arabica)
            status = calcular_status_regra(temperatura, solo, self.arabica)
            mensagem = gerar_mensagem_orientacao(status, deficit, desvio, self.arabica, temperatura, solo)
            for palavra in palavras:
                self.assertIn(palavra, mensagem, (temperatura, solo))


# ---------------------------------------------------------------------------
# 5. Avaliação honesta
# ---------------------------------------------------------------------------
class TesteAvaliacaoHonesta(BaseBanco):
    """Divisão temporal, validação temporal, robustez a ruído, antecipação e orçamento de alarmes."""

    def dataset_rotulado(self, horas: int) -> Tuple[pd.DataFrame, Dict[str, ParametrosCultura]]:
        """Dataset limpo pequeno (duas culturas) com rótulo da regra."""
        conilon = buscar_parametros_cultura("cafe_conilon", caminho_banco=self.banco)
        parametros = {"cafe_arabica": self.arabica, "cafe_conilon": conilon}
        series = [simular_serie_temporal(self.arabica, horas, semente=1),
                  simular_serie_temporal(conilon, horas, semente=2)]
        return calcular_features_dataframe(pd.concat(series, ignore_index=True), parametros), parametros

    def test_a_divisao_temporal_sem_vazamento(self) -> None:
        """(a) 1.000 h: sem sobreposição e max(t_treino) + 12 h ≤ min(t_teste) por cultura e por dobra."""
        df, parametros = self.dataset_rotulado(1000)
        treino, teste = treinamento_ia.dividir_temporal(df, 0.3, 12)
        self.assertEqual(len(set(treino) & set(teste)), 0)
        momentos = pd.to_datetime(df["data_hora"])
        for cultura in parametros:
            filtro = df["cultura"] == cultura
            fim_treino = momentos[treino][filtro[treino]].max()
            inicio_teste = momentos[teste][filtro[teste]].min()
            self.assertLessEqual(fim_treino + pd.Timedelta(hours=12), inicio_teste)
            self.assertAlmostEqual(int(filtro[teste].sum()) / 1000, 0.3, delta=0.01)
        df_prog = treinamento_ia.preparar_dataset_prognostico(df, parametros)
        self.assertTrue((df_prog["status_atual"] < 2).all())
        self.assertFalse(df_prog[prognostico.COLUNAS_FEATURES_PROGNOSTICO].isna().any().any())
        validacao = treinamento_ia.validacao_temporal(df_prog, dobras=4)
        self.assertEqual(len(validacao["divisoes"]), 4)
        dados = df_prog.sort_values(["data_hora", "cultura"], kind="mergesort").reset_index(drop=True)
        for posicoes_treino, posicoes_validacao in validacao["divisoes"]:
            self.assertEqual(len(set(posicoes_treino) & set(posicoes_validacao)), 0)
            for cultura in parametros:
                treino_cultura = dados.iloc[posicoes_treino].query("cultura == @cultura")["data_hora"]
                validacao_cultura = dados.iloc[posicoes_validacao].query("cultura == @cultura")["data_hora"]
                self.assertLessEqual(treino_cultura.max() + pd.Timedelta(hours=12), validacao_cultura.min())
        for abordagem in ("prognostico", "persistencia", "regra_pura"):
            self.assertIn("f1_macro", validacao["media"][abordagem])

    def test_b_robustez_sem_ruido_nao_erra(self) -> None:
        """(b) σ = (0, 0) → 0 % de cor errada; mais ruído → mais erro; a mediana de 12 amostras ajuda."""
        df, parametros = self.dataset_rotulado(600)
        linhas = treinamento_ia.avaliar_robustez_ruido(df, parametros)
        self.assertEqual(len(linhas), 4)
        self.assertEqual(linhas[0]["cor_errada_pct_sem_filtro"], 0.0)
        self.assertEqual(linhas[0]["cor_errada_pct_mediana12"], 0.0)
        self.assertGreater(linhas[-1]["cor_errada_pct_sem_filtro"], linhas[1]["cor_errada_pct_sem_filtro"])
        self.assertLess(linhas[-1]["cor_errada_pct_mediana12"], linhas[-1]["cor_errada_pct_sem_filtro"])

    def test_d_aviso_previo_de_quatro_horas(self) -> None:
        """(d) Vermelho às 10 h e alerta das 6 h às 10 h → aviso prévio = 4 h."""
        status = pd.Series([0] * 10 + [2, 2] + [0] * 12)
        alertas = pd.Series([False] * 6 + [True] * 5 + [False] * 13)
        resultado = treinamento_ia.avaliar_antecipacao(alertas, status)
        self.assertEqual(resultado["inicios_vermelho"], 1.0)
        self.assertEqual(resultado["mediana_horas_aviso"], 4.0)
        self.assertEqual(resultado["pct_inicios_com_3h"], 100.0)
        self.assertEqual(resultado["horas_alarme_falso_semana"], 0.0)
        tardio = treinamento_ia.avaliar_antecipacao(pd.Series([False] * 9 + [True] * 2 + [False] * 13), status)
        self.assertEqual(tardio["mediana_horas_aviso"], 1.0)

    def test_limiar_respeita_orcamento(self) -> None:
        """escolher_limiar_por_orcamento: falsos/semana dentro do orçamento e menor limiar possível."""
        gerador = np.random.default_rng(3)
        alvo = (gerador.random(5000) < 0.3).astype(int)
        probabilidade = np.clip(0.35 * alvo + gerador.normal(0.3, 0.15, 5000), 0.0, 1.0)
        limiar = treinamento_ia.escolher_limiar_por_orcamento(probabilidade, alvo, 14.0)
        falsos = np.sum((probabilidade >= limiar) & (alvo == 0)) / (5000 / 168.0)
        self.assertLessEqual(falsos, 14.0)
        candidatos = np.unique(np.round(probabilidade, 4))
        mais_baixo = float(np.max(candidatos[candidatos < limiar]))
        self.assertGreater(np.sum((probabilidade >= mais_baixo) & (alvo == 0)) / (5000 / 168.0), 14.0)


class TesteMetasDoPrognostico(unittest.TestCase):
    """Treino + avaliação completos no simulador de 8.760 h (banco temporário): metas de aceite."""

    @classmethod
    def setUpClass(cls) -> None:
        """Gera o dataset de 2 × 8.760 h e roda a avaliação honesta completa uma vez."""
        cls._diretorio = tempfile.mkdtemp(prefix="agroanalise_metas_")
        pasta = Path(cls._diretorio)
        cls.banco = pasta / "teste.db"
        inicializar_banco(cls.banco)
        with contextlib.redirect_stdout(io.StringIO()):
            _, limpo = gerador_dataset.gerar_dataset_completo(
                caminho_bruto=pasta / "bruto.csv", caminho_limpo=pasta / "limpo.csv", caminho_banco=cls.banco,
                data_inicial=gerador_dataset.DATA_INICIAL_DATASET)
        cls.parametros = {codigo: buscar_parametros_cultura(codigo, caminho_banco=cls.banco)
                          for codigo in ("cafe_arabica", "cafe_conilon")}
        cls.pacote, cls.avaliacao = treinamento_ia.avaliar_prognostico_completo(limpo, cls.parametros)
        cls.caminho_pacote = treinamento_ia.salvar_modelo_prognostico(cls.pacote, pasta / "modelo_prognostico.pkl")

    @classmethod
    def tearDownClass(cls) -> None:
        """Remove o diretório temporário."""
        shutil.rmtree(cls._diretorio, ignore_errors=True)

    def test_pacote_com_nove_chaves(self) -> None:
        """O pacote salvo tem exatamente as 9 chaves e recarrega pelo módulo de prognóstico."""
        self.assertEqual(set(self.pacote), set(prognostico.CHAVES_PACOTE_PROGNOSTICO))
        self.assertEqual(len(self.pacote), 9)
        carregado = prognostico.carregar_modelo_prognostico(self.caminho_pacote)
        assert carregado is not None
        self.assertEqual(carregado["features"], prognostico.COLUNAS_FEATURES_PROGNOSTICO)
        self.assertEqual(carregado["horizonte_horas"], 12)
        self.assertEqual(carregado["culturas"], ["cafe_arabica", "cafe_conilon"])
        self.assertTrue(0.0 < carregado["limiar"] < 1.0)
        with self.assertRaises(ValueError):
            treinamento_ia.salvar_modelo_prognostico({"modelo": None}, Path(self._diretorio) / "x.pkl")

    def test_metas_do_teste_temporal(self) -> None:
        """AUC ≥ 0,80; Brier ≤ 0,12 e < persistência; ≥ 50 % dos inícios com ≥ 3 h; F1 ≥ persistência + 0,10."""
        holdout = self.avaliacao["holdout"]
        self.assertGreaterEqual(holdout["prognostico"]["auc"], 0.80)
        self.assertLessEqual(holdout["prognostico"]["brier"], 0.12)
        self.assertLess(holdout["prognostico"]["brier"], holdout["persistencia"]["brier"])
        self.assertGreaterEqual(self.avaliacao["antecipacao"]["modelo"]["pct_inicios_com_3h"], 50.0)
        self.assertGreaterEqual(holdout["prognostico"]["f1_macro"], holdout["persistencia"]["f1_macro"] + 0.10)
        self.assertLessEqual(self.avaliacao["antecipacao"]["modelo"]["horas_alarme_falso_semana"],
                             2.0 * treinamento_ia.ORCAMENTO_AMARELO_ANTECIPADO_HORAS_SEMANA)

    def test_c_calibracao_nao_piora(self) -> None:
        """(c) Brier do calibrado ≤ Brier do não calibrado + 0,005 no teste temporal; curva em 5 faixas."""
        calibracao = self.avaliacao["calibracao"]
        self.assertLessEqual(calibracao["brier_calibrado"], calibracao["brier_nao_calibrado"] + 0.005)
        self.assertEqual(len(calibracao["curva_calibrado"]), 5)
        grafico = treinamento_ia.gerar_grafico_confiabilidade(
            {"calibrado": calibracao["curva_calibrado"], "nao_calibrado": calibracao["curva_nao_calibrado"]},
            Path(self._diretorio) / "curva.png")
        self.assertGreater(grafico.stat().st_size, 1000)

    def test_metas_de_geada(self) -> None:
        """Geada no teste temporal: ≥ 80 % avisadas até as 19 h, ≤ 25 % de avisos sem frio, erro médio ≤ 2 °C."""
        geada = self.avaliacao["geada"]
        self.assertGreater(geada["noites_geada"], 0)
        self.assertGreaterEqual(geada["pct_geadas_avisadas"], 80.0)
        self.assertLessEqual(geada["pct_avisos_sem_frio"], 25.0)
        self.assertLessEqual(geada["erro_absoluto_medio"], 2.0)

    def test_relatorio_tem_as_secoes_com_escopo(self) -> None:
        """O texto das seções 2 a 6 traz os títulos e o rótulo de escopo."""
        texto = treinamento_ia.montar_relatorio_prognostico(self.avaliacao, Path("curva.png"))
        for titulo in treinamento_ia.TITULOS_SECOES_RELATORIO[1:]:
            self.assertIn(titulo, texto)
        self.assertIn("Aviso de geada", texto)
        self.assertGreaterEqual(texto.count("[escopo:"), 5)

    def test_modelo_real_antecipa_secagem_no_banco(self) -> None:
        """O pacote treinado funciona em aplicar_antecipacao (sem exceção e sem criar Vermelho)."""
        pasta = Path(self._diretorio)
        banco = pasta / "tempo_real.db"
        inicializar_banco(banco)
        arabica = self.parametros["cafe_arabica"]
        agora = datetime(2026, 3, 10, 14, 0)
        for hora in range(24):
            instante = agora - timedelta(hours=24 - hora)
            inserir_leitura(montar_registro(arabica, instante, 20.0, 70.0, 62.0 + (23 - hora)), caminho_banco=banco)
        registro = montar_registro(arabica, agora, 20.0, 70.0, 61.0)
        novo, resultado = aplicar_antecipacao(registro, arabica, banco, pacote_prognostico=self.pacote)
        assert resultado is not None
        self.assertEqual(novo.status_alerta, 1)
        self.assertTrue(0.0 <= float(resultado.probabilidade_risco_futuro) <= 1.0)
        self.assertEqual(resultado.versao_prognostico, self.pacote["versao"])


class TesteArtefatosReais(unittest.TestCase):
    """Artefatos gerados por ``python src/treinamento_ia.py`` (pulados se ainda não houver treino)."""

    @unittest.skipUnless(treinamento_ia.CAMINHO_RELATORIO_TREINO.is_file()
                         and prognostico.CAMINHO_MODELO_PROGNOSTICO.is_file(), "treino real ainda não executado")
    def test_relatorio_e_png_reais(self) -> None:
        """Relatório com os 5 títulos de seção, seção de geada, PNG de confiabilidade e pacote com 9 chaves."""
        texto = treinamento_ia.CAMINHO_RELATORIO_TREINO.read_text(encoding="utf-8")
        for titulo in treinamento_ia.TITULOS_SECOES_RELATORIO:
            self.assertIn(titulo, texto)
        self.assertIn("Aviso de geada", texto)
        self.assertTrue(treinamento_ia.CAMINHO_CURVA_CONFIABILIDADE.is_file())
        pacote = prognostico.carregar_modelo_prognostico(prognostico.CAMINHO_MODELO_PROGNOSTICO)
        assert pacote is not None
        self.assertEqual(set(pacote), set(prognostico.CHAVES_PACOTE_PROGNOSTICO))


if __name__ == "__main__":
    unittest.main()
