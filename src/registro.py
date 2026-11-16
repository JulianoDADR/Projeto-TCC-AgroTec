"""Registro (logs) do AgroAnálise para operação 24/7 sem intervenção.

Cada serviço (``coletor``, ``web``) escreve em ``logs/<nome>.log`` com rotação
por tamanho (5 arquivos de 5 MB no máximo: o disco do computador do sítio
nunca enche por causa de log) e também na saída de erro, que o systemd leva
ao ``journald`` (``journalctl -u agroanalise-coletor``).

Os logs de acesso HTTP (``werkzeug`` e ``waitress``) ficam em WARNING: a
página do agricultor consulta ``/api/status`` a cada poucos segundos e
encheria o arquivo de linhas sem valor.

A pasta pode ser trocada pela variável de ambiente ``AGRO_PASTA_LOGS``.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List

RAIZ: Path = Path(__file__).resolve().parent.parent
PASTA_LOGS_PADRAO: Path = RAIZ / "logs"
TAMANHO_MAXIMO_LOG_BYTES: int = 5 * 1024 * 1024
QUANTIDADE_BACKUPS_LOG: int = 5
FORMATO_LOG: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
FORMATO_DATA_LOG: str = "%Y-%m-%d %H:%M:%S"
REGISTRADORES_ACESSO_HTTP: tuple = ("werkzeug", "waitress", "waitress.queue")

# Marca colocada nos handlers criados aqui (idempotência e desfazer).
_MARCA_HANDLER: str = "_agroanalise_registro"


def _pasta_padrao() -> Path:
    """Pasta de logs: ``AGRO_PASTA_LOGS`` (se definida) ou ``<raiz do projeto>/logs``."""
    variavel = os.environ.get("AGRO_PASTA_LOGS", "").strip()
    return Path(variavel).expanduser() if variavel else PASTA_LOGS_PADRAO


def _handlers_agroanalise(raiz: logging.Logger) -> List[logging.Handler]:
    """Handlers do logger raiz criados por ``configurar_logs``."""
    return [handler for handler in raiz.handlers if getattr(handler, _MARCA_HANDLER, None) is not None]


def configurar_logs(
    nome: str,
    pasta: Path = PASTA_LOGS_PADRAO,
    nivel: int = logging.INFO,
    verboso: bool = False,
) -> logging.Logger:
    """Configura o log do processo: arquivo rotativo ``<pasta>/<nome>.log`` + saída de erro (journald).

    Idempotente: chamar de novo com o mesmo arquivo não duplica handlers; com
    outro nome/pasta, os handlers anteriores criados aqui são trocados.

    Args:
        nome: nome do serviço (vira o nome do arquivo), ex.: ``"coletor"`` ou ``"web"``.
        pasta: pasta dos arquivos de log (``AGRO_PASTA_LOGS`` tem prioridade
            quando ``pasta`` é o padrão).
        nivel: nível mínimo registrado (``logging.INFO``).
        verboso: se verdadeiro, registra também DEBUG (ex.: uma linha por leitura).

    Returns:
        O logger ``agroanalise.<nome>``.

    Raises:
        ValueError: nome vazio ou com separador de pasta.
    """
    nome_limpo = str(nome or "").strip()
    if not nome_limpo or "/" in nome_limpo or "\\" in nome_limpo:
        raise ValueError(f"Nome de log inválido: {nome!r}.")
    destino = Path(pasta).expanduser() if Path(pasta) != PASTA_LOGS_PADRAO else _pasta_padrao()
    destino.mkdir(parents=True, exist_ok=True)
    arquivo = (destino / f"{nome_limpo}.log").resolve()
    nivel_efetivo = logging.DEBUG if verboso else int(nivel)
    raiz = logging.getLogger()

    existentes = _handlers_agroanalise(raiz)
    if any(getattr(handler, _MARCA_HANDLER, None) == str(arquivo) for handler in existentes):
        raiz.setLevel(nivel_efetivo)
        for handler in existentes:
            handler.setLevel(nivel_efetivo)
    else:
        for handler in existentes:
            raiz.removeHandler(handler)
            handler.close()
        formatador = logging.Formatter(FORMATO_LOG, datefmt=FORMATO_DATA_LOG)
        em_arquivo = RotatingFileHandler(arquivo, maxBytes=TAMANHO_MAXIMO_LOG_BYTES,
                                         backupCount=QUANTIDADE_BACKUPS_LOG, encoding="utf-8")
        na_tela = logging.StreamHandler(sys.stderr)
        for handler in (em_arquivo, na_tela):
            handler.setFormatter(formatador)
            handler.setLevel(nivel_efetivo)
            setattr(handler, _MARCA_HANDLER, str(arquivo))
            raiz.addHandler(handler)
        raiz.setLevel(nivel_efetivo)
    for nome_http in REGISTRADORES_ACESSO_HTTP:
        logging.getLogger(nome_http).setLevel(logging.WARNING)
    return logging.getLogger(f"agroanalise.{nome_limpo}")


def desfazer_configuracao_logs() -> None:
    """Remove e fecha os handlers criados por ``configurar_logs`` (uso em testes e no encerramento)."""
    raiz = logging.getLogger()
    for handler in _handlers_agroanalise(raiz):
        raiz.removeHandler(handler)
        handler.close()
