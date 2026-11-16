"""Módulo 5 — Motor de Coleta 24/7 do AgroAnálise.

Fluxo de cada ciclo do coletor:

    Arduino (USB/serial) ──► interpretar linha ──► SaneadorTempoReal
         │ (falhou?)                                   │ (NaN, faixa física, salto)
         ▼                                             ▼
    SimuladorTempoReal ──────────────────────► calcular_features
    (Modo Simulação)                                   │
                                                       ▼
    Cenário extremo ─────────────────────────► motor de regras + modelo .pkl
    (Modo Demonstração)                                │
                                                       ▼
                                   status final = max(regra, IA) + mensagem falada
                                                       │
                                                       ▼
                                            historico_leituras (SQLite)

Máquina de estados (modos operantes):
    * ``CAMPO_USB``: lê o Arduino. Se a porta falhar (``SerialException``/``OSError``)
      ou se ficar ``LIMITE_CICLOS_SEM_DADOS`` ciclos sem nenhuma linha de dados
      válida, fecha a porta e passa para ``SIMULACAO``.
    * ``SIMULACAO``: gera leituras sintéticas em tempo real e, a cada
      ``intervalo_reconexao_segundos``, tenta abrir a porta de novo; se
      conseguir, volta para ``CAMPO_USB``.
    * ``DEMONSTRACAO``: ativado pelo aplicativo (``modo_operacao=DEMONSTRACAO`` e
      ``demonstracao_expira_em`` no futuro). Grava leituras do cenário extremo
      escolhido e mantém a serial aberta, só drenando o buffer. Ao expirar (ou
      quando o aplicativo encerra) volta para ``CAMPO_USB`` se o Arduino
      estiver conectado, senão para ``SIMULACAO``.

Também em tempo real: troca da cultura ativa (``cultura_ativa`` na tabela
``configuracao_sistema``) e recarga do modelo ``.pkl`` quando o arquivo muda
(data de modificação). Sem modelo, o coletor usa só o motor de regras
(``versao_modelo = "REGRAS"``).

Confiabilidade 24/7 do campo:
    * protocolo serial v2 (``$2,t,ur,solo*XX`` com checksum XOR) e v1 (CSV
      puro), contadores de protocolo e descarte da 1ª linha de dados depois de
      abrir a porta (linha cortada pelo reset da placa);
    * saneador do campo semeado pela última leitura real (< 10 min), com
      plausibilidade física da queda de umidade do solo;
    * mediana móvel antes da regra e diagnóstico de sensor solto, travado ou
      fisicamente impossível (``qualidade_sensores``): sensor suspeito nunca
      gera Vermelho, gera Amarelo com o pedido de conferir o sensor;
    * gravação sem perda (fila em memória quando o banco trava), batimento do
      coletor, vigia do disco e do relógio, retenção horária do histórico;
    * gancho opcional de antecipação de estresse (módulo ``prognostico``).

Regra fisiológica (v3, ``regras_agronomicas``): com
``regra_temperatura='fisiologica'`` (semeada pelo coletor ao iniciar), a faixa
de temperatura do cliente vale sobre a média de 24 h (no máximo Amarelo), a
geada e o calor são limites fisiológicos instantâneos e o status gravado tem
banda morta (``MotorRegrasCampo``). Demonstração e dataset seguem na regra
instantânea. O perfil do sensor de ar (DHT11/DHT22/SHT3x) vem do banner do
firmware, e a calibração da sonda de solo (módulo opcional ``calibracao_sonda``)
é aplicada no host antes do saneador.

Execução: ``python src/coletor_serial.py [--porta /dev/ttyUSB0] [--simular]
[--intervalo 5] [--max-iteracoes N] [--cultura cafe_conilon] [--banco caminho.db]``.
Ctrl+C (ou SIGTERM) encerra de forma limpa.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import logging
import math
import os
import shutil
import signal
import sqlite3
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType, ModuleType
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

import joblib
import pandas as pd
import serial
from serial.tools import list_ports

from database import (
    CAMINHO_BANCO,
    CAMINHO_MODELO,
    CULTURA_PADRAO,
    ESTADO_SENSOR_OK,
    MODO_CAMPO_USB,
    MODO_DEMONSTRACAO,
    MODO_SIMULACAO,
    NOMES_STATUS,
    ORIGENS_LEITURA,
    VARIAVEL_TEMPERATURA_AR,
    VARIAVEL_UMIDADE_AR,
    VARIAVEL_UMIDADE_SOLO,
    ParametrosCultura,
    RegistroLeitura,
    aplicar_retencao,
    buscar_parametros_cultura,
    buscar_ultima_leitura_origem,
    definir_configuracao,
    definir_configuracao_condicional,
    definir_configuracoes,
    encerrar_demonstracao_se_inalterada,
    inicializar_banco,
    inserir_leitura,
    inserir_leituras_em_lote,
    obter_configuracao_tipada,
    obter_todas_configuracoes,
    semear_configuracoes_confiabilidade,
    semear_configuracoes_v3,
)
from gerador_dataset import (
    CENARIOS_DEMONSTRACAO,
    COLUNAS_SENSORES,
    FORMATO_DATA_HORA,
    LIMITES_FISICOS_SENSORES,
    LIMITES_SALTO,
    SimuladorTempoReal,
    calcular_features,
    calcular_status_regra,
    classificar_variavel,
    gerar_leitura_cenario,
    gerar_mensagem_fisiologica,
    gerar_mensagem_orientacao,
)
from registro import configurar_logs
from regras_agronomicas import (
    CHAVES_CONFIGURACAO_TERMICA,
    MODO_FISIOLOGICO,
    MODO_INSTANTANEO,
    PERFIS_SENSOR_AR,
    RESUMO_VAZIO,
    SENSOR_AR_PADRAO,
    AvaliacaoRegra,
    ConfiguracaoTermica,
    ResumoTermico,
    avaliar_status_campo,
    buscar_resumo_termico,
    carregar_configuracao_termica,
    configuracao_termica_de_dicionario,
    limites_fisicos_sensor_ar,
    normalizar_modelo_sensor_ar,
    restringir_avaliacao,
)
from qualidade_sensores import (
    MENSAGEM_SENSOR_AR,
    MENSAGEM_SENSOR_SOLO,
    DiagnosticoSensores,
    EstadoSensores,
    FiltroMedianaMovel,
)

# ---------------------------------------------------------------------------
# Constantes do coletor
# ---------------------------------------------------------------------------
registrador: logging.Logger = logging.getLogger("agroanalise.coletor_serial")

FORMATO_LOG: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
FORMATO_DATA_LOG: str = "%Y-%m-%d %H:%M:%S"

BAUD_RATE_PADRAO: int = 9600
ESPERA_RESET_ARDUINO_SEGUNDOS: float = 2.0      # abrir a porta reinicia o Arduino (sinal DTR)
TIMEOUT_PORTA_SEGUNDOS: float = 0.5             # timeout de cada read() feito pelo coletor
TIMEOUT_LEITURA_SEGUNDOS: float = 3.0           # espera máxima por uma linha de dados por ciclo
TAMANHO_MAXIMO_LINHA: int = 256                 # bytes sem "\n" acima disso = lixo elétrico
LIMITE_CICLOS_SEM_DADOS: int = 3                # "timeout repetido" → Modo Simulação
INTERVALO_COLETA_PADRAO: float = 5.0
INTERVALO_RECONEXAO_PADRAO: float = 15.0
INTERVALO_MINIMO_SEGUNDOS: float = 0.2
TENTATIVAS_POR_VERSAO_MODELO: int = 3           # releituras de um .pkl que falhou (pode estar sendo gravado)

ESTADO_CONECTADO: str = "CONECTADO"
ESTADO_DESCONECTADO: str = "DESCONECTADO"
VERSAO_SEM_MODELO: str = "REGRAS"

# Fabricantes de conversores USB-serial usados em placas Arduino e clones.
VIDS_ARDUINO: Dict[int, str] = {
    0x2341: "Arduino",
    0x2A03: "Arduino (arduino.org)",
    0x1A86: "CH340 (clone)",
    0x0403: "FTDI",
    0x10C4: "Silicon Labs CP210x",
}
PADROES_NOME_PORTA: Tuple[str, ...] = ("ttyUSB", "ttyACM", "COM", "usbmodem", "usbserial", "wchusbserial")

SENTINELAS_NAN: Tuple[str, ...] = ("nan", "-nan", "+nan", "")

# Protocolo serial
VERSAO_PROTOCOLO_V1: int = 1                    # "t,ur,solo" (sem checksum)
VERSAO_PROTOCOLO_V2: int = 2                    # "$2,t,ur,solo*XX" (XOR NMEA)
TIPO_LINHA_DADOS: str = "DADOS"
TIPO_LINHA_DIAGNOSTICO: str = "DIAGNOSTICO"
TIPO_LINHA_INVALIDA: str = "INVALIDA"
CODIGO_BANNER_PRONTO: str = "AGROANALISE_PRONTO"

# Confiabilidade 24/7
IDADE_MAXIMA_SEMENTE_SEGUNDOS: float = 600.0    # semeia o saneador com leitura real de até 10 min
LIMITE_HOLD_LAST_CAMPO: int = 30                # leituras seguras no último válido antes de DEFEITO
TAMANHO_MAXIMO_FILA: int = 720                  # leituras em memória com o banco travado (1 h a 5 s)
LIMITE_SALTO_RELOGIO_SEGUNDOS: float = 60.0     # Δ relógio de parede − Δ monotônico acima disso = salto
INTERVALO_RETENCAO_SEGUNDOS: float = 3600.0     # retenção do histórico: 1 vez por hora
ATRASO_PRIMEIRA_RETENCAO_SEGUNDOS: float = 300.0  # 1ª retenção 5 min depois de iniciar
INTERVALO_RETENCAO_EMERGENCIA_SEGUNDOS: float = 600.0  # disco cheio: no máximo a cada 10 min
DIAS_RETENCAO_EMERGENCIA: int = 7
ESPACO_MINIMO_PADRAO_MB: float = 200.0
INTERVALO_RESUMO_SEGUNDOS: float = 300.0        # resumo INFO no log a cada 5 min
NOME_MODULO_ANTECIPACAO: str = "prognostico"
NOME_MODULO_CALIBRACAO: str = "calibracao_sonda"   # Frente 3: calibração da sonda de solo por sonda
SEGUNDOS_LEMBRAR_FALHA_CALIBRACAO: float = 60.0  # módulo ausente: nova tentativa de import a cada 60 s
ID_NO_PRINCIPAL: str = "principal"
CODIGO_PERFIL_SENSORES: str = "AGROANALISE_PERFIL"  # 2ª linha do banner do firmware v3 (AR=, SOLO=, PULSO=)
IDADE_MAXIMA_SEMENTE_BANDA_SEGUNDOS: float = 3600.0  # banda morta parte da última leitura gravada de até 1 h
# Origens em que vale a regra fisiológica; DEMONSTRACAO e DATASET são cenários
# prontos para a regra instantânea (valores didáticos da banca).
ORIGENS_REGRA_FISIOLOGICA: Tuple[str, ...] = (MODO_CAMPO_USB, MODO_SIMULACAO)

# Módulos de antecipação já avisados como ausentes/incompletos (log INFO uma vez).
_MODULOS_ANTECIPACAO_AVISADOS: Set[str] = set()

# (versão do modelo, cultura) já avisadas no log como fora do treino (evita repetir o aviso a cada leitura).
_CULTURAS_FORA_DO_MODELO_AVISADAS: Set[Tuple[str, str]] = set()
# Módulo de calibração que falhou ao carregar: nome → instante (time.monotonic) da falha.
_FALHAS_MODULO_CALIBRACAO: Dict[str, float] = {}
# Erros da calibração já avisados no log (um aviso por tipo de erro, sem inundar o log a cada 5 s).
_AVISOS_CALIBRACAO: Set[str] = set()


# ---------------------------------------------------------------------------
# Comunicação serial
# ---------------------------------------------------------------------------
def _pontuar_porta(porta: Any) -> int:
    """Dá uma nota à porta listada: 2 = fabricante conhecido, 1 = nome típico, 0 = descartar."""
    vid = getattr(porta, "vid", None)
    if vid is not None and int(vid) in VIDS_ARDUINO:
        return 2
    dispositivo = str(getattr(porta, "device", "") or "")
    nome = dispositivo.rsplit("/", 1)[-1]
    if any(nome.startswith(padrao) or padrao in nome for padrao in PADROES_NOME_PORTA):
        return 1
    return 0


def localizar_porta_arduino(porta_configurada: str = "AUTO") -> Optional[str]:
    """Descobre em qual porta serial o Arduino está ligado.

    Args:
        porta_configurada: nome da porta (``/dev/ttyUSB0``, ``COM3``...) ou
            ``"AUTO"`` para autodetecção via ``serial.tools.list_ports``.

    Returns:
        A porta configurada (se não for AUTO) ou a melhor candidata encontrada:
        primeiro as de fabricante conhecido (Arduino, CH340, FTDI, CP210x),
        depois as de nome típico (``ttyUSB``, ``ttyACM``, ``COM``...). ``None``
        se nenhuma for encontrada.
    """
    texto = str(porta_configurada or "").strip()
    if texto and texto.upper() != "AUTO":
        return texto
    try:
        portas = list(list_ports.comports())
    except Exception as erro:  # noqa: BLE001 — listagem depende do sistema operacional
        registrador.warning("Não foi possível listar as portas seriais: %s", erro)
        return None
    candidatas: List[Tuple[int, str, str]] = []
    for porta in portas:
        nota = _pontuar_porta(porta)
        if nota > 0:
            descricao = str(getattr(porta, "description", "") or "")
            candidatas.append((nota, str(porta.device), descricao))
    if not candidatas:
        registrador.debug("Autodetecção: nenhuma porta com cara de Arduino entre %d porta(s).", len(portas))
        return None
    candidatas.sort(key=lambda item: (-item[0], item[1]))
    nota, dispositivo, descricao = candidatas[0]
    registrador.info("Autodetecção: porta %s escolhida (%s).", dispositivo, descricao or "sem descrição")
    return dispositivo


def abrir_conexao_serial(porta: str, baud_rate: int = BAUD_RATE_PADRAO, timeout: float = 2.0) -> serial.Serial:
    """Abre a porta serial e espera o Arduino reiniciar.

    Abrir a porta aciona o sinal DTR, que reinicia a placa; durante
    ``ESPERA_RESET_ARDUINO_SEGUNDOS`` o firmware ainda está no ``setup``.

    Args:
        porta: dispositivo da porta.
        baud_rate: velocidade (igual ao firmware: 9600).
        timeout: tempo máximo de cada ``read`` em segundos.

    Raises:
        serial.SerialException: porta inexistente, ocupada ou sem permissão.
        ValueError: parâmetros inválidos (baud rate, timeout).
    """
    if timeout <= 0:
        raise ValueError(f"O timeout da porta precisa ser positivo, recebido {timeout}.")
    conexao = serial.Serial()
    conexao.port = porta
    conexao.baudrate = int(baud_rate)
    conexao.timeout = float(timeout)
    conexao.write_timeout = 1.0
    conexao.open()
    registrador.info("Porta %s aberta a %d bps; aguardando o Arduino reiniciar (%.1f s).",
                     porta, int(baud_rate), ESPERA_RESET_ARDUINO_SEGUNDOS)
    time.sleep(ESPERA_RESET_ARDUINO_SEGUNDOS)
    return conexao


ValoresSensores = Tuple[Optional[float], Optional[float], Optional[float]]


@dataclass(frozen=True)
class LinhaSerial:
    """Uma linha recebida do firmware, já interpretada.

    Atributos:
        tipo: ``DADOS``, ``DIAGNOSTICO`` (começa com ``#``) ou ``INVALIDA``.
        valores: ``(temperatura_ar, umidade_ar, umidade_solo)`` para ``DADOS``
            (``None`` no campo que veio ``nan``); ``None`` nos demais tipos.
        versao_protocolo: 1 (CSV puro), 2 (``$2,...*XX``) ou 0 (diagnóstico/indefinida).
        checksum_valido: ``True``/``False`` para linhas v2; ``None`` sem checksum.
        codigo_diagnostico: texto após ``#`` (ex.: ``AVISO:HIGROMETRO_DESCONECTADO``).
    """

    tipo: str
    valores: Optional[ValoresSensores] = None
    versao_protocolo: int = 0
    checksum_valido: Optional[bool] = None
    codigo_diagnostico: str = ""


@dataclass
class ContadoresProtocolo:
    """Contadores do protocolo serial (gravados no batimento do coletor)."""

    linhas_v1: int = 0
    linhas_v2: int = 0
    checksum_invalido: int = 0
    descartadas: int = 0

    def contabilizar(self, linha: LinhaSerial) -> None:
        """Conta uma linha interpretada (dados v1/v2 ou checksum inválido)."""
        if linha.tipo == TIPO_LINHA_DADOS:
            if linha.versao_protocolo >= VERSAO_PROTOCOLO_V2:
                self.linhas_v2 += 1
            else:
                self.linhas_v1 += 1
        elif linha.tipo == TIPO_LINHA_INVALIDA and linha.checksum_valido is False:
            self.checksum_invalido += 1

    def como_configuracoes(self) -> Dict[str, int]:
        """Chaves ``serial_*`` de ``configuracao_sistema`` com os valores atuais."""
        return {
            "serial_linhas_v1": int(self.linhas_v1),
            "serial_linhas_v2": int(self.linhas_v2),
            "serial_checksum_invalido": int(self.checksum_invalido),
            "serial_linhas_descartadas": int(self.descartadas),
        }


def calcular_checksum_nmea(conteudo: str) -> str:
    """XOR de todos os bytes de ``conteudo`` em hexadecimal maiúsculo com 2 dígitos (estilo NMEA).

    ``conteudo`` é o trecho ENTRE ``$`` e ``*`` (ex.: ``"2,21.0,65.0,72.4"``).
    """
    soma = 0
    for byte in str(conteudo).encode("utf-8", errors="replace"):
        soma ^= byte
    return f"{soma:02X}"


def _converter_campo(texto: str) -> Optional[float]:
    """Converte um campo da linha CSV: ``nan``/vazio/infinito → ``None``.

    Raises:
        ValueError: campo que não é número nem marcador de ausência.
    """
    campo = texto.strip()
    if campo.lower() in SENTINELAS_NAN:
        return None
    numero = float(campo)
    return numero if math.isfinite(numero) else None


def _converter_valores(partes: List[str]) -> Optional[ValoresSensores]:
    """Converte os 3 campos de sensor; ``None`` se algum não for número nem ``nan``."""
    if len(partes) != 3:
        return None
    try:
        return _converter_campo(partes[0]), _converter_campo(partes[1]), _converter_campo(partes[2])
    except ValueError:
        return None


def _registrar_diagnostico_no_log(mensagem: str) -> None:
    """Leva a linha de diagnóstico do firmware ao log com o nível adequado."""
    if mensagem.upper().startswith("ERRO"):
        registrador.warning("Arduino informou erro: %s", mensagem)
    elif mensagem.upper().startswith("AVISO"):
        registrador.warning("Arduino informou aviso: %s", mensagem)
    else:
        registrador.info("Arduino: %s", mensagem)


def interpretar_linha_serial_detalhada(linha: str) -> LinhaSerial:
    """Interpreta uma linha do firmware em qualquer versão do protocolo.

    * v2: ``$2,<t>,<ur>,<solo>*XX`` — ``XX`` é o XOR dos bytes entre ``$`` e
      ``*`` (``calcular_checksum_nmea``). Checksum errado ou ausente → ``INVALIDA``.
    * v1: ``<t>,<ur>,<solo>`` (sem checksum), aceito por compatibilidade.
    * Diagnóstico: começa com ``#`` (registrado no log).
    * ``nan`` (ou infinito) num campo vira ``None`` naquele campo.
    """
    texto = str(linha or "").replace("\x00", "").strip()
    if not texto:
        return LinhaSerial(tipo=TIPO_LINHA_INVALIDA)
    if texto.startswith("#"):
        mensagem = texto[1:].strip()
        _registrar_diagnostico_no_log(mensagem)
        return LinhaSerial(tipo=TIPO_LINHA_DIAGNOSTICO, codigo_diagnostico=mensagem)
    if texto.startswith("$"):
        corpo = texto[1:]
        if "*" not in corpo:
            registrador.debug("Linha v2 sem checksum ignorada: %r", texto)
            return LinhaSerial(tipo=TIPO_LINHA_INVALIDA, versao_protocolo=VERSAO_PROTOCOLO_V2)
        conteudo, _, recebido = corpo.rpartition("*")
        if calcular_checksum_nmea(conteudo) != recebido.strip().upper():
            registrador.debug("Linha serial com checksum inválido descartada: %r", texto)
            return LinhaSerial(tipo=TIPO_LINHA_INVALIDA, versao_protocolo=VERSAO_PROTOCOLO_V2, checksum_valido=False)
        partes = conteudo.split(",")
        versao_texto = partes[0].strip() if partes else ""
        versao = int(versao_texto) if versao_texto.isdigit() else 0
        valores = _converter_valores(partes[1:]) if versao == VERSAO_PROTOCOLO_V2 else None
        if valores is None:
            registrador.debug("Linha v2 com versão ou campos inesperados ignorada: %r", texto)
            return LinhaSerial(tipo=TIPO_LINHA_INVALIDA, versao_protocolo=versao, checksum_valido=True)
        return LinhaSerial(tipo=TIPO_LINHA_DADOS, valores=valores, versao_protocolo=VERSAO_PROTOCOLO_V2,
                           checksum_valido=True)
    partes = texto.split(",")
    if len(partes) != 3:
        registrador.debug("Linha serial malformada ignorada (esperados 3 campos): %r", texto)
        return LinhaSerial(tipo=TIPO_LINHA_INVALIDA)
    valores = _converter_valores(partes)
    if valores is None:
        registrador.debug("Linha serial com campo não numérico ignorada: %r", texto)
        return LinhaSerial(tipo=TIPO_LINHA_INVALIDA, versao_protocolo=VERSAO_PROTOCOLO_V1)
    return LinhaSerial(tipo=TIPO_LINHA_DADOS, valores=valores, versao_protocolo=VERSAO_PROTOCOLO_V1)


def interpretar_linha_serial(
    linha: str, contadores: Optional[ContadoresProtocolo] = None
) -> Optional[ValoresSensores]:
    """Interpreta uma linha enviada pelo firmware (protocolos v1 e v2).

    * Dado v1: ``temperatura_ar,umidade_ar,umidade_solo`` (ex.: ``21.0,65.0,72.4``);
    * Dado v2: ``$2,21.0,65.0,72.4*XX`` — devolve ``None`` se o checksum não bater;
    * ``nan`` (ou infinito) em um campo vira ``None`` naquele campo — o
      saneador trata a ausência;
    * Diagnóstico (começa com ``#``): registrado em log e ignorado;
    * Linha vazia ou malformada: ignorada.

    Args:
        linha: texto recebido.
        contadores: se informado, a linha é contabilizada (v1, v2, checksum inválido).

    Returns:
        A tupla de 3 valores (cada um ``float`` ou ``None``) ou ``None`` para
        diagnóstico/malformada/checksum inválido.
    """
    interpretada = interpretar_linha_serial_detalhada(linha)
    if contadores is not None:
        contadores.contabilizar(interpretada)
    return interpretada.valores if interpretada.tipo == TIPO_LINHA_DADOS else None


def ler_linhas_serial(
    conexao: serial.Serial,
    timeout_total: float = TIMEOUT_LEITURA_SEGUNDOS,
    ao_receber: Optional[Callable[[LinhaSerial], bool]] = None,
) -> List[LinhaSerial]:
    """Drena o buffer da serial e devolve TODAS as linhas recebidas, já interpretadas.

    Lê tudo o que já chegou. Se ainda não houver nenhuma linha de dados
    aceita, espera até ``timeout_total`` segundos (o tempo pode passar do
    limite em até um ``timeout`` da porta). Sempre faz ao menos uma leitura,
    o que também serve para detectar desconexão.

    Args:
        conexao: porta aberta.
        timeout_total: espera máxima por uma linha de dados aceita.
        ao_receber: chamada para cada linha não vazia, na ordem de chegada;
            devolve ``True`` se a linha é um dado aproveitável. Padrão: toda
            linha ``DADOS`` é aproveitável.

    Raises:
        serial.SerialException: porta fechada ou Arduino desconectado.
    """
    if conexao is None or not conexao.is_open:
        raise serial.SerialException("A porta serial não está aberta.")
    aceitar: Callable[[LinhaSerial], bool] = ao_receber or (lambda item: item.tipo == TIPO_LINHA_DADOS)
    prazo = time.monotonic() + max(0.0, float(timeout_total))
    buffer = bytearray()
    recebidas: List[LinhaSerial] = []
    tem_dado = False
    primeira_leitura = True
    try:
        while True:
            pendentes = int(conexao.in_waiting)
            if not primeira_leitura:
                if tem_dado and not buffer and pendentes == 0:
                    return recebidas
                if time.monotonic() >= prazo:
                    if buffer:
                        registrador.debug("Linha serial incompleta descartada: %r", bytes(buffer))
                    return recebidas
            primeira_leitura = False
            bloco = conexao.read(max(1, pendentes))
            if not bloco:
                continue
            buffer.extend(bloco)
            while b"\n" in buffer:
                linha, _, resto = bytes(buffer).partition(b"\n")
                buffer = bytearray(resto)
                texto = linha.decode("utf-8", errors="replace")
                if not texto.replace("\x00", "").strip():
                    continue
                interpretada = interpretar_linha_serial_detalhada(texto)
                recebidas.append(interpretada)
                if aceitar(interpretada):
                    tem_dado = True
            if len(buffer) > TAMANHO_MAXIMO_LINHA:
                registrador.warning("Descartados %d bytes sem fim de linha (ruído na serial).", len(buffer))
                buffer.clear()
    except serial.SerialException:
        raise
    except OSError as erro:
        raise serial.SerialException(f"Falha de E/S na porta serial: {erro}") from erro


def ler_linha_mais_recente(
    conexao: serial.Serial, timeout_total: float = TIMEOUT_LEITURA_SEGUNDOS
) -> Optional[ValoresSensores]:
    """Drena o buffer da serial e devolve a linha de DADOS mais recente.

    Linhas de diagnóstico vão para o log; linhas v2 com checksum inválido são
    ignoradas. Ver ``ler_linhas_serial`` para os detalhes da espera.

    Returns:
        A tupla mais recente ou ``None`` se nenhuma linha de dados chegou.

    Raises:
        serial.SerialException: porta fechada ou Arduino desconectado.
    """
    dados = [linha.valores for linha in ler_linhas_serial(conexao, timeout_total)
             if linha.tipo == TIPO_LINHA_DADOS and linha.valores is not None]
    return dados[-1] if dados else None


# ---------------------------------------------------------------------------
# Modelo de IA
# ---------------------------------------------------------------------------
def carregar_modelo(caminho: Path = CAMINHO_MODELO) -> Optional[Dict[str, Any]]:
    """Carrega o pacote ``.pkl`` gerado pelo treinamento.

    O pacote é um ``dict`` com ``modelo`` (estimador com ``predict``),
    ``features`` (lista de colunas), ``versao`` etc.

    Returns:
        O pacote ou ``None`` se o arquivo não existir, estiver corrompido ou
        não tiver o formato esperado (com aviso no log). Sem modelo o sistema
        segue com o motor de regras.
    """
    arquivo = Path(caminho)
    if not arquivo.is_file():
        registrador.warning("Modelo de IA não encontrado em %s; usando só o motor de regras.", arquivo)
        return None
    try:
        pacote = joblib.load(arquivo)
    except Exception as erro:  # noqa: BLE001 — pickle pode falhar de muitas formas
        registrador.warning("Modelo de IA em %s não pôde ser lido (%s); usando só o motor de regras.", arquivo, erro)
        return None
    if not isinstance(pacote, dict):
        registrador.warning("Modelo de IA em %s tem formato inesperado (%s); usando só o motor de regras.",
                            arquivo, type(pacote).__name__)
        return None
    modelo = pacote.get("modelo")
    features = pacote.get("features")
    if modelo is None or not callable(getattr(modelo, "predict", None)):
        registrador.warning("Pacote %s não contém um estimador com predict(); usando só o motor de regras.", arquivo)
        return None
    if not isinstance(features, (list, tuple)) or not features or not all(isinstance(f, str) for f in features):
        registrador.warning("Pacote %s não lista as features do modelo; usando só o motor de regras.", arquivo)
        return None
    pacote["features"] = list(features)
    if not str(pacote.get("versao") or "").strip():
        pacote["versao"] = str(pacote.get("nome_modelo") or type(modelo).__name__)
    culturas = pacote.get("culturas")
    if isinstance(culturas, (list, tuple, set)):
        pacote["culturas"] = sorted({str(codigo).strip() for codigo in culturas if str(codigo).strip()})
        registrador.info("Modelo de IA carregado: %s (%d features; culturas do treino: %s).", pacote["versao"],
                         len(pacote["features"]), ", ".join(pacote["culturas"]) or "nenhuma")
    else:
        pacote.pop("culturas", None)
        registrador.warning("Modelo de IA %s não informa as culturas do treino (pacote antigo); ele será aplicado "
                            "a todas as culturas. Retreine com 'python src/treinamento_ia.py'.", pacote["versao"])
    return pacote


def modelo_cobre_cultura(pacote_modelo: Optional[Dict[str, Any]], codigo_cultura: Optional[str]) -> bool:
    """Indica se o modelo ``.pkl`` foi treinado com dados da cultura informada.

    Três das cinco features do modelo são valores absolutos (temperatura e
    umidades). Uma cultura cadastrada depois do treino tem outras faixas
    ideais, e o modelo do café, aplicado a ela, erra para cima — o que, com
    ``status_alerta = max(regra, IA)``, viraria alarme falso. Por isso o
    modelo só é usado nas culturas listadas em ``pacote["culturas"]``.

    Returns:
        ``False`` sem modelo ou se a cultura não estiver na lista do pacote;
        ``True`` se estiver, se ``codigo_cultura`` for ``None`` (chamador não
        informou a cultura) ou se o pacote for antigo e não trouxer a lista.
    """
    if pacote_modelo is None:
        return False
    if codigo_cultura is None:
        return True
    culturas = pacote_modelo.get("culturas")
    if not isinstance(culturas, (list, tuple, set)):
        return True
    return str(codigo_cultura) in {str(codigo) for codigo in culturas}


# ---------------------------------------------------------------------------
# Saneamento em tempo real
# ---------------------------------------------------------------------------
def _para_float_finito(valor: Optional[float]) -> Optional[float]:
    """Converte para ``float`` finito; ``None`` para ausente, NaN, infinito ou não numérico."""
    if valor is None or isinstance(valor, bool):
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    return numero if math.isfinite(numero) else None


class SaneadorTempoReal:
    """Equivalente on-line do ``detectar_anomalias`` + interpolação do dataset.

    Regras por variável (as mesmas do detector do dataset):
        * ``nan``: leitura ausente ou não numérica;
        * ``negativo``: abaixo do mínimo físico do sensor;
        * ``pico``: acima do máximo físico, ou salto maior que ``LIMITES_SALTO``
          em relação ao último valor válido.

    A leitura anômala é trocada pelo último valor válido (*hold-last*). Os
    valores de referência iniciais são o ponto médio da faixa ideal da cultura;
    enquanto nenhuma leitura real foi aceita, não há base para medir salto,
    então a primeira leitura fisicamente possível é aceita como está — a não
    ser que o saneador tenha sido semeado (``semear``) com uma leitura real.

    Para não "travar" num patamar antigo quando a mudança é real e persistente
    (ex.: irrigação forte), depois de ``LEITURAS_PARA_NOVO_PATAMAR`` leituras
    consecutivas recusadas SÓ pelo salto, e coerentes entre si, o novo patamar
    é aceito.

    Opções da confiabilidade de campo (o padrão mantém o comportamento da v1,
    usado pela demonstração do aplicativo):
        * ``plausibilidade_fisica``: uma QUEDA da umidade do solo maior que
          ``LIMITES_SALTO`` só vira novo patamar se for fisicamente possível
          pela evaporação desde o último valor aceito
          (``queda <= LIMITES_SALTO + taxa_maxima_queda_solo_pp_h × horas``).
          A terra não seca de 70 % para 0 % em segundos: isso é espeto solto.
          A SUBIDA (chuva, rega) continua pela regra das 3 leituras.
        * ``limite_hold_last``: a partir de quantas leituras seguidas presas no
          último válido a variável é listada em
          ``variaveis_hold_last_excedido`` (o diagnóstico vira ``DEFEITO``).
        * ``limites_fisicos``: faixa medível de cada sensor (padrão
          ``LIMITES_FISICOS_SENSORES``, do DHT11). Com DHT22 ou SHT3x
          (``regras_agronomicas.limites_fisicos_sensor_ar``) uma leitura abaixo
          de 0 °C é medição válida, e não anomalia ``negativo``.
    """

    LEITURAS_PARA_NOVO_PATAMAR: int = 3

    def __init__(
        self,
        parametros: ParametrosCultura,
        plausibilidade_fisica: bool = False,
        taxa_maxima_queda_solo_pp_h: float = 8.0,
        limite_hold_last: Optional[int] = None,
        limites_fisicos: Optional[Dict[str, Tuple[float, float]]] = None,
    ) -> None:
        """Define as referências iniciais no ponto médio da faixa ideal de cada sensor.

        Raises:
            ValueError: taxa negativa/não finita, limite de hold-last menor que 1
                ou limites físicos com mínimo maior ou igual ao máximo.
        """
        if not (math.isfinite(float(taxa_maxima_queda_solo_pp_h)) and float(taxa_maxima_queda_solo_pp_h) >= 0):
            raise ValueError(f"taxa_maxima_queda_solo_pp_h inválida: {taxa_maxima_queda_solo_pp_h!r}.")
        if limite_hold_last is not None and (isinstance(limite_hold_last, bool) or int(limite_hold_last) < 1):
            raise ValueError(f"limite_hold_last deve ser inteiro >= 1, recebido {limite_hold_last!r}.")
        self.parametros: ParametrosCultura = parametros
        self.plausibilidade_fisica: bool = bool(plausibilidade_fisica)
        self.taxa_maxima_queda_solo_pp_h: float = float(taxa_maxima_queda_solo_pp_h)
        self.limite_hold_last: Optional[int] = int(limite_hold_last) if limite_hold_last is not None else None
        self.limites_fisicos: Dict[str, Tuple[float, float]] = dict(LIMITES_FISICOS_SENSORES)
        for variavel, (minimo_fisico, maximo_fisico) in (limites_fisicos or {}).items():
            if not float(minimo_fisico) < float(maximo_fisico):
                raise ValueError(f"Limites físicos inválidos para {variavel}: {minimo_fisico!r} a {maximo_fisico!r}.")
            self.limites_fisicos[variavel] = (float(minimo_fisico), float(maximo_fisico))
        self.ultimos_validos: Dict[str, float] = {}
        for variavel in COLUNAS_SENSORES:
            try:
                faixa = parametros.faixa(variavel)
                referencia = (faixa.ideal_min + faixa.ideal_max) / 2.0
            except KeyError:
                minimo, maximo = self.limites_fisicos[variavel]
                referencia = (minimo + maximo) / 2.0
            self.ultimos_validos[variavel] = round(float(referencia), 1)
        self._tem_historico: Dict[str, bool] = {variavel: False for variavel in COLUNAS_SENSORES}
        self._candidatos_salto: Dict[str, List[float]] = {variavel: [] for variavel in COLUNAS_SENSORES}
        self._instante_aceito: Dict[str, Optional[float]] = {variavel: None for variavel in COLUNAS_SENSORES}
        self.leituras_em_hold_last: Dict[str, int] = {variavel: 0 for variavel in COLUNAS_SENSORES}

    def semear(self, valores: Dict[str, float]) -> None:
        """Usa uma leitura real conhecida (ex.: a última gravada) como referência de salto.

        Cada variável informada, finita e dentro da faixa física do sensor
        passa a ser o "último válido", como se tivesse acabado de ser aceita.
        Assim, a primeira linha depois de reconectar o Arduino já é comparada
        com a realidade, e não com o ponto médio da faixa ideal.
        """
        agora = time.monotonic()
        for variavel in COLUNAS_SENSORES:
            numero = _para_float_finito(valores.get(variavel))
            if numero is None:
                continue
            minimo, maximo = self.limites_fisicos[variavel]
            if not minimo <= numero <= maximo:
                continue
            self._aceitar(variavel, round(numero, 1), agora)

    def variaveis_hold_last_excedido(self) -> List[str]:
        """Variáveis presas no último válido há mais de ``limite_hold_last`` leituras (vazio sem limite)."""
        if self.limite_hold_last is None:
            return []
        return [variavel for variavel in COLUNAS_SENSORES
                if self.leituras_em_hold_last[variavel] > self.limite_hold_last]

    def _aceitar(self, variavel: str, valor: float, instante: Optional[float] = None) -> float:
        """Registra ``valor`` como último válido da variável e zera candidatos a novo patamar."""
        self.ultimos_validos[variavel] = valor
        self._tem_historico[variavel] = True
        self._candidatos_salto[variavel].clear()
        self._instante_aceito[variavel] = time.monotonic() if instante is None else float(instante)
        self.leituras_em_hold_last[variavel] = 0
        return valor

    def _queda_plausivel(self, variavel: str, queda: float, instante: float) -> bool:
        """Indica se a queda do solo cabe na evaporação máxima desde o último valor aceito."""
        if not self.plausibilidade_fisica or variavel != VARIAVEL_UMIDADE_SOLO:
            return True
        aceito_em = self._instante_aceito[variavel]
        if aceito_em is None:
            return True
        horas = max(0.0, (instante - aceito_em) / 3600.0)
        return queda <= LIMITES_SALTO[variavel] + self.taxa_maxima_queda_solo_pp_h * horas

    def _sanear_variavel(self, variavel: str, valor_bruto: Optional[float], instante: float) -> Tuple[float, str]:
        """Saneia uma variável. Retorna ``(valor_limpo, rotulo)``; rótulo vazio = leitura aceita."""
        numero = _para_float_finito(valor_bruto)
        referencia = self.ultimos_validos[variavel]
        candidatos = self._candidatos_salto[variavel]
        if numero is None:
            candidatos.clear()
            return referencia, "nan"
        minimo, maximo = self.limites_fisicos[variavel]
        if numero < minimo:
            candidatos.clear()
            return referencia, "negativo"
        if numero > maximo:
            candidatos.clear()
            return referencia, "pico"
        if not self._tem_historico[variavel] or abs(numero - referencia) <= LIMITES_SALTO[variavel]:
            return self._aceitar(variavel, numero, instante), ""
        # Salto em relação ao último válido: candidato a novo patamar real.
        if candidatos and abs(numero - candidatos[-1]) > LIMITES_SALTO[variavel]:
            candidatos.clear()
        candidatos.append(numero)
        del candidatos[:-self.LEITURAS_PARA_NOVO_PATAMAR]
        if len(candidatos) >= self.LEITURAS_PARA_NOVO_PATAMAR:
            if numero < referencia and not self._queda_plausivel(variavel, referencia - numero, instante):
                return referencia, "pico"
            registrador.info("Saneador: %s mudou de patamar de forma persistente (%.1f → %.1f); novo valor aceito.",
                             variavel, referencia, numero)
            return self._aceitar(variavel, numero, instante), ""
        return referencia, "pico"

    def sanear(
        self,
        temperatura_ar: Optional[float],
        umidade_ar: Optional[float],
        umidade_solo: Optional[float],
        instante_monotonic: Optional[float] = None,
    ) -> Tuple[Dict[str, float], int, str]:
        """Saneia uma leitura completa.

        Args:
            temperatura_ar, umidade_ar, umidade_solo: valores brutos (``None`` = ausente).
            instante_monotonic: ``time.monotonic()`` da leitura (padrão: agora);
                usado pela plausibilidade física da queda do solo.

        Returns:
            ``(valores_limpos, flag_anomalia, descricao)``; ``valores_limpos`` tem
            as chaves de ``COLUNAS_SENSORES`` (1 casa decimal), ``flag_anomalia``
            é 0/1 e ``descricao`` segue o formato do detector do dataset
            (ex.: ``"umidade_solo:negativo;temperatura_ar:pico"``, vazio se limpa).
        """
        instante = time.monotonic() if instante_monotonic is None else float(instante_monotonic)
        entradas: Dict[str, Optional[float]] = {
            VARIAVEL_TEMPERATURA_AR: temperatura_ar,
            VARIAVEL_UMIDADE_AR: umidade_ar,
            VARIAVEL_UMIDADE_SOLO: umidade_solo,
        }
        valores_limpos: Dict[str, float] = {}
        rotulos: List[str] = []
        for variavel in COLUNAS_SENSORES:
            valor, rotulo = self._sanear_variavel(variavel, entradas[variavel], instante)
            valores_limpos[variavel] = round(float(valor), 1)
            if rotulo:
                rotulos.append(f"{variavel}:{rotulo}")
                self.leituras_em_hold_last[variavel] += 1
        return valores_limpos, (1 if rotulos else 0), ";".join(rotulos)


# ---------------------------------------------------------------------------
# Predição e processamento
# ---------------------------------------------------------------------------
def predizer_status(
    pacote_modelo: Optional[Dict[str, Any]],
    valores: Dict[str, float],
    deficit_hidrico_solo: float,
    desvio_termico: float,
    status_regra: int,
    codigo_cultura: Optional[str] = None,
) -> Tuple[int, float, str]:
    """Aplica o modelo ``.pkl`` à leitura saneada.

    Args:
        pacote_modelo: pacote carregado por ``carregar_modelo`` ou ``None``.
        valores: leituras saneadas (``temperatura_ar``, ``umidade_ar``, ``umidade_solo``).
        deficit_hidrico_solo, desvio_termico: features derivadas.
        status_regra: status do motor de regras (usado no fallback).
        codigo_cultura: cultura da leitura. Se informada e o modelo não tiver
            sido treinado com ela (``modelo_cobre_cultura``), o motor de regras
            é usado no lugar da IA.

    Returns:
        ``(status_predito, probabilidade_da_classe_predita, versao_modelo)``.
        Sem modelo, ou cultura fora do treino: ``(status_regra, 1.0, "REGRAS")``.
        Erro de predição: o mesmo fallback, com aviso no log.
    """
    if pacote_modelo is None:
        return int(status_regra), 1.0, VERSAO_SEM_MODELO
    if not modelo_cobre_cultura(pacote_modelo, codigo_cultura):
        chave_aviso = (str(pacote_modelo.get("versao")), str(codigo_cultura))
        if chave_aviso not in _CULTURAS_FORA_DO_MODELO_AVISADAS:
            _CULTURAS_FORA_DO_MODELO_AVISADAS.add(chave_aviso)
            registrador.warning("A cultura '%s' não fez parte do treino do modelo %s; usando o motor de regras "
                                "para ela até um novo treino (python src/gerador_dataset.py e "
                                "python src/treinamento_ia.py).", codigo_cultura, chave_aviso[0])
        return int(status_regra), 1.0, VERSAO_SEM_MODELO
    try:
        modelo = pacote_modelo["modelo"]
        features: List[str] = list(pacote_modelo["features"])
        disponiveis: Dict[str, float] = {
            **{chave: float(valor) for chave, valor in valores.items()},
            "deficit_hidrico_solo": float(deficit_hidrico_solo),
            "desvio_termico": float(desvio_termico),
        }
        entrada = pd.DataFrame([[disponiveis[coluna] for coluna in features]], columns=features)
        predito = int(modelo.predict(entrada)[0])
        if predito not in NOMES_STATUS:
            raise ValueError(f"o modelo devolveu a classe {predito}, fora de 0/1/2")
        probabilidade = 1.0
        if callable(getattr(modelo, "predict_proba", None)):
            probabilidades = modelo.predict_proba(entrada)[0]
            classes = [int(classe) for classe in getattr(modelo, "classes_", pacote_modelo.get("classes", [0, 1, 2]))]
            probabilidade = float(probabilidades[classes.index(predito)])
        if not math.isfinite(probabilidade):
            probabilidade = 1.0
        probabilidade = min(1.0, max(0.0, probabilidade))
        versao = str(pacote_modelo.get("versao") or pacote_modelo.get("nome_modelo") or type(modelo).__name__)
        return predito, round(probabilidade, 4), versao
    except Exception as erro:  # noqa: BLE001 — qualquer falha do modelo cai no motor de regras
        registrador.warning("Falha na predição do modelo (%s); usando o motor de regras nesta leitura.", erro)
        return int(status_regra), 1.0, VERSAO_SEM_MODELO


def carregar_modulo_antecipacao(nome: str = NOME_MODULO_ANTECIPACAO) -> Optional[ModuleType]:
    """Carrega, sem dependência rígida, o módulo de antecipação de estresse (``prognostico``).

    O módulo é opcional: se não existir (``ImportError``) ou não oferecer
    ``aplicar_antecipacao`` e ``gravar_analise_antecipacao``, o coletor segue
    só com regra + IA, e um aviso INFO é registrado uma única vez.

    Returns:
        O módulo carregado ou ``None``.
    """
    try:
        modulo = importlib.import_module(nome)
    except ImportError as erro:
        if nome not in _MODULOS_ANTECIPACAO_AVISADOS:
            _MODULOS_ANTECIPACAO_AVISADOS.add(nome)
            registrador.info("Módulo de antecipação '%s' indisponível (%s); seguindo só com regra + IA.", nome, erro)
        return None
    except Exception as erro:  # noqa: BLE001 — módulo opcional com defeito não pode parar o coletor
        if nome not in _MODULOS_ANTECIPACAO_AVISADOS:
            _MODULOS_ANTECIPACAO_AVISADOS.add(nome)
            registrador.warning("Módulo de antecipação '%s' falhou ao carregar (%s); seguindo sem ele.", nome, erro)
        return None
    faltando = [funcao for funcao in ("aplicar_antecipacao", "gravar_analise_antecipacao")
                if not callable(getattr(modulo, funcao, None))]
    if faltando:
        if nome not in _MODULOS_ANTECIPACAO_AVISADOS:
            _MODULOS_ANTECIPACAO_AVISADOS.add(nome)
            registrador.info("Módulo de antecipação '%s' sem %s; seguindo só com regra + IA.", nome, ", ".join(faltando))
        return None
    registrador.info("Antecipação de estresse ativada (módulo %s).", nome)
    return modulo


def carregar_modulo_calibracao(nome: str = NOME_MODULO_CALIBRACAO) -> Optional[ModuleType]:
    """Carrega, sem dependência rígida, o módulo de calibração da sonda de solo (``calibracao_sonda``).

    Mesmo padrão de ``carregar_modulo_antecipacao``: ``importlib``, conferência
    de ``converter_umidade_solo`` chamável e aviso no log uma única vez. A
    falha é lembrada por ``SEGUNDOS_LEMBRAR_FALHA_CALIBRACAO`` (60 s) para não
    tentar o import a cada leitura; um módulo já presente em ``sys.modules``
    é sempre aceito.

    Returns:
        O módulo carregado ou ``None`` (o coletor usa a identidade).
    """
    agora = time.monotonic()
    falhou_em = _FALHAS_MODULO_CALIBRACAO.get(nome)
    if nome not in sys.modules and falhou_em is not None and agora - falhou_em < SEGUNDOS_LEMBRAR_FALHA_CALIBRACAO:
        return None
    primeira_falha = falhou_em is None
    try:
        modulo = importlib.import_module(nome)
    except ImportError as erro:
        _FALHAS_MODULO_CALIBRACAO[nome] = agora
        if primeira_falha:
            registrador.info("Módulo de calibração da sonda '%s' indisponível (%s); umidade do solo do firmware "
                             "usada como está.", nome, erro)
        return None
    except Exception as erro:  # noqa: BLE001 — módulo opcional com defeito não pode parar o coletor
        _FALHAS_MODULO_CALIBRACAO[nome] = agora
        if primeira_falha:
            registrador.warning("Módulo de calibração da sonda '%s' falhou ao carregar (%s); seguindo sem ele.",
                                nome, erro)
        return None
    if not callable(getattr(modulo, "converter_umidade_solo", None)):
        _FALHAS_MODULO_CALIBRACAO[nome] = agora
        if primeira_falha:
            registrador.info("Módulo de calibração '%s' sem converter_umidade_solo(); seguindo sem ele.", nome)
        return None
    if not primeira_falha:
        registrador.info("Calibração da sonda de solo ativada (módulo %s).", nome)
    _FALHAS_MODULO_CALIBRACAO.pop(nome, None)
    return modulo


def _avisar_calibracao(chave: str, mensagem: str, *argumentos: Any) -> None:
    """Aviso da calibração no log, uma vez por tipo de problema."""
    if chave in _AVISOS_CALIBRACAO:
        registrador.debug(mensagem, *argumentos)
        return
    _AVISOS_CALIBRACAO.add(chave)
    registrador.warning(mensagem, *argumentos)


def calibrar_umidade_solo(valor_firmware: Optional[float], caminho_banco: Path = CAMINHO_BANCO) -> Optional[float]:
    """Aplica a calibração da sonda (``calibracao_sonda.converter_umidade_solo``) ao valor do firmware.

    Sem módulo, com exceção, retorno ``None`` para entrada presente ou retorno
    não numérico/não finito: identidade (com aviso no log). Entrada ausente
    (``None``/NaN) continua ausente.
    """
    entrada = _para_float_finito(valor_firmware)
    if entrada is None:
        return None
    modulo = carregar_modulo_calibracao()
    if modulo is None:
        return entrada
    try:
        convertido = modulo.converter_umidade_solo(entrada, Path(caminho_banco), ID_NO_PRINCIPAL)
    except Exception as erro:  # noqa: BLE001 — a calibração nunca pode derrubar a leitura
        _avisar_calibracao(f"excecao:{type(erro).__name__}",
                           "Calibração da sonda falhou (%s); usando o valor do firmware.", erro)
        return entrada
    numero = _para_float_finito(convertido)
    if numero is None:
        _avisar_calibracao("retorno_invalido", "Calibração da sonda devolveu %r para %.1f; usando o valor do firmware.",
                           convertido, entrada)
        return entrada
    return numero


def interpretar_perfil_firmware(codigo_diagnostico: str) -> Dict[str, str]:
    """Lê os campos ``CHAVE=VALOR`` do banner do firmware.

    Aceita ``AGROANALISE_PRONTO;PROTOCOLO=2;AR=DHT22;SOLO=RESISTIVO;PULSO=1``
    (banner numa linha só) e ``AGROANALISE_PERFIL;AR=DHT11;SOLO=RESISTIVO;PULSO=0``
    (2ª linha do firmware v3, que mantém a 1ª idêntica à v2). Outras linhas → ``{}``.

    Returns:
        ``{CHAVE: VALOR}`` com chaves e valores em maiúsculas, sem o nome do banner.
    """
    partes = [parte.strip() for parte in str(codigo_diagnostico or "").strip().split(";")]
    if not partes or partes[0].upper() not in (CODIGO_BANNER_PRONTO, CODIGO_PERFIL_SENSORES):
        return {}
    campos: Dict[str, str] = {}
    for parte in partes[1:]:
        chave, separador, valor = parte.partition("=")
        if separador and chave.strip():
            campos[chave.strip().upper()] = valor.strip().upper()
    return campos


def _regra_temperatura_do_banco(caminho_banco: Path) -> str:
    """``regra_temperatura`` gravada (ausente, inválida ou banco indisponível → ``instantanea``)."""
    try:
        valor = obter_configuracao_tipada("regra_temperatura", MODO_INSTANTANEO, caminho_banco=caminho_banco)
    except sqlite3.Error as erro:
        registrador.warning("Não foi possível ler regra_temperatura (%s); usando a regra instantânea.", erro)
        return MODO_INSTANTANEO
    texto = str(valor or "").strip().lower()
    return MODO_FISIOLOGICO if texto == MODO_FISIOLOGICO else MODO_INSTANTANEO


@dataclass
class MotorRegrasCampo:
    """Regra agronômica do coletor com memória: modo fisiológico/instantâneo e banda morta.

    Atributos:
        configuracao: ``ConfiguracaoTermica`` vigente (recarregada pelo coletor).
        estados_anteriores: ``(codigo_cultura, origem) → (status_conforto,
            status_solo)`` da última leitura avaliada, memória da banda morta.
            Semeado pela última leitura GRAVADA daquela origem (até 1 h) e
            zerado na troca de cultura ou de modo.
        melhoras_pendentes: ``(codigo_cultura, origem, componente) → instante``
            em que a melhora (com folga da banda) começou. A cor melhor só é
            gravada depois de ``banda_morta_atraso_s`` (off-delay, 60 s) de
            melhora contínua; piorar é sempre imediato.

    ``DEMONSTRACAO`` e ``DATASET`` usam sempre a regra instantânea sem banda
    (cenários didáticos prontos). Geada e calor fisiológicos não usam banda.
    """

    configuracao: ConfiguracaoTermica = field(default_factory=ConfiguracaoTermica)
    estados_anteriores: Dict[Tuple[str, str], Tuple[int, int]] = field(default_factory=dict)
    melhoras_pendentes: Dict[Tuple[str, str, str], datetime] = field(default_factory=dict)
    _cultura_atual: Optional[str] = field(default=None, repr=False)

    def _zerar_memoria(self) -> None:
        """Esquece estados anteriores e melhoras pendentes."""
        self.estados_anteriores.clear()
        self.melhoras_pendentes.clear()

    @classmethod
    def do_banco(cls, caminho_banco: Path = CAMINHO_BANCO) -> "MotorRegrasCampo":
        """Motor sem memória com a configuração térmica lida do banco."""
        return cls(configuracao=carregar_configuracao_termica(caminho_banco), estados_anteriores={})

    def atualizar_configuracao(self, configuracao: ConfiguracaoTermica) -> None:
        """Troca a configuração; a memória da banda é zerada se o modo ou a banda mudarem."""
        anterior = self.configuracao
        if (configuracao.modo != anterior.modo or configuracao.banda_morta_ativa != anterior.banda_morta_ativa
                or configuracao.seca_programada_ativa != anterior.seca_programada_ativa):
            self._zerar_memoria()
        self.configuracao = configuracao

    def configuracao_da_origem(self, origem: str) -> ConfiguracaoTermica:
        """Configuração efetiva: demonstração e dataset ficam na regra instantânea sem banda."""
        if origem in ORIGENS_REGRA_FISIOLOGICA:
            return self.configuracao
        return dataclasses.replace(self.configuracao, modo=MODO_INSTANTANEO, banda_morta_ativa=False,
                                   seca_programada_ativa=False)

    def fisiologico(self, origem: str) -> bool:
        """Indica se a leitura desta origem usa a regra fisiológica."""
        return self.configuracao_da_origem(origem).modo == MODO_FISIOLOGICO

    def _semear_estado(
        self, parametros: ParametrosCultura, origem: str, caminho_banco: Path, cfg: ConfiguracaoTermica,
        agora: datetime,
    ) -> Optional[Tuple[int, int]]:
        """Estado anterior a partir da última leitura gravada da origem (até 1 h); ``None`` sem ela."""
        try:
            leitura = buscar_ultima_leitura_origem(parametros.codigo, origem, IDADE_MAXIMA_SEMENTE_BANDA_SEGUNDOS,
                                                   caminho_banco=caminho_banco)
        except Exception as erro:  # noqa: BLE001 — sem semente, a banda começa na próxima leitura
            registrador.debug("Banda morta sem semente (%s).", erro)
            return None
        if leitura is None:
            return None
        temperatura = _para_float_finito(leitura.get(VARIAVEL_TEMPERATURA_AR))
        solo = _para_float_finito(leitura.get(VARIAVEL_UMIDADE_SOLO))
        if temperatura is None or solo is None:
            return None
        media = _para_float_finito(leitura.get("temperatura_media_24h"))
        resumo = RESUMO_VAZIO if media is None else ResumoTermico(media, 24, None, None)
        anterior = avaliar_status_campo(temperatura, solo, parametros, resumo,
                                        dataclasses.replace(cfg, banda_morta_ativa=False), agora)
        return int(anterior.status_conforto), int(anterior.status_solo)

    def avaliar(
        self,
        temperatura: float,
        solo: float,
        parametros: ParametrosCultura,
        origem: str,
        caminho_banco: Path,
        agora: datetime,
    ) -> AvaliacaoRegra:
        """Avalia uma leitura saneada (e filtrada) e atualiza a memória da banda morta.

        Args:
            temperatura, solo: valores da leitura.
            parametros: faixas da cultura.
            origem: origem da leitura (define a regra efetiva).
            caminho_banco: banco do histórico térmico (média de 24 h e máximas).
            agora: instante da leitura.
        """
        if self._cultura_atual != parametros.codigo:
            self._zerar_memoria()
            self._cultura_atual = parametros.codigo
        cfg = self.configuracao_da_origem(origem)
        resumo = (buscar_resumo_termico(parametros.codigo, caminho_banco, agora, origem)
                  if cfg.modo == MODO_FISIOLOGICO else RESUMO_VAZIO)
        chave = (parametros.codigo, origem)
        anterior: Optional[Tuple[int, int]] = None
        if cfg.banda_morta_ativa:
            if chave not in self.estados_anteriores:
                semente = self._semear_estado(parametros, origem, caminho_banco, cfg, agora)
                if semente is not None:
                    self.estados_anteriores[chave] = semente
            anterior = self.estados_anteriores.get(chave)
        avaliacao = avaliar_status_campo(temperatura, solo, parametros, resumo, cfg, agora, estado_anterior=anterior)
        if cfg.banda_morta_ativa and anterior is not None:
            pisos = self._pisos_do_atraso(chave, anterior, avaliacao, cfg, agora)
            if pisos[0] > avaliacao.status_conforto or pisos[1] > avaliacao.status_solo:
                avaliacao = avaliar_status_campo(temperatura, solo, parametros, resumo, cfg, agora,
                                                 estado_anterior=anterior, pisos=pisos)
        if cfg.banda_morta_ativa:
            self.estados_anteriores[chave] = (int(avaliacao.status_conforto), int(avaliacao.status_solo))
        return avaliacao

    def _pisos_do_atraso(
        self, chave: Tuple[str, str], anterior: Tuple[int, int], avaliacao: AvaliacaoRegra,
        cfg: ConfiguracaoTermica, agora: datetime,
    ) -> Tuple[int, int]:
        """Off-delay da melhora: a cor anterior é mantida até a melhora durar ``banda_morta_atraso_s``.

        Returns:
            ``(piso_conforto, piso_solo)``: a cor anterior enquanto a melhora
            está pendente; 0 (sem piso) quando ela venceu ou não há melhora.
        """
        pisos: List[int] = []
        for indice, componente, candidato in ((0, "conforto", avaliacao.status_conforto),
                                              (1, "solo", avaliacao.status_solo)):
            pendencia = (chave[0], chave[1], componente)
            if int(candidato) >= int(anterior[indice]) or cfg.banda_morta_atraso_s <= 0:
                self.melhoras_pendentes.pop(pendencia, None)
                pisos.append(0)
                continue
            inicio = self.melhoras_pendentes.get(pendencia)
            if inicio is None or agora < inicio:  # relógio voltou: recomeça a contagem
                inicio = agora
                self.melhoras_pendentes[pendencia] = inicio
            if (agora - inicio).total_seconds() >= cfg.banda_morta_atraso_s:
                self.melhoras_pendentes.pop(pendencia, None)
                pisos.append(0)
            else:
                pisos.append(int(anterior[indice]))
        return pisos[0], pisos[1]


def _ponto_medio_ideal(parametros: ParametrosCultura, variavel: str) -> float:
    """Ponto médio da faixa ideal da variável (ou da faixa física, se a cultura não a tiver)."""
    try:
        faixa = parametros.faixa(variavel)
        return round((faixa.ideal_min + faixa.ideal_max) / 2.0, 1)
    except KeyError:
        minimo, maximo = LIMITES_FISICOS_SENSORES[variavel]
        return round((minimo + maximo) / 2.0, 1)


def _decidir_com_sensor_suspeito(
    valores: Dict[str, float],
    parametros: ParametrosCultura,
    estado: EstadoSensores,
    pacote_modelo: Optional[Dict[str, Any]],
) -> Tuple[int, int, float, str, int, str]:
    """Decide cor e voz quando um sensor está ``SUSPEITO`` ou com ``DEFEITO``.

    A variável não confiável é trocada pelo ponto médio da faixa ideal (ela
    não pode nem acender nem apagar alerta). O status final é
    ``max(1, status da variável confiável)``: nunca Verde (o agricultor
    precisa conferir o sensor) e nunca Vermelho por causa do sensor suspeito.
    A voz traz a orientação da variável confiável (quando ela não está Verde)
    seguida do pedido de conferir o sensor.

    Returns:
        ``(status_regra, status_predito, probabilidade, versao_modelo, status_alerta, mensagem_voz)``.
    """
    solo_ok = estado.solo_confiavel
    ar_ok = estado.ar_confiavel
    temperatura = valores[VARIAVEL_TEMPERATURA_AR] if ar_ok else _ponto_medio_ideal(parametros, VARIAVEL_TEMPERATURA_AR)
    umidade_ar = valores[VARIAVEL_UMIDADE_AR] if ar_ok else _ponto_medio_ideal(parametros, VARIAVEL_UMIDADE_AR)
    solo = valores[VARIAVEL_UMIDADE_SOLO] if solo_ok else _ponto_medio_ideal(parametros, VARIAVEL_UMIDADE_SOLO)
    deficit, desvio = calcular_features(temperatura, solo, parametros)
    status_regra = calcular_status_regra(temperatura, solo, parametros)
    valores_decisao = {VARIAVEL_TEMPERATURA_AR: temperatura, VARIAVEL_UMIDADE_AR: umidade_ar,
                       VARIAVEL_UMIDADE_SOLO: solo}
    status_predito, probabilidade, versao = predizer_status(
        pacote_modelo, valores_decisao, deficit, desvio, status_regra, codigo_cultura=parametros.codigo
    )
    if ar_ok and not solo_ok:
        status_confiavel = classificar_variavel(temperatura, parametros.faixa(VARIAVEL_TEMPERATURA_AR))
        aviso = MENSAGEM_SENSOR_SOLO
    elif solo_ok and not ar_ok:
        status_confiavel = classificar_variavel(solo, parametros.faixa(VARIAVEL_UMIDADE_SOLO))
        aviso = MENSAGEM_SENSOR_AR
    else:
        status_confiavel = 0
        aviso = f"{MENSAGEM_SENSOR_SOLO} {MENSAGEM_SENSOR_AR}"
    status_alerta = max(1, int(status_confiavel))
    partes: List[str] = []
    if status_confiavel != 0:
        partes.append(gerar_mensagem_orientacao(status_confiavel, deficit, desvio, parametros, temperatura, solo))
    partes.append(aviso)
    return status_regra, status_predito, probabilidade, versao, status_alerta, " ".join(partes)


def _decidir_fisiologico_com_sensor_suspeito(
    valores: Dict[str, float],
    parametros: ParametrosCultura,
    estado: EstadoSensores,
    pacote_modelo: Optional[Dict[str, Any]],
    avaliacao: AvaliacaoRegra,
) -> Tuple[int, int, float, str, int, str, AvaliacaoRegra]:
    """Versão fisiológica de ``_decidir_com_sensor_suspeito`` (mesmas garantias).

    O componente do sensor não confiável é zerado (``restringir_avaliacao``):
    não acende nem apaga alerta. A IA recebe a temperatura neutralizada no
    ponto médio do ideal (como em toda a regra fisiológica) e o solo
    neutralizado se ele for o suspeito. Status final = ``max(1, componente
    confiável)``: nunca Verde, nunca Vermelho por causa do sensor suspeito.

    Returns:
        ``(status_regra, status_predito, probabilidade, versao_modelo,
        status_alerta, mensagem_voz, avaliacao_restrita)``.
    """
    solo_ok = estado.solo_confiavel
    ar_ok = estado.ar_confiavel
    temperatura_ia = _ponto_medio_ideal(parametros, VARIAVEL_TEMPERATURA_AR)
    umidade_ar = valores[VARIAVEL_UMIDADE_AR] if ar_ok else _ponto_medio_ideal(parametros, VARIAVEL_UMIDADE_AR)
    solo = valores[VARIAVEL_UMIDADE_SOLO] if solo_ok else _ponto_medio_ideal(parametros, VARIAVEL_UMIDADE_SOLO)
    deficit, _ = calcular_features(temperatura_ia, solo, parametros)
    restrita = restringir_avaliacao(avaliacao, usar_temperatura=ar_ok, usar_solo=solo_ok)
    valores_ia = {VARIAVEL_TEMPERATURA_AR: temperatura_ia, VARIAVEL_UMIDADE_AR: umidade_ar,
                  VARIAVEL_UMIDADE_SOLO: solo}
    status_predito, probabilidade, versao = predizer_status(
        pacote_modelo, valores_ia, deficit, 0.0, restrita.status, codigo_cultura=parametros.codigo
    )
    if ar_ok != solo_ok:
        status_confiavel = int(restrita.status)
        aviso = MENSAGEM_SENSOR_SOLO if ar_ok else MENSAGEM_SENSOR_AR
    else:
        status_confiavel = 0
        aviso = f"{MENSAGEM_SENSOR_SOLO} {MENSAGEM_SENSOR_AR}"
    status_alerta = max(1, status_confiavel)
    partes: List[str] = []
    if status_confiavel != 0:
        temperatura_falada = valores[VARIAVEL_TEMPERATURA_AR] if ar_ok else temperatura_ia
        partes.append(gerar_mensagem_fisiologica(restrita, deficit, parametros, temperatura_falada, solo))
    partes.append(aviso)
    return (int(restrita.status), status_predito, probabilidade, versao, status_alerta, " ".join(partes), restrita)


def _aplicar_antecipacao(
    modulo: ModuleType, registro: RegistroLeitura, parametros: ParametrosCultura, caminho_banco: Path
) -> Tuple[RegistroLeitura, Optional[Any]]:
    """Chama ``modulo.aplicar_antecipacao`` com defesa total.

    Só ``status_predito``, ``status_alerta`` e ``mensagem_voz`` são copiados, e
    só se o novo status ficar entre o atual e ``max(atual, 1)`` (a antecipação
    pode no máximo acender o Amarelo; nunca leva a Vermelho nem apaga um
    alerta). Qualquer outra coisa — exceção, retorno estranho, status fora da
    faixa — vira aviso no log e o registro original segue.
    """
    try:
        retorno = modulo.aplicar_antecipacao(registro, parametros, caminho_banco)
        novo, resultado = retorno
        status_novo = int(novo.status_alerta)
        predito_novo = int(novo.status_predito)
        mensagem_nova = str(novo.mensagem_voz or "").strip()
    except Exception as erro:  # noqa: BLE001 — módulo opcional não pode impedir a gravação
        registrador.warning("Antecipação falhou nesta leitura (%s); gravando sem ela.", erro)
        return registro, None
    limite_superior = max(int(registro.status_alerta), 1)
    if not (int(registro.status_alerta) <= status_novo <= limite_superior) or predito_novo not in NOMES_STATUS \
            or not mensagem_nova:
        registrador.warning("Antecipação devolveu status %r (atual %d, máximo %d) ou mensagem vazia; ignorada.",
                            status_novo, registro.status_alerta, limite_superior)
        return registro, None
    return dataclasses.replace(registro, status_predito=predito_novo, status_alerta=status_novo,
                               mensagem_voz=mensagem_nova), resultado


def montar_registro_leitura(
    temperatura_ar: Optional[float],
    umidade_ar: Optional[float],
    umidade_solo: Optional[float],
    origem: str,
    codigo_cultura: str,
    saneador: SaneadorTempoReal,
    pacote_modelo: Optional[Dict[str, Any]],
    caminho_banco: Path = CAMINHO_BANCO,
    *,
    filtro: Optional[FiltroMedianaMovel] = None,
    estado_sensores: Optional[EstadoSensores] = None,
    modulo_antecipacao: Optional[ModuleType] = None,
    instante_monotonic: Optional[float] = None,
    motor_regras: Optional[MotorRegrasCampo] = None,
    agora: Optional[datetime] = None,
) -> Tuple[RegistroLeitura, Optional[Any]]:
    """Monta o ``RegistroLeitura`` de UMA leitura, sem gravar.

    Ordem: (só ``CAMPO_USB``: calibração da sonda no host) → sanear → mediana
    móvel (``filtro``) → features/regra/IA → ``max`` → mensagem → (sensor
    suspeito: Amarelo + aviso do sensor) → antecipação (só ``CAMPO_USB`` com
    sensores ``OK``).

    Regra: com ``motor_regras``, ele decide (fisiológica ou instantânea, com
    banda morta). Sem ele, nas origens ``CAMPO_USB``/``SIMULACAO`` com
    ``regra_temperatura='fisiologica'`` no banco, um motor sem memória é
    criado; nos demais casos vale a regra instantânea da v2, sem banda.
    ``DEMONSTRACAO`` e ``DATASET`` usam sempre a regra instantânea. Na regra
    fisiológica a IA do instante (treinada no rótulo antigo) recebe a
    temperatura no ponto médio do ideal e ``desvio_termico = 0``, para não
    reintroduzir o Vermelho térmico pelo ``max(regra, IA)``.

    Args:
        motor_regras: motor com memória (o ``ColetorAgroAnalise`` mantém um).
        agora: instante da leitura (padrão: agora); define ``data_hora`` e o
            dia civil da regra (florada, seca programada, máximas).

    Returns:
        ``(registro, resultado_antecipacao)``; o resultado é ``None`` sem antecipação.

    Raises:
        ValueError: origem inválida ou cultura inexistente.
    """
    if origem not in ORIGENS_LEITURA:
        raise ValueError(f"Origem inválida: {origem!r}. Valores aceitos: {', '.join(ORIGENS_LEITURA)}.")
    if saneador.parametros.codigo == codigo_cultura:
        parametros = saneador.parametros
    else:
        parametros = buscar_parametros_cultura(codigo_cultura, caminho_banco=caminho_banco)
    instante = time.monotonic() if instante_monotonic is None else float(instante_monotonic)
    momento = datetime.now() if agora is None else agora

    umidade_solo_firmware: Optional[float] = None
    umidade_solo_entrada = umidade_solo
    if origem == MODO_CAMPO_USB:
        umidade_solo_firmware = _para_float_finito(umidade_solo)
        umidade_solo_entrada = calibrar_umidade_solo(umidade_solo, caminho_banco)

    valores, flag_anomalia, descricao_anomalia = saneador.sanear(temperatura_ar, umidade_ar, umidade_solo_entrada,
                                                                 instante_monotonic=instante)
    if filtro is not None:
        valores = filtro.filtrar(valores, parametros, (parametros.codigo, origem), instante)
    temperatura = valores[VARIAVEL_TEMPERATURA_AR]
    solo = valores[VARIAVEL_UMIDADE_SOLO]
    deficit_hidrico_solo, desvio_termico = calcular_features(temperatura, solo, parametros)

    motor = motor_regras
    if (motor is None and origem in ORIGENS_REGRA_FISIOLOGICA
            and _regra_temperatura_do_banco(caminho_banco) == MODO_FISIOLOGICO):
        motor = MotorRegrasCampo.do_banco(caminho_banco)
    if motor is not None:
        avaliacao = motor.avaliar(temperatura, solo, parametros, origem, caminho_banco, momento)
        fisiologico = motor.fisiologico(origem)
    else:
        avaliacao = avaliar_status_campo(temperatura, solo, parametros, RESUMO_VAZIO, ConfiguracaoTermica(), momento)
        fisiologico = False

    estado_sensor = estado_sensores.estado_geral if estado_sensores is not None else ESTADO_SENSOR_OK
    if estado_sensores is None or estado_sensor == ESTADO_SENSOR_OK:
        if fisiologico:
            status_regra = int(avaliacao.status)
            valores_ia = dict(valores)
            valores_ia[VARIAVEL_TEMPERATURA_AR] = _ponto_medio_ideal(parametros, VARIAVEL_TEMPERATURA_AR)
            status_predito, probabilidade, versao_modelo = predizer_status(
                pacote_modelo, valores_ia, deficit_hidrico_solo, 0.0, status_regra, codigo_cultura=parametros.codigo
            )
            status_alerta = max(status_regra, status_predito)
            avaliacao_falada = (avaliacao if status_alerta == avaliacao.status
                                else dataclasses.replace(avaliacao, status=status_alerta))
            mensagem_voz = gerar_mensagem_fisiologica(avaliacao_falada, deficit_hidrico_solo, parametros,
                                                      temperatura, solo)
        else:
            status_regra = (int(avaliacao.status) if motor is not None
                            else calcular_status_regra(temperatura, solo, parametros))
            status_predito, probabilidade, versao_modelo = predizer_status(
                pacote_modelo, valores, deficit_hidrico_solo, desvio_termico, status_regra,
                codigo_cultura=parametros.codigo
            )
            status_alerta = max(status_regra, status_predito)
            mensagem_voz = gerar_mensagem_orientacao(
                status_alerta, deficit_hidrico_solo, desvio_termico, parametros, temperatura, solo
            )
    elif fisiologico:
        (status_regra, status_predito, probabilidade, versao_modelo, status_alerta, mensagem_voz,
         avaliacao) = _decidir_fisiologico_com_sensor_suspeito(valores, parametros, estado_sensores, pacote_modelo,
                                                               avaliacao)
    else:
        (status_regra, status_predito, probabilidade, versao_modelo, status_alerta,
         mensagem_voz) = _decidir_com_sensor_suspeito(valores, parametros, estado_sensores, pacote_modelo)
        avaliacao = restringir_avaliacao(avaliacao, usar_temperatura=estado_sensores.ar_confiavel,
                                         usar_solo=estado_sensores.solo_confiavel)
    media_24h = (round(float(avaliacao.temperatura_referencia), 2)
                 if avaliacao.base_temperatura == "media_24h" else None)
    registro = RegistroLeitura(
        data_hora=momento.strftime(FORMATO_DATA_HORA),
        codigo_cultura=parametros.codigo,
        origem=origem,
        temperatura_ar_bruta=_para_float_finito(temperatura_ar),
        umidade_ar_bruta=_para_float_finito(umidade_ar),
        umidade_solo_bruta=_para_float_finito(umidade_solo_entrada),  # já calibrada no CAMPO_USB
        temperatura_ar=temperatura,
        umidade_ar=valores[VARIAVEL_UMIDADE_AR],
        umidade_solo=solo,
        deficit_hidrico_solo=deficit_hidrico_solo,
        desvio_termico=desvio_termico,
        status_regra=status_regra,
        status_predito=status_predito,
        status_alerta=status_alerta,
        probabilidade_predicao=probabilidade,
        flag_anomalia=flag_anomalia,
        descricao_anomalia=descricao_anomalia,
        mensagem_voz=mensagem_voz,
        versao_modelo=versao_modelo,
        estado_sensor=estado_sensor,
        diagnostico_sensor=estado_sensores.descricao if estado_sensores is not None else "",
        instante_utc=(datetime.now(timezone.utc) if agora is None
                      else momento.astimezone(timezone.utc)).isoformat(timespec="seconds"),
        motivo_regra=avaliacao.motivo,
        base_temperatura=avaliacao.base_temperatura,
        temperatura_media_24h=media_24h,
        umidade_solo_firmware=umidade_solo_firmware,
    )
    resultado: Optional[Any] = None
    if modulo_antecipacao is not None and origem == MODO_CAMPO_USB and estado_sensor == ESTADO_SENSOR_OK:
        registro, resultado = _aplicar_antecipacao(modulo_antecipacao, registro, parametros, caminho_banco)
    return registro, resultado


def gravar_registro_leitura(
    registro: RegistroLeitura,
    caminho_banco: Path = CAMINHO_BANCO,
    *,
    resultado_antecipacao: Optional[Any] = None,
    modulo_antecipacao: Optional[ModuleType] = None,
    configuracoes_extras: Optional[Dict[str, Any]] = None,
) -> int:
    """Grava o registro (e as configurações extras na mesma transação) e, depois, a análise de antecipação.

    Falha ao gravar a análise vira aviso no log: a leitura já está gravada.

    Returns:
        ``id_leitura`` gerado.

    Raises:
        ValueError: registro inválido.
        sqlite3.Error: falha de gravação da leitura.
    """
    id_leitura = inserir_leitura(registro, caminho_banco=caminho_banco, configuracoes_extras=configuracoes_extras)
    if resultado_antecipacao is not None and modulo_antecipacao is not None:
        try:
            modulo_antecipacao.gravar_analise_antecipacao(id_leitura, resultado_antecipacao, caminho_banco)
        except Exception as erro:  # noqa: BLE001 — a leitura já foi gravada; a análise é opcional
            registrador.warning("Não foi possível gravar a análise de antecipação da leitura %d: %s", id_leitura, erro)
    return id_leitura


def processar_leitura(
    temperatura_ar: Optional[float],
    umidade_ar: Optional[float],
    umidade_solo: Optional[float],
    origem: str,
    codigo_cultura: str,
    saneador: SaneadorTempoReal,
    pacote_modelo: Optional[Dict[str, Any]],
    caminho_banco: Path = CAMINHO_BANCO,
    *,
    filtro: Optional[FiltroMedianaMovel] = None,
    estado_sensores: Optional[EstadoSensores] = None,
    modulo_antecipacao: Optional[ModuleType] = None,
    configuracoes_extras: Optional[Dict[str, Any]] = None,
    motor_regras: Optional[MotorRegrasCampo] = None,
    agora: Optional[datetime] = None,
) -> Tuple[int, RegistroLeitura]:
    """Pipeline completo de UMA leitura até o banco.

    sanear → mediana móvel (se ``filtro``) → ``calcular_features`` →
    ``calcular_status_regra`` → ``predizer_status`` (só nas culturas do treino
    do modelo; nas demais, o motor de regras) → ``status_alerta = max(regra,
    predito)`` (princípio da precaução) → ``gerar_mensagem_orientacao`` →
    ``inserir_leitura``.

    Com ``estado_sensores`` indicando sensor ``SUSPEITO``/``DEFEITO``, a cor
    vira ``max(1, status da variável confiável)`` e a voz pede para conferir o
    sensor (``MENSAGEM_SENSOR_SOLO``/``MENSAGEM_SENSOR_AR``): um sensor solto
    nunca produz Vermelho.

    Args:
        temperatura_ar, umidade_ar, umidade_solo: valores brutos (``None`` = NaN).
        origem: ``CAMPO_USB``, ``SIMULACAO``, ``DEMONSTRACAO`` ou ``DATASET``.
        codigo_cultura: cultura da leitura; se diferente da cultura do
            saneador, os parâmetros são buscados no banco.
        saneador: estado do saneamento em tempo real (mantém o último válido).
        pacote_modelo: pacote ``.pkl`` carregado ou ``None``.
        caminho_banco: banco de destino.
        filtro: mediana móvel aplicada depois do saneamento (só por palavra-chave).
        estado_sensores: diagnóstico dos sensores desta leitura.
        modulo_antecipacao: módulo ``prognostico`` (ver ``carregar_modulo_antecipacao``).
        configuracoes_extras: configurações gravadas na mesma transação da leitura.
        motor_regras: regra com memória (fisiológica/instantânea e banda morta);
            ``None`` segue ``montar_registro_leitura`` (motor sem memória só
            com ``regra_temperatura='fisiologica'`` no banco; sem banda morta).
        agora: instante da leitura (padrão: agora).

    Returns:
        ``(id_leitura, registro_gravado)``.

    Raises:
        ValueError: origem inválida ou cultura inexistente.
        sqlite3.Error: falha de gravação.
    """
    registro, resultado = montar_registro_leitura(
        temperatura_ar, umidade_ar, umidade_solo, origem, codigo_cultura, saneador, pacote_modelo, caminho_banco,
        filtro=filtro, estado_sensores=estado_sensores, modulo_antecipacao=modulo_antecipacao,
        motor_regras=motor_regras, agora=agora,
    )
    id_leitura = gravar_registro_leitura(registro, caminho_banco, resultado_antecipacao=resultado,
                                         modulo_antecipacao=modulo_antecipacao,
                                         configuracoes_extras=configuracoes_extras)
    return id_leitura, registro


# ---------------------------------------------------------------------------
# Coletor 24/7
# ---------------------------------------------------------------------------
def _interpretar_data_hora(texto: str) -> Optional[datetime]:
    """Converte o texto ISO de ``demonstracao_expira_em``; ``None`` se vazio ou inválido."""
    conteudo = str(texto or "").strip()
    if not conteudo:
        return None
    try:
        return datetime.fromisoformat(conteudo)
    except ValueError:
        return None


def _agora_comparavel(referencia: datetime) -> datetime:
    """Retorna o instante atual com (ou sem) fuso, conforme ``referencia``, para comparação."""
    if referencia.tzinfo is not None:
        return datetime.now().astimezone(referencia.tzinfo)
    return datetime.now()


class ColetorAgroAnalise:
    """Motor de coleta 24/7: Arduino → saneamento → IA → banco, com fallback automático.

    Atributos públicos úteis para acompanhar a execução:
        * ``modo_atual``: ``CAMPO_USB``, ``SIMULACAO`` ou ``DEMONSTRACAO``;
        * ``iteracoes_executadas``: ciclos concluídos (com ou sem leitura);
        * ``leituras_gravadas``: leituras inseridas no banco (inclusive as que
          esperaram na fila enquanto o banco estava travado);
        * ``caminho_modelo``: arquivo ``.pkl`` vigiado (padrão ``CAMINHO_MODELO``);
        * ``contadores``: ``ContadoresProtocolo`` da serial;
        * ``fila_pendente``: leituras em memória aguardando o banco destravar;
        * ``modulo_antecipacao``: módulo ``prognostico`` carregado (ou ``None``);
        * ``motor_regras``: ``MotorRegrasCampo`` com a regra térmica vigente e a
          memória da banda morta (recarregado a cada leitura da configuração).
    """

    def __init__(
        self,
        caminho_banco: Path = CAMINHO_BANCO,
        forcar_simulacao: bool = False,
        porta: Optional[str] = None,
        intervalo_segundos: Optional[float] = None,
        max_iteracoes: Optional[int] = None,
    ) -> None:
        """Configura o coletor (nada é aberto até ``executar``).

        Args:
            caminho_banco: banco SQLite usado para configurações e histórico.
            forcar_simulacao: nunca abrir a serial (Modo Simulação fixo).
            porta: porta serial; ``None`` usa ``porta_serial`` da configuração
                (``AUTO`` = autodetecção).
            intervalo_segundos: intervalo entre ciclos; ``None`` usa
                ``intervalo_coleta_segundos`` da configuração (lido a cada ciclo).
            max_iteracoes: número de ciclos antes de parar; ``None`` = infinito.

        Raises:
            ValueError: intervalo ou número de iterações inválidos.
        """
        if intervalo_segundos is not None and not (math.isfinite(float(intervalo_segundos))
                                                   and float(intervalo_segundos) > 0):
            raise ValueError(f"intervalo_segundos deve ser positivo, recebido {intervalo_segundos!r}.")
        if max_iteracoes is not None and (isinstance(max_iteracoes, bool) or int(max_iteracoes) < 1):
            raise ValueError(f"max_iteracoes deve ser inteiro >= 1, recebido {max_iteracoes!r}.")
        self.caminho_banco: Path = Path(caminho_banco)
        self.caminho_modelo: Path = CAMINHO_MODELO
        self.forcar_simulacao: bool = bool(forcar_simulacao)
        self.porta_preferida: Optional[str] = porta.strip() if porta and porta.strip() else None
        self.intervalo_fixo: Optional[float] = float(intervalo_segundos) if intervalo_segundos is not None else None
        self.max_iteracoes: Optional[int] = int(max_iteracoes) if max_iteracoes is not None else None

        self.modo_atual: str = MODO_SIMULACAO
        self.iteracoes_executadas: int = 0
        self.leituras_gravadas: int = 0

        self._config: Dict[str, Any] = {}
        self._conexao: Optional[serial.Serial] = None
        self._porta_conectada: Optional[str] = None
        self._proxima_tentativa_conexao: float = 0.0
        self._ciclos_sem_dados: int = 0
        self._falhas_conexao_seguidas: int = 0

        self._codigo_cultura: Optional[str] = None
        self._parametros: Optional[ParametrosCultura] = None
        self._cultura_invalida_avisada: Optional[str] = None
        self._saneador_campo: Optional[SaneadorTempoReal] = None
        self._saneador_simulacao: Optional[SaneadorTempoReal] = None
        self._saneador_demonstracao: Optional[SaneadorTempoReal] = None
        self._simulador: Optional[SimuladorTempoReal] = None
        self._cenario_atual: str = ""

        self._pacote_modelo: Optional[Dict[str, Any]] = None
        self._mtime_modelo: Optional[float] = None
        self._tentativas_modelo_mtime: int = 0
        self._aviso_modelo_ausente_emitido: bool = False

        self._parar_solicitado: threading.Event = threading.Event()
        self._ultimo_erro_ciclo: Optional[str] = None
        self._repeticoes_erro_ciclo: int = 0

        # Confiabilidade 24/7 do campo
        self.modulo_antecipacao: Optional[ModuleType] = carregar_modulo_antecipacao()
        self.contadores: ContadoresProtocolo = ContadoresProtocolo()
        self.fila_pendente: Deque[RegistroLeitura] = deque(maxlen=TAMANHO_MAXIMO_FILA)
        self._filtro_campo: FiltroMedianaMovel = FiltroMedianaMovel()
        self._diagnostico: DiagnosticoSensores = DiagnosticoSensores()
        self._descartar_proxima_linha_dados: bool = False
        self._linha_dados_ciclo: Optional[LinhaSerial] = None
        self._ultimo_erro_registrado: str = ""
        self._batimento_gravado_no_ciclo: bool = False
        self._disco_livre_mb: Optional[float] = None
        self._relogio_ultimo_salto_em: Optional[str] = None
        self._referencia_relogio: Optional[Tuple[float, float]] = None
        self._proxima_retencao: float = time.monotonic() + ATRASO_PRIMEIRA_RETENCAO_SEGUNDOS
        self._proxima_retencao_emergencia: float = 0.0
        self._proximo_resumo: float = time.monotonic() + INTERVALO_RESUMO_SEGUNDOS
        self._resumo_leituras: int = 0
        self._resumo_status: Dict[int, int] = {0: 0, 1: 0, 2: 0}

        # Regra fisiológica (v3)
        self.motor_regras: MotorRegrasCampo = MotorRegrasCampo()
        self._assinatura_config_termica: Optional[Tuple[Any, ...]] = None

    # ------------------------------------------------------------------
    # Configurações
    # ------------------------------------------------------------------
    def _atualizar_configuracoes(self) -> None:
        """Relê ``configuracao_sistema``; se o banco falhar, mantém as últimas lidas.

        Também recarrega a configuração térmica do ``motor_regras`` (regra,
        florada e seca programada escritas pela banca valem no ciclo seguinte).
        """
        try:
            self._config = obter_todas_configuracoes(caminho_banco=self.caminho_banco)
        except Exception as erro:  # noqa: BLE001 — banco indisponível não pode parar o coletor
            registrador.warning("Não foi possível ler as configurações (%s); mantendo as anteriores.", erro)
        self._recarregar_motor_regras()

    def _recarregar_motor_regras(self) -> None:
        """Atualiza o ``motor_regras`` quando alguma chave térmica mudou (avisos de valor inválido saem uma vez)."""
        assinatura = tuple(self._config.get(chave) for chave in CHAVES_CONFIGURACAO_TERMICA)
        if assinatura == self._assinatura_config_termica:
            return
        anterior = self.motor_regras.configuracao
        nova = configuracao_termica_de_dicionario(self._config)
        self.motor_regras.atualizar_configuracao(nova)
        if self._assinatura_config_termica is None or nova.modo != anterior.modo:
            registrador.info("Regra de temperatura: %s (banda morta %s, seca programada %s).", nova.modo,
                             "ligada" if nova.banda_morta_ativa else "desligada",
                             "ligada" if nova.seca_programada_ativa else "desligada")
        self._assinatura_config_termica = assinatura

    def _config_texto(self, chave: str, padrao: str = "") -> str:
        """Valor textual de uma configuração lida no ciclo."""
        valor = self._config.get(chave)
        return padrao if valor is None else str(valor)

    def _config_numero(self, chave: str, padrao: float) -> float:
        """Valor numérico positivo de uma configuração (ou ``padrao``)."""
        valor = self._config.get(chave)
        try:
            numero = float(valor) if valor is not None else padrao
        except (TypeError, ValueError):
            return padrao
        return numero if math.isfinite(numero) and numero > 0 else padrao

    def _definir_configuracao_segura(self, chave: str, valor: Any) -> bool:
        """Grava uma configuração sem deixar uma falha do banco derrubar o ciclo."""
        try:
            definir_configuracao(chave, valor, caminho_banco=self.caminho_banco)
            self._config[chave] = valor
            return True
        except Exception as erro:  # noqa: BLE001
            registrador.error("Não foi possível gravar a configuração %s=%r: %s", chave, valor, erro)
            return False

    def _gravar_modo(self, modo: str) -> None:
        """Grava ``modo_operacao`` sem atropelar uma demonstração recém-ativada pelo aplicativo.

        Para ``CAMPO_USB``/``SIMULACAO`` a gravação é condicional e ATÔMICA
        (``UPDATE ... WHERE valor <> 'DEMONSTRACAO'``): não existe intervalo
        entre conferir e gravar em que o aplicativo possa iniciar uma
        demonstração e tê-la sobrescrita pelo coletor.
        """
        if modo == MODO_DEMONSTRACAO:
            self._definir_configuracao_segura("modo_operacao", modo)
            return
        try:
            gravou = definir_configuracao_condicional("modo_operacao", modo, proibir_valor_atual=MODO_DEMONSTRACAO,
                                                      caminho_banco=self.caminho_banco)
        except Exception as erro:  # noqa: BLE001 — banco indisponível não pode parar o coletor
            registrador.error("Não foi possível gravar a configuração modo_operacao=%r: %s", modo, erro)
            return
        if gravou:
            self._config["modo_operacao"] = modo
        else:
            registrador.info("Demonstração ativada pelo aplicativo neste instante; modo %s não gravado.", modo)
            self._config["modo_operacao"] = MODO_DEMONSTRACAO

    def _config_inteiro(self, chave: str, padrao: int) -> int:
        """Valor inteiro positivo de uma configuração (ou ``padrao``)."""
        return max(1, int(round(self._config_numero(chave, float(padrao)))))

    def _criar_saneador_campo(self, parametros: ParametrosCultura) -> SaneadorTempoReal:
        """Saneador do Modo Campo USB: plausibilidade física da queda do solo, limite de hold-last e
        faixa medível do sensor de ar informado pelo firmware (``sensor_ar_modelo``)."""
        return SaneadorTempoReal(
            parametros,
            plausibilidade_fisica=True,
            taxa_maxima_queda_solo_pp_h=self._config_numero("taxa_maxima_queda_solo_pp_h", 8.0),
            limite_hold_last=LIMITE_HOLD_LAST_CAMPO,
            limites_fisicos=limites_fisicos_sensor_ar(self._config_texto("sensor_ar_modelo", SENSOR_AR_PADRAO)),
        )

    def _aplicar_perfil_firmware(self, codigo_diagnostico: str) -> None:
        """Grava o sensor de ar anunciado no banner (``AR=``) e ajusta o saneador e a regra de geada.

        Grava ``sensor_ar_modelo`` e ``incerteza_sensor_ar_c`` (perfil
        desconhecido → DHT11, o mais conservador). Se o modelo mudou, o
        saneador do campo é recriado com a nova faixa medível e semeado.
        Banner sem ``AR=`` (firmware v2) não muda nada.
        """
        campos = interpretar_perfil_firmware(codigo_diagnostico)
        if "AR" not in campos:
            return
        modelo = normalizar_modelo_sensor_ar(campos["AR"])
        incerteza = float(PERFIS_SENSOR_AR[modelo][2])
        modelo_atual = normalizar_modelo_sensor_ar(self._config_texto("sensor_ar_modelo", SENSOR_AR_PADRAO))
        registrador.info("Firmware informou: sensor de ar %s (±%.1f °C), sonda %s, alimentação pulsada %s.",
                         modelo, incerteza, campos.get("SOLO", "?"), "sim" if campos.get("PULSO") == "1" else "não")
        try:
            incerteza_atual = float(self._config.get("incerteza_sensor_ar_c"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            incerteza_atual = float("nan")
        if modelo != self._config_texto("sensor_ar_modelo", "") or incerteza_atual != incerteza:
            self._definir_configuracao_segura("sensor_ar_modelo", modelo)
            self._definir_configuracao_segura("incerteza_sensor_ar_c", incerteza)
            self._recarregar_motor_regras()
        if modelo != modelo_atual and self._parametros is not None:
            self._saneador_campo = self._criar_saneador_campo(self._parametros)
            self._semear_saneador_campo()
            registrador.info("Saneador do campo ajustado à faixa medível do %s.", modelo)

    def _recriar_qualidade_campo(self) -> None:
        """Recria a mediana móvel e o diagnóstico dos sensores com os valores da configuração."""
        self._filtro_campo = FiltroMedianaMovel(janela=self._config_inteiro("janela_mediana_leituras", 12))
        self._diagnostico = DiagnosticoSensores(
            minutos_travamento_solo=self._config_numero("minutos_travamento_solo", 60.0),
            horas_travamento_ar=self._config_numero("horas_travamento_ar", 3.0),
            limite_hold_last=LIMITE_HOLD_LAST_CAMPO,
        )

    def _semear_saneador_campo(self) -> None:
        """Parte o saneador do campo da última leitura CAMPO_USB com menos de 10 minutos, se houver."""
        if self._saneador_campo is None or self._codigo_cultura is None:
            return
        try:
            leitura = buscar_ultima_leitura_origem(self._codigo_cultura, MODO_CAMPO_USB, IDADE_MAXIMA_SEMENTE_SEGUNDOS,
                                                   caminho_banco=self.caminho_banco)
        except Exception as erro:  # noqa: BLE001 — sem semente o saneador usa a faixa ideal
            registrador.warning("Não foi possível buscar a última leitura de campo para semear o saneador: %s", erro)
            return
        if leitura is None:
            return
        valores = {variavel: leitura.get(variavel) for variavel in COLUNAS_SENSORES}
        self._saneador_campo.semear(valores)
        registrador.info("Saneador do campo semeado com a última leitura real (#%s: T=%.1f °C, solo=%.1f %%).",
                         leitura.get("id_leitura"), float(leitura["temperatura_ar"]), float(leitura["umidade_solo"]))

    def _intervalo_coleta(self) -> float:
        """Intervalo entre ciclos: o da linha de comando ou o da configuração."""
        intervalo = self.intervalo_fixo
        if intervalo is None:
            intervalo = self._config_numero("intervalo_coleta_segundos", INTERVALO_COLETA_PADRAO)
        return max(INTERVALO_MINIMO_SEGUNDOS, intervalo)

    def _intervalo_reconexao(self) -> float:
        """Intervalo entre tentativas de reconectar o Arduino."""
        return max(INTERVALO_MINIMO_SEGUNDOS,
                   self._config_numero("intervalo_reconexao_segundos", INTERVALO_RECONEXAO_PADRAO))

    # ------------------------------------------------------------------
    # Cultura e modelo
    # ------------------------------------------------------------------
    def _carregar_cultura(self, codigo: str) -> bool:
        """Carrega os parâmetros da cultura e recria saneadores e simulador."""
        try:
            parametros = buscar_parametros_cultura(codigo, caminho_banco=self.caminho_banco)
        except ValueError as erro:
            if self._cultura_invalida_avisada != codigo:
                registrador.error("Cultura ativa inválida (%s); mantendo '%s'.", erro, self._codigo_cultura)
                self._cultura_invalida_avisada = codigo
            return False
        anterior = self._codigo_cultura
        self._parametros = parametros
        self._codigo_cultura = parametros.codigo
        self._cultura_invalida_avisada = None
        self._saneador_campo = self._criar_saneador_campo(parametros)
        self._saneador_simulacao = SaneadorTempoReal(parametros)
        self._saneador_demonstracao = SaneadorTempoReal(parametros)
        self._simulador = SimuladorTempoReal(parametros)
        self._recriar_qualidade_campo()
        if self.conectado:
            self._semear_saneador_campo()
        if anterior is None:
            registrador.info("Cultura monitorada: %s (%s).", parametros.nome_comum, parametros.codigo)
        else:
            registrador.info("Cultura ativa trocada em tempo real: %s → %s (%s).",
                             anterior, parametros.codigo, parametros.nome_comum)
        return True

    def _verificar_troca_cultura(self) -> None:
        """Recarrega os parâmetros quando ``cultura_ativa`` muda na configuração."""
        desejada = self._config_texto("cultura_ativa", self._codigo_cultura or CULTURA_PADRAO).strip()
        if not desejada:
            desejada = CULTURA_PADRAO
        if desejada != self._codigo_cultura:
            carregou = self._carregar_cultura(desejada)
            if not carregou and self._parametros is None and desejada != CULTURA_PADRAO:
                registrador.warning("Usando a cultura padrão '%s'.", CULTURA_PADRAO)
                self._carregar_cultura(CULTURA_PADRAO)
        if self._parametros is None:
            raise RuntimeError("Nenhuma cultura pôde ser carregada do banco.")

    def _verificar_modelo(self) -> None:
        """Carrega/recarrega o ``.pkl`` quando ele aparece, muda (mtime) ou some."""
        try:
            mtime: Optional[float] = self.caminho_modelo.stat().st_mtime
        except FileNotFoundError:
            mtime = None
        except OSError as erro:
            registrador.warning("Não foi possível verificar o modelo (%s).", erro)
            return
        if mtime is None:
            if self._pacote_modelo is not None:
                registrador.warning("Modelo de IA removido de %s; usando só o motor de regras.", self.caminho_modelo)
                self._aviso_modelo_ausente_emitido = True
            elif not self._aviso_modelo_ausente_emitido:
                registrador.info("Modelo de IA ainda não existe em %s; usando o motor de regras (REGRAS) "
                                 "até ele aparecer.", self.caminho_modelo)
                self._aviso_modelo_ausente_emitido = True
            self._pacote_modelo = None
            self._mtime_modelo = None
            self._tentativas_modelo_mtime = 0
            return
        self._aviso_modelo_ausente_emitido = False
        if mtime == self._mtime_modelo and (self._pacote_modelo is not None
                                            or self._tentativas_modelo_mtime >= TENTATIVAS_POR_VERSAO_MODELO):
            return
        if mtime != self._mtime_modelo:
            self._mtime_modelo = mtime
            self._tentativas_modelo_mtime = 0
        self._tentativas_modelo_mtime += 1
        pacote = carregar_modelo(self.caminho_modelo)
        if pacote is not None:
            versao_anterior = (self._pacote_modelo or {}).get("versao")
            self._pacote_modelo = pacote
            self._tentativas_modelo_mtime = TENTATIVAS_POR_VERSAO_MODELO
            if versao_anterior is None:
                registrador.info("IA ativada no coletor: %s.", pacote["versao"])
            else:
                registrador.info("Modelo de IA recarregado: %s → %s.", versao_anterior, pacote["versao"])
        elif self._pacote_modelo is not None:
            registrador.warning("Novo arquivo de modelo inválido; mantendo %s.", self._pacote_modelo.get("versao"))

    # ------------------------------------------------------------------
    # Hardware
    # ------------------------------------------------------------------
    @property
    def conectado(self) -> bool:
        """Indica se há uma conexão serial aberta com o Arduino."""
        return self._conexao is not None

    def _modo_base(self) -> str:
        """Modo fora da demonstração: Campo USB com Arduino, senão Simulação."""
        return MODO_CAMPO_USB if self.conectado else MODO_SIMULACAO

    def _reconexao_devida(self) -> bool:
        """Indica se já é hora de tentar abrir a porta serial."""
        return not self.forcar_simulacao and time.monotonic() >= self._proxima_tentativa_conexao

    def _agendar_reconexao(self) -> None:
        """Agenda a próxima tentativa de conexão."""
        self._proxima_tentativa_conexao = time.monotonic() + self._intervalo_reconexao()

    def _tentar_conectar(self) -> bool:
        """Localiza e abre a porta do Arduino; em caso de sucesso passa para Campo USB."""
        porta_configurada = self.porta_preferida or self._config_texto("porta_serial", "AUTO") or "AUTO"
        porta = localizar_porta_arduino(porta_configurada)
        if porta is None:
            self._falhas_conexao_seguidas += 1
            self._agendar_reconexao()
            nivel = logging.INFO if self._falhas_conexao_seguidas == 1 else logging.DEBUG
            registrador.log(nivel, "Nenhum Arduino encontrado; nova tentativa em %.0f s.", self._intervalo_reconexao())
            return False
        baud_rate = int(self._config_numero("baud_rate", float(BAUD_RATE_PADRAO)))
        try:
            conexao = abrir_conexao_serial(porta, baud_rate=baud_rate, timeout=TIMEOUT_PORTA_SEGUNDOS)
        except (serial.SerialException, OSError, ValueError) as erro:
            self._falhas_conexao_seguidas += 1
            self._agendar_reconexao()
            nivel = logging.WARNING if self._falhas_conexao_seguidas == 1 else logging.INFO
            registrador.log(nivel, "Não foi possível abrir a porta %s (%s); nova tentativa em %.0f s.",
                            porta, erro, self._intervalo_reconexao())
            return False
        self._conexao = conexao
        self._porta_conectada = porta
        self._ciclos_sem_dados = 0
        self._falhas_conexao_seguidas = 0
        # A 1ª linha de dados depois de abrir a porta pode estar cortada (o
        # reset da placa interrompe a transmissão): é descartada e contada.
        self._descartar_proxima_linha_dados = True
        if self._parametros is not None:
            self._saneador_campo = self._criar_saneador_campo(self._parametros)
            self._semear_saneador_campo()
        self._filtro_campo.reiniciar()
        self._definir_configuracao_segura("estado_hardware", ESTADO_CONECTADO)
        if self.modo_atual == MODO_DEMONSTRACAO:
            registrador.info("Arduino conectado em %s durante a demonstração; o Modo Campo USB assume ao final dela.",
                             porta)
        else:
            self.modo_atual = MODO_CAMPO_USB
            self._gravar_modo(MODO_CAMPO_USB)
            registrador.info("Arduino conectado em %s: Modo Campo USB ativado.", porta)
        return True

    def _fechar_serial(self) -> None:
        """Fecha a porta serial ignorando erros (o dispositivo pode já ter sumido)."""
        conexao = self._conexao
        self._conexao = None
        if conexao is not None:
            try:
                conexao.close()
            except Exception as erro:  # noqa: BLE001
                registrador.debug("Erro ao fechar a porta serial (ignorado): %s", erro)

    def _desconectar(self, motivo: str) -> None:
        """Fecha a serial, grava DESCONECTADO e cai para o Modo Simulação."""
        porta = self._porta_conectada
        self._fechar_serial()
        self._porta_conectada = None
        self._ciclos_sem_dados = 0
        self._agendar_reconexao()
        self._definir_configuracao_segura("estado_hardware", ESTADO_DESCONECTADO)
        if self.modo_atual == MODO_DEMONSTRACAO:
            registrador.warning("Arduino desconectado durante a demonstração (%s, porta %s); "
                                "ao final dela o Modo Simulação assume.", motivo, porta)
            return
        self.modo_atual = MODO_SIMULACAO
        self._gravar_modo(MODO_SIMULACAO)
        registrador.warning("Arduino desconectado: Modo Simulação ativado (%s, porta %s). "
                            "Nova tentativa de conexão em %.0f s.", motivo, porta, self._intervalo_reconexao())

    # ------------------------------------------------------------------
    # Demonstração
    # ------------------------------------------------------------------
    def _encerrar_demonstracao(self, motivo: str, limpar_configuracao: bool) -> bool:
        """Sai do Modo Demonstração para Campo USB (se conectado) ou Simulação.

        Com ``limpar_configuracao`` o encerramento no banco é ATÔMICO e
        condicional (``encerrar_demonstracao_se_inalterada``): se o aplicativo
        iniciou outra demonstração depois da leitura da configuração, nada é
        apagado e o método retorna ``False``.

        Returns:
            ``True`` se a demonstração foi encerrada; ``False`` se uma nova
            demonstração foi iniciada nesse meio-tempo.
        """
        novo_modo = self._modo_base()
        if limpar_configuracao:
            try:
                encerrou = encerrar_demonstracao_se_inalterada(
                    self._config_texto("cenario_demonstracao"),
                    self._config_texto("demonstracao_expira_em"),
                    novo_modo,
                    caminho_banco=self.caminho_banco,
                )
            except Exception as erro:  # noqa: BLE001 — banco indisponível não pode parar o coletor
                registrador.error("Não foi possível gravar o fim da demonstração: %s", erro)
                encerrou = True
            if not encerrou:
                registrador.info("O aplicativo iniciou outra demonstração agora; encerramento (%s) cancelado.", motivo)
                return False
            self._config.update({"cenario_demonstracao": "", "demonstracao_expira_em": "", "modo_operacao": novo_modo})
        estava_em_demonstracao = self.modo_atual == MODO_DEMONSTRACAO
        self.modo_atual = novo_modo
        self._cenario_atual = ""
        if estava_em_demonstracao or limpar_configuracao:
            registrador.info("Modo Demonstração encerrado (%s): voltando ao modo %s.", motivo, novo_modo)
        return True

    def _motivo_fim_demonstracao(self) -> Optional[str]:
        """Motivo para encerrar a demonstração configurada (``None`` se ela segue válida)."""
        cenario = self._config_texto("cenario_demonstracao").strip()
        expira_em = _interpretar_data_hora(self._config_texto("demonstracao_expira_em"))
        if cenario not in CENARIOS_DEMONSTRACAO:
            return f"cenário inválido {cenario!r}"
        if expira_em is None:
            return "sem data de expiração válida"
        if _agora_comparavel(expira_em) >= expira_em:
            return "tempo expirou"
        return None

    def _demonstracao_ativa(self) -> bool:
        """Decide, pela configuração, se este ciclo é de demonstração (encerrando-a se expirou).

        Se o encerramento for cancelado porque o aplicativo acabou de iniciar
        outra demonstração, a configuração é relida e avaliada mais uma vez.
        """
        for _ in range(2):
            if self._config_texto("modo_operacao") != MODO_DEMONSTRACAO:
                if self.modo_atual == MODO_DEMONSTRACAO:
                    self._encerrar_demonstracao("encerrada pelo aplicativo", limpar_configuracao=False)
                return False
            motivo = self._motivo_fim_demonstracao()
            if motivo is None:
                return True
            if self._encerrar_demonstracao(motivo, limpar_configuracao=True):
                return False
            self._atualizar_configuracoes()
        return False

    def _ciclo_demonstracao(self) -> None:
        """Grava uma leitura do cenário extremo, mantendo a serial drenada."""
        parametros = self._parametros
        if parametros is None:
            raise RuntimeError("Cultura não carregada.")
        cenario = self._config_texto("cenario_demonstracao").strip()
        if self.modo_atual != MODO_DEMONSTRACAO or cenario != self._cenario_atual:
            self.modo_atual = MODO_DEMONSTRACAO
            self._cenario_atual = cenario
            # Saneador próprio: valores extremos do cenário não podem ser tratados
            # como "pico" em relação às leituras reais anteriores.
            self._saneador_demonstracao = SaneadorTempoReal(parametros)
            registrador.info("Modo Demonstração ativado: cenário %s (%s) até %s.", cenario,
                             CENARIOS_DEMONSTRACAO[cenario]["rotulo"], self._config_texto("demonstracao_expira_em"))
        if self._conexao is not None:
            try:
                ler_linha_mais_recente(self._conexao, timeout_total=0.0)
            except serial.SerialException as erro:
                self._desconectar(f"falha na serial: {erro}")
        elif self._reconexao_devida():
            self._tentar_conectar()
        leitura = gerar_leitura_cenario(cenario, parametros)
        saneador = self._saneador_demonstracao or SaneadorTempoReal(parametros)
        self._saneador_demonstracao = saneador
        self._processar(leitura[VARIAVEL_TEMPERATURA_AR], leitura[VARIAVEL_UMIDADE_AR],
                        leitura[VARIAVEL_UMIDADE_SOLO], MODO_DEMONSTRACAO, saneador)

    # ------------------------------------------------------------------
    # Ciclo principal
    # ------------------------------------------------------------------
    def _configuracoes_batimento(self) -> Dict[str, Any]:
        """Telemetria do coletor gravada a cada ciclo (lida por /api/saude e /api/status)."""
        extras: Dict[str, Any] = {
            "coletor_batimento_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "coletor_pid": int(os.getpid()),
            "coletor_ultimo_erro": self._ultimo_erro_registrado,
            "coletor_fila_pendente": len(self.fila_pendente),
        }
        if self._disco_livre_mb is not None:
            extras["disco_livre_mb"] = round(float(self._disco_livre_mb), 1)
        if self._relogio_ultimo_salto_em:
            extras["relogio_ultimo_salto_em"] = self._relogio_ultimo_salto_em
        extras.update(self.contadores.como_configuracoes())
        return extras

    def _despejar_fila(self) -> bool:
        """Grava, numa transação só, as leituras que esperavam o banco destravar.

        Returns:
            ``True`` se a fila ficou vazia; ``False`` se o banco continua travado.
        """
        if not self.fila_pendente:
            return True
        pendentes = list(self.fila_pendente)
        try:
            inserir_leituras_em_lote(pendentes, caminho_banco=self.caminho_banco)
        except sqlite3.OperationalError as erro:
            self._anotar_erro(erro)
            registrador.warning("Banco ainda indisponível (%s); %d leitura(s) seguem na fila.", erro, len(pendentes))
            return False
        except ValueError as erro:
            # Não deveria acontecer (os registros vêm do próprio pipeline); grava
            # um a um para não perder os válidos por causa de um inválido.
            registrador.error("Fila com leitura inválida (%s); gravando uma a uma.", erro)
            gravadas = 0
            for registro in pendentes:
                try:
                    inserir_leitura(registro, caminho_banco=self.caminho_banco)
                    gravadas += 1
                except ValueError as erro_registro:
                    registrador.error("Leitura inválida descartada da fila (%s): %s", registro.data_hora, erro_registro)
            self.fila_pendente.clear()
            self.leituras_gravadas += gravadas
            return True
        self.fila_pendente.clear()
        self.leituras_gravadas += len(pendentes)
        registrador.info("Fila despejada: %d leitura(s) gravada(s) com a data/hora original.", len(pendentes))
        return True

    def _anotar_erro(self, erro: BaseException) -> None:
        """Guarda o último erro (com o instante UTC) para o batimento ``coletor_ultimo_erro``."""
        instante = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._ultimo_erro_registrado = f"{instante} {type(erro).__name__}: {erro}"[:500]

    def _enfileirar(self, registro: RegistroLeitura, erro: Optional[BaseException] = None) -> None:
        """Guarda a leitura em memória quando o banco está travado (a mais antiga sai se a fila lotar)."""
        if len(self.fila_pendente) == self.fila_pendente.maxlen:
            registrador.error("Fila de leituras cheia (%d): a mais antiga foi descartada.", self.fila_pendente.maxlen)
        self.fila_pendente.append(registro)
        if erro is not None:
            self._anotar_erro(erro)
        registrador.warning("Banco indisponível: leitura de %s guardada na fila (%d pendente(s)).", registro.data_hora,
                            len(self.fila_pendente))

    def _processar(
        self,
        temperatura_ar: Optional[float],
        umidade_ar: Optional[float],
        umidade_solo: Optional[float],
        origem: str,
        saneador: SaneadorTempoReal,
        filtro: Optional[FiltroMedianaMovel] = None,
        estado_sensores: Optional[EstadoSensores] = None,
    ) -> None:
        """Monta a leitura, despeja a fila pendente, grava (com o batimento) e registra no log.

        Se o banco estiver travado (``sqlite3.OperationalError``), a leitura vai
        para a fila em memória com a data/hora de agora; no próximo ciclo a fila
        é gravada em lote ANTES da leitura nova.
        """
        if self._codigo_cultura is None:
            raise RuntimeError("Cultura não carregada.")
        registro, resultado = montar_registro_leitura(
            temperatura_ar, umidade_ar, umidade_solo, origem, self._codigo_cultura, saneador, self._pacote_modelo,
            caminho_banco=self.caminho_banco, filtro=filtro, estado_sensores=estado_sensores,
            modulo_antecipacao=self.modulo_antecipacao, motor_regras=self.motor_regras,
        )
        if not self._despejar_fila():
            self._enfileirar(registro)
            return
        try:
            id_leitura = gravar_registro_leitura(
                registro, self.caminho_banco, resultado_antecipacao=resultado,
                modulo_antecipacao=self.modulo_antecipacao, configuracoes_extras=self._configuracoes_batimento(),
            )
        except sqlite3.OperationalError as erro:
            self._enfileirar(registro, erro)
            return
        self._batimento_gravado_no_ciclo = True
        self.leituras_gravadas += 1
        self._resumo_leituras += 1
        self._resumo_status[int(registro.status_alerta)] = self._resumo_status.get(int(registro.status_alerta), 0) + 1
        anomalia = f" | anomalia corrigida: {registro.descricao_anomalia}" if registro.flag_anomalia else ""
        sensor = (f" | sensor {registro.estado_sensor}: {registro.diagnostico_sensor}"
                  if registro.estado_sensor != ESTADO_SENSOR_OK else "")
        registrador.debug(
            "[%s] #%d %s | T=%.1f °C UR=%.1f %% Solo=%.1f %% | %s (regra %d, IA %d, p=%.2f, %s)%s%s",
            origem, id_leitura, registro.codigo_cultura, registro.temperatura_ar, registro.umidade_ar,
            registro.umidade_solo, NOMES_STATUS[registro.status_alerta], registro.status_regra,
            registro.status_predito, registro.probabilidade_predicao, registro.versao_modelo, anomalia, sensor,
        )

    def _ciclo_simulacao(self) -> None:
        """Gera e grava uma leitura sintética (Modo Simulação)."""
        if self._simulador is None or self._saneador_simulacao is None:
            raise RuntimeError("Simulador não inicializado (cultura não carregada).")
        leitura = self._simulador.proxima_leitura()
        self._processar(leitura[VARIAVEL_TEMPERATURA_AR], leitura[VARIAVEL_UMIDADE_AR],
                        leitura[VARIAVEL_UMIDADE_SOLO], MODO_SIMULACAO, self._saneador_simulacao)

    def _ao_receber_linha(self, linha: LinhaSerial) -> bool:
        """Trata cada linha recebida da serial; devolve ``True`` se ela é um dado aproveitável.

        * conta a linha no protocolo (v1, v2, checksum inválido);
        * leva os diagnósticos do firmware ao ``DiagnosticoSensores``; o
          banner ``#AGROANALISE_PRONTO`` (placa reiniciou, ex.: watchdog) faz a
          próxima linha de dados ser descartada; ``AR=`` no banner (ou na linha
          ``#AGROANALISE_PERFIL``) grava o perfil do sensor de ar;
        * descarta a 1ª linha de dados depois de abrir a porta e, com
          ``exigir_protocolo_v2``, as linhas v1.
        """
        self.contadores.contabilizar(linha)
        if linha.tipo == TIPO_LINHA_DIAGNOSTICO:
            self._diagnostico.registrar_diagnostico(linha.codigo_diagnostico, time.monotonic())
            if linha.codigo_diagnostico.upper().startswith(CODIGO_BANNER_PRONTO):
                self._descartar_proxima_linha_dados = True
            if linha.codigo_diagnostico.upper().startswith((CODIGO_BANNER_PRONTO, CODIGO_PERFIL_SENSORES)):
                self._aplicar_perfil_firmware(linha.codigo_diagnostico)
            return False
        if linha.tipo != TIPO_LINHA_DADOS or linha.valores is None:
            return False
        if bool(self._config.get("exigir_protocolo_v2")) and linha.versao_protocolo < VERSAO_PROTOCOLO_V2:
            self.contadores.descartadas += 1
            registrador.debug("Linha v1 descartada: exigir_protocolo_v2 está ligado.")
            return False
        if self._descartar_proxima_linha_dados:
            self._descartar_proxima_linha_dados = False
            self.contadores.descartadas += 1
            registrador.debug("1ª linha de dados após (re)iniciar a placa descartada: %r", linha.valores)
            return False
        self._linha_dados_ciclo = linha
        return True

    def _ciclo_campo(self) -> bool:
        """Lê o Arduino e grava. Retorna ``False`` se o Arduino caiu neste ciclo."""
        conexao = self._conexao
        if conexao is None or self._saneador_campo is None:
            return False
        self._linha_dados_ciclo = None
        try:
            ler_linhas_serial(conexao, timeout_total=TIMEOUT_LEITURA_SEGUNDOS, ao_receber=self._ao_receber_linha)
        except serial.SerialException as erro:
            self._desconectar(f"falha na serial: {erro}")
            return False
        linha = self._linha_dados_ciclo
        if linha is None or linha.valores is None:
            self._ciclos_sem_dados += 1
            registrador.warning("Nenhuma linha de dados do Arduino neste ciclo (%d/%d).",
                                self._ciclos_sem_dados, LIMITE_CICLOS_SEM_DADOS)
            if self._ciclos_sem_dados >= LIMITE_CICLOS_SEM_DADOS:
                self._desconectar(f"{LIMITE_CICLOS_SEM_DADOS} ciclos seguidos sem dados válidos")
                return False
            return True
        self._ciclos_sem_dados = 0
        temperatura, umidade_ar, solo = linha.valores
        estado = self._diagnostico.avaliar(
            {VARIAVEL_TEMPERATURA_AR: temperatura, VARIAVEL_UMIDADE_AR: umidade_ar, VARIAVEL_UMIDADE_SOLO: solo},
            time.monotonic(),
            hold_last_excedido=self._saneador_campo.variaveis_hold_last_excedido(),
        )
        self._processar(temperatura, umidade_ar, solo, MODO_CAMPO_USB, self._saneador_campo,
                        filtro=self._filtro_campo, estado_sensores=estado)
        return True

    def _sincronizar_estado_gravado(self) -> None:
        """Corrige ``modo_operacao``/``estado_hardware`` se alguém gravou valores que não refletem a realidade."""
        estado_real = ESTADO_CONECTADO if self.conectado else ESTADO_DESCONECTADO
        if self._config_texto("estado_hardware") != estado_real:
            self._definir_configuracao_segura("estado_hardware", estado_real)
        modo_gravado = self._config_texto("modo_operacao")
        if modo_gravado != self.modo_atual and modo_gravado != MODO_DEMONSTRACAO:
            registrador.info("Ajustando modo_operacao gravado (%s) para o modo real %s.", modo_gravado or "vazio",
                             self.modo_atual)
            self._gravar_modo(self.modo_atual)

    def _executar_ciclo(self) -> None:
        """Um ciclo completo: configurações, cultura, modelo, modo e leitura."""
        self._atualizar_configuracoes()
        self._verificar_troca_cultura()
        self._verificar_modelo()
        if self._demonstracao_ativa():
            self._ciclo_demonstracao()
            return
        if not self.conectado and self._reconexao_devida():
            self._tentar_conectar()
        self._sincronizar_estado_gravado()
        if self.conectado and self._ciclo_campo():
            return
        self._ciclo_simulacao()

    def _preparar(self) -> None:
        """Inicializa banco (com as chaves da v3, ``INSERT OR IGNORE``), cultura, modelo e estado do hardware."""
        try:
            inicializar_banco(self.caminho_banco)
            semear_configuracoes_confiabilidade(self.caminho_banco)
            semear_configuracoes_v3(self.caminho_banco)
        except Exception as erro:  # noqa: BLE001 — o ciclo tentará de novo
            registrador.error("Falha ao inicializar o banco %s: %s", self.caminho_banco, erro)
        self._atualizar_configuracoes()
        try:
            self._verificar_troca_cultura()
        except Exception as erro:  # noqa: BLE001
            registrador.error("Falha ao carregar a cultura ativa: %s", erro)
        self._verificar_modelo()
        self.modo_atual = MODO_SIMULACAO
        self._definir_configuracao_segura("estado_hardware", ESTADO_DESCONECTADO)
        self._gravar_modo(MODO_SIMULACAO)  # condicional: preserva uma demonstração em andamento
        if self.forcar_simulacao:
            registrador.info("Modo Simulação forçado (--simular): a porta serial não será usada.")
        self._proxima_tentativa_conexao = time.monotonic()

    def _registrar_falha_ciclo(self, erro: Exception) -> None:
        """Registra a falha de um ciclo: traceback completo só na 1ª ocorrência de cada erro.

        Num serviço 24/7, um banco fora do ar geraria o mesmo traceback a cada
        poucos segundos; as repetições viram uma linha curta.
        """
        assinatura = f"{type(erro).__name__}: {erro}"
        numero_ciclo = self.iteracoes_executadas + 1
        self._anotar_erro(erro)
        if assinatura != self._ultimo_erro_ciclo:
            self._ultimo_erro_ciclo = assinatura
            self._repeticoes_erro_ciclo = 1
            registrador.exception("Falha no ciclo %d (%s); o coletor segue no próximo ciclo.", numero_ciclo, erro)
            return
        self._repeticoes_erro_ciclo += 1
        registrador.error("Falha no ciclo %d (mesmo erro, %dª vez seguida): %s", numero_ciclo,
                          self._repeticoes_erro_ciclo, assinatura)

    # ------------------------------------------------------------------
    # Vigias do serviço 24/7: relógio, disco, retenção, batimento e resumo
    # ------------------------------------------------------------------
    def _verificar_relogio(self) -> None:
        """Detecta saltos do relógio de parede comparando-o com o relógio monotônico.

        Se, entre dois ciclos, ``Δtime.time()`` e ``Δtime.monotonic()``
        diferirem mais de 60 s, alguém (NTP, bateria do RTC, usuário) mudou a
        hora: registra WARNING e grava ``relogio_ultimo_salto_em``. A ordem do
        histórico não é afetada (ela segue o ``id_leitura``).
        """
        parede, monotonico = time.time(), time.monotonic()
        referencia = self._referencia_relogio
        self._referencia_relogio = (parede, monotonico)
        if referencia is None:
            return
        salto = (parede - referencia[0]) - (monotonico - referencia[1])
        if abs(salto) > LIMITE_SALTO_RELOGIO_SEGUNDOS:
            self._relogio_ultimo_salto_em = datetime.now(timezone.utc).isoformat(timespec="seconds")
            registrador.warning("O relógio do computador saltou %+.0f s entre dois ciclos (ajuste de hora?). "
                                "A ordem das leituras segue a ordem de gravação.", salto)

    def _verificar_disco(self) -> None:
        """Mede o espaço livre do disco do banco; abaixo do mínimo, aplica a retenção de emergência."""
        pasta = self.caminho_banco.parent if str(self.caminho_banco.parent) else Path(".")
        try:
            uso = shutil.disk_usage(pasta)
        except OSError as erro:
            registrador.debug("Não foi possível medir o disco de %s: %s", pasta, erro)
            return
        livre_mb = float(uso.free) / (1024.0 * 1024.0)
        self._disco_livre_mb = livre_mb
        minimo_mb = self._config_numero("espaco_minimo_mb", ESPACO_MINIMO_PADRAO_MB)
        if livre_mb >= minimo_mb:
            return
        registrador.error("Disco quase cheio: %.0f MB livres em %s (mínimo %.0f MB).", livre_mb, pasta, minimo_mb)
        if time.monotonic() < self._proxima_retencao_emergencia:
            return
        self._proxima_retencao_emergencia = time.monotonic() + INTERVALO_RETENCAO_EMERGENCIA_SEGUNDOS
        try:
            resultado = aplicar_retencao(dias_brutos=DIAS_RETENCAO_EMERGENCIA, caminho_banco=self.caminho_banco)
            registrador.error("Retenção de emergência (%d dias) aplicada: %s", DIAS_RETENCAO_EMERGENCIA, resultado)
        except Exception as erro:  # noqa: BLE001 — o coletor segue mesmo sem conseguir liberar espaço
            self._anotar_erro(erro)
            registrador.error("Retenção de emergência falhou: %s", erro)

    def _verificar_retencao_periodica(self) -> None:
        """Aplica a retenção configurada (``retencao_dias_brutos``) uma vez por hora (relógio monotônico)."""
        agora = time.monotonic()
        if agora < self._proxima_retencao:
            return
        self._proxima_retencao = agora + INTERVALO_RETENCAO_SEGUNDOS
        dias = self._config_inteiro("retencao_dias_brutos", 30)
        try:
            aplicar_retencao(dias_brutos=dias, caminho_banco=self.caminho_banco)
        except Exception as erro:  # noqa: BLE001 — banco travado: tenta na próxima hora
            self._anotar_erro(erro)
            registrador.warning("Retenção periódica do histórico falhou (%s); nova tentativa em 1 h.", erro)

    def _gravar_batimento_avulso(self) -> None:
        """Grava o batimento quando o ciclo não gravou leitura (ex.: Arduino sem dados)."""
        try:
            definir_configuracoes(self._configuracoes_batimento(), caminho_banco=self.caminho_banco)
        except Exception as erro:  # noqa: BLE001 — batimento é telemetria; o ciclo segue
            registrador.debug("Batimento não gravado neste ciclo: %s", erro)

    def _registrar_resumo_periodico(self, forcar: bool = False) -> None:
        """Resumo INFO a cada 5 minutos: leituras, cores, modo, fila e contadores da serial."""
        agora = time.monotonic()
        if not forcar and agora < self._proximo_resumo:
            return
        self._proximo_resumo = agora + INTERVALO_RESUMO_SEGUNDOS
        contadores = self.contadores
        registrador.info(
            "Resumo: %d leitura(s) (verde %d, amarelo %d, vermelho %d) | modo %s | fila %d | sensores %s | "
            "serial v1=%d v2=%d checksum_inválido=%d descartadas=%d | disco livre %s MB",
            self._resumo_leituras, self._resumo_status.get(0, 0), self._resumo_status.get(1, 0),
            self._resumo_status.get(2, 0), self.modo_atual, len(self.fila_pendente),
            self._diagnostico.ultimo_estado.estado_geral, contadores.linhas_v1, contadores.linhas_v2,
            contadores.checksum_invalido, contadores.descartadas,
            f"{self._disco_livre_mb:.0f}" if self._disco_livre_mb is not None else "?",
        )
        self._resumo_leituras = 0
        self._resumo_status = {0: 0, 1: 0, 2: 0}

    def _vigias_do_ciclo(self) -> None:
        """Roda as verificações de serviço de um ciclo, cada uma isolada contra falhas."""
        for vigia in (self._verificar_relogio, self._verificar_disco, self._verificar_retencao_periodica):
            try:
                vigia()
            except Exception as erro:  # noqa: BLE001 — vigia nunca derruba a coleta
                registrador.warning("Verificação %s falhou: %s", getattr(vigia, "__name__", "?"), erro)

    def _aguardar(self, segundos: float) -> None:
        """Espera até o próximo ciclo, acordando cedo se a parada for pedida."""
        self._parar_solicitado.wait(timeout=max(0.0, segundos))

    def parar(self) -> None:
        """Pede ao loop que termine ao fim do ciclo atual (seguro entre threads)."""
        self._parar_solicitado.set()

    def _tratar_sinal_termino(self, numero_sinal: int, quadro: Optional[FrameType]) -> None:
        """Converte SIGTERM (ex.: ``systemctl stop``) em parada limpa."""
        registrador.info("Sinal %d recebido: encerrando o coletor.", numero_sinal)
        self.parar()

    def encerrar(self) -> None:
        """Grava a fila pendente, fecha a serial e grava o estado final (hardware desconectado)."""
        if self.fila_pendente:
            try:
                self._despejar_fila()
            except Exception as erro:  # noqa: BLE001 — encerramento não pode falhar por causa do banco
                registrador.error("Leituras pendentes não gravadas no encerramento: %s", erro)
            if self.fila_pendente:
                registrador.error("%d leitura(s) da fila não puderam ser gravadas antes de encerrar.",
                                  len(self.fila_pendente))
        self._fechar_serial()
        self._porta_conectada = None
        self._definir_configuracao_segura("estado_hardware", ESTADO_DESCONECTADO)
        # Condicional e atômico: só troca CAMPO_USB por SIMULACAO. Uma demonstração
        # gravada pelo aplicativo neste instante nunca é sobrescrita.
        try:
            definir_configuracao_condicional("modo_operacao", MODO_SIMULACAO, exigir_valor_atual=MODO_CAMPO_USB,
                                             caminho_banco=self.caminho_banco)
        except Exception as erro:  # noqa: BLE001 — encerramento não pode falhar por causa do banco
            registrador.error("Não foi possível gravar o modo final: %s", erro)
        self._registrar_resumo_periodico(forcar=True)
        registrador.info("Coletor encerrado: %d ciclo(s), %d leitura(s) gravada(s).",
                         self.iteracoes_executadas, self.leituras_gravadas)

    def executar(self) -> None:
        """Loop 24/7 (``while True``) até ``max_iteracoes``, Ctrl+C ou ``parar()``.

        Cada ciclo é protegido por ``try/except``: falha de serial, banco ou
        modelo é registrada no log e o próximo ciclo segue normalmente.
        """
        tratador_anterior: Any = None
        sinal_instalado = False
        if threading.current_thread() is threading.main_thread():
            try:
                tratador_anterior = signal.signal(signal.SIGTERM, self._tratar_sinal_termino)
                sinal_instalado = True
            except (ValueError, OSError) as erro:
                registrador.debug("SIGTERM não instalado: %s", erro)
        self._parar_solicitado.clear()
        registrador.info("Coletor AgroAnálise iniciado (banco %s, %s).", self.caminho_banco,
                         f"até {self.max_iteracoes} ciclo(s)" if self.max_iteracoes else "24/7")
        try:
            self._preparar()
            while True:
                inicio = time.monotonic()
                self._batimento_gravado_no_ciclo = False
                self._vigias_do_ciclo()
                try:
                    self._executar_ciclo()
                    if self._ultimo_erro_ciclo is not None:
                        registrador.info("Ciclo normalizado após %d falha(s) seguida(s).", self._repeticoes_erro_ciclo)
                        self._ultimo_erro_ciclo = None
                        self._repeticoes_erro_ciclo = 0
                except Exception as erro:  # noqa: BLE001 — o coletor nunca pode parar por erro de um ciclo
                    self._registrar_falha_ciclo(erro)
                if not self._batimento_gravado_no_ciclo:
                    self._gravar_batimento_avulso()
                self._registrar_resumo_periodico()
                self.iteracoes_executadas += 1
                if self.max_iteracoes is not None and self.iteracoes_executadas >= self.max_iteracoes:
                    registrador.info("Limite de %d ciclo(s) atingido.", self.max_iteracoes)
                    break
                if self._parar_solicitado.is_set():
                    break
                self._aguardar(self._intervalo_coleta() - (time.monotonic() - inicio))
                if self._parar_solicitado.is_set():
                    break
        except KeyboardInterrupt:
            registrador.info("Interrompido pelo usuário (Ctrl+C).")
        finally:
            try:
                self.encerrar()
            except KeyboardInterrupt:
                registrador.warning("Encerramento interrompido por um segundo Ctrl+C.")
            if sinal_instalado:
                try:
                    signal.signal(signal.SIGTERM, tratador_anterior)
                except (ValueError, OSError, TypeError) as erro:
                    registrador.debug("Não foi possível restaurar o tratador de SIGTERM: %s", erro)


# ---------------------------------------------------------------------------
# Linha de comando
# ---------------------------------------------------------------------------
def _criar_parser() -> argparse.ArgumentParser:
    """Monta o parser de argumentos da linha de comando."""
    parser = argparse.ArgumentParser(
        prog="coletor_serial.py",
        description="AgroAnálise — motor de coleta 24/7 (Arduino → saneamento → IA → banco), "
                    "com Modo Simulação automático quando o Arduino desconecta.",
    )
    parser.add_argument("--porta", type=str, default=None,
                        help="Porta serial do Arduino (ex.: /dev/ttyUSB0, COM3). Padrão: configuração "
                             "'porta_serial' do banco (AUTO = autodetecção).")
    parser.add_argument("--simular", action="store_true",
                        help="Força o Modo Simulação (não usa a porta serial).")
    parser.add_argument("--intervalo", type=float, default=None,
                        help="Segundos entre leituras. Padrão: configuração 'intervalo_coleta_segundos'.")
    parser.add_argument("--max-iteracoes", type=int, default=None, dest="max_iteracoes",
                        help="Para depois de N ciclos (padrão: roda 24/7 até Ctrl+C).")
    parser.add_argument("--cultura", type=str, default=None,
                        help="Código da cultura a monitorar (grava 'cultura_ativa'), ex.: cafe_conilon.")
    parser.add_argument("--banco", type=Path, default=CAMINHO_BANCO,
                        help=f"Caminho do banco SQLite (padrão: {CAMINHO_BANCO}).")
    parser.add_argument("--verboso", action="store_true",
                        help="Registra também mensagens de depuração (uma linha por leitura).")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Ponto de entrada da linha de comando.

    Returns:
        0 em execução normal (incluindo Ctrl+C), 2 para argumentos inválidos.
    """
    parser = _criar_parser()
    argumentos = parser.parse_args(argv)
    configurar_logs("coletor", verboso=argumentos.verboso)
    if argumentos.intervalo is not None and not (math.isfinite(argumentos.intervalo) and argumentos.intervalo > 0):
        registrador.error("--intervalo precisa ser um número positivo.")
        return 2
    if argumentos.max_iteracoes is not None and argumentos.max_iteracoes < 1:
        registrador.error("--max-iteracoes precisa ser pelo menos 1.")
        return 2
    caminho_banco = Path(argumentos.banco)
    try:
        inicializar_banco(caminho_banco)
        if argumentos.cultura:
            codigo = argumentos.cultura.strip()
            parametros = buscar_parametros_cultura(codigo, caminho_banco=caminho_banco)
            definir_configuracao("cultura_ativa", parametros.codigo, caminho_banco=caminho_banco)
            registrador.info("Cultura ativa definida pela linha de comando: %s.", parametros.nome_comum)
    except ValueError as erro:
        registrador.error("%s", erro)
        return 2
    except KeyboardInterrupt:
        registrador.info("Interrompido pelo usuário (Ctrl+C).")
        return 0
    coletor = ColetorAgroAnalise(
        caminho_banco=caminho_banco,
        forcar_simulacao=argumentos.simular,
        porta=argumentos.porta,
        intervalo_segundos=argumentos.intervalo,
        max_iteracoes=argumentos.max_iteracoes,
    )
    coletor.executar()
    return 0


if __name__ == "__main__":
    sys.exit(main())
