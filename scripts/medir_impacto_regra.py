"""Mede o impacto da regra térmica: % de horas por cor por mês e trocas de cor, v2 (instantânea) × v3 (fisiológica).

Bases:
* simulador — ``data/dataset_limpo.csv`` (gerado por ``src/gerador_dataset.py``), com a umidade do solo do
  próprio simulador; o rótulo v2 é ``gerador_dataset.calcular_status_regra`` (o mesmo ``status_alerta`` do CSV);
* dado real — reanálise ERA5-Land 2018–2021 de ``referencia_climatica/`` (Varginha/MG → Arábica, São Gabriel
  da Palha/ES → Conilon), com o solo fixo no meio do ideal (impacto só da temperatura).

O rótulo v3 é ``regras_agronomicas.rotular_status_horario`` com a configuração padrão (fisiológica, florada por
calendário, seca programada desligada). Na integração da v3 as entradas do simulador foram conferidas idênticas
às do commit ``1e20971`` (v2), então "antes" aqui é exatamente o que a v2 mostrava.

Uso (offline, ~1 min): ``python scripts/medir_impacto_regra.py``
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pandas as pd

RAIZ_PROJETO: Path = Path(__file__).resolve().parents[1]
if str(RAIZ_PROJETO / "src") not in sys.path:
    sys.path.insert(0, str(RAIZ_PROJETO / "src"))

from database import ParametrosCultura, buscar_parametros_cultura  # noqa: E402
from gerador_dataset import calcular_status_regra  # noqa: E402
from referencia_climatica import carregar_referencia  # noqa: E402
from regras_agronomicas import rotular_status_horario  # noqa: E402

MESES: Tuple[str, ...] = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")
MESES_INVERNO: Tuple[int, ...] = (6, 7, 8)
HORAS_ANO: int = 8760
CAMINHO_DATASET: Path = RAIZ_PROJETO / "data" / "dataset_limpo.csv"
REGIOES_CULTURAS: Tuple[Tuple[str, str], ...] = (("varginha_mg", "cafe_arabica"),
                                                 ("sao_gabriel_da_palha_es", "cafe_conilon"))


def trocas_de_cor(status: pd.Series) -> int:
    """Número de mudanças de cor consecutivas na série."""
    valores = np.asarray(status)
    return int((valores[1:] != valores[:-1]).sum())


def _percentuais(serie: pd.Series) -> str:
    """``'V/A/Vm'`` em % com uma casa."""
    return "/".join(f"{100.0 * float((serie == cor).mean()):5.1f}" for cor in (0, 1, 2))


def tabela_mensal(datas: pd.Series, antes: pd.Series, depois: pd.Series) -> List[str]:
    """Linhas de texto: % de horas Verde/Amarelo/Vermelho por mês, antes × depois, e o resumo do ano."""
    quadro = pd.DataFrame({"mes": pd.to_datetime(datas).dt.month.to_numpy(), "antes": antes.to_numpy(),
                           "depois": depois.to_numpy()})
    linhas = []
    for mes in range(1, 13):
        grupo = quadro[quadro["mes"] == mes]
        if grupo.empty:
            continue
        linhas.append(f"  {MESES[mes - 1]}  antes V/A/Vm {_percentuais(grupo['antes'])}   "
                      f"depois V/A/Vm {_percentuais(grupo['depois'])}")
    inverno = quadro["mes"].isin(MESES_INVERNO)
    fator = HORAS_ANO / max(1, len(quadro))
    linhas.append(
        f"  ANO  antes {_percentuais(quadro['antes'])}  depois {_percentuais(quadro['depois'])} | "
        f"Vermelho jun–ago: {100.0 * float((quadro.loc[inverno, 'antes'] == 2).mean()):.1f}% → "
        f"{100.0 * float((quadro.loc[inverno, 'depois'] == 2).mean()):.1f}% | trocas de cor/ano: "
        f"{trocas_de_cor(quadro['antes']) * fator:.0f} → {trocas_de_cor(quadro['depois']) * fator:.0f}")
    return linhas


def _rotulos(serie: pd.DataFrame, parametros: ParametrosCultura) -> Tuple[pd.Series, pd.Series]:
    """``(v2, v3)`` para uma série horária com ``data_hora``, ``temperatura_ar`` e ``umidade_solo``."""
    antes = pd.Series([calcular_status_regra(float(t), float(s), parametros)
                       for t, s in zip(serie["temperatura_ar"], serie["umidade_solo"])], index=serie.index)
    depois = rotular_status_horario(serie[["data_hora", "temperatura_ar", "umidade_solo"]], parametros)
    return antes, depois


def medir_simulador(caminho_dataset: Path = CAMINHO_DATASET) -> List[str]:
    """Tabelas do simulador para cada cultura do CSV."""
    dados = pd.read_csv(caminho_dataset)
    dados["data_hora"] = pd.to_datetime(dados["data_hora"])
    linhas: List[str] = []
    for codigo in sorted(dados["cultura"].unique()):
        parametros = buscar_parametros_cultura(str(codigo))
        serie = dados[dados["cultura"] == codigo].reset_index(drop=True)
        antes, depois = _rotulos(serie, parametros)
        linhas.append(f"SIMULADOR — {parametros.nome_comum} ({len(serie)} h)")
        linhas.extend(tabela_mensal(serie["data_hora"], antes, depois))
    return linhas


def medir_referencia() -> List[str]:
    """Tabelas do ERA5-Land (solo fixo no meio do ideal) para as duas regiões de referência."""
    linhas: List[str] = []
    for regiao, codigo in REGIOES_CULTURAS:
        parametros = buscar_parametros_cultura(codigo)
        faixa_solo = parametros.faixa("umidade_solo")
        solo = (faixa_solo.ideal_min + faixa_solo.ideal_max) / 2.0
        referencia = carregar_referencia(regiao)
        serie = pd.DataFrame({"data_hora": referencia["data_hora"], "temperatura_ar": referencia["temperatura_ar"],
                              "umidade_solo": solo})
        antes, depois = _rotulos(serie, parametros)
        linhas.append(f"DADO REAL ERA5-Land — {regiao} / {parametros.nome_comum} ({len(serie)} h; "
                      f"média {serie['temperatura_ar'].mean():.1f} °C; solo fixo {solo:.0f} %)")
        linhas.extend(tabela_mensal(serie["data_hora"], antes, depois))
    return linhas


def main() -> int:
    """Imprime as tabelas; devolve 0 (1 se faltar o dataset do simulador)."""
    if not CAMINHO_DATASET.is_file():
        print(f"Falta {CAMINHO_DATASET}: rode antes 'python src/gerador_dataset.py'.")
        return 1
    for linha in medir_simulador() + [""] + medir_referencia():
        print(linha)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
