"""Testes da frente "Confiabilidade 24/7 do campo" (unittest da biblioteca padrão).

Cada classe cobre uma melhoria e seus critérios de aceite, sempre com banco
SQLite TEMPORÁRIO:

    1. Saneamento de campo: mediana móvel, diagnóstico de sensor solto,
       travado ou impossível, plausibilidade física e migração do esquema;
    2. Protocolo serial v2 (checksum), descarte da 1ª linha, saneador semeado,
       linha só com ``nan`` no ar e compilação do firmware com g++;
    3. Gravação sem perda (fila com o banco travado), ordem por gravação,
       batimento do coletor, disco cheio e salto de relógio;
    4. Retenção com agregação horária, backup online e linha de comando;
    5. Operação 24/7: logs rotativos, servidor waitress, units systemd e instalador.

Execução (a partir da raiz do projeto): ``python -m unittest discover -s tests -v``.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from collections import namedtuple
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest import mock

RAIZ_PROJETO: Path = Path(__file__).resolve().parent.parent
PASTA_SRC: Path = RAIZ_PROJETO / "src"
if str(PASTA_SRC) not in sys.path:
    sys.path.insert(0, str(PASTA_SRC))

import numpy as np  # noqa: E402

import coletor_serial  # noqa: E402
import database  # noqa: E402
import registro  # noqa: E402
from coletor_serial import (  # noqa: E402
    ColetorAgroAnalise,
    ContadoresProtocolo,
    SaneadorTempoReal,
    calcular_checksum_nmea,
    carregar_modulo_antecipacao,
    interpretar_linha_serial,
    interpretar_linha_serial_detalhada,
    processar_leitura,
)
from database import (  # noqa: E402
    ParametrosCultura,
    RegistroLeitura,
    aplicar_retencao,
    buscar_historico,
    buscar_parametros_cultura,
    buscar_ultima_leitura,
    buscar_ultima_leitura_origem,
    fazer_backup,
    inicializar_banco,
    inserir_leitura,
    inserir_leituras_em_lote,
    migrar_esquema,
    obter_configuracao,
    obter_todas_configuracoes,
)
from qualidade_sensores import (  # noqa: E402
    MENSAGEM_SENSOR_AR,
    MENSAGEM_SENSOR_SOLO,
    DiagnosticoSensores,
    EstadoSensores,
    FiltroMedianaMovel,
)

FORMATO: str = "%Y-%m-%d %H:%M:%S"
SEMENTE: int = 42

# DDL da baseline v1 (commit 160055b) das tabelas usadas no teste de migração.
DDL_BASELINE_V1: str = """
CREATE TABLE culturas (
    id_cultura      INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo          TEXT    NOT NULL UNIQUE,
    nome_comum      TEXT    NOT NULL,
    nome_cientifico TEXT,
    categoria       TEXT    NOT NULL
                    CHECK (categoria IN ('cafe','fruta','grao','hortalica','leguminosa','outra')),
    descricao       TEXT,
    ativo           INTEGER NOT NULL DEFAULT 1 CHECK (ativo IN (0, 1)),
    criado_em       TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE historico_leituras (
    id_leitura             INTEGER PRIMARY KEY AUTOINCREMENT,
    data_hora              TEXT    NOT NULL,
    id_cultura             INTEGER NOT NULL REFERENCES culturas(id_cultura),
    origem                 TEXT    NOT NULL
                           CHECK (origem IN ('CAMPO_USB','SIMULACAO','DEMONSTRACAO','DATASET')),
    temperatura_ar_bruta   REAL,
    umidade_ar_bruta       REAL,
    umidade_solo_bruta     REAL,
    temperatura_ar         REAL    NOT NULL,
    umidade_ar             REAL    NOT NULL,
    umidade_solo           REAL    NOT NULL,
    deficit_hidrico_solo   REAL    NOT NULL,
    desvio_termico         REAL    NOT NULL,
    status_regra           INTEGER NOT NULL CHECK (status_regra IN (0, 1, 2)),
    status_predito         INTEGER NOT NULL CHECK (status_predito IN (0, 1, 2)),
    status_alerta          INTEGER NOT NULL CHECK (status_alerta IN (0, 1, 2)),
    probabilidade_predicao REAL    NOT NULL
                           CHECK (probabilidade_predicao >= 0.0 AND probabilidade_predicao <= 1.0),
    flag_anomalia          INTEGER NOT NULL DEFAULT 0 CHECK (flag_anomalia IN (0, 1)),
    descricao_anomalia     TEXT    NOT NULL DEFAULT '',
    mensagem_voz           TEXT    NOT NULL,
    versao_modelo          TEXT    NOT NULL
);
CREATE INDEX idx_historico_data_hora ON historico_leituras(data_hora);
CREATE INDEX idx_historico_cultura_data ON historico_leituras(id_cultura, data_hora);
CREATE TABLE configuracao_sistema (
    chave         TEXT PRIMARY KEY,
    valor         TEXT NOT NULL,
    tipo          TEXT NOT NULL CHECK (tipo IN ('str','int','float','bool')),
    descricao     TEXT NOT NULL,
    atualizado_em TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

_COLUNAS_SQL_LEITURA: str = (
    "data_hora, id_cultura, origem, temperatura_ar_bruta, umidade_ar_bruta, umidade_solo_bruta, "
    "temperatura_ar, umidade_ar, umidade_solo, deficit_hidrico_solo, desvio_termico, status_regra, "
    "status_predito, status_alerta, probabilidade_predicao, flag_anomalia, descricao_anomalia, "
    "mensagem_voz, versao_modelo"
)


def setUpModule() -> None:
    """Silencia logs informativos/avisos (erros continuam visíveis)."""
    logging.disable(logging.WARNING)


def tearDownModule() -> None:
    """Reativa os logs."""
    logging.disable(logging.NOTSET)


def linha_v2(temperatura: str, umidade_ar: str, solo: str) -> str:
    """Linha do protocolo 2 com checksum correto (como o firmware envia)."""
    conteudo = f"2,{temperatura},{umidade_ar},{solo}"
    return f"${conteudo}*{calcular_checksum_nmea(conteudo)}"


def trocas_de_cor(status: List[int]) -> int:
    """Quantas vezes a cor muda numa sequência de status."""
    return sum(1 for anterior, atual in zip(status, status[1:]) if anterior != atual)


def registro_exemplo(**alteracoes: Any) -> RegistroLeitura:
    """``RegistroLeitura`` válido (Verde, Café Arábica, agora) com campos alteráveis."""
    dados: Dict[str, Any] = dict(
        data_hora=datetime.now().strftime(FORMATO), codigo_cultura="cafe_arabica", origem="CAMPO_USB",
        temperatura_ar_bruta=20.0, umidade_ar_bruta=70.0, umidade_solo_bruta=70.0, temperatura_ar=20.0,
        umidade_ar=70.0, umidade_solo=70.0, deficit_hidrico_solo=0.0, desvio_termico=0.0, status_regra=0,
        status_predito=0, status_alerta=0, probabilidade_predicao=1.0, flag_anomalia=0, descricao_anomalia="",
        mensagem_voz="Tudo bem.", versao_modelo="REGRAS",
    )
    dados.update(alteracoes)
    return RegistroLeitura(**dados)


class BaseBanco(unittest.TestCase):
    """Diretório e banco SQLite temporários e isolados para cada teste."""

    def setUp(self) -> None:
        """Cria e inicializa o banco temporário."""
        self._diretorio = tempfile.mkdtemp(prefix="agro_confiab_")
        self.pasta = Path(self._diretorio)
        self.banco: Path = self.pasta / "teste.db"
        inicializar_banco(self.banco)
        self.arabica: ParametrosCultura = buscar_parametros_cultura("cafe_arabica", caminho_banco=self.banco)

    def tearDown(self) -> None:
        """Remove o diretório temporário."""
        shutil.rmtree(self._diretorio, ignore_errors=True)

    def leituras(self) -> List[Dict[str, Any]]:
        """Todas as leituras do banco temporário, da mais antiga para a mais recente."""
        return list(reversed(buscar_historico(limite=100000, caminho_banco=self.banco)))

    def saneador_campo(self) -> SaneadorTempoReal:
        """Saneador com as opções do Modo Campo USB."""
        return SaneadorTempoReal(self.arabica, plausibilidade_fisica=True, limite_hold_last=30)

    def coletor(self, iteracoes: int, **argumentos: Any) -> ColetorAgroAnalise:
        """Coletor rápido, sem modelo e sem antecipação, apontado para o banco temporário."""
        coletor = ColetorAgroAnalise(caminho_banco=self.banco, intervalo_segundos=0.2, max_iteracoes=iteracoes,
                                     **argumentos)
        coletor.caminho_modelo = self.pasta / "sem_modelo.pkl"
        coletor.modulo_antecipacao = None
        return coletor


# ---------------------------------------------------------------------------
# 1. Saneamento de campo: mediana móvel e diagnóstico de sensores
# ---------------------------------------------------------------------------
class TesteMedianaEDiagnostico(BaseBanco):
    """Mediana móvel antes da regra; sensor solto, travado ou impossível nunca vira Vermelho."""

    def _serie_pela_mediana(self, solo_verdadeiro: float) -> List[int]:
        """720 leituras com solo ~ N(solo_verdadeiro, 1) pelo pipeline com mediana; devolve os status gravados."""
        gerador = np.random.default_rng(SEMENTE)
        ruido = np.round(solo_verdadeiro + gerador.normal(0.0, 1.0, 720), 1)
        saneador = self.saneador_campo()
        filtro = FiltroMedianaMovel(janela=12)
        estado = EstadoSensores()
        primeiro_id: Optional[int] = None
        for valor in ruido:
            id_leitura, _ = processar_leitura(20.0, 70.0, float(valor), "CAMPO_USB", "cafe_arabica", saneador, None,
                                              caminho_banco=self.banco, filtro=filtro, estado_sensores=estado)
            primeiro_id = primeiro_id or id_leitura
        status = [int(leitura["status_alerta"]) for leitura in self.leituras() if leitura["id_leitura"] >= primeiro_id]
        self.assertEqual(len(status), 720)
        return status

    def test_a_mediana_estabiliza_perto_da_borda(self) -> None:
        """Solo real 1 pp dentro/fora da faixa ideal com ruído σ=1: cor estável (≥ 99 % e no máximo 1 troca)."""
        faixa = self.arabica.faixa("umidade_solo")
        dentro = self._serie_pela_mediana(faixa.ideal_min + 1)
        self.assertGreaterEqual(dentro.count(0) / len(dentro), 0.99)
        self.assertLessEqual(trocas_de_cor(dentro), 1)
        fora = self._serie_pela_mediana(faixa.ideal_min - 1)
        self.assertGreaterEqual(fora.count(1) / len(fora), 0.99)
        self.assertLessEqual(trocas_de_cor(fora), 1)

    def test_a_sem_mediana_o_semaforo_pisca(self) -> None:
        """Controle: a mesma série SEM mediana troca de cor dezenas de vezes (o filtro é que estabiliza)."""
        gerador = np.random.default_rng(SEMENTE)
        ruido = np.round(self.arabica.faixa("umidade_solo").ideal_min + 1 + gerador.normal(0.0, 1.0, 200), 1)
        saneador = self.saneador_campo()
        status = [processar_leitura(20.0, 70.0, float(valor), "CAMPO_USB", "cafe_arabica", saneador, None,
                                    caminho_banco=self.banco)[1].status_alerta for valor in ruido]
        self.assertGreater(trocas_de_cor(status), 10)

    def test_b_valor_critico_sai_sem_mediana(self) -> None:
        """Uma leitura isolada abaixo do crítico de temperatura vira Vermelho já na primeira vez."""
        filtro = FiltroMedianaMovel(janela=12)
        filtro.preencher({"temperatura_ar": 20.0, "umidade_ar": 70.0, "umidade_solo": 70.0})
        critico_min = self.arabica.faixa("temperatura_ar").critico_min
        _, registro_frio = processar_leitura(critico_min - 3, 70.0, 70.0, "CAMPO_USB", "cafe_arabica",
                                             SaneadorTempoReal(self.arabica), None, caminho_banco=self.banco,
                                             filtro=filtro, estado_sensores=EstadoSensores())
        self.assertEqual((registro_frio.status_alerta, registro_frio.temperatura_ar), (2, critico_min - 3))
        self.assertIn("Urgente", registro_frio.mensagem_voz)
        # Controle: um valor fora do ideal mas NÃO crítico continua passando pela mediana.
        filtro.preencher({"temperatura_ar": 20.0, "umidade_ar": 70.0, "umidade_solo": 70.0})
        _, registro_fresco = processar_leitura(16.0, 70.0, 70.0, "CAMPO_USB", "cafe_arabica",
                                               SaneadorTempoReal(self.arabica), None, caminho_banco=self.banco,
                                               filtro=filtro, estado_sensores=EstadoSensores())
        self.assertEqual((registro_fresco.status_alerta, registro_fresco.temperatura_ar), (0, 20.0))

    def test_filtro_reinicia_por_contexto_e_lacuna(self) -> None:
        """Buffer zerado ao mudar (cultura, origem) e depois de uma lacuna maior que idade_maxima_s."""
        filtro = FiltroMedianaMovel(janela=5, idade_maxima_s=90.0)
        contexto = ("cafe_arabica", "CAMPO_USB")
        for indice in range(5):
            filtro.filtrar({"umidade_solo": 70.0}, self.arabica, contexto, float(indice))
        self.assertEqual(filtro.filtrar({"umidade_solo": 50.0}, self.arabica, contexto, 5.0)["umidade_solo"], 70.0)
        self.assertEqual(filtro.filtrar({"umidade_solo": 50.0}, self.arabica, ("cafe_conilon", "CAMPO_USB"),
                                        6.0)["umidade_solo"], 50.0)
        self.assertEqual(filtro.tamanho_buffer("umidade_solo"), 1)
        filtro.filtrar({"umidade_solo": 70.0}, self.arabica, ("cafe_conilon", "CAMPO_USB"), 7.0)
        self.assertEqual(filtro.filtrar({"umidade_solo": 40.0}, self.arabica, ("cafe_conilon", "CAMPO_USB"),
                                        200.0)["umidade_solo"], 40.0)

    def test_c_higrometro_solto_nunca_vira_vermelho(self) -> None:
        """70 % → 0,0 % com '#AVISO:HIGROMETRO_DESCONECTADO': nada de Vermelho; DEFEITO e 'sensor da terra'."""
        saneador = self.saneador_campo()
        filtro = FiltroMedianaMovel()
        diagnostico = DiagnosticoSensores()
        registros: List[RegistroLeitura] = []
        for indice in range(20):
            solo = 70.0 if indice < 10 else 0.0
            agora = time.monotonic()
            if indice == 10:
                diagnostico.registrar_diagnostico("#AVISO:HIGROMETRO_DESCONECTADO", agora)
            estado = diagnostico.avaliar({"temperatura_ar": 20.0, "umidade_ar": 70.0, "umidade_solo": solo}, agora,
                                         hold_last_excedido=saneador.variaveis_hold_last_excedido())
            registros.append(processar_leitura(20.0, 70.0, solo, "CAMPO_USB", "cafe_arabica", saneador, None,
                                               caminho_banco=self.banco, filtro=filtro, estado_sensores=estado)[1])
        gravadas = self.leituras()
        self.assertEqual(len(gravadas), 20)
        self.assertTrue(all(int(leitura["status_alerta"]) < 2 for leitura in gravadas))
        self.assertTrue(all(leitura["estado_sensor"] == "OK" for leitura in gravadas[:10]))
        for leitura in gravadas[10:]:
            self.assertEqual(leitura["estado_sensor"], "DEFEITO")
            self.assertEqual(int(leitura["status_alerta"]), 1)
            self.assertIn("sensor da terra", leitura["mensagem_voz"])
            self.assertIn("higrometro_desconectado", leitura["diagnostico_sensor"])
            self.assertNotIn("Urgente", leitura["mensagem_voz"])

    def test_c_saturacao_sem_aviso_do_firmware(self) -> None:
        """Firmware antigo (sem o aviso): solo cravado em 0,0 por 3 leituras já vira DEFEITO, sem Vermelho."""
        saneador = self.saneador_campo()
        filtro = FiltroMedianaMovel()
        diagnostico = DiagnosticoSensores()
        for indice in range(12):
            solo = 70.0 if indice < 6 else 0.0
            agora = time.monotonic()
            estado = diagnostico.avaliar({"temperatura_ar": 20.0, "umidade_ar": 70.0, "umidade_solo": solo}, agora)
            processar_leitura(20.0, 70.0, solo, "CAMPO_USB", "cafe_arabica", saneador, None,
                              caminho_banco=self.banco, filtro=filtro, estado_sensores=estado)
        gravadas = self.leituras()
        self.assertTrue(all(int(leitura["status_alerta"]) < 2 for leitura in gravadas))
        self.assertEqual([leitura["estado_sensor"] for leitura in gravadas[8:]], ["DEFEITO"] * 4)

    def test_d_solo_travado_vira_suspeito(self) -> None:
        """720 leituras idênticas a cada 5 s (60 min) → solo SUSPEITO; antes disso, OK."""
        diagnostico = DiagnosticoSensores()
        estados = [diagnostico.avaliar({"temperatura_ar": 20.0 + (indice % 7) / 10, "umidade_ar": 70.0,
                                        "umidade_solo": 65.3}, 1000.0 + indice * 5.0) for indice in range(720)]
        self.assertEqual(estados[-1].estado_solo, "SUSPEITO")
        self.assertEqual(estados[-1].estado_geral, "SUSPEITO")
        self.assertEqual(estados[-1].estado_ar, "OK")
        self.assertEqual(estados[700].estado_solo, "OK")
        self.assertIn("solo:travado", estados[-1].descricao)

    def test_d_ar_travado_e_dht_sem_resposta(self) -> None:
        """Ar idêntico por 3 h → SUSPEITO; temperatura ausente por mais de 30 leituras → DEFEITO."""
        diagnostico = DiagnosticoSensores()
        estado = EstadoSensores()
        for indice in range(3 * 720):
            estado = diagnostico.avaliar({"temperatura_ar": 21.0, "umidade_ar": 66.0,
                                          "umidade_solo": 60.0 + (indice % 5) / 10}, indice * 5.0)
        self.assertEqual((estado.estado_ar, estado.estado_solo), ("SUSPEITO", "OK"))
        diagnostico = DiagnosticoSensores()
        estados = [diagnostico.avaliar({"temperatura_ar": None, "umidade_ar": None, "umidade_solo": 60.0 + indice % 3},
                                       float(indice)) for indice in range(32)]
        self.assertEqual(estados[29].estado_ar, "OK")
        self.assertEqual(estados[30].estado_ar, "DEFEITO")

    def test_sensor_suspeito_nunca_gera_vermelho_mas_variavel_boa_gera(self) -> None:
        """Solo suspeito em 20 % (seria Vermelho) → Amarelo; temperatura crítica (sensor bom) → Vermelho."""
        suspeito = EstadoSensores(estado_solo="SUSPEITO", descricao="solo:travado_60min")
        _, amarelo = processar_leitura(20.0, 70.0, 20.0, "CAMPO_USB", "cafe_arabica", SaneadorTempoReal(self.arabica),
                                       None, caminho_banco=self.banco, estado_sensores=suspeito)
        self.assertEqual((amarelo.status_alerta, amarelo.estado_sensor), (1, "SUSPEITO"))
        self.assertEqual(amarelo.mensagem_voz, MENSAGEM_SENSOR_SOLO)
        _, vermelho = processar_leitura(5.0, 70.0, 20.0, "CAMPO_USB", "cafe_arabica", SaneadorTempoReal(self.arabica),
                                        None, caminho_banco=self.banco, estado_sensores=suspeito)
        self.assertEqual(vermelho.status_alerta, 2)
        self.assertIn("frio", vermelho.mensagem_voz)
        self.assertTrue(vermelho.mensagem_voz.endswith(MENSAGEM_SENSOR_SOLO))
        self.assertNotIn("terra está muito seca", vermelho.mensagem_voz)
        ar_quebrado = EstadoSensores(estado_ar="DEFEITO", descricao="ar:sem_leitura_31x")
        _, so_solo = processar_leitura(None, None, 25.0, "CAMPO_USB", "cafe_arabica", SaneadorTempoReal(self.arabica),
                                       None, caminho_banco=self.banco, estado_sensores=ar_quebrado)
        self.assertEqual(so_solo.status_alerta, 2)
        self.assertIn("Regue", so_solo.mensagem_voz)
        self.assertTrue(so_solo.mensagem_voz.endswith(MENSAGEM_SENSOR_AR))
        ambos = EstadoSensores(estado_solo="DEFEITO", estado_ar="DEFEITO")
        _, nada = processar_leitura(5.0, 70.0, 0.0, "CAMPO_USB", "cafe_arabica", SaneadorTempoReal(self.arabica), None,
                                    caminho_banco=self.banco, estado_sensores=ambos)
        self.assertEqual(nada.status_alerta, 1)
        self.assertIn("sensor da terra", nada.mensagem_voz)
        self.assertIn("sensor do ar", nada.mensagem_voz)

    def test_estado_geral_e_ordem(self) -> None:
        """estado_geral é o pior dos dois (OK < SUSPEITO < DEFEITO)."""
        self.assertEqual(EstadoSensores("OK", "OK", "").estado_geral, "OK")
        self.assertEqual(EstadoSensores("SUSPEITO", "OK", "").estado_geral, "SUSPEITO")
        self.assertEqual(EstadoSensores("SUSPEITO", "DEFEITO", "").estado_geral, "DEFEITO")

    def test_e_subida_persistente_e_queda_impossivel(self) -> None:
        """Subida 40 → 75 % vira patamar em 3 leituras; queda 70 → 20 % em segundos fica em hold-last."""
        saneador = SaneadorTempoReal(self.arabica, plausibilidade_fisica=True)
        saneador.sanear(20.0, 70.0, 40.0, instante_monotonic=0.0)
        self.assertEqual(saneador.sanear(20.0, 70.0, 75.0, instante_monotonic=5.0)[2], "umidade_solo:pico")
        self.assertEqual(saneador.sanear(20.0, 70.0, 76.0, instante_monotonic=10.0)[2], "umidade_solo:pico")
        valores, flag, _ = saneador.sanear(20.0, 70.0, 77.0, instante_monotonic=15.0)
        self.assertEqual((valores["umidade_solo"], flag), (77.0, 0))

        fisico = SaneadorTempoReal(self.arabica, plausibilidade_fisica=True, limite_hold_last=3)
        fisico.sanear(20.0, 70.0, 70.0, instante_monotonic=0.0)
        for indice in range(1, 6):
            valores, _, descricao = fisico.sanear(20.0, 70.0, 20.0, instante_monotonic=indice * 5.0)
            self.assertEqual((valores["umidade_solo"], descricao), (70.0, "umidade_solo:pico"))
        self.assertEqual(fisico.variaveis_hold_last_excedido(), ["umidade_solo"])
        # Comportamento padrão (v1) preservado: sem plausibilidade, 3 leituras aceitam a queda.
        padrao = SaneadorTempoReal(self.arabica)
        padrao.sanear(20.0, 70.0, 70.0)
        resultados = [padrao.sanear(20.0, 70.0, 20.0)[0]["umidade_solo"] for _ in range(3)]
        self.assertEqual(resultados, [70.0, 70.0, 20.0])
        # Queda real e lenta: 20 pp depois de 1 h sem leitura cabe em 15 + 8 × 1 = 23 pp.
        lenta = SaneadorTempoReal(self.arabica, plausibilidade_fisica=True)
        lenta.sanear(20.0, 70.0, 70.0, instante_monotonic=0.0)
        aceitos = [lenta.sanear(20.0, 70.0, 50.0, instante_monotonic=3600.0 + indice)[0]["umidade_solo"]
                   for indice in range(3)]
        self.assertEqual(aceitos, [70.0, 70.0, 50.0])

    def test_f_migracao_de_banco_v1_idempotente(self) -> None:
        """migrar_esquema 2× num banco da v1: sem erro, linhas antigas com OK, '' e instante_utc preenchido."""
        caminho = self.pasta / "banco_v1.db"
        conexao = sqlite3.connect(caminho)
        try:
            conexao.executescript(DDL_BASELINE_V1)
            conexao.execute("INSERT INTO culturas (codigo, nome_comum, nome_cientifico, categoria) "
                            "VALUES ('cafe_arabica', 'Café Arábica', 'Coffea arabica', 'cafe')")
            for hora in range(5):
                conexao.execute(
                    f"INSERT INTO historico_leituras ({_COLUNAS_SQL_LEITURA}) VALUES "
                    "(?, 1, 'CAMPO_USB', 20, 70, 70, 20, 70, 70, 0, 0, 0, 0, 0, 1.0, 0, '', 'Tudo bem.', 'REGRAS')",
                    (f"2026-01-01 0{hora}:30:00",),
                )
            conexao.commit()
        finally:
            conexao.close()
        conexao = database.obter_conexao(caminho)
        try:
            migrar_esquema(conexao)
            migrar_esquema(conexao)
            colunas = {linha[1] for linha in conexao.execute("PRAGMA table_info(historico_leituras)")}
            linhas = [dict(linha) for linha in conexao.execute("SELECT * FROM historico_leituras ORDER BY id_leitura")]
            tabelas = {linha[0] for linha in conexao.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            indices = {linha[0] for linha in conexao.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        finally:
            conexao.close()
        self.assertTrue({"estado_sensor", "diagnostico_sensor", "instante_utc"} <= colunas)
        self.assertIn("historico_agregado_hora", tabelas)
        self.assertIn("idx_historico_cultura_id", indices)
        self.assertEqual(len(linhas), 5)
        for linha in linhas:
            self.assertEqual((linha["estado_sensor"], linha["diagnostico_sensor"]), ("OK", ""))
            esperado = datetime.strptime(linha["data_hora"], FORMATO).astimezone().astimezone(timezone.utc)
            self.assertEqual(linha["instante_utc"], esperado.isoformat(timespec="seconds"))
            self.assertTrue(linha["instante_utc"].endswith("+00:00"))
        # O banco migrado funciona com a API nova (inicialização completa + busca com as colunas novas).
        inicializar_banco(caminho)
        ultima = buscar_ultima_leitura(caminho_banco=caminho)
        assert ultima is not None
        self.assertEqual((ultima["id_leitura"], ultima["estado_sensor"]), (5, "OK"))

    def test_f_migracao_concorrente(self) -> None:
        """Coletor e servidor migrando o mesmo banco v1 ao mesmo tempo: nenhum erro, colunas criadas uma vez."""
        caminho = self.pasta / "banco_v1_concorrente.db"
        conexao = sqlite3.connect(caminho)
        try:
            conexao.executescript(DDL_BASELINE_V1)
        finally:
            conexao.close()
        erros: List[BaseException] = []
        largada = threading.Barrier(6)

        def migrar() -> None:
            """Uma conexão própria migrando o banco."""
            conexao_thread = database.obter_conexao(caminho)
            try:
                largada.wait(timeout=10)
                migrar_esquema(conexao_thread)
            except BaseException as erro:  # noqa: BLE001 — o teste quer ver qualquer falha
                erros.append(erro)
            finally:
                conexao_thread.close()
        tarefas = [threading.Thread(target=migrar) for _ in range(6)]
        for tarefa in tarefas:
            tarefa.start()
        for tarefa in tarefas:
            tarefa.join(timeout=30)
        self.assertEqual(erros, [])
        conexao = sqlite3.connect(caminho)
        try:
            colunas = [linha[1] for linha in conexao.execute("PRAGMA table_info(historico_leituras)")]
        finally:
            conexao.close()
        self.assertEqual(colunas.count("estado_sensor"), 1)
        self.assertEqual(colunas[-3:], ["estado_sensor", "diagnostico_sensor", "instante_utc"])

    def test_registro_leitura_compativel(self) -> None:
        """Construtor antigo de RegistroLeitura (19 campos) continua válido; os novos têm padrão."""
        antigo = registro_exemplo()
        self.assertEqual((antigo.estado_sensor, antigo.diagnostico_sensor, antigo.instante_utc), ("OK", "", None))
        id_leitura = inserir_leitura(antigo, caminho_banco=self.banco)
        gravada = buscar_ultima_leitura(caminho_banco=self.banco)
        assert gravada is not None
        self.assertEqual(gravada["id_leitura"], id_leitura)
        self.assertTrue(str(gravada["instante_utc"]).endswith("+00:00"))
        with self.assertRaises(ValueError):
            inserir_leitura(registro_exemplo(estado_sensor="QUEBRADO"), caminho_banco=self.banco)


# ---------------------------------------------------------------------------
# 2. Protocolo serial v2, saneador semeado e firmware
# ---------------------------------------------------------------------------
class TesteProtocoloV2(BaseBanco):
    """Checksum, compatibilidade com v1, contadores e saneador semeado."""

    def test_a_checksum_e_compatibilidade(self) -> None:
        """v2 com checksum certo é aceita; errado → None e contado; v1 continua aceita."""
        conteudo = "2,21.0,65.0,72.4"
        self.assertEqual(interpretar_linha_serial("$2,21.0,65.0,72.4*" + calcular_checksum_nmea(conteudo)),
                         (21.0, 65.0, 72.4))
        contadores = ContadoresProtocolo()
        errado = "FF" if calcular_checksum_nmea(conteudo) != "FF" else "00"
        self.assertIsNone(interpretar_linha_serial(f"${conteudo}*{errado}", contadores=contadores))
        self.assertEqual(contadores.checksum_invalido, 1)
        self.assertEqual(interpretar_linha_serial("21.0,65.0,72.4", contadores=contadores), (21.0, 65.0, 72.4))
        self.assertEqual((contadores.linhas_v1, contadores.linhas_v2), (1, 0))
        self.assertEqual(interpretar_linha_serial(linha_v2("nan", "nan", "70.0"), contadores=contadores),
                         (None, None, 70.0))
        self.assertEqual(contadores.linhas_v2, 1)
        # Ruído trocando um dígito ("21.0" → "71.0") é pego pelo checksum.
        self.assertIsNone(interpretar_linha_serial("$2,71.0,65.0,72.4*" + calcular_checksum_nmea(conteudo)))
        self.assertIsNone(interpretar_linha_serial("$2,21.0,65.0,72.4"))  # sem checksum
        self.assertIsNone(interpretar_linha_serial(linha_v2("21.0", "65.0", "x")))
        minusculo = "$" + conteudo + "*" + calcular_checksum_nmea(conteudo).lower()
        self.assertEqual(interpretar_linha_serial(minusculo), (21.0, 65.0, 72.4))
        detalhada = interpretar_linha_serial_detalhada("#AGROANALISE_PRONTO;PROTOCOLO=2")
        self.assertEqual((detalhada.tipo, detalhada.codigo_diagnostico), ("DIAGNOSTICO", "AGROANALISE_PRONTO;PROTOCOLO=2"))
        self.assertEqual(calcular_checksum_nmea("2,21.0,65.0,72.3"), "06")

    def test_c_saneador_semeado_marca_pico(self) -> None:
        """Saneador semeado com 21 °C: '1.0,65.0,70.2' recebe 'temperatura_ar:pico' e não vira Vermelho."""
        saneador = self.saneador_campo()
        saneador.semear({"temperatura_ar": 21.0, "umidade_ar": 65.0, "umidade_solo": 70.0})
        valores = interpretar_linha_serial("1.0,65.0,70.2")
        assert valores is not None
        _, registro_pico = processar_leitura(*valores, "CAMPO_USB", "cafe_arabica", saneador, None,
                                             caminho_banco=self.banco)
        self.assertIn("temperatura_ar:pico", registro_pico.descricao_anomalia)
        self.assertEqual((registro_pico.temperatura_ar, registro_pico.status_alerta), (21.0, 0))

    def test_buscar_ultima_leitura_origem_respeita_idade(self) -> None:
        """Semente só de leitura CAMPO_USB recente (< 10 min) da mesma cultura."""
        inserir_leitura(registro_exemplo(temperatura_ar=22.0,
                                         data_hora=(datetime.now() - timedelta(minutes=30)).strftime(FORMATO)),
                        caminho_banco=self.banco)
        self.assertIsNone(buscar_ultima_leitura_origem("cafe_arabica", "CAMPO_USB", 600, caminho_banco=self.banco))
        inserir_leitura(registro_exemplo(temperatura_ar=23.0), caminho_banco=self.banco)
        inserir_leitura(registro_exemplo(temperatura_ar=30.0, origem="SIMULACAO"), caminho_banco=self.banco)
        recente = buscar_ultima_leitura_origem("cafe_arabica", "CAMPO_USB", 600, caminho_banco=self.banco)
        assert recente is not None
        self.assertEqual(recente["temperatura_ar"], 23.0)
        self.assertIsNone(buscar_ultima_leitura_origem("cafe_conilon", "CAMPO_USB", 600, caminho_banco=self.banco))

    def test_coletor_descarta_v1_quando_exigido_e_banner(self) -> None:
        """exigir_protocolo_v2 descarta v1; banner de reinício faz descartar a próxima linha de dados."""
        coletor = self.coletor(1)
        coletor._config = {"exigir_protocolo_v2": True}
        self.assertFalse(coletor._ao_receber_linha(interpretar_linha_serial_detalhada("21.0,65.0,70.0")))
        self.assertTrue(coletor._ao_receber_linha(interpretar_linha_serial_detalhada(linha_v2("21.0", "65.0", "70.0"))))
        coletor._ao_receber_linha(interpretar_linha_serial_detalhada("#AGROANALISE_PRONTO;PROTOCOLO=2"))
        self.assertFalse(coletor._ao_receber_linha(interpretar_linha_serial_detalhada(linha_v2("1.0", "65.0", "70.0"))))
        self.assertTrue(coletor._ao_receber_linha(interpretar_linha_serial_detalhada(linha_v2("21.0", "65.0", "70.0"))))
        self.assertEqual((coletor.contadores.descartadas, coletor.contadores.linhas_v1, coletor.contadores.linhas_v2),
                         (2, 1, 3))


@unittest.skipUnless(shutil.which("g++"), "g++ não instalado")
class TesteFirmwareCompila(unittest.TestCase):
    """O firmware compila (sintaxe C++) com stubs mínimos e fala o protocolo 2 que o coletor entende."""

    def _compilar(self, *extras: str) -> subprocess.CompletedProcess:
        """Roda g++ -fsyntax-only no firmware."""
        return subprocess.run(["g++", "-fsyntax-only", "-Wall", "-Wextra", *extras, "-x", "c++",
                               "-I", str(RAIZ_PROJETO / "tests" / "stubs_arduino"),
                               str(RAIZ_PROJETO / "arduino" / "coleta_sensores.ino")],
                              capture_output=True, text=True, timeout=120, check=False)

    def test_e_sintaxe_com_e_sem_avr(self) -> None:
        """g++ -fsyntax-only termina com código 0, sem e com -D__AVR__ (watchdog)."""
        sem_avr = self._compilar()
        self.assertEqual(sem_avr.returncode, 0, sem_avr.stderr)
        com_avr = self._compilar("-D__AVR__")
        self.assertEqual(com_avr.returncode, 0, com_avr.stderr)

    def test_firmware_no_host_fala_protocolo_2(self) -> None:
        """Firmware executado no computador: banner v2, linha com checksum válido e 'nan' quando o DHT falha."""
        with tempfile.TemporaryDirectory(prefix="agro_fw_") as pasta:
            executavel = Path(pasta) / "firmware_host"
            compilacao = subprocess.run(
                ["g++", "-I", str(RAIZ_PROJETO / "tests" / "stubs_arduino"), "-I", str(RAIZ_PROJETO / "arduino"),
                 "-o", str(executavel), str(RAIZ_PROJETO / "tests" / "stubs_arduino" / "execucao_host.cpp")],
                capture_output=True, text=True, timeout=180, check=False)
            self.assertEqual(compilacao.returncode, 0, compilacao.stderr)
            saida = subprocess.run([str(executavel)], capture_output=True, text=True, timeout=30, check=True).stdout
        linhas = [linha.strip() for linha in saida.splitlines() if linha.strip()]
        self.assertEqual(linhas[0], "#AGROANALISE_PRONTO;PROTOCOLO=2")
        dados = [interpretar_linha_serial_detalhada(linha) for linha in linhas if linha.startswith("$")]
        self.assertGreaterEqual(len(dados), 2)
        self.assertTrue(all(linha.tipo == "DADOS" and linha.checksum_valido for linha in dados))
        self.assertEqual(dados[0].valores[:2], (21.0, 65.0))
        self.assertEqual(dados[-1].valores[:2], (None, None))
        self.assertIsNotNone(dados[-1].valores[2])
        self.assertIn("#ERRO:DHT11_LEITURA_INVALIDA", linhas)


@unittest.skipUnless(sys.platform.startswith("linux") and hasattr(os, "openpty"), "emulador pty requer Linux")
class TesteSerialPty(BaseBanco):
    """Caminho serial real (pyserial) com o Arduino emulado em pseudo-terminal."""

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

    def tearDown(self) -> None:
        """Restaura a constante e fecha os descritores."""
        coletor_serial.ESPERA_RESET_ARDUINO_SEGUNDOS = self._espera_original
        self._parar_envio.set()
        for descritor in (self.mestre, self.escravo):
            with contextlib.suppress(OSError):
                os.close(descritor)
        super().tearDown()

    def _escrever(self, linha: str) -> bool:
        """Escreve uma linha no lado do 'Arduino'; ``False`` se o pty já foi fechado."""
        try:
            os.write(self.mestre, (linha + "\r\n").encode("ascii"))
            return True
        except OSError:
            return False

    def _firmware(self, coletor: ColetorAgroAnalise, primeira: Optional[str],
                  linhas: Callable[[int], List[str]], periodo: float = 0.05) -> threading.Thread:
        """Firmware emulado: espera a porta abrir, manda ``primeira`` e depois ``linhas(n)`` em ciclo."""
        def enviar() -> None:
            """Laço de envio."""
            prazo = time.monotonic() + 15
            while not coletor.conectado and time.monotonic() < prazo and not self._parar_envio.is_set():
                time.sleep(0.01)
            if primeira is not None and not self._escrever(primeira):
                return
            passo = 0
            while not self._parar_envio.is_set():
                for linha in linhas(passo):
                    if not self._escrever(linha):
                        return
                passo += 1
                self._parar_envio.wait(periodo)
        tarefa = threading.Thread(target=enviar, daemon=True)
        tarefa.start()
        return tarefa

    def _executar(self, coletor: ColetorAgroAnalise) -> None:
        """Roda o coletor até o fim e para o firmware emulado."""
        execucao = threading.Thread(target=coletor.executar, daemon=True)
        execucao.start()
        execucao.join(timeout=90)
        self._parar_envio.set()
        self.assertFalse(execucao.is_alive(), "o coletor não terminou")

    def test_b_primeira_linha_cortada_e_descartada(self) -> None:
        """Fragmento '1.0,65.0,70.2' logo após conectar: não é gravado e não gera Vermelho."""
        coletor = self.coletor(8, porta=self.nome_porta)
        self._firmware(coletor, "1.0,65.0,70.2", lambda _: [linha_v2("21.0", "65.0", "70.2")])
        self._executar(coletor)
        gravadas = self.leituras()
        campo = [leitura for leitura in gravadas if leitura["origem"] == "CAMPO_USB"]
        self.assertGreaterEqual(len(campo), 5)
        self.assertFalse(any(leitura["temperatura_ar"] == 1.0 or leitura["temperatura_ar_bruta"] == 1.0
                             for leitura in gravadas))
        self.assertFalse(any(int(leitura["status_alerta"]) == 2 for leitura in gravadas))
        self.assertGreaterEqual(coletor.contadores.descartadas, 1)
        self.assertGreaterEqual(coletor.contadores.linhas_v2, 5)
        self.assertGreaterEqual(int(obter_configuracao("serial_linhas_descartadas", caminho_banco=self.banco)), 1)

    def test_d_dht_sem_resposta_continua_em_campo(self) -> None:
        """'nan,nan,70.0' por 40 ciclos: segue CAMPO_USB, flag 1 e, depois de 30, Amarelo com 'sensor do ar'."""
        coletor = self.coletor(40, porta=self.nome_porta)
        modos: List[str] = []
        original = coletor._ciclo_campo

        def ciclo_espiado() -> bool:
            """Registra o modo a cada ciclo de campo."""
            resultado = original()
            modos.append(coletor.modo_atual)
            return resultado
        coletor._ciclo_campo = ciclo_espiado  # type: ignore[method-assign]
        self._firmware(coletor, None, lambda _: ["#ERRO:DHT11_LEITURA_INVALIDA", "nan,nan,70.0"])
        self._executar(coletor)
        gravadas = self.leituras()
        self.assertEqual({leitura["origem"] for leitura in gravadas}, {"CAMPO_USB"})
        self.assertGreaterEqual(len(gravadas), 38)
        self.assertEqual(set(modos), {"CAMPO_USB"})
        self.assertTrue(all(int(leitura["flag_anomalia"]) == 1 for leitura in gravadas))
        for leitura in gravadas[30:]:
            self.assertGreaterEqual(int(leitura["status_alerta"]), 1)
            self.assertIn("sensor do ar", leitura["mensagem_voz"])
            self.assertEqual(leitura["estado_sensor"], "DEFEITO")
        self.assertTrue(all(leitura["estado_sensor"] == "OK" for leitura in gravadas[:29]))

    def test_c_higrometro_solto_pela_serial(self) -> None:
        """Cadeia completa: espeto solto (0,0 % + aviso do firmware) nunca vira 'Urgente, regue'."""
        coletor = self.coletor(30, porta=self.nome_porta)

        def linhas(passo: int) -> List[str]:
            """Primeiro solo normal; depois espeto fora da terra."""
            if passo < 12:
                return [linha_v2("20.0", "70.0", "70.0")]
            return ["#AVISO:HIGROMETRO_DESCONECTADO", linha_v2("20.0", "70.0", "0.0")]
        self._firmware(coletor, None, linhas, periodo=0.1)
        self._executar(coletor)
        gravadas = [leitura for leitura in self.leituras() if leitura["origem"] == "CAMPO_USB"]
        self.assertGreaterEqual(len(gravadas), 20)
        self.assertFalse(any(int(leitura["status_alerta"]) == 2 for leitura in gravadas))
        self.assertFalse(any("Urgente" in leitura["mensagem_voz"] for leitura in gravadas))
        self.assertEqual(gravadas[-1]["estado_sensor"], "DEFEITO")
        self.assertIn("sensor da terra", gravadas[-1]["mensagem_voz"])


# ---------------------------------------------------------------------------
# 3. Gravação sem perda, batimento e relógio
# ---------------------------------------------------------------------------
class TesteGravacaoSemPerda(BaseBanco):
    """Fila com o banco travado, ordem por gravação, batimento, disco e relógio."""

    def test_a_banco_travado_nao_perde_leitura(self) -> None:
        """BEGIN IMMEDIATE externo por ~3 s: depois do COMMIT, linhas novas = ciclos, com a data/hora original."""
        espera_original = database.TEMPO_ESPERA_TRAVA_MS
        database.TEMPO_ESPERA_TRAVA_MS = 100
        self.addCleanup(setattr, database, "TEMPO_ESPERA_TRAVA_MS", espera_original)
        coletor = self.coletor(30, forcar_simulacao=True)
        retidas: List[str] = []
        instante_commit: List[datetime] = []

        def travar() -> None:
            """Segura a trava de escrita do banco por ~3 s."""
            prazo = time.monotonic() + 20
            while coletor.leituras_gravadas < 2 and time.monotonic() < prazo:
                time.sleep(0.02)
            conexao = sqlite3.connect(self.banco, timeout=5, isolation_level=None)
            try:
                conexao.execute("BEGIN IMMEDIATE")
                time.sleep(3.0)
                retidas.extend(registro_fila.data_hora for registro_fila in list(coletor.fila_pendente))
                instante_commit.append(datetime.now())
                conexao.execute("COMMIT")
            finally:
                conexao.close()
        trava = threading.Thread(target=travar, daemon=True)
        trava.start()
        coletor.executar()
        trava.join(timeout=30)
        gravadas = self.leituras()
        self.assertEqual(len(gravadas), coletor.iteracoes_executadas)
        self.assertEqual(coletor.iteracoes_executadas, 30)
        self.assertEqual(len(coletor.fila_pendente), 0)
        self.assertGreaterEqual(len(retidas), 5, "o banco travado deveria ter enchido a fila")
        datas_gravadas = [leitura["data_hora"] for leitura in gravadas]
        for data_hora in retidas:
            self.assertIn(data_hora, datas_gravadas)
        self.assertLess(datetime.strptime(min(retidas), FORMATO), instante_commit[0] - timedelta(seconds=1.5))
        self.assertEqual(datas_gravadas, sorted(datas_gravadas))

    def test_b_relogio_adiantado_nao_prende_leitura_velha(self) -> None:
        """Vermelha com hora 3 h à frente + 5 verdes com a hora certa: a última é a última GRAVADA."""
        adiantada = (datetime.now() + timedelta(hours=3)).strftime(FORMATO)
        inserir_leitura(registro_exemplo(data_hora=adiantada, status_regra=2, status_predito=2, status_alerta=2,
                                         mensagem_voz="Perigo."), caminho_banco=self.banco)
        ids = [inserir_leitura(registro_exemplo(), caminho_banco=self.banco) for _ in range(5)]
        ultima = buscar_ultima_leitura(caminho_banco=self.banco)
        assert ultima is not None
        self.assertEqual((ultima["id_leitura"], ultima["status_alerta"]), (ids[-1], 0))
        self.assertEqual([leitura["id_leitura"] for leitura in buscar_historico(limite=3, caminho_banco=self.banco)],
                         list(reversed(ids))[:3])

    def test_c_batimento_do_coletor(self) -> None:
        """Depois de 1 ciclo: coletor_batimento_em (UTC +00:00), coletor_pid e disco_livre_mb gravados."""
        self.coletor(1, forcar_simulacao=True).executar()
        configuracoes = obter_todas_configuracoes(caminho_banco=self.banco)
        self.assertTrue(str(configuracoes["coletor_batimento_em"]).endswith("+00:00"))
        batimento = datetime.fromisoformat(str(configuracoes["coletor_batimento_em"]))
        self.assertLess(abs((datetime.now(timezone.utc) - batimento).total_seconds()), 30)
        self.assertEqual(configuracoes["coletor_pid"], os.getpid())
        self.assertIsInstance(configuracoes["disco_livre_mb"], float)
        self.assertGreater(configuracoes["disco_livre_mb"], 0.0)
        self.assertEqual(configuracoes["coletor_fila_pendente"], 0)
        for chave in ("serial_linhas_v1", "serial_linhas_v2", "serial_checksum_invalido", "serial_linhas_descartadas",
                      "espaco_minimo_mb", "retencao_dias_brutos", "janela_mediana_leituras"):
            self.assertIn(chave, configuracoes)
        ultima = buscar_ultima_leitura(caminho_banco=self.banco)
        assert ultima is not None
        self.assertTrue(str(ultima["instante_utc"]).endswith("+00:00"))

    def test_d_disco_cheio_aciona_retencao_de_emergencia(self) -> None:
        """disk_usage abaixo do mínimo: log ERROR e aplicar_retencao(dias_brutos=7)."""
        Uso = namedtuple("Uso", "total used free")
        with mock.patch.object(coletor_serial.shutil, "disk_usage", return_value=Uso(10**9, 10**9 - 50 * 2**20,
                                                                                     50 * 2**20)), \
                mock.patch.object(coletor_serial, "aplicar_retencao",
                                  return_value={"agregadas": 0, "apagadas": 0, "demonstracao_apagadas": 0}) as retencao, \
                self.assertLogs("agroanalise.coletor_serial", level="ERROR") as logs:
            self.coletor(1, forcar_simulacao=True).executar()
        retencao.assert_called_once()
        self.assertEqual(retencao.call_args.kwargs["dias_brutos"], 7)
        self.assertTrue(any("Disco quase cheio" in linha for linha in logs.output))
        self.assertAlmostEqual(obter_todas_configuracoes(caminho_banco=self.banco)["disco_livre_mb"], 50.0, places=1)

    def test_salto_de_relogio_detectado(self) -> None:
        """Δ relógio de parede − Δ monotônico > 60 s → WARNING e relogio_ultimo_salto_em gravado no batimento."""
        coletor = self.coletor(1, forcar_simulacao=True)
        coletor._referencia_relogio = (time.time() - 3600.0, time.monotonic() - 5.0)
        logging.disable(logging.NOTSET)
        try:
            with self.assertLogs("agroanalise.coletor_serial", level="WARNING") as logs:
                coletor._verificar_relogio()
        finally:
            logging.disable(logging.WARNING)
        self.assertTrue(any("relógio" in linha for linha in logs.output))
        self.assertTrue(str(coletor._relogio_ultimo_salto_em).endswith("+00:00"))
        self.assertIn("relogio_ultimo_salto_em", coletor._configuracoes_batimento())
        coletor._verificar_relogio()  # sem novo salto: nada muda
        marcado = coletor._relogio_ultimo_salto_em
        coletor._verificar_relogio()
        self.assertEqual(coletor._relogio_ultimo_salto_em, marcado)

    def test_lote_e_configuracoes_na_mesma_transacao(self) -> None:
        """Lote grava tudo numa transação; registro inválido no lote não grava nada (nem as configurações)."""
        ids = inserir_leituras_em_lote([registro_exemplo(), registro_exemplo(status_alerta=1, status_regra=1)],
                                       caminho_banco=self.banco, configuracoes_extras={"coletor_pid": 123})
        self.assertEqual(ids, [ids[0], ids[0] + 1])
        self.assertEqual(obter_todas_configuracoes(caminho_banco=self.banco)["coletor_pid"], 123)
        with self.assertRaises(ValueError):
            inserir_leituras_em_lote([registro_exemplo(), registro_exemplo(status_alerta=7)],
                                     caminho_banco=self.banco, configuracoes_extras={"coletor_pid": 999})
        self.assertEqual(len(self.leituras()), 2)
        self.assertEqual(obter_todas_configuracoes(caminho_banco=self.banco)["coletor_pid"], 123)
        inserir_leitura(registro_exemplo(), caminho_banco=self.banco,
                        configuracoes_extras={"coletor_ultimo_erro": "teste", "chave_nova_teste": 2.5})
        configuracoes = obter_todas_configuracoes(caminho_banco=self.banco)
        self.assertEqual((configuracoes["coletor_ultimo_erro"], configuracoes["chave_nova_teste"]), ("teste", 2.5))


class TesteGanchoAntecipacao(BaseBanco):
    """Gancho do módulo 'prognostico' (antecipação), testado com módulos falsos."""

    def _modulo(self, status_novo: int, falhar: bool = False) -> types.ModuleType:
        """Módulo falso com aplicar_antecipacao/gravar_analise_antecipacao."""
        modulo = types.ModuleType("prognostico_falso")
        modulo.gravadas = []  # type: ignore[attr-defined]

        def aplicar_antecipacao(registro_atual: RegistroLeitura, parametros: ParametrosCultura,
                                caminho_banco: Path) -> Tuple[RegistroLeitura, Dict[str, Any]]:
            """Eleva o status e troca a mensagem (ou falha)."""
            if falhar:
                raise RuntimeError("falha simulada")
            return (replace(registro_atual, status_alerta=status_novo, status_predito=status_novo,
                            mensagem_voz="Atenção: a terra vai secar em 6 horas.", umidade_solo=1.0),
                    {"motivo_principal": "SECA_CHEGANDO"})

        def gravar_analise_antecipacao(id_leitura: int, resultado: Any, caminho_banco: Path) -> None:
            """Guarda a chamada."""
            modulo.gravadas.append((id_leitura, resultado))  # type: ignore[attr-defined]
        modulo.aplicar_antecipacao = aplicar_antecipacao  # type: ignore[attr-defined]
        modulo.gravar_analise_antecipacao = gravar_analise_antecipacao  # type: ignore[attr-defined]
        return modulo

    def _processar(self, modulo: types.ModuleType, origem: str = "CAMPO_USB",
                   estado: Optional[EstadoSensores] = None) -> Tuple[int, RegistroLeitura]:
        """Leitura Verde pelo pipeline com o módulo de antecipação."""
        return processar_leitura(20.0, 70.0, 70.0, origem, "cafe_arabica", SaneadorTempoReal(self.arabica), None,
                                 caminho_banco=self.banco, modulo_antecipacao=modulo, estado_sensores=estado)

    def test_antecipacao_acende_amarelo_e_grava_analise(self) -> None:
        """Verde → Amarelo aceito; só status e voz são copiados; análise gravada com o id da leitura."""
        modulo = self._modulo(1)
        id_leitura, gravado = self._processar(modulo)
        self.assertEqual((gravado.status_alerta, gravado.umidade_solo), (1, 70.0))
        self.assertIn("vai secar", gravado.mensagem_voz)
        self.assertEqual(modulo.gravadas, [(id_leitura, {"motivo_principal": "SECA_CHEGANDO"})])  # type: ignore[attr-defined]

    def test_antecipacao_nunca_leva_a_vermelho_nem_quebra(self) -> None:
        """Verde → Vermelho é recusado; exceção do módulo vira aviso e a leitura é gravada mesmo assim."""
        modulo = self._modulo(2)
        _, gravado = self._processar(modulo)
        self.assertEqual(gravado.status_alerta, 0)
        self.assertEqual(modulo.gravadas, [])  # type: ignore[attr-defined]
        _, gravado = self._processar(self._modulo(1, falhar=True))
        self.assertEqual(gravado.status_alerta, 0)
        self.assertEqual(len(self.leituras()), 2)

    def test_antecipacao_so_em_campo_com_sensor_ok(self) -> None:
        """SIMULACAO ou sensor SUSPEITO: o módulo nem é chamado."""
        modulo = self._modulo(1)
        _, simulada = self._processar(modulo, origem="SIMULACAO")
        self.assertEqual(simulada.status_alerta, 0)
        _, suspeita = self._processar(modulo, estado=EstadoSensores(estado_solo="SUSPEITO"))
        self.assertEqual(suspeita.mensagem_voz, MENSAGEM_SENSOR_SOLO)
        self.assertEqual(modulo.gravadas, [])  # type: ignore[attr-defined]

    def test_carregar_modulo_ausente_ou_incompleto(self) -> None:
        """Módulo inexistente ou sem as funções → None (o coletor segue só com regra + IA)."""
        self.assertIsNone(carregar_modulo_antecipacao("modulo_que_nao_existe_agro"))
        incompleto = types.ModuleType("prognostico_incompleto_agro")
        with mock.patch.dict(sys.modules, {"prognostico_incompleto_agro": incompleto}):
            self.assertIsNone(carregar_modulo_antecipacao("prognostico_incompleto_agro"))
        completo = self._modulo(1)
        with mock.patch.dict(sys.modules, {"prognostico_completo_agro": completo}):
            self.assertIs(carregar_modulo_antecipacao("prognostico_completo_agro"), completo)


# ---------------------------------------------------------------------------
# 4. Retenção, agregação horária, backup e linha de comando
# ---------------------------------------------------------------------------
class TesteRetencaoBackup(BaseBanco):
    """Retenção com agregação horária exata, backup online e CLI."""

    AGORA: datetime = datetime(2026, 6, 15, 0, 0, 0)
    DIAS_SINTETICOS: int = 40
    DIAS_RETENCAO: int = 30

    def _povoar(self) -> None:
        """40 dias a 60 s em CAMPO_USB e SIMULACAO (57.600 linhas cada) + demonstrações antigas e recentes."""
        inicio = self.AGORA - timedelta(days=self.DIAS_SINTETICOS)
        minutos = self.DIAS_SINTETICOS * 24 * 60
        linhas: List[Tuple[Any, ...]] = []
        for origem_indice, origem in enumerate(("CAMPO_USB", "SIMULACAO")):
            for minuto in range(minutos):
                instante = inicio + timedelta(minutes=minuto)
                status = (minuto // 7 + origem_indice) % 3 if minuto % 11 else 0
                linhas.append((instante.strftime(FORMATO), 1, origem, 20.0, 70.0, 70.0,
                               18.0 + (minuto % 50) / 10.0, 60.0 + (minuto % 13), 55.0 + (minuto % 29), 0.0, 0.0,
                               status, status, status, 1.0, 1 if minuto % 17 == 0 else 0, "", "Mensagem.", "REGRAS"))
        for horas_atras in (72, 50, 30, 2):
            linhas.append(((self.AGORA - timedelta(hours=horas_atras)).strftime(FORMATO), 1, "DEMONSTRACAO",
                           5.0, 70.0, 20.0, 5.0, 70.0, 20.0, 0.0, 0.0, 2, 2, 2, 1.0, 0, "", "Demo.", "REGRAS"))
        conexao = database.obter_conexao(self.banco)
        try:
            conexao.executemany(f"INSERT INTO historico_leituras ({_COLUNAS_SQL_LEITURA}) VALUES "
                                f"({', '.join(['?'] * 19)})", linhas)
            conexao.commit()
        finally:
            conexao.close()

    def _consultar(self, sql: str, parametros: Tuple[Any, ...] = ()) -> List[sqlite3.Row]:
        """Executa uma consulta no banco temporário."""
        conexao = database.obter_conexao(self.banco)
        try:
            return conexao.execute(sql, parametros).fetchall()
        finally:
            conexao.close()

    def test_retencao_agrega_por_hora_e_apaga(self) -> None:
        """Brutas fora do corte viram 24 linhas/dia por cultura e origem; SUM(n) e status máximos exatos."""
        self._povoar()
        corte = (self.AGORA - timedelta(days=self.DIAS_RETENCAO)).strftime(FORMATO)
        esperado = {(linha["hora"], linha["origem"]): (linha["n"], linha["maximo"], linha["tmin"], linha["tmax"])
                    for linha in self._consultar(
                        "SELECT strftime('%Y-%m-%d %H:00:00', data_hora) AS hora, origem, COUNT(*) AS n, "
                        "MAX(status_alerta) AS maximo, MIN(temperatura_ar) AS tmin, MAX(temperatura_ar) AS tmax "
                        "FROM historico_leituras WHERE data_hora < ? AND origem <> 'DEMONSTRACAO' "
                        "GROUP BY hora, origem", (corte,))}
        brutas_antes = sum(valor[0] for valor in esperado.values())
        self.assertEqual(brutas_antes, 2 * (self.DIAS_SINTETICOS - self.DIAS_RETENCAO) * 1440)
        # Filha com ON DELETE CASCADE (como a análise de antecipação): some junto com a leitura.
        conexao = database.obter_conexao(self.banco)
        try:
            conexao.execute("CREATE TABLE analise_teste (id_leitura INTEGER PRIMARY KEY "
                            "REFERENCES historico_leituras(id_leitura) ON DELETE CASCADE)")
            conexao.execute("INSERT INTO analise_teste SELECT id_leitura FROM historico_leituras "
                            "WHERE id_leitura % 1000 = 0")
            conexao.commit()
        finally:
            conexao.close()

        resultado = aplicar_retencao(self.DIAS_RETENCAO, caminho_banco=self.banco, agora=self.AGORA)
        self.assertEqual(resultado["agregadas"], brutas_antes)
        self.assertEqual(resultado["demonstracao_apagadas"], 3)
        self.assertEqual(resultado["apagadas"], brutas_antes + 3)
        restantes = self._consultar("SELECT MIN(data_hora) AS minimo, COUNT(*) AS n FROM historico_leituras "
                                    "WHERE origem <> 'DEMONSTRACAO'")[0]
        self.assertGreaterEqual(restantes["minimo"], corte)
        self.assertEqual(restantes["n"], 2 * self.DIAS_RETENCAO * 1440)
        demonstracoes = self._consultar("SELECT data_hora FROM historico_leituras WHERE origem = 'DEMONSTRACAO'")
        self.assertEqual(len(demonstracoes), 1)

        agregado = {(linha["hora"], linha["origem"]): linha for linha in self._consultar(
            "SELECT * FROM historico_agregado_hora")}
        dias_apagados = self.DIAS_SINTETICOS - self.DIAS_RETENCAO
        for origem in ("CAMPO_USB", "SIMULACAO"):
            self.assertEqual(sum(1 for chave in agregado if chave[1] == origem), 24 * dias_apagados)
        self.assertEqual(sum(linha["n"] for linha in agregado.values()), resultado["agregadas"])
        self.assertEqual(set(agregado), set(esperado))
        for chave, (quantidade, maximo, tmin, tmax) in esperado.items():
            linha = agregado[chave]
            self.assertEqual((linha["n"], linha["status_alerta_max"], linha["temperatura_min"],
                              linha["temperatura_max"]), (quantidade, maximo, tmin, tmax))
            self.assertEqual(linha["n_verde"] + linha["n_amarelo"] + linha["n_vermelho"], quantidade)
        self.assertEqual(self._consultar("SELECT COUNT(*) FROM analise_teste a LEFT JOIN historico_leituras h "
                                         "ON h.id_leitura = a.id_leitura WHERE h.id_leitura IS NULL")[0][0], 0)
        self.assertEqual(self._consultar("PRAGMA integrity_check")[0][0], "ok")
        # Idempotente: rodar de novo não agrega nada.
        self.assertEqual(aplicar_retencao(self.DIAS_RETENCAO, caminho_banco=self.banco, agora=self.AGORA)["agregadas"], 0)

    def test_retencao_soma_em_hora_ja_agregada(self) -> None:
        """Leitura atrasada numa hora já agregada: n somado e média ponderada (upsert)."""
        hora = (self.AGORA - timedelta(days=40)).replace(minute=0)
        for minuto, temperatura in ((0, 10.0), (10, 20.0)):
            inserir_leitura(registro_exemplo(data_hora=(hora + timedelta(minutes=minuto)).strftime(FORMATO),
                                             temperatura_ar=temperatura), caminho_banco=self.banco)
        aplicar_retencao(30, caminho_banco=self.banco, agora=self.AGORA, tamanho_lote=1)
        inserir_leitura(registro_exemplo(data_hora=(hora + timedelta(minutes=20)).strftime(FORMATO),
                                         temperatura_ar=40.0, status_alerta=2, status_regra=2, status_predito=2,
                                         mensagem_voz="Perigo."), caminho_banco=self.banco)
        aplicar_retencao(30, caminho_banco=self.banco, agora=self.AGORA)
        linha = self._consultar("SELECT * FROM historico_agregado_hora")[0]
        self.assertEqual((linha["n"], linha["temperatura_min"], linha["temperatura_max"], linha["status_alerta_max"],
                          linha["n_vermelho"]), (3, 10.0, 40.0, 2, 1))
        self.assertAlmostEqual(linha["temperatura_med"], (10.0 + 20.0 + 40.0) / 3)

    def test_backup_com_coletor_gravando(self) -> None:
        """fazer_backup com o coletor gravando em paralelo gera cópia íntegra (integrity_check = ok)."""
        coletor = self.coletor(25, forcar_simulacao=True)
        execucao = threading.Thread(target=coletor.executar, daemon=True)
        execucao.start()
        backups: List[Path] = []
        while execucao.is_alive() and len(backups) < 4:
            time.sleep(0.4)
            backups.append(fazer_backup(self.pasta / f"copia_{len(backups)}.db", caminho_banco=self.banco))
        execucao.join(timeout=60)
        self.assertGreaterEqual(len(backups), 2)
        for copia in backups:
            conexao = sqlite3.connect(copia)
            try:
                self.assertEqual(conexao.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertGreater(conexao.execute("SELECT COUNT(*) FROM culturas").fetchone()[0], 0)
            finally:
                conexao.close()
        self.assertFalse(any(caminho.name.endswith(".parcial") for caminho in self.pasta.iterdir()))

    def test_linha_de_comando(self) -> None:
        """`database.py --backup DESTINO`, `--compactar` e `--reter DIAS` terminam com código 0."""
        inserir_leitura(registro_exemplo(), caminho_banco=self.banco)
        destino = self.pasta / "x.db"
        for argumentos in (["--backup", str(destino)], ["--compactar"], ["--reter", "30"]):
            processo = subprocess.run([sys.executable, str(PASTA_SRC / "database.py"), "--banco", str(self.banco),
                                       *argumentos], capture_output=True, text=True, timeout=120, check=False)
            self.assertEqual(processo.returncode, 0, processo.stderr)
        conexao = sqlite3.connect(destino)
        try:
            self.assertEqual(conexao.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conexao.execute("SELECT COUNT(*) FROM historico_leituras").fetchone()[0], 1)
        finally:
            conexao.close()

    def test_api_status_depois_da_retencao(self) -> None:
        """/api/status continua respondendo (200) depois da retenção."""
        import app as modulo_app
        self._povoar()
        inserir_leitura(registro_exemplo(), caminho_banco=self.banco)
        aplicar_retencao(self.DIAS_RETENCAO, caminho_banco=self.banco)
        original = modulo_app.app.config["CAMINHO_BANCO"]
        modulo_app.app.config["CAMINHO_BANCO"] = self.banco
        try:
            resposta = modulo_app.app.test_client().get("/api/status")
        finally:
            modulo_app.app.config["CAMINHO_BANCO"] = original
        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.get_json()["tem_leitura"])


# ---------------------------------------------------------------------------
# 5. Operação 24/7: logs, servidor, systemd e instalador
# ---------------------------------------------------------------------------
class TesteOperacao24x7(unittest.TestCase):
    """Logs rotativos, servidor waitress, units systemd e instalador."""

    def setUp(self) -> None:
        """Pasta temporária."""
        self.pasta = Path(tempfile.mkdtemp(prefix="agro_operacao_"))

    def tearDown(self) -> None:
        """Desfaz os logs configurados e remove a pasta."""
        registro.desfazer_configuracao_logs()
        shutil.rmtree(self.pasta, ignore_errors=True)

    def test_a_logs_rotativos_limitados(self) -> None:
        """30 MB de log: no máximo 6 arquivos e nenhum maior que 5 MB + 1 linha."""
        logging.disable(logging.NOTSET)
        try:
            logger = registro.configurar_logs("teste", pasta=self.pasta)
            registro.configurar_logs("teste", pasta=self.pasta)  # idempotente: não duplica handlers
            marcados = [handler for handler in logging.getLogger().handlers
                        if getattr(handler, "_agroanalise_registro", None)]
            self.assertEqual(len(marcados), 2)
            mensagem = "x" * 1000
            with contextlib.redirect_stderr(io.StringIO()):
                stderr_handler = [handler for handler in marcados if not hasattr(handler, "baseFilename")][0]
                stderr_handler.setStream(io.StringIO())
                for _ in range(30 * 1024):
                    logger.info(mensagem)
        finally:
            registro.desfazer_configuracao_logs()
            logging.disable(logging.WARNING)
        arquivos = sorted(self.pasta.glob("teste.log*"))
        self.assertLessEqual(len(arquivos), 6)
        self.assertGreaterEqual(len(arquivos), 5)
        tamanho_linha = len(mensagem) + 200
        for arquivo in arquivos:
            self.assertLessEqual(arquivo.stat().st_size, registro.TAMANHO_MAXIMO_LOG_BYTES + tamanho_linha)
        self.assertEqual(logging.getLogger("werkzeug").level, logging.WARNING)
        self.assertEqual(logging.getLogger("waitress").level, logging.WARNING)

    def test_b_servidor_waitress_responde(self) -> None:
        """src/servidor.py numa porta livre: GET / e /api/status com 200 em até 10 s."""
        with socket.socket() as soquete:
            soquete.bind(("127.0.0.1", 0))
            porta = soquete.getsockname()[1]
        ambiente = dict(os.environ, AGRO_BANCO=str(self.pasta / "web.db"), AGRO_PASTA_LOGS=str(self.pasta / "logs"),
                        AGRO_HOST="127.0.0.1")
        processo = subprocess.Popen([sys.executable, str(PASTA_SRC / "servidor.py"), "--porta", str(porta)],
                                    env=ambiente, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            prazo = time.monotonic() + 10
            respostas: Dict[str, int] = {}
            while time.monotonic() < prazo and len(respostas) < 2:
                for rota in ("/", "/api/status"):
                    if rota in respostas:
                        continue
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{porta}{rota}", timeout=2) as resposta:
                            respostas[rota] = resposta.status
                    except (urllib.error.URLError, ConnectionError, OSError):
                        time.sleep(0.2)
            self.assertEqual(respostas, {"/": 200, "/api/status": 200})
            self.assertTrue((self.pasta / "web.db").exists())
            self.assertTrue((self.pasta / "logs" / "web.log").exists())
        finally:
            processo.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                processo.wait(timeout=10)
            if processo.poll() is None:
                processo.kill()

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze não disponível")
    def test_c_units_systemd_validas(self) -> None:
        """`systemd-analyze verify deploy/*.service` sem erros; reinício infinito e grupo dialout."""
        units = sorted((RAIZ_PROJETO / "deploy").glob("*.service"))
        # v3: além dos dois serviços 24/7 da v2, deploy/ ganhou o alarme sonoro local (agroanalise-alarme.service,
        # 24/7, Frente 3) e serviços "oneshot" disparados por .timer (agroanalise-desempenho, de hora em hora,
        # Frente 2). Coletor e web continuam obrigatórios; o verify cobre também os .timer.
        nomes = [unit.name for unit in units]
        for esperado in ("agroanalise-coletor.service", "agroanalise-web.service"):
            self.assertIn(esperado, nomes)
        timers = sorted((RAIZ_PROJETO / "deploy").glob("*.timer"))
        processo = subprocess.run(["systemd-analyze", "verify", *map(str, units), *map(str, timers)],
                                  capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(processo.returncode, 0, processo.stderr + processo.stdout)
        self.assertEqual(processo.stderr.strip(), "")
        for unit in units:
            texto = unit.read_text(encoding="utf-8")
            if "Type=oneshot" in texto:
                # Restart=always é inválido em Type=oneshot; quem reinicia é o .timer (Persistent=true).
                timer = unit.with_suffix(".timer")
                self.assertTrue(timer.is_file(), f"{unit.name} é oneshot sem {timer.name}")
                self.assertIn("Persistent=true", timer.read_text(encoding="utf-8"))
                continue
            for diretiva in ("Restart=always", "RestartSec=5", "StartLimitIntervalSec=0", "After=network.target",
                             "WorkingDirectory=", "User=agroanalise", "SupplementaryGroups=dialout"):
                self.assertIn(diretiva, texto, f"{unit.name} sem {diretiva}")

    @unittest.skipUnless(shutil.which("bash"), "bash não disponível")
    def test_d_instalador_sintaxe(self) -> None:
        """`bash -n deploy/instalar.sh` termina com código 0; o .bat tem laço de reinício."""
        processo = subprocess.run(["bash", "-n", str(RAIZ_PROJETO / "deploy" / "instalar.sh")], capture_output=True,
                                  text=True, timeout=30, check=False)
        self.assertEqual(processo.returncode, 0, processo.stderr)
        instalador = (RAIZ_PROJETO / "deploy" / "instalar.sh").read_text(encoding="utf-8")
        for trecho in ("python3 -m venv", "pip install -r", "useradd", "dialout", "systemctl enable --now",
                       "timedatectl set-ntp true", "modelo_agroanalise.pkl"):
            self.assertIn(trecho, instalador)
        bat = (RAIZ_PROJETO / "deploy" / "iniciar_windows.bat").read_text(encoding="utf-8")
        self.assertIn("goto laco_coletor", bat)
        self.assertIn("goto laco_web", bat)

    def test_requirements_e_gitignore(self) -> None:
        """waitress em requirements.txt; logs/ e *.log.* ignorados pelo git."""
        self.assertIn("waitress>=3.0", (RAIZ_PROJETO / "requirements.txt").read_text(encoding="utf-8"))
        ignorados = (RAIZ_PROJETO / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("logs/", ignorados)
        self.assertIn("*.log.*", ignorados)


if __name__ == "__main__":
    unittest.main()
