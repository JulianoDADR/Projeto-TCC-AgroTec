"""Calibração da sonda de umidade do solo no computador (host), sem mudar o firmware.

O firmware converte o ADC do higrômetro em porcentagem por uma reta FIXA de
fábrica (``ADC_SOLO_SECO = 1023`` → 0 % e ``ADC_SOLO_ENCHARCADO = 300`` → 100 %,
``arduino/coleta_sensores.ino`` linhas 144–145). Essa reta não é a do solo do
sítio:

    * calibração de laboratório levada ao campo erra de 0,055 a 0,191 m³/m³;
      a aferição de UM ponto no próprio campo erra de 0,006 a 0,078 m³/m³
      (sensores capacitivos de baixo custo, WUR):
      https://research.wur.nl/en/publications/field-performance-and-calibration-strategies-for-low-cost-capacit/
    * uma equação feita para um solo dificilmente serve para outro (protocolo
      Embrapa de calibração e aferição de sensores de umidade do solo):
      https://www.infoteca.cnptia.embrapa.br/infoteca/bitstream/doc/1170710/1/Protocolo-para-a-calibracao-e-a-afericao-de-sensores-de-umidade-de-solo.pdf
    * o sensor resistivo é menos exato que o capacitivo:
      https://pmc.ncbi.nlm.nih.gov/articles/PMC7014303

Como o mapeamento do firmware é linear e conhecido, o ADC médio é recuperado
EXATAMENTE da porcentagem recebida (``adc_do_percentual_firmware``) e a nova
reta é aplicada no host: dispensa protocolo serial novo.

Métodos (tabela ``calibracao_sonda``, uma linha ativa por nó):
    * ``FABRICA``: a reta do firmware (identidade);
    * ``DOIS_PONTOS``: sonda na terra SECA e depois na terra MOLHADA do próprio
      sítio (``capturar_ponto('SECO')``, ``capturar_ponto('UMIDO')``,
      ``concluir_dois_pontos``);
    * ``UM_PONTO_CAMPO``: aferição com um valor de referência medido no campo
      (ex.: método da estufa ou sonda de referência do técnico); desloca a reta
      ativa pela diferença (``aferir_um_ponto``).

``converter_umidade_solo`` é o ponto de uso (consumido pelo coletor da Frente 1
via ``importlib``): nunca lança exceção, guarda a calibração por 60 s e é a
identidade sem calibração ativa.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import statistics
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from database import CAMINHO_BANCO, MODO_CAMPO_USB, definir_configuracao, obter_conexao, obter_configuracao

registrador: logging.Logger = logging.getLogger("agroanalise.calibracao")

# Espelho do firmware (arduino/coleta_sensores.ino:144-145).
ADC_FIRMWARE_SECO: float = 1023.0
ADC_FIRMWARE_ENCHARCADO: float = 300.0

ID_NO_PADRAO: str = "principal"
METODOS_CALIBRACAO: Tuple[str, ...] = ("FABRICA", "DOIS_PONTOS", "UM_PONTO_CAMPO")
TIPOS_PONTO: Tuple[str, ...] = ("SECO", "UMIDO", "CAMPO")
# Captura de ponto: pelo menos 6 leituras na janela (30 s com o intervalo padrão de 5 s)
# e desvio-padrão de no máximo 1 ponto percentual (sonda parada no mesmo solo).
LEITURAS_MINIMAS_PONTO: int = 6
JANELA_PONTO_SEGUNDOS: int = 60
DESVIO_MAXIMO_PONTO_PP: float = 1.0
# Os dois pontos precisam estar a pelo menos 30 contagens do ADC (~4 p.p. na reta de fábrica)
# para a reta não amplificar o ruído.
SEPARACAO_MINIMA_ADC: float = 30.0
# Pontos guardados para concluir valem por 2 horas (a calibração é feita numa visita).
VALIDADE_PONTO_HORAS: float = 2.0
VALIDADE_CACHE_SEGUNDOS: float = 60.0
CHAVE_PONTOS_PENDENTES: str = "calibracao_pontos_pendentes"

_DDL_CALIBRACAO: str = """
CREATE TABLE IF NOT EXISTS calibracao_sonda (
    id_calibracao  INTEGER PRIMARY KEY AUTOINCREMENT,
    id_no          TEXT    NOT NULL DEFAULT 'principal',
    adc_seco       REAL    NOT NULL,
    adc_umido      REAL    NOT NULL,
    alvo_seco_pct  REAL    NOT NULL,
    alvo_umido_pct REAL    NOT NULL,
    metodo         TEXT    NOT NULL CHECK (metodo IN ('FABRICA','DOIS_PONTOS','UM_PONTO_CAMPO')),
    registrada_em  TEXT    NOT NULL,
    ativa          INTEGER NOT NULL DEFAULT 1 CHECK (ativa IN (0, 1)),
    observacao     TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_calibracao_sonda_no_ativa ON calibracao_sonda(id_no, ativa);
"""

_TRAVA_CACHE: threading.Lock = threading.Lock()
# (caminho, id_no) -> (instante monotônico da leitura, calibração ativa ou None)
_CACHE: Dict[Tuple[str, str], Tuple[float, Optional["CalibracaoSonda"]]] = {}


class ErroCalibracao(ValueError):
    """Calibração recusada (leituras insuficientes, instáveis ou pontos inválidos), com mensagem simples."""


@dataclass(frozen=True)
class CalibracaoSonda:
    """Reta de calibração da sonda: ``adc_seco`` → ``alvo_seco_pct`` e ``adc_umido`` → ``alvo_umido_pct``."""

    id_no: str
    adc_seco: float
    adc_umido: float
    alvo_seco_pct: float
    alvo_umido_pct: float
    metodo: str
    registrada_em: str

    def converter_adc(self, adc: float) -> float:
        """Porcentagem de água na terra para o ADC dado, limitada a 0–100."""
        inclinacao = (self.alvo_umido_pct - self.alvo_seco_pct) / (self.adc_umido - self.adc_seco)
        valor = self.alvo_seco_pct + (adc - self.adc_seco) * inclinacao
        return max(0.0, min(100.0, valor))


CALIBRACAO_FABRICA: CalibracaoSonda = CalibracaoSonda(
    ID_NO_PADRAO, ADC_FIRMWARE_SECO, ADC_FIRMWARE_ENCHARCADO, 0.0, 100.0, "FABRICA", "")


# ---------------------------------------------------------------------------
# Firmware
# ---------------------------------------------------------------------------
def adc_do_percentual_firmware(pct: float) -> float:
    """ADC médio que o firmware converteu em ``pct`` (inversão exata da reta de fábrica).

    Firmware: ``pct = (1023 - adc) * 100 / (1023 - 300)``. Logo
    ``adc = 1023 - pct * (1023 - 300) / 100``. Valores saturados em 0 ou 100 %
    voltam aos extremos 1023 e 300.
    """
    return ADC_FIRMWARE_SECO - float(pct) * (ADC_FIRMWARE_SECO - ADC_FIRMWARE_ENCHARCADO) / 100.0


def percentual_firmware_do_adc(adc: float) -> float:
    """Reta de fábrica do firmware (ADC → %), limitada a 0–100."""
    valor = (ADC_FIRMWARE_SECO - float(adc)) * 100.0 / (ADC_FIRMWARE_SECO - ADC_FIRMWARE_ENCHARCADO)
    return max(0.0, min(100.0, valor))


# ---------------------------------------------------------------------------
# Banco
# ---------------------------------------------------------------------------
def garantir_tabela(caminho_banco: Path = CAMINHO_BANCO) -> None:
    """Cria a tabela ``calibracao_sonda`` se faltar (idempotente).

    Raises:
        sqlite3.Error: banco inacessível.
    """
    conexao = obter_conexao(caminho_banco)
    try:
        conexao.executescript(_DDL_CALIBRACAO)
        conexao.commit()
    finally:
        conexao.close()


def limpar_cache() -> None:
    """Esquece as calibrações guardadas em memória (a próxima conversão relê o banco)."""
    with _TRAVA_CACHE:
        _CACHE.clear()


def _linha_para_calibracao(linha: sqlite3.Row) -> CalibracaoSonda:
    """Converte uma linha de ``calibracao_sonda`` em ``CalibracaoSonda``."""
    return CalibracaoSonda(
        id_no=str(linha["id_no"]), adc_seco=float(linha["adc_seco"]), adc_umido=float(linha["adc_umido"]),
        alvo_seco_pct=float(linha["alvo_seco_pct"]), alvo_umido_pct=float(linha["alvo_umido_pct"]),
        metodo=str(linha["metodo"]), registrada_em=str(linha["registrada_em"]),
    )


def obter_calibracao_ativa(caminho_banco: Path = CAMINHO_BANCO, id_no: str = ID_NO_PADRAO) -> Optional[CalibracaoSonda]:
    """Calibração ativa do nó (``None`` sem tabela ou sem calibração).

    Raises:
        sqlite3.Error: banco inacessível.
    """
    conexao = obter_conexao(caminho_banco)
    try:
        existe = conexao.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'calibracao_sonda'").fetchone()
        if existe is None:
            return None
        linha = conexao.execute(
            "SELECT * FROM calibracao_sonda WHERE id_no = ? AND ativa = 1 ORDER BY id_calibracao DESC LIMIT 1",
            (id_no,),
        ).fetchone()
    finally:
        conexao.close()
    return _linha_para_calibracao(linha) if linha is not None else None


def registrar_calibracao(caminho_banco: Path, calibracao: CalibracaoSonda, observacao: str = "") -> int:
    """Grava a calibração como a ÚNICA ativa do nó (as anteriores ficam no histórico, inativas).

    Raises:
        ErroCalibracao: método inválido, pontos iguais ou números não finitos.
        sqlite3.Error: falha do banco.
    """
    if calibracao.metodo not in METODOS_CALIBRACAO:
        raise ErroCalibracao(f"Método de calibração desconhecido: {calibracao.metodo}.")
    numeros = (calibracao.adc_seco, calibracao.adc_umido, calibracao.alvo_seco_pct, calibracao.alvo_umido_pct)
    if not all(math.isfinite(float(numero)) for numero in numeros):
        raise ErroCalibracao("A calibração tem números inválidos.")
    if abs(calibracao.adc_seco - calibracao.adc_umido) < 1e-6 or \
            abs(calibracao.alvo_seco_pct - calibracao.alvo_umido_pct) < 1e-6:
        raise ErroCalibracao("Os dois pontos da calibração não podem ser iguais.")
    garantir_tabela(caminho_banco)
    conexao = obter_conexao(caminho_banco)
    try:
        conexao.execute("UPDATE calibracao_sonda SET ativa = 0 WHERE id_no = ?", (calibracao.id_no,))
        cursor = conexao.execute(
            "INSERT INTO calibracao_sonda (id_no, adc_seco, adc_umido, alvo_seco_pct, alvo_umido_pct, metodo, "
            "registrada_em, ativa, observacao) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (calibracao.id_no, float(calibracao.adc_seco), float(calibracao.adc_umido),
             float(calibracao.alvo_seco_pct), float(calibracao.alvo_umido_pct), calibracao.metodo,
             calibracao.registrada_em or _agora_iso(), str(observacao or "")[:200]),
        )
        conexao.commit()
        identificador = int(cursor.lastrowid or 0)
    finally:
        conexao.close()
    limpar_cache()
    registrador.info("Calibração da sonda %s registrada (%s): ADC %.1f→%.1f %%, ADC %.1f→%.1f %%.",
                     calibracao.id_no, calibracao.metodo, calibracao.adc_seco, calibracao.alvo_seco_pct,
                     calibracao.adc_umido, calibracao.alvo_umido_pct)
    return identificador


def _agora_iso(agora_utc: Optional[datetime] = None) -> str:
    """Instante ISO UTC com ``+00:00``."""
    return (agora_utc or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def _calibracao_em_cache(caminho_banco: Path, id_no: str) -> Optional[CalibracaoSonda]:
    """Calibração ativa guardada por até ``VALIDADE_CACHE_SEGUNDOS`` (relê o banco depois)."""
    chave = (str(Path(caminho_banco)), id_no)
    agora = time.monotonic()
    with _TRAVA_CACHE:
        guardado = _CACHE.get(chave)
        if guardado is not None and agora - guardado[0] < VALIDADE_CACHE_SEGUNDOS:
            return guardado[1]
    # Banco ainda inexistente: não cria o arquivo só para descobrir que não há calibração.
    calibracao = obter_calibracao_ativa(caminho_banco, id_no) if Path(caminho_banco).exists() else None
    with _TRAVA_CACHE:
        _CACHE[chave] = (agora, calibracao)
    return calibracao


def converter_umidade_solo(umidade_solo_firmware: Optional[float], caminho_banco: Path = CAMINHO_BANCO,
                           id_no: str = ID_NO_PADRAO) -> Optional[float]:
    """Umidade do solo calibrada para o solo do sítio a partir da porcentagem do firmware.

    Interpola linearmente entre ``(adc_seco → alvo_seco_pct)`` e
    ``(adc_umido → alvo_umido_pct)`` da calibração ativa, com o ADC recuperado
    da reta de fábrica, e limita a 0–100. Sem calibração ativa (ou com a de
    fábrica) é a identidade. NUNCA lança exceção: entrada inválida, tabela
    ausente ou erro de banco devolvem a própria entrada.
    """
    if umidade_solo_firmware is None:
        return None
    try:
        valor = float(umidade_solo_firmware)
    except (TypeError, ValueError):
        return umidade_solo_firmware
    if not math.isfinite(valor):
        return umidade_solo_firmware
    try:
        calibracao = _calibracao_em_cache(caminho_banco, id_no)
    except Exception as erro:  # banco travado, corrompido ou ausente: segue sem calibração
        registrador.warning("Calibração da sonda indisponível (%s); usando a leitura do firmware.", erro)
        return umidade_solo_firmware
    if calibracao is None or calibracao.metodo == "FABRICA":
        return umidade_solo_firmware
    try:
        return round(calibracao.converter_adc(adc_do_percentual_firmware(valor)), 2)
    except (ArithmeticError, ValueError):
        return umidade_solo_firmware


# ---------------------------------------------------------------------------
# Captura de pontos e assistente
# ---------------------------------------------------------------------------
def _valores_da_janela(caminho_banco: Path, janela_s: int, agora_utc: datetime) -> List[float]:
    """Porcentagens do FIRMWARE das leituras de campo da janela (coluna ``umidade_solo_firmware`` ou a bruta)."""
    inicio = _agora_iso(agora_utc - timedelta(seconds=max(1, int(janela_s))))
    conexao = obter_conexao(caminho_banco)
    try:
        colunas = {str(linha[1]) for linha in conexao.execute("PRAGMA table_info(historico_leituras)")}
        expressao = ("COALESCE(umidade_solo_firmware, umidade_solo_bruta)" if "umidade_solo_firmware" in colunas
                     else "umidade_solo_bruta")
        filtro = "instante_utc >= ?" if "instante_utc" in colunas else "data_hora >= ?"
        parametro = inicio if "instante_utc" in colunas else (
            (agora_utc - timedelta(seconds=janela_s)).astimezone().strftime("%Y-%m-%d %H:%M:%S"))
        linhas = conexao.execute(
            f"SELECT {expressao} FROM historico_leituras WHERE origem = ? AND {filtro} ORDER BY id_leitura",
            (MODO_CAMPO_USB, parametro),
        ).fetchall()
    finally:
        conexao.close()
    return [float(linha[0]) for linha in linhas if linha[0] is not None and math.isfinite(float(linha[0]))]


def _ler_pendentes(caminho_banco: Path) -> Dict[str, Dict[str, Any]]:
    """Pontos capturados e ainda não usados (JSON em ``configuracao_sistema``)."""
    texto = obter_configuracao(CHAVE_PONTOS_PENDENTES, "", caminho_banco=caminho_banco) or ""
    try:
        dados = json.loads(texto) if texto else {}
    except ValueError:
        return {}
    return dados if isinstance(dados, dict) else {}


def _gravar_pendentes(caminho_banco: Path, pendentes: Dict[str, Dict[str, Any]]) -> None:
    """Guarda os pontos capturados (``{}`` limpa)."""
    definir_configuracao(CHAVE_PONTOS_PENDENTES, json.dumps(pendentes, ensure_ascii=False) if pendentes else "",
                         caminho_banco=caminho_banco)


def capturar_ponto(caminho_banco: Path, tipo: str, janela_s: int = JANELA_PONTO_SEGUNDOS,
                   desvio_maximo_pp: float = DESVIO_MAXIMO_PONTO_PP,
                   agora_utc: Optional[datetime] = None) -> Dict[str, Any]:
    """Captura um ponto (SECO, UMIDO ou CAMPO) pela média das leituras de campo da última janela.

    Usa ``umidade_solo_firmware`` (ou ``umidade_solo_bruta``) das leituras
    ``CAMPO_USB`` dos últimos ``janela_s`` segundos. O ponto fica guardado para
    ``concluir_dois_pontos``/``aferir_um_ponto``.

    Returns:
        ``{"tipo", "percentual_firmware", "adc", "leituras", "desvio_pp", "capturado_em"}``.

    Raises:
        ErroCalibracao: tipo inválido, menos de 6 leituras ou desvio-padrão acima do limite.
    """
    codigo = str(tipo or "").strip().upper()
    if codigo not in TIPOS_PONTO:
        raise ErroCalibracao("Tipo de ponto inválido: use SECO, UMIDO ou CAMPO.")
    agora = (agora_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    valores = _valores_da_janela(caminho_banco, janela_s, agora)
    if len(valores) < LEITURAS_MINIMAS_PONTO:
        raise ErroCalibracao(f"Poucas medições da sonda ({len(valores)} de {LEITURAS_MINIMAS_PONTO}). "
                             "Confira se o aparelho está ligado e espere um minuto.")
    desvio = statistics.pstdev(valores)
    if desvio > float(desvio_maximo_pp):
        raise ErroCalibracao(f"A medição ainda está variando ({desvio:.1f} pontos). "
                             "Deixe a sonda parada na terra e tente de novo em um minuto.")
    media = statistics.fmean(valores)
    ponto = {
        "tipo": codigo,
        "percentual_firmware": round(media, 2),
        "adc": round(adc_do_percentual_firmware(media), 2),
        "leituras": len(valores),
        "desvio_pp": round(desvio, 2),
        "capturado_em": _agora_iso(agora),
    }
    pendentes = _ler_pendentes(caminho_banco)
    pendentes[codigo] = ponto
    _gravar_pendentes(caminho_banco, pendentes)
    return ponto


def _ponto_valido(ponto: Optional[Dict[str, Any]], agora_utc: datetime) -> bool:
    """``True`` se o ponto existe e foi capturado há menos de ``VALIDADE_PONTO_HORAS``."""
    if not ponto:
        return False
    try:
        capturado = datetime.fromisoformat(str(ponto.get("capturado_em")))
        float(ponto["adc"])
    except (KeyError, TypeError, ValueError):
        return False
    if capturado.tzinfo is None:
        capturado = capturado.replace(tzinfo=timezone.utc)
    return agora_utc - capturado <= timedelta(hours=VALIDADE_PONTO_HORAS)


def concluir_dois_pontos(caminho_banco: Path, id_no: str = ID_NO_PADRAO, alvo_seco_pct: float = 0.0,
                         alvo_umido_pct: float = 100.0, observacao: str = "",
                         agora_utc: Optional[datetime] = None) -> CalibracaoSonda:
    """Fecha a calibração de dois pontos com os pontos SECO e UMIDO capturados (há até 2 h).

    Raises:
        ErroCalibracao: falta um ponto, pontos vencidos ou próximos demais.
    """
    agora = (agora_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    pendentes = _ler_pendentes(caminho_banco)
    seco, umido = pendentes.get("SECO"), pendentes.get("UMIDO")
    if not _ponto_valido(seco, agora) or not _ponto_valido(umido, agora):
        raise ErroCalibracao("Falta medir a terra seca e a terra molhada (cada uma vale por 2 horas).")
    adc_seco, adc_umido = float(seco["adc"]), float(umido["adc"])  # type: ignore[index]
    if adc_seco - adc_umido < SEPARACAO_MINIMA_ADC:
        raise ErroCalibracao("A terra seca e a molhada deram quase o mesmo valor. Molhe bem a terra e meça de novo.")
    calibracao = CalibracaoSonda(id_no, adc_seco, adc_umido, float(alvo_seco_pct), float(alvo_umido_pct),
                                 "DOIS_PONTOS", _agora_iso(agora))
    registrar_calibracao(caminho_banco, calibracao, observacao)
    pendentes.pop("SECO", None)
    pendentes.pop("UMIDO", None)
    _gravar_pendentes(caminho_banco, pendentes)
    return calibracao


def aferir_um_ponto(caminho_banco: Path, valor_referencia_pct: float, id_no: str = ID_NO_PADRAO,
                    observacao: str = "", agora_utc: Optional[datetime] = None) -> CalibracaoSonda:
    """Aferição de UM ponto no campo: desloca a reta ativa para a leitura atual bater com a referência.

    A leitura atual é o ponto CAMPO capturado há até 2 h ou, sem ele, uma
    captura feita agora. Os dois alvos da reta ativa (ou da de fábrica) andam a
    mesma diferença ``referência − leitura calibrada``.

    Raises:
        ErroCalibracao: referência fora de 0–100 ou leituras insuficientes/instáveis.
    """
    try:
        referencia = float(valor_referencia_pct)
    except (TypeError, ValueError):
        raise ErroCalibracao("Informe a umidade de referência em porcentagem (0 a 100).") from None
    if not math.isfinite(referencia) or not 0.0 <= referencia <= 100.0:
        raise ErroCalibracao("A umidade de referência precisa ficar entre 0 e 100 %.")
    agora = (agora_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    pendentes = _ler_pendentes(caminho_banco)
    ponto = pendentes.get("CAMPO")
    if not _ponto_valido(ponto, agora):
        ponto = capturar_ponto(caminho_banco, "CAMPO", agora_utc=agora)
    base = obter_calibracao_ativa(caminho_banco, id_no) or CALIBRACAO_FABRICA
    leitura = base.converter_adc(float(ponto["adc"]))  # type: ignore[index]
    diferenca = referencia - leitura
    calibracao = CalibracaoSonda(id_no, base.adc_seco, base.adc_umido, base.alvo_seco_pct + diferenca,
                                 base.alvo_umido_pct + diferenca, "UM_PONTO_CAMPO", _agora_iso(agora))
    registrar_calibracao(caminho_banco, calibracao,
                         observacao or f"Referência {referencia:.1f} %, leitura {leitura:.1f} % ({diferenca:+.1f}).")
    pendentes = _ler_pendentes(caminho_banco)
    pendentes.pop("CAMPO", None)
    _gravar_pendentes(caminho_banco, pendentes)
    return calibracao


def voltar_para_fabrica(caminho_banco: Path, id_no: str = ID_NO_PADRAO,
                        agora_utc: Optional[datetime] = None) -> CalibracaoSonda:
    """Volta à reta de fábrica do firmware (registrada como ``FABRICA``, que é a identidade)."""
    calibracao = CalibracaoSonda(id_no, ADC_FIRMWARE_SECO, ADC_FIRMWARE_ENCHARCADO, 0.0, 100.0, "FABRICA",
                                 _agora_iso(agora_utc))
    registrar_calibracao(caminho_banco, calibracao, "Volta à reta de fábrica do firmware.")
    return calibracao


def dias_desde_calibracao(caminho_banco: Path = CAMINHO_BANCO, id_no: str = ID_NO_PADRAO,
                          agora_utc: Optional[datetime] = None) -> Optional[int]:
    """Dias desde a calibração ativa feita no sítio (``None`` sem calibração ou só a de fábrica).

    Nunca lança exceção (erro de banco → ``None``).
    """
    try:
        calibracao = obter_calibracao_ativa(caminho_banco, id_no)
    except (sqlite3.Error, OSError):
        return None
    if calibracao is None or calibracao.metodo == "FABRICA":
        return None
    try:
        registrada = datetime.fromisoformat(calibracao.registrada_em)
    except ValueError:
        return None
    if registrada.tzinfo is None:
        registrada = registrada.replace(tzinfo=timezone.utc)
    agora = (agora_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return max(0, (agora - registrada).days)


def estado_calibracao(caminho_banco: Path = CAMINHO_BANCO, id_no: str = ID_NO_PADRAO,
                      agora_utc: Optional[datetime] = None) -> Dict[str, Any]:
    """Resumo para a API: calibração ativa, método, dias desde ela e pontos pendentes. Nunca lança exceção."""
    try:
        ativa = obter_calibracao_ativa(caminho_banco, id_no)
        pendentes = _ler_pendentes(caminho_banco)
    except (sqlite3.Error, OSError) as erro:
        registrador.warning("Estado da calibração indisponível: %s", erro)
        ativa, pendentes = None, {}
    return {
        "metodo": ativa.metodo if ativa is not None else "FABRICA",
        "ativa": asdict(ativa) if ativa is not None else None,
        "dias_desde_calibracao": dias_desde_calibracao(caminho_banco, id_no, agora_utc),
        "pontos_pendentes": pendentes,
    }
