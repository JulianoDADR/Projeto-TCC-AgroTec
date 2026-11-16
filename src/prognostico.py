"""Prognóstico de 12 h do AgroAnálise: antecipação de estresse hídrico e térmico.

O semáforo do instante (motor de regras + IA do instante) diz como a lavoura
está AGORA. Este módulo olha as últimas 24 h de leituras de CAMPO e responde
"vai piorar nas próximas 12 h?", sempre na mesma linguagem simples:

* **IA de prognóstico** (``models/modelo_prognostico.pkl``, treinada em
  ``treinamento_ia.py``): probabilidade de a lavoura entrar no Vermelho nas
  próximas 12 h, a partir de tendências (variações de 1/3/6/12 h), médias e
  extremos de 6 e 24 h, VPD, horas de estresse e margens até as bordas das
  faixas da cultura — tudo relativo às faixas, para valer em qualquer cultura.
* **Quando a terra seca** (``estimar_horas_ate_secar``): ETA até a borda de
  atenção do solo pela perda líquida das últimas 24 h.
* **Geada ainda à tarde** (``prever_temperatura_minima_noite``): a mínima da
  madrugada pelo ponto de orvalho do fim da tarde (17 h–19 h).

Regras de segurança da antecipação (``aplicar_antecipacao``):

* só vale para leituras de campo (``CAMPO_USB``) com pelo menos 18 das
  últimas 24 h com dados;
* só leva o semáforo de Verde para Amarelo — NUNCA cria um Vermelho e nunca
  rebaixa uma cor; o Vermelho continua sendo exclusividade do motor de regras
  e da IA do instante;
* altera somente ``status_predito``, ``status_alerta`` e ``mensagem_voz``
  (via ``dataclasses.replace``: campos extras do registro são preservados);
* nunca lança exceção: qualquer falha é registrada no log e a leitura segue
  como veio.

A saída ao agricultor continua sendo só a cor e a voz: o motivo e o prazo
viram frases curtas ("a terra deve ficar seca amanhã de manhã").

v3 — IA validada em dado real (todas as chaves lidas de
``configuracao_sistema``; ausentes = comportamento da v2):

* ``regra_temperatura = 'fisiologica'``: o aviso de geada usa o limiar
  fisiológico no abrigo (``limiar_geada_abrigo_c`` 2,0 °C +
  ``incerteza_sensor_ar_c`` 2,0 °C), coeficientes da mínima por região
  (local > complemento > pacote > padrão) e a margem conformal da estação;
  "frio sem geada" vira só uma frase de FRIO, sem mandar cobrir as mudas.
* ``prognostico_suspenso = true`` (portão de segurança de
  ``desempenho_local.py``): o gatilho do MODELO é ignorado (geada e secagem
  continuam) e a versão registrada ganha o sufixo ``-suspenso``.
* ``platt_local = 'a,b'``: recalibração local ``σ(a·logit(p) + b)`` antes do
  limiar (a probabilidade gravada continua a do modelo, para o próximo ajuste).
* Certeza falada em dois níveis quando o Amarelo vem do modelo: "É quase
  certo que …" (p ≥ 0,8) ou "Pode ser que …" — nunca números.

Persistência própria: tabela ``analise_antecipacao`` (1:1 com
``historico_leituras``, ``ON DELETE CASCADE``), criada sob demanda.

Este módulo importa apenas ``database``, ``gerador_dataset`` e
``referencia_climatica`` (nunca ``coletor_serial`` nem ``app``), para que os
dois possam carregá-lo de forma opcional com ``importlib``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import joblib
import numpy as np
import pandas as pd

from database import (
    CAMINHO_BANCO,
    CAMINHO_MODELO,
    MODO_CAMPO_USB,
    STATUS_AMARELO,
    STATUS_VERDE,
    STATUS_VERMELHO,
    VARIAVEL_TEMPERATURA_AR,
    VARIAVEL_UMIDADE_AR,
    VARIAVEL_UMIDADE_SOLO,
    FaixaParametro,
    ParametrosCultura,
    RegistroLeitura,
    obter_conexao,
)
from gerador_dataset import FORMATO_DATA_HORA, _juntar_acoes, classificar_variavel, ponto_orvalho_magnus
from referencia_climatica import estacao_do_mes, resolver_regiao

# ---------------------------------------------------------------------------
# Constantes públicas
# ---------------------------------------------------------------------------
HORIZONTE_PROGNOSTICO_HORAS: int = 12
JANELA_HISTORICO_HORAS: int = 24
HORAS_MINIMAS_HISTORICO: int = 18
CAMINHO_MODELO_PROGNOSTICO: Path = CAMINHO_MODELO.parent / "modelo_prognostico.pkl"
CAMINHO_COMPLEMENTO_PROGNOSTICO: Path = CAMINHO_MODELO.parent / "complemento_prognostico.json"
VERSAO_SEM_PROGNOSTICO: str = "SEM_PROGNOSTICO"
MOTIVOS: Tuple[str, ...] = ("SECA_CHEGANDO", "SECA_AGORA", "ENCHARCAMENTO", "CALOR_TARDE", "GEADA_NOITE", "FRIO", "NENHUM")
CHAVES_PACOTE_PROGNOSTICO: Tuple[str, ...] = (
    "modelo", "features", "limiar", "horizonte_horas", "culturas", "coef_tmin", "metricas", "versao", "treinado_em",
)

TETO_HORAS_ATE_SECAR: float = 72.0
SUBIDA_MAXIMA_SECAGEM_PP_H: float = 1.0  # subida maior que isto na janela = choveu/regou: sem estimativa
TAXA_MINIMA_SECAGEM_PP_H: float = 0.02
HORAS_FRASE_CONDICIONAL_SECA: float = 24.0
MARGEM_GEADA_C: float = 2.0
COEF_TMIN_PADRAO: Tuple[float, float, float] = (-1.0, 0.25, 0.75)  # Tmin = a + b·T_tarde + c·Td_tarde
HORA_INICIO_TARDE: int = 17
HORA_FIM_TARDE: int = 19  # a média do fim da tarde usa 17:00–19:59 (mesma janela do ajuste no treino)
HORA_FIM_MADRUGADA: int = 6  # o aviso de geada vale das 17 h às 5:59

# Regra de temperatura (chave configuracao_sistema.regra_temperatura; ausente = 'instantanea', a v2).
REGRA_TEMPERATURA_INSTANTANEA: str = "instantanea"
REGRA_TEMPERATURA_FISIOLOGICA: str = "fisiologica"
REGRAS_TEMPERATURA: Tuple[str, ...] = (REGRA_TEMPERATURA_INSTANTANEA, REGRA_TEMPERATURA_FISIOLOGICA)
# Geada no cafeeiro: temperatura no ABRIGO abaixo de 2 °C (Epamig/Embrapa Café,
# https://revistacultivar.com.br/noticias/pesquisadores-da-epamig-e-embrapa-cafe-orientam-cafeicultores-sobre-o-que-fazer-apos-as-geadas).
LIMIAR_GEADA_ABRIGO_PADRAO_C: float = 2.0
# Incerteza do sensor de ar somada ao limiar: ±2 °C no DHT11 (datasheet Aosong/ASAIR DHT11;
# DHT22 ±0,5 °C; SHT3x ±0,2 °C). Com o DHT11 o aviso fica em 2 + 2 = 4 °C.
INCERTEZA_SENSOR_AR_PADRAO_C: float = 2.0
# Certeza falada: "quase certo" a partir de 0,8 (abaixo disso, e acima do limiar, "pode ser").
# Comunicar a incerteza aumenta a obediência ao alerta (LeClerc & Joslyn, Risk Analysis 35(3), 2015,
# https://econpapers.repec.org/RePEc:wly:riskan:v:35:y:2015:i:3:p:385-395).
PROBABILIDADE_QUASE_CERTO: float = 0.8
NIVEIS_CERTEZA: Tuple[str, str] = ("quase_certo", "pode_ser")
_LIMITE_LOGIT: float = 1e-6
MINUTOS_MINIMOS_TARDE: float = 30.0
MAXIMO_FRASES_ANTECIPACAO: int = 4
EXPRESSOES_PRAZO: Tuple[str, ...] = (
    "daqui a pouco", "hoje de manhã", "hoje à tarde", "esta noite", "amanhã de manhã", "amanhã à tarde",
    "amanhã à noite", "em uns 2 dias", "em uns 3 dias",
)

# Grupos de features (o contrafactual de ``explicar_alerta`` neutraliza um grupo inteiro).
FEATURES_TEMPERATURA: Tuple[str, ...] = (
    "temp_var_1h", "temp_var_3h", "temp_var_6h", "temp_var_12h",
    "temp_media_6h", "temp_min_6h", "temp_max_6h", "temp_media_24h", "temp_min_24h", "temp_max_24h",
    "temp_margem_ideal_min", "temp_margem_ideal_max", "temp_margem_atencao_min", "temp_margem_atencao_max",
    "vpd_kpa", "orvalho_margem_critico", "horas_estresse_temperatura_24",
)
FEATURES_SOLO: Tuple[str, ...] = (
    "solo_var_1h", "solo_var_3h", "solo_var_6h", "solo_var_12h",
    "solo_media_6h", "solo_min_6h", "solo_max_6h", "solo_media_24h", "solo_min_24h", "solo_max_24h",
    "solo_margem_ideal_min", "solo_margem_ideal_max", "solo_margem_atencao_min", "solo_margem_atencao_max",
    "horas_ate_secar", "horas_estresse_solo_24",
)
FEATURES_COMUNS: Tuple[str, ...] = ("horas_estresse_24", "hora_sen", "hora_cos")
GRUPOS_FEATURES: Dict[str, Tuple[str, ...]] = {"temperatura": FEATURES_TEMPERATURA, "solo": FEATURES_SOLO}
COLUNAS_FEATURES_PROGNOSTICO: List[str] = list(FEATURES_TEMPERATURA) + list(FEATURES_SOLO) + list(FEATURES_COMUNS)

registrador: logging.Logger = logging.getLogger("agroanalise.prognostico")

_TRAVA_CACHE: threading.Lock = threading.Lock()
_CACHE_SERIE: Dict[Tuple[str, str, str, int, str], pd.DataFrame] = {}
_LIMITE_CACHE_SERIE: int = 64
_CACHE_MODELO: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
_CACHE_COMPLEMENTO: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}

_DDL_ANALISE: str = f"""
CREATE TABLE IF NOT EXISTS analise_antecipacao (
    id_leitura                  INTEGER PRIMARY KEY
                                REFERENCES historico_leituras(id_leitura) ON DELETE CASCADE,
    probabilidade_risco_futuro  REAL,
    horas_ate_secar             REAL,
    temperatura_minima_prevista REAL,
    motivo_principal            TEXT    NOT NULL
                                CHECK (motivo_principal IN ({", ".join(f"'{motivo}'" for motivo in MOTIVOS)})),
    prazo_horas                 REAL,
    horas_em_estresse           REAL,
    status_antecipado           INTEGER NOT NULL CHECK (status_antecipado IN (0, 1)),
    versao_prognostico          TEXT    NOT NULL,
    criado_em                   TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
)
"""


@dataclass
class ResultadoAntecipacao:
    """Resultado do prognóstico de UMA leitura (gravado em ``analise_antecipacao``).

    Attributes:
        status_antecipado: 1 quando a antecipação levou (ou levaria) o semáforo a Amarelo; senão 0.
        probabilidade_risco_futuro: P(Vermelho nas próximas 12 h) da IA de prognóstico (``None`` sem modelo).
        horas_ate_secar: horas até a terra chegar à borda de atenção (``None`` = não está secando).
        temperatura_minima_prevista: mínima prevista para a madrugada (``None`` fora das 17 h–6 h).
        motivo_principal: um dos ``MOTIVOS``.
        prazo_horas: em quantas horas o problema deve chegar (``None`` = sem prazo estimado).
        horas_em_estresse: horas seguidas fora da faixa ideal até agora (``None`` = a série não cobre o período).
        versao_prognostico: versão do pacote de prognóstico ou ``VERSAO_SEM_PROGNOSTICO``.
    """

    status_antecipado: int
    probabilidade_risco_futuro: Optional[float]
    horas_ate_secar: Optional[float]
    temperatura_minima_prevista: Optional[float]
    motivo_principal: str
    prazo_horas: Optional[float]
    horas_em_estresse: Optional[float]
    versao_prognostico: str


# ---------------------------------------------------------------------------
# Física auxiliar
# ---------------------------------------------------------------------------
def _pressao_saturacao_kpa(temperatura: np.ndarray) -> np.ndarray:
    """Pressão de saturação do vapor (kPa), fórmula de Tetens."""
    return 0.6108 * np.exp(17.27 * temperatura / (temperatura + 237.3))


def _largura(faixa: FaixaParametro) -> float:
    """Largura da faixa ideal (nunca zero)."""
    return max(1e-6, float(faixa.ideal_max - faixa.ideal_min))


# ---------------------------------------------------------------------------
# Features temporais e alvo
# ---------------------------------------------------------------------------
def _taxa_secagem_janela(valores: np.ndarray) -> float:
    """Perda líquida (pp/h, positiva = secando) pela inclinação de mínimos quadrados da janela.

    Devolve ``nan`` se a janela tiver menos de 6 pontos válidos ou se houver
    alguma subida maior que ``SUBIDA_MAXIMA_SECAGEM_PP_H`` (chuva ou rega).
    """
    pontos = np.asarray(valores, dtype=float)
    posicoes = np.arange(pontos.size, dtype=float)
    validos = np.isfinite(pontos)
    if validos.sum() < 6:
        return float("nan")
    subidas = np.diff(pontos[validos])
    if subidas.size and float(np.max(subidas)) > SUBIDA_MAXIMA_SECAGEM_PP_H:
        return float("nan")
    x = posicoes[validos]
    y = pontos[validos]
    x_centro = x - x.mean()
    denominador = float(np.sum(x_centro ** 2))
    if denominador <= 0.0:
        return float("nan")
    return float(-np.sum(x_centro * (y - y.mean())) / denominador)


def _eta_a_partir_da_taxa(solo_atual: float, taxa: float, borda: float) -> Optional[float]:
    """Horas até ``borda`` dada a taxa de secagem; ``None`` se não estiver secando ou passar do teto."""
    if not math.isfinite(solo_atual):
        return None
    if solo_atual <= borda:
        return 0.0
    if not math.isfinite(taxa) or taxa <= TAXA_MINIMA_SECAGEM_PP_H:
        return None
    eta = (solo_atual - borda) / taxa
    if eta > TETO_HORAS_ATE_SECAR:
        return None
    return float(eta)


def estimar_horas_ate_secar(
    serie_horaria_solo: pd.Series, faixa_solo: FaixaParametro, borda: str = "atencao_min"
) -> Optional[float]:
    """Estima em quantas horas a umidade do solo chega à ``borda`` da faixa (padrão: ``atencao_min``).

    Usa a perda líquida das últimas 24 h (inclinação de mínimos quadrados,
    robusta ao ruído do sensor e ao ciclo dia/noite da evapotranspiração).

    Returns:
        ``0.0`` se a terra já está na borda ou abaixo dela; ``None`` se houve
        subida maior que 1 pp/h na janela (choveu ou regou), se a taxa de
        secagem for ≤ 0,02 pp/h, se houver menos de 6 horas válidas ou se o
        prazo passar de 72 h; senão, as horas estimadas.
    """
    valores = pd.to_numeric(pd.Series(serie_horaria_solo), errors="coerce").astype(float).dropna().to_numpy()
    if valores.size == 0:
        return None
    janela = valores[-JANELA_HISTORICO_HORAS:]
    limite = float(getattr(faixa_solo, borda))
    taxa = _taxa_secagem_janela(janela)
    atual = float(janela[-1])
    if atual <= limite:
        return 0.0
    if not math.isfinite(taxa):
        return None
    return _eta_a_partir_da_taxa(atual, taxa, limite)


def status_vetorizado(valores: np.ndarray, faixa: FaixaParametro) -> np.ndarray:
    """Classificação vetorizada idêntica a ``classificar_variavel`` (NaN → NaN)."""
    resultado = np.where((valores >= faixa.ideal_min) & (valores <= faixa.ideal_max), STATUS_VERDE,
                         np.where((valores >= faixa.atencao_min) & (valores <= faixa.atencao_max),
                                  STATUS_AMARELO, STATUS_VERMELHO)).astype(float)
    resultado[~np.isfinite(valores)] = np.nan
    return resultado


def calcular_features_temporais(df_horario: pd.DataFrame, parametros: ParametrosCultura) -> pd.DataFrame:
    """Calcula ``COLUNAS_FEATURES_PROGNOSTICO`` para cada linha de uma série horária de UMA cultura.

    A entrada precisa de ``data_hora``, ``temperatura_ar``, ``umidade_ar`` e
    ``umidade_solo``. As horas são alinhadas numa grade horária (buracos de
    até 3 h são interpolados; buracos maiores ficam sem feature), e todas as
    grandezas são RELATIVAS às faixas da cultura: temperatura e solo divididos
    pela largura da faixa ideal, medidos a partir de ``ideal_min``; margens
    até ``ideal_min``/``ideal_max``/``atencao_min``/``atencao_max``.

    Returns:
        DataFrame com o mesmo índice e a mesma ordem da entrada; linhas sem
        histórico suficiente (primeiras 24 h) ficam com NaN.
    """
    faixa_t = parametros.faixa(VARIAVEL_TEMPERATURA_AR)
    faixa_s = parametros.faixa(VARIAVEL_UMIDADE_SOLO)
    largura_t, largura_s = _largura(faixa_t), _largura(faixa_s)
    momentos = pd.to_datetime(df_horario["data_hora"]).dt.floor("h")
    base = pd.DataFrame({
        "t": pd.to_numeric(df_horario["temperatura_ar"], errors="coerce").to_numpy(dtype=float),
        "ur": pd.to_numeric(df_horario["umidade_ar"], errors="coerce").to_numpy(dtype=float),
        "s": pd.to_numeric(df_horario["umidade_solo"], errors="coerce").to_numpy(dtype=float),
    }, index=pd.DatetimeIndex(momentos.to_numpy()))
    base = base[~base.index.duplicated(keep="last")].sort_index()
    if base.empty:
        return pd.DataFrame(np.nan, index=df_horario.index, columns=COLUNAS_FEATURES_PROGNOSTICO)
    grade = base.asfreq("h").interpolate(method="linear", limit=3, limit_area="inside")
    t, ur, s = grade["t"], grade["ur"], grade["s"]
    feats = pd.DataFrame(index=grade.index)

    def relativo_t(valores: pd.Series) -> pd.Series:
        """Temperatura relativa: 0 = ideal_min, 1 = ideal_max."""
        return (valores - faixa_t.ideal_min) / largura_t

    def relativo_s(valores: pd.Series) -> pd.Series:
        """Umidade do solo relativa: 0 = ideal_min, 1 = ideal_max."""
        return (valores - faixa_s.ideal_min) / largura_s

    for horas in (1, 3, 6, 12):
        feats[f"temp_var_{horas}h"] = (t - t.shift(horas)) / largura_t
        feats[f"solo_var_{horas}h"] = (s - s.shift(horas)) / largura_s
    for janela in (6, 24):
        rolagem_t = t.rolling(janela, min_periods=janela)
        rolagem_s = s.rolling(janela, min_periods=janela)
        feats[f"temp_media_{janela}h"] = relativo_t(rolagem_t.mean())
        feats[f"temp_min_{janela}h"] = relativo_t(rolagem_t.min())
        feats[f"temp_max_{janela}h"] = relativo_t(rolagem_t.max())
        feats[f"solo_media_{janela}h"] = relativo_s(rolagem_s.mean())
        feats[f"solo_min_{janela}h"] = relativo_s(rolagem_s.min())
        feats[f"solo_max_{janela}h"] = relativo_s(rolagem_s.max())
    feats["temp_margem_ideal_min"] = (t - faixa_t.ideal_min) / largura_t
    feats["temp_margem_ideal_max"] = (faixa_t.ideal_max - t) / largura_t
    feats["temp_margem_atencao_min"] = (t - faixa_t.atencao_min) / largura_t
    feats["temp_margem_atencao_max"] = (faixa_t.atencao_max - t) / largura_t
    feats["solo_margem_ideal_min"] = (s - faixa_s.ideal_min) / largura_s
    feats["solo_margem_ideal_max"] = (faixa_s.ideal_max - s) / largura_s
    feats["solo_margem_atencao_min"] = (s - faixa_s.atencao_min) / largura_s
    feats["solo_margem_atencao_max"] = (faixa_s.atencao_max - s) / largura_s
    feats["vpd_kpa"] = _pressao_saturacao_kpa(t.to_numpy()) * (1.0 - np.clip(ur.to_numpy(), 0.0, 100.0) / 100.0)
    orvalho = np.asarray(ponto_orvalho_magnus(t.to_numpy(), ur.to_numpy()), dtype=float)
    feats["orvalho_margem_critico"] = (orvalho - faixa_t.critico_min) / largura_t

    status_t = status_vetorizado(t.to_numpy(), faixa_t)
    status_s = status_vetorizado(s.to_numpy(), faixa_s)
    fora_t = pd.Series(np.where(np.isfinite(status_t), (status_t > 0).astype(float), np.nan), index=grade.index)
    fora_s = pd.Series(np.where(np.isfinite(status_s), (status_s > 0).astype(float), np.nan), index=grade.index)
    fora_qualquer = pd.Series(np.where(np.isfinite(status_t) & np.isfinite(status_s),
                                       ((status_t > 0) | (status_s > 0)).astype(float), np.nan), index=grade.index)
    janela = JANELA_HISTORICO_HORAS
    feats["horas_estresse_temperatura_24"] = fora_t.rolling(janela, min_periods=janela).sum()
    feats["horas_estresse_solo_24"] = fora_s.rolling(janela, min_periods=janela).sum()
    feats["horas_estresse_24"] = fora_qualquer.rolling(janela, min_periods=janela).sum()

    taxas = s.rolling(janela, min_periods=janela).apply(_taxa_secagem_janela, raw=True)
    etas = [(_eta_a_partir_da_taxa(float(atual), float(taxa), faixa_s.atencao_min)
             if math.isfinite(float(atual)) and (math.isfinite(float(taxa)) or float(atual) <= faixa_s.atencao_min)
             else None)
            for atual, taxa in zip(s.to_numpy(), taxas.to_numpy())]
    horas_ate_secar = np.array([TETO_HORAS_ATE_SECAR if eta is None else min(eta, TETO_HORAS_ATE_SECAR) for eta in etas])
    horas_ate_secar[~np.isfinite(s.to_numpy())] = np.nan
    horas_ate_secar[: max(0, janela - 1)] = np.nan
    feats["horas_ate_secar"] = horas_ate_secar
    hora = grade.index.hour.to_numpy().astype(float)
    feats["hora_sen"] = np.sin(2.0 * np.pi * hora / 24.0)
    feats["hora_cos"] = np.cos(2.0 * np.pi * hora / 24.0)

    resultado = feats.reindex(pd.DatetimeIndex(momentos.to_numpy()))[COLUNAS_FEATURES_PROGNOSTICO]
    resultado.index = df_horario.index
    return resultado


def calcular_alvo_risco_futuro(status_horario: pd.Series, horizonte: int = HORIZONTE_PROGNOSTICO_HORAS) -> pd.Series:
    """Alvo do prognóstico: 1 se ``max(status[t+1 .. t+horizonte]) == 2``, senão 0.

    A série deve estar em ordem horária. As últimas ``horizonte`` horas (sem
    futuro completo) ficam ``NaN``.
    """
    if int(horizonte) < 1:
        raise ValueError("horizonte deve ser >= 1.")
    status = pd.to_numeric(pd.Series(status_horario), errors="coerce").astype(float)
    futuros = pd.concat([status.shift(-passo) for passo in range(1, int(horizonte) + 1)], axis=1)
    maximo = futuros.max(axis=1, skipna=False)
    alvo = (maximo >= STATUS_VERMELHO).astype(float)
    alvo[maximo.isna()] = np.nan
    return alvo


# ---------------------------------------------------------------------------
# Leitura do histórico de campo
# ---------------------------------------------------------------------------
_CONSULTA_SERIE: str = """
    SELECT strftime('%Y-%m-%d %H:00:00', h.data_hora) AS hora,
           AVG(h.temperatura_ar) AS temperatura_ar,
           AVG(h.umidade_ar)     AS umidade_ar,
           AVG(h.umidade_solo)   AS umidade_solo,
           COUNT(*)              AS n_leituras,
           MIN(h.data_hora)      AS primeira_leitura,
           MAX(h.data_hora)      AS ultima_leitura
      FROM historico_leituras h
      JOIN culturas c ON c.id_cultura = h.id_cultura
     WHERE c.codigo = ? AND h.origem = ? AND h.flag_anomalia = 0
       AND h.data_hora >= ? AND h.data_hora <= ?
     GROUP BY hora
     ORDER BY hora
"""
_COLUNAS_SERIE: List[str] = ["data_hora", "temperatura_ar", "umidade_ar", "umidade_solo",
                             "n_leituras", "primeira_leitura", "ultima_leitura"]


def _serie_vazia() -> pd.DataFrame:
    """Série horária vazia com as colunas e tipos esperados."""
    return pd.DataFrame({
        "data_hora": pd.Series([], dtype="datetime64[ns]"),
        "temperatura_ar": pd.Series([], dtype=float),
        "umidade_ar": pd.Series([], dtype=float),
        "umidade_solo": pd.Series([], dtype=float),
        "n_leituras": pd.Series([], dtype=np.int64),
        "primeira_leitura": pd.Series([], dtype="datetime64[ns]"),
        "ultima_leitura": pd.Series([], dtype="datetime64[ns]"),
    })


def _consultar_serie(caminho: Path, codigo: str, origem: str, inicio: datetime, fim: datetime) -> pd.DataFrame:
    """SELECT agregado por hora entre ``inicio`` e ``fim`` (inclusive), só leitura."""
    conexao = obter_conexao(caminho)
    try:
        linhas = conexao.execute(_CONSULTA_SERIE, (codigo, origem, inicio.strftime(FORMATO_DATA_HORA),
                                                  fim.strftime(FORMATO_DATA_HORA))).fetchall()
    finally:
        conexao.close()
    if not linhas:
        return _serie_vazia()
    df = pd.DataFrame([tuple(linha) for linha in linhas], columns=_COLUNAS_SERIE)
    for coluna in ("data_hora", "primeira_leitura", "ultima_leitura"):
        df[coluna] = pd.to_datetime(df[coluna], errors="coerce")
    for coluna in ("temperatura_ar", "umidade_ar", "umidade_solo"):
        df[coluna] = df[coluna].astype(float)
    df["n_leituras"] = df["n_leituras"].astype(np.int64)
    return df.dropna(subset=["data_hora"]).reset_index(drop=True)


def limpar_cache_serie_horaria() -> None:
    """Esvazia o cache em memória de ``buscar_serie_horaria`` (útil em testes e após retenção)."""
    with _TRAVA_CACHE:
        _CACHE_SERIE.clear()


def buscar_serie_horaria(
    codigo_cultura: str,
    caminho_banco: Path = CAMINHO_BANCO,
    horas: int = JANELA_HISTORICO_HORAS,
    agora: Optional[datetime] = None,
    origem: str = MODO_CAMPO_USB,
) -> pd.DataFrame:
    """Série horária (médias por hora) das leituras de UMA origem, sem anomalias, das últimas ``horas`` horas.

    Consulta somente leitura em ``historico_leituras``:
    ``AVG(...) GROUP BY strftime('%Y-%m-%d %H:00:00', data_hora)``, filtrando
    ``origem`` e ``flag_anomalia = 0``, de ``hora cheia de agora − horas``
    até ``agora``. As horas completas ficam em cache por
    (banco, cultura, origem, horas, hora corrente); a hora corrente (ainda
    recebendo leituras) é sempre consultada de novo.

    Returns:
        DataFrame ``data_hora`` (início da hora), ``temperatura_ar``,
        ``umidade_ar``, ``umidade_solo``, ``n_leituras``,
        ``primeira_leitura`` e ``ultima_leitura``; vazio se o banco não existir.
    """
    caminho = Path(caminho_banco)
    if not caminho.is_file():
        return _serie_vazia()
    momento = agora if agora is not None else datetime.now()
    hora_corrente = momento.replace(minute=0, second=0, microsecond=0)
    inicio = hora_corrente - timedelta(hours=int(horas))
    chave = (str(caminho.resolve()), str(codigo_cultura), str(origem), int(horas), hora_corrente.isoformat())
    with _TRAVA_CACHE:
        passado = _CACHE_SERIE.get(chave)
    if passado is None:
        passado = _consultar_serie(caminho, codigo_cultura, origem, inicio, hora_corrente - timedelta(seconds=1))
        with _TRAVA_CACHE:
            if len(_CACHE_SERIE) >= _LIMITE_CACHE_SERIE:
                _CACHE_SERIE.clear()
            _CACHE_SERIE[chave] = passado
    atual = _consultar_serie(caminho, codigo_cultura, origem, hora_corrente, momento)
    if atual.empty:
        return passado.copy()
    if passado.empty:
        return atual
    return pd.concat([passado, atual], ignore_index=True)


def _instante_do_registro(registro: RegistroLeitura) -> Optional[datetime]:
    """Converte ``registro.data_hora`` em ``datetime`` (``None`` se inválido)."""
    try:
        return datetime.strptime(str(registro.data_hora).strip()[:19], FORMATO_DATA_HORA)
    except (TypeError, ValueError):
        return None


def _incluir_leitura_atual(serie: pd.DataFrame, registro: RegistroLeitura, agora: datetime) -> pd.DataFrame:
    """Soma a leitura que ainda vai ser gravada à média da hora corrente."""
    if int(registro.flag_anomalia or 0) != 0:
        return serie
    hora = pd.Timestamp(agora.replace(minute=0, second=0, microsecond=0))
    valores = {"temperatura_ar": float(registro.temperatura_ar), "umidade_ar": float(registro.umidade_ar),
               "umidade_solo": float(registro.umidade_solo)}
    if not all(math.isfinite(valor) for valor in valores.values()):
        return serie
    resultado = serie.copy()
    instante = pd.Timestamp(agora)
    posicoes = np.flatnonzero(resultado["data_hora"].to_numpy() == hora.to_datetime64()) if not resultado.empty else []
    if len(posicoes):
        indice = resultado.index[int(posicoes[0])]
        quantidade = int(resultado.at[indice, "n_leituras"])
        for coluna, valor in valores.items():
            resultado.at[indice, coluna] = (float(resultado.at[indice, coluna]) * quantidade + valor) / (quantidade + 1)
        resultado.at[indice, "n_leituras"] = quantidade + 1
        resultado.at[indice, "ultima_leitura"] = max(pd.Timestamp(resultado.at[indice, "ultima_leitura"]), instante)
        resultado.at[indice, "primeira_leitura"] = min(pd.Timestamp(resultado.at[indice, "primeira_leitura"]), instante)
        return resultado
    nova = pd.DataFrame([{"data_hora": hora, **valores, "n_leituras": 1,
                          "primeira_leitura": instante, "ultima_leitura": instante}])
    if resultado.empty:
        return nova
    return pd.concat([resultado, nova], ignore_index=True).sort_values("data_hora").reset_index(drop=True)


def _horas_com_dados(serie: pd.DataFrame, agora: datetime, janela: int = JANELA_HISTORICO_HORAS) -> int:
    """Quantas das últimas ``janela`` horas (incluindo a corrente) têm ao menos uma leitura."""
    if serie.empty:
        return 0
    hora_corrente = pd.Timestamp(agora.replace(minute=0, second=0, microsecond=0))
    inicio = hora_corrente - pd.Timedelta(hours=janela - 1)
    horas = pd.to_datetime(serie["data_hora"])
    return int(((horas >= inicio) & (horas <= hora_corrente) & (serie["n_leituras"] > 0)).sum())


def _regularizar_serie(serie: pd.DataFrame, agora: datetime, janela: int = JANELA_HISTORICO_HORAS) -> pd.DataFrame:
    """Grade horária completa de ``agora − janela`` até a hora corrente, com interpolação linear dos buracos."""
    hora_corrente = pd.Timestamp(agora.replace(minute=0, second=0, microsecond=0))
    grade = pd.date_range(end=hora_corrente, periods=janela + 1, freq="h")
    dados = serie.set_index(pd.to_datetime(serie["data_hora"]))[["temperatura_ar", "umidade_ar", "umidade_solo"]]
    dados = dados[~dados.index.duplicated(keep="last")].reindex(grade)
    dados = dados.interpolate(method="linear", limit_direction="both")
    dados.index.name = "data_hora"
    return dados.reset_index()


# ---------------------------------------------------------------------------
# Linguagem: prazo falado
# ---------------------------------------------------------------------------
def falar_prazo(horas: float, agora: datetime) -> str:
    """Traduz "daqui a N horas" para uma expressão que o agricultor entende.

    Exemplos: ``falar_prazo(14, 18 h)`` → ``'amanhã de manhã'``;
    ``falar_prazo(2, qualquer hora)`` → ``'daqui a pouco'``. Nunca usa
    números, exceto em ``'em uns 2 dias'``/``'em uns 3 dias'``.
    """
    prazo = max(0.0, float(horas))
    if prazo <= 3.0:
        return "daqui a pouco"
    alvo = agora + timedelta(hours=prazo)
    dias = (alvo.date() - agora.date()).days
    hora = alvo.hour
    if dias == 0:
        if hora < 6:
            return "esta noite"
        if hora < 12:
            return "hoje de manhã"
        if hora < 18:
            return "hoje à tarde"
        return "esta noite"
    if dias == 1:
        if hora < 6:
            return "esta noite" if agora.hour >= 6 else "amanhã de manhã"
        if hora < 12:
            return "amanhã de manhã"
        if hora < 18:
            return "amanhã à tarde"
        return "amanhã à noite"
    return "em uns 2 dias" if prazo <= 60.0 else "em uns 3 dias"


# ---------------------------------------------------------------------------
# Modelo de prognóstico
# ---------------------------------------------------------------------------
def _pacote_valido(pacote: Any) -> bool:
    """Confere o formato mínimo do pacote de prognóstico."""
    if not isinstance(pacote, dict):
        return False
    modelo = pacote.get("modelo")
    features = pacote.get("features")
    if modelo is None or not callable(getattr(modelo, "predict_proba", None)):
        return False
    if not isinstance(features, (list, tuple)) or not features or not all(isinstance(f, str) for f in features):
        return False
    try:
        limiar = float(pacote.get("limiar"))
    except (TypeError, ValueError):
        return False
    return 0.0 <= limiar <= 1.0


def carregar_modelo_prognostico(caminho: Path = CAMINHO_MODELO_PROGNOSTICO) -> Optional[Dict[str, Any]]:
    """Carrega o pacote ``modelo_prognostico.pkl`` (cache pela data de modificação do arquivo).

    Returns:
        O pacote, ou ``None`` se o arquivo não existir, estiver corrompido ou
        não tiver o formato esperado (o sistema segue com secagem e geada,
        que não dependem do modelo).
    """
    arquivo = Path(caminho)
    try:
        modificado = arquivo.stat().st_mtime
    except OSError:
        return None
    chave = str(arquivo.resolve())
    with _TRAVA_CACHE:
        em_cache = _CACHE_MODELO.get(chave)
    if em_cache is not None and em_cache[0] == modificado:
        return em_cache[1]
    try:
        pacote = joblib.load(arquivo)
    except Exception as erro:  # noqa: BLE001 — pickle pode falhar de muitas formas
        registrador.warning("Modelo de prognóstico em %s não pôde ser lido (%s); seguindo sem ele.", arquivo, erro)
        pacote = None
    if pacote is not None and not _pacote_valido(pacote):
        registrador.warning("Modelo de prognóstico em %s tem formato inesperado; seguindo sem ele.", arquivo)
        pacote = None
    if pacote is not None:
        pacote["features"] = list(pacote["features"])
        pacote["limiar"] = float(pacote["limiar"])
    with _TRAVA_CACHE:
        _CACHE_MODELO[chave] = (modificado, pacote)
    return pacote


def predizer_risco_futuro(linha_features: pd.DataFrame, pacote: Dict[str, Any]) -> float:
    """Probabilidade (0–1) de Vermelho nas próximas 12 h para a ÚLTIMA linha de ``linha_features``.

    Valores ausentes são preenchidos com as medianas do treino
    (``pacote['metricas']['medianas_features']``) ou com 0.

    Raises:
        KeyError: se faltar alguma feature do pacote.
    """
    features = list(pacote["features"])
    linha = linha_features.iloc[[-1]][features].astype(float)
    if linha.isna().any(axis=None):
        medianas = (pacote.get("metricas") or {}).get("medianas_features") or {}
        linha = linha.fillna({coluna: float(medianas.get(coluna, 0.0)) for coluna in features}).fillna(0.0)
    modelo = pacote["modelo"]
    probabilidades = np.asarray(modelo.predict_proba(linha), dtype=float)
    classes = list(getattr(modelo, "classes_", [0, 1]))
    coluna = classes.index(1) if 1 in classes else probabilidades.shape[1] - 1
    return float(np.clip(probabilidades[0, coluna], 0.0, 1.0))


def carregar_complemento_prognostico(
    caminho: Path = CAMINHO_COMPLEMENTO_PROGNOSTICO,
) -> Optional[Dict[str, Any]]:
    """Carrega ``models/complemento_prognostico.json`` (cache pela data de modificação).

    O complemento (gerado por ``treinamento_ia.salvar_complemento_prognostico``)
    fica FORA do pacote de 9 chaves e traz ``climatologia_por_cultura``,
    ``coef_tmin_por_regiao`` e ``quantis_tmin_por_regiao``.

    Returns:
        O dicionário, ou ``None`` se o arquivo não existir, estiver corrompido
        ou não for um objeto JSON (nunca lança exceção).
    """
    arquivo = Path(caminho)
    try:
        modificado = arquivo.stat().st_mtime
    except OSError:
        return None
    chave = str(arquivo.resolve())
    with _TRAVA_CACHE:
        em_cache = _CACHE_COMPLEMENTO.get(chave)
    if em_cache is not None and em_cache[0] == modificado:
        return em_cache[1]
    try:
        conteudo = json.loads(arquivo.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as erro:
        registrador.warning("Complemento do prognóstico em %s não pôde ser lido (%s); seguindo sem ele.", arquivo, erro)
        conteudo = None
    if conteudo is not None and not isinstance(conteudo, dict):
        registrador.warning("Complemento do prognóstico em %s não é um objeto JSON; seguindo sem ele.", arquivo)
        conteudo = None
    with _TRAVA_CACHE:
        _CACHE_COMPLEMENTO[chave] = (modificado, conteudo)
    return conteudo


@dataclass(frozen=True)
class ConfiguracaoPrognostico:
    """Chaves de ``configuracao_sistema`` que mudam o prognóstico (padrões = comportamento da v2).

    Attributes:
        regra_temperatura: ``'instantanea'`` (padrão, chave ausente) ou ``'fisiologica'``.
        limiar_geada_abrigo_c: geada no abrigo (padrão 2,0 °C, Epamig/Embrapa Café).
        incerteza_sensor_ar_c: incerteza do sensor de ar somada ao limiar (padrão 2,0 °C, DHT11).
        regiao_referencia: região da referência climática (vazio = a padrão da cultura).
        prognostico_suspenso: portão de segurança local ligado (gatilho do modelo ignorado).
        platt_local: ``(a, b)`` da recalibração local ou ``None``.
        coef_tmin_local: ``(a, b, c)`` da mínima ajustados no sítio ou ``None``.
        quantis_tmin_local: margens conformais locais ``{'seca': q, 'aguas': q}`` (pode faltar estação).
    """

    regra_temperatura: str = REGRA_TEMPERATURA_INSTANTANEA
    limiar_geada_abrigo_c: float = LIMIAR_GEADA_ABRIGO_PADRAO_C
    incerteza_sensor_ar_c: float = INCERTEZA_SENSOR_AR_PADRAO_C
    regiao_referencia: str = ""
    prognostico_suspenso: bool = False
    platt_local: Optional[Tuple[float, float]] = None
    coef_tmin_local: Optional[Tuple[float, float, float]] = None
    quantis_tmin_local: Dict[str, float] = dataclasses.field(default_factory=dict)

    @property
    def fisiologica(self) -> bool:
        """``True`` quando a regra de temperatura é a fisiológica."""
        return self.regra_temperatura == REGRA_TEMPERATURA_FISIOLOGICA


_CHAVES_CONFIGURACAO_PROGNOSTICO: Tuple[str, ...] = (
    "regra_temperatura", "limiar_geada_abrigo_c", "incerteza_sensor_ar_c", "regiao_referencia",
    "prognostico_suspenso", "platt_local", "coef_tmin_local", "quantis_tmin_local",
)


def _texto_para_bool(valor: Any) -> bool:
    """Converte 'true'/'1'/'sim' (e bool) em ``True``; qualquer outra coisa em ``False``."""
    if isinstance(valor, bool):
        return valor
    return str(valor or "").strip().lower() in ("true", "1", "sim", "yes", "verdadeiro")


def _texto_para_float(valor: Any, padrao: float) -> float:
    """Número finito do texto; ``padrao`` se vazio ou inválido."""
    try:
        numero = float(str(valor).strip().replace(",", ".")) if str(valor or "").strip() else padrao
    except ValueError:
        return padrao
    return numero if math.isfinite(numero) else padrao


def _texto_para_tupla(valor: Any, tamanho: int) -> Optional[Tuple[float, ...]]:
    """``'a,b[,c]'`` → tupla de floats finitos com ``tamanho`` itens; ``None`` se vazio ou inválido."""
    texto = str(valor or "").strip()
    if not texto:
        return None
    partes = [parte.strip() for parte in texto.replace(";", ",").split(",")]
    if len(partes) != tamanho:
        return None
    try:
        numeros = tuple(float(parte) for parte in partes)
    except ValueError:
        return None
    return numeros if all(math.isfinite(numero) for numero in numeros) else None


def _texto_para_quantis(valor: Any) -> Dict[str, float]:
    """JSON ``{'seca': q, 'aguas': q}`` → dicionário só com estações conhecidas e valores finitos ≥ 0."""
    texto = str(valor or "").strip()
    if not texto:
        return {}
    try:
        conteudo = json.loads(texto)
    except ValueError:
        return {}
    if not isinstance(conteudo, dict):
        return {}
    resultado: Dict[str, float] = {}
    for estacao in ("seca", "aguas"):
        try:
            numero = float(conteudo.get(estacao))
        except (TypeError, ValueError):
            continue
        if math.isfinite(numero) and numero >= 0.0:
            resultado[estacao] = numero
    return resultado


def ler_configuracao_prognostico(caminho_banco: Path = CAMINHO_BANCO) -> ConfiguracaoPrognostico:
    """Lê, numa só consulta, as chaves de ``configuracao_sistema`` que afetam o prognóstico.

    Cada chave ausente ou inválida assume o padrão de ``ConfiguracaoPrognostico``
    (os mesmos da Frente 1: ``regra_temperatura='instantanea'``,
    ``limiar_geada_abrigo_c=2.0``, ``incerteza_sensor_ar_c=2.0``,
    ``prognostico_suspenso=false``...). Se o banco não existir ou a leitura
    falhar, devolve todos os padrões — o comportamento da v2. Nunca lança
    exceção e nunca cria o banco.
    """
    caminho = Path(caminho_banco)
    if not caminho.is_file():
        return ConfiguracaoPrognostico()
    try:
        conexao = obter_conexao(caminho)
        try:
            marcadores = ", ".join("?" for _ in _CHAVES_CONFIGURACAO_PROGNOSTICO)
            linhas = conexao.execute(f"SELECT chave, valor FROM configuracao_sistema WHERE chave IN ({marcadores})",
                                     _CHAVES_CONFIGURACAO_PROGNOSTICO).fetchall()
        finally:
            conexao.close()
    except Exception as erro:  # noqa: BLE001 — sem configuração legível, vale o comportamento da v2
        registrador.warning("Configuração do prognóstico ilegível em %s (%s); usando os padrões.", caminho, erro)
        return ConfiguracaoPrognostico()
    valores = {str(linha[0]): linha[1] for linha in linhas}
    regra = str(valores.get("regra_temperatura") or "").strip().lower()
    platt = _texto_para_tupla(valores.get("platt_local"), 2)
    coef = _texto_para_tupla(valores.get("coef_tmin_local"), 3)
    return ConfiguracaoPrognostico(
        regra_temperatura=regra if regra in REGRAS_TEMPERATURA else REGRA_TEMPERATURA_INSTANTANEA,
        limiar_geada_abrigo_c=_texto_para_float(valores.get("limiar_geada_abrigo_c"), LIMIAR_GEADA_ABRIGO_PADRAO_C),
        incerteza_sensor_ar_c=_texto_para_float(valores.get("incerteza_sensor_ar_c"), INCERTEZA_SENSOR_AR_PADRAO_C),
        regiao_referencia=str(valores.get("regiao_referencia") or "").strip(),
        prognostico_suspenso=_texto_para_bool(valores.get("prognostico_suspenso")),
        platt_local=None if platt is None else (float(platt[0]), float(platt[1])),
        coef_tmin_local=None if coef is None else (float(coef[0]), float(coef[1]), float(coef[2])),
        quantis_tmin_local=_texto_para_quantis(valores.get("quantis_tmin_local")),
    )


def calibrar_platt(probabilidade: float, platt: Optional[Tuple[float, float]]) -> float:
    """Recalibração de Platt: ``σ(a·logit(p) + b)``; sem ``platt`` devolve ``p``.

    Platt/temperature scaling como recalibração sob mudança de domínio:
    https://arxiv.org/pdf/2507.07780.
    """
    p = float(probabilidade)
    if platt is None or not math.isfinite(p):
        return p
    a, b = platt
    limitado = min(max(p, _LIMITE_LOGIT), 1.0 - _LIMITE_LOGIT)
    z = float(a) * math.log(limitado / (1.0 - limitado)) + float(b)
    if z >= 0.0:
        return 1.0 / (1.0 + math.exp(-z))
    exponencial = math.exp(z)
    return exponencial / (1.0 + exponencial)


def _numeros_validos(valores: Any, tamanho: int) -> Optional[Tuple[float, ...]]:
    """Sequência de ``tamanho`` números finitos (de lista/tupla JSON ou do pacote); senão ``None``."""
    if not isinstance(valores, (list, tuple)) or len(valores) != tamanho:
        return None
    try:
        numeros = tuple(float(valor) for valor in valores)
    except (TypeError, ValueError):
        return None
    return numeros if all(math.isfinite(numero) for numero in numeros) else None


def escolher_coef_tmin(
    configuracao: ConfiguracaoPrognostico, complemento: Optional[Dict[str, Any]], regiao: Optional[str],
    pacote: Optional[Dict[str, Any]],
) -> Tuple[Tuple[float, float, float], str]:
    """Coeficientes da mínima na regra fisiológica: local > região do complemento > pacote > padrão.

    Returns:
        ``((a, b, c), origem)`` com origem ``'local'``, ``'regiao'``, ``'pacote'`` ou ``'padrao'``.
    """
    if configuracao.coef_tmin_local is not None:
        return configuracao.coef_tmin_local, "local"
    if complemento is not None and regiao:
        por_regiao = complemento.get("coef_tmin_por_regiao")
        coef = _numeros_validos(por_regiao.get(regiao) if isinstance(por_regiao, dict) else None, 3)
        if coef is not None:
            return (coef[0], coef[1], coef[2]), "regiao"
    if pacote is not None:
        coef = _numeros_validos(pacote.get("coef_tmin"), 3)
        if coef is not None:
            return (coef[0], coef[1], coef[2]), "pacote"
    return COEF_TMIN_PADRAO, "padrao"


def margem_geada(
    configuracao: ConfiguracaoPrognostico, complemento: Optional[Dict[str, Any]], regiao: Optional[str], mes: int,
) -> float:
    """Margem (°C) da mínima prevista: quantil conformal q90 da estação do ``mes``.

    Prioridade: ``quantis_tmin_local`` do banco > ``quantis_tmin_por_regiao``
    do complemento > ``MARGEM_GEADA_C``.
    """
    estacao = estacao_do_mes(int(mes))
    if estacao in configuracao.quantis_tmin_local:
        return float(configuracao.quantis_tmin_local[estacao])
    if complemento is not None and regiao:
        por_regiao = complemento.get("quantis_tmin_por_regiao")
        quantis = por_regiao.get(regiao) if isinstance(por_regiao, dict) else None
        if isinstance(quantis, dict):
            try:
                valor = float(quantis.get(estacao))
            except (TypeError, ValueError):
                valor = float("nan")
            if math.isfinite(valor) and valor >= 0.0:
                return valor
    return MARGEM_GEADA_C


# ---------------------------------------------------------------------------
# Geada ainda à tarde
# ---------------------------------------------------------------------------
def prever_temperatura_minima_noite(
    serie_horaria_ar: pd.DataFrame,
    instante: datetime,
    coef_tmin: Optional[Tuple[float, float, float]] = None,
) -> Optional[float]:
    """Prevê a mínima da madrugada pelo ponto de orvalho do fim da tarde.

    ``Tmin = a + b·T_tarde + c·Td_tarde``, com ``Td = ponto_orvalho_magnus``
    e ``(a, b, c) = coef_tmin`` (padrão ``COEF_TMIN_PADRAO``; o treino ajusta
    por mínimos quadrados nas noites do simulador). ``T_tarde``/``UR_tarde``
    são a média das leituras de 17:00 até o instante (no máximo até 19:59,
    a mesma janela do ajuste).

    Vale com o instante entre 17 h e 23:59 (tarde do mesmo dia) ou entre 0 h e
    5:59 (tarde do dia anterior), e exige pelo menos 30 min de dados na janela.

    Returns:
        A mínima prevista (°C) ou ``None`` fora da janela 17 h–6 h, sem dados
        suficientes ou com valores inválidos.
    """
    if serie_horaria_ar is None or serie_horaria_ar.empty:
        return None
    hora = instante.hour
    if hora >= HORA_INICIO_TARDE:
        dia = instante.date()
    elif hora < HORA_FIM_MADRUGADA:
        dia = (instante - timedelta(days=1)).date()
    else:
        return None
    inicio = pd.Timestamp(datetime(dia.year, dia.month, dia.day, HORA_INICIO_TARDE))
    fim_tarde = pd.Timestamp(datetime(dia.year, dia.month, dia.day, HORA_FIM_TARDE, 59, 59))
    fim = min(pd.Timestamp(instante), fim_tarde)
    horas = pd.to_datetime(serie_horaria_ar["data_hora"])
    filtro = (horas >= inicio) & (horas <= fim)
    janela = serie_horaria_ar.loc[filtro.to_numpy()]
    if janela.empty:
        return None
    temperatura = pd.to_numeric(janela["temperatura_ar"], errors="coerce").to_numpy(dtype=float)
    umidade = pd.to_numeric(janela["umidade_ar"], errors="coerce").to_numpy(dtype=float)
    pesos = (pd.to_numeric(janela["n_leituras"], errors="coerce").fillna(1.0).to_numpy(dtype=float)
             if "n_leituras" in janela.columns else np.ones(len(janela)))
    validos = np.isfinite(temperatura) & np.isfinite(umidade) & (pesos > 0)
    if not validos.any():
        return None
    if {"primeira_leitura", "ultima_leitura"} <= set(janela.columns):
        primeira = pd.to_datetime(janela["primeira_leitura"]).min()
        ultima = pd.to_datetime(janela["ultima_leitura"]).max()
        cobertura = (min(ultima, fim) - max(primeira, inicio)).total_seconds() / 60.0
    else:
        primeira = horas[filtro].min()
        ultima = min(horas[filtro].max() + pd.Timedelta(minutes=60), fim)
        cobertura = (ultima - primeira).total_seconds() / 60.0
    if not math.isfinite(cobertura) or cobertura < MINUTOS_MINIMOS_TARDE:
        return None
    temperatura_tarde = float(np.average(temperatura[validos], weights=pesos[validos]))
    umidade_tarde = float(np.average(umidade[validos], weights=pesos[validos]))
    orvalho = float(ponto_orvalho_magnus(temperatura_tarde, umidade_tarde))
    a, b, c = coef_tmin if coef_tmin is not None else COEF_TMIN_PADRAO
    previsao = float(a) + float(b) * temperatura_tarde + float(c) * orvalho
    return float(previsao) if math.isfinite(previsao) else None


# ---------------------------------------------------------------------------
# Explicação: motivo, prazo e memória de estresse
# ---------------------------------------------------------------------------
def _horas_seguidas_fora(valores: Sequence[float], faixa: FaixaParametro) -> Optional[float]:
    """Horas seguidas fora da faixa ideal no fim da série (``None`` se a série inteira estiver fora)."""
    pontos = [float(v) for v in valores if v is not None and math.isfinite(float(v))]
    if not pontos:
        return None
    contagem = 0
    for valor in reversed(pontos):
        if faixa.ideal_min <= valor <= faixa.ideal_max:
            return float(contagem)
        contagem += 1
    return None


def _horas_ate_hora_do_dia(agora: datetime, hora_alvo: int) -> float:
    """Horas até a próxima ocorrência de ``hora_alvo`` (hoje ou amanhã)."""
    alvo = agora.replace(hour=hora_alvo, minute=0, second=0, microsecond=0)
    if alvo <= agora:
        alvo += timedelta(days=1)
    return (alvo - agora).total_seconds() / 3600.0


def _features_neutras(linha: pd.DataFrame, grupo: str, parametros: ParametrosCultura) -> pd.DataFrame:
    """Cópia da linha com o grupo de features no "ponto neutro" (meio da faixa ideal, estável, sem estresse)."""
    neutra = linha.copy()
    if grupo == "solo":
        for coluna in ("solo_var_1h", "solo_var_3h", "solo_var_6h", "solo_var_12h"):
            neutra[coluna] = 0.0
        for coluna in ("solo_media_6h", "solo_min_6h", "solo_max_6h", "solo_media_24h", "solo_min_24h", "solo_max_24h"):
            neutra[coluna] = 0.5
        faixa = parametros.faixa(VARIAVEL_UMIDADE_SOLO)
        largura = _largura(faixa)
        meio = (faixa.ideal_min + faixa.ideal_max) / 2.0
        neutra["solo_margem_ideal_min"] = (meio - faixa.ideal_min) / largura
        neutra["solo_margem_ideal_max"] = (faixa.ideal_max - meio) / largura
        neutra["solo_margem_atencao_min"] = (meio - faixa.atencao_min) / largura
        neutra["solo_margem_atencao_max"] = (faixa.atencao_max - meio) / largura
        neutra["horas_ate_secar"] = TETO_HORAS_ATE_SECAR
        neutra["horas_estresse_solo_24"] = 0.0
        neutra["horas_estresse_24"] = linha["horas_estresse_temperatura_24"]
    else:
        for coluna in ("temp_var_1h", "temp_var_3h", "temp_var_6h", "temp_var_12h"):
            neutra[coluna] = 0.0
        for coluna in ("temp_media_6h", "temp_min_6h", "temp_max_6h", "temp_media_24h", "temp_min_24h", "temp_max_24h"):
            neutra[coluna] = 0.5
        faixa = parametros.faixa(VARIAVEL_TEMPERATURA_AR)
        largura = _largura(faixa)
        meio = (faixa.ideal_min + faixa.ideal_max) / 2.0
        neutra["temp_margem_ideal_min"] = (meio - faixa.ideal_min) / largura
        neutra["temp_margem_ideal_max"] = (faixa.ideal_max - meio) / largura
        neutra["temp_margem_atencao_min"] = (meio - faixa.atencao_min) / largura
        neutra["temp_margem_atencao_max"] = (faixa.atencao_max - meio) / largura
        umidade_meio = 70.0
        if VARIAVEL_UMIDADE_AR in parametros.faixas:
            faixa_ar = parametros.faixa(VARIAVEL_UMIDADE_AR)
            umidade_meio = (faixa_ar.ideal_min + faixa_ar.ideal_max) / 2.0
        neutra["vpd_kpa"] = float(_pressao_saturacao_kpa(np.array([meio]))[0] * (1.0 - umidade_meio / 100.0))
        neutra["orvalho_margem_critico"] = (float(ponto_orvalho_magnus(meio, umidade_meio)) - faixa.critico_min) / largura
        neutra["horas_estresse_temperatura_24"] = 0.0
        neutra["horas_estresse_24"] = linha["horas_estresse_solo_24"]
    return neutra


def _lado_temperatura(contexto: Dict[str, Any]) -> str:
    """'calor' ou 'frio': o lado da faixa de temperatura mais ameaçado nas últimas 24 h."""
    parametros: ParametrosCultura = contexto["parametros"]
    faixa = parametros.faixa(VARIAVEL_TEMPERATURA_AR)
    serie = contexto.get("serie")
    if isinstance(serie, pd.DataFrame) and not serie.empty:
        temperaturas = pd.to_numeric(serie["temperatura_ar"], errors="coerce").dropna()
    else:
        temperaturas = pd.Series([float(contexto.get("temperatura_ar", (faixa.ideal_min + faixa.ideal_max) / 2.0))])
    tmin_prevista = contexto.get("temperatura_minima_prevista")
    minimo = float(temperaturas.min()) if not temperaturas.empty else faixa.ideal_min
    if tmin_prevista is not None and math.isfinite(float(tmin_prevista)):
        minimo = min(minimo, float(tmin_prevista))
    maximo = float(temperaturas.max()) if not temperaturas.empty else faixa.ideal_max
    distancia_frio = (minimo - faixa.atencao_min) / _largura(faixa)
    distancia_calor = (faixa.atencao_max - maximo) / _largura(faixa)
    return "calor" if distancia_calor < distancia_frio else "frio"


def _lado_solo(contexto: Dict[str, Any]) -> str:
    """'seca' ou 'encharcamento': o lado da faixa do solo mais próximo agora."""
    parametros: ParametrosCultura = contexto["parametros"]
    faixa = parametros.faixa(VARIAVEL_UMIDADE_SOLO)
    solo = float(contexto.get("umidade_solo", (faixa.ideal_min + faixa.ideal_max) / 2.0))
    return "seca" if (solo - faixa.ideal_min) <= (faixa.ideal_max - solo) else "encharcamento"


def explicar_alerta(contexto: Dict[str, Any]) -> Dict[str, Any]:
    """Escolhe o motivo principal, o prazo e as horas em estresse de um alerta antecipado.

    Chaves usadas de ``contexto``: ``parametros`` (obrigatória), ``gatilho``
    (``'geada'``, ``'secagem'``, ``'modelo'`` ou ``None``), ``agora``,
    ``serie`` (grade horária com ``temperatura_ar``/``umidade_solo``),
    ``umidade_solo``, ``temperatura_ar``, ``horas_ate_secar``,
    ``temperatura_minima_prevista``, ``linha_features``, ``pacote`` e
    ``probabilidade``.

    Quando o Amarelo vem do modelo, o motivo sai de um CONTRAFACTUAL
    determinístico: o grupo de features ``'solo'`` e depois o
    ``'temperatura'`` são levados ao ponto neutro (meio da faixa ideal,
    estável) e vence o grupo cuja neutralização mais reduz a probabilidade
    (empate: o lado com a menor margem até a borda de atenção).

    Returns:
        ``{"motivo_principal", "prazo_horas", "horas_em_estresse", "grupo"}``.
    """
    parametros: ParametrosCultura = contexto["parametros"]
    agora: datetime = contexto.get("agora") or datetime.now()
    gatilho = contexto.get("gatilho")
    eta = contexto.get("horas_ate_secar")
    serie = contexto.get("serie")
    faixa_t = parametros.faixa(VARIAVEL_TEMPERATURA_AR)
    faixa_s = parametros.faixa(VARIAVEL_UMIDADE_SOLO)
    solos = list(serie["umidade_solo"]) if isinstance(serie, pd.DataFrame) and "umidade_solo" in serie else [
        contexto.get("umidade_solo")]
    temperaturas = list(serie["temperatura_ar"]) if isinstance(serie, pd.DataFrame) and "temperatura_ar" in serie else [
        contexto.get("temperatura_ar")]

    grupo: Optional[str] = None
    if gatilho == "geada":
        grupo = "temperatura"
    elif gatilho == "secagem":
        grupo = "solo"
    elif gatilho == "modelo":
        linha = contexto.get("linha_features")
        pacote = contexto.get("pacote")
        probabilidade = contexto.get("probabilidade")
        reducoes: Dict[str, float] = {"solo": 0.0, "temperatura": 0.0}
        if isinstance(linha, pd.DataFrame) and pacote is not None and probabilidade is not None:
            for nome in ("solo", "temperatura"):
                neutra = _features_neutras(linha.iloc[[-1]], nome, parametros)
                reducoes[nome] = float(probabilidade) - predizer_risco_futuro(neutra, pacote)
        if abs(reducoes["solo"] - reducoes["temperatura"]) > 1e-9:
            grupo = max(reducoes, key=lambda chave: reducoes[chave])
        else:
            solo_atual = float(contexto.get("umidade_solo", (faixa_s.ideal_min + faixa_s.ideal_max) / 2.0))
            temperatura_atual = float(contexto.get("temperatura_ar", (faixa_t.ideal_min + faixa_t.ideal_max) / 2.0))
            margem_solo = min(solo_atual - faixa_s.atencao_min, faixa_s.atencao_max - solo_atual) / _largura(faixa_s)
            margem_temp = min(temperatura_atual - faixa_t.atencao_min,
                              faixa_t.atencao_max - temperatura_atual) / _largura(faixa_t)
            grupo = "solo" if margem_solo <= margem_temp else "temperatura"

    if grupo is None:
        return {"motivo_principal": "NENHUM", "prazo_horas": None, "horas_em_estresse": None, "grupo": None}
    if grupo == "solo":
        horas_estresse = _horas_seguidas_fora(solos, faixa_s)
        if _lado_solo(contexto) == "encharcamento":
            return {"motivo_principal": "ENCHARCAMENTO", "prazo_horas": None, "horas_em_estresse": horas_estresse,
                    "grupo": grupo}
        solo_atual = float(contexto.get("umidade_solo", solos[-1] if solos else faixa_s.ideal_min))
        if eta is None and solo_atual < faixa_s.ideal_min:
            return {"motivo_principal": "SECA_AGORA", "prazo_horas": 0.0, "horas_em_estresse": horas_estresse,
                    "grupo": grupo}
        return {"motivo_principal": "SECA_CHEGANDO", "prazo_horas": None if eta is None else float(eta),
                "horas_em_estresse": horas_estresse, "grupo": grupo}
    horas_estresse = _horas_seguidas_fora(temperaturas, faixa_t)
    if gatilho == "geada":
        return {"motivo_principal": "GEADA_NOITE", "prazo_horas": _horas_ate_hora_do_dia(agora, 5),
                "horas_em_estresse": horas_estresse, "grupo": grupo}
    if _lado_temperatura(contexto) == "calor":
        return {"motivo_principal": "CALOR_TARDE", "prazo_horas": _horas_ate_hora_do_dia(agora, 14),
                "horas_em_estresse": horas_estresse, "grupo": grupo}
    return {"motivo_principal": "FRIO", "prazo_horas": _horas_ate_hora_do_dia(agora, 5),
            "horas_em_estresse": horas_estresse, "grupo": grupo}


def _acao_rega(prazo_horas: Optional[float], agora: datetime) -> str:
    """Quando regar, conforme o prazo e a hora do dia (mantém 'Regue agora', 'fim da tarde' e 'de manhã')."""
    hora = agora.hour
    if prazo_horas is not None and (prazo_horas <= 3.0 or (hora < 17 and prazo_horas < 17 - hora)):
        return "Regue agora, devagar, até a terra ficar úmida."
    if hora < 20 and hora >= 6:
        return "Regue hoje no fim da tarde."
    return "Regue amanhã cedo, de manhã."


# Frase de prazo com a certeza falada em dois níveis (só quando o Amarelo vem do modelo).
# Fixas, sem número nem porcentagem; verbo no subjuntivo depois de "que". "{prazo}" = falar_prazo(...).
# v4: "que tenha geada" no lugar de "que geie" (subjuntivo raro, que o agricultor não usa e que
# também não tinha nenhuma palavra-chave da causa FRIO_GEADA).
FRASES_CERTEZA: Dict[str, Dict[str, str]] = {
    "SECA_CHEGANDO": {"quase_certo": "É quase certo que a terra fique muito seca {prazo}.",
                      "pode_ser": "Pode ser que a terra fique muito seca {prazo}."},
    "SECA_AGORA": {"quase_certo": "É quase certo que a terra continue seca nas próximas horas.",
                   "pode_ser": "Pode ser que a terra continue seca nas próximas horas."},
    "ENCHARCAMENTO": {"quase_certo": "É quase certo que a terra fique molhada demais {prazo}.",
                      "pode_ser": "Pode ser que a terra fique molhada demais {prazo}."},
    "CALOR_TARDE": {"quase_certo": "É quase certo que faça muito calor {prazo}.",
                    "pode_ser": "Pode ser que faça muito calor {prazo}."},
    "GEADA_NOITE": {"quase_certo": "É quase certo que tenha geada {prazo}.",
                    "pode_ser": "Pode ser que tenha geada {prazo}."},
    "FRIO": {"quase_certo": "É quase certo que faça frio {prazo}.",
             "pode_ser": "Pode ser que faça frio {prazo}."},
    "NENHUM": {"quase_certo": "", "pode_ser": ""},
}


def nivel_certeza(probabilidade: float) -> str:
    """``'quase_certo'`` (p ≥ ``PROBABILIDADE_QUASE_CERTO``) ou ``'pode_ser'``."""
    return "quase_certo" if float(probabilidade) >= PROBABILIDADE_QUASE_CERTO else "pode_ser"


def _memoria_de_estresse(motivo: str, horas_em_estresse: Optional[float]) -> Optional[str]:
    """Frase "há quanto tempo" que acompanha a certeza (a mesma informação da frase da v2)."""
    estresse = None if horas_em_estresse is None else int(round(float(horas_em_estresse)))
    if motivo in ("SECA_CHEGANDO", "SECA_AGORA"):
        if estresse is None:
            return "A terra está seca há mais de um dia."
        return f"A terra está seca há {estresse} horas." if estresse >= 2 else None
    if motivo == "ENCHARCAMENTO" and estresse is not None and estresse >= 2:
        return f"A terra está molhada demais há {estresse} horas."
    if motivo == "CALOR_TARDE" and estresse is not None and estresse >= 2:
        return f"Está quente há {estresse} horas."
    return None


def gerar_frase_antecipacao(
    motivo: str,
    prazo_horas: Optional[float],
    horas_em_estresse: Optional[float],
    agora: datetime,
    parametros: ParametrosCultura,
    *,
    probabilidade: Optional[float] = None,
) -> str:
    """Frases curtas (sem a abertura) que dizem o motivo, o prazo e a ação.

    Exemplos: ``'Ainda está tudo bem, mas a terra está secando e deve ficar
    seca amanhã de manhã. Regue hoje no fim da tarde.'`` e ``'A terra está
    seca há 9 horas. Regue agora.'``. Devolve ``''`` para ``'NENHUM'``.

    Com ``probabilidade`` (só quando o Amarelo veio do MODELO), a frase do
    prazo troca por ``FRASES_CERTEZA[motivo][nivel]``: "É quase certo que …"
    com p ≥ 0,8 e "Pode ser que …" abaixo disso — nunca um número. A
    memória de estresse ("há 9 horas") e as frases de ação (as mesmas
    palavras-chave da v2, usadas pelos pictogramas) são mantidas. Com
    ``probabilidade=None`` a saída é idêntica à da v2.

    Raises:
        ValueError: motivo fora de ``MOTIVOS``.
    """
    if motivo not in MOTIVOS:
        raise ValueError(f"Motivo desconhecido: {motivo!r}. Válidos: {', '.join(MOTIVOS)}.")
    frase_v2 = _frase_antecipacao_v2(motivo, prazo_horas, horas_em_estresse, agora)
    if probabilidade is None or not frase_v2 or not math.isfinite(float(probabilidade)):
        return frase_v2
    prazo_falado = falar_prazo(prazo_horas, agora) if prazo_horas is not None else "nas próximas horas"
    certeza = FRASES_CERTEZA[motivo][nivel_certeza(float(probabilidade))].format(prazo=prazo_falado)
    acoes = _dividir_frases(frase_v2)[1:]
    memoria = _memoria_de_estresse(motivo, horas_em_estresse)
    return " ".join(([memoria] if memoria else []) + [certeza] + acoes)


def _frase_antecipacao_v2(
    motivo: str, prazo_horas: Optional[float], horas_em_estresse: Optional[float], agora: datetime
) -> str:
    """Frase da v2 (motivo + prazo + ação), sem nível de certeza."""
    prazo_falado = falar_prazo(prazo_horas, agora) if prazo_horas is not None else "nas próximas horas"
    estresse = None if horas_em_estresse is None else int(round(float(horas_em_estresse)))
    hora = agora.hour
    if motivo == "SECA_CHEGANDO":
        if estresse is None:
            inicio = f"A terra está seca há mais de um dia e deve ficar muito seca {prazo_falado}."
        elif estresse >= 2:
            inicio = f"A terra está seca há {estresse} horas e deve ficar muito seca {prazo_falado}."
        elif estresse == 1:
            inicio = f"A terra começou a secar e deve ficar muito seca {prazo_falado}."
        elif prazo_horas is None:
            inicio = "Ainda está tudo bem, mas a terra pode secar nas próximas horas."
        else:
            inicio = f"Ainda está tudo bem, mas a terra está secando e deve ficar seca {prazo_falado}."
        return f"{inicio} {_acao_rega(prazo_horas, agora)}"
    if motivo == "SECA_AGORA":
        if estresse is None:
            return "A terra está seca há mais de um dia. Regue agora."
        if estresse >= 2:
            return f"A terra está seca há {estresse} horas. Regue agora."
        return "A terra está seca. Regue agora."
    if motivo == "ENCHARCAMENTO":
        inicio = (f"A terra está molhada demais há {estresse} horas." if estresse is not None and estresse >= 2
                  else "A terra está ficando molhada demais.")
        return f"{inicio} Não regue hoje e abra valetas se a água empoçar."
    if motivo == "CALOR_TARDE":
        inicio = (f"Está quente há {estresse} horas e deve esquentar mais {prazo_falado}."
                  if estresse is not None and estresse >= 2 else f"Deve fazer muito calor {prazo_falado}.")
        return f"{inicio} Faça sombra nas mudas novas e regue no fim da tarde para refrescar."
    if motivo == "GEADA_NOITE":
        if HORA_INICIO_TARDE <= hora <= 18:
            return "Esta noite pode gear. Antes de escurecer, cubra as mudas novas e chegue terra no pé das plantas."
        if hora > 18:
            return "Esta noite pode gear. Cubra as mudas novas agora e chegue terra no pé das plantas."
        if hora < HORA_FIM_MADRUGADA:
            return "Pode gear ainda nesta madrugada. Cubra as mudas novas agora e chegue terra no pé das plantas."
        return "Esta noite deve fazer muito frio. Proteja as mudas novas e chegue terra no pé das plantas."
    if motivo == "FRIO":
        if hora < HORA_FIM_MADRUGADA:
            return "Vai fazer mais frio nesta madrugada. Proteja as mudas novas."
        return "Esta noite vai fazer frio. Proteja as mudas novas."
    return ""


def _dividir_frases(texto: str) -> List[str]:
    """Separa um texto em frases terminadas por ponto, exclamação ou interrogação."""
    frases: List[str] = []
    atual = ""
    for caractere in texto.strip():
        atual += caractere
        if caractere in ".!?":
            if atual.strip():
                frases.append(atual.strip())
            atual = ""
    if atual.strip():
        frases.append(atual.strip())
    return frases


def _acao_complementar_da_regra(
    registro: RegistroLeitura, parametros: ParametrosCultura, motivo: str, fisiologica: bool = False
) -> Optional[str]:
    """Ação do Amarelo da regra quando ele vem de outra variável que não a do motivo antecipado.

    Na regra fisiológica o Amarelo térmico do dia (frio ou calor sem dano) é
    só informativo ("Não precisa fazer nada"), então não ganha ação de
    temperatura aqui.
    """
    if registro.status_regra != STATUS_AMARELO:
        return None
    faixa_t = parametros.faixa(VARIAVEL_TEMPERATURA_AR)
    faixa_s = parametros.faixa(VARIAVEL_UMIDADE_SOLO)
    temperatura, solo = float(registro.temperatura_ar), float(registro.umidade_solo)
    if motivo not in ("SECA_CHEGANDO", "SECA_AGORA", "ENCHARCAMENTO"):
        if solo < faixa_s.ideal_min:
            return "Regue hoje no fim da tarde."
        if solo > faixa_s.ideal_max:
            return "Não regue hoje. Espere a terra secar um pouco."
    if motivo not in ("CALOR_TARDE", "GEADA_NOITE", "FRIO") and not fisiologica:
        if temperatura > faixa_t.ideal_max:
            return "Regue no fim da tarde para refrescar as plantas."
        if temperatura < faixa_t.ideal_min:
            return "Proteja as mudas do frio hoje à noite."
    return None


def _mensagem_antecipada(
    registro: RegistroLeitura, parametros: ParametrosCultura, explicacao: Dict[str, Any], agora: datetime,
    probabilidade: Optional[float] = None, fisiologica: bool = False,
) -> str:
    """Mensagem do Amarelo antecipado: abertura 'Atenção com a plantação de …' + motivo/prazo + ação (≤ 4 frases).

    ``probabilidade`` (só para o gatilho do modelo) liga a certeza falada. A
    montagem final (``gerador_dataset._juntar_acoes``, v4) deixa uma ordem por
    ação: a rega do complemento não repete a rega do motivo, e "Não regue hoje"
    da terra molhada ganha da rega para refrescar do calor.
    """
    abertura = f"Atenção com a plantação de {parametros.nome_comum.lower()}."
    frase = gerar_frase_antecipacao(explicacao["motivo_principal"], explicacao["prazo_horas"],
                                    explicacao["horas_em_estresse"], agora, parametros, probabilidade=probabilidade)
    frases = [abertura] + _dividir_frases(frase)
    complemento = _acao_complementar_da_regra(registro, parametros, explicacao["motivo_principal"], fisiologica)
    if complemento and complemento not in frases:
        frases.append(complemento)
    return " ".join(_juntar_acoes(frases[:MAXIMO_FRASES_ANTECIPACAO]))


# Motivos da regra fisiológica (Frente 1, ``registro.motivo_regra``) → motivo do registro de análise.
_MOTIVO_REGRA_PARA_ANALISE: Dict[str, str] = {
    "GEADA_AGORA": "GEADA_NOITE",
    "CALOR_FLORADA": "CALOR_TARDE",
    "CALOR_SEGUIDO": "CALOR_TARDE",
    "CALOR_DIA": "CALOR_TARDE",
    "DIA_QUENTE": "CALOR_TARDE",
    "DIA_FRIO": "FRIO",
    "SECA_PROGRAMADA": "SECA_AGORA",
}


def _motivo_da_regra(motivo_regra: str) -> Optional[str]:
    """Traduz ``registro.motivo_regra`` para ``MOTIVOS`` (``None`` se não houver tradução segura)."""
    motivo = str(motivo_regra or "").strip().upper()
    if not motivo:
        return None
    if motivo in _MOTIVO_REGRA_PARA_ANALISE:
        return _MOTIVO_REGRA_PARA_ANALISE[motivo]
    if motivo in MOTIVOS and motivo != "NENHUM":
        return motivo
    if motivo.startswith("CALOR"):
        return "CALOR_TARDE"
    if motivo.startswith("GEADA"):
        return "GEADA_NOITE"
    if motivo.startswith("SECA") or motivo.startswith("SOLO_SECO"):
        return "SECA_AGORA"
    if motivo.startswith("ENCHARC") or motivo.startswith("SOLO_ENCHARC"):
        return "ENCHARCAMENTO"
    return None


def _motivo_do_vermelho(registro: RegistroLeitura, parametros: ParametrosCultura) -> str:
    """Motivo (para o registro de análise) de uma leitura que já está Vermelha pela regra.

    Usa ``registro.motivo_regra`` (regra fisiológica da Frente 1) quando ele
    existe e tem tradução: ``GEADA_AGORA`` → ``GEADA_NOITE``, ``CALOR_*`` →
    ``CALOR_TARDE``; senão classifica pelas faixas, como na v2.
    """
    traduzido = _motivo_da_regra(getattr(registro, "motivo_regra", "") or "")
    if traduzido is not None:
        return traduzido
    faixa_t = parametros.faixa(VARIAVEL_TEMPERATURA_AR)
    faixa_s = parametros.faixa(VARIAVEL_UMIDADE_SOLO)
    solo, temperatura = float(registro.umidade_solo), float(registro.temperatura_ar)
    if classificar_variavel(solo, faixa_s) == STATUS_VERMELHO:
        return "SECA_AGORA" if solo < faixa_s.atencao_min else "ENCHARCAMENTO"
    if classificar_variavel(temperatura, faixa_t) == STATUS_VERMELHO:
        if temperatura > faixa_t.atencao_max:
            return "CALOR_TARDE"
        return "GEADA_NOITE" if temperatura < faixa_t.critico_min else "FRIO"
    return "NENHUM"


# ---------------------------------------------------------------------------
# Orquestração em tempo real
# ---------------------------------------------------------------------------
def aplicar_antecipacao(
    registro: RegistroLeitura,
    parametros: ParametrosCultura,
    caminho_banco: Path = CAMINHO_BANCO,
    agora: Optional[datetime] = None,
    pacote_prognostico: Optional[Dict[str, Any]] = None,
) -> Tuple[RegistroLeitura, Optional[ResultadoAntecipacao]]:
    """Aplica o prognóstico de 12 h a UMA leitura antes de ela ser gravada.

    Fluxo: série horária de campo das últimas 24 h (+ a leitura atual) →
    features temporais → P(Vermelho em 12 h) (se houver modelo) → ETA até a
    terra secar → mínima prevista da madrugada (17 h–6 h) → decisão.

    A antecipação vale quando ``registro.status_regra < 2`` e ocorre ao menos
    um gatilho: ``P ≥ pacote['limiar']``; ETA até ``atencao_min`` ≤ 12 h; ou
    mínima prevista < ``critico_min + MARGEM_GEADA_C``. Então devolve
    ``replace(registro, status_predito=max(·,1), status_alerta=max(·,1),
    mensagem_voz=nova)``. Sem gatilho, ainda pode acrescentar UMA frase de
    frio leve (mínima prevista < ``atencao_min``) e/ou a frase condicional de
    seca (ETA ≤ 24 h) à mensagem, sem mudar a cor.

    Chaves de ``configuracao_sistema`` (``ler_configuracao_prognostico``;
    ausentes = tudo como acima):

    * ``regra_temperatura='fisiologica'``: geada quando ``Tmin_prevista −
      margem ≤ limiar_geada_abrigo_c + incerteza_sensor_ar_c`` (coeficientes
      ``escolher_coef_tmin``; margem ``margem_geada``); frio leve sem geada
      só acrescenta a frase de FRIO;
    * ``prognostico_suspenso=true``: o gatilho do modelo é ignorado e a
      versão registrada recebe ``-suspenso``;
    * ``platt_local='a,b'``: ``P`` recalibrada antes do limiar e usada na
      certeza falada.

    Args:
        registro: leitura já classificada pelo coletor (ainda não gravada).
        parametros: faixas da cultura do registro.
        caminho_banco: banco com o histórico de campo.
        agora: instante da leitura (padrão: ``registro.data_hora``).
        pacote_prognostico: pacote já carregado; ``None`` carrega
            ``CAMINHO_MODELO_PROGNOSTICO``; ``{}`` força a operação sem modelo.

    Returns:
        ``(registro_final, resultado)``; ``(registro, None)`` se a origem não
        for ``CAMPO_USB``, se houver menos de 18 das últimas 24 h com dados de
        campo ou em qualquer falha (nunca lança exceção).
    """
    try:
        if registro.origem != MODO_CAMPO_USB:
            return registro, None
        momento = agora or _instante_do_registro(registro) or datetime.now()
        serie = buscar_serie_horaria(parametros.codigo, caminho_banco, JANELA_HISTORICO_HORAS, momento, MODO_CAMPO_USB)
        serie = _incluir_leitura_atual(serie, registro, momento)
        if _horas_com_dados(serie, momento) < HORAS_MINIMAS_HISTORICO:
            return registro, None

        if pacote_prognostico is None:
            pacote = carregar_modelo_prognostico(CAMINHO_MODELO_PROGNOSTICO)
        else:
            pacote = pacote_prognostico if _pacote_valido(pacote_prognostico) else None
        configuracao = ler_configuracao_prognostico(caminho_banco)
        grade = _regularizar_serie(serie, momento)
        features = calcular_features_temporais(grade, parametros)
        linha = features.iloc[[-1]]
        probabilidade = predizer_risco_futuro(linha, pacote) if pacote is not None else None
        probabilidade_decisao = (None if probabilidade is None
                                 else calibrar_platt(probabilidade, configuracao.platt_local))
        faixa_t = parametros.faixa(VARIAVEL_TEMPERATURA_AR)
        faixa_s = parametros.faixa(VARIAVEL_UMIDADE_SOLO)
        eta = estimar_horas_ate_secar(grade["umidade_solo"], faixa_s, "atencao_min")
        if configuracao.fisiologica:
            complemento = carregar_complemento_prognostico(CAMINHO_COMPLEMENTO_PROGNOSTICO)
            regiao = resolver_regiao(parametros.codigo, configuracao.regiao_referencia)
            coef_escolhido, _ = escolher_coef_tmin(configuracao, complemento, regiao, pacote)
            tmin = prever_temperatura_minima_noite(serie, momento, coef_escolhido)
            limite_geada = configuracao.limiar_geada_abrigo_c + configuracao.incerteza_sensor_ar_c
            gatilho_geada = tmin is not None and (
                tmin - margem_geada(configuracao, complemento, regiao, momento.month) <= limite_geada)
        else:
            coef = tuple(pacote["coef_tmin"]) if pacote is not None and pacote.get("coef_tmin") is not None else None
            tmin = prever_temperatura_minima_noite(serie, momento, coef)  # type: ignore[arg-type]
            gatilho_geada = tmin is not None and tmin < faixa_t.critico_min + MARGEM_GEADA_C
        gatilho_secagem = eta is not None and eta <= HORIZONTE_PROGNOSTICO_HORAS
        gatilho_modelo = (not configuracao.prognostico_suspenso and probabilidade_decisao is not None
                          and probabilidade_decisao >= float(pacote["limiar"]))  # type: ignore[index]
        gatilho = "geada" if gatilho_geada else "secagem" if gatilho_secagem else "modelo" if gatilho_modelo else None
        antecipa = registro.status_regra < STATUS_VERMELHO and gatilho is not None
        frio_leve = tmin is not None and not gatilho_geada and tmin < faixa_t.atencao_min
        seca_condicional = (eta is not None and HORIZONTE_PROGNOSTICO_HORAS < eta <= HORAS_FRASE_CONDICIONAL_SECA
                            and classificar_variavel(float(registro.umidade_solo), faixa_s) < STATUS_VERMELHO)

        contexto: Dict[str, Any] = {
            "parametros": parametros, "gatilho": gatilho, "agora": momento, "serie": grade,
            "umidade_solo": float(registro.umidade_solo), "temperatura_ar": float(registro.temperatura_ar),
            "horas_ate_secar": eta, "temperatura_minima_prevista": tmin, "linha_features": linha,
            "pacote": pacote, "probabilidade": probabilidade,
        }
        if registro.status_regra >= STATUS_VERMELHO:
            motivo = _motivo_do_vermelho(registro, parametros)
            faixa_motivo = faixa_s if motivo in ("SECA_AGORA", "ENCHARCAMENTO") else faixa_t
            coluna = "umidade_solo" if faixa_motivo is faixa_s else "temperatura_ar"
            explicacao = {"motivo_principal": motivo, "prazo_horas": 0.0,
                          "horas_em_estresse": _horas_seguidas_fora(list(grade[coluna]), faixa_motivo)}
        elif gatilho is not None:
            explicacao = explicar_alerta(contexto)
        elif frio_leve:
            explicacao = {"motivo_principal": "FRIO", "prazo_horas": _horas_ate_hora_do_dia(momento, 5),
                          "horas_em_estresse": _horas_seguidas_fora(list(grade["temperatura_ar"]), faixa_t)}
        elif seca_condicional:
            explicacao = {"motivo_principal": "SECA_CHEGANDO", "prazo_horas": float(eta),  # type: ignore[arg-type]
                          "horas_em_estresse": _horas_seguidas_fora(list(grade["umidade_solo"]), faixa_s)}
        else:
            explicacao = {"motivo_principal": "NENHUM", "prazo_horas": None, "horas_em_estresse": None}

        resultado = ResultadoAntecipacao(
            status_antecipado=1 if antecipa else 0,
            probabilidade_risco_futuro=None if probabilidade is None else round(float(probabilidade), 4),
            horas_ate_secar=None if eta is None else round(float(eta), 2),
            temperatura_minima_prevista=None if tmin is None else round(float(tmin), 2),
            motivo_principal=str(explicacao["motivo_principal"]),
            prazo_horas=None if explicacao["prazo_horas"] is None else round(float(explicacao["prazo_horas"]), 2),
            horas_em_estresse=(None if explicacao["horas_em_estresse"] is None
                               else float(explicacao["horas_em_estresse"])),
            versao_prognostico=(str(pacote.get("versao") or VERSAO_SEM_PROGNOSTICO)
                                + ("-suspenso" if configuracao.prognostico_suspenso else "")) if pacote is not None
            else VERSAO_SEM_PROGNOSTICO,
        )

        if registro.status_regra >= STATUS_VERMELHO or registro.status_alerta >= STATUS_VERMELHO:
            return registro, resultado
        if antecipa:
            certeza = probabilidade_decisao if gatilho == "modelo" else None
            mensagem = _mensagem_antecipada(registro, parametros, explicacao, momento, certeza,
                                            configuracao.fisiologica)
            return dataclasses.replace(
                registro,
                status_predito=max(int(registro.status_predito), STATUS_AMARELO),
                status_alerta=max(int(registro.status_alerta), STATUS_AMARELO),
                mensagem_voz=mensagem,
            ), resultado
        extras: List[str] = []
        if frio_leve:
            extras.append("Esta noite vai fazer frio. Proteja as mudas novas.")
        if seca_condicional:
            extras.append(f"Se não chover nem regar, a terra deve ficar muito seca {falar_prazo(float(eta), momento)}.")  # type: ignore[arg-type]
        if extras:
            mensagem = " ".join([str(registro.mensagem_voz).strip()] + extras)
            return dataclasses.replace(registro, mensagem_voz=mensagem), resultado
        return registro, resultado
    except Exception as erro:  # noqa: BLE001 — a antecipação nunca pode derrubar o coletor
        registrador.warning("Prognóstico ignorado para a leitura de %s (%s); a leitura segue sem antecipação.",
                            getattr(registro, "data_hora", "?"), erro, exc_info=True)
        return registro, None


# ---------------------------------------------------------------------------
# Persistência própria
# ---------------------------------------------------------------------------
def _numero_ou_nulo(valor: Optional[float]) -> Optional[float]:
    """Converte para float finito ou ``None``."""
    if valor is None:
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    return numero if math.isfinite(numero) else None


def gravar_analise_antecipacao(
    id_leitura: int, resultado: ResultadoAntecipacao, caminho_banco: Path = CAMINHO_BANCO
) -> None:
    """Grava (``INSERT OR REPLACE``) a análise de antecipação de uma leitura.

    Cria a tabela ``analise_antecipacao`` se ela não existir (idempotente;
    não toca nas outras tabelas). A chave estrangeira com ``ON DELETE
    CASCADE`` faz a análise sumir junto com a leitura.

    Raises:
        ValueError: motivo fora de ``MOTIVOS`` ou ``status_antecipado`` fora de 0/1.
        sqlite3.Error: falha de gravação (ex.: ``id_leitura`` inexistente).
    """
    if resultado.motivo_principal not in MOTIVOS:
        raise ValueError(f"motivo_principal inválido: {resultado.motivo_principal!r}.")
    if int(resultado.status_antecipado) not in (0, 1):
        raise ValueError(f"status_antecipado deve ser 0 ou 1, recebido {resultado.status_antecipado!r}.")
    conexao = obter_conexao(Path(caminho_banco))
    try:
        conexao.execute(_DDL_ANALISE)
        conexao.execute(
            """
            INSERT OR REPLACE INTO analise_antecipacao (
                id_leitura, probabilidade_risco_futuro, horas_ate_secar, temperatura_minima_prevista,
                motivo_principal, prazo_horas, horas_em_estresse, status_antecipado, versao_prognostico
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (int(id_leitura), _numero_ou_nulo(resultado.probabilidade_risco_futuro),
             _numero_ou_nulo(resultado.horas_ate_secar), _numero_ou_nulo(resultado.temperatura_minima_prevista),
             resultado.motivo_principal, _numero_ou_nulo(resultado.prazo_horas),
             _numero_ou_nulo(resultado.horas_em_estresse), int(resultado.status_antecipado),
             str(resultado.versao_prognostico or VERSAO_SEM_PROGNOSTICO)),
        )
        conexao.commit()
    except Exception:
        conexao.rollback()
        raise
    finally:
        conexao.close()


def obter_analise_antecipacao(id_leitura: int, caminho_banco: Path = CAMINHO_BANCO) -> Optional[Dict[str, Any]]:
    """Lê a análise de antecipação de uma leitura.

    Returns:
        ``{"probabilidade_risco_futuro", "horas_ate_secar",
        "temperatura_minima_prevista", "motivo_principal", "prazo_horas",
        "horas_em_estresse", "status_antecipado", "versao_prognostico"}`` ou
        ``None`` se o banco, a tabela ou a linha não existirem.
    """
    caminho = Path(caminho_banco)
    if not caminho.is_file():
        return None
    conexao = obter_conexao(caminho)
    try:
        existe = conexao.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'analise_antecipacao'").fetchone()
        if existe is None:
            return None
        linha = conexao.execute(
            """
            SELECT probabilidade_risco_futuro, horas_ate_secar, temperatura_minima_prevista, motivo_principal,
                   prazo_horas, horas_em_estresse, status_antecipado, versao_prognostico
              FROM analise_antecipacao WHERE id_leitura = ?
            """,
            (int(id_leitura),),
        ).fetchone()
    finally:
        conexao.close()
    if linha is None:
        return None
    return {
        "probabilidade_risco_futuro": None if linha[0] is None else float(linha[0]),
        "horas_ate_secar": None if linha[1] is None else float(linha[1]),
        "temperatura_minima_prevista": None if linha[2] is None else float(linha[2]),
        "motivo_principal": str(linha[3]),
        "prazo_horas": None if linha[4] is None else float(linha[4]),
        "horas_em_estresse": None if linha[5] is None else float(linha[5]),
        "status_antecipado": int(linha[6]),
        "versao_prognostico": str(linha[7]),
    }
