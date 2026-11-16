"""Módulo 2 — Banco de Dados e Regras Agronômicas do AgroAnálise.

Responsável por:
    * Definir as constantes compartilhadas por todos os módulos (caminhos,
      códigos de status, modos operantes, nomes de variáveis).
    * Criar o banco SQLite3 ``data/agroanalise.db`` com um esquema ESCALÁVEL:
        - ``culturas``: catálogo de vegetais/frutas/grãos monitorados;
        - ``parametros_cultura``: faixas agronômicas em formato LONGO
          (uma linha por cultura × variável), o que permite acrescentar novas
          culturas e até novas variáveis (pH, luminosidade...) sem ALTER TABLE;
        - ``historico_leituras``: leituras sanitizadas, features, predição e
          flag de anomalia;
        - ``configuracao_sistema``: chave–valor tipado (modo operante, porta
          serial, cultura ativa...).
    * Semear, de forma idempotente, os parâmetros do Café Arábica e do Café
      Conilon e as configurações iniciais.
    * Oferecer funções tipadas de inserção e busca usadas pelo gerador de
      dataset, pelo treinamento, pelo coletor serial e pelo servidor Flask.

Confiabilidade 24/7 (esquema evoluído por ``migrar_esquema``, idempotente):
    * colunas ``estado_sensor``, ``diagnostico_sensor`` e ``instante_utc`` em
      ``historico_leituras`` (bancos antigos são migrados sem perder linhas);
    * ordem de gravação (``id_leitura``) como ordem oficial do histórico, imune
      a saltos do relógio;
    * gravação em lote e de configurações na MESMA transação da leitura;
    * retenção com agregação horária (``historico_agregado_hora``), backup
      online e compactação.

Execução direta:
    ``python src/database.py``                   inicializa e imprime um resumo;
    ``python src/database.py --backup DESTINO``  cópia consistente (segura com WAL);
    ``python src/database.py --compactar``       VACUUM manual;
    ``python src/database.py --reter DIAS``      agrega e apaga as leituras brutas antigas.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import re
import sqlite3
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

# ---------------------------------------------------------------------------
# Constantes compartilhadas (contrato técnico, seção 2)
# ---------------------------------------------------------------------------
BASE_DIR: Path = Path(__file__).resolve().parent.parent
DATA_DIR: Path = BASE_DIR / "data"
MODELS_DIR: Path = BASE_DIR / "models"
REPORTS_DIR: Path = BASE_DIR / "reports"
CAMINHO_BANCO: Path = DATA_DIR / "agroanalise.db"
CAMINHO_DATASET_BRUTO: Path = DATA_DIR / "dataset_bruto.csv"
CAMINHO_DATASET_LIMPO: Path = DATA_DIR / "dataset_limpo.csv"
CAMINHO_MODELO: Path = MODELS_DIR / "modelo_agroanalise.pkl"
CAMINHO_MATRIZ_CONFUSAO: Path = REPORTS_DIR / "matriz_confusao.png"
CAMINHO_RELATORIO_TREINO: Path = REPORTS_DIR / "relatorio_treinamento.txt"

STATUS_VERDE: int = 0
STATUS_AMARELO: int = 1
STATUS_VERMELHO: int = 2
NOMES_STATUS: Dict[int, str] = {0: "Verde (Ideal)", 1: "Amarelo (Atenção)", 2: "Vermelho (Risco Crítico)"}
CORES_STATUS: Dict[int, str] = {0: "verde", 1: "amarelo", 2: "vermelho"}

MODO_CAMPO_USB: str = "CAMPO_USB"
MODO_SIMULACAO: str = "SIMULACAO"
MODO_DEMONSTRACAO: str = "DEMONSTRACAO"
MODOS_OPERACAO: Tuple[str, ...] = (MODO_CAMPO_USB, MODO_SIMULACAO, MODO_DEMONSTRACAO)
ORIGEM_DATASET: str = "DATASET"  # origens válidas de leitura = MODOS_OPERACAO + ("DATASET",)
ORIGENS_LEITURA: Tuple[str, ...] = MODOS_OPERACAO + (ORIGEM_DATASET,)

VARIAVEL_TEMPERATURA_AR: str = "temperatura_ar"
VARIAVEL_UMIDADE_AR: str = "umidade_ar"
VARIAVEL_UMIDADE_SOLO: str = "umidade_solo"
CULTURA_PADRAO: str = "cafe_arabica"

CATEGORIAS_CULTURA: Tuple[str, ...] = ("cafe", "fruta", "grao", "hortalica", "leguminosa", "outra")
TIPOS_CONFIGURACAO: Tuple[str, ...] = ("str", "int", "float", "bool")

# Variáveis que TODA cultura precisa ter cadastradas: os sensores do campo
# (DHT11 + higrômetro). Variáveis extras (ph_solo, luminosidade...) são aceitas.
VARIAVEIS_OBRIGATORIAS: Tuple[str, ...] = (VARIAVEL_TEMPERATURA_AR, VARIAVEL_UMIDADE_AR, VARIAVEL_UMIDADE_SOLO)

# Faixa física medível pelos sensores (espelha LIMITES_FISICOS_SENSORES do
# gerador_dataset). Usada só para validar cadastros: as bordas de atenção das
# variáveis que definem o semáforo precisam ficar DENTRO do que o sensor mede,
# senão o estado Vermelho daquela borda jamais poderia ser observado.
_LIMITES_FISICOS_CADASTRO: Dict[str, Tuple[float, float]] = {
    VARIAVEL_TEMPERATURA_AR: (0.0, 50.0),
    VARIAVEL_UMIDADE_AR: (0.0, 100.0),
    VARIAVEL_UMIDADE_SOLO: (0.0, 100.0),
}
_VARIAVEIS_DO_SEMAFORO: Tuple[str, ...] = (VARIAVEL_TEMPERATURA_AR, VARIAVEL_UMIDADE_SOLO)
_PADRAO_CODIGO: "re.Pattern[str]" = re.compile(r"^[a-z][a-z0-9_]{1,49}$")
_PADRAO_VARIAVEL: "re.Pattern[str]" = re.compile(r"^[a-z][a-z0-9_]{1,49}$")
# Motivo da regra gravado em historico_leituras.motivo_regra: um dos códigos de
# ``regras_agronomicas.MOTIVOS_REGRA`` (este módulo não importa aquele, que
# depende dele); vazio = leitura sem regra v3 (registros antigos).
_PADRAO_MOTIVO_REGRA: "re.Pattern[str]" = re.compile(r"^[A-Z][A-Z_]{1,39}$")
BASES_TEMPERATURA_VALIDAS: Tuple[str, ...] = ("", "instantanea", "media_24h")

registrador: logging.Logger = logging.getLogger("agroanalise.database")

# Estados do sensor gravados em historico_leituras.estado_sensor (pior → maior índice).
ESTADO_SENSOR_OK: str = "OK"
ESTADO_SENSOR_SUSPEITO: str = "SUSPEITO"
ESTADO_SENSOR_DEFEITO: str = "DEFEITO"
ESTADOS_SENSOR: Tuple[str, ...] = (ESTADO_SENSOR_OK, ESTADO_SENSOR_SUSPEITO, ESTADO_SENSOR_DEFEITO)

# Espera máxima (ms) por uma trava do SQLite antes de desistir com
# ``sqlite3.OperationalError: database is locked``. O coletor trata essa falha
# guardando a leitura numa fila em memória; os testes reduzem este valor.
TEMPO_ESPERA_TRAVA_MS: int = 10000

FORMATO_DATA_HORA_BANCO: str = "%Y-%m-%d %H:%M:%S"
TAMANHO_LOTE_RETENCAO: int = 5000
DIAS_RETENCAO_DEMONSTRACAO: int = 1

# Memoização: caminhos de banco já inicializados neste processo.
_BANCOS_INICIALIZADOS: Set[str] = set()
_TRAVA_INICIALIZACAO: threading.Lock = threading.Lock()


# ---------------------------------------------------------------------------
# Estruturas de dados
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FaixaParametro:
    """Faixas agronômicas de UMA variável para UMA cultura.

    Ordem obrigatória: ``critico_min <= atencao_min <= ideal_min < ideal_max
    <= atencao_max <= critico_max``.
    """

    variavel: str
    unidade: str
    ideal_min: float
    ideal_max: float
    atencao_min: float
    atencao_max: float
    critico_min: float
    critico_max: float


@dataclass(frozen=True)
class ParametrosCultura:
    """Conjunto de faixas de uma cultura cadastrada, indexadas pelo nome da variável."""

    id_cultura: int
    codigo: str
    nome_comum: str
    nome_cientifico: str
    categoria: str
    faixas: Dict[str, FaixaParametro]

    def faixa(self, variavel: str) -> FaixaParametro:
        """Retorna a faixa da variável pedida.

        Raises:
            KeyError: se a cultura não tiver a variável cadastrada.
        """
        if variavel not in self.faixas:
            disponiveis = ", ".join(sorted(self.faixas)) or "nenhuma"
            raise KeyError(
                f"A cultura '{self.codigo}' não possui faixa cadastrada para a variável "
                f"'{variavel}'. Variáveis disponíveis: {disponiveis}."
            )
        return self.faixas[variavel]


@dataclass
class RegistroLeitura:
    """Uma leitura completa, pronta para ser gravada em ``historico_leituras``."""

    data_hora: str
    codigo_cultura: str
    origem: str
    temperatura_ar_bruta: Optional[float]
    umidade_ar_bruta: Optional[float]
    umidade_solo_bruta: Optional[float]
    temperatura_ar: float
    umidade_ar: float
    umidade_solo: float
    deficit_hidrico_solo: float
    desvio_termico: float
    status_regra: int
    status_predito: int
    status_alerta: int
    probabilidade_predicao: float
    flag_anomalia: int
    descricao_anomalia: str
    mensagem_voz: str
    versao_modelo: str
    # Campos da confiabilidade de campo (com padrão, no fim, para manter os
    # construtores antigos válidos).
    estado_sensor: str = ESTADO_SENSOR_OK
    diagnostico_sensor: str = ""
    instante_utc: Optional[str] = None
    # Campos da regra fisiológica (v3), também com padrão: os construtores v1 e
    # v2 continuam válidos. ``motivo_regra`` é um de
    # ``regras_agronomicas.MOTIVOS_REGRA`` (ou vazio); ``base_temperatura`` é
    # ``instantanea`` ou ``media_24h``; ``umidade_solo_firmware`` é o valor do
    # firmware ANTES da calibração da sonda feita no host.
    motivo_regra: str = ""
    base_temperatura: str = ""
    temperatura_media_24h: Optional[float] = None
    umidade_solo_firmware: Optional[float] = None


# ---------------------------------------------------------------------------
# Dados semeados (contrato técnico, seções 3.2 e 3.4)
# ---------------------------------------------------------------------------
_CULTURAS_INICIAIS: List[Dict[str, str]] = [
    {
        "codigo": "cafe_arabica",
        "nome_comum": "Café Arábica",
        "nome_cientifico": "Coffea arabica",
        "categoria": "cafe",
        "descricao": "Café de altitude e clima ameno; sensível a calor excessivo e a geadas.",
    },
    {
        "codigo": "cafe_conilon",
        "nome_comum": "Café Conilon",
        "nome_cientifico": "Coffea canephora",
        "categoria": "cafe",
        "descricao": "Café robusta de regiões baixas e quentes; mais tolerante ao calor, sensível ao frio.",
    },
]

# (codigo_cultura, variavel, unidade, ideal_min, ideal_max, atencao_min, atencao_max, critico_min, critico_max, fonte)
_PARAMETROS_INICIAIS: List[Tuple[str, str, str, float, float, float, float, float, float, str]] = [
    ("cafe_arabica", VARIAVEL_TEMPERATURA_AR, "°C", 18.0, 22.0, 15.0, 25.0, 8.0, 32.0, "Embrapa Café / Incaper"),
    ("cafe_arabica", VARIAVEL_UMIDADE_SOLO, "%", 60.0, 80.0, 50.0, 88.0, 35.0, 95.0, "Embrapa Café / Incaper"),
    ("cafe_arabica", VARIAVEL_UMIDADE_AR, "%", 60.0, 80.0, 50.0, 90.0, 35.0, 98.0, "Embrapa Café / Incaper"),
    ("cafe_conilon", VARIAVEL_TEMPERATURA_AR, "°C", 22.0, 26.0, 19.0, 29.0, 12.0, 34.0, "Incaper / Embrapa Rondônia"),
    ("cafe_conilon", VARIAVEL_UMIDADE_SOLO, "%", 55.0, 75.0, 45.0, 83.0, 30.0, 95.0, "Incaper / Embrapa Rondônia"),
    ("cafe_conilon", VARIAVEL_UMIDADE_AR, "%", 60.0, 85.0, 50.0, 90.0, 35.0, 98.0, "Incaper / Embrapa Rondônia"),
]

# (chave, valor, tipo, descricao)
_CONFIGURACOES_INICIAIS: List[Tuple[str, str, str, str]] = [
    ("cultura_ativa", CULTURA_PADRAO, "str", "Código da cultura monitorada no momento (tabela culturas)."),
    ("modo_operacao", MODO_SIMULACAO, "str", "Modo operante atual: CAMPO_USB, SIMULACAO ou DEMONSTRACAO."),
    ("estado_hardware", "DESCONECTADO", "str", "Estado do Arduino escrito pelo coletor: CONECTADO ou DESCONECTADO."),
    ("porta_serial", "AUTO", "str", "Porta serial do Arduino; AUTO = autodetecção via serial.tools.list_ports."),
    ("baud_rate", "9600", "int", "Velocidade da comunicação serial em bps (igual ao firmware)."),
    ("intervalo_coleta_segundos", "5", "float", "Intervalo entre duas leituras gravadas pelo coletor, em segundos."),
    ("intervalo_reconexao_segundos", "15", "float", "Intervalo entre tentativas de reconectar o Arduino no Modo Simulação."),
    ("cenario_demonstracao", "", "str", "Cenário extremo em exibição no Modo Demonstração (vazio = nenhum)."),
    ("demonstracao_expira_em", "", "str", "Data/hora ISO em que o Modo Demonstração termina (vazio = inativo)."),
    ("duracao_demonstracao_segundos", "120", "int", "Duração de cada cenário de demonstração, em segundos."),
    ("versao_modelo", "REGRAS", "str", "Versão do modelo de IA em uso (atualizada pelo treinamento)."),
    ("nome_propriedade", "Sítio Modelo", "str", "Nome da propriedade rural exibido na interface."),
]

# Configurações da confiabilidade de campo (chave, valor, tipo, descricao).
# São semeadas com INSERT OR IGNORE por ``semear_configuracoes_confiabilidade``,
# chamada pelo coletor ao iniciar e pela linha de comando deste módulo. Ficam
# FORA de ``_CONFIGURACOES_INICIAIS`` de propósito: ``inicializar_banco``
# continua criando exatamente as 12 chaves do contrato v1 (há um teste antigo
# que confere essa contagem), e quem lê estas chaves sempre tem um padrão.
_CONFIGURACOES_CONFIABILIDADE: List[Tuple[str, str, str, str]] = [
    ("janela_mediana_leituras", "12", "int", "Leituras na mediana móvel aplicada ao campo antes da regra."),
    ("taxa_maxima_queda_solo_pp_h", "8.0", "float",
     "Queda máxima plausível da umidade do solo, em pontos percentuais por hora (evaporação)."),
    ("minutos_travamento_solo", "60", "int", "Minutos com o solo idêntico para suspeitar de sensor travado."),
    ("horas_travamento_ar", "3", "int", "Horas com temperatura e umidade do ar idênticas para suspeitar do DHT11."),
    ("exigir_protocolo_v2", "false", "bool", "Se verdadeiro, o coletor descarta linhas seriais sem checksum (v1)."),
    ("espaco_minimo_mb", "200", "float", "Espaço livre mínimo no disco do banco, em MB, antes da retenção de emergência."),
    ("retencao_dias_brutos", "30", "int", "Dias de leituras brutas mantidas; as mais antigas viram médias por hora."),
    ("coletor_batimento_em", "", "str", "Instante ISO UTC (+00:00) do último ciclo do coletor (batimento)."),
    ("coletor_pid", "0", "int", "PID do processo coletor que gravou o último batimento."),
    ("coletor_ultimo_erro", "", "str", "Último erro de ciclo registrado pelo coletor (vazio = sem erro)."),
    ("coletor_fila_pendente", "0", "int", "Leituras em memória aguardando gravação (banco travado)."),
    ("disco_livre_mb", "", "float", "Espaço livre no disco do banco, em MB, medido pelo coletor."),
    ("relogio_ultimo_salto_em", "", "str", "Instante ISO UTC do último salto de relógio detectado pelo coletor."),
    ("serial_linhas_v1", "0", "int", "Linhas de dados recebidas no protocolo v1 (sem checksum)."),
    ("serial_linhas_v2", "0", "int", "Linhas de dados recebidas no protocolo v2 (com checksum)."),
    ("serial_checksum_invalido", "0", "int", "Linhas v2 descartadas por checksum inválido."),
    ("serial_linhas_descartadas", "0", "int", "Linhas de dados descartadas (1ª após conectar, v1 recusada...)."),
]

# Configurações da v3 (regra fisiológica, perfil do sensor de ar e chaves das
# outras frentes), semeadas com INSERT OR IGNORE por ``semear_configuracoes_v3``
# (coletor ao iniciar e linha de comando). Também ficam fora de
# ``_CONFIGURACOES_INICIAIS``: ``inicializar_banco`` continua com as 12 chaves
# do contrato v1. Quem lê usa o MESMO padrão quando a chave falta — em
# especial ``regra_temperatura`` ausente = ``instantanea`` (legado v2); a
# semente ``fisiologica`` leva toda instalação de campo ao modo novo, e o
# técnico pode voltar a ``instantanea``. Fontes dos números em
# ``src/regras_agronomicas.py``.
_CONFIGURACOES_V3: List[Tuple[str, str, str, str]] = [
    ("regra_temperatura", "fisiologica", "str",
     "Regra térmica: 'fisiologica' (faixa do cliente na média de 24 h + geada e calor) ou 'instantanea' (v2)."),
    ("limiar_geada_abrigo_c", "2.0", "float",
     "Temperatura do abrigo (1,5 m) com dano de geada ao cafeeiro, em °C (Epamig/Embrapa Café)."),
    ("incerteza_sensor_ar_c", "2.0", "float",
     "Incerteza do sensor de ar em °C (DHT11 ±2; DHT22 ±0,5; SHT3x ±0,2), somada ao limiar de geada."),
    ("limiar_calor_dano_c", "34.0", "float", "Máxima do dia, em °C, que aborta flores do café (Embrapa, 2003)."),
    ("dias_janela_florada", "45", "int", "Dias da janela de florada a partir de data_florada_principal."),
    ("data_florada_principal", "", "str", "Data da florada principal (AAAA-MM-DD); vazio = setembro a novembro."),
    ("seca_programada_ativa", "false", "bool",
     "Se verdadeiro, solo abaixo do ideal na janela da seca programada é só Amarelo (estresse hídrico controlado)."),
    ("seca_programada_inicio", "06-24", "str", "Início (MM-DD) da seca programada (Embrapa Cerrados: 24/jun)."),
    ("seca_programada_fim", "09-04", "str", "Fim (MM-DD) da seca programada (Embrapa Cerrados: 4/set)."),
    ("banda_morta_ativa", "true", "bool", "Banda morta no status gravado: piorar é imediato, melhorar exige folga."),
    ("banda_morta_fracao", "0.03", "float", "Banda morta como fração da faixa crítica (0,03 = 0,72 °C no Arábica)."),
    ("banda_morta_atraso_s", "60", "float",
     "Off-delay da banda morta: segundos de melhora contínua antes de gravar a cor melhor (ISA-18.2)."),
    ("sensor_ar_modelo", "DHT11", "str", "Sensor de ar informado pelo firmware no banner (DHT11, DHT22 ou SHT3X)."),
    ("prognostico_suspenso", "false", "bool", "Prognóstico de 12 h suspenso pela avaliação em dado local (Frente 2)."),
    ("prognostico_brier_local_28d", "", "float",
     "Brier do prognóstico nas últimas 4 semanas do próprio sítio (vazio = ainda não avaliado; Frente 2)."),
    ("prognostico_avaliado_em", "", "str", "Instante ISO da última avaliação local do prognóstico (Frente 2)."),
    ("coef_tmin_local", "", "str",
     "Coeficientes locais 'a,b,c' da mínima noturna (Tmin = a + b·T_tarde + c·Td_tarde), escritos pela Frente 2."),
    ("quantis_tmin_local", "", "str",
     "Margens conformais locais da mínima por estação, JSON {'seca': q, 'aguas': q} (Frente 2)."),
    ("platt_local", "", "str", "Calibração de Platt local 'a,b' do prognóstico: p' = σ(a·logit(p)+b) (Frente 2)."),
    ("regiao_referencia", "", "str", "Região climática de referência (ERA5-Land) usada pela Frente 2."),
    ("alarme_local_ativo", "true", "bool", "Alarme sonoro local para Vermelho (Frente 3)."),
    ("alarme_silencio_inicio", "21:30", "str",
     "Início (HH:MM) do silêncio do alarme local (Frente 3); o Vermelho fica pendente até o fim do silêncio."),
    ("alarme_silencio_fim", "05:00", "str", "Fim (HH:MM) do silêncio do alarme local (Frente 3)."),
    ("numero_whatsapp_tecnico", "", "str", "Número de WhatsApp do técnico para o pedido de ajuda (Frente 3)."),
]


# ---------------------------------------------------------------------------
# DDL do esquema
# ---------------------------------------------------------------------------
_DDL_ESQUEMA: str = """
CREATE TABLE IF NOT EXISTS culturas (
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

CREATE TABLE IF NOT EXISTS parametros_cultura (
    id_parametro     INTEGER PRIMARY KEY AUTOINCREMENT,
    id_cultura       INTEGER NOT NULL
                     REFERENCES culturas(id_cultura) ON DELETE CASCADE,
    variavel         TEXT    NOT NULL,
    unidade          TEXT    NOT NULL,
    ideal_min        REAL    NOT NULL,
    ideal_max        REAL    NOT NULL,
    atencao_min      REAL    NOT NULL,
    atencao_max      REAL    NOT NULL,
    critico_min      REAL    NOT NULL,
    critico_max      REAL    NOT NULL,
    fonte_referencia TEXT,
    atualizado_em    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (id_cultura, variavel),
    CHECK (critico_min <= atencao_min AND atencao_min <= ideal_min AND ideal_min < ideal_max
           AND ideal_max <= atencao_max AND atencao_max <= critico_max)
);

CREATE INDEX IF NOT EXISTS idx_parametros_cultura_id ON parametros_cultura(id_cultura);

CREATE TABLE IF NOT EXISTS historico_leituras (
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

CREATE INDEX IF NOT EXISTS idx_historico_data_hora ON historico_leituras(data_hora);
CREATE INDEX IF NOT EXISTS idx_historico_cultura_data ON historico_leituras(id_cultura, data_hora);

CREATE TABLE IF NOT EXISTS configuracao_sistema (
    chave         TEXT PRIMARY KEY,
    valor         TEXT NOT NULL,
    tipo          TEXT NOT NULL CHECK (tipo IN ('str','int','float','bool')),
    descricao     TEXT NOT NULL,
    atualizado_em TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

# Colunas acrescentadas a historico_leituras: (nome, definição usada no ALTER
# TABLE ADD COLUMN). As da regra fisiológica (v3) vêm ANTES das da
# confiabilidade de campo (v2) de propósito: num banco v1 (ou novo) as três da
# v2 continuam sendo as últimas colunas da tabela, ordem que um teste de
# migração concorrente da v2 confere; num banco v2 as da v3 entram no fim.
_COLUNAS_MIGRACAO_HISTORICO: List[Tuple[str, str]] = [
    ("motivo_regra", "TEXT NOT NULL DEFAULT ''"),
    ("base_temperatura", "TEXT NOT NULL DEFAULT ''"),
    ("temperatura_media_24h", "REAL"),
    ("umidade_solo_firmware", "REAL"),
    ("estado_sensor",
     "TEXT NOT NULL DEFAULT 'OK' CHECK (estado_sensor IN ('OK','SUSPEITO','DEFEITO'))"),
    ("diagnostico_sensor", "TEXT NOT NULL DEFAULT ''"),
    ("instante_utc", "TEXT"),
]

_DDL_MIGRACAO: str = """
CREATE INDEX IF NOT EXISTS idx_historico_cultura_id ON historico_leituras(id_cultura, id_leitura);
CREATE INDEX IF NOT EXISTS idx_historico_cultura_origem_id ON historico_leituras(id_cultura, origem, id_leitura);

CREATE TABLE IF NOT EXISTS historico_agregado_hora (
    id_cultura          INTEGER NOT NULL REFERENCES culturas(id_cultura),
    hora                TEXT    NOT NULL,
    origem              TEXT    NOT NULL,
    n                   INTEGER NOT NULL,
    temperatura_min     REAL,
    temperatura_med     REAL,
    temperatura_max     REAL,
    umidade_ar_min      REAL,
    umidade_ar_med      REAL,
    umidade_ar_max      REAL,
    umidade_solo_min    REAL,
    umidade_solo_med    REAL,
    umidade_solo_max    REAL,
    status_alerta_max   INTEGER,
    n_verde             INTEGER,
    n_amarelo           INTEGER,
    n_vermelho          INTEGER,
    n_anomalias         INTEGER,
    PRIMARY KEY (id_cultura, hora, origem)
);
"""

# Tabelas do retorno do agricultor (criadas por ``retorno_agricultor.garantir_tabelas``) que
# ganham, na v4, a marca ``demonstracao`` (1 = gravado durante a demonstração da banca): o
# resumo do técnico deixa essas linhas de fora, como já fazia com o "Feito" da banca.
TABELAS_COM_MARCA_DEMONSTRACAO: Tuple[str, ...] = ("pedido_ajuda", "eventos_interface")
COLUNA_DEMONSTRACAO: str = "demonstracao"
DEFINICAO_COLUNA_DEMONSTRACAO: str = "INTEGER NOT NULL DEFAULT 0"

# Preenche instante_utc das linhas antigas a partir da data/hora local.
_SQL_PREENCHER_INSTANTE_UTC: str = (
    "UPDATE historico_leituras SET instante_utc = strftime('%Y-%m-%dT%H:%M:%S+00:00', data_hora, 'utc') "
    "WHERE instante_utc IS NULL"
)


# ---------------------------------------------------------------------------
# Conexão e inicialização
# ---------------------------------------------------------------------------
def obter_conexao(caminho_banco: Path = CAMINHO_BANCO) -> sqlite3.Connection:
    """Abre uma conexão SQLite configurada para uso concorrente (coletor + Flask).

    Cria o diretório do banco, ativa chaves estrangeiras e o modo WAL e usa
    ``sqlite3.Row`` para acesso às colunas por nome. O chamador deve fechar a
    conexão.
    """
    caminho = Path(caminho_banco)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    banco_novo = not caminho.exists() or caminho.stat().st_size == 0
    espera_ms = max(0, int(TEMPO_ESPERA_TRAVA_MS))
    conexao = sqlite3.connect(str(caminho), timeout=espera_ms / 1000.0, check_same_thread=False)
    conexao.row_factory = sqlite3.Row
    if banco_novo:
        # Precisa vir ANTES do modo WAL e de qualquer tabela: permite devolver
        # ao disco, aos poucos, o espaço liberado pela retenção.
        conexao.execute("PRAGMA auto_vacuum = INCREMENTAL")
    conexao.execute(f"PRAGMA busy_timeout = {espera_ms}")
    conexao.execute("PRAGMA foreign_keys = ON")
    conexao.execute("PRAGMA journal_mode = WAL")
    return conexao


def migrar_esquema(conexao: sqlite3.Connection) -> None:
    """Leva um banco de qualquer versão anterior ao esquema atual (idempotente).

    * acrescenta a ``historico_leituras`` as colunas que faltarem
      (``PRAGMA table_info`` + ``ALTER TABLE ADD COLUMN``): ``estado_sensor``,
      ``diagnostico_sensor`` e ``instante_utc`` (v2) e ``motivo_regra``,
      ``base_temperatura``, ``temperatura_media_24h`` e
      ``umidade_solo_firmware`` (v3); linhas antigas ficam com
      ``estado_sensor = 'OK'``, textos vazios e ``NULL`` nos números;
    * preenche ``instante_utc`` das linhas que não o têm, convertendo a
      data/hora local para UTC;
    * cria os índices por ordem de gravação e a tabela ``historico_agregado_hora``;
    * acrescenta a marca ``demonstracao`` a ``pedido_ajuda`` e ``eventos_interface``
      quando essas tabelas já existem (v4, ``migrar_colunas_demonstracao``).

    Rodar duas (ou mais) vezes não dá erro nem altera dados já migrados. A
    conferência das colunas e os ``ALTER TABLE`` acontecem sob ``BEGIN
    IMMEDIATE``: se o coletor e o servidor web subirem juntos logo depois de
    uma atualização, só um deles migra e o outro encontra tudo pronto.
    """
    conexao.commit()
    conexao.execute("BEGIN IMMEDIATE")
    try:
        existentes = {str(linha[1]) for linha in conexao.execute("PRAGMA table_info(historico_leituras)").fetchall()}
        if not existentes:
            raise RuntimeError("Tabela historico_leituras inexistente: rode criar_tabelas antes de migrar_esquema.")
        for nome, definicao in _COLUNAS_MIGRACAO_HISTORICO:
            if nome not in existentes:
                conexao.execute(f"ALTER TABLE historico_leituras ADD COLUMN {nome} {definicao}")
                registrador.info("Migração do esquema: coluna historico_leituras.%s criada.", nome)
        conexao.execute(_SQL_PREENCHER_INSTANTE_UTC)
        conexao.commit()
    except Exception:
        conexao.rollback()
        raise
    conexao.executescript(_DDL_MIGRACAO)
    conexao.commit()
    migrar_colunas_demonstracao(conexao)


def migrar_colunas_demonstracao(conexao: sqlite3.Connection) -> List[str]:
    """Acrescenta ``demonstracao INTEGER NOT NULL DEFAULT 0`` às tabelas do retorno que existirem (idempotente).

    Vale para ``TABELAS_COM_MARCA_DEMONSTRACAO`` (``pedido_ajuda`` e
    ``eventos_interface``, v4). Tabela ausente é ignorada (ela nasce já com a
    coluna em ``retorno_agricultor``); coluna já existente também. Linhas
    antigas ficam com 0 (contam no resumo, como antes). Se outro processo criar
    a coluna ao mesmo tempo, o erro de coluna duplicada é tolerado.

    Args:
        conexao: conexão aberta (é feito ``commit`` ao final).

    Returns:
        Nomes das tabelas que ganharam a coluna nesta chamada.

    Raises:
        sqlite3.Error: falha do banco que não seja a coluna duplicada.
    """
    criadas: List[str] = []
    for tabela in TABELAS_COM_MARCA_DEMONSTRACAO:
        colunas = {str(linha[1]) for linha in conexao.execute(f"PRAGMA table_info({tabela})").fetchall()}
        if not colunas or COLUNA_DEMONSTRACAO in colunas:
            continue
        try:
            conexao.execute(f"ALTER TABLE {tabela} ADD COLUMN {COLUNA_DEMONSTRACAO} {DEFINICAO_COLUNA_DEMONSTRACAO}")
        except sqlite3.OperationalError as erro:
            if "duplicate column" in str(erro).lower():
                continue
            raise
        criadas.append(tabela)
        registrador.info("Migração do esquema: coluna %s.%s criada.", tabela, COLUNA_DEMONSTRACAO)
    conexao.commit()
    return criadas


def criar_tabelas(conexao: sqlite3.Connection) -> None:
    """Cria (se ainda não existirem) todas as tabelas e índices e migra o esquema."""
    conexao.executescript(_DDL_ESQUEMA)
    conexao.commit()
    migrar_esquema(conexao)


def popular_parametros_iniciais(conexao: sqlite3.Connection) -> None:
    """Semeia Café Arábica e Café Conilon com suas faixas agronômicas.

    Idempotente: só insere o que ainda não existe (``INSERT ... WHERE NOT
    EXISTS``) — rodar várias vezes não duplica linhas, NÃO sobrescreve faixas
    que já tenham sido ajustadas no banco e não "queima" números do
    AUTOINCREMENT (o SQLite avança a sequência até em ``INSERT OR IGNORE``
    ignorado).
    """
    for cultura in _CULTURAS_INICIAIS:
        conexao.execute(
            """
            INSERT INTO culturas (codigo, nome_comum, nome_cientifico, categoria, descricao, ativo)
            SELECT :codigo, :nome_comum, :nome_cientifico, :categoria, :descricao, 1
             WHERE NOT EXISTS (SELECT 1 FROM culturas WHERE codigo = :codigo)
            """,
            cultura,
        )
    for (codigo, variavel, unidade, ideal_min, ideal_max, atencao_min, atencao_max,
         critico_min, critico_max, fonte) in _PARAMETROS_INICIAIS:
        linha = conexao.execute("SELECT id_cultura FROM culturas WHERE codigo = ?", (codigo,)).fetchone()
        if linha is None:
            raise RuntimeError(f"Falha ao semear: cultura '{codigo}' não foi encontrada após a inserção.")
        conexao.execute(
            """
            INSERT INTO parametros_cultura
                (id_cultura, variavel, unidade, ideal_min, ideal_max, atencao_min, atencao_max,
                 critico_min, critico_max, fonte_referencia)
            SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
             WHERE NOT EXISTS (SELECT 1 FROM parametros_cultura WHERE id_cultura = ? AND variavel = ?)
            """,
            (int(linha["id_cultura"]), variavel, unidade, ideal_min, ideal_max, atencao_min,
             atencao_max, critico_min, critico_max, fonte, int(linha["id_cultura"]), variavel),
        )
    conexao.commit()


def popular_configuracao_inicial(conexao: sqlite3.Connection) -> None:
    """Semeia as chaves de configuração do sistema.

    Idempotente: ``INSERT OR IGNORE`` (chave primária textual, sem
    AUTOINCREMENT) nunca sobrescreve valores já alterados (por exemplo, a
    cultura ativa escolhida pelo agricultor).
    """
    conexao.executemany(
        "INSERT OR IGNORE INTO configuracao_sistema (chave, valor, tipo, descricao) VALUES (?, ?, ?, ?)",
        _CONFIGURACOES_INICIAIS,
    )
    conexao.commit()


def semear_configuracoes_confiabilidade(caminho_banco: Path = CAMINHO_BANCO) -> None:
    """Semeia as chaves da confiabilidade de campo (``INSERT OR IGNORE``, idempotente).

    Nunca sobrescreve um valor já ajustado. Chamada pelo coletor ao iniciar e
    pela linha de comando; quem lê estas chaves usa padrões quando faltam.
    """
    with _sessao(caminho_banco) as conexao:
        conexao.executemany(
            "INSERT OR IGNORE INTO configuracao_sistema (chave, valor, tipo, descricao) VALUES (?, ?, ?, ?)",
            _CONFIGURACOES_CONFIABILIDADE,
        )


def semear_configuracoes_v3(caminho_banco: Path = CAMINHO_BANCO) -> None:
    """Semeia as chaves da v3 (``INSERT OR IGNORE``, idempotente; nunca sobrescreve valor ajustado).

    Chamada pelo coletor ao iniciar e pela linha de comando. Inclui
    ``regra_temperatura='fisiologica'``: um banco que já passou pelo coletor
    ou pela linha de comando usa a regra fisiológica; um banco criado só com
    ``inicializar_banco`` (sem a chave) continua na regra instantânea da v2.
    """
    with _sessao(caminho_banco) as conexao:
        conexao.executemany(
            "INSERT OR IGNORE INTO configuracao_sistema (chave, valor, tipo, descricao) VALUES (?, ?, ?, ?)",
            _CONFIGURACOES_V3,
        )


def _chave_caminho(caminho_banco: Path) -> str:
    """Normaliza o caminho do banco para uso como chave de memoização."""
    return str(Path(caminho_banco).expanduser().resolve())


def inicializar_banco(caminho_banco: Path = CAMINHO_BANCO) -> None:
    """Cria o diretório, as tabelas e os dados iniciais do banco (idempotente)."""
    caminho = Path(caminho_banco)
    with _TRAVA_INICIALIZACAO:
        conexao = obter_conexao(caminho)
        try:
            criar_tabelas(conexao)
            popular_parametros_iniciais(conexao)
            popular_configuracao_inicial(conexao)
        finally:
            conexao.close()
        _BANCOS_INICIALIZADOS.add(_chave_caminho(caminho))
    registrador.debug("Banco inicializado em %s", caminho)


def _garantir_banco(caminho_banco: Path) -> None:
    """Inicializa o banco na primeira utilização do caminho neste processo.

    Também reinicializa se o arquivo tiver sido apagado depois da memoização
    (situação comum em testes com diretórios temporários). A inicialização
    passa por ``criar_tabelas`` → ``migrar_esquema``, então um banco da v1
    aberto por este processo é migrado antes do primeiro uso.
    """
    chave = _chave_caminho(caminho_banco)
    if chave not in _BANCOS_INICIALIZADOS or not Path(caminho_banco).exists():
        inicializar_banco(caminho_banco)


@contextmanager
def _sessao(caminho_banco: Path) -> Iterator[sqlite3.Connection]:
    """Abre uma conexão com o banco garantido, confirma ao final e sempre fecha.

    Em caso de exceção a transação é desfeita (rollback) e a exceção propagada.
    """
    _garantir_banco(caminho_banco)
    conexao = obter_conexao(caminho_banco)
    try:
        yield conexao
        conexao.commit()
    except Exception:
        conexao.rollback()
        raise
    finally:
        conexao.close()


# ---------------------------------------------------------------------------
# Culturas e parâmetros
# ---------------------------------------------------------------------------
def _numero_finito(valor: Any, descricao: str) -> float:
    """Converte para float e garante que seja finito (sem NaN/infinito)."""
    if isinstance(valor, bool):
        raise ValueError(f"{descricao} deve ser numérico, recebido booleano.")
    try:
        numero = float(valor)
    except (TypeError, ValueError) as erro:
        raise ValueError(f"{descricao} deve ser numérico, recebido {valor!r}.") from erro
    if not math.isfinite(numero):
        raise ValueError(f"{descricao} deve ser um número finito, recebido {valor!r}.")
    return numero


def validar_faixa(faixa: FaixaParametro) -> None:
    """Valida uma faixa agronômica antes de gravá-la.

    Regras:
        * nome da variável em minúsculas (slug) e unidade não vazia;
        * todos os limites numéricos e finitos;
        * ``critico_min <= atencao_min <= ideal_min < ideal_max <= atencao_max <= critico_max``;
        * para as variáveis de sensor, a faixa ideal deve caber no que o sensor
          mede e, para as que definem o semáforo (temperatura do ar e umidade do
          solo), as bordas de atenção devem ficar estritamente dentro da faixa
          física — caso contrário o Vermelho daquela borda seria inatingível.

    Raises:
        ValueError: com mensagem explicando a regra violada.
    """
    if not isinstance(faixa, FaixaParametro):
        raise ValueError(f"Esperado FaixaParametro, recebido {type(faixa).__name__}.")
    if not _PADRAO_VARIAVEL.match(faixa.variavel or ""):
        raise ValueError(
            f"Nome de variável inválido: {faixa.variavel!r}. Use letras minúsculas, números e '_' "
            "(ex.: 'umidade_solo', 'ph_solo')."
        )
    if not str(faixa.unidade or "").strip():
        raise ValueError(f"A variável '{faixa.variavel}' precisa de uma unidade (ex.: '°C', '%').")
    nomes = ("critico_min", "atencao_min", "ideal_min", "ideal_max", "atencao_max", "critico_max")
    valores = [_numero_finito(getattr(faixa, nome), f"{faixa.variavel}.{nome}") for nome in nomes]
    critico_min, atencao_min, ideal_min, ideal_max, atencao_max, critico_max = valores
    if not (critico_min <= atencao_min <= ideal_min < ideal_max <= atencao_max <= critico_max):
        raise ValueError(
            f"Ordem inválida das faixas de '{faixa.variavel}': exige-se "
            "critico_min <= atencao_min <= ideal_min < ideal_max <= atencao_max <= critico_max, "
            f"recebido crítico {critico_min}–{critico_max}, atenção {atencao_min}–{atencao_max}, "
            f"ideal {ideal_min}–{ideal_max}."
        )
    if faixa.variavel in _LIMITES_FISICOS_CADASTRO:
        fisico_min, fisico_max = _LIMITES_FISICOS_CADASTRO[faixa.variavel]
        if ideal_min < fisico_min or ideal_max > fisico_max:
            raise ValueError(
                f"A faixa ideal de '{faixa.variavel}' ({ideal_min}–{ideal_max}) está fora do que o "
                f"sensor consegue medir ({fisico_min}–{fisico_max})."
            )
        if faixa.variavel in _VARIAVEIS_DO_SEMAFORO and not (fisico_min < atencao_min and atencao_max < fisico_max):
            raise ValueError(
                f"As bordas de atenção de '{faixa.variavel}' ({atencao_min}–{atencao_max}) precisam ficar "
                f"estritamente dentro da faixa física do sensor ({fisico_min}–{fisico_max})."
            )


def cadastrar_cultura(
    codigo: str,
    nome_comum: str,
    nome_cientifico: str,
    categoria: str,
    descricao: str,
    faixas: List[FaixaParametro],
    caminho_banco: Path = CAMINHO_BANCO,
) -> int:
    """Cadastra (ou atualiza) uma cultura e suas faixas — sem mudar código.

    Comportamento de *upsert*:
        * se o ``codigo`` não existir, cria a cultura (ativa);
        * se existir, atualiza nome, nome científico, categoria e descrição e
          reativa a cultura;
        * cada faixa é inserida ou atualizada pela chave (cultura, variável);
          variáveis já cadastradas que não aparecem em ``faixas`` são mantidas.

    Tudo ocorre em uma única transação: se uma faixa for inválida, nada é gravado.

    Args:
        codigo: slug em minúsculas (ex.: ``"tomate"``, ``"milho_safrinha"``).
        nome_comum: nome falado ao agricultor (ex.: ``"Tomate"``).
        nome_cientifico: nome botânico.
        categoria: uma de ``CATEGORIAS_CULTURA``.
        descricao: texto livre.
        faixas: faixas por variável; precisam incluir ``VARIAVEIS_OBRIGATORIAS``.
        caminho_banco: banco de destino.

    Returns:
        O ``id_cultura`` da cultura cadastrada/atualizada.

    Raises:
        ValueError: dados ou faixas inválidos.
    """
    codigo_limpo = str(codigo or "").strip()
    if not _PADRAO_CODIGO.match(codigo_limpo):
        raise ValueError(
            f"Código de cultura inválido: {codigo!r}. Use de 2 a 50 caracteres: letras minúsculas sem acento, "
            "números e '_' (ex.: 'tomate', 'milho_safrinha')."
        )
    nome_limpo = str(nome_comum or "").strip()
    if not nome_limpo:
        raise ValueError("O nome comum da cultura é obrigatório.")
    if categoria not in CATEGORIAS_CULTURA:
        raise ValueError(f"Categoria inválida: {categoria!r}. Valores aceitos: {', '.join(CATEGORIAS_CULTURA)}.")
    if not faixas:
        raise ValueError("Informe ao menos as faixas das variáveis obrigatórias.")
    vistas: Set[str] = set()
    for faixa in faixas:
        validar_faixa(faixa)
        if faixa.variavel in vistas:
            raise ValueError(f"A variável '{faixa.variavel}' foi informada mais de uma vez.")
        vistas.add(faixa.variavel)
    faltantes = [variavel for variavel in VARIAVEIS_OBRIGATORIAS if variavel not in vistas]
    if faltantes:
        raise ValueError(f"Faltam faixas obrigatórias para as variáveis: {', '.join(faltantes)}.")

    with _sessao(caminho_banco) as conexao:
        # Upsert explícito (UPDATE ou INSERT) em vez de ON CONFLICT: evita que o
        # AUTOINCREMENT pule números a cada recadastro.
        dados_cultura = (nome_limpo, str(nome_cientifico or "").strip(), categoria, str(descricao or "").strip())
        linha = conexao.execute("SELECT id_cultura FROM culturas WHERE codigo = ?", (codigo_limpo,)).fetchone()
        if linha is None:
            cursor = conexao.execute(
                "INSERT INTO culturas (nome_comum, nome_cientifico, categoria, descricao, ativo, codigo) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (*dados_cultura, codigo_limpo),
            )
            id_cultura = int(cursor.lastrowid or 0)
        else:
            id_cultura = int(linha["id_cultura"])
            conexao.execute(
                "UPDATE culturas SET nome_comum = ?, nome_cientifico = ?, categoria = ?, descricao = ?, ativo = 1 "
                "WHERE id_cultura = ?",
                (*dados_cultura, id_cultura),
            )
        for faixa in faixas:
            limites = (faixa.unidade.strip(), float(faixa.ideal_min), float(faixa.ideal_max),
                       float(faixa.atencao_min), float(faixa.atencao_max), float(faixa.critico_min),
                       float(faixa.critico_max), "Cadastro via cadastrar_cultura")
            atualizado = conexao.execute(
                """
                UPDATE parametros_cultura
                   SET unidade = ?, ideal_min = ?, ideal_max = ?, atencao_min = ?, atencao_max = ?,
                       critico_min = ?, critico_max = ?, fonte_referencia = ?,
                       atualizado_em = datetime('now','localtime')
                 WHERE id_cultura = ? AND variavel = ?
                """,
                (*limites, id_cultura, faixa.variavel),
            )
            if atualizado.rowcount == 0:
                conexao.execute(
                    """
                    INSERT INTO parametros_cultura
                        (unidade, ideal_min, ideal_max, atencao_min, atencao_max, critico_min, critico_max,
                         fonte_referencia, id_cultura, variavel)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*limites, id_cultura, faixa.variavel),
                )
    registrador.info("Cultura '%s' cadastrada/atualizada (id %d, %d variáveis).", codigo_limpo, id_cultura, len(faixas))
    return id_cultura


def listar_culturas(apenas_ativas: bool = True, caminho_banco: Path = CAMINHO_BANCO) -> List[Dict[str, Any]]:
    """Lista as culturas do catálogo, ordenadas por ``id_cultura``.

    Cada item traz as colunas de ``culturas`` e ``qtd_parametros`` (quantas
    variáveis têm faixa cadastrada).
    """
    consulta = """
        SELECT c.id_cultura, c.codigo, c.nome_comum, c.nome_cientifico, c.categoria, c.descricao,
               c.ativo, c.criado_em, COUNT(p.id_parametro) AS qtd_parametros
          FROM culturas c
          LEFT JOIN parametros_cultura p ON p.id_cultura = c.id_cultura
         {filtro}
         GROUP BY c.id_cultura
         ORDER BY c.id_cultura
    """.format(filtro="WHERE c.ativo = 1" if apenas_ativas else "")
    with _sessao(caminho_banco) as conexao:
        return [dict(linha) for linha in conexao.execute(consulta).fetchall()]


def buscar_parametros_cultura(codigo_cultura: str, caminho_banco: Path = CAMINHO_BANCO) -> ParametrosCultura:
    """Carrega a cultura e todas as suas faixas.

    Raises:
        ValueError: se a cultura não existir ou não tiver nenhuma faixa.
    """
    with _sessao(caminho_banco) as conexao:
        cultura = conexao.execute(
            "SELECT id_cultura, codigo, nome_comum, nome_cientifico, categoria FROM culturas WHERE codigo = ?",
            (codigo_cultura,),
        ).fetchone()
        if cultura is None:
            raise ValueError(f"Cultura '{codigo_cultura}' não está cadastrada no banco.")
        linhas = conexao.execute(
            """
            SELECT variavel, unidade, ideal_min, ideal_max, atencao_min, atencao_max, critico_min, critico_max
              FROM parametros_cultura WHERE id_cultura = ? ORDER BY variavel
            """,
            (int(cultura["id_cultura"]),),
        ).fetchall()
    if not linhas:
        raise ValueError(f"Cultura '{codigo_cultura}' não possui faixas cadastradas em parametros_cultura.")
    faixas: Dict[str, FaixaParametro] = {
        str(linha["variavel"]): FaixaParametro(
            variavel=str(linha["variavel"]),
            unidade=str(linha["unidade"]),
            ideal_min=float(linha["ideal_min"]),
            ideal_max=float(linha["ideal_max"]),
            atencao_min=float(linha["atencao_min"]),
            atencao_max=float(linha["atencao_max"]),
            critico_min=float(linha["critico_min"]),
            critico_max=float(linha["critico_max"]),
        )
        for linha in linhas
    }
    return ParametrosCultura(
        id_cultura=int(cultura["id_cultura"]),
        codigo=str(cultura["codigo"]),
        nome_comum=str(cultura["nome_comum"]),
        nome_cientifico=str(cultura["nome_cientifico"] or ""),
        categoria=str(cultura["categoria"]),
        faixas=faixas,
    )


# ---------------------------------------------------------------------------
# Histórico de leituras
# ---------------------------------------------------------------------------
def _valor_bruto(valor: Optional[float]) -> Optional[float]:
    """Converte o valor bruto do sensor para gravação: NaN/None/inválido → NULL."""
    if valor is None or isinstance(valor, bool):
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    return numero if math.isfinite(numero) else None


def _validar_status(valor: Any, nome: str) -> int:
    """Garante que um status seja inteiro 0, 1 ou 2."""
    if isinstance(valor, bool):
        raise ValueError(f"{nome} deve ser 0, 1 ou 2, recebido booleano.")
    try:
        status = int(valor)
    except (TypeError, ValueError) as erro:
        raise ValueError(f"{nome} deve ser 0, 1 ou 2, recebido {valor!r}.") from erro
    if status != valor and not (isinstance(valor, float) and float(status) == valor):
        raise ValueError(f"{nome} deve ser inteiro (0, 1 ou 2), recebido {valor!r}.")
    if status not in NOMES_STATUS:
        raise ValueError(f"{nome} deve ser 0, 1 ou 2, recebido {valor!r}.")
    return status


def _instante_utc_de_data_hora(data_hora: str) -> Optional[str]:
    """Converte a data/hora LOCAL (``YYYY-MM-DD HH:MM:SS`` ou ISO) para ISO UTC com ``+00:00``.

    Texto com fuso explícito é convertido a partir do próprio fuso. Retorna
    ``None`` quando o texto não é uma data válida.
    """
    texto = str(data_hora or "").strip()
    if not texto:
        return None
    try:
        instante = datetime.fromisoformat(texto)
    except ValueError:
        return None
    if instante.tzinfo is None:
        instante = instante.astimezone()  # interpreta como horário local do sistema
    return instante.astimezone(timezone.utc).isoformat(timespec="seconds")


def _normalizar_instante_utc(valor: Optional[str], data_hora: str) -> Optional[str]:
    """``instante_utc`` informado (normalizado para UTC) ou calculado a partir de ``data_hora``.

    Raises:
        ValueError: ``instante_utc`` informado que não é uma data ISO válida.
    """
    if valor is None or not str(valor).strip():
        return _instante_utc_de_data_hora(data_hora)
    texto = str(valor).strip()
    try:
        instante = datetime.fromisoformat(texto)
    except ValueError as erro:
        raise ValueError(f"instante_utc inválido: {valor!r} (use ISO-8601, ex.: 2026-01-01T13:00:00+00:00).") from erro
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=timezone.utc)
    return instante.astimezone(timezone.utc).isoformat(timespec="seconds")


def _validar_registro(registro: RegistroLeitura) -> Dict[str, Any]:
    """Valida um ``RegistroLeitura`` e devolve os valores prontos para o INSERT (sem ``id_cultura``).

    Raises:
        ValueError: registro inválido (origem, status, probabilidade, flag,
            valores não finitos, textos obrigatórios, estado do sensor,
            motivo da regra, base da temperatura).
    """
    if registro.origem not in ORIGENS_LEITURA:
        raise ValueError(f"Origem inválida: {registro.origem!r}. Valores aceitos: {', '.join(ORIGENS_LEITURA)}.")
    status_regra = _validar_status(registro.status_regra, "status_regra")
    status_predito = _validar_status(registro.status_predito, "status_predito")
    status_alerta = _validar_status(registro.status_alerta, "status_alerta")
    probabilidade = _numero_finito(registro.probabilidade_predicao, "probabilidade_predicao")
    if not 0.0 <= probabilidade <= 1.0:
        raise ValueError(f"probabilidade_predicao deve estar entre 0 e 1, recebido {probabilidade}.")
    if registro.flag_anomalia not in (0, 1) or isinstance(registro.flag_anomalia, bool):
        raise ValueError(f"flag_anomalia deve ser 0 ou 1, recebido {registro.flag_anomalia!r}.")
    temperatura = _numero_finito(registro.temperatura_ar, "temperatura_ar")
    umidade_ar = _numero_finito(registro.umidade_ar, "umidade_ar")
    umidade_solo = _numero_finito(registro.umidade_solo, "umidade_solo")
    deficit = _numero_finito(registro.deficit_hidrico_solo, "deficit_hidrico_solo")
    desvio = _numero_finito(registro.desvio_termico, "desvio_termico")
    if not str(registro.data_hora or "").strip():
        raise ValueError("data_hora é obrigatória (formato 'YYYY-MM-DD HH:MM:SS').")
    if not str(registro.mensagem_voz or "").strip():
        raise ValueError("mensagem_voz é obrigatória.")
    if not str(registro.versao_modelo or "").strip():
        raise ValueError("versao_modelo é obrigatória (use 'REGRAS' quando não houver modelo).")
    estado_sensor = str(registro.estado_sensor or ESTADO_SENSOR_OK).strip().upper()
    if estado_sensor not in ESTADOS_SENSOR:
        raise ValueError(f"estado_sensor inválido: {registro.estado_sensor!r}. Valores aceitos: {', '.join(ESTADOS_SENSOR)}.")
    motivo_regra = str(getattr(registro, "motivo_regra", "") or "").strip().upper()
    if motivo_regra and not _PADRAO_MOTIVO_REGRA.match(motivo_regra):
        raise ValueError(f"motivo_regra inválido: {registro.motivo_regra!r} (use um de regras_agronomicas.MOTIVOS_REGRA).")
    base_temperatura = str(getattr(registro, "base_temperatura", "") or "").strip().lower()
    if base_temperatura not in BASES_TEMPERATURA_VALIDAS:
        raise ValueError(f"base_temperatura inválida: {registro.base_temperatura!r}. "
                         f"Valores aceitos: {', '.join(repr(base) for base in BASES_TEMPERATURA_VALIDAS)}.")
    media_24h_bruta = getattr(registro, "temperatura_media_24h", None)
    media_24h = None if media_24h_bruta is None else _numero_finito(media_24h_bruta, "temperatura_media_24h")
    data_hora = str(registro.data_hora).strip()
    return {
        "data_hora": data_hora,
        "origem": registro.origem,
        "temperatura_ar_bruta": _valor_bruto(registro.temperatura_ar_bruta),
        "umidade_ar_bruta": _valor_bruto(registro.umidade_ar_bruta),
        "umidade_solo_bruta": _valor_bruto(registro.umidade_solo_bruta),
        "temperatura_ar": temperatura,
        "umidade_ar": umidade_ar,
        "umidade_solo": umidade_solo,
        "deficit_hidrico_solo": deficit,
        "desvio_termico": desvio,
        "status_regra": status_regra,
        "status_predito": status_predito,
        "status_alerta": status_alerta,
        "probabilidade_predicao": probabilidade,
        "flag_anomalia": int(registro.flag_anomalia),
        "descricao_anomalia": str(registro.descricao_anomalia or ""),
        "mensagem_voz": str(registro.mensagem_voz).strip(),
        "versao_modelo": str(registro.versao_modelo).strip(),
        "estado_sensor": estado_sensor,
        "diagnostico_sensor": str(registro.diagnostico_sensor or ""),
        "instante_utc": _normalizar_instante_utc(registro.instante_utc, data_hora),
        "motivo_regra": motivo_regra,
        "base_temperatura": base_temperatura,
        "temperatura_media_24h": media_24h,
        "umidade_solo_firmware": _valor_bruto(getattr(registro, "umidade_solo_firmware", None)),
    }


_COLUNAS_INSERT_HISTORICO: Tuple[str, ...] = (
    "data_hora", "id_cultura", "origem",
    "temperatura_ar_bruta", "umidade_ar_bruta", "umidade_solo_bruta",
    "temperatura_ar", "umidade_ar", "umidade_solo",
    "deficit_hidrico_solo", "desvio_termico",
    "status_regra", "status_predito", "status_alerta", "probabilidade_predicao",
    "flag_anomalia", "descricao_anomalia", "mensagem_voz", "versao_modelo",
    "estado_sensor", "diagnostico_sensor", "instante_utc",
    "motivo_regra", "base_temperatura", "temperatura_media_24h", "umidade_solo_firmware",
)
_SQL_INSERT_HISTORICO: str = (
    f"INSERT INTO historico_leituras ({', '.join(_COLUNAS_INSERT_HISTORICO)}) "
    f"VALUES ({', '.join(':' + coluna for coluna in _COLUNAS_INSERT_HISTORICO)})"
)


def _id_cultura(conexao: sqlite3.Connection, codigo_cultura: str, cache: Dict[str, int]) -> int:
    """``id_cultura`` do código (com cache por transação).

    Raises:
        ValueError: cultura inexistente.
    """
    if codigo_cultura not in cache:
        linha = conexao.execute("SELECT id_cultura FROM culturas WHERE codigo = ?", (codigo_cultura,)).fetchone()
        if linha is None:
            raise ValueError(f"Cultura '{codigo_cultura}' não está cadastrada no banco.")
        cache[codigo_cultura] = int(linha["id_cultura"])
    return cache[codigo_cultura]


def _upsert_configuracoes(conexao: sqlite3.Connection, configuracoes: Optional[Dict[str, Any]]) -> None:
    """Grava várias configurações na transação aberta em ``conexao``.

    Chave existente: o valor é validado contra o ``tipo`` declarado; se for
    incompatível, a chave é pulada com aviso no log (uma configuração de
    telemetria nunca derruba a gravação da leitura). Chave nova: criada com o
    tipo inferido do valor Python.
    """
    if not configuracoes:
        return
    for chave, valor in configuracoes.items():
        chave_limpa = str(chave or "").strip()
        if not chave_limpa:
            registrador.warning("Configuração extra com chave vazia ignorada.")
            continue
        linha = conexao.execute("SELECT tipo FROM configuracao_sistema WHERE chave = ?", (chave_limpa,)).fetchone()
        if linha is not None:
            tipo = str(linha["tipo"])
            try:
                texto = _serializar_valor_configuracao(valor, tipo)
            except ValueError as erro:
                registrador.warning("Configuração extra '%s'=%r incompatível com o tipo %s (%s); ignorada.",
                                    chave_limpa, valor, tipo, erro)
                continue
            conexao.execute(
                "UPDATE configuracao_sistema SET valor = ?, atualizado_em = datetime('now','localtime') WHERE chave = ?",
                (texto, chave_limpa),
            )
            continue
        tipo = _inferir_tipo(valor)
        try:
            texto = _serializar_valor_configuracao(valor, tipo)
        except ValueError as erro:
            registrador.warning("Configuração extra '%s'=%r inválida (%s); ignorada.", chave_limpa, valor, erro)
            continue
        conexao.execute(
            "INSERT INTO configuracao_sistema (chave, valor, tipo, descricao) VALUES (?, ?, ?, ?)",
            (chave_limpa, texto, tipo, "Configuração criada em tempo de execução."),
        )


def inserir_leitura(
    registro: RegistroLeitura,
    caminho_banco: Path = CAMINHO_BANCO,
    configuracoes_extras: Optional[Dict[str, Any]] = None,
) -> int:
    """Grava uma leitura sanitizada no histórico.

    Valida origem, status (0/1/2), probabilidade (0–1), flag (0/1), estado do
    sensor e valores sanitizados finitos; converte NaN dos valores brutos em
    NULL. Sem ``instante_utc`` no registro, ele é calculado a partir de
    ``data_hora`` (horário local).

    Args:
        registro: leitura a gravar.
        caminho_banco: banco de destino.
        configuracoes_extras: configurações (``{chave: valor}``) gravadas na
            MESMA transação da leitura (ex.: batimento do coletor). Ou tudo é
            gravado, ou nada.

    Returns:
        ``id_leitura`` gerado.

    Raises:
        ValueError: registro inválido ou cultura inexistente.
        sqlite3.OperationalError: banco travado por mais de ``TEMPO_ESPERA_TRAVA_MS``.
    """
    return inserir_leituras_em_lote([registro], caminho_banco=caminho_banco,
                                    configuracoes_extras=configuracoes_extras)[0]


def inserir_leituras_em_lote(
    registros: Sequence[RegistroLeitura],
    caminho_banco: Path = CAMINHO_BANCO,
    configuracoes_extras: Optional[Dict[str, Any]] = None,
) -> List[int]:
    """Grava várias leituras numa ÚNICA transação (todas ou nenhuma).

    Usada pelo coletor para despejar a fila de leituras que não puderam ser
    gravadas enquanto o banco estava travado, mantendo a ``data_hora``
    original de cada uma.

    Returns:
        Os ``id_leitura`` gerados, na ordem dos registros.

    Raises:
        ValueError: algum registro inválido (nada é gravado).
        sqlite3.OperationalError: banco travado.
    """
    validados = [_validar_registro(registro) for registro in registros]
    ids: List[int] = []
    with _sessao(caminho_banco) as conexao:
        cache: Dict[str, int] = {}
        for registro, valores in zip(registros, validados):
            valores["id_cultura"] = _id_cultura(conexao, registro.codigo_cultura, cache)
            cursor = conexao.execute(_SQL_INSERT_HISTORICO, valores)
            if cursor.lastrowid is None:
                raise RuntimeError("O SQLite não retornou o id da leitura inserida.")
            ids.append(int(cursor.lastrowid))
        _upsert_configuracoes(conexao, configuracoes_extras)
    return ids


_CONSULTA_LEITURAS: str = """
    SELECT h.*, c.codigo AS codigo_cultura, c.nome_comum AS nome_cultura
      FROM historico_leituras h
      JOIN culturas c ON c.id_cultura = h.id_cultura
"""


def buscar_ultima_leitura(
    codigo_cultura: Optional[str] = None, caminho_banco: Path = CAMINHO_BANCO
) -> Optional[Dict[str, Any]]:
    """Retorna a leitura mais recente (opcionalmente de uma cultura) ou ``None``.

    O dicionário traz todas as colunas de ``historico_leituras`` mais
    ``codigo_cultura`` e ``nome_cultura``.
    """
    historico = buscar_historico(limite=1, codigo_cultura=codigo_cultura, caminho_banco=caminho_banco)
    return historico[0] if historico else None


def buscar_historico(
    limite: int = 24, codigo_cultura: Optional[str] = None, caminho_banco: Path = CAMINHO_BANCO
) -> List[Dict[str, Any]]:
    """Retorna as últimas ``limite`` leituras, da mais recente para a mais antiga.

    A ordem é a de GRAVAÇÃO (``id_leitura``), e não ``data_hora``: um relógio
    adiantado por engano não deixa uma leitura velha "presa" no topo.

    Raises:
        ValueError: se ``limite`` não for um inteiro positivo.
    """
    if isinstance(limite, bool) or not isinstance(limite, int) or limite < 1:
        raise ValueError(f"limite deve ser um inteiro positivo, recebido {limite!r}.")
    parametros: List[Any] = []
    filtro = ""
    if codigo_cultura is not None:
        filtro = "WHERE c.codigo = ?"
        parametros.append(codigo_cultura)
    parametros.append(limite)
    consulta = f"{_CONSULTA_LEITURAS} {filtro} ORDER BY h.id_leitura DESC LIMIT ?"
    with _sessao(caminho_banco) as conexao:
        return [dict(linha) for linha in conexao.execute(consulta, parametros).fetchall()]


def _idade_leitura_segundos(leitura: Dict[str, Any], agora_utc: Optional[datetime] = None) -> Optional[float]:
    """Idade da leitura em segundos, pelo ``instante_utc`` (ou pela ``data_hora`` local, se faltar).

    Retorna ``None`` se nenhum dos dois for uma data válida; negativa se a
    leitura estiver "no futuro" (relógio ajustado para trás depois da gravação).
    """
    texto = leitura.get("instante_utc") or _instante_utc_de_data_hora(str(leitura.get("data_hora") or ""))
    if not texto:
        return None
    try:
        instante = datetime.fromisoformat(str(texto))
    except ValueError:
        return None
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=timezone.utc)
    referencia = agora_utc or datetime.now(timezone.utc)
    return (referencia - instante).total_seconds()


def buscar_ultima_leitura_origem(
    codigo_cultura: str,
    origem: str,
    idade_maxima_segundos: float,
    caminho_banco: Path = CAMINHO_BANCO,
) -> Optional[Dict[str, Any]]:
    """Última leitura gravada (ordem de gravação) de uma cultura e origem, se for recente.

    Usada pelo coletor para semear o saneador do campo ao (re)conectar o
    Arduino: a referência passa a ser a última medição real, e não o ponto
    médio da faixa ideal.

    Returns:
        O dicionário da leitura (mesmas chaves de ``buscar_historico``) ou
        ``None`` se não houver leitura, se ela for mais antiga que
        ``idade_maxima_segundos`` ou se estiver mais de 60 s "no futuro"
        (relógio inconsistente).
    """
    consulta = f"{_CONSULTA_LEITURAS} WHERE c.codigo = ? AND h.origem = ? ORDER BY h.id_leitura DESC LIMIT 1"
    with _sessao(caminho_banco) as conexao:
        linha = conexao.execute(consulta, (codigo_cultura, origem)).fetchone()
    if linha is None:
        return None
    leitura = dict(linha)
    idade = _idade_leitura_segundos(leitura)
    if idade is None or idade > float(idade_maxima_segundos) or idade < -60.0:
        return None
    return leitura


# ---------------------------------------------------------------------------
# Configurações do sistema
# ---------------------------------------------------------------------------
def _converter_valor_configuracao(valor: str, tipo: str) -> Any:
    """Converte o texto armazenado para o tipo Python declarado na coluna ``tipo``.

    Raises:
        ValueError: se o texto não puder ser convertido.
    """
    if tipo == "int":
        numero = float(valor)
        if not numero.is_integer():
            raise ValueError(f"valor {valor!r} não é inteiro")
        return int(numero)
    if tipo == "float":
        numero_real = float(valor)
        if not math.isfinite(numero_real):
            raise ValueError(f"valor {valor!r} não é finito")
        return numero_real
    if tipo == "bool":
        texto = valor.strip().lower()
        if texto in ("true", "1", "sim", "yes", "verdadeiro"):
            return True
        if texto in ("false", "0", "nao", "não", "no", "falso", ""):
            return False
        raise ValueError(f"valor {valor!r} não é booleano")
    return valor


def _serializar_valor_configuracao(valor: Any, tipo: str) -> str:
    """Converte um valor Python para o texto gravado, validando contra o tipo.

    Raises:
        ValueError: se o valor não for compatível com o tipo da chave.
    """
    if valor is None:
        if tipo in ("int", "float"):
            raise ValueError("chaves numéricas não aceitam valor vazio")
        return "false" if tipo == "bool" else ""
    if tipo == "bool":
        if isinstance(valor, bool):
            return "true" if valor else "false"
        return "true" if _converter_valor_configuracao(str(valor), "bool") else "false"
    if tipo == "int":
        if isinstance(valor, bool):
            raise ValueError("chave inteira não aceita booleano")
        return str(_converter_valor_configuracao(str(valor), "int"))
    if tipo == "float":
        if isinstance(valor, bool):
            raise ValueError("chave real não aceita booleano")
        return repr(_converter_valor_configuracao(str(valor), "float"))
    if isinstance(valor, bool):
        return "true" if valor else "false"
    return str(valor)


def _inferir_tipo(valor: Any) -> str:
    """Deduz o tipo de uma chave nova a partir do valor Python."""
    if isinstance(valor, bool):
        return "bool"
    if isinstance(valor, int):
        return "int"
    if isinstance(valor, float):
        return "float"
    return "str"


def obter_configuracao(chave: str, padrao: Optional[str] = None, caminho_banco: Path = CAMINHO_BANCO) -> Optional[str]:
    """Lê o valor textual de uma configuração; ``padrao`` se a chave não existir."""
    with _sessao(caminho_banco) as conexao:
        linha = conexao.execute("SELECT valor FROM configuracao_sistema WHERE chave = ?", (chave,)).fetchone()
    return padrao if linha is None else str(linha["valor"])


def obter_configuracao_tipada(chave: str, padrao: Any = None, caminho_banco: Path = CAMINHO_BANCO) -> Any:
    """Lê uma configuração já convertida segundo a coluna ``tipo``.

    Retorna ``padrao`` se a chave não existir, se o valor numérico estiver vazio
    ou se a conversão falhar (neste caso registra um aviso no log).
    """
    with _sessao(caminho_banco) as conexao:
        linha = conexao.execute("SELECT valor, tipo FROM configuracao_sistema WHERE chave = ?", (chave,)).fetchone()
    if linha is None:
        return padrao
    valor, tipo = str(linha["valor"]), str(linha["tipo"])
    if tipo in ("int", "float") and not valor.strip():
        return padrao
    try:
        return _converter_valor_configuracao(valor, tipo)
    except ValueError as erro:
        registrador.warning("Configuração '%s' com valor inválido para o tipo %s (%s); usando padrão.", chave, tipo, erro)
        return padrao


def definir_configuracao(chave: str, valor: Any, caminho_banco: Path = CAMINHO_BANCO) -> None:
    """Grava (upsert) uma configuração.

    Se a chave existir, preserva ``tipo`` e ``descricao`` e valida o valor contra
    o tipo; se não existir, cria com o tipo inferido do valor Python. Booleanos
    são gravados como ``'true'``/``'false'``.

    Raises:
        ValueError: chave vazia ou valor incompatível com o tipo da chave.
    """
    chave_limpa = str(chave or "").strip()
    if not chave_limpa:
        raise ValueError("A chave de configuração não pode ser vazia.")
    with _sessao(caminho_banco) as conexao:
        linha = conexao.execute("SELECT tipo FROM configuracao_sistema WHERE chave = ?", (chave_limpa,)).fetchone()
        if linha is not None:
            tipo = str(linha["tipo"])
            try:
                texto = _serializar_valor_configuracao(valor, tipo)
            except ValueError as erro:
                raise ValueError(f"Valor {valor!r} inválido para a configuração '{chave_limpa}' ({tipo}): {erro}.") from erro
            conexao.execute(
                "UPDATE configuracao_sistema SET valor = ?, atualizado_em = datetime('now','localtime') WHERE chave = ?",
                (texto, chave_limpa),
            )
        else:
            tipo = _inferir_tipo(valor)
            texto = _serializar_valor_configuracao(valor, tipo)
            conexao.execute(
                "INSERT INTO configuracao_sistema (chave, valor, tipo, descricao) VALUES (?, ?, ?, ?)",
                (chave_limpa, texto, tipo, "Configuração criada em tempo de execução."),
            )


def definir_configuracoes(valores: Dict[str, Any], caminho_banco: Path = CAMINHO_BANCO) -> None:
    """Grava várias configurações numa única transação (mesmas regras de ``configuracoes_extras``).

    Chaves existentes preservam ``tipo`` e ``descricao`` (valor incompatível é
    pulado com aviso); chaves novas recebem o tipo inferido do valor Python.
    """
    with _sessao(caminho_banco) as conexao:
        _upsert_configuracoes(conexao, valores)


def obter_todas_configuracoes(caminho_banco: Path = CAMINHO_BANCO) -> Dict[str, Any]:
    """Retorna todas as configurações como ``{chave: valor_tipado}``.

    Valores numéricos vazios ou inválidos viram ``None``.
    """
    with _sessao(caminho_banco) as conexao:
        linhas = conexao.execute("SELECT chave, valor, tipo FROM configuracao_sistema ORDER BY chave").fetchall()
    configuracoes: Dict[str, Any] = {}
    for linha in linhas:
        valor, tipo = str(linha["valor"]), str(linha["tipo"])
        if tipo in ("int", "float") and not valor.strip():
            configuracoes[str(linha["chave"])] = None
            continue
        try:
            configuracoes[str(linha["chave"])] = _converter_valor_configuracao(valor, tipo)
        except ValueError:
            registrador.warning("Configuração '%s' com valor inválido para o tipo %s.", linha["chave"], tipo)
            configuracoes[str(linha["chave"])] = None
    return configuracoes


def encerrar_demonstracao_se_inalterada(
    cenario_avaliado: str,
    expira_em_avaliada: str,
    modo_destino: str,
    caminho_banco: Path = CAMINHO_BANCO,
) -> bool:
    """Encerra o Modo Demonstração de forma ATÔMICA, só se ela ainda for a mesma que foi avaliada.

    O coletor e o servidor Flask encerram, cada um por conta própria, uma
    demonstração vencida. Entre ler a configuração e gravar o encerramento, o
    aplicativo pode ter iniciado uma NOVA demonstração; um encerramento cego
    apagaria a demonstração recém-iniciada. Por isso a conferência do cenário
    e da expiração e a limpeza ocorrem na mesma transação (``BEGIN IMMEDIATE``).

    Args:
        cenario_avaliado: valor de ``cenario_demonstracao`` lido por quem decidiu encerrar.
        expira_em_avaliada: valor de ``demonstracao_expira_em`` lido por quem decidiu encerrar.
        modo_destino: ``CAMPO_USB`` ou ``SIMULACAO``.
        caminho_banco: banco de destino.

    Returns:
        ``True`` se encerrou (cenário e expiração limpos e ``modo_operacao``
        gravado); ``False`` se outra demonstração foi iniciada nesse meio-tempo
        (nada é alterado).

    Raises:
        ValueError: ``modo_destino`` inválido.
    """
    if modo_destino not in (MODO_CAMPO_USB, MODO_SIMULACAO):
        raise ValueError(f"Modo de destino inválido: {modo_destino!r}. Use {MODO_CAMPO_USB} ou {MODO_SIMULACAO}.")
    with _sessao(caminho_banco) as conexao:
        conexao.execute("BEGIN IMMEDIATE")
        linhas = conexao.execute(
            "SELECT chave, valor FROM configuracao_sistema WHERE chave IN ('cenario_demonstracao', 'demonstracao_expira_em')"
        ).fetchall()
        atuais = {str(linha["chave"]): str(linha["valor"]) for linha in linhas}
        if (atuais.get("cenario_demonstracao", "") != str(cenario_avaliado or "")
                or atuais.get("demonstracao_expira_em", "") != str(expira_em_avaliada or "")):
            return False
        for chave, valor in (("cenario_demonstracao", ""), ("demonstracao_expira_em", ""), ("modo_operacao", modo_destino)):
            conexao.execute(
                "UPDATE configuracao_sistema SET valor = ?, atualizado_em = datetime('now','localtime') WHERE chave = ?",
                (valor, chave),
            )
    return True


def definir_configuracao_condicional(
    chave: str,
    valor: Any,
    exigir_valor_atual: Optional[str] = None,
    proibir_valor_atual: Optional[str] = None,
    caminho_banco: Path = CAMINHO_BANCO,
) -> bool:
    """Grava uma configuração JÁ EXISTENTE só se o valor atual atender à condição, de forma ATÔMICA.

    A conferência e a gravação acontecem num único ``UPDATE ... WHERE`` (o
    SQLite executa cada comando de forma atômica). Isso fecha a janela de
    corrida entre "ler o modo" e "gravar o modo": por exemplo, o coletor só
    grava ``modo_operacao = SIMULACAO`` se o aplicativo NÃO tiver acabado de
    gravar ``DEMONSTRACAO``.

    Args:
        chave: configuração existente (ex.: ``modo_operacao``).
        valor: novo valor (validado contra o tipo da chave).
        exigir_valor_atual: se informado, só grava quando o valor atual for exatamente este.
        proibir_valor_atual: se informado, só grava quando o valor atual for diferente deste.
        caminho_banco: banco de destino.

    Returns:
        ``True`` se gravou; ``False`` se a condição não foi atendida ou a chave não existe.

    Raises:
        ValueError: chave vazia ou valor incompatível com o tipo da chave.
    """
    chave_limpa = str(chave or "").strip()
    if not chave_limpa:
        raise ValueError("A chave de configuração não pode ser vazia.")
    with _sessao(caminho_banco) as conexao:
        linha = conexao.execute("SELECT tipo FROM configuracao_sistema WHERE chave = ?", (chave_limpa,)).fetchone()
        if linha is None:
            return False
        tipo = str(linha["tipo"])
        try:
            texto = _serializar_valor_configuracao(valor, tipo)
        except ValueError as erro:
            raise ValueError(f"Valor {valor!r} inválido para a configuração '{chave_limpa}' ({tipo}): {erro}.") from erro
        comando = ("UPDATE configuracao_sistema SET valor = ?, atualizado_em = datetime('now','localtime') "
                   "WHERE chave = ?")
        argumentos: List[Any] = [texto, chave_limpa]
        if exigir_valor_atual is not None:
            comando += " AND valor = ?"
            argumentos.append(str(exigir_valor_atual))
        if proibir_valor_atual is not None:
            comando += " AND valor <> ?"
            argumentos.append(str(proibir_valor_atual))
        cursor = conexao.execute(comando, tuple(argumentos))
        return int(cursor.rowcount) > 0


# ---------------------------------------------------------------------------
# Retenção, agregação horária, backup e compactação
# ---------------------------------------------------------------------------
_SQL_AGREGAR_LOTE: str = """
    INSERT INTO historico_agregado_hora (
        id_cultura, hora, origem, n,
        temperatura_min, temperatura_med, temperatura_max,
        umidade_ar_min, umidade_ar_med, umidade_ar_max,
        umidade_solo_min, umidade_solo_med, umidade_solo_max,
        status_alerta_max, n_verde, n_amarelo, n_vermelho, n_anomalias
    )
    SELECT h.id_cultura, strftime('%Y-%m-%d %H:00:00', h.data_hora) AS hora, h.origem, COUNT(*),
           MIN(h.temperatura_ar), AVG(h.temperatura_ar), MAX(h.temperatura_ar),
           MIN(h.umidade_ar), AVG(h.umidade_ar), MAX(h.umidade_ar),
           MIN(h.umidade_solo), AVG(h.umidade_solo), MAX(h.umidade_solo),
           MAX(h.status_alerta),
           SUM(CASE WHEN h.status_alerta = 0 THEN 1 ELSE 0 END),
           SUM(CASE WHEN h.status_alerta = 1 THEN 1 ELSE 0 END),
           SUM(CASE WHEN h.status_alerta = 2 THEN 1 ELSE 0 END),
           SUM(h.flag_anomalia)
      FROM historico_leituras h
      JOIN temp._lote_retencao l ON l.id_leitura = h.id_leitura
     GROUP BY h.id_cultura, hora, h.origem
    ON CONFLICT (id_cultura, hora, origem) DO UPDATE SET
        temperatura_min  = MIN(temperatura_min, excluded.temperatura_min),
        temperatura_max  = MAX(temperatura_max, excluded.temperatura_max),
        temperatura_med  = (temperatura_med * n + excluded.temperatura_med * excluded.n) / (n + excluded.n),
        umidade_ar_min   = MIN(umidade_ar_min, excluded.umidade_ar_min),
        umidade_ar_max   = MAX(umidade_ar_max, excluded.umidade_ar_max),
        umidade_ar_med   = (umidade_ar_med * n + excluded.umidade_ar_med * excluded.n) / (n + excluded.n),
        umidade_solo_min = MIN(umidade_solo_min, excluded.umidade_solo_min),
        umidade_solo_max = MAX(umidade_solo_max, excluded.umidade_solo_max),
        umidade_solo_med = (umidade_solo_med * n + excluded.umidade_solo_med * excluded.n) / (n + excluded.n),
        status_alerta_max = MAX(status_alerta_max, excluded.status_alerta_max),
        n_verde     = n_verde + excluded.n_verde,
        n_amarelo   = n_amarelo + excluded.n_amarelo,
        n_vermelho  = n_vermelho + excluded.n_vermelho,
        n_anomalias = n_anomalias + excluded.n_anomalias,
        n           = n + excluded.n
"""


def _texto_data_hora(instante: datetime) -> str:
    """Formata no padrão de ``historico_leituras.data_hora`` (horário local, sem fuso)."""
    if instante.tzinfo is not None:
        instante = instante.astimezone().replace(tzinfo=None)
    return instante.strftime(FORMATO_DATA_HORA_BANCO)


def _processar_lotes(
    conexao: sqlite3.Connection, filtro_sql: str, parametros: Tuple[Any, ...], tamanho_lote: int, agregar: bool
) -> int:
    """Agrega (opcionalmente) e apaga, em lotes de ``tamanho_lote`` por transação, as linhas do filtro.

    Cada lote é ATÔMICO: agregar e apagar acontecem na mesma transação. Se o
    processo cair no meio, nada é contado duas vezes (as linhas agregadas já
    foram apagadas no mesmo COMMIT) e nada é perdido.

    Returns:
        Quantidade de linhas apagadas.
    """
    total = 0
    conexao.execute("CREATE TEMP TABLE IF NOT EXISTS _lote_retencao (id_leitura INTEGER PRIMARY KEY)")
    while True:
        conexao.execute("BEGIN IMMEDIATE")
        try:
            conexao.execute("DELETE FROM temp._lote_retencao")
            conexao.execute(
                f"INSERT INTO temp._lote_retencao (id_leitura) SELECT id_leitura FROM historico_leituras "
                f"WHERE {filtro_sql} ORDER BY id_leitura LIMIT ?",
                (*parametros, int(tamanho_lote)),
            )
            quantidade = int(conexao.execute("SELECT COUNT(*) FROM temp._lote_retencao").fetchone()[0])
            if quantidade == 0:
                conexao.execute("COMMIT")
                return total
            if agregar:
                conexao.execute(_SQL_AGREGAR_LOTE)
            apagadas = conexao.execute(
                "DELETE FROM historico_leituras WHERE id_leitura IN (SELECT id_leitura FROM temp._lote_retencao)"
            ).rowcount
            conexao.execute("COMMIT")
        except Exception:
            conexao.execute("ROLLBACK")
            raise
        total += int(apagadas)


def aplicar_retencao(
    dias_brutos: int = 30,
    caminho_banco: Path = CAMINHO_BANCO,
    agora: Optional[datetime] = None,
    tamanho_lote: int = TAMANHO_LOTE_RETENCAO,
) -> Dict[str, int]:
    """Mantém o banco com tamanho limitado sem perder o histórico de longo prazo.

    1. As leituras brutas de HORAS COMPLETAS anteriores ao corte
       (``agora - dias_brutos``, arredondado para o início da hora) viram uma
       linha por (cultura, hora, origem) em ``historico_agregado_hora`` — com
       mínimo, média e máximo de cada variável, pior status e contagem por cor
       e de anomalias — e são apagadas. Se a hora já tiver agregado (retenção
       anterior), os valores são somados/combinados (upsert).
    2. As leituras ``DEMONSTRACAO`` com mais de 1 dia são apagadas sem agregar
       (são cenários de banca, não medições).
    3. ``PRAGMA wal_checkpoint(TRUNCATE)`` e ``PRAGMA incremental_vacuum``
       devolvem o espaço ao disco.

    Tudo em lotes de ``tamanho_lote`` linhas por transação, para não segurar a
    trava do banco por muito tempo enquanto o coletor e o servidor trabalham.
    Tabelas filhas com ``FOREIGN KEY ... ON DELETE CASCADE`` (ex.: análises de
    antecipação) perdem as linhas junto, porque ``obter_conexao`` liga as
    chaves estrangeiras.

    Args:
        dias_brutos: dias de leituras brutas a manter (>= 1).
        caminho_banco: banco alvo.
        agora: instante de referência (horário local; padrão: agora).
        tamanho_lote: linhas por transação.

    Returns:
        ``{'agregadas': linhas brutas agregadas e apagadas,
        'apagadas': total de linhas apagadas, 'demonstracao_apagadas': ...}``.

    Raises:
        ValueError: ``dias_brutos`` ou ``tamanho_lote`` inválidos.
    """
    if isinstance(dias_brutos, bool) or int(dias_brutos) < 1:
        raise ValueError(f"dias_brutos deve ser inteiro >= 1, recebido {dias_brutos!r}.")
    if isinstance(tamanho_lote, bool) or int(tamanho_lote) < 1:
        raise ValueError(f"tamanho_lote deve ser inteiro >= 1, recebido {tamanho_lote!r}.")
    referencia = agora or datetime.now()
    if referencia.tzinfo is not None:
        referencia = referencia.astimezone().replace(tzinfo=None)
    corte = (referencia - timedelta(days=int(dias_brutos))).replace(minute=0, second=0, microsecond=0)
    corte_demonstracao = referencia - timedelta(days=DIAS_RETENCAO_DEMONSTRACAO)
    _garantir_banco(caminho_banco)
    conexao = obter_conexao(caminho_banco)
    conexao.isolation_level = None  # transações explícitas por lote
    try:
        agregadas = _processar_lotes(
            conexao, "data_hora < ? AND origem <> ?", (_texto_data_hora(corte), MODO_DEMONSTRACAO),
            int(tamanho_lote), agregar=True,
        )
        demonstracao = _processar_lotes(
            conexao, "data_hora < ? AND origem = ?", (_texto_data_hora(corte_demonstracao), MODO_DEMONSTRACAO),
            int(tamanho_lote), agregar=False,
        )
        conexao.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        conexao.execute("PRAGMA incremental_vacuum").fetchall()
    finally:
        conexao.close()
    resultado = {"agregadas": agregadas, "apagadas": agregadas + demonstracao, "demonstracao_apagadas": demonstracao}
    if resultado["apagadas"]:
        registrador.info("Retenção (%d dias): %d leitura(s) agregadas por hora e apagadas, %d de demonstração apagadas.",
                         int(dias_brutos), agregadas, demonstracao)
    return resultado


def fazer_backup(destino: Path, caminho_banco: Path = CAMINHO_BANCO) -> Path:
    """Cópia consistente do banco, segura com WAL e com o coletor gravando.

    Usa a API de backup online do SQLite (``sqlite3.Connection.backup``):
    a cópia reflete um instante consistente, sem travar os gravadores por mais
    que alguns milissegundos por etapa. Grava num arquivo temporário ao lado do
    destino e o renomeia no final (nunca deixa um backup pela metade com o nome
    final). O arquivo gerado é autossuficiente (``journal_mode=DELETE``).

    Args:
        destino: arquivo de destino (ou pasta: o nome vira ``agroanalise_AAAAMMDD_HHMMSS.db``).
        caminho_banco: banco de origem.

    Returns:
        O caminho do backup gerado.
    """
    _garantir_banco(caminho_banco)
    alvo = Path(destino).expanduser()
    if alvo.is_dir():
        alvo = alvo / f"agroanalise_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    alvo.parent.mkdir(parents=True, exist_ok=True)
    temporario = alvo.with_name(alvo.name + ".parcial")
    if temporario.exists():
        temporario.unlink()
    origem = obter_conexao(caminho_banco)
    try:
        copia = sqlite3.connect(str(temporario))
        try:
            origem.backup(copia, pages=256)
            copia.execute("PRAGMA journal_mode = DELETE").fetchall()
            situacao = copia.execute("PRAGMA integrity_check").fetchone()[0]
            if situacao != "ok":
                raise sqlite3.DatabaseError(f"Backup inconsistente: integrity_check = {situacao!r}.")
        finally:
            copia.close()
    finally:
        origem.close()
    os.replace(temporario, alvo)
    registrador.info("Backup do banco gravado em %s.", alvo)
    return alvo


def compactar_banco(caminho_banco: Path = CAMINHO_BANCO) -> Dict[str, int]:
    """Compactação manual: checkpoint do WAL + ``VACUUM`` (reescreve o arquivo inteiro).

    Use fora do horário de coleta intensa: o ``VACUUM`` segura a trava de
    escrita enquanto reescreve o banco.

    Returns:
        ``{'bytes_antes': ..., 'bytes_depois': ...}`` (tamanho do arquivo principal).
    """
    _garantir_banco(caminho_banco)
    caminho = Path(caminho_banco)
    antes = caminho.stat().st_size if caminho.exists() else 0
    conexao = obter_conexao(caminho)
    conexao.isolation_level = None
    try:
        conexao.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        conexao.execute("VACUUM")
        conexao.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    finally:
        conexao.close()
    depois = caminho.stat().st_size if caminho.exists() else 0
    registrador.info("Banco compactado: %d → %d bytes.", antes, depois)
    return {"bytes_antes": int(antes), "bytes_depois": int(depois)}


# ---------------------------------------------------------------------------
# Execução direta
# ---------------------------------------------------------------------------
def _imprimir_resumo(caminho_banco: Path = CAMINHO_BANCO) -> None:
    """Imprime no terminal o conteúdo essencial do banco."""
    print("=" * 78)
    print(f"AgroAnálise — banco de dados: {Path(caminho_banco)}")
    print("=" * 78)
    culturas = listar_culturas(apenas_ativas=False, caminho_banco=caminho_banco)
    print(f"\nCulturas cadastradas: {len(culturas)}")
    for cultura in culturas:
        situacao = "ativa" if int(cultura["ativo"]) == 1 else "inativa"
        print(f"  [{cultura['id_cultura']}] {cultura['codigo']:<16} {cultura['nome_comum']:<16} "
              f"({cultura['nome_cientifico']}) categoria={cultura['categoria']} — {situacao}, "
              f"{cultura['qtd_parametros']} parâmetros")
        parametros = buscar_parametros_cultura(str(cultura["codigo"]), caminho_banco=caminho_banco)
        for faixa in parametros.faixas.values():
            print(f"      - {faixa.variavel:<15} ({faixa.unidade:>2})  ideal {faixa.ideal_min:g}–{faixa.ideal_max:g} | "
                  f"atenção {faixa.atencao_min:g}–{faixa.atencao_max:g} | crítico {faixa.critico_min:g}–{faixa.critico_max:g}")
    print("\nConfigurações do sistema:")
    for chave, valor in obter_todas_configuracoes(caminho_banco=caminho_banco).items():
        print(f"  {chave:<32} = {valor!r}")
    with _sessao(caminho_banco) as conexao:
        total = conexao.execute("SELECT COUNT(*) AS total FROM historico_leituras").fetchone()["total"]
        modo = conexao.execute("PRAGMA journal_mode").fetchone()[0]
    print(f"\nLeituras no histórico: {total}")
    print(f"Modo de journal do SQLite: {modo}")


def _criar_parser() -> argparse.ArgumentParser:
    """Monta o parser da linha de comando do módulo de banco."""
    parser = argparse.ArgumentParser(
        prog="database.py",
        description="AgroAnálise — inicializa o banco e oferece backup, compactação e retenção do histórico.",
    )
    parser.add_argument("--banco", type=Path, default=CAMINHO_BANCO, help=f"Banco SQLite (padrão: {CAMINHO_BANCO}).")
    parser.add_argument("--backup", type=Path, default=None, metavar="DESTINO",
                        help="Gera uma cópia consistente do banco em DESTINO (arquivo ou pasta).")
    parser.add_argument("--compactar", action="store_true", help="Executa VACUUM manual (reescreve o arquivo).")
    parser.add_argument("--reter", type=int, default=None, metavar="DIAS",
                        help="Agrega por hora e apaga as leituras brutas com mais de DIAS dias.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Ponto de entrada da linha de comando.

    Sem opções: inicializa o banco e imprime o resumo (comportamento da v1).

    Returns:
        0 em sucesso, 2 para argumentos inválidos, 1 para falha de banco.
    """
    argumentos = _criar_parser().parse_args(argv)
    caminho = Path(argumentos.banco)
    try:
        inicializar_banco(caminho)
        semear_configuracoes_confiabilidade(caminho)
        semear_configuracoes_v3(caminho)
        acao_executada = False
        if argumentos.reter is not None:
            if argumentos.reter < 1:
                registrador.error("--reter precisa ser pelo menos 1 dia.")
                return 2
            resultado = aplicar_retencao(dias_brutos=argumentos.reter, caminho_banco=caminho)
            print(f"Retenção de {argumentos.reter} dia(s): {resultado}")
            acao_executada = True
        if argumentos.compactar:
            resultado_compactacao = compactar_banco(caminho)
            print(f"Banco compactado: {resultado_compactacao['bytes_antes']} → "
                  f"{resultado_compactacao['bytes_depois']} bytes.")
            acao_executada = True
        if argumentos.backup is not None:
            print(f"Backup gerado em: {fazer_backup(argumentos.backup, caminho_banco=caminho)}")
            acao_executada = True
        if not acao_executada:
            _imprimir_resumo(caminho)
    except (sqlite3.Error, OSError) as erro:
        registrador.error("Falha no banco %s: %s", caminho, erro)
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    sys.exit(main())
