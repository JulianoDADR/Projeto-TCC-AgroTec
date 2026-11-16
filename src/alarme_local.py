"""Alarme sonoro local na estação base: o alerta toca mesmo sem nenhum celular aberto.

Na v2 a repetição do Vermelho só acontecia com a página À VISTA no celular
(``repetirAlertaVermelho`` em ``src/templates/index.html``). Numa família em
que o celular é compartilhado e passa o dia fora de casa, o aviso podia não
chegar a ninguém (uso compartilhado de celulares: https://dl.acm.org/doi/10.1145/3290605.3300410).
Este serviço roda no computador do sítio, ao lado do coletor, e toca bipes no
alto-falante dele:

    * ``VERMELHO``: na TRANSIÇÃO para Vermelho (só leitura de campo; nunca de
      simulação — dados de teste, v4 — nem de demonstração da banca);
    * ``GEADA``: quando o prognóstico avisa ``GEADA_NOITE`` entre 17:00 e 19:59
      (ainda com luz do dia para cobrir as mudas), uma vez por noite.

Não toca: com ``alarme_local_ativo = false``; no horário de silêncio
(``alarme_silencio_inicio``–``alarme_silencio_fim``, padrão 21:30–05:00); com a
causa silenciada por um "Feito" recente; nem mais de 3 vezes em 10 minutos. Um
Vermelho que começou no silêncio toca quando o silêncio acaba, se continuar.
EXCEÇÃO: o Vermelho de GEADA (``motivo_regra = GEADA_AGORA``) toca mesmo no
horário de silêncio — a geada de radiação acontece justamente de madrugada
(mínima entre 4 h e 7 h) e é quando o produtor ainda pode irrigar por aspersão,
acender proteção ou cobrir mudas antes do sol nascer (Epamig/Embrapa Café:
https://revistacultivar.com.br/noticias/pesquisadores-da-epamig-e-embrapa-cafe-orientam-cafeicultores-sobre-o-que-fazer-apos-as-geadas).
O limite de 3 toques em 10 minutos e o "Feito" continuam valendo.

Os bipes seguem o padrão da página (1, 2 ou 3 tons, como nas cores), gerados
em memória com ``wave`` + ``math`` e tocados por ``aplay``/``paplay``
(Linux) ou ``winsound`` (Windows); sem dispositivo de som, só registra no log.

Uso: ``python src/alarme_local.py [--banco data/agroanalise.db] [--intervalo 30] [--testar-som]``.
"""

from __future__ import annotations

import argparse
import importlib
import io
import logging
import math
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import FrameType, ModuleType
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

PASTA_SRC: Path = Path(__file__).resolve().parent
if str(PASTA_SRC) not in sys.path:
    sys.path.insert(0, str(PASTA_SRC))

from acoes_orientacao import derivar_causa  # noqa: E402
from database import (  # noqa: E402
    CAMINHO_BANCO,
    CULTURA_PADRAO,
    MODO_CAMPO_USB,
    STATUS_VERMELHO,
    buscar_historico,
    buscar_parametros_cultura,
    inicializar_banco,
    obter_todas_configuracoes,
)
from retorno_agricultor import causa_silenciada, limpar_eventos_antigos  # noqa: E402

registrador: logging.Logger = logging.getLogger("agroanalise.alarme")

PADRAO_VERMELHO: str = "VERMELHO"
PADRAO_GEADA: str = "GEADA"
# Só leitura de campo toca. Integração v4 (meta 11, "0 alarmes em simulação"): a Simulação é o
# fallback automático com o cabo solto — a tela mostra "Dados de teste: não são da sua lavoura" com o
# semáforo apagado, e o alarme da estação não pode dizer o contrário (efeito lobo).
ORIGENS_QUE_TOCAM: Tuple[str, ...] = (MODO_CAMPO_USB,)
# Motivos de Vermelho que furam o horário de silêncio (dano irreversível que acontece de madrugada).
MOTIVOS_QUE_IGNORAM_SILENCIO: Tuple[str, ...] = ("GEADA_AGORA",)
# Aviso de geada à tarde: mesma janela do prognóstico (17 h às 19:59), com luz para agir.
HORA_INICIO_GEADA: int = 17
HORA_FIM_GEADA: int = 20
MAXIMO_TOQUES_JANELA: int = 3
JANELA_TOQUES: timedelta = timedelta(minutes=10)
SILENCIO_INICIO_PADRAO: str = "21:30"
SILENCIO_FIM_PADRAO: str = "05:00"
INTERVALO_PADRAO_S: float = 30.0
# Leitura mais velha que isso não toca alarme (o coletor parado é avisado pela página e por /api/saude).
IDADE_MAXIMA_LEITURA: timedelta = timedelta(minutes=10)
LIMPEZA_EVENTOS_A_CADA: timedelta = timedelta(hours=1)
TEMPO_LIMITE_TOCADOR_S: float = 15.0

# Bipes por padrão: (quantidade, frequência Hz, duração s, pausa s) — iguais aos da página
# (verde 1 grave, amarelo 2 médios, vermelho 3 agudos). A geada usa o padrão do Amarelo:
# é um aviso antecipado, não um perigo medido agora.
BIPES: Dict[str, Tuple[int, float, float, float]] = {
    "VERDE": (1, 330.0, 0.8, 0.2),
    "AMARELO": (2, 660.0, 0.32, 0.18),
    PADRAO_GEADA: (2, 660.0, 0.32, 0.18),
    PADRAO_VERMELHO: (3, 1046.0, 0.14, 0.09),
}
REPETICOES_SEQUENCIA: int = 3
PAUSA_ENTRE_SEQUENCIAS_S: float = 0.7
TAXA_AMOSTRAGEM: int = 22050
VOLUME: float = 0.8


class TocadorSom(Protocol):
    """Quem toca um padrão de bipes (o do sistema ou um dublê nos testes)."""

    def tocar(self, padrao: str) -> None:
        """Toca o padrão (``VERMELHO``, ``GEADA``...); nunca deve derrubar o serviço."""


def gerar_wav(padrao: str, taxa: int = TAXA_AMOSTRAGEM) -> bytes:
    """WAV mono 16 bits do padrão de bipes, gerado em memória (sem arquivo de som no projeto).

    Cada bipe tem rampa de 10 ms na entrada e na saída (sem estalo). A
    sequência se repete ``REPETICOES_SEQUENCIA`` vezes para ser ouvida longe.
    """
    quantidade, frequencia, duracao, pausa = BIPES.get(padrao, BIPES["AMARELO"])
    amostras: List[int] = []
    rampa = max(1, int(0.01 * taxa))
    for repeticao in range(REPETICOES_SEQUENCIA):
        for _ in range(quantidade):
            total = int(duracao * taxa)
            for indice in range(total):
                envelope = min(1.0, indice / rampa, (total - indice) / rampa)
                valor = VOLUME * envelope * math.sin(2.0 * math.pi * frequencia * indice / taxa)
                amostras.append(int(max(-1.0, min(1.0, valor)) * 32767))
            amostras.extend([0] * int(pausa * taxa))
        if repeticao < REPETICOES_SEQUENCIA - 1:
            amostras.extend([0] * int(PAUSA_ENTRE_SEQUENCIAS_S * taxa))
    memoria = io.BytesIO()
    with wave.open(memoria, "wb") as arquivo:
        arquivo.setnchannels(1)
        arquivo.setsampwidth(2)
        arquivo.setframerate(taxa)
        arquivo.writeframes(struct.pack(f"<{len(amostras)}h", *amostras))
    return memoria.getvalue()


class TocadorSistema:
    """Toca os bipes no alto-falante do computador: ``winsound`` no Windows, ``aplay``/``paplay`` no Linux.

    Sem tocador disponível, ou se ele falhar, só registra no log: o alarme
    nunca derruba o serviço.
    """

    def __init__(self, executar: Callable[..., Any] = subprocess.run) -> None:
        """Guarda a função de execução de processos (injetável nos testes)."""
        self._executar = executar

    def _comando_linux(self) -> Optional[str]:
        """Caminho do ``aplay`` (ALSA) ou do ``paplay`` (PulseAudio/PipeWire); ``None`` se nenhum existir."""
        for programa in ("aplay", "paplay"):
            caminho = shutil.which(programa)
            if caminho:
                return caminho
        return None

    def tocar(self, padrao: str) -> None:
        """Toca o padrão; qualquer falha vira um aviso no log."""
        registrador.warning("ALARME LOCAL: %s", padrao)
        try:
            if os.name == "nt":
                self._tocar_windows(padrao)
                return
            comando = self._comando_linux()
            if comando is None:
                registrador.warning("Sem aplay/paplay neste computador: alarme %s só no log.", padrao)
                return
            with tempfile.NamedTemporaryFile(prefix="agro_alarme_", suffix=".wav", delete=False) as arquivo:
                arquivo.write(gerar_wav(padrao))
                caminho_wav = arquivo.name
            try:
                argumentos = [comando, "-q", caminho_wav] if comando.endswith("aplay") else [comando, caminho_wav]
                self._executar(argumentos, check=False, timeout=TEMPO_LIMITE_TOCADOR_S,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            finally:
                try:
                    os.unlink(caminho_wav)
                except OSError:
                    pass
        except Exception as erro:  # sem placa de som, sem permissão, tempo esgotado...
            registrador.warning("Não foi possível tocar o alarme %s: %s", padrao, erro)

    @staticmethod
    def _tocar_windows(padrao: str) -> None:
        """Bipes pelo ``winsound.Beep`` (Windows)."""
        import winsound  # type: ignore[import-not-found]  # só existe no Windows

        quantidade, frequencia, duracao, pausa = BIPES.get(padrao, BIPES["AMARELO"])
        for repeticao in range(REPETICOES_SEQUENCIA):
            for _ in range(quantidade):
                winsound.Beep(int(frequencia), int(duracao * 1000))
                time.sleep(pausa)
            if repeticao < REPETICOES_SEQUENCIA - 1:
                time.sleep(PAUSA_ENTRE_SEQUENCIAS_S)


@dataclass
class EstadoAlarme:
    """Memória do alarme entre os ciclos.

    Attributes:
        ultimo_status: status visto no ciclo anterior (``None`` no início).
        ultimo_toque_utc: instante do último toque.
        toques_janela: toques dentro da janela de 10 minutos que começou em ``inicio_janela_utc``.
        inicio_janela_utc: início da janela de contagem de toques.
        vermelho_pendente: transição para Vermelho que não pôde tocar (silêncio
            ou limite); toca quando puder, se o Vermelho continuar.
        ultima_geada: dia (local) do último aviso de geada tocado (uma vez por noite).
    """

    ultimo_status: Optional[int] = None
    ultimo_toque_utc: Optional[datetime] = None
    toques_janela: int = 0
    inicio_janela_utc: Optional[datetime] = None
    vermelho_pendente: bool = False
    ultima_geada: Optional[date] = None


def _hora_minuto(texto: Any, padrao: str) -> Tuple[int, int]:
    """Converte "HH:MM" em ``(hora, minuto)``; o padrão se inválido."""
    for candidato in (str(texto or "").strip(), padrao):
        try:
            hora, minuto = candidato.split(":", 1)
            valor = (int(hora), int(minuto))
        except ValueError:
            continue
        if 0 <= valor[0] <= 23 and 0 <= valor[1] <= 59:
            return valor
    return 0, 0


def _booleano(valor: Any, padrao: bool) -> bool:
    """Interpreta ``True``/``"true"``/``"1"``/``"sim"`` (e o contrário); ``padrao`` se ausente."""
    if valor is None or valor == "":
        return padrao
    if isinstance(valor, bool):
        return valor
    texto = str(valor).strip().lower()
    if texto in ("true", "1", "sim", "yes", "verdadeiro"):
        return True
    if texto in ("false", "0", "nao", "não", "no", "falso"):
        return False
    return padrao


def em_silencio(agora_local: datetime, config: Dict[str, Any]) -> bool:
    """``True`` dentro do horário de silêncio (que pode atravessar a meia-noite)."""
    inicio = _hora_minuto(config.get("alarme_silencio_inicio"), SILENCIO_INICIO_PADRAO)
    fim = _hora_minuto(config.get("alarme_silencio_fim"), SILENCIO_FIM_PADRAO)
    atual = (agora_local.hour, agora_local.minute)
    if inicio == fim:
        return False
    if inicio < fim:
        return inicio <= atual < fim
    return atual >= inicio or atual < fim


def _local_e_utc(agora: datetime) -> Tuple[datetime, datetime]:
    """``(hora local sem fuso, UTC aware)``; um ``datetime`` sem fuso é hora local."""
    if agora.tzinfo is None:
        return agora, agora.astimezone(timezone.utc)
    return agora.astimezone().replace(tzinfo=None), agora.astimezone(timezone.utc)


def _limite_de_toques_atingido(estado: EstadoAlarme, agora_utc: datetime) -> bool:
    """``True`` se já houve ``MAXIMO_TOQUES_JANELA`` toques na janela de 10 min em curso."""
    if estado.inicio_janela_utc is None or agora_utc - estado.inicio_janela_utc >= JANELA_TOQUES:
        return False
    return estado.toques_janela >= MAXIMO_TOQUES_JANELA


def _registrar_toque(estado: EstadoAlarme, agora_utc: datetime) -> None:
    """Conta o toque na janela de 10 minutos (abre uma janela nova se a anterior venceu)."""
    if estado.inicio_janela_utc is None or agora_utc - estado.inicio_janela_utc >= JANELA_TOQUES:
        estado.inicio_janela_utc = agora_utc
        estado.toques_janela = 0
    estado.toques_janela += 1
    estado.ultimo_toque_utc = agora_utc


def decidir_toque(
    leitura: Optional[Dict[str, Any]],
    analise: Optional[Dict[str, Any]],
    estado: EstadoAlarme,
    agora: datetime,
    config: Dict[str, Any],
    silenciada: bool,
) -> Optional[str]:
    """Decide se o alarme toca agora e qual padrão; ATUALIZA ``estado`` (status visto e toques).

    Args:
        leitura: leitura exibida (``status_alerta``, ``origem``); ``None`` sem leitura.
        analise: análise de antecipação da leitura (``motivo_principal``) ou ``None``.
        estado: memória entre os ciclos (alterada aqui).
        agora: instante atual (sem fuso = hora local; com fuso = convertido).
        config: configurações (``alarme_local_ativo``, ``alarme_silencio_inicio``,
            ``alarme_silencio_fim``); chaves ausentes usam os padrões da Frente 1.
        silenciada: ``True`` se a causa atual teve "Feito" recente.

    Returns:
        ``"VERMELHO"``, ``"GEADA"`` ou ``None`` (não toca).
    """
    agora_local, agora_utc = _local_e_utc(agora)
    if leitura is None or str(leitura.get("origem") or "") not in ORIGENS_QUE_TOCAM:
        # Sem leitura ou leitura de simulação/demonstração/dataset: não toca e não mexe na memória.
        return None
    try:
        status: Optional[int] = int(leitura.get("status_alerta"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        status = None
    if status == STATUS_VERMELHO and estado.ultimo_status != STATUS_VERMELHO:
        estado.vermelho_pendente = True
    if status != STATUS_VERMELHO:
        estado.vermelho_pendente = False
    estado.ultimo_status = status

    motivo = str((analise or {}).get("motivo_principal") or "").strip().upper()
    geada = (motivo == "GEADA_NOITE" and HORA_INICIO_GEADA <= agora_local.hour < HORA_FIM_GEADA
             and estado.ultima_geada != agora_local.date())
    if not (estado.vermelho_pendente or geada):
        return None
    if not _booleano(config.get("alarme_local_ativo"), True) or silenciada:
        # Desligado pelo técnico ou já atendido ("Feito"): esta transição não toca mais.
        estado.vermelho_pendente = False
        if geada:
            estado.ultima_geada = agora_local.date()
        return None
    motivo_regra = str(leitura.get("motivo_regra") or "").strip().upper()
    urgente_de_madrugada = estado.vermelho_pendente and motivo_regra in MOTIVOS_QUE_IGNORAM_SILENCIO
    if (em_silencio(agora_local, config) and not urgente_de_madrugada) \
            or _limite_de_toques_atingido(estado, agora_utc):
        return None  # o Vermelho fica pendente e toca quando puder
    _registrar_toque(estado, agora_utc)
    if estado.vermelho_pendente:
        estado.vermelho_pendente = False
        return PADRAO_VERMELHO
    estado.ultima_geada = agora_local.date()
    return PADRAO_GEADA


# ---------------------------------------------------------------------------
# Serviço
# ---------------------------------------------------------------------------
def _modulo_prognostico() -> Optional[ModuleType]:
    """Módulo ``prognostico`` (opcional); ``None`` se não puder ser importado."""
    try:
        modulo = importlib.import_module("prognostico")
    except Exception as erro:  # ausente ou quebrado: o alarme segue só com o Vermelho
        registrador.debug("Prognóstico indisponível para o alarme: %s", erro)
        return None
    return modulo if callable(getattr(modulo, "obter_analise_antecipacao", None)) else None


def _instante_leitura(leitura: Dict[str, Any]) -> Optional[datetime]:
    """Instante UTC da leitura (``instante_utc`` ou ``data_hora`` local)."""
    for chave, local in (("instante_utc", False), ("data_hora", True)):
        texto = str(leitura.get(chave) or "").strip()
        if not texto:
            continue
        try:
            instante = datetime.fromisoformat(texto)
        except ValueError:
            continue
        if instante.tzinfo is None:
            instante = instante.astimezone() if local else instante.replace(tzinfo=timezone.utc)
        return instante.astimezone(timezone.utc)
    return None


def ler_situacao(caminho_banco: Path, agora_utc: datetime,
                 prognostico: Optional[ModuleType] = None) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]],
                                                                     Dict[str, Any], bool]:
    """Leitura atual da cultura monitorada, a análise de antecipação, as configurações e o silêncio por "Feito".

    Leituras de simulação (dados de teste), de demonstração e velhas (mais de 10 min) viram ``None``.
    """
    config = obter_todas_configuracoes(caminho_banco=caminho_banco)
    cultura = str(config.get("cultura_ativa") or CULTURA_PADRAO)
    recentes = buscar_historico(limite=1, codigo_cultura=cultura, caminho_banco=caminho_banco)
    leitura = recentes[0] if recentes else None
    if leitura is not None:
        instante = _instante_leitura(leitura)
        if str(leitura.get("origem")) not in ORIGENS_QUE_TOCAM or instante is None \
                or agora_utc - instante > IDADE_MAXIMA_LEITURA:
            leitura = None
    analise: Optional[Dict[str, Any]] = None
    silenciada = False
    if leitura is not None:
        if prognostico is not None and leitura.get("origem") == MODO_CAMPO_USB:
            try:
                analise = prognostico.obter_analise_antecipacao(int(leitura["id_leitura"]), caminho_banco)
            except Exception as erro:
                registrador.warning("Antecipação indisponível para o alarme: %s", erro)
        try:
            parametros = buscar_parametros_cultura(cultura, caminho_banco=caminho_banco)
            motivo = str((analise or {}).get("motivo_principal") or "NENHUM")
            causa = derivar_causa(int(leitura["status_alerta"]), leitura, parametros, motivo,
                                  str(leitura.get("estado_sensor") or "OK"),
                                  mensagem_voz=str(leitura.get("mensagem_voz") or ""))
            silenciada = causa_silenciada(caminho_banco, causa, agora_utc)
        except (KeyError, ValueError) as erro:
            registrador.warning("Causa indisponível para o alarme: %s", erro)
    return leitura, analise, config, silenciada


def executar(caminho_banco: Path = CAMINHO_BANCO, intervalo_s: float = INTERVALO_PADRAO_S,
             tocador: Optional[TocadorSom] = None, max_ciclos: Optional[int] = None,
             relogio: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
    """Laço do serviço: a cada ``intervalo_s`` lê a situação, decide e toca.

    Erros de um ciclo (banco travado, prognóstico quebrado) são registrados e o
    laço continua. ``SIGTERM``/``SIGINT`` encerram no fim do ciclo.

    Args:
        caminho_banco: banco SQLite do coletor.
        intervalo_s: segundos entre os ciclos.
        tocador: quem toca (padrão: ``TocadorSistema``).
        max_ciclos: encerra depois desse número de ciclos (``None`` = para sempre).
        relogio: fonte do instante atual em UTC (injetável nos testes).
    """
    tocador_efetivo: TocadorSom = tocador if tocador is not None else TocadorSistema()
    inicializar_banco(caminho_banco)
    estado = EstadoAlarme()
    prognostico = _modulo_prognostico()
    parar = {"pedido": False}
    ultima_limpeza: Optional[datetime] = None

    def pedir_parada(sinal: int, _quadro: Optional[FrameType]) -> None:
        """Marca o pedido de parada (o laço termina no fim do ciclo)."""
        registrador.info("Sinal %d recebido: alarme local encerrando.", sinal)
        parar["pedido"] = True

    anteriores: Dict[int, Any] = {}
    if max_ciclos is None:
        for sinal in (signal.SIGTERM, signal.SIGINT):
            try:
                anteriores[sinal] = signal.signal(sinal, pedir_parada)
            except (ValueError, OSError):  # fora da thread principal
                pass
    ciclos = 0
    registrador.info("Alarme local iniciado (banco %s, a cada %.0f s).", caminho_banco, intervalo_s)
    try:
        while not parar["pedido"]:
            agora = relogio()
            try:
                leitura, analise, config, silenciada = ler_situacao(caminho_banco, agora, prognostico)
                padrao = decidir_toque(leitura, analise, estado, agora, config, silenciada)
                if padrao is not None:
                    tocador_efetivo.tocar(padrao)
                if ultima_limpeza is None or agora - ultima_limpeza >= LIMPEZA_EVENTOS_A_CADA:
                    removidos = limpar_eventos_antigos(caminho_banco, agora_utc=agora)
                    if removidos:
                        registrador.info("%d eventos da interface com mais de 90 dias removidos.", removidos)
                    ultima_limpeza = agora
            except Exception as erro:  # o serviço nunca para por um ciclo ruim
                registrador.warning("Ciclo do alarme local falhou: %s", erro)
            ciclos += 1
            if max_ciclos is not None and ciclos >= max_ciclos:
                break
            time.sleep(max(0.0, float(intervalo_s)))
    finally:
        for sinal, tratador in anteriores.items():
            try:
                signal.signal(sinal, tratador)
            except (ValueError, OSError):
                pass
    registrador.info("Alarme local encerrado depois de %d ciclos.", ciclos)


def _criar_parser() -> argparse.ArgumentParser:
    """Linha de comando do alarme local."""
    parser = argparse.ArgumentParser(prog="alarme_local.py",
                                     description="AgroAnálise — alarme sonoro local da estação base.")
    parser.add_argument("--banco", type=Path, default=None, help="Banco SQLite (padrão: AGRO_BANCO ou data/agroanalise.db).")
    parser.add_argument("--intervalo", type=float, default=INTERVALO_PADRAO_S, help="Segundos entre as verificações.")
    parser.add_argument("--testar-som", action="store_true", help="Toca o padrão VERMELHO uma vez e sai.")
    parser.add_argument("--verboso", action="store_true", help="Registra também mensagens de depuração.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Ponto de entrada; devolve o código de saída (0 = sucesso)."""
    argumentos = _criar_parser().parse_args(argv)
    from registro import configurar_logs  # import tardio: só a linha de comando grava arquivo de log

    configurar_logs("alarme", verboso=argumentos.verboso)
    if argumentos.testar_som:
        TocadorSistema().tocar(PADRAO_VERMELHO)
        return 0
    banco_texto = str(argumentos.banco) if argumentos.banco is not None else os.environ.get("AGRO_BANCO", "").strip()
    caminho = Path(banco_texto).expanduser() if banco_texto else CAMINHO_BANCO
    executar(caminho, intervalo_s=max(1.0, float(argumentos.intervalo)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
