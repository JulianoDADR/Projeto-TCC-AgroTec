"""Testes automatizados do AgroAnálise (unittest da biblioteca padrão).

Cobrem, sempre com banco SQLite TEMPORÁRIO (``tempfile``) passado via
``caminho_banco`` ou ``app.config["CAMINHO_BANCO"]``:

    * regras agronômicas: classificação, features derivadas e mensagem falada;
    * cenários do Modo Demonstração vs. status esperado (inclusive cultura nova);
    * saneamento do dataset (NaN, pico, negativo, salto) e saneamento em tempo real;
    * banco de dados: inicialização idempotente, inserção/busca, configurações;
    * pipeline de dados + treinamento + pacote ``.pkl`` em miniatura;
    * coletor: protocolo serial, fallback do Modo Simulação, Modo Demonstração,
      caminho serial real com emulador pty (Linux) e queda para Simulação;
    * rotas Flask com ``app.test_client()``;
    * lacunas da auditoria final: modelo só nas culturas do treino, histerese
      do semáforo, leitura antiga, aviso de dados de teste, troca de cultura
      sem falha falsa e gravação atômica do modo pelo coletor.

Execução (a partir da raiz do projeto): ``python -m unittest discover -s tests -v``.
"""

from __future__ import annotations

import contextlib
import io
import logging
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

RAIZ_PROJETO: Path = Path(__file__).resolve().parent.parent
PASTA_SRC: Path = RAIZ_PROJETO / "src"
if str(PASTA_SRC) not in sys.path:
    sys.path.insert(0, str(PASTA_SRC))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import joblib  # noqa: E402
import serial  # noqa: E402
from sklearn.dummy import DummyClassifier  # noqa: E402

import coletor_serial  # noqa: E402
import database  # noqa: E402
import gerador_dataset  # noqa: E402
import treinamento_ia  # noqa: E402
import app as modulo_app  # noqa: E402
from app import app  # noqa: E402
from coletor_serial import (  # noqa: E402
    ColetorAgroAnalise,
    SaneadorTempoReal,
    carregar_modelo,
    interpretar_linha_serial,
    modelo_cobre_cultura,
    ler_linha_mais_recente,
    predizer_status,
    processar_leitura,
)
from database import (  # noqa: E402
    FaixaParametro,
    ParametrosCultura,
    RegistroLeitura,
    buscar_historico,
    buscar_parametros_cultura,
    buscar_ultima_leitura,
    cadastrar_cultura,
    definir_configuracao,
    definir_configuracao_condicional,
    encerrar_demonstracao_se_inalterada,
    inicializar_banco,
    inserir_leitura,
    listar_culturas,
    obter_configuracao,
    obter_configuracao_tipada,
)
from gerador_dataset import (  # noqa: E402
    CENARIOS_DEMONSTRACAO,
    COLUNAS_FEATURES_MODELO,
    COLUNAS_SENSORES,
    LIMITES_FISICOS_SENSORES,
    calcular_desvio_faixa,
    calcular_features,
    calcular_features_dataframe,
    calcular_status_regra,
    classificar_variavel,
    detectar_anomalias,
    gerar_leitura_cenario,
    gerar_mensagem_orientacao,
    injetar_ruidos,
    sanear_dataset,
    simular_serie_temporal,
)

FORMATO: str = "%Y-%m-%d %H:%M:%S"


def setUpModule() -> None:
    """Silencia os logs informativos/avisos durante os testes (erros continuam visíveis)."""
    logging.disable(logging.WARNING)


def tearDownModule() -> None:
    """Reativa os logs."""
    logging.disable(logging.NOTSET)


class BaseComBancoTemporario(unittest.TestCase):
    """Cria um diretório e um banco SQLite temporários e isolados para cada teste."""

    def setUp(self) -> None:
        """Cria e inicializa o banco temporário."""
        self._diretorio = tempfile.mkdtemp(prefix="agroanalise_teste_")
        self.pasta = Path(self._diretorio)
        self.banco: Path = self.pasta / "teste.db"
        inicializar_banco(self.banco)
        self.arabica: ParametrosCultura = buscar_parametros_cultura("cafe_arabica", caminho_banco=self.banco)
        self.conilon: ParametrosCultura = buscar_parametros_cultura("cafe_conilon", caminho_banco=self.banco)

    def tearDown(self) -> None:
        """Remove o diretório temporário."""
        shutil.rmtree(self._diretorio, ignore_errors=True)

    def leituras(self) -> List[Dict[str, Any]]:
        """Todas as leituras do banco temporário, da mais antiga para a mais recente."""
        return list(reversed(buscar_historico(limite=500, caminho_banco=self.banco)))

    def cadastrar_tomate(self) -> int:
        """Cadastra uma cultura fictícia (sem mudar código) para testar a escalabilidade."""
        return cadastrar_cultura(
            "tomate", "Tomate", "Solanum lycopersicum", "hortalica", "Cultura de teste.",
            [
                FaixaParametro("temperatura_ar", "°C", 20.0, 26.0, 16.0, 30.0, 10.0, 35.0),
                FaixaParametro("umidade_ar", "%", 60.0, 80.0, 50.0, 90.0, 40.0, 95.0),
                FaixaParametro("umidade_solo", "%", 60.0, 80.0, 50.0, 88.0, 35.0, 95.0),
                FaixaParametro("ph_solo", "pH", 6.0, 6.8, 5.5, 7.2, 5.0, 7.8),
            ],
            caminho_banco=self.banco,
        )


# ---------------------------------------------------------------------------
# Regras agronômicas
# ---------------------------------------------------------------------------
class TesteRegrasAgronomicas(BaseComBancoTemporario):
    """Classificação por faixa, features derivadas e mensagem falada."""

    def test_classificar_variavel_bordas(self) -> None:
        """Bordas inclusivas do ideal e da atenção (Café Arábica, temperatura 18–22 / 15–25)."""
        faixa = self.arabica.faixa("temperatura_ar")
        casos = {18.0: 0, 20.0: 0, 22.0: 0, 17.9: 1, 15.0: 1, 22.1: 1, 25.0: 1, 14.9: 2, 25.1: 2, 0.0: 2, 50.0: 2}
        for valor, esperado in casos.items():
            with self.subTest(valor=valor):
                self.assertEqual(classificar_variavel(valor, faixa), esperado)
        with self.assertRaises(ValueError):
            classificar_variavel(float("nan"), faixa)

    def test_calcular_desvio_faixa(self) -> None:
        """0 dentro, positivo acima, negativo abaixo."""
        self.assertEqual(calcular_desvio_faixa(20.0, 18.0, 22.0), 0.0)
        self.assertAlmostEqual(calcular_desvio_faixa(25.0, 18.0, 22.0), 3.0)
        self.assertAlmostEqual(calcular_desvio_faixa(15.0, 18.0, 22.0), -3.0)

    def test_calcular_features_sinais(self) -> None:
        """Déficit positivo = falta de água; negativo = excesso; desvio térmico com sinal do calor/frio."""
        self.assertEqual(calcular_features(25.0, 50.0, self.arabica), (10.0, 3.0))
        self.assertEqual(calcular_features(15.0, 90.0, self.arabica), (-10.0, -3.0))
        self.assertEqual(calcular_features(20.0, 70.0, self.arabica), (0.0, 0.0))
        # Conilon tem faixas próprias (temperatura 22–26, solo 55–75).
        self.assertEqual(calcular_features(20.0, 50.0, self.conilon), (5.0, -2.0))

    def test_calcular_status_regra(self) -> None:
        """Pior classe entre temperatura e umidade do solo."""
        self.assertEqual(calcular_status_regra(20.0, 70.0, self.arabica), 0)
        self.assertEqual(calcular_status_regra(23.0, 70.0, self.arabica), 1)
        self.assertEqual(calcular_status_regra(20.0, 55.0, self.arabica), 1)
        self.assertEqual(calcular_status_regra(20.0, 40.0, self.arabica), 2)
        self.assertEqual(calcular_status_regra(33.0, 70.0, self.arabica), 2)
        # A mesma temperatura é ideal para o Conilon e atenção para o Arábica.
        self.assertEqual(calcular_status_regra(24.0, 65.0, self.conilon), 0)
        self.assertEqual(calcular_status_regra(24.0, 65.0, self.arabica), 1)

    def test_mensagem_orientacao(self) -> None:
        """Mensagem falável: sem símbolos, com estado, causa e ação; 'Urgente!' além do crítico."""
        deficit, desvio = calcular_features(20.0, 30.0, self.arabica)
        urgente = gerar_mensagem_orientacao(2, deficit, desvio, self.arabica, 20.0, 30.0)
        self.assertTrue(urgente.startswith("Urgente!"))
        self.assertIn("seca", urgente)
        self.assertIn("Regue", urgente)
        deficit, desvio = calcular_features(20.0, 70.0, self.arabica)
        tudo_bem = gerar_mensagem_orientacao(0, deficit, desvio, self.arabica, 20.0, 70.0)
        self.assertTrue(tudo_bem.startswith("Tudo bem"))
        deficit, desvio = calcular_features(23.5, 70.0, self.arabica)
        atencao = gerar_mensagem_orientacao(1, deficit, desvio, self.arabica, 23.5, 70.0)
        self.assertTrue(atencao.startswith("Atenção"))
        self.assertIn("graus", atencao)
        for texto in (urgente, tudo_bem, atencao):
            self.assertNotIn("°", texto)
            self.assertNotIn("%", texto)


# ---------------------------------------------------------------------------
# Cenários do Modo Demonstração
# ---------------------------------------------------------------------------
class TesteCenarios(BaseComBancoTemporario):
    """Cada cenário produz o status esperado, dentro dos limites físicos, para qualquer cultura."""

    CHAVES_ESPERADAS = {"SECA_EXTREMA": 2, "ONDA_DE_CALOR": 2, "GEADA": 2, "ENCHARCAMENTO": 2,
                        "ATENCAO_MODERADA": 1, "CONDICAO_IDEAL": 0}

    def test_chaves_e_status_esperados(self) -> None:
        """As chaves e os status esperados são exatamente os do contrato."""
        self.assertEqual({chave: int(dados["status_esperado"]) for chave, dados in CENARIOS_DEMONSTRACAO.items()},
                         self.CHAVES_ESPERADAS)
        for dados in CENARIOS_DEMONSTRACAO.values():
            self.assertTrue(dados["rotulo"].startswith("Simular"))

    def test_cenarios_todas_as_culturas(self) -> None:
        """Arábica, Conilon e uma cultura cadastrada em tempo de execução (tomate)."""
        self.cadastrar_tomate()
        for codigo in ("cafe_arabica", "cafe_conilon", "tomate"):
            parametros = buscar_parametros_cultura(codigo, caminho_banco=self.banco)
            for cenario, esperado in self.CHAVES_ESPERADAS.items():
                with self.subTest(cultura=codigo, cenario=cenario):
                    leitura = gerar_leitura_cenario(cenario, parametros)
                    self.assertEqual(set(leitura), set(COLUNAS_SENSORES))
                    for coluna, valor in leitura.items():
                        minimo, maximo = LIMITES_FISICOS_SENSORES[coluna]
                        self.assertTrue(minimo <= valor <= maximo, f"{coluna}={valor}")
                    status = calcular_status_regra(leitura["temperatura_ar"], leitura["umidade_solo"], parametros)
                    self.assertEqual(status, esperado)

    def test_cenario_desconhecido(self) -> None:
        """Cenário inexistente gera ValueError."""
        with self.assertRaises(ValueError):
            gerar_leitura_cenario("TERREMOTO", self.arabica)

    def test_processar_leitura_cenario_com_saneador_novo(self) -> None:
        """Pipeline completo de uma leitura de cenário (como o Flask faz): grava o status esperado."""
        for cenario, esperado in self.CHAVES_ESPERADAS.items():
            with self.subTest(cenario=cenario):
                leitura = gerar_leitura_cenario(cenario, self.conilon)
                id_leitura, registro = processar_leitura(
                    leitura["temperatura_ar"], leitura["umidade_ar"], leitura["umidade_solo"], "DEMONSTRACAO",
                    "cafe_conilon", SaneadorTempoReal(self.conilon), None, caminho_banco=self.banco)
                self.assertGreater(id_leitura, 0)
                self.assertEqual(registro.flag_anomalia, 0)
                self.assertEqual(registro.status_regra, esperado)
                self.assertEqual(registro.status_alerta, esperado)
                self.assertEqual(registro.versao_modelo, "REGRAS")


# ---------------------------------------------------------------------------
# Saneamento (dataset e tempo real)
# ---------------------------------------------------------------------------
class TesteSaneamentoDataset(BaseComBancoTemporario):
    """Detector de anomalias, interpolação e features vetorizadas."""

    def _df_com_ruidos(self) -> pd.DataFrame:
        """Série curta com NaN, pico físico, negativo e salto injetados em linhas conhecidas."""
        temperaturas = [20.0, 21.0, float("nan"), 23.0, 60.0, 25.0, -3.0, 27.0, 28.0]
        solo = [70.0, 70.0, 70.0, 70.0, 70.0, 95.0, 70.0, 70.0, 70.0]
        return pd.DataFrame({
            "data_hora": [f"2026-01-01 0{h}:00:00" for h in range(9)],
            "cultura": ["cafe_arabica"] * 9,
            "temperatura_ar": temperaturas,
            "umidade_ar": [70.0] * 9,
            "umidade_solo": solo,
        })

    def test_detectar_anomalias(self) -> None:
        """NaN, pico físico, negativo e salto vs. mediana móvel são marcados e trocados por NaN."""
        resultado = detectar_anomalias(self._df_com_ruidos())
        self.assertEqual(resultado["flag_anomalia"].tolist(), [0, 0, 1, 0, 1, 1, 1, 0, 0])
        self.assertEqual(resultado.loc[2, "descricao_anomalia"], "temperatura_ar:nan")
        self.assertEqual(resultado.loc[4, "descricao_anomalia"], "temperatura_ar:pico")
        self.assertEqual(resultado.loc[5, "descricao_anomalia"], "umidade_solo:pico")
        self.assertEqual(resultado.loc[6, "descricao_anomalia"], "temperatura_ar:negativo")
        self.assertTrue(math.isnan(resultado.loc[4, "temperatura_ar"]))
        self.assertTrue(math.isnan(resultado.loc[5, "umidade_solo"]))

    def test_sanear_dataset_interpola(self) -> None:
        """Valores anômalos são interpolados linearmente; nenhum NaN sobra."""
        limpo = sanear_dataset(self._df_com_ruidos())
        self.assertFalse(limpo[COLUNAS_SENSORES].isna().any().any())
        self.assertAlmostEqual(limpo.loc[2, "temperatura_ar"], 22.0)
        self.assertAlmostEqual(limpo.loc[4, "temperatura_ar"], 24.0)
        self.assertAlmostEqual(limpo.loc[6, "temperatura_ar"], 26.0)
        self.assertAlmostEqual(limpo.loc[5, "umidade_solo"], 70.0)
        self.assertEqual(int(limpo["flag_anomalia"].sum()), 4)

    def test_injetar_ruidos_e_detector(self) -> None:
        """~3% das células por sensor corrompidas e todas detectadas na série simulada."""
        serie = simular_serie_temporal(self.arabica, horas=600, semente=7)
        self.assertEqual(list(serie.columns), ["data_hora", "cultura", "temperatura_ar", "umidade_ar", "umidade_solo"])
        bruto = injetar_ruidos(serie, semente=7)
        for coluna in COLUNAS_SENSORES:
            celulas = bruto["tipo_ruido_injetado"].str.count(f"{coluna}:").sum()
            self.assertEqual(int(celulas), 18)
        limpo = sanear_dataset(bruto)
        metricas = gerador_dataset.avaliar_detector(bruto, limpo)
        self.assertGreaterEqual(metricas["recall"], 0.99)
        self.assertFalse(limpo[COLUNAS_SENSORES].isna().any().any())

    def test_features_vetorizadas_iguais_as_escalares(self) -> None:
        """calcular_features_dataframe reproduz calcular_features + calcular_status_regra."""
        serie = simular_serie_temporal(self.conilon, horas=300, semente=3)
        com_features = calcular_features_dataframe(serie, {"cafe_conilon": self.conilon})
        for _, linha in com_features.iterrows():
            deficit, desvio = calcular_features(linha["temperatura_ar"], linha["umidade_solo"], self.conilon)
            self.assertAlmostEqual(linha["deficit_hidrico_solo"], deficit)
            self.assertAlmostEqual(linha["desvio_termico"], desvio)
            self.assertEqual(int(linha["status_alerta"]),
                             calcular_status_regra(linha["temperatura_ar"], linha["umidade_solo"], self.conilon))


class TesteSaneadorTempoReal(BaseComBancoTemporario):
    """Equivalente on-line do detector: hold-last e aceitação de novo patamar."""

    def test_regras_do_saneador(self) -> None:
        """NaN, negativo, pico físico e salto são marcados; o último válido é mantido."""
        saneador = SaneadorTempoReal(self.arabica)
        valores, flag, descricao = saneador.sanear(20.0, 70.0, 70.0)
        self.assertEqual((flag, descricao), (0, ""))
        valores, flag, descricao = saneador.sanear(None, 71.0, 69.0)
        self.assertEqual((valores["temperatura_ar"], flag, descricao), (20.0, 1, "temperatura_ar:nan"))
        valores, flag, descricao = saneador.sanear(20.5, 70.0, -5.0)
        self.assertEqual((valores["umidade_solo"], descricao), (69.0, "umidade_solo:negativo"))
        valores, flag, descricao = saneador.sanear(20.5, 150.0, 69.0)
        self.assertEqual((valores["umidade_ar"], descricao), (70.0, "umidade_ar:pico"))
        valores, flag, descricao = saneador.sanear(35.0, 70.0, 69.0)
        self.assertEqual((valores["temperatura_ar"], descricao), (20.5, "temperatura_ar:pico"))
        valores, flag, descricao = saneador.sanear(float("nan"), -1.0, 500.0)
        self.assertEqual(descricao, "temperatura_ar:nan;umidade_ar:negativo;umidade_solo:pico")

    def test_novo_patamar_apos_tres_leituras(self) -> None:
        """Mudança real e persistente (irrigação) é aceita na 3ª leitura coerente."""
        saneador = SaneadorTempoReal(self.arabica)
        saneador.sanear(20.0, 70.0, 40.0)
        self.assertEqual(saneador.sanear(20.0, 70.0, 75.0)[2], "umidade_solo:pico")
        self.assertEqual(saneador.sanear(20.0, 70.0, 76.0)[2], "umidade_solo:pico")
        valores, flag, _ = saneador.sanear(20.0, 70.0, 77.0)
        self.assertEqual((valores["umidade_solo"], flag), (77.0, 0))


# ---------------------------------------------------------------------------
# Banco de dados
# ---------------------------------------------------------------------------
class TesteBancoDados(BaseComBancoTemporario):
    """Esquema, sementes, inserção/busca e configurações."""

    def _registro(self, **alteracoes: Any) -> RegistroLeitura:
        """Registro válido de exemplo com campos alteráveis."""
        dados: Dict[str, Any] = dict(
            data_hora="2026-01-01 10:00:00", codigo_cultura="cafe_arabica", origem="SIMULACAO",
            temperatura_ar_bruta=20.0, umidade_ar_bruta=float("nan"), umidade_solo_bruta=70.0,
            temperatura_ar=20.0, umidade_ar=70.0, umidade_solo=70.0, deficit_hidrico_solo=0.0, desvio_termico=0.0,
            status_regra=0, status_predito=0, status_alerta=0, probabilidade_predicao=0.98, flag_anomalia=1,
            descricao_anomalia="umidade_ar:nan", mensagem_voz="Tudo bem.", versao_modelo="REGRAS",
        )
        dados.update(alteracoes)
        return RegistroLeitura(**dados)

    def test_inicializacao_idempotente_e_sementes(self) -> None:
        """Rodar a inicialização várias vezes não duplica; valores semeados exatos."""
        definir_configuracao("cultura_ativa", "cafe_conilon", caminho_banco=self.banco)
        inicializar_banco(self.banco)
        inicializar_banco(self.banco)
        conexao = sqlite3.connect(self.banco)
        try:
            contagens = [conexao.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]
                         for tabela in ("culturas", "parametros_cultura", "configuracao_sistema")]
            modo_journal = conexao.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conexao.close()
        self.assertEqual(contagens, [2, 6, 12])
        self.assertEqual(modo_journal, "wal")
        self.assertEqual(obter_configuracao("cultura_ativa", caminho_banco=self.banco), "cafe_conilon")
        solo = self.arabica.faixa("umidade_solo")
        self.assertEqual((solo.ideal_min, solo.ideal_max, solo.atencao_min, solo.atencao_max,
                          solo.critico_min, solo.critico_max), (60.0, 80.0, 50.0, 88.0, 35.0, 95.0))
        temperatura = self.conilon.faixa("temperatura_ar")
        self.assertEqual((temperatura.ideal_min, temperatura.ideal_max, temperatura.atencao_min,
                          temperatura.atencao_max, temperatura.critico_min, temperatura.critico_max),
                         (22.0, 26.0, 19.0, 29.0, 12.0, 34.0))
        with self.assertRaises(KeyError):
            self.arabica.faixa("luminosidade")
        with self.assertRaises(ValueError):
            buscar_parametros_cultura("nao_existe", caminho_banco=self.banco)

    def test_inserir_e_buscar(self) -> None:
        """Inserção grava NaN bruto como NULL; busca traz o JOIN e a ordem é da mais recente."""
        primeiro = inserir_leitura(self._registro(), caminho_banco=self.banco)
        segundo = inserir_leitura(self._registro(data_hora="2026-01-01 11:00:00", codigo_cultura="cafe_conilon",
                                                 status_alerta=1, status_regra=1), caminho_banco=self.banco)
        self.assertEqual(segundo, primeiro + 1)
        ultima = buscar_ultima_leitura(caminho_banco=self.banco)
        assert ultima is not None
        self.assertEqual((ultima["id_leitura"], ultima["codigo_cultura"], ultima["nome_cultura"]),
                         (segundo, "cafe_conilon", "Café Conilon"))
        arabica = buscar_ultima_leitura("cafe_arabica", caminho_banco=self.banco)
        assert arabica is not None
        self.assertIsNone(arabica["umidade_ar_bruta"])
        self.assertEqual(arabica["descricao_anomalia"], "umidade_ar:nan")
        self.assertEqual([linha["id_leitura"] for linha in buscar_historico(limite=24, caminho_banco=self.banco)],
                         [segundo, primeiro])
        self.assertEqual(len(buscar_historico(limite=1, caminho_banco=self.banco)), 1)
        self.assertIsNone(buscar_ultima_leitura("cafe_arabica", caminho_banco=self.pasta / "vazio.db"))

    def test_inserir_recusa_registro_invalido(self) -> None:
        """Origem, status, probabilidade e cultura inválidos são recusados."""
        invalidos = [dict(origem="BLUETOOTH"), dict(status_alerta=3), dict(probabilidade_predicao=1.5),
                     dict(codigo_cultura="banana_inexistente"), dict(flag_anomalia=2), dict(temperatura_ar=float("nan"))]
        for alteracao in invalidos:
            with self.subTest(alteracao=alteracao):
                with self.assertRaises(ValueError):
                    inserir_leitura(self._registro(**alteracao), caminho_banco=self.banco)
        self.assertEqual(buscar_historico(caminho_banco=self.banco), [])

    def test_configuracoes_tipadas(self) -> None:
        """Conversão pela coluna tipo; booleano gravado como 'true'; tipo preservado."""
        self.assertEqual(obter_configuracao_tipada("baud_rate", caminho_banco=self.banco), 9600)
        self.assertEqual(obter_configuracao_tipada("intervalo_coleta_segundos", caminho_banco=self.banco), 5.0)
        self.assertEqual(obter_configuracao_tipada("chave_inexistente", "padrao", caminho_banco=self.banco), "padrao")
        definir_configuracao("modo_silencioso", True, caminho_banco=self.banco)
        self.assertEqual(obter_configuracao("modo_silencioso", caminho_banco=self.banco), "true")
        self.assertIs(obter_configuracao_tipada("modo_silencioso", caminho_banco=self.banco), True)
        with self.assertRaises(ValueError):
            definir_configuracao("baud_rate", "abc", caminho_banco=self.banco)

    def test_cadastrar_cultura_escalavel(self) -> None:
        """Nova cultura sem mudar código; faixas fora de ordem são recusadas sem gravar nada."""
        id_tomate = self.cadastrar_tomate()
        self.assertEqual(id_tomate, 3)
        self.assertEqual(self.cadastrar_tomate(), 3)
        tomate = buscar_parametros_cultura("tomate", caminho_banco=self.banco)
        self.assertIn("ph_solo", tomate.faixas)
        self.assertIn("tomate", [cultura["codigo"] for cultura in listar_culturas(caminho_banco=self.banco)])
        with self.assertRaises(ValueError):
            cadastrar_cultura("milho", "Milho", "Zea mays", "grao", "",
                              [FaixaParametro("temperatura_ar", "°C", 30.0, 20.0, 15.0, 35.0, 10.0, 40.0),
                               FaixaParametro("umidade_ar", "%", 60.0, 80.0, 50.0, 90.0, 40.0, 95.0),
                               FaixaParametro("umidade_solo", "%", 60.0, 80.0, 50.0, 88.0, 35.0, 95.0)],
                              caminho_banco=self.banco)
        self.assertNotIn("milho", [cultura["codigo"] for cultura in listar_culturas(caminho_banco=self.banco)])

    def test_encerrar_demonstracao_condicional(self) -> None:
        """O encerramento atômico não apaga uma demonstração iniciada depois da avaliação."""
        definir_configuracao("cenario_demonstracao", "GEADA", caminho_banco=self.banco)
        definir_configuracao("demonstracao_expira_em", "2026-01-01 10:02:00", caminho_banco=self.banco)
        definir_configuracao("modo_operacao", "DEMONSTRACAO", caminho_banco=self.banco)
        self.assertFalse(encerrar_demonstracao_se_inalterada("GEADA", "2026-01-01 10:00:00", "SIMULACAO",
                                                             caminho_banco=self.banco))
        self.assertEqual(obter_configuracao("modo_operacao", caminho_banco=self.banco), "DEMONSTRACAO")
        self.assertTrue(encerrar_demonstracao_se_inalterada("GEADA", "2026-01-01 10:02:00", "SIMULACAO",
                                                            caminho_banco=self.banco))
        self.assertEqual([obter_configuracao(chave, caminho_banco=self.banco) for chave in
                          ("modo_operacao", "cenario_demonstracao", "demonstracao_expira_em")],
                         ["SIMULACAO", "", ""])
        with self.assertRaises(ValueError):
            encerrar_demonstracao_se_inalterada("", "", "DEMONSTRACAO", caminho_banco=self.banco)


# ---------------------------------------------------------------------------
# Pipeline de dados + treinamento em miniatura
# ---------------------------------------------------------------------------
class TestePipelineTreinamento(BaseComBancoTemporario):
    """gerar_dataset_completo → treino → .pkl → predição no coletor."""

    def test_pipeline_em_miniatura(self) -> None:
        """Dataset pequeno com colunas do contrato, RF treinado, pacote .pkl compatível com o coletor."""
        bruto_csv, limpo_csv = self.pasta / "bruto.csv", self.pasta / "limpo.csv"
        with contextlib.redirect_stdout(io.StringIO()):
            bruto, limpo = gerador_dataset.gerar_dataset_completo(
                horas=400, semente=11, caminho_bruto=bruto_csv, caminho_limpo=limpo_csv, caminho_banco=self.banco)
        self.assertEqual(list(pd.read_csv(bruto_csv).columns),
                         ["data_hora", "cultura", "temperatura_ar", "umidade_ar", "umidade_solo", "tipo_ruido_injetado"])
        self.assertEqual(list(pd.read_csv(limpo_csv).columns),
                         ["data_hora", "cultura", "temperatura_ar", "umidade_ar", "umidade_solo", "flag_anomalia",
                          "descricao_anomalia", "deficit_hidrico_solo", "desvio_termico", "status_alerta"])
        self.assertEqual(len(limpo), 800)
        self.assertFalse(limpo[COLUNAS_FEATURES_MODELO].isna().any().any())

        df = treinamento_ia.carregar_dataset(limpo_csv)
        X_treino, X_teste, y_treino, y_teste = treinamento_ia.separar_treino_teste(df)
        self.assertEqual(len(X_teste), 160)
        modelo = treinamento_ia.treinar_random_forest(X_treino, y_treino)
        resultado = treinamento_ia.avaliar_modelo("RandomForestClassifier", modelo, X_teste, y_teste)
        self.assertGreaterEqual(resultado["accuracy"], 0.9)
        culturas = sorted(df["cultura"].astype(str).unique().tolist())
        caminho_pkl = treinamento_ia.salvar_modelo(modelo, "RandomForestClassifier", resultado,
                                                   caminho=self.pasta / "modelo.pkl", culturas=culturas)
        pacote = carregar_modelo(caminho_pkl)
        assert pacote is not None
        self.assertEqual(pacote["culturas"], ["cafe_arabica", "cafe_conilon"])
        self.assertEqual(pacote["features"], COLUNAS_FEATURES_MODELO)
        self.assertEqual(pacote["classes"], [0, 1, 2])
        self.assertTrue(pacote["versao"].startswith("RandomForestClassifier-"))
        leitura = gerar_leitura_cenario("SECA_EXTREMA", self.arabica)
        deficit, desvio = calcular_features(leitura["temperatura_ar"], leitura["umidade_solo"], self.arabica)
        predito, probabilidade, versao = predizer_status(pacote, leitura, deficit, desvio, 2)
        self.assertEqual(predito, 2)
        self.assertTrue(0.0 <= probabilidade <= 1.0)
        self.assertEqual(versao, pacote["versao"])

    def test_falsos_operacionais_e_campeao(self) -> None:
        """FP = real 0 e predito > 0; FN = real > 0 e predito 0; campeão = menor FP+FN."""
        falsos = treinamento_ia.calcular_falsos_operacionais(pd.Series([0, 0, 1, 2, 2]), np.array([0, 1, 0, 1, 2]))
        self.assertEqual((falsos["falsos_positivos"], falsos["falsos_negativos"], falsos["subestimacoes_criticas"]),
                         (1, 1, 1))
        resultados = {
            "A": {"falsos": {"falsos_positivos": 3, "falsos_negativos": 1}, "f1_macro": 0.9},
            "B": {"falsos": {"falsos_positivos": 0, "falsos_negativos": 2}, "f1_macro": 0.8},
            "C": {"falsos": {"falsos_positivos": 1, "falsos_negativos": 1}, "f1_macro": 0.85},
        }
        self.assertEqual(treinamento_ia.selecionar_campeao(resultados), "C")

    @unittest.skipUnless(database.CAMINHO_MODELO.is_file(), "modelo real ainda não treinado")
    def test_modelo_real_formato(self) -> None:
        """O .pkl gerado por treinamento_ia.py segue o formato do contrato."""
        pacote = carregar_modelo(database.CAMINHO_MODELO)
        assert pacote is not None
        self.assertEqual(set(pacote), {"modelo", "nome_modelo", "versao", "features", "classes", "metricas",
                                       "treinado_em", "culturas"})
        self.assertEqual(pacote["culturas"], ["cafe_arabica", "cafe_conilon"])
        self.assertEqual(pacote["features"], COLUNAS_FEATURES_MODELO)
        for cenario, dados in CENARIOS_DEMONSTRACAO.items():
            leitura = gerar_leitura_cenario(cenario, self.arabica)
            deficit, desvio = calcular_features(leitura["temperatura_ar"], leitura["umidade_solo"], self.arabica)
            regra = calcular_status_regra(leitura["temperatura_ar"], leitura["umidade_solo"], self.arabica)
            predito, _, _ = predizer_status(pacote, leitura, deficit, desvio, regra)
            self.assertEqual(predito, int(dados["status_esperado"]), cenario)


# ---------------------------------------------------------------------------
# Coletor serial
# ---------------------------------------------------------------------------
class TesteProtocoloSerial(unittest.TestCase):
    """Interpretação das linhas do firmware e predição sem modelo."""

    def test_interpretar_linha_serial(self) -> None:
        """Dados viram tupla; diagnóstico/malformada viram None; 'nan' vira None no campo."""
        self.assertEqual(interpretar_linha_serial("21.0,65.0,72.4\r\n"), (21.0, 65.0, 72.4))
        self.assertEqual(interpretar_linha_serial("20.7,nan,69.9"), (20.7, None, 69.9))
        for linha in ("#AGROANALISE_PRONTO", "#ERRO:DHT11_LEITURA_INVALIDA", "#AVISO:HIGROMETRO_FORA_DA_FAIXA",
                      "", "1,2", "a,b,c", "1,2,3,4"):
            with self.subTest(linha=linha):
                self.assertIsNone(interpretar_linha_serial(linha))

    def test_predizer_sem_modelo(self) -> None:
        """Sem .pkl: (status_regra, 1.0, 'REGRAS'); pacote quebrado cai no mesmo fallback."""
        valores = {"temperatura_ar": 20.0, "umidade_ar": 70.0, "umidade_solo": 70.0}
        self.assertEqual(predizer_status(None, valores, 0.0, 0.0, 1), (1, 1.0, "REGRAS"))
        quebrado = {"modelo": object(), "features": ["x"], "versao": "quebrado"}
        self.assertEqual(predizer_status(quebrado, valores, 0.0, 0.0, 2), (2, 1.0, "REGRAS"))

    def test_carregar_modelo_ausente_ou_corrompido(self) -> None:
        """Arquivo ausente ou corrompido retorna None (o sistema segue com as regras)."""
        with tempfile.TemporaryDirectory() as pasta:
            self.assertIsNone(carregar_modelo(Path(pasta) / "nao_existe.pkl"))
            corrompido = Path(pasta) / "corrompido.pkl"
            corrompido.write_bytes(b"isto nao e um pickle")
            self.assertIsNone(carregar_modelo(corrompido))


class TesteColetor(BaseComBancoTemporario):
    """Máquina de estados do coletor com banco temporário."""

    def _coletor(self, iteracoes: int, **argumentos: Any) -> ColetorAgroAnalise:
        """Coletor rápido, sem modelo (versão REGRAS), apontado para o banco temporário."""
        coletor = ColetorAgroAnalise(caminho_banco=self.banco, intervalo_segundos=0.2, max_iteracoes=iteracoes,
                                     **argumentos)
        coletor.caminho_modelo = self.pasta / "sem_modelo.pkl"
        return coletor

    def test_fallback_simulacao_forcada(self) -> None:
        """ColetorAgroAnalise(forcar_simulacao=True, max_iteracoes=3): 3 leituras de SIMULACAO."""
        coletor = self._coletor(3, forcar_simulacao=True)
        coletor.executar()
        leituras = self.leituras()
        self.assertEqual(len(leituras), 3)
        self.assertEqual({leitura["origem"] for leitura in leituras}, {"SIMULACAO"})
        self.assertEqual({leitura["versao_modelo"] for leitura in leituras}, {"REGRAS"})
        self.assertEqual(coletor.iteracoes_executadas, 3)
        self.assertEqual(obter_configuracao("modo_operacao", caminho_banco=self.banco), "SIMULACAO")
        self.assertEqual(obter_configuracao("estado_hardware", caminho_banco=self.banco), "DESCONECTADO")

    def test_porta_inexistente_cai_para_simulacao(self) -> None:
        """Porta configurada que não abre: o coletor não trava e grava em Modo Simulação."""
        coletor = self._coletor(2, porta=str(self.pasta / "porta_que_nao_existe"))
        coletor.executar()
        self.assertEqual([leitura["origem"] for leitura in self.leituras()], ["SIMULACAO", "SIMULACAO"])

    def test_troca_de_cultura_em_tempo_real(self) -> None:
        """--cultura / cultura_ativa: as leituras passam a ser da cultura configurada."""
        definir_configuracao("cultura_ativa", "cafe_conilon", caminho_banco=self.banco)
        self._coletor(2, forcar_simulacao=True).executar()
        self.assertEqual({leitura["codigo_cultura"] for leitura in self.leituras()}, {"cafe_conilon"})

    def test_modo_demonstracao_persiste_e_expira(self) -> None:
        """Demonstração vigente gera leituras do cenário; vencida é encerrada pelo coletor."""
        expira = (datetime.now() + timedelta(seconds=60)).strftime(FORMATO)
        definir_configuracao("demonstracao_expira_em", expira, caminho_banco=self.banco)
        definir_configuracao("cenario_demonstracao", "SECA_EXTREMA", caminho_banco=self.banco)
        definir_configuracao("modo_operacao", "DEMONSTRACAO", caminho_banco=self.banco)
        self._coletor(2, forcar_simulacao=True).executar()
        leituras = self.leituras()
        self.assertEqual([(leitura["origem"], leitura["status_alerta"]) for leitura in leituras],
                         [("DEMONSTRACAO", 2), ("DEMONSTRACAO", 2)])
        self.assertEqual(obter_configuracao("modo_operacao", caminho_banco=self.banco), "DEMONSTRACAO")

        vencida = (datetime.now() - timedelta(seconds=1)).strftime(FORMATO)
        definir_configuracao("demonstracao_expira_em", vencida, caminho_banco=self.banco)
        self._coletor(1, forcar_simulacao=True).executar()
        self.assertEqual(self.leituras()[-1]["origem"], "SIMULACAO")
        self.assertEqual([obter_configuracao(chave, caminho_banco=self.banco) for chave in
                          ("modo_operacao", "cenario_demonstracao", "demonstracao_expira_em")],
                         ["SIMULACAO", "", ""])


    def test_nao_apaga_demonstracao_recem_iniciada(self) -> None:
        """Corrida: o coletor leu uma demonstração vencida, mas o app iniciou outra antes do encerramento."""
        vencida = (datetime.now() - timedelta(seconds=5)).strftime(FORMATO)
        definir_configuracao("demonstracao_expira_em", vencida, caminho_banco=self.banco)
        definir_configuracao("cenario_demonstracao", "GEADA", caminho_banco=self.banco)
        definir_configuracao("modo_operacao", "DEMONSTRACAO", caminho_banco=self.banco)
        coletor = self._coletor(1, forcar_simulacao=True)
        coletor._atualizar_configuracoes()  # snapshot com a demonstração vencida
        nova = (datetime.now() + timedelta(seconds=60)).strftime(FORMATO)
        definir_configuracao("demonstracao_expira_em", nova, caminho_banco=self.banco)  # mesma ordem do app
        definir_configuracao("cenario_demonstracao", "SECA_EXTREMA", caminho_banco=self.banco)
        self.assertTrue(coletor._demonstracao_ativa())
        self.assertEqual([obter_configuracao(chave, caminho_banco=self.banco) for chave in
                          ("modo_operacao", "cenario_demonstracao", "demonstracao_expira_em")],
                         ["DEMONSTRACAO", "SECA_EXTREMA", nova])


@unittest.skipUnless(sys.platform.startswith("linux") and hasattr(os, "openpty"), "emulador pty requer Linux")
class TesteCaminhoSerialPty(BaseComBancoTemporario):
    """Caminho serial REAL (pyserial) com um Arduino emulado em pseudo-terminal."""

    def setUp(self) -> None:
        """Cria o par pty e zera a espera de reset do Arduino (só nos testes)."""
        super().setUp()
        import tty
        self.mestre, self.escravo = os.openpty()
        tty.setraw(self.escravo)
        self.nome_porta = os.ttyname(self.escravo)
        self._espera_original = coletor_serial.ESPERA_RESET_ARDUINO_SEGUNDOS
        coletor_serial.ESPERA_RESET_ARDUINO_SEGUNDOS = 0.0
        self._parar_envio = threading.Event()
        self._mestre_fechado = False

    def tearDown(self) -> None:
        """Restaura a constante e fecha os descritores."""
        coletor_serial.ESPERA_RESET_ARDUINO_SEGUNDOS = self._espera_original
        self._parar_envio.set()
        self._fechar_mestre()
        with contextlib.suppress(OSError):
            os.close(self.escravo)
        super().tearDown()

    def _fechar_mestre(self) -> None:
        """Simula o cabo USB sendo puxado."""
        if not self._mestre_fechado:
            self._mestre_fechado = True
            os.close(self.mestre)

    def _enviar(self, linhas: List[str], periodo: float) -> None:
        """Firmware emulado: envia as linhas em ciclo até ser parado."""
        indice = 0
        while not self._parar_envio.is_set():
            try:
                os.write(self.mestre, (linhas[indice % len(linhas)] + "\r\n").encode("ascii"))
            except OSError:
                return
            indice += 1
            self._parar_envio.wait(periodo)

    def test_ler_linha_mais_recente(self) -> None:
        """Drena o buffer, ignora diagnósticos e devolve a linha de dados mais recente."""
        # O pyserial descarta o buffer de entrada ao abrir a porta: escreve depois de abrir.
        conexao = serial.Serial(self.nome_porta, 9600, timeout=0.2)
        try:
            os.write(self.mestre, b"#AGROANALISE_PRONTO\r\n21.0,65.0,72.4\r\n#ERRO:DHT11_LEITURA_INVALIDA\r\n"
                                  b"21.5,64.0,71.0\r\n#AVISO:HIGROMETRO_FORA_DA_FAIXA\r\n")
            self.assertEqual(ler_linha_mais_recente(conexao, timeout_total=1.0), (21.5, 64.0, 71.0))
            self.assertIsNone(ler_linha_mais_recente(conexao, timeout_total=0.3))
            self._fechar_mestre()
            with self.assertRaises(serial.SerialException):
                ler_linha_mais_recente(conexao, timeout_total=0.5)
        finally:
            conexao.close()

    def test_campo_usb_e_queda_para_simulacao(self) -> None:
        """Arduino emulado → CAMPO_USB (com anomalia corrigida); cabo puxado → SIMULACAO e DESCONECTADO."""
        envio = threading.Thread(target=self._enviar, args=(["21.0,65.0,72.4", "#ERRO:DHT11_LEITURA_INVALIDA",
                                                             "21.1,nan,72.3", "21.2,64.8,72.2"], 0.05), daemon=True)
        envio.start()
        coletor = self._coletor_pty(18)
        execucao = threading.Thread(target=coletor.executar, daemon=True)
        execucao.start()
        prazo = time.monotonic() + 15
        while time.monotonic() < prazo:
            origens = [leitura["origem"] for leitura in self.leituras()]
            if origens.count("CAMPO_USB") >= 4:
                break
            time.sleep(0.1)
        self.assertEqual(obter_configuracao("estado_hardware", caminho_banco=self.banco), "CONECTADO")
        self.assertEqual(obter_configuracao("modo_operacao", caminho_banco=self.banco), "CAMPO_USB")
        self._parar_envio.set()
        envio.join(timeout=2)
        self._fechar_mestre()
        execucao.join(timeout=60)
        self.assertFalse(execucao.is_alive(), "o coletor não terminou")
        leituras = self.leituras()
        origens = [leitura["origem"] for leitura in leituras]
        self.assertGreaterEqual(origens.count("CAMPO_USB"), 4)
        primeira_simulacao = origens.index("SIMULACAO")
        self.assertTrue(all(origem == "CAMPO_USB" for origem in origens[:primeira_simulacao]))
        self.assertTrue(all(origem == "SIMULACAO" for origem in origens[primeira_simulacao:]))
        self.assertEqual(obter_configuracao("estado_hardware", caminho_banco=self.banco), "DESCONECTADO")
        self.assertEqual(obter_configuracao("modo_operacao", caminho_banco=self.banco), "SIMULACAO")
        for leitura in leituras:
            if leitura["origem"] == "CAMPO_USB" and leitura["umidade_ar_bruta"] is None:
                self.assertEqual((leitura["flag_anomalia"], leitura["descricao_anomalia"]), (1, "umidade_ar:nan"))

    def _coletor_pty(self, iteracoes: int) -> ColetorAgroAnalise:
        """Coletor apontado para o pty, sem modelo."""
        coletor = ColetorAgroAnalise(caminho_banco=self.banco, porta=self.nome_porta, intervalo_segundos=0.2,
                                     max_iteracoes=iteracoes)
        coletor.caminho_modelo = self.pasta / "sem_modelo.pkl"
        return coletor


# ---------------------------------------------------------------------------
# Rotas Flask
# ---------------------------------------------------------------------------
class TesteRotasFlask(BaseComBancoTemporario):
    """API JSON e página com app.test_client() sobre o banco temporário."""

    def setUp(self) -> None:
        """Aponta o app para o banco temporário e para um modelo inexistente (REGRAS)."""
        super().setUp()
        self._config_original = (app.config["CAMINHO_BANCO"], app.config["CAMINHO_MODELO"])
        app.config["CAMINHO_BANCO"] = self.banco
        app.config["CAMINHO_MODELO"] = self.pasta / "sem_modelo.pkl"
        app.config["TESTING"] = True
        self.cliente = app.test_client()

    def tearDown(self) -> None:
        """Restaura a configuração do app."""
        app.config["CAMINHO_BANCO"], app.config["CAMINHO_MODELO"] = self._config_original
        super().tearDown()

    def _post(self, rota: str, corpo: Any) -> Any:
        """POST com corpo JSON."""
        return self.cliente.post(rota, json=corpo)

    def test_pagina_inicial_offline(self) -> None:
        """GET / devolve o HTML sem dependências externas."""
        resposta = self.cliente.get("/")
        self.assertEqual(resposta.status_code, 200)
        html = resposta.get_data(as_text=True)
        self.assertIn("speechSynthesis", html)
        self.assertIn("pt-BR", html)
        self.assertNotIn("cdn.", html)
        self.assertNotIn("<script src=", html)

    def test_status_sem_leitura(self) -> None:
        """Sem leitura: tem_leitura=false, amarelo e mensagem do contrato."""
        resposta = self.cliente.get("/api/status")
        self.assertEqual(resposta.status_code, 200)
        dados = resposta.get_json()
        chaves = {"tem_leitura", "status_alerta", "cor", "titulo", "mensagem_voz", "nome_cultura", "codigo_cultura",
                  "temperatura_ar", "umidade_ar", "umidade_solo", "deficit_hidrico_solo", "desvio_termico",
                  "data_hora", "origem", "modo_operacao", "estado_hardware", "flag_anomalia", "versao_modelo",
                  "cenario_demonstracao", "nome_propriedade"}
        self.assertTrue(chaves.issubset(dados))
        self.assertEqual((dados["tem_leitura"], dados["status_alerta"], dados["cor"]), (False, 1, "amarelo"))
        self.assertEqual(dados["mensagem_voz"],
                         "Ainda não recebi dados do sensor. Verifique se o aparelho está ligado.")
        self.assertIn("Ainda não recebi", resposta.get_data(as_text=True))  # ensure_ascii desligado

    def test_status_com_leitura_do_coletor(self) -> None:
        """Leitura gravada pelo coletor aparece no semáforo."""
        ColetorAgroAnalise(caminho_banco=self.banco, forcar_simulacao=True, intervalo_segundos=0.2,
                           max_iteracoes=1).executar()
        dados = self.cliente.get("/api/status").get_json()
        self.assertTrue(dados["tem_leitura"])
        self.assertEqual(dados["origem"], "SIMULACAO")
        self.assertIn(dados["cor"], ("verde", "amarelo", "vermelho"))

    def test_culturas_e_troca(self) -> None:
        """Lista culturas, troca a ativa e valida erros 400/404."""
        dados = self.cliente.get("/api/culturas").get_json()
        self.assertEqual([cultura["codigo"] for cultura in dados["culturas"]], ["cafe_arabica", "cafe_conilon"])
        self.assertEqual(dados["cultura_ativa"], "cafe_arabica")
        self.assertEqual(self._post("/api/cultura", {"codigo": "cafe_conilon"}).status_code, 200)
        self.assertEqual(obter_configuracao("cultura_ativa", caminho_banco=self.banco), "cafe_conilon")
        self.assertEqual(self._post("/api/cultura", {"codigo": "banana"}).status_code, 404)
        self.assertEqual(self._post("/api/cultura", {}).status_code, 400)
        resposta = self.cliente.post("/api/cultura", data="nao e json", content_type="application/json")
        self.assertEqual(resposta.status_code, 400)
        self.assertIn("erro", resposta.get_json())

    def test_cenarios_e_demonstracao(self) -> None:
        """Cada cenário injetado grava a leitura na hora com o status esperado."""
        cenarios = self.cliente.get("/api/cenarios").get_json()["cenarios"]
        self.assertEqual(len(cenarios), 6)
        for cultura in ("cafe_arabica", "cafe_conilon"):
            self._post("/api/cultura", {"codigo": cultura})
            for cenario in cenarios:
                with self.subTest(cultura=cultura, cenario=cenario["codigo"]):
                    resposta = self._post("/api/demonstracao", {"cenario": cenario["codigo"]})
                    self.assertEqual(resposta.status_code, 200)
                    dados = resposta.get_json()
                    self.assertEqual(dados["status_alerta"], cenario["status_esperado"])
                    self.assertEqual(dados["modo_operacao"], "DEMONSTRACAO")
                    self.assertEqual(dados["origem"], "DEMONSTRACAO")
                    self.assertEqual(dados["codigo_cultura"], cultura)
                    self.assertEqual(obter_configuracao("cenario_demonstracao", caminho_banco=self.banco),
                                     cenario["codigo"])
        self.assertEqual(self._post("/api/demonstracao", {"cenario": "TERREMOTO"}).status_code, 404)
        self.assertEqual(self._post("/api/demonstracao", {}).status_code, 400)

    def test_encerrar_demonstracao(self) -> None:
        """Encerrar volta ao modo normal e as leituras de demonstração deixam de ser exibidas."""
        self._post("/api/demonstracao", {"cenario": "SECA_EXTREMA"})
        dados = self._post("/api/demonstracao/encerrar", {}).get_json()
        self.assertEqual(dados["modo_operacao"], "SIMULACAO")
        self.assertFalse(dados["status"]["tem_leitura"])
        definir_configuracao("estado_hardware", "CONECTADO", caminho_banco=self.banco)
        self._post("/api/demonstracao", {"cenario": "GEADA"})
        self.assertEqual(self._post("/api/demonstracao/encerrar", {}).get_json()["modo_operacao"], "CAMPO_USB")
        self.assertEqual(obter_configuracao("cenario_demonstracao", caminho_banco=self.banco), "")

    def test_demonstracao_vencida_encerrada_pelo_servidor(self) -> None:
        """Uma demonstração vencida é encerrada ao consultar o status."""
        self._post("/api/demonstracao", {"cenario": "ENCHARCAMENTO"})
        definir_configuracao("demonstracao_expira_em", (datetime.now() - timedelta(seconds=5)).strftime(FORMATO),
                             caminho_banco=self.banco)
        dados = self.cliente.get("/api/status").get_json()
        self.assertEqual(dados["modo_operacao"], "SIMULACAO")
        self.assertEqual(dados["cenario_demonstracao"], "")

    def test_historico(self) -> None:
        """Histórico com limite validado (1–500)."""
        self._post("/api/demonstracao", {"cenario": "GEADA"})
        self._post("/api/demonstracao", {"cenario": "CONDICAO_IDEAL"})
        leituras = self.cliente.get("/api/historico?limite=1").get_json()["leituras"]
        self.assertEqual(len(leituras), 1)
        self.assertEqual(leituras[0]["status_alerta"], 0)
        self.assertEqual(len(self.cliente.get("/api/historico").get_json()["leituras"]), 2)
        for limite in ("0", "501", "abc"):
            with self.subTest(limite=limite):
                self.assertEqual(self.cliente.get(f"/api/historico?limite={limite}").status_code, 400)

    def test_saude_e_erros_http(self) -> None:
        """Saúde sem modelo (REGRAS); 404/405 em JSON."""
        resposta = self.cliente.get("/api/saude")
        self.assertEqual(resposta.status_code, 200)
        dados = resposta.get_json()
        self.assertEqual((dados["ok"], dados["banco"], dados["modelo_carregado"]), (True, True, False))
        self.assertEqual(dados["modo_operacao"], "SIMULACAO")
        self.assertEqual(self.cliente.get("/api/nao_existe").status_code, 404)
        self.assertEqual(self.cliente.delete("/api/status").status_code, 405)

    @unittest.skipUnless(database.CAMINHO_MODELO.is_file(), "modelo real ainda não treinado")
    def test_saude_com_modelo_real(self) -> None:
        """Com o .pkl real o app informa a versão do modelo."""
        app.config["CAMINHO_MODELO"] = database.CAMINHO_MODELO
        dados = self.cliente.get("/api/saude").get_json()
        self.assertTrue(dados["modelo_carregado"])
        self.assertTrue(dados["versao_modelo"].startswith("RandomForestClassifier-"))
        resposta = self._post("/api/demonstracao", {"cenario": "SECA_EXTREMA"}).get_json()
        self.assertEqual(resposta["versao_modelo"], dados["versao_modelo"])
        self.assertEqual(resposta["status_predito"], 2)


# ---------------------------------------------------------------------------
# Lacunas da auditoria final
# ---------------------------------------------------------------------------
def _pacote_sempre_vermelho(culturas: Optional[List[str]]) -> Dict[str, Any]:
    """Pacote no formato do contrato com um modelo que SEMPRE prevê Vermelho (2)."""
    modelo = DummyClassifier(strategy="constant", constant=2)
    entrada = pd.DataFrame([[20.0, 70.0, 70.0, 0.0, 0.0]] * 3, columns=COLUNAS_FEATURES_MODELO)
    modelo.fit(entrada, [0, 1, 2])
    pacote: Dict[str, Any] = {"modelo": modelo, "nome_modelo": "Fixo", "versao": "Fixo-1",
                              "features": list(COLUNAS_FEATURES_MODELO), "classes": [0, 1, 2], "metricas": {},
                              "treinado_em": "2026-01-01T00:00:00"}
    if culturas is not None:
        pacote["culturas"] = culturas
    return pacote


class TesteModeloPorCultura(BaseComBancoTemporario):
    """O .pkl só vale para as culturas do treino; nas demais o motor de regras decide."""

    def test_predizer_status_respeita_culturas_do_treino(self) -> None:
        """Cultura fora de pacote["culturas"] → (regra, 1.0, REGRAS); dentro, ou sem cultura informada → IA."""
        pacote = _pacote_sempre_vermelho(["cafe_arabica"])
        valores = {"temperatura_ar": 20.0, "umidade_ar": 70.0, "umidade_solo": 70.0}
        self.assertEqual(predizer_status(pacote, valores, 0.0, 0.0, 0, codigo_cultura="tomate"), (0, 1.0, "REGRAS"))
        self.assertEqual(predizer_status(pacote, valores, 0.0, 0.0, 0, codigo_cultura="cafe_arabica")[0], 2)
        self.assertEqual(predizer_status(pacote, valores, 0.0, 0.0, 0)[0], 2)
        self.assertFalse(modelo_cobre_cultura(None, "cafe_arabica"))
        self.assertTrue(modelo_cobre_cultura(_pacote_sempre_vermelho(None), "tomate"))  # pacote antigo
        self.assertFalse(modelo_cobre_cultura(_pacote_sempre_vermelho([]), "cafe_arabica"))

    def test_cultura_nova_sem_alarme_falso(self) -> None:
        """Cultura cadastrada depois do treino: condição ideal continua Verde (sem max(regra, IA) falso)."""
        self.cadastrar_tomate()
        tomate = buscar_parametros_cultura("tomate", caminho_banco=self.banco)
        pacote = _pacote_sempre_vermelho(["cafe_arabica", "cafe_conilon"])
        leitura = gerar_leitura_cenario("CONDICAO_IDEAL", tomate)
        _, registro = processar_leitura(leitura["temperatura_ar"], leitura["umidade_ar"], leitura["umidade_solo"],
                                        "SIMULACAO", "tomate", SaneadorTempoReal(tomate), pacote,
                                        caminho_banco=self.banco)
        self.assertEqual((registro.status_regra, registro.status_predito, registro.status_alerta), (0, 0, 0))
        self.assertEqual(registro.versao_modelo, "REGRAS")
        _, registro_cafe = processar_leitura(20.0, 70.0, 70.0, "SIMULACAO", "cafe_arabica",
                                             SaneadorTempoReal(self.arabica), pacote, caminho_banco=self.banco)
        self.assertEqual((registro_cafe.status_predito, registro_cafe.versao_modelo), (2, "Fixo-1"))

    def test_carregar_modelo_normaliza_culturas(self) -> None:
        """carregar_modelo devolve a lista de culturas ordenada e sem repetição."""
        caminho = self.pasta / "fixo.pkl"
        joblib.dump(_pacote_sempre_vermelho(["cafe_conilon", "cafe_arabica", "cafe_conilon"]), caminho)
        pacote = carregar_modelo(caminho)
        assert pacote is not None
        self.assertEqual(pacote["culturas"], ["cafe_arabica", "cafe_conilon"])

    def test_mensagem_quando_ia_eleva_sem_causa(self) -> None:
        """Alerta com tudo na faixa ideal aponta a borda mais próxima, com ação concreta."""
        calor = gerar_mensagem_orientacao(1, 0.0, 0.0, self.arabica, 21.8, 70.0)
        self.assertIn("perto do limite de calor", calor)
        seca = gerar_mensagem_orientacao(1, 0.0, 0.0, self.arabica, 20.0, 61.0)
        self.assertIn("quase secando", seca)
        for mensagem in (calor, seca):
            self.assertNotIn("notou uma mudança", mensagem)


class TesteConfiguracaoAtomica(BaseComBancoTemporario):
    """Gravação condicional do modo (sem janela de corrida entre conferir e gravar)."""

    def test_definir_configuracao_condicional(self) -> None:
        """exigir/proibir valor atual; chave inexistente não é criada."""
        definir_configuracao("modo_operacao", "DEMONSTRACAO", caminho_banco=self.banco)
        self.assertFalse(definir_configuracao_condicional("modo_operacao", "SIMULACAO",
                                                          proibir_valor_atual="DEMONSTRACAO", caminho_banco=self.banco))
        self.assertEqual(obter_configuracao("modo_operacao", caminho_banco=self.banco), "DEMONSTRACAO")
        self.assertFalse(definir_configuracao_condicional("modo_operacao", "SIMULACAO", exigir_valor_atual="CAMPO_USB",
                                                          caminho_banco=self.banco))
        definir_configuracao("modo_operacao", "CAMPO_USB", caminho_banco=self.banco)
        self.assertTrue(definir_configuracao_condicional("modo_operacao", "SIMULACAO", exigir_valor_atual="CAMPO_USB",
                                                         caminho_banco=self.banco))
        self.assertEqual(obter_configuracao("modo_operacao", caminho_banco=self.banco), "SIMULACAO")
        self.assertFalse(definir_configuracao_condicional("chave_inexistente", "x", caminho_banco=self.banco))
        self.assertIsNone(obter_configuracao("chave_inexistente", caminho_banco=self.banco))

    def test_coletor_nao_sobrescreve_demonstracao(self) -> None:
        """_gravar_modo e encerrar() com configuração velha preservam a demonstração gravada pelo app."""
        coletor = ColetorAgroAnalise(caminho_banco=self.banco, forcar_simulacao=True)
        coletor._atualizar_configuracoes()
        coletor._config["modo_operacao"] = "CAMPO_USB"  # o que o coletor leu no início do ciclo
        definir_configuracao("modo_operacao", "DEMONSTRACAO", caminho_banco=self.banco)  # o app grava depois
        coletor._gravar_modo("SIMULACAO")
        self.assertEqual(obter_configuracao("modo_operacao", caminho_banco=self.banco), "DEMONSTRACAO")
        coletor._config["modo_operacao"] = "CAMPO_USB"
        coletor.encerrar()
        self.assertEqual(obter_configuracao("modo_operacao", caminho_banco=self.banco), "DEMONSTRACAO")
        definir_configuracao("modo_operacao", "CAMPO_USB", caminho_banco=self.banco)
        coletor.encerrar()
        self.assertEqual(obter_configuracao("modo_operacao", caminho_banco=self.banco), "SIMULACAO")


class TesteExibicaoSemaforo(BaseComBancoTemporario):
    """Histerese, leitura antiga, simulação e troca de cultura em /api/status."""

    def setUp(self) -> None:
        """App apontado para o banco temporário, sem modelo."""
        super().setUp()
        self._config_original = (app.config["CAMINHO_BANCO"], app.config["CAMINHO_MODELO"])
        app.config["CAMINHO_BANCO"] = self.banco
        app.config["CAMINHO_MODELO"] = self.pasta / "sem_modelo.pkl"
        self.cliente = app.test_client()

    def tearDown(self) -> None:
        """Restaura a configuração do app."""
        app.config["CAMINHO_BANCO"], app.config["CAMINHO_MODELO"] = self._config_original
        super().tearDown()

    def _inserir(self, segundos_atras: float, status: int, origem: str = "CAMPO_USB",
                 cultura: str = "cafe_arabica", temperatura: float = 20.0) -> int:
        """Grava uma leitura com data/hora relativa a agora."""
        mensagens = {0: "Tudo bem.", 1: "Atenção.", 2: "Perigo."}
        return inserir_leitura(RegistroLeitura(
            data_hora=(datetime.now() - timedelta(seconds=segundos_atras)).strftime(FORMATO), codigo_cultura=cultura,
            origem=origem, temperatura_ar_bruta=temperatura, umidade_ar_bruta=70.0, umidade_solo_bruta=70.0,
            temperatura_ar=temperatura, umidade_ar=70.0, umidade_solo=70.0, deficit_hidrico_solo=0.0,
            desvio_termico=0.0, status_regra=status, status_predito=status, status_alerta=status,
            probabilidade_predicao=1.0, flag_anomalia=0, descricao_anomalia="", mensagem_voz=mensagens[status],
            versao_modelo="REGRAS"), caminho_banco=self.banco)

    def _status(self) -> Dict[str, Any]:
        """GET /api/status."""
        return self.cliente.get("/api/status").get_json()

    def test_histerese_piora_imediata_melhora_confirmada(self) -> None:
        """Piorar aparece na hora; melhorar só depois de LEITURAS_CONFIRMACAO_MELHORA leituras melhores."""
        self._inserir(40, 1)
        self._inserir(35, 0)
        dados = self._status()
        self.assertEqual((dados["status_alerta"], dados["status_em_confirmacao"]), (1, True))
        self.assertEqual(dados["mensagem_voz"], "Atenção.")
        self._inserir(30, 2)
        self.assertEqual(self._status()["status_alerta"], 2)
        for indice in range(modulo_app.LEITURAS_CONFIRMACAO_MELHORA):
            self._inserir(25 - indice * 0.5, 0)
        dados = self._status()
        self.assertEqual((dados["status_alerta"], dados["status_em_confirmacao"]), (0, False))

    def test_histerese_funcao_pura(self) -> None:
        """aplicar_histerese ignora leituras de outra origem ou fora da janela."""
        agora = datetime.now()
        def leitura(segundos: float, status: int, origem: str = "CAMPO_USB") -> Dict[str, Any]:
            """Leitura mínima para a histerese."""
            return {"status_alerta": status, "origem": origem,
                    "data_hora": (agora - timedelta(seconds=segundos)).strftime(FORMATO)}
        exibida, confirmando = modulo_app.aplicar_histerese([leitura(0, 0), leitura(5, 2, "SIMULACAO")])
        self.assertEqual((exibida["status_alerta"], confirmando), (0, False))
        janela = modulo_app.JANELA_CONFIRMACAO_SEGUNDOS
        exibida, confirmando = modulo_app.aplicar_histerese([leitura(0, 0), leitura(janela + 10, 2)])
        self.assertEqual((exibida["status_alerta"], confirmando), (0, False))
        with self.assertRaises(ValueError):
            modulo_app.aplicar_histerese([])

    def test_leitura_antiga_nao_vira_orientacao(self) -> None:
        """Coletor parado: leitura de dias atrás aparece como 'Sem medições novas' (amarelo)."""
        self._inserir(3 * 86400, 0)
        dados = self._status()
        self.assertTrue(dados["tem_leitura"])
        self.assertTrue(dados["leitura_desatualizada"])
        self.assertEqual((dados["status_alerta"], dados["cor"], dados["titulo"]), (1, "amarelo", "Sem medições novas"))
        self.assertIn("3 dias", dados["mensagem_voz"])
        self.assertNotIn("Tudo", dados["titulo"])
        self._inserir(1, 0)
        dados = self._status()
        self.assertFalse(dados["leitura_desatualizada"])
        self.assertEqual(dados["titulo"], "Tudo certo com o seu café")

    def test_simulacao_avisa_que_sao_dados_de_teste(self) -> None:
        """Leitura do Modo Simulação: título e voz avisam que não é medição da lavoura."""
        ColetorAgroAnalise(caminho_banco=self.banco, forcar_simulacao=True, intervalo_segundos=0.2,
                           max_iteracoes=1).executar()
        dados = self._status()
        self.assertTrue(dados["dados_simulados"])
        self.assertEqual(dados["titulo"], modulo_app.TITULO_SIMULACAO)
        self.assertIn("só um teste", dados["mensagem_voz"])
        self.assertNotIn("Não precisa fazer nada", dados["mensagem_voz"])
        self.assertTrue(dados["orientacao_simulada"])

    def test_troca_de_cultura_reclassifica_medicao_de_campo(self) -> None:
        """Medição de campo recente é reclassificada na hora para a nova cultura (sem 'Esperando o sensor')."""
        self._inserir(2, 1, temperatura=24.0)  # 24 °C: Amarelo no Arábica, Verde no Conilon
        dados = self.cliente.post("/api/cultura", json={"codigo": "cafe_conilon"}).get_json()["status"]
        self.assertTrue(dados["tem_leitura"])
        self.assertEqual((dados["codigo_cultura"], dados["origem"], dados["status_alerta"]),
                         ("cafe_conilon", "CAMPO_USB", 0))
        self.assertEqual(dados["temperatura_ar"], 24.0)

    def test_troca_de_cultura_sem_medicao_de_campo(self) -> None:
        """Só leitura simulada de outra cultura: 'Trocando de plantação', sem pedir para verificar o aparelho."""
        self._inserir(2, 0, origem="SIMULACAO")
        dados = self.cliente.post("/api/cultura", json={"codigo": "cafe_conilon"}).get_json()["status"]
        self.assertFalse(dados["tem_leitura"])
        self.assertEqual(dados["titulo"], "Trocando de plantação")
        self.assertNotIn("Verifique", dados["mensagem_voz"])

    def test_pagina_anuncia_so_mudanca_de_situacao(self) -> None:
        """A chave do anúncio automático não usa o texto da mensagem (números).

        v3 (Frente 3): o minificador encurta os nomes locais do JS (orçamento de 16 KB gzip); a presença de
        ``chaveAnuncio`` é conferida pelo texto fixo que só ela produz ("sem-leitura|").
        """
        html = self.cliente.get("/").get_data(as_text=True)
        self.assertIn('"sem-leitura|"', html)
        self.assertNotIn('nivel + "|" + (dados.mensagem_voz', html)
        self.assertIn("dados_simulados", html)
        self.assertIn("leitura_desatualizada", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
