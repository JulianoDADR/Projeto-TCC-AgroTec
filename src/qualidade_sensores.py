"""Qualidade dos sensores de campo: mediana móvel e diagnóstico de sensor solto, travado ou impossível.

No campo, a maior fonte de orientação errada não é a IA: é o HARDWARE. Um
espeto do higrômetro solto lê "0 %" (terra seca) e, sem proteção, a voz diria
"Urgente, regue!" para uma lavoura encharcada. Este módulo reúne as duas
defesas aplicadas pelo coletor ANTES de a leitura virar cor e voz:

* ``FiltroMedianaMovel``: mediana das últimas N leituras saneadas, por
  variável. Tira o "tremido" do sensor perto de uma borda da faixa (o
  semáforo não pisca entre verde e amarelo a cada leitura) sem atrasar um
  perigo real: um valor além do limite CRÍTICO sai na hora, sem mediana.
* ``DiagnosticoSensores``: decide, a cada leitura, se o sensor da terra e o
  do ar estão ``OK``, ``SUSPEITO`` (travado no mesmo valor por tempo demais)
  ou com ``DEFEITO`` (cravado em 0 % ou 100 %, aviso do firmware de
  higrômetro desconectado, DHT11 sem resposta por muitas leituras).

Quando um sensor não é confiável, o coletor NUNCA mostra Vermelho por causa
dele: mostra Amarelo e pede para o agricultor conferir o sensor
(``MENSAGEM_SENSOR_SOLO`` / ``MENSAGEM_SENSOR_AR``).

Este módulo não importa ``coletor_serial`` nem ``app`` (sem dependência circular).
"""

from __future__ import annotations

import logging
import math
import statistics
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Sequence, Tuple

from database import (
    ESTADO_SENSOR_DEFEITO,
    ESTADO_SENSOR_OK,
    ESTADO_SENSOR_SUSPEITO,
    ESTADOS_SENSOR,
    VARIAVEL_TEMPERATURA_AR,
    VARIAVEL_UMIDADE_AR,
    VARIAVEL_UMIDADE_SOLO,
    ParametrosCultura,
)

registrador: logging.Logger = logging.getLogger("agroanalise.qualidade_sensores")

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
MENSAGEM_SENSOR_SOLO: str = ("O sensor da terra parece solto ou quebrado. Confira o espeto na terra. "
                             "Não regue só por causa deste aviso: olhe a terra com a mão.")
MENSAGEM_SENSOR_AR: str = "O sensor do ar está com defeito. A água da terra continua sendo medida."

CODIGO_HIGROMETRO_DESCONECTADO: str = "AVISO:HIGROMETRO_DESCONECTADO"
CODIGO_DHT_LEITURA_INVALIDA: str = "ERRO:DHT11_LEITURA_INVALIDA"
# O firmware v3 pode usar DHT22 (AGRO_SENSOR_AR=AGRO_AR_DHT22): o código de falha traz o nome do sensor.
CODIGOS_DHT_LEITURA_INVALIDA: Tuple[str, ...] = (CODIGO_DHT_LEITURA_INVALIDA, "ERRO:DHT22_LEITURA_INVALIDA")
SEGUNDOS_DEFEITO_HIGROMETRO_DESCONECTADO: float = 120.0

# Variáveis que podem sair sem mediana quando passam do limite crítico.
VARIAVEIS_EXCECAO_CRITICA: Tuple[str, ...] = (VARIAVEL_TEMPERATURA_AR, VARIAVEL_UMIDADE_SOLO)
VARIAVEIS_AR: Tuple[str, ...] = (VARIAVEL_TEMPERATURA_AR, VARIAVEL_UMIDADE_AR)

_ORDEM_ESTADOS: Dict[str, int] = {estado: indice for indice, estado in enumerate(ESTADOS_SENSOR)}


def pior_estado(*estados: str) -> str:
    """Devolve o pior estado entre os informados (ordem ``OK < SUSPEITO < DEFEITO``).

    Estados desconhecidos são tratados como ``DEFEITO`` (precaução).
    """
    if not estados:
        return ESTADO_SENSOR_OK
    return max(estados, key=lambda estado: _ORDEM_ESTADOS.get(estado, len(ESTADOS_SENSOR)))


# ---------------------------------------------------------------------------
# Mediana móvel
# ---------------------------------------------------------------------------
class FiltroMedianaMovel:
    """Mediana móvel por variável, aplicada às leituras JÁ saneadas.

    * O buffer de cada variável guarda as últimas ``janela`` leituras.
    * É zerado quando o contexto ``(cultura, origem)`` muda (outra cultura ou
      outra fonte de dados não se misturam) e quando há uma lacuna maior que
      ``idade_maxima_s`` entre duas leituras (Arduino desligado, coletor
      parado: as leituras antigas já não representam o presente).
    * Exceção de segurança: se a leitura mais recente de temperatura ou de
      umidade do solo estiver além de ``critico_min``/``critico_max`` da
      cultura, ela sai SEM mediana (geada e murcha não podem esperar meia janela).
    """

    def __init__(self, janela: int = 12, idade_maxima_s: float = 90.0) -> None:
        """Configura o filtro.

        Raises:
            ValueError: janela menor que 1 ou idade máxima não positiva.
        """
        if isinstance(janela, bool) or int(janela) < 1:
            raise ValueError(f"A janela da mediana precisa ser >= 1, recebido {janela!r}.")
        if not (math.isfinite(float(idade_maxima_s)) and float(idade_maxima_s) > 0):
            raise ValueError(f"idade_maxima_s precisa ser positiva, recebido {idade_maxima_s!r}.")
        self.janela: int = int(janela)
        self.idade_maxima_s: float = float(idade_maxima_s)
        self._buffers: Dict[str, Deque[float]] = {}
        self._contexto: Optional[Tuple[str, str]] = None
        self._ultimo_instante: Optional[float] = None

    @property
    def contexto(self) -> Optional[Tuple[str, str]]:
        """Contexto ``(cultura, origem)`` das leituras no buffer (``None`` = ainda nenhum)."""
        return self._contexto

    def tamanho_buffer(self, variavel: str) -> int:
        """Quantas leituras da variável estão no buffer."""
        return len(self._buffers.get(variavel, ()))

    def reiniciar(self) -> None:
        """Esvazia todos os buffers e esquece o instante da última leitura (mantém o contexto)."""
        self._buffers.clear()
        self._ultimo_instante = None

    def preencher(self, valores: Dict[str, float]) -> None:
        """Enche o buffer de cada variável informada com ``janela`` cópias do valor.

        Útil para partir de uma referência conhecida (ex.: a última leitura
        real gravada). Valores não finitos são ignorados.
        """
        for variavel, valor in valores.items():
            try:
                numero = float(valor)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(numero):
                continue
            self._buffers[variavel] = deque([numero] * self.janela, maxlen=self.janela)

    def filtrar(
        self,
        valores: Dict[str, float],
        parametros: ParametrosCultura,
        contexto: Tuple[str, str],
        instante_monotonic: float,
    ) -> Dict[str, float]:
        """Aplica a mediana móvel a uma leitura saneada.

        Args:
            valores: leitura saneada ``{variavel: valor}`` (valores finitos).
            parametros: faixas da cultura (para a exceção crítica).
            contexto: ``(codigo_cultura, origem)`` da leitura.
            instante_monotonic: ``time.monotonic()`` da leitura.

        Returns:
            ``{variavel: valor_filtrado}`` com 1 casa decimal, mesmas chaves de ``valores``.
        """
        contexto_atual = (str(contexto[0]), str(contexto[1]))
        instante = float(instante_monotonic)
        if self._contexto is not None and contexto_atual != self._contexto:
            registrador.debug("Mediana móvel reiniciada: contexto %s → %s.", self._contexto, contexto_atual)
            self.reiniciar()
        elif self._ultimo_instante is not None and (instante - self._ultimo_instante > self.idade_maxima_s
                                                    or instante < self._ultimo_instante):
            registrador.debug("Mediana móvel reiniciada: lacuna de %.0f s entre leituras.",
                              instante - self._ultimo_instante)
            self.reiniciar()
        self._contexto = contexto_atual
        self._ultimo_instante = instante

        filtrados: Dict[str, float] = {}
        for variavel, valor in valores.items():
            numero = float(valor)
            buffer = self._buffers.setdefault(variavel, deque(maxlen=self.janela))
            buffer.append(numero)
            if variavel in VARIAVEIS_EXCECAO_CRITICA and self._alem_do_critico(variavel, numero, parametros):
                filtrados[variavel] = round(numero, 1)
                continue
            filtrados[variavel] = round(float(statistics.median(buffer)), 1)
        return filtrados

    @staticmethod
    def _alem_do_critico(variavel: str, valor: float, parametros: ParametrosCultura) -> bool:
        """Indica se o valor passou do limite crítico da cultura (sem faixa cadastrada → ``False``)."""
        try:
            faixa = parametros.faixa(variavel)
        except KeyError:
            return False
        return valor < faixa.critico_min or valor > faixa.critico_max


# ---------------------------------------------------------------------------
# Diagnóstico dos sensores
# ---------------------------------------------------------------------------
@dataclass
class EstadoSensores:
    """Situação dos sensores numa leitura.

    Atributos:
        estado_solo: ``OK``, ``SUSPEITO`` ou ``DEFEITO`` do higrômetro.
        estado_ar: ``OK``, ``SUSPEITO`` ou ``DEFEITO`` do DHT11 (temperatura e umidade do ar).
        descricao: motivos técnicos separados por ``;`` (vazio se tudo OK),
            gravados em ``historico_leituras.diagnostico_sensor``.
    """

    estado_solo: str = ESTADO_SENSOR_OK
    estado_ar: str = ESTADO_SENSOR_OK
    descricao: str = ""

    @property
    def estado_geral(self) -> str:
        """O pior entre o estado do solo e o do ar (``OK < SUSPEITO < DEFEITO``)."""
        return pior_estado(self.estado_solo, self.estado_ar)

    @property
    def solo_confiavel(self) -> bool:
        """``True`` quando o sensor da terra está ``OK``."""
        return self.estado_solo == ESTADO_SENSOR_OK

    @property
    def ar_confiavel(self) -> bool:
        """``True`` quando o sensor do ar está ``OK``."""
        return self.estado_ar == ESTADO_SENSOR_OK


def _arredondar(valor: Optional[float]) -> Optional[float]:
    """Valor bruto com 1 casa decimal; ``None`` para ausente, NaN ou não numérico."""
    if valor is None or isinstance(valor, bool):
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    return round(numero, 1) if math.isfinite(numero) else None


class _MonitorTravamento:
    """Acompanha há quanto tempo um valor (ou tupla de valores) se repete exatamente.

    O tempo coberto vai do início da primeira amostra idêntica ao fim da
    última: ``(instante_atual - instante_da_primeira) + intervalo_entre_amostras``.
    Assim, 720 leituras idênticas a cada 5 s cobrem exatamente 60 minutos.
    """

    def __init__(self) -> None:
        """Começa sem nenhum valor observado."""
        self._valor: Optional[Tuple[float, ...]] = None
        self._desde: float = 0.0
        self._instante_anterior: Optional[float] = None
        self._intervalo: float = 0.0

    def observar(self, valor: Tuple[float, ...], instante: float) -> float:
        """Registra a amostra e devolve, em segundos, há quanto tempo o valor está parado."""
        if self._instante_anterior is not None and instante > self._instante_anterior:
            self._intervalo = instante - self._instante_anterior
        if self._instante_anterior is not None and instante < self._instante_anterior:
            self._valor = None  # relógio monotônico não volta: novo processo/teste; recomeça
        self._instante_anterior = instante
        if valor != self._valor:
            self._valor = valor
            self._desde = instante
            return self._intervalo if self._intervalo > 0 else 0.0
        return (instante - self._desde) + self._intervalo

    def reiniciar(self) -> None:
        """Esquece o valor acompanhado."""
        self._valor = None
        self._instante_anterior = None
        self._intervalo = 0.0


class DiagnosticoSensores:
    """Classifica a saúde do higrômetro e do DHT11 a partir das leituras BRUTAS e dos avisos do firmware.

    Regras (padrões configuráveis no banco):
        * solo bruto idêntico (1 casa decimal) por ``minutos_travamento_solo``
          (60 min) → solo ``SUSPEITO`` (sensor travado ou fio partido);
        * temperatura e umidade do ar idênticas por ``horas_travamento_ar``
          (3 h) → ar ``SUSPEITO``;
        * solo exatamente 0,0 ou 100,0 por ``leituras_saturacao`` (3) leituras
          seguidas → solo ``DEFEITO`` (espeto fora da terra ou em curto);
        * temperatura ausente (``nan``) por mais de ``limite_hold_last`` (30)
          leituras seguidas → ar ``DEFEITO``; o mesmo vale para o solo;
        * aviso ``#AVISO:HIGROMETRO_DESCONECTADO`` do firmware → solo
          ``DEFEITO`` pelos 120 s seguintes;
        * variável que o saneador está segurando no último válido (*hold-last*)
          há mais de ``limite_hold_last`` leituras → ``DEFEITO``.
    """

    def __init__(
        self,
        minutos_travamento_solo: float = 60,
        horas_travamento_ar: float = 3,
        leituras_saturacao: int = 3,
        limite_hold_last: int = 30,
    ) -> None:
        """Configura os limites do diagnóstico.

        Raises:
            ValueError: algum limite não positivo.
        """
        for nome, valor in (("minutos_travamento_solo", minutos_travamento_solo),
                            ("horas_travamento_ar", horas_travamento_ar),
                            ("leituras_saturacao", leituras_saturacao),
                            ("limite_hold_last", limite_hold_last)):
            if isinstance(valor, bool) or not math.isfinite(float(valor)) or float(valor) <= 0:
                raise ValueError(f"{nome} precisa ser positivo, recebido {valor!r}.")
        self.segundos_travamento_solo: float = float(minutos_travamento_solo) * 60.0
        self.segundos_travamento_ar: float = float(horas_travamento_ar) * 3600.0
        self.leituras_saturacao: int = int(leituras_saturacao)
        self.limite_hold_last: int = int(limite_hold_last)

        self.contagem_diagnosticos: Dict[str, int] = {}
        self.leituras_dht_invalidas: int = 0
        self._solo_defeito_ate: Optional[float] = None
        self._travamento_solo = _MonitorTravamento()
        self._travamento_ar = _MonitorTravamento()
        self._saturacao_seguidas: int = 0
        self._temperatura_ausente_seguidas: int = 0
        self._solo_ausente_seguidas: int = 0
        self.ultimo_estado: EstadoSensores = EstadoSensores()

    def reiniciar(self) -> None:
        """Esquece o histórico (ex.: troca de cultura ou de sensor); mantém as contagens de diagnóstico."""
        self._solo_defeito_ate = None
        self._travamento_solo.reiniciar()
        self._travamento_ar.reiniciar()
        self._saturacao_seguidas = 0
        self._temperatura_ausente_seguidas = 0
        self._solo_ausente_seguidas = 0
        self.ultimo_estado = EstadoSensores()

    def registrar_diagnostico(self, codigo: str, instante_monotonic: float) -> None:
        """Registra uma linha de diagnóstico do firmware (com ou sem ``#`` inicial).

        ``AVISO:HIGROMETRO_DESCONECTADO`` deixa o solo em ``DEFEITO`` por 120 s;
        ``ERRO:DHT11_LEITURA_INVALIDA`` (ou ``DHT22``) é contado. Todo código é contado em
        ``contagem_diagnosticos``.
        """
        texto = str(codigo or "").strip().lstrip("#").strip().upper()
        if not texto:
            return
        self.contagem_diagnosticos[texto] = self.contagem_diagnosticos.get(texto, 0) + 1
        if texto.startswith(CODIGO_HIGROMETRO_DESCONECTADO):
            self._solo_defeito_ate = float(instante_monotonic) + SEGUNDOS_DEFEITO_HIGROMETRO_DESCONECTADO
        elif texto.startswith(CODIGOS_DHT_LEITURA_INVALIDA):
            self.leituras_dht_invalidas += 1

    def avaliar(
        self,
        brutos: Dict[str, Optional[float]],
        instante_monotonic: float,
        hold_last_excedido: Sequence[str] = (),
    ) -> EstadoSensores:
        """Avalia uma leitura BRUTA (antes do saneamento).

        Args:
            brutos: ``{variavel: valor bruto ou None}`` (chaves ausentes = ``None``).
            instante_monotonic: ``time.monotonic()`` da leitura.
            hold_last_excedido: variáveis que o saneador segura no último válido
                há mais leituras que o limite (``SaneadorTempoReal.variaveis_hold_last_excedido``).

        Returns:
            O ``EstadoSensores`` desta leitura (também guardado em ``ultimo_estado``).
        """
        instante = float(instante_monotonic)
        temperatura = _arredondar(brutos.get(VARIAVEL_TEMPERATURA_AR))
        umidade_ar = _arredondar(brutos.get(VARIAVEL_UMIDADE_AR))
        solo = _arredondar(brutos.get(VARIAVEL_UMIDADE_SOLO))
        motivos_solo: Dict[str, str] = {}
        motivos_ar: Dict[str, str] = {}

        # --- Solo -----------------------------------------------------------
        if solo is None:
            self._solo_ausente_seguidas += 1
            self._saturacao_seguidas = 0
        else:
            self._solo_ausente_seguidas = 0
            parado = self._travamento_solo.observar((solo,), instante)
            if parado >= self.segundos_travamento_solo:
                motivos_solo[ESTADO_SENSOR_SUSPEITO] = f"solo:travado_{int(parado // 60)}min"
            self._saturacao_seguidas = self._saturacao_seguidas + 1 if solo in (0.0, 100.0) else 0
        if self._saturacao_seguidas >= self.leituras_saturacao:
            motivos_solo[ESTADO_SENSOR_DEFEITO] = f"solo:saturado_{self._saturacao_seguidas}x"
        if self._solo_ausente_seguidas > self.limite_hold_last:
            motivos_solo[ESTADO_SENSOR_DEFEITO] = f"solo:sem_leitura_{self._solo_ausente_seguidas}x"
        if self._solo_defeito_ate is not None:
            if instante < self._solo_defeito_ate:
                motivos_solo[ESTADO_SENSOR_DEFEITO] = "solo:higrometro_desconectado"
            else:
                self._solo_defeito_ate = None
        if VARIAVEL_UMIDADE_SOLO in hold_last_excedido and ESTADO_SENSOR_DEFEITO not in motivos_solo:
            motivos_solo[ESTADO_SENSOR_DEFEITO] = "solo:hold_last_excedido"

        # --- Ar -------------------------------------------------------------
        if temperatura is None:
            self._temperatura_ausente_seguidas += 1
        else:
            self._temperatura_ausente_seguidas = 0
        if temperatura is not None and umidade_ar is not None:
            parado_ar = self._travamento_ar.observar((temperatura, umidade_ar), instante)
            if parado_ar >= self.segundos_travamento_ar:
                motivos_ar[ESTADO_SENSOR_SUSPEITO] = f"ar:travado_{parado_ar / 3600.0:.1f}h"
        if self._temperatura_ausente_seguidas > self.limite_hold_last:
            motivos_ar[ESTADO_SENSOR_DEFEITO] = f"ar:sem_leitura_{self._temperatura_ausente_seguidas}x"
        elif any(variavel in hold_last_excedido for variavel in VARIAVEIS_AR):
            motivos_ar[ESTADO_SENSOR_DEFEITO] = "ar:hold_last_excedido"

        estado_solo = pior_estado(ESTADO_SENSOR_OK, *motivos_solo.keys())
        estado_ar = pior_estado(ESTADO_SENSOR_OK, *motivos_ar.keys())
        descricao = ";".join(list(motivos_solo.values()) + list(motivos_ar.values()))
        estado = EstadoSensores(estado_solo=estado_solo, estado_ar=estado_ar, descricao=descricao)
        if estado.estado_geral != self.ultimo_estado.estado_geral:
            nivel = logging.INFO if estado.estado_geral == ESTADO_SENSOR_OK else logging.WARNING
            registrador.log(nivel, "Sensores: solo %s, ar %s (%s).", estado_solo, estado_ar, descricao or "sem alerta")
        self.ultimo_estado = estado
        return estado
