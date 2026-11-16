"""Servidor web de produção do AgroAnálise (waitress), para rodar 24/7 como serviço.

``python src/app.py`` continua servindo para desenvolvimento (servidor do
Flask). No computador do sítio use este módulo — o servidor ``waitress`` é
multiusuário, estável por meses e não precisa de compilador:

    python src/servidor.py [--host 0.0.0.0] [--porta 5000] [--banco data/agroanalise.db]

Variáveis de ambiente (as opções da linha de comando têm prioridade):
    * ``AGRO_HOST``  — endereço de escuta (padrão ``0.0.0.0``: celulares na rede do sítio);
    * ``AGRO_PORTA`` — porta TCP (padrão ``5000``);
    * ``AGRO_BANCO`` — caminho do banco SQLite (padrão: o de ``database.CAMINHO_BANCO``).

Os logs vão para ``logs/web.log`` (rotativo) e para a saída de erro (journald).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

PASTA_SRC: Path = Path(__file__).resolve().parent
if str(PASTA_SRC) not in sys.path:
    sys.path.insert(0, str(PASTA_SRC))

from registro import configurar_logs  # noqa: E402

HOST_PADRAO: str = "0.0.0.0"
PORTA_PADRAO: int = 5000
THREADS_SERVIDOR: int = 8

registrador: logging.Logger = logging.getLogger("agroanalise.servidor")


def _porta_valida(texto: str) -> int:
    """Converte e valida a porta TCP (1–65535) para o argparse.

    Raises:
        argparse.ArgumentTypeError: porta fora da faixa ou não numérica.
    """
    try:
        porta = int(str(texto).strip())
    except ValueError as erro:
        raise argparse.ArgumentTypeError(f"porta inválida: {texto!r}") from erro
    if not 1 <= porta <= 65535:
        raise argparse.ArgumentTypeError(f"porta fora da faixa 1–65535: {porta}")
    return porta


def _criar_parser() -> argparse.ArgumentParser:
    """Monta o parser da linha de comando (padrões vindos das variáveis AGRO_*)."""
    parser = argparse.ArgumentParser(
        prog="servidor.py",
        description="AgroAnálise — servidor web de produção (waitress) do Companheiro Virtual.",
    )
    parser.add_argument("--host", default=os.environ.get("AGRO_HOST", HOST_PADRAO),
                        help="Endereço de escuta (padrão: AGRO_HOST ou 0.0.0.0).")
    parser.add_argument("--porta", type=_porta_valida, default=None,
                        help="Porta TCP (padrão: AGRO_PORTA ou 5000).")
    parser.add_argument("--banco", type=Path, default=None,
                        help="Banco SQLite (padrão: AGRO_BANCO ou data/agroanalise.db).")
    parser.add_argument("--verboso", action="store_true", help="Registra também mensagens de depuração.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Sobe o aplicativo Flask no waitress e bloqueia até o encerramento.

    Returns:
        0 ao encerrar normalmente, 2 para configuração inválida.
    """
    parser = _criar_parser()
    argumentos = parser.parse_args(argv)
    configurar_logs("web", verboso=argumentos.verboso)
    porta = argumentos.porta
    if porta is None:
        try:
            porta = _porta_valida(os.environ.get("AGRO_PORTA", str(PORTA_PADRAO)))
        except argparse.ArgumentTypeError as erro:
            registrador.error("AGRO_PORTA inválida: %s", erro)
            return 2
    banco_texto = str(argumentos.banco) if argumentos.banco is not None else os.environ.get("AGRO_BANCO", "").strip()

    from waitress import serve  # import tardio: só o servidor de produção depende do waitress

    from app import app
    from database import inicializar_banco

    if banco_texto:
        caminho_banco = Path(banco_texto).expanduser()
        inicializar_banco(caminho_banco)
        app.config["CAMINHO_BANCO"] = caminho_banco
    registrador.info("Companheiro Virtual (waitress) em http://%s:%d — banco %s.", argumentos.host, porta,
                     app.config.get("CAMINHO_BANCO"))
    try:
        serve(app, host=argumentos.host, port=porta, threads=THREADS_SERVIDOR, ident="AgroAnalise")
    except KeyboardInterrupt:
        registrador.info("Servidor encerrado pelo usuário (Ctrl+C).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
