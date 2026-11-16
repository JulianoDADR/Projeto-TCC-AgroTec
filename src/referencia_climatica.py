"""Referência climática REAL e offline do AgroAnálise (reanálise ERA5-Land).

Até a v2, toda métrica da IA de prognóstico vinha do simulador climático: a
IA aprendia a física que o próprio simulador impõe. Este módulo traz uma
régua de clima de verdade, guardada no repositório (nada é baixado no campo):

* ``referencia_climatica/era5land_varginha_mg_2018_2021.csv.gz`` — ponto de
  grade de Varginha/MG (Sul de Minas, Café Arábica);
* ``referencia_climatica/era5land_sao_gabriel_da_palha_es_2018_2021.csv.gz``
  — ponto de grade de São Gabriel da Palha/ES (Café Conilon).

Fonte: Copernicus ERA5-Land (0,1°, horário), redistribuído pela Open-Meteo no
AWS Open Data (https://github.com/open-meteo/open-data), licença CC BY 4.0
("Weather data by Open-Meteo.com"; Copernicus ERA5-Land). Detalhes, pontos de
grade e ressalvas em ``referencia_climatica/LICENCA_E_FONTE.txt``; o script
``scripts/baixar_referencia_era5land.py`` refaz os arquivos (fora do campo).

Ressalva importante: a reanálise de ~11 km suaviza extremos locais. A geada
radiativa do fundo de vale fica mais quente na grade do que no talhão, então
a referência serve para medir viés e erro dos MÉTODOS (climatologia,
persistência, coeficientes da mínima), não para dizer quantas geadas houve
num sítio.

Funções públicas:

* ``carregar_referencia``: série horária em hora local (America/Sao_Paulo,
  sem fuso), com temperatura, ponto de orvalho, umidade relativa (Magnus) e
  umidade do solo do modelo (m³/m³);
* ``regiao_da_cultura`` / ``resolver_regiao``: região de referência de uma
  cultura (chave ``regiao_referencia`` do banco ou a região padrão da cultura);
* ``tabela_noites``: uma linha por noite com o fim da tarde (17:00–19:59), a
  mínima da noite (20:00–06:00) e a mínima da noite anterior (persistência);
* ``climatologia_mes_hora``: probabilidade climatológica por mês × hora
  (288 células, suavização de Laplace);
* ``minimos_quadrados_tmin``, ``residuos_deixa_um_fora`` e
  ``quantil_conformal``: o núcleo estatístico do aviso de geada, usado tanto
  no treino (``treinamento_ia``) quanto no ajuste local
  (``desempenho_local``).

Este módulo só importa ``database`` e ``gerador_dataset`` (leve, sem
scikit-learn nem matplotlib), para poder ser carregado pelo prognóstico em
tempo real.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

from database import BASE_DIR, obter_conexao
from gerador_dataset import umidade_relativa_magnus

# ---------------------------------------------------------------------------
# Constantes públicas
# ---------------------------------------------------------------------------
DIRETORIO_REFERENCIA: Path = BASE_DIR / "referencia_climatica"
FUSO_LOCAL: str = "America/Sao_Paulo"
CHAVE_REGIAO_REFERENCIA: str = "regiao_referencia"

# Janelas da noite (mesmas do aviso de geada de ``prognostico.prever_temperatura_minima_noite``).
HORA_INICIO_TARDE: int = 17
HORA_FIM_TARDE: int = 19  # tarde = 17:00–19:59
HORA_INICIO_NOITE: int = 20
HORA_FIM_NOITE: int = 6  # noite = 20:00 do dia até 06:00 do dia seguinte (inclusive)
HORAS_MINIMAS_TARDE: int = 2
HORAS_MINIMAS_NOITE: int = 8

# Estações para a margem conformal da mínima: maio–setembro = estação seca (inverno, noites de céu limpo e
# geada radiativa); outubro–abril = águas. Divisão usual do calendário do café no Sudeste (Embrapa Cerrados,
# https://sbicafe.ufv.br/items/46703cd6-1804-4062-841a-3af5714ec5ac — período seco de junho a setembro).
MESES_ESTACAO_SECA: Tuple[int, ...] = (5, 6, 7, 8, 9)
ESTACOES: Tuple[str, str] = ("seca", "aguas")

registrador: logging.Logger = logging.getLogger("agroanalise.referencia_climatica")


@dataclass(frozen=True)
class RegiaoReferencia:
    """Um ponto de grade da referência climática.

    Attributes:
        codigo: chave da região (ex.: ``'varginha_mg'``).
        arquivo: nome do ``.csv.gz`` dentro de ``DIRETORIO_REFERENCIA``.
        latitude: latitude do ponto de grade ERA5-Land (graus).
        longitude: longitude do ponto de grade ERA5-Land (graus).
        cultura_padrao: código da cultura que usa esta região quando o banco
            não diz outra (``configuracao_sistema.regiao_referencia``).
    """

    codigo: str
    arquivo: str
    latitude: float
    longitude: float
    cultura_padrao: str


# Pontos de grade 0,1° do ERA5-Land mais próximos das cidades (Varginha −21,55/−45,43;
# São Gabriel da Palha −19,02/−40,54), conforme a extração em scripts/baixar_referencia_era5land.py.
REGIOES_REFERENCIA: Dict[str, RegiaoReferencia] = {
    "varginha_mg": RegiaoReferencia("varginha_mg", "era5land_varginha_mg_2018_2021.csv.gz",
                                    -21.6, -45.4, "cafe_arabica"),
    "sao_gabriel_da_palha_es": RegiaoReferencia("sao_gabriel_da_palha_es",
                                                "era5land_sao_gabriel_da_palha_es_2018_2021.csv.gz",
                                                -19.0, -40.5, "cafe_conilon"),
}

COLUNAS_REFERENCIA: Tuple[str, ...] = (
    "data_hora", "temperatura_ar", "ponto_orvalho", "umidade_ar", "umidade_solo_modelo_m3m3",
)
COLUNAS_NOITES: Tuple[str, ...] = ("data", "t_tarde", "td_tarde", "tmin_noite", "tmin_noite_anterior")


# ---------------------------------------------------------------------------
# Leitura da referência
# ---------------------------------------------------------------------------
@lru_cache(maxsize=8)
def _ler_csv_referencia(caminho: str, modificado: float) -> pd.DataFrame:
    """Lê e converte um CSV ERA5-Land (cache por caminho e data de modificação)."""
    bruto = pd.read_csv(caminho, parse_dates=["utc"])
    instantes = pd.to_datetime(bruto["utc"], utc=True).dt.tz_convert(FUSO_LOCAL).dt.tz_localize(None)
    temperatura = pd.to_numeric(bruto["temperature_2m"], errors="coerce").astype(float)
    orvalho = pd.to_numeric(bruto["dew_point_2m"], errors="coerce").astype(float)
    umidade = np.clip(np.asarray(umidade_relativa_magnus(temperatura.to_numpy(), orvalho.to_numpy()), dtype=float),
                      0.0, 100.0)
    return pd.DataFrame({
        "data_hora": instantes.astype("datetime64[ns]").to_numpy(),
        "temperatura_ar": temperatura.to_numpy(),
        "ponto_orvalho": orvalho.to_numpy(),
        "umidade_ar": umidade,
        "umidade_solo_modelo_m3m3": pd.to_numeric(bruto["soil_moisture_0_to_7cm"], errors="coerce").astype(float).to_numpy(),
    })


def carregar_referencia(regiao: str, diretorio: Path = DIRETORIO_REFERENCIA) -> pd.DataFrame:
    """Série horária da referência ERA5-Land de uma região, em hora local.

    Args:
        regiao: chave de ``REGIOES_REFERENCIA`` (ex.: ``'varginha_mg'``).
        diretorio: pasta dos ``.csv.gz`` (padrão ``DIRETORIO_REFERENCIA``).

    Returns:
        DataFrame (uma linha por hora UTC do arquivo, na ordem do arquivo) com
        ``data_hora`` (hora local America/Sao_Paulo, sem fuso — nas viradas
        de horário de verão de 2018/2019 há uma hora repetida ou pulada),
        ``temperatura_ar`` (°C, 2 m), ``ponto_orvalho`` (°C),
        ``umidade_ar`` (% pela fórmula de Magnus, limitada a 0–100) e
        ``umidade_solo_modelo_m3m3`` (0–7 cm, m³/m³, do modelo de superfície;
        NÃO é a mesma grandeza do higrômetro resistivo em %).

    Raises:
        KeyError: região desconhecida.
        FileNotFoundError: arquivo da referência ausente.
    """
    if regiao not in REGIOES_REFERENCIA:
        raise KeyError(f"Região de referência desconhecida: {regiao!r}. Válidas: {', '.join(REGIOES_REFERENCIA)}.")
    caminho = Path(diretorio) / REGIOES_REFERENCIA[regiao].arquivo
    if not caminho.is_file():
        raise FileNotFoundError(f"Referência climática ausente: {caminho}.")
    return _ler_csv_referencia(str(caminho.resolve()), caminho.stat().st_mtime).copy()


def referencia_disponivel(regiao: Optional[str] = None, diretorio: Path = DIRETORIO_REFERENCIA) -> bool:
    """``True`` se o arquivo da ``regiao`` (ou, sem ``regiao``, os de TODAS as regiões) existir em ``diretorio``."""
    regioes = [regiao] if regiao else list(REGIOES_REFERENCIA)
    return all(codigo in REGIOES_REFERENCIA and (Path(diretorio) / REGIOES_REFERENCIA[codigo].arquivo).is_file()
               for codigo in regioes)


def resolver_regiao(codigo_cultura: str, regiao_configurada: Optional[str] = None) -> Optional[str]:
    """Região de referência de uma cultura sem acessar o banco.

    Args:
        codigo_cultura: código da cultura (ex.: ``'cafe_arabica'``).
        regiao_configurada: valor de ``configuracao_sistema.regiao_referencia``
            (vazio ou desconhecido = usar a região padrão da cultura).

    Returns:
        A chave da região, ou ``None`` se nenhuma região tiver a cultura como padrão.
    """
    configurada = str(regiao_configurada or "").strip()
    if configurada in REGIOES_REFERENCIA:
        return configurada
    for codigo, regiao in REGIOES_REFERENCIA.items():
        if regiao.cultura_padrao == str(codigo_cultura).strip():
            return codigo
    return None


def regiao_da_cultura(codigo_cultura: str, caminho_banco: Path) -> Optional[str]:
    """Região de referência de uma cultura: chave ``regiao_referencia`` do banco; senão a ``cultura_padrao``.

    Nunca lança exceção e nunca cria o banco: se o arquivo não existir ou a
    leitura falhar, usa só a região padrão da cultura.
    """
    configurada = ""
    caminho = Path(caminho_banco)
    if caminho.is_file():
        try:
            conexao = obter_conexao(caminho)
            try:
                linha = conexao.execute("SELECT valor FROM configuracao_sistema WHERE chave = ?",
                                        (CHAVE_REGIAO_REFERENCIA,)).fetchone()
            finally:
                conexao.close()
            configurada = "" if linha is None else str(linha[0] or "")
        except Exception as erro:  # noqa: BLE001 — a região é opcional: cai na região padrão
            registrador.warning("Não foi possível ler %s em %s (%s); usando a região padrão da cultura.",
                                CHAVE_REGIAO_REFERENCIA, caminho, erro)
    return resolver_regiao(codigo_cultura, configurada)


# ---------------------------------------------------------------------------
# Noites, climatologia e estatística da mínima
# ---------------------------------------------------------------------------
def estacao_do_mes(mes: int) -> str:
    """``'seca'`` (maio–setembro) ou ``'aguas'`` (outubro–abril)."""
    return "seca" if int(mes) in MESES_ESTACAO_SECA else "aguas"


def tabela_noites(df: pd.DataFrame) -> pd.DataFrame:
    """Uma linha por noite: fim da tarde, mínima da noite e mínima da noite anterior.

    Args:
        df: série horária com ``data_hora``, ``temperatura_ar`` e ``ponto_orvalho``.

    Returns:
        DataFrame com ``COLUNAS_NOITES``: ``data`` (dia da tarde, Timestamp à
        meia-noite), ``t_tarde`` e ``td_tarde`` (médias de 17:00 a 19:59),
        ``tmin_noite`` (mínima de 20:00 do dia até 06:00 do dia seguinte) e
        ``tmin_noite_anterior`` (a mesma mínima da noite anterior; NaN quando
        ela não existe — é a previsão de persistência). Só entram noites com
        pelo menos 2 horas de tarde e 8 horas de noite.
    """
    if df is None or df.empty:
        return pd.DataFrame({coluna: pd.Series(dtype=float) for coluna in COLUNAS_NOITES})
    momentos = pd.to_datetime(df["data_hora"])
    temperatura = pd.to_numeric(df["temperatura_ar"], errors="coerce").astype(float).to_numpy()
    orvalho = pd.to_numeric(df["ponto_orvalho"], errors="coerce").astype(float).to_numpy()
    hora = momentos.dt.hour.to_numpy()
    dia = momentos.dt.normalize().to_numpy()

    tarde = (hora >= HORA_INICIO_TARDE) & (hora <= HORA_FIM_TARDE) & np.isfinite(temperatura) & np.isfinite(orvalho)
    grupo_tarde = pd.DataFrame({"data": dia[tarde], "t": temperatura[tarde], "td": orvalho[tarde]}).groupby("data")
    resumo_tarde = pd.DataFrame({"t_tarde": grupo_tarde["t"].mean(), "td_tarde": grupo_tarde["td"].mean(),
                                 "n_tarde": grupo_tarde["t"].size()})

    noite = ((hora >= HORA_INICIO_NOITE) | (hora <= HORA_FIM_NOITE)) & np.isfinite(temperatura)
    dia_da_noite = np.where(hora[noite] >= HORA_INICIO_NOITE, dia[noite], dia[noite] - np.timedelta64(1, "D"))
    grupo_noite = pd.DataFrame({"data": dia_da_noite, "t": temperatura[noite]}).groupby("data")
    resumo_noite = pd.DataFrame({"tmin_noite": grupo_noite["t"].min(), "n_noite": grupo_noite["t"].size()})
    resumo_noite = resumo_noite[resumo_noite["n_noite"] >= HORAS_MINIMAS_NOITE]

    juntas = resumo_tarde[resumo_tarde["n_tarde"] >= HORAS_MINIMAS_TARDE].join(resumo_noite, how="inner")
    anterior = resumo_noite["tmin_noite"].copy()
    anterior.index = anterior.index + pd.Timedelta(days=1)
    juntas["tmin_noite_anterior"] = anterior.reindex(juntas.index).to_numpy()
    juntas.index.name = "data"
    resultado = juntas.reset_index()[list(COLUNAS_NOITES)]
    resultado["data"] = pd.to_datetime(resultado["data"])
    return resultado.sort_values("data").reset_index(drop=True)


def chave_mes_hora(instantes: pd.Series) -> pd.Series:
    """Chave ``'MM-HH'`` (mês e hora com dois dígitos) de cada instante."""
    momentos = pd.to_datetime(pd.Series(instantes))
    return momentos.dt.strftime("%m-%H")


def climatologia_mes_hora(instantes: pd.Series, alvo: pd.Series) -> Dict[str, float]:
    """Probabilidade climatológica do alvo binário por mês × hora do dia.

    ``p = (eventos + 1) / (amostras + 2)`` (suavização de Laplace): nenhuma
    célula vale 0 ou 1, e uma célula sem dados vale 0,5. Linhas com alvo
    ausente são ignoradas.

    Returns:
        Dicionário com as 288 chaves ``'01-00'`` … ``'12-23'``, valores em (0, 1).
    """
    momentos = pd.to_datetime(pd.Series(instantes).reset_index(drop=True))
    valores = pd.to_numeric(pd.Series(alvo).reset_index(drop=True), errors="coerce").astype(float)
    validos = valores.notna().to_numpy() & momentos.notna().to_numpy()
    tabela = pd.DataFrame({"chave": chave_mes_hora(momentos[validos]).to_numpy(),
                           "y": (valores[validos].to_numpy() >= 0.5).astype(float)})
    agregado = tabela.groupby("chave")["y"].agg(["sum", "size"])
    resultado: Dict[str, float] = {}
    for mes in range(1, 13):
        for hora in range(24):
            chave = f"{mes:02d}-{hora:02d}"
            eventos, amostras = (float(agregado.at[chave, "sum"]), float(agregado.at[chave, "size"])) \
                if chave in agregado.index else (0.0, 0.0)
            resultado[chave] = (eventos + 1.0) / (amostras + 2.0)
    return resultado


def aplicar_climatologia(instantes: pd.Series, climatologia: Dict[str, float], padrao: float = 0.5) -> np.ndarray:
    """Probabilidade climatológica de cada instante (``padrao`` para chave ausente)."""
    chaves = chave_mes_hora(pd.Series(instantes))
    return np.array([float(climatologia.get(chave, padrao)) for chave in chaves], dtype=float)


def minimos_quadrados_tmin(noites: pd.DataFrame) -> Tuple[float, float, float]:
    """Ajusta ``tmin_noite = a + b·t_tarde + c·td_tarde`` por mínimos quadrados.

    Raises:
        ValueError: menos de 3 noites completas (o sistema seria indeterminado).
    """
    dados = noites[["t_tarde", "td_tarde", "tmin_noite"]].astype(float).dropna()
    if len(dados) < 3:
        raise ValueError(f"São necessárias ao menos 3 noites completas para ajustar a mínima (há {len(dados)}).")
    matriz = np.column_stack([np.ones(len(dados)), dados["t_tarde"].to_numpy(), dados["td_tarde"].to_numpy()])
    coeficientes, *_ = np.linalg.lstsq(matriz, dados["tmin_noite"].to_numpy(), rcond=None)
    return float(coeficientes[0]), float(coeficientes[1]), float(coeficientes[2])


def prever_tmin(noites: pd.DataFrame, coef: Tuple[float, float, float]) -> np.ndarray:
    """``a + b·t_tarde + c·td_tarde`` para cada noite."""
    a, b, c = (float(valor) for valor in coef)
    return a + b * noites["t_tarde"].astype(float).to_numpy() + c * noites["td_tarde"].astype(float).to_numpy()


def residuos_deixa_um_fora(noites: pd.DataFrame) -> pd.Series:
    """Resíduos (real − previsto) de deixar-uma-noite-fora do ajuste linear da mínima.

    Pela matriz chapéu: ``e_i / (1 − h_ii)``, sem reajustar n vezes. São
    resíduos FORA DA AMOSTRA, próprios para a margem conformal mesmo com
    poucas noites (o resíduo dentro da amostra subestima o erro).

    Returns:
        Série com o mesmo índice das noites completas.
    """
    dados = noites[["t_tarde", "td_tarde", "tmin_noite"]].astype(float).dropna()
    if len(dados) < 4:
        raise ValueError("São necessárias ao menos 4 noites completas para os resíduos deixa-um-fora.")
    matriz = np.column_stack([np.ones(len(dados)), dados["t_tarde"].to_numpy(), dados["td_tarde"].to_numpy()])
    alvo = dados["tmin_noite"].to_numpy()
    pseudo_inversa = np.linalg.pinv(matriz)
    coeficientes = pseudo_inversa @ alvo
    alavancas = np.einsum("ij,ji->i", matriz, pseudo_inversa)
    residuos = alvo - matriz @ coeficientes
    denominador = np.clip(1.0 - alavancas, 1e-6, None)
    return pd.Series(residuos / denominador, index=dados.index)


def quantil_conformal(valores_absolutos: np.ndarray, q: float = 0.9) -> float:
    """Quantil empírico com correção de amostra finita: o ``⌈(n+1)·q⌉``-ésimo menor valor.

    Garantia da predição conformal dividida: com resíduos permutáveis, o
    intervalo ``previsão ± quantil`` cobre o valor real com probabilidade
    ≥ q (ideia metodológica em https://arxiv.org/pdf/2609.13864; nenhum
    número do artigo é adotado).

    Returns:
        O quantil; ``inf`` quando ``⌈(n+1)·q⌉ > n`` (amostra pequena demais);
        ``nan`` sem valores.
    """
    valores = np.sort(np.abs(np.asarray(valores_absolutos, dtype=float)))
    valores = valores[np.isfinite(valores)]
    if valores.size == 0:
        return float("nan")
    posicao = int(math.ceil((valores.size + 1) * float(q)))
    if posicao > valores.size:
        return float("inf")
    return float(valores[posicao - 1])
