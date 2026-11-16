"""Refaz os arquivos da referência climática ERA5-Land (uso OPCIONAL, fora do campo, com internet).

Lê, por HTTP Range, só a célula de grade de cada ponto nos arquivos ``.om`` da Open-Meteo no AWS Open
Data (``https://openmeteo.s3.amazonaws.com/data/copernicus_era5_land``; documentação em
https://github.com/open-meteo/open-data; dados Copernicus ERA5-Land, licença CC BY 4.0, atribuição
"Weather data by Open-Meteo.com"). Grava ``referencia_climatica/era5land_<regiao>_<ano0>_<ano1>.csv.gz``
com as colunas que ``src/referencia_climatica.py`` espera.

O sistema no sítio NÃO usa este script: os arquivos de 2018–2021 já estão no repositório. Dependências
em ``requirements-referencia.txt`` (``pip install -r requirements-referencia.txt``).

Uso: ``python scripts/baixar_referencia_era5land.py [--anos 2018 2021] [--saida referencia_climatica]
[--cache /tmp/cache_om] [--regioes varginha_mg sao_gabriel_da_palha_es]``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

RAIZ: Path = Path(__file__).resolve().parent.parent
BASE_OPEN_METEO: str = "https://openmeteo.s3.amazonaws.com/data/copernicus_era5_land"
VARIAVEIS: Tuple[str, ...] = ("temperature_2m", "dew_point_2m", "soil_moisture_0_to_7cm", "soil_moisture_7_to_28cm")
RESOLUCAO_GRAUS: float = 0.1  # grade regular do ERA5-Land
# Coordenadas das cidades; a célula usada é a de grade 0,1° mais próxima (Varginha → −21,6/−45,4;
# São Gabriel da Palha → −19,0/−40,5, as mesmas de referencia_climatica.REGIOES_REFERENCIA).
PONTOS: Dict[str, Tuple[float, float]] = {
    "varginha_mg": (-21.55, -45.43),
    "sao_gabriel_da_palha_es": (-19.02, -40.54),
}
TEMPO_LIMITE_SEGUNDOS: int = 60

registrador: logging.Logger = logging.getLogger("agroanalise.baixar_referencia")


def indice_grade(latitude: float, longitude: float) -> Tuple[int, int]:
    """Índices (y, x) da célula 0,1° mais próxima na grade global que começa em −90° / −180°."""
    return int(round((latitude + 90.0) / RESOLUCAO_GRAUS)), int(round((longitude + 180.0) / RESOLUCAO_GRAUS))


def ler_variavel(variavel: str, ano: int, y: int, x: int, cache: Path) -> np.ndarray:
    """Série horária de UMA variável num ano (leitura parcial do arquivo ``.om``)."""
    try:
        import aiohttp  # noqa: PLC0415 — dependência opcional (requirements-referencia.txt)
        import fsspec  # noqa: PLC0415
        from omfiles import OmFileReader  # noqa: PLC0415
    except ImportError as erro:  # pragma: no cover — depende do ambiente
        raise SystemExit(f"Dependências ausentes ({erro}). Rode: pip install -r requirements-referencia.txt") from erro
    arquivo = fsspec.open(
        f"blockcache::{BASE_OPEN_METEO}/{variavel}/year_{int(ano)}.om", mode="rb",
        https={"client_kwargs": {"trust_env": True, "timeout": aiohttp.ClientTimeout(total=TEMPO_LIMITE_SEGUNDOS)},
               "block_size": 65536},
        blockcache={"cache_storage": str(cache)},
    )
    with OmFileReader(arquivo) as leitor:
        return np.asarray(leitor[y, x, :], dtype=float)


def extrair_regiao(regiao: str, anos: List[int], cache: Path) -> pd.DataFrame:
    """Todas as variáveis de uma região nos anos pedidos, com o índice ``utc`` horário."""
    latitude, longitude = PONTOS[regiao]
    y, x = indice_grade(latitude, longitude)
    colunas: Dict[str, np.ndarray] = {}
    for variavel in VARIAVEIS:
        colunas[variavel] = np.concatenate([ler_variavel(variavel, ano, y, x, cache) for ano in anos])
    indice = pd.date_range(f"{anos[0]}-01-01", periods=len(colunas[VARIAVEIS[0]]), freq="h", tz="UTC")
    tabela = pd.DataFrame(colunas, index=indice)
    tabela.index.name = "utc"
    registrador.info("%s: célula y=%d x=%d (lat %.1f, lon %.1f), %d horas.", regiao, y, x,
                     -90.0 + y * RESOLUCAO_GRAUS, -180.0 + x * RESOLUCAO_GRAUS, len(tabela))
    return tabela


def main(argv: Optional[List[str]] = None) -> int:
    """Baixa e grava os ``.csv.gz`` da referência; devolve 0 em sucesso e 1 em erro."""
    parser = argparse.ArgumentParser(description="Refaz a referência climática ERA5-Land do AgroAnálise.")
    parser.add_argument("--anos", nargs=2, type=int, default=[2018, 2021], metavar=("INICIO", "FIM"))
    parser.add_argument("--saida", type=Path, default=RAIZ / "referencia_climatica")
    parser.add_argument("--cache", type=Path, default=Path.home() / ".cache" / "agroanalise_om")
    parser.add_argument("--regioes", nargs="+", default=list(PONTOS), choices=list(PONTOS))
    argumentos = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    anos = list(range(int(argumentos.anos[0]), int(argumentos.anos[1]) + 1))
    argumentos.saida.mkdir(parents=True, exist_ok=True)
    argumentos.cache.mkdir(parents=True, exist_ok=True)
    try:
        for regiao in argumentos.regioes:
            tabela = extrair_regiao(regiao, anos, argumentos.cache)
            destino = argumentos.saida / f"era5land_{regiao}_{anos[0]}_{anos[-1]}.csv.gz"
            tabela.to_csv(destino, float_format="%.3f", compression={"method": "gzip", "mtime": 0})
            registrador.info("Gravado %s.", destino)
    except Exception as erro:  # noqa: BLE001 — ponto de entrada: registra e devolve código de erro
        registrador.exception("Falha ao baixar a referência ERA5-Land: %s", erro)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
