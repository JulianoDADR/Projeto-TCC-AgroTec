"""Regras agronômicas fisiológicas do AgroAnálise (v3): da leitura ao status gravado.

Por que este módulo existe
--------------------------
As faixas do cliente (Café Arábica 18–22 °C, Café Conilon 22–26 °C) são
critérios de aptidão climática por TEMPERATURA MÉDIA ANUAL do zoneamento
agrícola (Portaria MAPA 58/2005,
https://licenciamento.cetesb.sp.gov.br/legislacao/federal/portarias/2005_Port_Fed_58.pdf;
Incaper, https://biblioteca.incaper.es.gov.br/digital/bitstream/item/5052/1/tecnologiasconilon.pdf).
Comparar cada leitura horária com elas confunde CLIMA com TEMPO: em dado real
(ERA5-Land, Varginha 2018–2021, local apto) a regra antiga pintava 27,6 % do ano
de Vermelho, com 1.843 trocas de cor por ano — e um alarme que "grita lobo"
perde a obediência (https://econpapers.repec.org/RePEc:wly:riskan:v:35:y:2015:i:3:p:385-395).

O dano real ao cafeeiro é fisiológico: geada (abrigo < 2 °C, Epamig/Embrapa
Café) e calor ≥ 34 °C na florada (Embrapa, CBAgro 2003). Por isso, no modo
``fisiologica`` (chave ``configuracao_sistema.regra_temperatura``):

1. CONFORTO TÉRMICO: a faixa do cliente vale sobre a MÉDIA MÓVEL DE 24 h
   (≥ 18 horas com dado; sem isso, a leitura instantânea), limitada a
   Amarelo (``DIA_FRIO``/``DIA_QUENTE``) — informativo, sem ação urgente.
2. GEADA: leitura ≤ ``limiar_geada_abrigo_c`` + ``incerteza_sensor_ar_c`` →
   Vermelho instantâneo (``GEADA_AGORA``).
3. CALOR: máxima do dia ≥ ``critico_max`` → Amarelo (``CALOR_DIA``); máxima
   ≥ max(34 °C, ``critico_max``) e leitura ≥ ``critico_max`` → Vermelho na
   florada (``CALOR_FLORADA``) ou no 2º dia seguido (``CALOR_SEGUIDO``).
4. O antigo ``critico_min`` de temperatura deixa de gerar Vermelho.
5. SECA PROGRAMADA (opcional, desligada por padrão): solo abaixo do ideal
   na janela de suspensão da irrigação vira Amarelo (``SECA_PROGRAMADA``).

O solo continua instantâneo. ``status = max(componentes)`` como no contrato v2.
O modo ``instantanea`` (chave ausente) reproduz exatamente ``calcular_status_regra``.

Também ficam aqui a banda morta do status gravado (fim da oscilação na borda),
os perfis do sensor de ar e a versão vetorizada do rótulo para treino
(``rotular_status_horario``). Nada aqui usa a rede.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from database import (
    CAMINHO_BANCO,
    STATUS_AMARELO,
    STATUS_VERDE,
    STATUS_VERMELHO,
    VARIAVEL_TEMPERATURA_AR,
    VARIAVEL_UMIDADE_SOLO,
    FaixaParametro,
    ParametrosCultura,
    obter_configuracao_tipada,
    obter_conexao,
)
from gerador_dataset import LIMITES_FISICOS_SENSORES, classificar_variavel

registrador: logging.Logger = logging.getLogger("agroanalise.regras_agronomicas")

# ---------------------------------------------------------------------------
# Constantes públicas
# ---------------------------------------------------------------------------
MODO_INSTANTANEO: str = "instantanea"
MODO_FISIOLOGICO: str = "fisiologica"
MODOS_REGRA: Tuple[str, ...] = (MODO_INSTANTANEO, MODO_FISIOLOGICO)

MOTIVO_NENHUM: str = "NENHUM"
MOTIVO_GEADA_AGORA: str = "GEADA_AGORA"
MOTIVO_CALOR_FLORADA: str = "CALOR_FLORADA"
MOTIVO_CALOR_SEGUIDO: str = "CALOR_SEGUIDO"
MOTIVO_CALOR_DIA: str = "CALOR_DIA"
MOTIVO_DIA_FRIO: str = "DIA_FRIO"
MOTIVO_DIA_QUENTE: str = "DIA_QUENTE"
MOTIVO_SECA_AGORA: str = "SECA_AGORA"
MOTIVO_SECA_PROGRAMADA: str = "SECA_PROGRAMADA"
MOTIVO_ENCHARCAMENTO: str = "ENCHARCAMENTO"
MOTIVOS_REGRA: Tuple[str, ...] = (
    MOTIVO_NENHUM, MOTIVO_GEADA_AGORA, MOTIVO_CALOR_FLORADA, MOTIVO_CALOR_SEGUIDO, MOTIVO_CALOR_DIA,
    MOTIVO_DIA_FRIO, MOTIVO_DIA_QUENTE, MOTIVO_SECA_AGORA, MOTIVO_SECA_PROGRAMADA, MOTIVO_ENCHARCAMENTO,
)
# Desempate quando temperatura e solo têm a MESMA cor: o perigo mais urgente
# vira o motivo principal (é o que decide o pictograma da Frente 3).
PRIORIDADE_MOTIVOS: Tuple[str, ...] = (
    MOTIVO_GEADA_AGORA, MOTIVO_CALOR_FLORADA, MOTIVO_CALOR_SEGUIDO, MOTIVO_SECA_AGORA, MOTIVO_ENCHARCAMENTO,
    MOTIVO_CALOR_DIA, MOTIVO_SECA_PROGRAMADA, MOTIVO_DIA_FRIO, MOTIVO_DIA_QUENTE, MOTIVO_NENHUM,
)
MOTIVOS_CALOR_VERMELHO: Tuple[str, ...] = (MOTIVO_CALOR_FLORADA, MOTIVO_CALOR_SEGUIDO)

BASE_INSTANTANEA: str = "instantanea"
BASE_MEDIA_24H: str = "media_24h"
BASES_TEMPERATURA: Tuple[str, ...] = (BASE_INSTANTANEA, BASE_MEDIA_24H)

# Mínimo de horas com dado nas últimas 24 h para a média valer (mesmo critério
# da antecipação de 12 h: 75 % das horas).
HORAS_MINIMAS_MEDIA_24H: int = 18
HORAS_JANELA_MEDIA: int = 24
HORAS_CONSULTA_HISTORICO: int = 48
CASAS_MEDIA: int = 6  # arredondamento comum aos dois caminhos (tempo real e vetorizado)

# Geada: dano ao cafeeiro com temperatura do ABRIGO (1,5 m) abaixo de 2 °C —
# Epamig/Embrapa Café: https://revistacultivar.com.br/noticias/pesquisadores-da-epamig-e-embrapa-cafe-orientam-cafeicultores-sobre-o-que-fazer-apos-as-geadas
LIMIAR_GEADA_ABRIGO_PADRAO_C: float = 2.0
# Calor: máxima ≥ 34 °C na florada aborta flores (Embrapa, CBAgro 2003):
# https://www.alice.cnptia.embrapa.br/alice/bitstream/doc/8420/1/PC-Temperaturas-elevadas-CBAGRO-2003.pdf
LIMIAR_CALOR_DANO_PADRAO_C: float = 34.0
DIAS_JANELA_FLORADA_PADRAO: int = 45
# Florada principal do café no Sudeste: setembro a novembro (mesma fonte acima;
# fenologia por graus-dia em https://sbicafe.ufv.br/items/e6b3e293-5d7c-4194-a07a-6eec74c6497b).
MESES_FLORADA_PADRAO: Tuple[int, ...] = (9, 10, 11)
# Suspensão da irrigação de 24/jun a 4/set para uniformizar a florada (Embrapa
# Cerrados): https://sbicafe.ufv.br/items/46703cd6-1804-4062-841a-3af5714ec5ac e
# https://www.alice.cnptia.embrapa.br/handle/doc/1180974?locale=en
SECA_PROGRAMADA_INICIO_PADRAO: str = "06-24"
SECA_PROGRAMADA_FIM_PADRAO: str = "09-04"
# Banda morta (deadband) contra "chattering" de alarme, ISA-18.2:
# https://blog.isa.org/solve-common-problems-industrial-automation-alarm-systems
# 3 % da faixa crítica: 0,72 °C no Arábica (< incerteza ±2 °C do DHT11) e 1,8 pp no solo.
BANDA_MORTA_FRACAO_PADRAO: float = 0.03
# Off-delay da MELHORA (mesma fonte ISA: "an off-delay of 60 seconds converts a full
# cluster of chattering alarms to a single alarm"). Só a volta para uma cor
# melhor espera; piorar continua imediato. Medido nesta frente: com mediana de 12
# e σ = 1 pp a 1 pp da borda do solo, só a banda de 1,8 pp deixava 3 de 20
# sementes com 2 trocas em 720 leituras; banda + 60 s de off-delay: no máximo 1.
BANDA_MORTA_ATRASO_MELHORA_PADRAO_S: float = 60.0

SENSOR_AR_PADRAO: str = "DHT11"
# (mínimo medível °C, máximo medível °C, incerteza ± °C) pelos datasheets:
# DHT11 — https://ece-classes.usc.edu/ee459/library/datasheets/DHT11.pdf (0–50 °C, ±2 °C);
# DHT22/AM2302 — Aosong AM2302 (−40–80 °C, ±0,5 °C; decodificação com bit de sinal no Adafruit DHT.cpp);
# SHT3x — Sensirion SHT3x-DIS (−40–125 °C, ±0,2 °C típico).
PERFIS_SENSOR_AR: Dict[str, Tuple[float, float, float]] = {
    "DHT11": (0.0, 50.0, 2.0),
    "DHT22": (-40.0, 80.0, 0.5),
    "SHT3X": (-40.0, 125.0, 0.2),
}

# Chaves de configuracao_sistema lidas por carregar_configuracao_termica.
CHAVE_REGRA_TEMPERATURA: str = "regra_temperatura"


# ---------------------------------------------------------------------------
# Estruturas
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ConfiguracaoTermica:
    """Parâmetros da regra térmica lidos de ``configuracao_sistema`` (padrões = chave ausente).

    O padrão de ``modo`` é ``instantanea`` (legado v2): um banco sem a chave
    ``regra_temperatura`` se comporta exatamente como antes. O coletor semeia
    ``fisiologica`` ao iniciar (``database.semear_configuracoes_v3``).
    """

    modo: str = MODO_INSTANTANEO
    limiar_geada_abrigo_c: float = LIMIAR_GEADA_ABRIGO_PADRAO_C
    incerteza_sensor_ar_c: float = 2.0
    limiar_calor_dano_c: float = LIMIAR_CALOR_DANO_PADRAO_C
    dias_janela_florada: int = DIAS_JANELA_FLORADA_PADRAO
    data_florada_principal: Optional[date] = None
    meses_florada_padrao: Tuple[int, ...] = MESES_FLORADA_PADRAO
    seca_programada_ativa: bool = False
    seca_programada_inicio: str = SECA_PROGRAMADA_INICIO_PADRAO
    seca_programada_fim: str = SECA_PROGRAMADA_FIM_PADRAO
    banda_morta_ativa: bool = False
    banda_morta_fracao: float = BANDA_MORTA_FRACAO_PADRAO
    banda_morta_atraso_s: float = BANDA_MORTA_ATRASO_MELHORA_PADRAO_S


@dataclass(frozen=True)
class ResumoTermico:
    """Resumo do histórico de temperatura ANTERIOR ao instante avaliado.

    Atributos:
        media_24h: média das médias horárias das últimas 24 h (``None`` com
            menos de ``HORAS_MINIMAS_MEDIA_24H`` horas com dado).
        horas_com_dado: horas (de 1 a 24 "horas atrás") com ao menos uma leitura.
        maxima_hoje: maior média de hora-relógio do dia civil de hoje, antes do instante.
        maxima_ontem: maior média de hora-relógio do dia civil anterior.
    """

    media_24h: Optional[float]
    horas_com_dado: int
    maxima_hoje: Optional[float]
    maxima_ontem: Optional[float]


RESUMO_VAZIO: ResumoTermico = ResumoTermico(media_24h=None, horas_com_dado=0, maxima_hoje=None, maxima_ontem=None)


@dataclass(frozen=True)
class AvaliacaoRegra:
    """Resultado da regra para UMA leitura.

    Os seis primeiros campos são o contrato da v3; os três últimos (com padrão)
    detalham os componentes para a mensagem falada e para a banda morta.

    Atributos:
        status: 0 Verde, 1 Amarelo, 2 Vermelho (= max dos componentes).
        status_temperatura: pior entre conforto, geada e calor.
        status_solo: componente do solo (instantâneo).
        motivo: motivo principal, um de ``MOTIVOS_REGRA``.
        base_temperatura: ``instantanea`` ou ``media_24h`` (base do conforto).
        temperatura_referencia: valor usado no conforto térmico.
        status_conforto: só o conforto térmico (faixa do cliente), memória da banda morta.
        motivo_temperatura: motivo do componente de temperatura.
        motivo_solo: motivo do componente de solo.
    """

    status: int
    status_temperatura: int
    status_solo: int
    motivo: str
    base_temperatura: str
    temperatura_referencia: float
    status_conforto: int = STATUS_VERDE
    motivo_temperatura: str = MOTIVO_NENHUM
    motivo_solo: str = MOTIVO_NENHUM


# ---------------------------------------------------------------------------
# Leitura da configuração
# ---------------------------------------------------------------------------
def _avisar_invalido(chave: str, valor: Any, padrao: Any, motivo: str) -> None:
    """Registra no log uma configuração inválida trocada pelo padrão."""
    registrador.warning("Configuração '%s'=%r inválida (%s); usando o padrão %r.", chave, valor, motivo, padrao)


def _ler_real(config: Mapping[str, Any], chave: str, padrao: float, minimo: float, maximo: float) -> float:
    """Número real finito dentro de ``[minimo, maximo]`` ou o padrão (com aviso)."""
    valor = config.get(chave)
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        return padrao
    if isinstance(valor, bool):
        _avisar_invalido(chave, valor, padrao, "booleano não é número")
        return padrao
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        _avisar_invalido(chave, valor, padrao, "não é número")
        return padrao
    if not math.isfinite(numero) or not minimo <= numero <= maximo:
        _avisar_invalido(chave, valor, padrao, f"fora de [{minimo}, {maximo}]")
        return padrao
    return numero


def _ler_inteiro(config: Mapping[str, Any], chave: str, padrao: int, minimo: int, maximo: int) -> int:
    """Inteiro dentro de ``[minimo, maximo]`` ou o padrão (com aviso)."""
    numero = _ler_real(config, chave, float(padrao), float(minimo), float(maximo))
    if not float(numero).is_integer():
        _avisar_invalido(chave, numero, padrao, "não é inteiro")
        return padrao
    return int(numero)


def _ler_booleano(config: Mapping[str, Any], chave: str, padrao: bool) -> bool:
    """Booleano (aceita ``true``/``false``/``1``/``0``/``sim``/``não``) ou o padrão."""
    valor = config.get(chave)
    if valor is None:
        return padrao
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, (int, float)) and valor in (0, 1):
        return bool(valor)
    texto = str(valor).strip().lower()
    if texto in ("true", "1", "sim", "yes", "verdadeiro"):
        return True
    if texto in ("false", "0", "nao", "não", "no", "falso"):
        return False
    if not texto:
        return padrao
    _avisar_invalido(chave, valor, padrao, "não é booleano")
    return padrao


def _interpretar_mes_dia(texto: str) -> Optional[Tuple[int, int]]:
    """``'MM-DD'`` → ``(mes, dia)`` válido num ano bissexto; ``None`` se inválido."""
    partes = str(texto or "").strip().split("-")
    if len(partes) != 2 or not all(parte.isdigit() for parte in partes):
        return None
    mes, dia = int(partes[0]), int(partes[1])
    try:
        date(2024, mes, dia)
    except ValueError:
        return None
    return mes, dia


def _ler_mes_dia(config: Mapping[str, Any], chave: str, padrao: str) -> str:
    """Texto ``'MM-DD'`` válido ou o padrão (com aviso)."""
    valor = config.get(chave)
    if valor is None or not str(valor).strip():
        return padrao
    convertido = _interpretar_mes_dia(str(valor))
    if convertido is None:
        _avisar_invalido(chave, valor, padrao, "use MM-DD")
        return padrao
    return f"{convertido[0]:02d}-{convertido[1]:02d}"


def _ler_data(config: Mapping[str, Any], chave: str) -> Optional[date]:
    """Data ISO ``AAAA-MM-DD`` ou ``None`` (vazia ou inválida, com aviso)."""
    valor = config.get(chave)
    if valor is None or not str(valor).strip():
        return None
    if isinstance(valor, date) and not isinstance(valor, datetime):
        return valor
    try:
        return date.fromisoformat(str(valor).strip()[:10])
    except ValueError:
        _avisar_invalido(chave, valor, None, "use AAAA-MM-DD")
        return None


def configuracao_termica_de_dicionario(config: Mapping[str, Any]) -> ConfiguracaoTermica:
    """Monta a ``ConfiguracaoTermica`` a partir de ``{chave: valor}`` (ex.: ``obter_todas_configuracoes``).

    Chave ausente ou vazia → padrão; valor inválido → aviso no log e padrão.
    """
    padrao = ConfiguracaoTermica()
    modo_texto = config.get(CHAVE_REGRA_TEMPERATURA)
    modo = padrao.modo
    if modo_texto is not None and str(modo_texto).strip():
        candidato = str(modo_texto).strip().lower()
        if candidato in MODOS_REGRA:
            modo = candidato
        else:
            _avisar_invalido(CHAVE_REGRA_TEMPERATURA, modo_texto, padrao.modo, f"use {' ou '.join(MODOS_REGRA)}")
    return ConfiguracaoTermica(
        modo=modo,
        limiar_geada_abrigo_c=_ler_real(config, "limiar_geada_abrigo_c", padrao.limiar_geada_abrigo_c, -10.0, 10.0),
        incerteza_sensor_ar_c=_ler_real(config, "incerteza_sensor_ar_c", padrao.incerteza_sensor_ar_c, 0.0, 5.0),
        limiar_calor_dano_c=_ler_real(config, "limiar_calor_dano_c", padrao.limiar_calor_dano_c, 20.0, 50.0),
        dias_janela_florada=_ler_inteiro(config, "dias_janela_florada", padrao.dias_janela_florada, 1, 180),
        data_florada_principal=_ler_data(config, "data_florada_principal"),
        meses_florada_padrao=padrao.meses_florada_padrao,
        seca_programada_ativa=_ler_booleano(config, "seca_programada_ativa", padrao.seca_programada_ativa),
        seca_programada_inicio=_ler_mes_dia(config, "seca_programada_inicio", padrao.seca_programada_inicio),
        seca_programada_fim=_ler_mes_dia(config, "seca_programada_fim", padrao.seca_programada_fim),
        banda_morta_ativa=_ler_booleano(config, "banda_morta_ativa", padrao.banda_morta_ativa),
        banda_morta_fracao=_ler_real(config, "banda_morta_fracao", padrao.banda_morta_fracao, 0.0, 0.25),
        banda_morta_atraso_s=_ler_real(config, "banda_morta_atraso_s", padrao.banda_morta_atraso_s, 0.0, 3600.0),
    )


CHAVES_CONFIGURACAO_TERMICA: Tuple[str, ...] = (
    CHAVE_REGRA_TEMPERATURA, "limiar_geada_abrigo_c", "incerteza_sensor_ar_c", "limiar_calor_dano_c",
    "dias_janela_florada", "data_florada_principal", "seca_programada_ativa", "seca_programada_inicio",
    "seca_programada_fim", "banda_morta_ativa", "banda_morta_fracao", "banda_morta_atraso_s",
)


def carregar_configuracao_termica(caminho_banco: Path = CAMINHO_BANCO) -> ConfiguracaoTermica:
    """Lê as chaves da regra térmica do banco via ``obter_configuracao_tipada``.

    Chave ausente usa o padrão de ``ConfiguracaoTermica``; valor inválido gera
    aviso no log e o padrão. Falha do banco (travado, inexistente) também cai
    nos padrões, com aviso: a regra nunca deixa de responder.
    """
    valores: Dict[str, Any] = {}
    try:
        for chave in CHAVES_CONFIGURACAO_TERMICA:
            valores[chave] = obter_configuracao_tipada(chave, None, caminho_banco=caminho_banco)
    except sqlite3.Error as erro:
        registrador.warning("Não foi possível ler a configuração térmica (%s); usando os padrões.", erro)
        return ConfiguracaoTermica()
    return configuracao_termica_de_dicionario(valores)


def normalizar_modelo_sensor_ar(modelo: Optional[str]) -> str:
    """Nome do perfil do sensor de ar (``DHT11``, ``DHT22``, ``SHT3X``); desconhecido → ``DHT11``."""
    texto = str(modelo or "").strip().upper()
    sinonimos = {"SHT30": "SHT3X", "SHT31": "SHT3X", "SHT35": "SHT3X", "AM2302": "DHT22"}
    texto = sinonimos.get(texto, texto)
    if texto in PERFIS_SENSOR_AR:
        return texto
    if texto:
        registrador.warning("Sensor de ar '%s' desconhecido; usando o perfil %s (o mais conservador).",
                            modelo, SENSOR_AR_PADRAO)
    return SENSOR_AR_PADRAO


def limites_fisicos_sensor_ar(modelo: Optional[str]) -> Dict[str, Tuple[float, float]]:
    """``LIMITES_FISICOS_SENSORES`` com a faixa de temperatura do perfil do sensor de ar."""
    minimo, maximo, _ = PERFIS_SENSOR_AR[normalizar_modelo_sensor_ar(modelo)]
    limites = dict(LIMITES_FISICOS_SENSORES)
    limites[VARIAVEL_TEMPERATURA_AR] = (float(minimo), float(maximo))
    return limites


# ---------------------------------------------------------------------------
# Calendário: florada e seca programada
# ---------------------------------------------------------------------------
def _data_no_ano(referencia: date, ano: int) -> date:
    """A mesma data (mês/dia) no ano pedido; 29/02 vira 28/02 em ano não bissexto."""
    try:
        return referencia.replace(year=ano)
    except ValueError:
        return referencia.replace(year=ano, day=28)


def em_janela_florada(dia: date, cfg: ConfiguracaoTermica) -> bool:
    """Indica se ``dia`` está na janela de florada.

    Com ``data_florada_principal``: de ``[data, data + dias_janela_florada]``,
    repetida a cada ano pelo mês/dia (a florada principal acontece na mesma
    época todo ano; uma data de dezembro cobre também o começo de janeiro).
    Sem ela: os meses de ``meses_florada_padrao`` (set.–nov.).
    """
    if cfg.data_florada_principal is None:
        return dia.month in cfg.meses_florada_padrao
    duracao = timedelta(days=int(cfg.dias_janela_florada))
    for ano in (dia.year, dia.year - 1):
        inicio = _data_no_ano(cfg.data_florada_principal, ano)
        if inicio <= dia <= inicio + duracao:
            return True
    return False


def fase_florada(dia: date, cfg: ConfiguracaoTermica) -> str:
    """``'FLORADA'`` dentro da janela de florada, ``'FORA_FLORADA'`` fora dela."""
    return "FLORADA" if em_janela_florada(dia, cfg) else "FORA_FLORADA"


def em_seca_programada(dia: date, cfg: ConfiguracaoTermica) -> bool:
    """Indica se a seca programada vale em ``dia`` (chave ligada E dia na janela ``MM-DD``).

    A janela pode cruzar o ano (ex.: ``11-15`` a ``02-10``).
    """
    if not cfg.seca_programada_ativa:
        return False
    inicio = _interpretar_mes_dia(cfg.seca_programada_inicio) or _interpretar_mes_dia(SECA_PROGRAMADA_INICIO_PADRAO)
    fim = _interpretar_mes_dia(cfg.seca_programada_fim) or _interpretar_mes_dia(SECA_PROGRAMADA_FIM_PADRAO)
    assert inicio is not None and fim is not None
    chave = (dia.month, dia.day)
    if inicio <= fim:
        return inicio <= chave <= fim
    return chave >= inicio or chave <= fim


def limiar_geada_vermelho(cfg: ConfiguracaoTermica) -> float:
    """Leitura do abrigo que já dá Vermelho de geada: limiar fisiológico + incerteza do sensor.

    DHT11: 2,0 + 2,0 = 4,0 °C (dentro da faixa medível de 0–50 °C); DHT22: 2,5 °C.
    """
    return float(cfg.limiar_geada_abrigo_c) + float(cfg.incerteza_sensor_ar_c)


def limiar_calor_vermelho(cfg: ConfiguracaoTermica, faixa_temperatura: FaixaParametro) -> float:
    """Máxima do dia que pode dar Vermelho de calor: ``max(limiar_calor_dano_c, critico_max)``."""
    return max(float(cfg.limiar_calor_dano_c), float(faixa_temperatura.critico_max))


# ---------------------------------------------------------------------------
# Banda morta
# ---------------------------------------------------------------------------
def banda_morta_padrao(faixa: FaixaParametro, fracao: float) -> float:
    """Banda morta = ``fracao × (critico_max − critico_min)`` (≈ 0,72 °C no Arábica e 1,8 pp no solo)."""
    return max(0.0, float(fracao)) * (float(faixa.critico_max) - float(faixa.critico_min))


def classificar_com_banda_morta(
    valor: float, faixa: FaixaParametro, status_anterior: Optional[int], banda: float
) -> int:
    """Classifica com histerese: piorar é imediato; MELHORAR exige folga ``banda``.

    Para melhorar, o valor precisa estar na faixa melhor com as bordas internas
    deslocadas ``banda`` para dentro (ex.: solo do Arábica que estava Amarelo só
    volta a Verde com ≥ 60 + 1,8 = 61,8 %). ``status_anterior`` ``None`` (ou
    banda zero) equivale a ``classificar_variavel``.

    Raises:
        ValueError: valor não finito.
    """
    atual = classificar_variavel(valor, faixa)
    if status_anterior is None:
        return atual
    anterior = int(status_anterior)
    if atual >= anterior:
        return atual
    largura_ideal = float(faixa.ideal_max) - float(faixa.ideal_min)
    folga = min(max(0.0, float(banda)), largura_ideal / 2.0)
    if folga <= 0.0:
        return atual
    numero = float(valor)
    if faixa.ideal_min + folga <= numero <= faixa.ideal_max - folga:
        candidato = STATUS_VERDE
    elif faixa.atencao_min + folga <= numero <= faixa.atencao_max - folga:
        candidato = STATUS_AMARELO
    else:
        candidato = STATUS_VERMELHO
    return min(max(candidato, atual), anterior)


# ---------------------------------------------------------------------------
# Resumo do histórico térmico
# ---------------------------------------------------------------------------
Grupo = Tuple[int, str, float, int]  # (horas_atras 1.., hora-relógio 'YYYY-MM-DD HH:00:00', soma, n)


def _resumo_de_grupos(grupos: Iterable[Grupo], agora: datetime) -> ResumoTermico:
    """Núcleo comum ao tempo real (SQL) e ao ``resumir_serie_termica`` (pandas).

    ``horas_atras`` = ⌈(agora − instante) / 1 h⌉: as leituras de 0 a 1 h atrás
    formam o balde 1, as de 23 a 24 h atrás o balde 24. A média de 24 h é a
    média das médias dos baldes 1..24 (cada hora pesa igual, com 1 ou 720
    leituras). As máximas diárias usam as médias de HORA-RELÓGIO do dia civil.
    """
    soma_balde: Dict[int, float] = {}
    n_balde: Dict[int, int] = {}
    soma_hora: Dict[str, float] = {}
    n_hora: Dict[str, int] = {}
    for horas_atras, hora, soma, quantidade in grupos:
        if quantidade <= 0:
            continue
        if 1 <= int(horas_atras) <= HORAS_JANELA_MEDIA:
            soma_balde[int(horas_atras)] = soma_balde.get(int(horas_atras), 0.0) + float(soma)
            n_balde[int(horas_atras)] = n_balde.get(int(horas_atras), 0) + int(quantidade)
        soma_hora[str(hora)] = soma_hora.get(str(hora), 0.0) + float(soma)
        n_hora[str(hora)] = n_hora.get(str(hora), 0) + int(quantidade)
    horas = len(n_balde)
    media: Optional[float] = None
    if horas >= HORAS_MINIMAS_MEDIA_24H:
        medias = [soma_balde[balde] / n_balde[balde] for balde in sorted(n_balde)]
        media = round(float(np.mean(medias)), CASAS_MEDIA)
    hoje = agora.date().isoformat()
    ontem = (agora.date() - timedelta(days=1)).isoformat()
    maxima_hoje: Optional[float] = None
    maxima_ontem: Optional[float] = None
    for hora, quantidade in n_hora.items():
        media_hora = round(soma_hora[hora] / quantidade, CASAS_MEDIA)
        dia = hora[:10]
        if dia == hoje:
            maxima_hoje = media_hora if maxima_hoje is None else max(maxima_hoje, media_hora)
        elif dia == ontem:
            maxima_ontem = media_hora if maxima_ontem is None else max(maxima_ontem, media_hora)
    return ResumoTermico(media_24h=media, horas_com_dado=horas, maxima_hoje=maxima_hoje, maxima_ontem=maxima_ontem)


def _truncar_segundo(instante: datetime) -> datetime:
    """Remove fuso (convertendo para o horário local) e frações de segundo."""
    if instante.tzinfo is not None:
        instante = instante.astimezone().replace(tzinfo=None)
    return instante.replace(microsecond=0)


def resumir_serie_termica(serie: pd.DataFrame, agora: datetime) -> ResumoTermico:
    """Resume a série ANTERIOR a ``agora`` (colunas ``data_hora`` e ``temperatura_ar``).

    Só entram as leituras de até 48 h antes e estritamente antes de ``agora``;
    ``NaN`` é ignorado. Mesma definição do caminho em tempo real
    (``buscar_resumo_termico``): média das médias horárias das últimas 24 h
    (``None`` com menos de 18 horas com dado) e máximas por dia civil.
    """
    if serie is None or len(serie) == 0:
        return RESUMO_VAZIO
    referencia = _truncar_segundo(agora)
    instantes = pd.to_datetime(serie["data_hora"]).dt.floor("s")
    if getattr(instantes.dt, "tz", None) is not None:
        instantes = instantes.dt.tz_convert(None)
    temperaturas = pd.to_numeric(serie["temperatura_ar"], errors="coerce")
    segundos = (pd.Timestamp(referencia) - instantes).dt.total_seconds()
    mascara = (segundos > 0) & (segundos <= HORAS_CONSULTA_HISTORICO * 3600) & temperaturas.notna()
    if not bool(mascara.any()):
        return RESUMO_VAZIO
    tabela = pd.DataFrame({
        "horas_atras": ((segundos[mascara].astype("int64") + 3599) // 3600).astype(int),
        "hora": instantes[mascara].dt.floor("h").dt.strftime("%Y-%m-%d %H:00:00"),
        "temperatura": temperaturas[mascara].astype(float),
    })
    agregados = tabela.groupby(["horas_atras", "hora"], sort=False)["temperatura"].agg(["sum", "count"])
    grupos: List[Grupo] = [(int(chave[0]), str(chave[1]), float(linha["sum"]), int(linha["count"]))
                           for chave, linha in agregados.iterrows()]
    return _resumo_de_grupos(grupos, referencia)


_SQL_GRUPOS_BRUTOS: str = """
    SELECT ((CAST(strftime('%s', :agora) AS INTEGER) - CAST(strftime('%s', h.data_hora) AS INTEGER)) + 3599) / 3600
               AS horas_atras,
           strftime('%Y-%m-%d %H:00:00', h.data_hora) AS hora,
           SUM(h.temperatura_ar) AS soma,
           COUNT(*) AS n
      FROM historico_leituras h
      JOIN culturas c ON c.id_cultura = h.id_cultura
     WHERE c.codigo = :codigo AND h.origem = :origem
       AND h.data_hora >= :inicio AND h.data_hora < :agora
     GROUP BY horas_atras, hora
"""

_SQL_GRUPOS_AGREGADOS: str = """
    SELECT a.hora AS hora, a.temperatura_med AS media, a.n AS n
      FROM historico_agregado_hora a
      JOIN culturas c ON c.id_cultura = a.id_cultura
     WHERE c.codigo = :codigo AND a.origem = :origem
       AND a.hora >= :inicio AND a.hora < :agora AND a.temperatura_med IS NOT NULL AND a.n > 0
"""


def buscar_resumo_termico(
    codigo_cultura: str, caminho_banco: Path, agora: datetime, origem: str
) -> ResumoTermico:
    """Resumo térmico das últimas 48 h de ``historico_leituras`` (mesma cultura e origem).

    A agregação (por "horas atrás" e por hora-relógio) é feita no SQL, o que
    mantém a consulta leve mesmo com 34 mil leituras em 48 h. As horas sem
    leitura bruta (apagadas pela retenção) são completadas com
    ``historico_agregado_hora`` (média da hora, centrada na meia hora).
    Falha do banco → resumo vazio (a regra usa a leitura instantânea), com aviso.
    """
    referencia = _truncar_segundo(agora)
    parametros = {
        "codigo": str(codigo_cultura),
        "origem": str(origem),
        "agora": referencia.strftime("%Y-%m-%d %H:%M:%S"),
        "inicio": (referencia - timedelta(hours=HORAS_CONSULTA_HISTORICO)).strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        conexao = obter_conexao(caminho_banco)
        try:
            grupos: List[Grupo] = [
                (int(linha["horas_atras"]), str(linha["hora"]), float(linha["soma"]), int(linha["n"]))
                for linha in conexao.execute(_SQL_GRUPOS_BRUTOS, parametros).fetchall()
                if linha["soma"] is not None
            ]
            horas_brutas = {grupo[1] for grupo in grupos}
            if len({grupo[0] for grupo in grupos if 1 <= grupo[0] <= HORAS_JANELA_MEDIA}) < HORAS_MINIMAS_MEDIA_24H:
                parametros_agregado = dict(parametros)
                parametros_agregado["inicio"] = (referencia - timedelta(hours=HORAS_CONSULTA_HISTORICO)).strftime(
                    "%Y-%m-%d %H:00:00")
                try:
                    linhas_agregadas = conexao.execute(_SQL_GRUPOS_AGREGADOS, parametros_agregado).fetchall()
                except sqlite3.OperationalError:
                    linhas_agregadas = []  # banco anterior à retenção (sem a tabela)
                for linha in linhas_agregadas:
                    hora = str(linha["hora"])
                    if hora in horas_brutas:
                        continue
                    centro = datetime.strptime(hora, "%Y-%m-%d %H:%M:%S") + timedelta(minutes=30)
                    segundos = int((referencia - centro).total_seconds())
                    if segundos <= 0:
                        continue
                    quantidade = int(linha["n"])
                    grupos.append(((segundos + 3599) // 3600, hora, float(linha["media"]) * quantidade, quantidade))
        finally:
            conexao.close()
    except sqlite3.Error as erro:
        registrador.warning("Não foi possível ler o histórico térmico (%s); usando a leitura instantânea.", erro)
        return RESUMO_VAZIO
    return _resumo_de_grupos(grupos, referencia)


# ---------------------------------------------------------------------------
# Avaliação de uma leitura
# ---------------------------------------------------------------------------
def _escolher_motivo(componentes: Sequence[Tuple[int, str]]) -> Tuple[int, str]:
    """``(status, motivo)`` do pior componente; empate decidido por ``PRIORIDADE_MOTIVOS``."""
    pior = max(status for status, _ in componentes)
    candidatos = [motivo for status, motivo in componentes if status == pior]
    if pior == STATUS_VERDE:
        return STATUS_VERDE, MOTIVO_NENHUM
    candidatos = [motivo for motivo in candidatos if motivo != MOTIVO_NENHUM] or [MOTIVO_NENHUM]
    return pior, min(candidatos, key=PRIORIDADE_MOTIVOS.index)


def _lado(valor: float, faixa: FaixaParametro) -> str:
    """``'baixo'`` se o valor estiver abaixo do meio da faixa ideal, senão ``'alto'``."""
    return "baixo" if float(valor) < (faixa.ideal_min + faixa.ideal_max) / 2.0 else "alto"


def _classificar(valor: float, faixa: FaixaParametro, anterior: Optional[int], cfg: ConfiguracaoTermica) -> int:
    """Classificação com banda morta (se ativa e houver estado anterior) ou sem ela."""
    if cfg.banda_morta_ativa and anterior is not None:
        return classificar_com_banda_morta(valor, faixa, anterior, banda_morta_padrao(faixa, cfg.banda_morta_fracao))
    return classificar_variavel(valor, faixa)


def _avaliar_solo(
    umidade_solo: float, faixa_solo: FaixaParametro, anterior: Optional[int], cfg: ConfiguracaoTermica,
    seca_programada: bool, piso: int = STATUS_VERDE,
) -> Tuple[int, str]:
    """Componente do solo (instantâneo) com seca programada opcional e piso (off-delay da melhora)."""
    faixa = replace(faixa_solo, atencao_min=faixa_solo.critico_min) if seca_programada else faixa_solo
    status = max(_classificar(umidade_solo, faixa, anterior, cfg), int(piso))
    if status == STATUS_VERDE:
        return status, MOTIVO_NENHUM
    if _lado(umidade_solo, faixa_solo) == "alto":
        return status, MOTIVO_ENCHARCAMENTO
    if seca_programada and status == STATUS_AMARELO:
        return status, MOTIVO_SECA_PROGRAMADA
    return status, MOTIVO_SECA_AGORA


def _componentes_instantaneos(
    temperatura_ar: float, umidade_solo: float, parametros: ParametrosCultura, cfg: ConfiguracaoTermica,
    estado_anterior: Optional[Tuple[int, int]], pisos: Tuple[int, int],
) -> AvaliacaoRegra:
    """Modo ``instantanea`` (legado v2): mesmo status de ``calcular_status_regra`` e motivo derivado."""
    faixa_temperatura = parametros.faixa(VARIAVEL_TEMPERATURA_AR)
    faixa_solo = parametros.faixa(VARIAVEL_UMIDADE_SOLO)
    anterior_temperatura = estado_anterior[0] if estado_anterior is not None else None
    anterior_solo = estado_anterior[1] if estado_anterior is not None else None
    status_temperatura = max(_classificar(temperatura_ar, faixa_temperatura, anterior_temperatura, cfg), pisos[0])
    if status_temperatura == STATUS_VERDE:
        motivo_temperatura = MOTIVO_NENHUM
    elif _lado(temperatura_ar, faixa_temperatura) == "baixo":
        # Abaixo do crítico a voz legada já diz "Está gelando agora" (mesmo motivo dos pictogramas de geada).
        motivo_temperatura = MOTIVO_GEADA_AGORA if temperatura_ar < faixa_temperatura.critico_min else MOTIVO_DIA_FRIO
    else:
        motivo_temperatura = MOTIVO_CALOR_DIA if status_temperatura == STATUS_VERMELHO else MOTIVO_DIA_QUENTE
    status_solo, motivo_solo = _avaliar_solo(umidade_solo, faixa_solo, anterior_solo, cfg, seca_programada=False,
                                             piso=pisos[1])
    status, motivo = _escolher_motivo([(status_temperatura, motivo_temperatura), (status_solo, motivo_solo)])
    return AvaliacaoRegra(
        status=status, status_temperatura=status_temperatura, status_solo=status_solo, motivo=motivo,
        base_temperatura=BASE_INSTANTANEA, temperatura_referencia=float(temperatura_ar),
        status_conforto=status_temperatura, motivo_temperatura=motivo_temperatura, motivo_solo=motivo_solo,
    )


def _validar_finito(valor: float, nome: str) -> float:
    """Converte para ``float`` finito.

    Raises:
        ValueError: valor ausente ou não finito.
    """
    try:
        numero = float(valor)
    except (TypeError, ValueError) as erro:
        raise ValueError(f"{nome} precisa ser número, recebido {valor!r}.") from erro
    if not math.isfinite(numero):
        raise ValueError(f"{nome} precisa ser finito, recebido {valor!r}.")
    return numero


def avaliar_status_campo(
    temperatura_ar: float,
    umidade_solo: float,
    parametros: ParametrosCultura,
    resumo: ResumoTermico,
    cfg: ConfiguracaoTermica,
    agora: datetime,
    estado_anterior: Optional[Tuple[int, int]] = None,
    pisos: Optional[Tuple[int, int]] = None,
) -> AvaliacaoRegra:
    """Aplica a regra térmica e a do solo a UMA leitura.

    Modo ``fisiologica`` (regras 1 a 5 da decisão do comitê):
        1. conforto: faixa do cliente sobre a média de 24 h (≥ 18 h com dado;
           senão a leitura instantânea), no máximo Amarelo (``DIA_FRIO``/``DIA_QUENTE``);
        2. geada: leitura ≤ ``limiar_geada_vermelho(cfg)`` → Vermelho ``GEADA_AGORA``;
        3. calor: máxima do dia (incluindo a leitura) ≥ ``critico_max`` →
           Amarelo ``CALOR_DIA``; máxima ≥ ``limiar_calor_vermelho`` e leitura ≥
           ``critico_max`` → Vermelho ``CALOR_FLORADA`` (na janela de florada) ou
           ``CALOR_SEGUIDO`` (máxima de ontem também ≥ ``limiar_calor_vermelho``);
        4. o ``critico_min`` de temperatura não gera Vermelho (frio = Amarelo);
        5. solo instantâneo; com seca programada, abaixo do ideal e acima do
           ``critico_min`` → Amarelo ``SECA_PROGRAMADA``.
    Modo ``instantanea``: o mesmo status de ``calcular_status_regra``.

    Args:
        temperatura_ar, umidade_solo: leitura saneada.
        parametros: faixas da cultura.
        resumo: histórico térmico ANTERIOR à leitura (``buscar_resumo_termico``).
        cfg: configuração térmica.
        agora: instante da leitura (dia civil, florada e seca programada).
        estado_anterior: ``(status_conforto, status_solo)`` da leitura anterior,
            usado pela banda morta quando ``cfg.banda_morta_ativa``. Geada e
            calor fisiológicos nunca usam banda: o perigo não espera.
        pisos: ``(conforto, solo)`` mínimos — o ``MotorRegrasCampo`` segura a
            cor anterior enquanto o off-delay da melhora não venceu.

    Raises:
        ValueError: valores não finitos.
        KeyError: cultura sem faixa de temperatura ou de solo.
    """
    temperatura = _validar_finito(temperatura_ar, "temperatura_ar")
    solo = _validar_finito(umidade_solo, "umidade_solo")
    piso_conforto, piso_solo = (int(pisos[0]), int(pisos[1])) if pisos is not None else (STATUS_VERDE, STATUS_VERDE)
    if cfg.modo != MODO_FISIOLOGICO:
        return _componentes_instantaneos(temperatura, solo, parametros, cfg, estado_anterior,
                                         (piso_conforto, piso_solo))

    faixa_temperatura = parametros.faixa(VARIAVEL_TEMPERATURA_AR)
    faixa_solo = parametros.faixa(VARIAVEL_UMIDADE_SOLO)
    anterior_conforto = estado_anterior[0] if estado_anterior is not None else None
    anterior_solo = estado_anterior[1] if estado_anterior is not None else None
    dia = agora.date()

    # 1) Conforto térmico: faixa do cliente sobre a média de 24 h, no máximo Amarelo.
    if resumo.media_24h is not None and resumo.horas_com_dado >= HORAS_MINIMAS_MEDIA_24H:
        base, referencia = BASE_MEDIA_24H, float(resumo.media_24h)
    else:
        base, referencia = BASE_INSTANTANEA, temperatura
    conforto = min(STATUS_AMARELO, max(_classificar(referencia, faixa_temperatura, anterior_conforto, cfg),
                                       piso_conforto))
    motivo_conforto = MOTIVO_NENHUM
    if conforto != STATUS_VERDE:
        motivo_conforto = MOTIVO_DIA_FRIO if _lado(referencia, faixa_temperatura) == "baixo" else MOTIVO_DIA_QUENTE

    # 2) Geada: limiar fisiológico + incerteza do sensor (instantâneo, sem banda).
    geada = STATUS_VERMELHO if temperatura <= limiar_geada_vermelho(cfg) else STATUS_VERDE

    # 3) Calor: máxima do dia (a leitura atual pertence ao dia de hoje).
    maxima_hoje = temperatura if resumo.maxima_hoje is None else max(float(resumo.maxima_hoje), temperatura)
    limiar_vermelho = limiar_calor_vermelho(cfg, faixa_temperatura)
    calor, motivo_calor = STATUS_VERDE, MOTIVO_NENHUM
    if maxima_hoje >= limiar_vermelho and temperatura >= faixa_temperatura.critico_max:
        if em_janela_florada(dia, cfg):
            calor, motivo_calor = STATUS_VERMELHO, MOTIVO_CALOR_FLORADA
        elif resumo.maxima_ontem is not None and float(resumo.maxima_ontem) >= limiar_vermelho:
            calor, motivo_calor = STATUS_VERMELHO, MOTIVO_CALOR_SEGUIDO
        else:
            calor, motivo_calor = STATUS_AMARELO, MOTIVO_CALOR_DIA
    elif maxima_hoje >= faixa_temperatura.critico_max:
        calor, motivo_calor = STATUS_AMARELO, MOTIVO_CALOR_DIA

    status_temperatura, motivo_temperatura = _escolher_motivo([
        (geada, MOTIVO_GEADA_AGORA if geada else MOTIVO_NENHUM),
        (calor, motivo_calor),
        (conforto, motivo_conforto),
    ])

    # 5) Solo (instantâneo) com seca programada opcional.
    status_solo, motivo_solo = _avaliar_solo(solo, faixa_solo, anterior_solo, cfg,
                                             seca_programada=em_seca_programada(dia, cfg), piso=piso_solo)
    status, motivo = _escolher_motivo([(status_temperatura, motivo_temperatura), (status_solo, motivo_solo)])
    return AvaliacaoRegra(
        status=status, status_temperatura=status_temperatura, status_solo=status_solo, motivo=motivo,
        base_temperatura=base, temperatura_referencia=round(referencia, CASAS_MEDIA),
        status_conforto=conforto, motivo_temperatura=motivo_temperatura, motivo_solo=motivo_solo,
    )


def restringir_avaliacao(avaliacao: AvaliacaoRegra, usar_temperatura: bool, usar_solo: bool) -> AvaliacaoRegra:
    """Zera os componentes de sensores não confiáveis e recalcula status e motivo.

    Usada quando um sensor está ``SUSPEITO``/``DEFEITO``: a variável não
    confiável não pode acender nem apagar alerta.
    """
    status_temperatura = avaliacao.status_temperatura if usar_temperatura else STATUS_VERDE
    motivo_temperatura = avaliacao.motivo_temperatura if usar_temperatura else MOTIVO_NENHUM
    status_solo = avaliacao.status_solo if usar_solo else STATUS_VERDE
    motivo_solo = avaliacao.motivo_solo if usar_solo else MOTIVO_NENHUM
    status, motivo = _escolher_motivo([(status_temperatura, motivo_temperatura), (status_solo, motivo_solo)])
    return replace(avaliacao, status=status, status_temperatura=status_temperatura, status_solo=status_solo,
                   motivo=motivo, motivo_temperatura=motivo_temperatura, motivo_solo=motivo_solo,
                   status_conforto=avaliacao.status_conforto if usar_temperatura else STATUS_VERDE)


# ---------------------------------------------------------------------------
# Versão vetorizada (rótulo de treino)
# ---------------------------------------------------------------------------
def rotular_status_horario(
    df_horario: pd.DataFrame,
    parametros: ParametrosCultura,
    configuracao: Optional[ConfiguracaoTermica] = None,
) -> pd.Series:
    """Rotula uma série HORÁRIA com a mesma regra de ``avaliar_status_campo`` (sem banda morta).

    Contrato com a Frente 2 (``treinamento_ia``/``prognostico``):
        * entrada com ``data_hora`` (ordenada, uma linha por hora),
          ``temperatura_ar`` e ``umidade_solo`` saneadas (finitas);
        * saída ``pd.Series`` de inteiros 0/1/2 com o MESMO índice da entrada;
        * ``configuracao`` ``None`` = modo fisiológico padrão (florada por
          calendário, seca programada desligada); ``modo='instantanea'`` dá o
          rótulo da v2 (``calcular_status_regra``).

    Para cada linha, o "histórico" é o das linhas ANTERIORES: média das
    últimas 24 h ``[t − 24 h, t)`` com ≥ 18 linhas, máxima do dia civil até a
    linha (inclusive) e máxima do dia civil anterior. Concorda com o caminho em
    tempo real em 100 % das horas de uma série horária (teste de propriedade).

    Raises:
        ValueError: colunas ausentes, ``data_hora`` fora de ordem ou valores não finitos.
    """
    faltando = [coluna for coluna in ("data_hora", VARIAVEL_TEMPERATURA_AR, VARIAVEL_UMIDADE_SOLO)
                if coluna not in df_horario.columns]
    if faltando:
        raise ValueError(f"rotular_status_horario: faltam as colunas {', '.join(faltando)}.")
    cfg = configuracao if configuracao is not None else ConfiguracaoTermica(modo=MODO_FISIOLOGICO)
    if len(df_horario) == 0:
        return pd.Series([], index=df_horario.index, dtype=int)
    instantes = pd.to_datetime(df_horario["data_hora"]).dt.floor("s")
    if getattr(instantes.dt, "tz", None) is not None:
        instantes = instantes.dt.tz_convert(None)
    if not instantes.is_monotonic_increasing:
        raise ValueError("rotular_status_horario: data_hora precisa estar em ordem crescente.")
    temperatura = pd.to_numeric(df_horario[VARIAVEL_TEMPERATURA_AR], errors="coerce").astype(float).to_numpy()
    solo = pd.to_numeric(df_horario[VARIAVEL_UMIDADE_SOLO], errors="coerce").astype(float).to_numpy()
    invalidos = int((~np.isfinite(temperatura)).sum() + (~np.isfinite(solo)).sum())
    if invalidos:
        raise ValueError(f"rotular_status_horario: {invalidos} valor(es) ausente(s) ou não finito(s); sanear antes.")

    faixa_temperatura = parametros.faixa(VARIAVEL_TEMPERATURA_AR)
    faixa_solo = parametros.faixa(VARIAVEL_UMIDADE_SOLO)
    indice_tempo = pd.DatetimeIndex(instantes.to_numpy())

    def classificar_vetor(valores: np.ndarray, faixa: FaixaParametro) -> np.ndarray:
        """``classificar_variavel`` vetorizada."""
        ideal = (valores >= faixa.ideal_min) & (valores <= faixa.ideal_max)
        atencao = (valores >= faixa.atencao_min) & (valores <= faixa.atencao_max)
        return np.where(ideal, STATUS_VERDE, np.where(atencao, STATUS_AMARELO, STATUS_VERMELHO))

    if cfg.modo != MODO_FISIOLOGICO:
        status = np.maximum(classificar_vetor(temperatura, faixa_temperatura), classificar_vetor(solo, faixa_solo))
        return pd.Series(status.astype(int), index=df_horario.index)

    serie_tempo = pd.Series(temperatura, index=indice_tempo)
    janela = serie_tempo.rolling(f"{HORAS_JANELA_MEDIA}h", closed="left")
    medias = janela.mean().round(CASAS_MEDIA).to_numpy()
    contagens = janela.count().fillna(0).to_numpy()
    usa_media = contagens >= HORAS_MINIMAS_MEDIA_24H
    referencia = np.where(usa_media, medias, temperatura)
    conforto = np.minimum(classificar_vetor(referencia, faixa_temperatura), STATUS_AMARELO)

    geada = temperatura <= limiar_geada_vermelho(cfg)

    dias = pd.Series(instantes.dt.date.to_numpy(), index=df_horario.index)
    temperatura_por_linha = pd.Series(temperatura, index=df_horario.index)
    maxima_hoje = temperatura_por_linha.groupby(dias.to_numpy()).cummax().to_numpy()
    maxima_por_dia = temperatura_por_linha.groupby(dias.to_numpy()).max().to_dict()
    maxima_ontem = np.array([maxima_por_dia.get(dia - timedelta(days=1), np.nan) for dia in dias], dtype=float)
    limiar_vermelho = limiar_calor_vermelho(cfg, faixa_temperatura)
    dias_unicos = sorted(set(dias))
    florada_por_dia = {dia: em_janela_florada(dia, cfg) for dia in dias_unicos}
    seca_por_dia = {dia: em_seca_programada(dia, cfg) for dia in dias_unicos}
    na_florada = np.array([florada_por_dia[dia] for dia in dias], dtype=bool)
    na_seca = np.array([seca_por_dia[dia] for dia in dias], dtype=bool)

    pode_vermelho = (maxima_hoje >= limiar_vermelho) & (temperatura >= faixa_temperatura.critico_max)
    ontem_quente = np.nan_to_num(maxima_ontem, nan=-np.inf) >= limiar_vermelho
    calor = np.where(pode_vermelho & (na_florada | ontem_quente), STATUS_VERMELHO,
                     np.where(maxima_hoje >= faixa_temperatura.critico_max, STATUS_AMARELO, STATUS_VERDE))
    status_temperatura = np.maximum(np.maximum(conforto, calor), np.where(geada, STATUS_VERMELHO, STATUS_VERDE))

    status_solo = classificar_vetor(solo, faixa_solo)
    seca_relaxada = na_seca & (solo < faixa_solo.ideal_min) & (solo >= faixa_solo.critico_min)
    status_solo = np.where(seca_relaxada, STATUS_AMARELO, status_solo)
    return pd.Series(np.maximum(status_temperatura, status_solo).astype(int), index=df_horario.index)
