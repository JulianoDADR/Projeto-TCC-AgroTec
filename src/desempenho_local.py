"""Portão de segurança LOCAL da IA de prognóstico (desempenho no próprio sítio).

A IA de prognóstico foi treinada no simulador e, em dado real (reanálise
ERA5-Land), o comitê mediu Brier pior que o da climatologia (0,211 contra
0,155). Como cada previsão de 12 h se resolve sozinha 12 h depois (o status
da regra das horas seguintes já fica gravado em ``historico_leituras``), o
próprio sítio pode julgar a IA, de hora em hora, sem internet:

1. ``previsoes_resolvidas``: junta a probabilidade gravada em
   ``analise_antecipacao`` (média de cada hora) com o MAIOR ``status_regra``
   das 12 horas seguintes, da mesma cultura e só de leituras de campo
   (``CAMPO_USB``), nos últimos 28 dias.
2. ``avaliar_e_decidir``: com ≥ 168 previsões resolvidas (uma semana),
   calcula Brier e log-loss de 28 dias e o Brier da climatologia mês × hora
   do complemento do prognóstico. Grava ``prognostico_suspenso = true`` se a
   IA não vencer a climatologia (Brier local ≥ Brier da climatologia) ou se o
   teste de Page-Hinkley sobre o log-loss detectar piora; senão ``false``.
   Com ≥ 200 resolvidas e ≥ 20 positivas, ajusta a recalibração de Platt
   (``platt_local = 'a,b'``, regressão logística sobre ``logit(p)``).
3. ``ajustar_coef_tmin_local``: ajusta os coeficientes da mínima noturna com
   as noites do próprio sítio (``coef_tmin_local``) e a margem conformal por
   estação (``quantis_tmin_local``), só quando eles prevêem melhor que os
   coeficientes em uso.

Com o prognóstico suspenso, ``prognostico.aplicar_antecipacao`` ignora o
gatilho do modelo; a geada e a secagem (física simples, sem modelo)
continuam. A suspensão se desfaz sozinha quando o desempenho volta.

Fundamentos: detecção de deriva leve na borda (Page-Hinkley),
https://arxiv.org/pdf/2211.01840; Platt/temperature scaling como
recalibração sob mudança de domínio, https://arxiv.org/pdf/2507.07780.

Execução (serviço ``deploy/agroanalise-desempenho.timer``, de hora em hora):
``python src/desempenho_local.py [--banco CAMINHO]``.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from database import (
    CAMINHO_BANCO,
    MODO_CAMPO_USB,
    STATUS_VERMELHO,
    definir_configuracoes,
    obter_conexao,
)
from gerador_dataset import FORMATO_DATA_HORA, ponto_orvalho_magnus
from prognostico import (
    CAMINHO_COMPLEMENTO_PROGNOSTICO,
    HORIZONTE_PROGNOSTICO_HORAS,
    calibrar_platt,
    carregar_complemento_prognostico,
    escolher_coef_tmin,
    ler_configuracao_prognostico,
)
from referencia_climatica import (
    ESTACOES,
    aplicar_climatologia,
    estacao_do_mes,
    minimos_quadrados_tmin,
    prever_tmin,
    quantil_conformal,
    residuos_deixa_um_fora,
    resolver_regiao,
    tabela_noites,
)

# ---------------------------------------------------------------------------
# Constantes públicas
# ---------------------------------------------------------------------------
DIAS_JANELA_AVALIACAO: int = 28
MINIMO_PREVISOES_RESOLVIDAS: int = 168  # uma semana de previsões horárias
MINIMO_PREVISOES_PLATT: int = 200
MINIMO_POSITIVAS_PLATT: int = 20
HORAS_MINIMAS_FUTURO: int = 6  # das 12 h seguintes, ao menos 6 com leitura para a previsão contar como resolvida
LIMITE_PROBABILIDADE: float = 1e-6
# Page-Hinkley (delta = tolerância por passo; limiar = soma acumulada, em nats de log-loss, que dispara).
PAGE_HINKLEY_DELTA: float = 0.005
PAGE_HINKLEY_LIMIAR: float = 50.0
# Ajuste local da mínima: noites com ≥ 18 h de dado na janela de 24 h (12:00 → 11:59), o mesmo critério
# de cobertura da antecipação (18 das últimas 24 h).
HORAS_MINIMAS_NOITE_LOCAL: int = 18
MINIMO_NOITES_ESTACAO: int = 10
QUANTIL_CONFORMAL_LOCAL: float = 0.9

registrador: logging.Logger = logging.getLogger("agroanalise.desempenho_local")


# ---------------------------------------------------------------------------
# Page-Hinkley
# ---------------------------------------------------------------------------
@dataclass
class PageHinkley:
    """Teste de Page-Hinkley para AUMENTO da média de uma sequência (ex.: log-loss por previsão).

    A cada valor ``x``: ``média ← média + (x − média)/n``;
    ``soma ← soma + (x − média − delta)``; ``mínimo ← min(mínimo, soma)``;
    dispara quando ``soma − mínimo > limiar``. Leve (O(1) por valor, sem
    guardar a sequência), próprio para rodar na borda.

    Nota quantitativa: um degrau de altura ``h`` faz a soma crescer no máximo
    ``h − delta`` por passo, então o disparo leva pelo menos
    ``limiar / (h − delta)`` passos (com os padrões e ``h = 0,5``: ≈ 101).

    Attributes:
        delta: tolerância de variação por passo (padrão 0,005).
        limiar: soma acumulada que dispara (padrão 50,0).
    """

    delta: float = PAGE_HINKLEY_DELTA
    limiar: float = PAGE_HINKLEY_LIMIAR
    n: int = field(default=0, init=False)
    media: float = field(default=0.0, init=False)
    soma: float = field(default=0.0, init=False)
    minimo: float = field(default=0.0, init=False)

    def atualizar(self, x: float) -> bool:
        """Acrescenta um valor e devolve ``True`` se a piora foi detectada (valores não finitos são ignorados)."""
        valor = float(x)
        if not math.isfinite(valor):
            return self.soma - self.minimo > self.limiar
        self.n += 1
        self.media += (valor - self.media) / self.n
        self.soma += valor - self.media - self.delta
        self.minimo = min(self.minimo, self.soma)
        return self.soma - self.minimo > self.limiar

    def reiniciar(self) -> None:
        """Volta ao estado inicial (depois de uma troca de modelo, por exemplo)."""
        self.n = 0
        self.media = 0.0
        self.soma = 0.0
        self.minimo = 0.0


# ---------------------------------------------------------------------------
# Previsões resolvidas
# ---------------------------------------------------------------------------
_CONSULTA_PREVISOES: str = """
    SELECT c.codigo AS codigo_cultura,
           strftime('%Y-%m-%d %H:00:00', h.data_hora) AS hora,
           AVG(a.probabilidade_risco_futuro) AS probabilidade,
           MAX(h.status_regra) AS status_agora,
           COUNT(*) AS n_previsoes
      FROM analise_antecipacao a
      JOIN historico_leituras h ON h.id_leitura = a.id_leitura
      JOIN culturas c ON c.id_cultura = h.id_cultura
     WHERE h.origem = ? AND h.flag_anomalia = 0 AND a.probabilidade_risco_futuro IS NOT NULL
       AND h.data_hora >= ? AND h.data_hora <= ?
     GROUP BY c.codigo, hora
"""
_CONSULTA_STATUS: str = """
    SELECT c.codigo AS codigo_cultura,
           strftime('%Y-%m-%d %H:00:00', h.data_hora) AS hora,
           MAX(h.status_regra) AS status_hora
      FROM historico_leituras h
      JOIN culturas c ON c.id_cultura = h.id_cultura
     WHERE h.origem = ? AND h.flag_anomalia = 0 AND h.data_hora >= ? AND h.data_hora <= ?
     GROUP BY c.codigo, hora
"""
COLUNAS_RESOLVIDAS: Tuple[str, ...] = ("codigo_cultura", "hora", "probabilidade", "alvo", "horas_futuro_com_dado")


def _resolvidas_vazias() -> pd.DataFrame:
    """DataFrame vazio com as colunas de ``previsoes_resolvidas``."""
    return pd.DataFrame({
        "codigo_cultura": pd.Series([], dtype=object), "hora": pd.Series([], dtype="datetime64[ns]"),
        "probabilidade": pd.Series([], dtype=float), "alvo": pd.Series([], dtype=np.int64),
        "horas_futuro_com_dado": pd.Series([], dtype=np.int64),
    })


def previsoes_resolvidas(caminho_banco: Path, agora: datetime, dias: int = DIAS_JANELA_AVALIACAO) -> pd.DataFrame:
    """Previsões de 12 h dos últimos ``dias`` dias cujo desfecho já é conhecido.

    Granularidade horária: a probabilidade de cada hora é a média das
    previsões gravadas em ``analise_antecipacao`` naquela hora (só horas
    não vermelhas, como no treino), e o alvo é 1 se o MAIOR ``status_regra``
    das 12 horas cheias seguintes (mesma cultura, origem ``CAMPO_USB``, sem
    anomalia) for Vermelho. Só conta a previsão com pelo menos
    ``HORAS_MINIMAS_FUTURO`` dessas 12 horas com leitura (aparelho desligado
    não vira "acertou que não ia ficar Vermelho").

    Returns:
        DataFrame com ``COLUNAS_RESOLVIDAS``, em ordem de ``hora``; vazio se o
        banco ou a tabela de análise não existirem.
    """
    caminho = Path(caminho_banco)
    if not caminho.is_file():
        return _resolvidas_vazias()
    inicio = agora - timedelta(days=int(dias))
    fim_previsoes = agora - timedelta(hours=HORIZONTE_PROGNOSTICO_HORAS)
    conexao = obter_conexao(caminho)
    try:
        existe = conexao.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'analise_antecipacao'").fetchone()
        if existe is None:
            return _resolvidas_vazias()
        previsoes = pd.DataFrame([tuple(linha) for linha in conexao.execute(
            _CONSULTA_PREVISOES, (MODO_CAMPO_USB, inicio.strftime(FORMATO_DATA_HORA),
                                  fim_previsoes.strftime(FORMATO_DATA_HORA))).fetchall()],
            columns=["codigo_cultura", "hora", "probabilidade", "status_agora", "n_previsoes"])
        status = pd.DataFrame([tuple(linha) for linha in conexao.execute(
            _CONSULTA_STATUS, (MODO_CAMPO_USB, inicio.strftime(FORMATO_DATA_HORA),
                               agora.strftime(FORMATO_DATA_HORA))).fetchall()],
            columns=["codigo_cultura", "hora", "status_hora"])
    finally:
        conexao.close()
    if previsoes.empty or status.empty:
        return _resolvidas_vazias()
    previsoes["hora"] = pd.to_datetime(previsoes["hora"])
    status["hora"] = pd.to_datetime(status["hora"])
    partes: List[pd.DataFrame] = []
    for codigo, grupo in previsoes.groupby("codigo_cultura", sort=True):
        serie_status = status.loc[status["codigo_cultura"] == codigo].set_index("hora")["status_hora"].astype(float)
        if serie_status.empty:
            continue
        grade = pd.date_range(min(serie_status.index.min(), grupo["hora"].min()),
                              max(serie_status.index.max(), grupo["hora"].max())
                              + pd.Timedelta(hours=HORIZONTE_PROGNOSTICO_HORAS), freq="h")
        completa = serie_status.reindex(grade)
        futuros = pd.concat([completa.shift(-passo) for passo in range(1, HORIZONTE_PROGNOSTICO_HORAS + 1)], axis=1)
        maximo = futuros.max(axis=1, skipna=True)
        contagem = futuros.notna().sum(axis=1)
        bloco = grupo.loc[grupo["status_agora"].astype(float) < STATUS_VERMELHO].copy()
        bloco["maximo_futuro"] = maximo.reindex(bloco["hora"]).to_numpy()
        bloco["horas_futuro_com_dado"] = contagem.reindex(bloco["hora"]).fillna(0).astype(np.int64).to_numpy()
        bloco = bloco.loc[bloco["horas_futuro_com_dado"] >= HORAS_MINIMAS_FUTURO]
        bloco["alvo"] = (bloco["maximo_futuro"].astype(float) >= STATUS_VERMELHO).astype(np.int64)
        partes.append(bloco)
    if not partes:
        return _resolvidas_vazias()
    resultado = pd.concat(partes, ignore_index=True)
    resultado["probabilidade"] = resultado["probabilidade"].astype(float).clip(0.0, 1.0)
    resultado = resultado.sort_values(["hora", "codigo_cultura"], kind="mergesort").reset_index(drop=True)
    return resultado[list(COLUNAS_RESOLVIDAS)]


# ---------------------------------------------------------------------------
# Avaliação e decisão
# ---------------------------------------------------------------------------
def _log_loss_por_previsao(alvo: np.ndarray, probabilidade: np.ndarray) -> np.ndarray:
    """Log-loss (nats) de cada previsão, com a probabilidade limitada a [1e-6, 1 − 1e-6]."""
    p = np.clip(np.asarray(probabilidade, dtype=float), LIMITE_PROBABILIDADE, 1.0 - LIMITE_PROBABILIDADE)
    y = np.asarray(alvo, dtype=float)
    return -(y * np.log(p) + (1.0 - y) * np.log(1.0 - p))


def _climatologia_das_previsoes(resolvidas: pd.DataFrame, complemento: Optional[Dict[str, Any]]) -> Tuple[np.ndarray, str]:
    """Probabilidade climatológica de cada previsão resolvida e a origem dela.

    Usa ``complemento['climatologia_por_cultura'][cultura]['MM-HH']``. Sem
    complemento (ou sem a cultura), usa a taxa de Vermelho da própria janela
    com suavização de Laplace — uma régua constante, conservadora.
    """
    por_cultura = (complemento or {}).get("climatologia_por_cultura")
    taxa_janela = (float(resolvidas["alvo"].sum()) + 1.0) / (float(len(resolvidas)) + 2.0)
    valores = np.full(len(resolvidas), taxa_janela, dtype=float)
    origem = "taxa_da_janela"
    if isinstance(por_cultura, dict):
        for codigo in resolvidas["codigo_cultura"].unique():
            tabela = por_cultura.get(str(codigo))
            if not isinstance(tabela, dict) or not tabela:
                continue
            filtro = (resolvidas["codigo_cultura"] == codigo).to_numpy()
            valores[filtro] = aplicar_climatologia(resolvidas.loc[filtro, "hora"], tabela, padrao=taxa_janela)
            origem = "complemento"
    return np.clip(valores, 0.0, 1.0), origem


def _ajustar_platt(alvo: np.ndarray, probabilidade: np.ndarray) -> Optional[Tuple[float, float]]:
    """Platt sobre ``logit(p)`` (regressão logística de uma variável); ``None`` se não houver variação."""
    from sklearn.linear_model import LogisticRegression  # import tardio: só quando há dados para ajustar

    p = np.clip(np.asarray(probabilidade, dtype=float), LIMITE_PROBABILIDADE, 1.0 - LIMITE_PROBABILIDADE)
    logit = np.log(p / (1.0 - p)).reshape(-1, 1)
    y = np.asarray(alvo, dtype=np.int64)
    if np.std(logit) < 1e-9 or len(np.unique(y)) < 2:
        return None
    modelo = LogisticRegression(C=1e4, solver="lbfgs", max_iter=1000)
    modelo.fit(logit, y)
    a, b = float(modelo.coef_[0, 0]), float(modelo.intercept_[0])
    return (a, b) if math.isfinite(a) and math.isfinite(b) else None


def avaliar_e_decidir(
    caminho_banco: Path = CAMINHO_BANCO,
    agora: Optional[datetime] = None,
    *,
    caminho_complemento: Path = CAMINHO_COMPLEMENTO_PROGNOSTICO,
) -> Dict[str, Any]:
    """Avalia a IA de prognóstico no próprio sítio e liga/desliga o portão de segurança.

    * Menos de ``MINIMO_PREVISOES_RESOLVIDAS`` previsões resolvidas: nada é
      gravado (``avaliado = False``) e a decisão anterior continua.
    * Senão: Brier e log-loss de 28 dias com a probabilidade EFETIVA (a do
      modelo passada pela ``platt_local`` em vigor), Brier da climatologia do
      complemento nas mesmas horas e Page-Hinkley sobre o log-loss em ordem
      de tempo. Grava ``prognostico_suspenso`` (true se Brier local ≥ Brier
      da climatologia ou se o Page-Hinkley disparar),
      ``prognostico_brier_local_28d`` e ``prognostico_avaliado_em``.
    * Com ≥ ``MINIMO_PREVISOES_PLATT`` resolvidas e ≥ ``MINIMO_POSITIVAS_PLATT``
      positivas, ajusta Platt sobre a probabilidade CRUA do modelo e grava
      ``platt_local = 'a,b'`` (vale a partir da próxima leitura).

    Returns:
        ``{"avaliado", "motivo", "n_resolvidas", "n_positivas", "brier_local",
        "brier_modelo_cru", "log_loss_local", "brier_climatologia",
        "origem_climatologia", "page_hinkley_disparou", "suspenso", "platt_local"}``.
    """
    momento = agora or datetime.now()
    resultado: Dict[str, Any] = {
        "avaliado": False, "motivo": "", "n_resolvidas": 0, "n_positivas": 0, "brier_local": None,
        "brier_modelo_cru": None, "log_loss_local": None, "brier_climatologia": None, "origem_climatologia": "",
        "page_hinkley_disparou": False, "suspenso": None, "platt_local": None,
    }
    caminho = Path(caminho_banco)
    if not caminho.is_file():
        resultado["motivo"] = f"banco inexistente: {caminho}"
        return resultado
    configuracao = ler_configuracao_prognostico(caminho)
    resolvidas = previsoes_resolvidas(caminho, momento)
    resultado["n_resolvidas"] = int(len(resolvidas))
    resultado["n_positivas"] = int(resolvidas["alvo"].sum()) if not resolvidas.empty else 0
    if len(resolvidas) < MINIMO_PREVISOES_RESOLVIDAS:
        resultado["motivo"] = (f"só {len(resolvidas)} previsões resolvidas em {DIAS_JANELA_AVALIACAO} dias "
                               f"(mínimo {MINIMO_PREVISOES_RESOLVIDAS}); decisão anterior mantida")
        registrador.info("Portão local: %s.", resultado["motivo"])
        return resultado

    alvo = resolvidas["alvo"].to_numpy(dtype=np.int64)
    cru = resolvidas["probabilidade"].to_numpy(dtype=float)
    efetiva = np.array([calibrar_platt(valor, configuracao.platt_local) for valor in cru], dtype=float)
    perdas = _log_loss_por_previsao(alvo, efetiva)
    climatologia, origem = _climatologia_das_previsoes(resolvidas, carregar_complemento_prognostico(caminho_complemento))
    detector = PageHinkley()
    disparou = False
    for perda in perdas:
        disparou = detector.atualizar(float(perda)) or disparou
    brier_local = float(np.mean((efetiva - alvo) ** 2))
    brier_climatologia = float(np.mean((climatologia - alvo) ** 2))
    suspenso = bool(brier_local >= brier_climatologia or disparou)
    resultado.update({
        "avaliado": True, "brier_local": brier_local, "brier_modelo_cru": float(np.mean((cru - alvo) ** 2)),
        "log_loss_local": float(np.mean(perdas)), "brier_climatologia": brier_climatologia,
        "origem_climatologia": origem, "page_hinkley_disparou": bool(disparou), "suspenso": suspenso,
        "motivo": ("Brier local ≥ Brier da climatologia" if brier_local >= brier_climatologia
                   else "Page-Hinkley detectou piora do log-loss" if disparou else "IA vence a climatologia"),
    })
    valores: Dict[str, Any] = {
        "prognostico_suspenso": suspenso,
        "prognostico_brier_local_28d": round(brier_local, 4),
        "prognostico_avaliado_em": momento.isoformat(timespec="seconds"),
    }
    if len(resolvidas) >= MINIMO_PREVISOES_PLATT and resultado["n_positivas"] >= MINIMO_POSITIVAS_PLATT:
        platt = _ajustar_platt(alvo, cru)
        if platt is not None:
            valores["platt_local"] = f"{platt[0]:.6f},{platt[1]:.6f}"
            resultado["platt_local"] = valores["platt_local"]
    definir_configuracoes(valores, caminho)
    if suspenso != configuracao.prognostico_suspenso:
        registrador.warning("Portão local: antecipação pelo modelo %s (%s; Brier local %.4f × climatologia %.4f).",
                            "SUSPENSA" if suspenso else "REATIVADA", resultado["motivo"], brier_local,
                            brier_climatologia)
    else:
        registrador.info("Portão local: suspenso=%s (%s; Brier local %.4f × climatologia %.4f, n=%d).", suspenso,
                         resultado["motivo"], brier_local, brier_climatologia, len(resolvidas))
    return resultado


# ---------------------------------------------------------------------------
# Mínima noturna do próprio sítio
# ---------------------------------------------------------------------------
def _serie_horaria_ar(caminho: Path) -> pd.DataFrame:
    """Médias horárias de temperatura e umidade do ar de campo (brutas + agregadas pela retenção)."""
    conexao = obter_conexao(caminho)
    try:
        linhas = conexao.execute(
            """
            SELECT strftime('%Y-%m-%d %H:00:00', data_hora) AS hora,
                   AVG(temperatura_ar) AS temperatura_ar, AVG(umidade_ar) AS umidade_ar
              FROM historico_leituras
             WHERE origem = ? AND flag_anomalia = 0
             GROUP BY hora
            """, (MODO_CAMPO_USB,)).fetchall()
        agregadas: List[Any] = []
        existe = conexao.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'historico_agregado_hora'").fetchone()
        if existe is not None:
            agregadas = conexao.execute(
                """
                SELECT hora, AVG(temperatura_med) AS temperatura_ar, AVG(umidade_ar_med) AS umidade_ar
                  FROM historico_agregado_hora
                 WHERE origem = ?
                 GROUP BY hora
                """, (MODO_CAMPO_USB,)).fetchall()
    finally:
        conexao.close()
    colunas = ["data_hora", "temperatura_ar", "umidade_ar"]
    brutas = pd.DataFrame([tuple(linha) for linha in linhas], columns=colunas)
    antigas = pd.DataFrame([tuple(linha) for linha in agregadas], columns=colunas)
    juntas = pd.concat([antigas, brutas], ignore_index=True) if not antigas.empty else brutas
    if juntas.empty:
        return pd.DataFrame(columns=colunas)
    juntas["data_hora"] = pd.to_datetime(juntas["data_hora"], errors="coerce")
    juntas = juntas.dropna().drop_duplicates("data_hora", keep="last").sort_values("data_hora")
    return juntas.reset_index(drop=True)


def _noites_locais(serie: pd.DataFrame) -> pd.DataFrame:
    """Noites do sítio (``tabela_noites``) com ≥ ``HORAS_MINIMAS_NOITE_LOCAL`` h de dado de 12:00 a 11:59."""
    if serie.empty:
        return tabela_noites(serie)
    horaria = serie.assign(ponto_orvalho=np.asarray(
        ponto_orvalho_magnus(serie["temperatura_ar"].to_numpy(dtype=float), serie["umidade_ar"].to_numpy(dtype=float)),
        dtype=float))
    noites = tabela_noites(horaria)
    janela = (pd.to_datetime(horaria["data_hora"]) - pd.Timedelta(hours=12)).dt.normalize()
    cobertura = janela.value_counts()
    noites["horas_com_dado"] = cobertura.reindex(pd.to_datetime(noites["data"])).fillna(0).astype(int).to_numpy()
    return noites.loc[noites["horas_com_dado"] >= HORAS_MINIMAS_NOITE_LOCAL].reset_index(drop=True)


def _coef_em_uso_sem_local(caminho: Path) -> Tuple[float, float, float]:
    """Coeficientes que valeriam sem o ajuste local (região do complemento da cultura ativa > padrão)."""
    configuracao = ler_configuracao_prognostico(caminho)
    sem_local = dataclasses.replace(configuracao, coef_tmin_local=None)
    cultura = ""
    try:
        conexao = obter_conexao(caminho)
        try:
            linha = conexao.execute("SELECT valor FROM configuracao_sistema WHERE chave = 'cultura_ativa'").fetchone()
        finally:
            conexao.close()
        cultura = "" if linha is None else str(linha[0] or "")
    except Exception as erro:  # noqa: BLE001 — sem cultura ativa, compara com o padrão
        registrador.warning("Cultura ativa ilegível (%s); comparando com os coeficientes padrão.", erro)
    regiao = resolver_regiao(cultura, configuracao.regiao_referencia)
    coef, _ = escolher_coef_tmin(sem_local, carregar_complemento_prognostico(CAMINHO_COMPLEMENTO_PROGNOSTICO),
                                 regiao, None)
    return coef


def ajustar_coef_tmin_local(
    caminho_banco: Path = CAMINHO_BANCO, minimo_noites: int = 14
) -> Optional[Tuple[float, float, float]]:
    """Ajusta a mínima noturna com as noites do próprio sítio e grava ``coef_tmin_local``/``quantis_tmin_local``.

    Usa as médias horárias de campo (``CAMPO_USB``; inclui as horas já
    agregadas pela retenção), noites com ≥ 18 h de dado na janela de 24 h e
    pelo menos 2 h de tarde (17:00–19:59) e 8 h de noite (20:00–06:00). Os
    coeficientes ``(a, b, c)`` de ``Tmin = a + b·T_tarde + c·Td_tarde`` saem
    de mínimos quadrados; a margem de cada estação é o quantil conformal q90
    dos resíduos DEIXA-UMA-NOITE-FORA (estação com < 10 noites fica sem
    margem local e usa a do complemento).

    Proteção: o ajuste só é gravado se o seu erro deixa-uma-fora for menor
    que o dos coeficientes que valeriam sem ele (região do complemento ou
    padrão), nas mesmas noites.

    Returns:
        Os coeficientes gravados, ou ``None`` (poucas noites, banco ausente ou
        ajuste que não melhora).
    """
    caminho = Path(caminho_banco)
    if not caminho.is_file():
        return None
    noites = _noites_locais(_serie_horaria_ar(caminho)).dropna(subset=["t_tarde", "td_tarde", "tmin_noite"])
    if len(noites) < max(4, int(minimo_noites)):
        registrador.info("Ajuste local da mínima: %d noites completas (mínimo %d).", len(noites), minimo_noites)
        return None
    coef = minimos_quadrados_tmin(noites)
    residuos = residuos_deixa_um_fora(noites)
    erro_local = float(np.mean(np.abs(residuos)))
    alternativo = _coef_em_uso_sem_local(caminho)
    erro_alternativo = float(np.mean(np.abs(noites["tmin_noite"].to_numpy() - prever_tmin(noites, alternativo))))
    if not erro_local < erro_alternativo:
        registrador.info("Ajuste local da mínima descartado: erro %.2f °C não vence %.2f °C dos coeficientes em uso.",
                         erro_local, erro_alternativo)
        return None
    meses = pd.to_datetime(noites["data"]).dt.month.to_numpy()
    quantis: Dict[str, float] = {}
    for estacao in ESTACOES:
        filtro = np.array([estacao_do_mes(int(mes)) == estacao for mes in meses])
        if filtro.sum() >= MINIMO_NOITES_ESTACAO:
            quantil = quantil_conformal(np.abs(residuos.to_numpy()[filtro]), QUANTIL_CONFORMAL_LOCAL)
            if math.isfinite(quantil):
                quantis[estacao] = round(float(quantil), 3)
    definir_configuracoes({
        "coef_tmin_local": f"{coef[0]:.6f},{coef[1]:.6f},{coef[2]:.6f}",
        "quantis_tmin_local": json.dumps(quantis, sort_keys=True),
    }, caminho)
    registrador.info("Mínima local ajustada em %d noites: a=%.3f b=%.3f c=%.3f (erro %.2f °C × %.2f °C; margens %s).",
                     len(noites), coef[0], coef[1], coef[2], erro_local, erro_alternativo, quantis or "do complemento")
    return coef


# ---------------------------------------------------------------------------
# Linha de comando
# ---------------------------------------------------------------------------
def _criar_parser() -> argparse.ArgumentParser:
    """Argumentos da linha de comando."""
    parser = argparse.ArgumentParser(description="Portão de segurança local da IA de prognóstico do AgroAnálise.")
    parser.add_argument("--banco", type=Path, default=CAMINHO_BANCO, help="Caminho do banco SQLite (padrão: data/agroanalise.db).")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Avalia o prognóstico no sítio e ajusta a mínima local; devolve 0 (sucesso) ou 1 (erro inesperado).

    Pouco dado ainda NÃO é erro: o resumo diz quantas previsões e noites
    faltam e a decisão anterior continua valendo.
    """
    argumentos = _criar_parser().parse_args(argv)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        decisao = avaliar_e_decidir(argumentos.banco)
        coef = ajustar_coef_tmin_local(argumentos.banco)
    except Exception as erro:  # noqa: BLE001 — ponto de entrada do serviço: registra e devolve código de erro
        registrador.exception("Falha na avaliação local do prognóstico: %s", erro)
        return 1
    resumo = {chave: valor for chave, valor in decisao.items()}
    resumo["coef_tmin_local"] = None if coef is None else [round(valor, 4) for valor in coef]
    print(json.dumps(resumo, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
