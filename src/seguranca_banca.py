"""Proteção do painel da banca contra toques acidentais e curiosos.

O painel da banca (Modo Demonstração) e a troca de cultura mudam o que TODOS
os celulares mostram. Numa propriedade, basta um toque acidental de uma
criança para o semáforo de todos passar a mostrar um cenário de teste. Este
módulo oferece um PIN opcional, guardado só como hash PBKDF2-SHA256 na
configuração ``pin_banca_hash`` (string vazia = sem PIN = comportamento
original), e um limitador de tentativas por endereço IP.

v4: as respostas distinguem "falta a senha" (``CODIGO_PIN_NECESSARIO``) de
"senha errada" (``CODIGO_PIN_ERRADO``, ``MENSAGEM_PIN_ERRADO``), e o bloqueio
diz quanto falta (``LimitadorTentativas.segundos_restantes``), para a tela
mostrar a contagem em vez de um aviso que some.

Linha de comando (para o técnico)::

    python src/seguranca_banca.py --definir-pin 1234 [--banco CAMINHO]
    python src/seguranca_banca.py --remover-pin [--banco CAMINHO]
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import math
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from database import CAMINHO_BANCO, definir_configuracao, inicializar_banco

CHAVE_CONFIGURACAO_PIN: str = "pin_banca_hash"
ALGORITMO_PIN: str = "pbkdf2_sha256"
ITERACOES_PADRAO: int = 200_000
TAMANHO_SAL_BYTES: int = 16
DIGITOS_MINIMOS_PIN: int = 4
DIGITOS_MAXIMOS_PIN: int = 12
# Códigos e texto das respostas de PIN (v4): a página trata cada caso de um jeito.
CODIGO_PIN_ERRADO: str = "pin_errado"
CODIGO_PIN_NECESSARIO: str = "pin_necessario"
MENSAGEM_PIN_ERRADO: str = "Senha errada. Tente de novo."


def gerar_hash_pin(pin: str, iteracoes: int = ITERACOES_PADRAO) -> str:
    """Gera o hash do PIN no formato ``pbkdf2_sha256$<iter>$<salt_hex>$<hash_hex>``.

    Raises:
        ValueError: PIN vazio ou número de iterações menor que 1.
    """
    texto = str(pin or "")
    if not texto:
        raise ValueError("O PIN não pode ser vazio.")
    if int(iteracoes) < 1:
        raise ValueError("O número de iterações precisa ser positivo.")
    sal = secrets.token_bytes(TAMANHO_SAL_BYTES)
    resumo = hashlib.pbkdf2_hmac("sha256", texto.encode("utf-8"), sal, int(iteracoes))
    return f"{ALGORITMO_PIN}${int(iteracoes)}${sal.hex()}${resumo.hex()}"


def verificar_pin(pin: str, hash_armazenado: str) -> bool:
    """Confere o PIN contra o hash gravado (comparação em tempo constante).

    Devolve ``False`` para PIN vazio ou hash malformado, sem lançar exceção.
    """
    texto = str(pin or "")
    partes = str(hash_armazenado or "").split("$")
    if not texto or len(partes) != 4 or partes[0] != ALGORITMO_PIN:
        return False
    try:
        iteracoes = int(partes[1])
        sal = bytes.fromhex(partes[2])
        esperado = bytes.fromhex(partes[3])
    except ValueError:
        return False
    if iteracoes < 1 or not sal or not esperado:
        return False
    calculado = hashlib.pbkdf2_hmac("sha256", texto.encode("utf-8"), sal, iteracoes)
    return hmac.compare_digest(calculado, esperado)


class LimitadorTentativas:
    """Bloqueia por ``bloqueio_s`` segundos o IP que errar o PIN ``max_erros`` vezes seguidas.

    Seguro para uso concorrente (o Flask atende requisições em threads).
    """

    def __init__(self, max_erros: int = 5, bloqueio_s: float = 60.0,
                 relogio: Callable[[], float] = time.monotonic) -> None:
        """Configura o limite de erros, a duração do bloqueio e o relógio (injetável em testes)."""
        self.max_erros: int = max(1, int(max_erros))
        self.bloqueio_s: float = max(0.0, float(bloqueio_s))
        self._relogio: Callable[[], float] = relogio
        self._trava: threading.Lock = threading.Lock()
        self._erros: Dict[str, int] = {}
        self._bloqueado_ate: Dict[str, float] = {}

    def registrar_erro(self, ip: str) -> None:
        """Conta um erro do IP; ao chegar em ``max_erros``, inicia o bloqueio."""
        chave = str(ip or "desconhecido")
        with self._trava:
            total = self._erros.get(chave, 0) + 1
            self._erros[chave] = total
            if total >= self.max_erros:
                self._bloqueado_ate[chave] = self._relogio() + self.bloqueio_s

    def bloqueado(self, ip: str) -> bool:
        """``True`` enquanto o IP estiver bloqueado; ao vencer, zera a contagem."""
        chave = str(ip or "desconhecido")
        with self._trava:
            limite = self._bloqueado_ate.get(chave)
            if limite is None:
                return False
            if self._relogio() < limite:
                return True
            self._bloqueado_ate.pop(chave, None)
            self._erros.pop(chave, None)
            return False

    def segundos_restantes(self, ip: str) -> int:
        """Segundos inteiros que faltam para o IP poder tentar de novo; 0 se não está bloqueado.

        Arredonda para cima, então um IP bloqueado sempre vê ao menos 1 segundo e
        nunca mais que ``bloqueio_s``. Ao vencer o bloqueio, zera a contagem (como
        ``bloqueado``).

        Args:
            ip: endereço do cliente.

        Returns:
            0 sem bloqueio; um inteiro em ``[1, ceil(bloqueio_s)]`` durante o bloqueio.
        """
        chave = str(ip or "desconhecido")
        with self._trava:
            limite = self._bloqueado_ate.get(chave)
            if limite is None:
                return 0
            restante = limite - self._relogio()
            if restante <= 0:
                self._bloqueado_ate.pop(chave, None)
                self._erros.pop(chave, None)
                return 0
            return max(1, min(int(math.ceil(restante)), max(1, int(math.ceil(self.bloqueio_s)))))

    def limpar(self, ip: str) -> None:
        """Esquece os erros e o bloqueio do IP (ex.: depois de um PIN certo)."""
        chave = str(ip or "desconhecido")
        with self._trava:
            self._erros.pop(chave, None)
            self._bloqueado_ate.pop(chave, None)


class LimitadorJanela:
    """Aceita no máximo ``max_eventos`` pedidos por IP numa janela deslizante de ``janela_s`` segundos.

    Protege as rotas SEM PIN do agricultor ("Feito", "Chamar o técnico",
    eventos da tela) contra toques repetidos e scripts: passado o limite, a rota
    responde 429 até a janela andar. Seguro para uso concorrente.
    """

    def __init__(self, max_eventos: int, janela_s: float,
                 relogio: Callable[[], float] = time.monotonic) -> None:
        """Configura o limite, a janela e o relógio (injetável em testes)."""
        self.max_eventos: int = max(1, int(max_eventos))
        self.janela_s: float = max(0.001, float(janela_s))
        self._relogio: Callable[[], float] = relogio
        self._trava: threading.Lock = threading.Lock()
        self._instantes: Dict[str, List[float]] = {}

    def permitir(self, ip: str) -> bool:
        """Conta o pedido e devolve ``True`` se ainda estiver dentro do limite; ``False`` = 429.

        Pedidos recusados não entram na conta (quem insiste não se bloqueia para sempre).
        """
        chave = str(ip or "desconhecido")
        agora = self._relogio()
        with self._trava:
            recentes = [instante for instante in self._instantes.get(chave, []) if agora - instante < self.janela_s]
            if len(recentes) >= self.max_eventos:
                self._instantes[chave] = recentes
                return False
            recentes.append(agora)
            self._instantes[chave] = recentes
            if len(self._instantes) > 1024:  # muitos IPs: esquece os que já saíram da janela
                for outro in [ip_antigo for ip_antigo, lista in self._instantes.items()
                              if not lista or agora - lista[-1] >= self.janela_s]:
                    self._instantes.pop(outro, None)
            return True

    def limpar(self, ip: Optional[str] = None) -> None:
        """Esquece os pedidos de um IP (ou de todos, com ``None``)."""
        with self._trava:
            if ip is None:
                self._instantes.clear()
            else:
                self._instantes.pop(str(ip or "desconhecido"), None)


def validar_pin_digitado(pin: str) -> str:
    """PIN da linha de comando: só dígitos, de 4 a 12.

    Raises:
        ValueError: PIN fora do formato.
    """
    texto = str(pin or "").strip()
    if not texto.isdigit() or not DIGITOS_MINIMOS_PIN <= len(texto) <= DIGITOS_MAXIMOS_PIN:
        raise ValueError(f"O PIN precisa ter de {DIGITOS_MINIMOS_PIN} a {DIGITOS_MAXIMOS_PIN} números.")
    return texto


def _interpretar_argumentos(argv: Optional[List[str]]) -> argparse.Namespace:
    """Lê ``--definir-pin``/``--remover-pin`` e ``--banco``."""
    analisador = argparse.ArgumentParser(
        description="Define ou remove o PIN do painel da banca do AgroAnálise.",
    )
    grupo = analisador.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--definir-pin", metavar="PIN", help="PIN numérico (4 a 12 dígitos) exigido pela banca.")
    grupo.add_argument("--remover-pin", action="store_true", help="Remove o PIN (painel volta a ficar livre).")
    analisador.add_argument("--banco", type=Path, default=None, help="Caminho do banco SQLite (padrão: data/agroanalise.db).")
    return analisador.parse_args(argv)


def aplicar_comando(definir_pin: Optional[str], remover_pin: bool, caminho_banco: Path) -> Tuple[bool, str]:
    """Grava ou remove o hash do PIN; devolve ``(sucesso, mensagem)``."""
    inicializar_banco(caminho_banco)
    if remover_pin:
        definir_configuracao(CHAVE_CONFIGURACAO_PIN, "", caminho_banco=caminho_banco)
        return True, "PIN da banca removido: o painel fica livre (só escondido)."
    try:
        pin = validar_pin_digitado(str(definir_pin or ""))
    except ValueError as erro:
        return False, str(erro)
    definir_configuracao(CHAVE_CONFIGURACAO_PIN, gerar_hash_pin(pin), caminho_banco=caminho_banco)
    return True, "PIN da banca definido. Ele será pedido na banca e na troca de plantação."


def main(argv: Optional[List[str]] = None) -> int:
    """Ponto de entrada da linha de comando; devolve o código de saída (0 = sucesso)."""
    argumentos = _interpretar_argumentos(argv)
    caminho = Path(argumentos.banco) if argumentos.banco is not None else CAMINHO_BANCO
    sucesso, mensagem = aplicar_comando(argumentos.definir_pin, bool(argumentos.remover_pin), caminho)
    print(mensagem, file=sys.stdout if sucesso else sys.stderr)
    return 0 if sucesso else 2


if __name__ == "__main__":
    sys.exit(main())
