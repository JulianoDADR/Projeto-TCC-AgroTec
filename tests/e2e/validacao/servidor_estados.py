"""Servidor da suíte de validação UX: o app do AgroAnálise com rotas extras para FORÇAR estados.

Só neste processo (nunca no servidor do sítio) são registradas:

    * ``POST /__ux/estado/<nome>``: troca o banco em uso por um banco temporário
      novo, montado com a leitura (e a configuração) de um estado da matriz
      (``ESTADOS``); responde ``{"estado", "banco", "cliente"}``, em que
      ``cliente`` diz o que o navegador ainda precisa fazer depois da 1ª carga
      (ficar sem rede, provocar 503, trocar a cultura...);
    * ``POST /__ux/falha`` com ``{"status": 503}`` faz ``/api/status`` responder
      com esse erro 5xx; ``{"pagina": true}`` faz a própria página sair sem o
      estado embutido; ``{}`` desliga as duas falhas;
    * ``POST /__ux/cultura/<codigo>``: troca a cultura como outro celular faria;
    * ``GET /__ux/estados``: a lista dos estados com a descrição de cada um.

A demonstração GEADA e a troca de cultura vêm de ``OUTRO_APARELHO`` (127.0.0.2),
como a banca de outro celular; o navegador da suíte é 127.0.0.1.

Os estados são montados com as funções públicas de ``database``,
``gerador_dataset``, ``regras_agronomicas`` e ``prognostico`` da v3 (as mesmas
assinaturas garantidas pela frente B), a partir do harness da auditoria
"componentes-estados" do comitê de UX da v4.

Uso (a partir da raiz do projeto)::

    python tests/e2e/validacao/servidor_estados.py <porta>

A porta 5000 (a do sítio) é recusada. Tudo é gravado numa pasta temporária,
apagada ao sair; ``data/`` e ``models/`` do projeto não são tocados.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# AGRO_UX_PROJETO aponta para outra cópia do projeto (por exemplo a v3, para a linha de base).
RAIZ_PROJETO: Path = Path(os.environ.get("AGRO_UX_PROJETO") or Path(__file__).resolve().parents[3]).resolve()
PASTA_SRC: Path = RAIZ_PROJETO / "src"
PORTA_PROIBIDA: int = 5000

PASTA_TEMPORARIA: Path = Path(tempfile.mkdtemp(prefix="agro_ux4_"))
atexit.register(shutil.rmtree, PASTA_TEMPORARIA, True)
# O app lê AGRO_BANCO ao ser importado: aponta para a pasta temporária antes do import.
os.environ["AGRO_BANCO"] = str(PASTA_TEMPORARIA / "inicial.db")
if str(PASTA_SRC) not in sys.path:
    sys.path.insert(0, str(PASTA_SRC))

import app as modulo_app  # noqa: E402
import prognostico  # noqa: E402
from database import (  # noqa: E402
    RegistroLeitura,
    buscar_parametros_cultura,
    definir_configuracoes,
    inicializar_banco,
    inserir_leitura,
    semear_configuracoes_v3,
)
from flask import Response, jsonify, request  # noqa: E402
from gerador_dataset import calcular_features, gerar_mensagem_fisiologica, gerar_mensagem_orientacao  # noqa: E402
from regras_agronomicas import ConfiguracaoTermica, ResumoTermico, avaliar_status_campo  # noqa: E402

CULTURA: str = "cafe_arabica"
# Endereço de "outro celular" (a banca, ou quem troca a cultura): o navegador da suíte é 127.0.0.1.
OUTRO_APARELHO: str = "127.0.0.2"
NOME_PROPRIEDADE: str = "Sítio Boa Esperança"
BANCO_BASE: Path = PASTA_TEMPORARIA / "base.db"
inicializar_banco(BANCO_BASE)
semear_configuracoes_v3(BANCO_BASE)

# Falha provocada em /api/status (código 5xx) e na página (sem estado embutido).
FALHA: Dict[str, Any] = {"status": None, "pagina": False}
_CONTADOR: Dict[str, int] = {"bancos": 0}


@dataclass(frozen=True)
class Estado:
    """Um estado da matriz: como montar o banco e o que o navegador faz depois da 1ª carga."""

    nome: str
    descricao: str
    montar: Callable[[Path], None]
    cliente: str = ""


def _agora_utc() -> datetime:
    """Instante atual em UTC (com fuso)."""
    return datetime.now(timezone.utc)


def _novo_banco() -> Path:
    """Cópia nova do banco base (cada estado começa limpo)."""
    _CONTADOR["bancos"] += 1
    destino = PASTA_TEMPORARIA / f"estado_{_CONTADOR['bancos']}.db"
    shutil.copy(BANCO_BASE, destino)
    return destino


def gravar_leitura(banco: Path, temperatura: float, solo: float, status: int, mensagem: str, *,
                   segundos_atras: float = 2.0, origem: str = "CAMPO_USB", estado_sensor: str = "OK",
                   motivo_regra: str = "", anomalia: bool = False, cultura: str = CULTURA) -> int:
    """Grava uma leitura completa (bruta = tratada) e devolve o ``id_leitura``.

    Args:
        banco: banco temporário do estado.
        temperatura: temperatura do ar (°C).
        solo: umidade do solo (%).
        status: 0 verde, 1 amarelo, 2 vermelho (regra = modelo = alerta).
        mensagem: mensagem falada gravada com a leitura.
        segundos_atras: idade da leitura (negativa = leitura "do futuro").
        origem: ``CAMPO_USB``, ``SIMULACAO`` ou ``DEMONSTRACAO``.
        estado_sensor: ``OK`` ou um diagnóstico de defeito.
        motivo_regra: motivo da regra fisiológica (vazio na regra instantânea).
        anomalia: marca a leitura como anômala (salto).
        cultura: código da cultura.
    """
    parametros = buscar_parametros_cultura(cultura, caminho_banco=banco)
    instante = _agora_utc() - timedelta(seconds=segundos_atras)
    deficit, desvio = calcular_features(temperatura, solo, parametros)
    return inserir_leitura(RegistroLeitura(
        data_hora=instante.astimezone().replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S"),
        codigo_cultura=cultura, origem=origem, temperatura_ar_bruta=temperatura, umidade_ar_bruta=65.0,
        umidade_solo_bruta=solo, temperatura_ar=temperatura, umidade_ar=65.0, umidade_solo=solo,
        deficit_hidrico_solo=deficit, desvio_termico=desvio, status_regra=status, status_predito=status,
        status_alerta=status, probabilidade_predicao=0.9, flag_anomalia=int(anomalia),
        descricao_anomalia="salto" if anomalia else "", mensagem_voz=mensagem, versao_modelo="REGRAS",
        estado_sensor=estado_sensor, instante_utc=instante.isoformat(timespec="seconds"),
        motivo_regra=motivo_regra, base_temperatura="media_24h" if motivo_regra else ""), caminho_banco=banco)


def mensagem_instantanea(banco: Path, temperatura: float, solo: float, status: int) -> str:
    """Mensagem da regra instantânea (v2) para a leitura."""
    parametros = buscar_parametros_cultura(CULTURA, caminho_banco=banco)
    deficit, desvio = calcular_features(temperatura, solo, parametros)
    return gerar_mensagem_orientacao(status, deficit, desvio, parametros, temperatura, solo)


def _leitura_instantanea(temperatura: float, solo: float, status: int, **opcoes: Any) -> Callable[[Path], None]:
    """Montador de estado com uma leitura da regra instantânea."""
    def montar(banco: Path) -> None:
        gravar_leitura(banco, temperatura, solo, status, mensagem_instantanea(banco, temperatura, solo, status),
                       **opcoes)
    return montar


def _leitura_fisiologica(temperatura: float, solo: float, mes: int, media_24h: float, maxima_ontem: float, *,
                         seca: bool = False, florada_hoje: bool = False) -> Callable[[Path], None]:
    """Montador de estado com a regra fisiológica (geada, florada, dia frio, seca programada)."""
    def montar(banco: Path) -> None:
        configuracoes: Dict[str, Any] = {}
        if seca:
            configuracoes["seca_programada_ativa"] = True
        if florada_hoje:
            configuracoes["data_florada_principal"] = datetime.now().strftime("%Y-%m-%d")
        if configuracoes:
            definir_configuracoes(configuracoes, caminho_banco=banco)
        parametros = buscar_parametros_cultura(CULTURA, caminho_banco=banco)
        avaliacao = avaliar_status_campo(
            temperatura, solo, parametros, ResumoTermico(media_24h, 24, maxima_ontem, maxima_ontem),
            ConfiguracaoTermica(modo="fisiologica", seca_programada_ativa=seca), datetime(2026, mes, 15, 14))
        deficit, _ = calcular_features(temperatura, solo, parametros)
        mensagem = gerar_mensagem_fisiologica(avaliacao, deficit, parametros, temperatura, solo)
        gravar_leitura(banco, temperatura, solo, avaliacao.status, mensagem, motivo_regra=avaliacao.motivo)
    return montar


def _antecipacao(motivo: str, prazo_horas: float, probabilidade: float) -> Callable[[Path], None]:
    """Montador do Amarelo antecipado (prognóstico), com a mensagem montada como o app monta."""
    def montar(banco: Path) -> None:
        parametros = buscar_parametros_cultura(CULTURA, caminho_banco=banco)
        temperatura, solo = 20.0, 64.0 if motivo == "SECA_CHEGANDO" else 70.0
        deficit, desvio = calcular_features(temperatura, solo, parametros)
        registro = RegistroLeitura(
            data_hora=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), codigo_cultura=CULTURA, origem="CAMPO_USB",
            temperatura_ar_bruta=temperatura, umidade_ar_bruta=65.0, umidade_solo_bruta=solo,
            temperatura_ar=temperatura, umidade_ar=65.0, umidade_solo=solo, deficit_hidrico_solo=deficit,
            desvio_termico=desvio, status_regra=0, status_predito=1, status_alerta=1, probabilidade_predicao=0.9,
            flag_anomalia=0, descricao_anomalia="", mensagem_voz="", versao_modelo="REGRAS")
        explicacao = {"motivo_principal": motivo, "prazo_horas": prazo_horas, "horas_em_estresse": 0.0}
        mensagem = prognostico._mensagem_antecipada(registro, parametros, explicacao, datetime.now(), probabilidade)
        identificador = gravar_leitura(banco, temperatura, solo, 1, mensagem)
        prognostico.gravar_analise_antecipacao(identificador, prognostico.ResultadoAntecipacao(
            probabilidade_risco_futuro=probabilidade,
            horas_ate_secar=prazo_horas if motivo == "SECA_CHEGANDO" else None,
            temperatura_minima_prevista=1.5 if motivo == "GEADA_NOITE" else None, motivo_principal=motivo,
            prazo_horas=prazo_horas, horas_em_estresse=0.0, status_antecipado=1, versao_prognostico="ux4"), banco)
    return montar


def _simulacao(temperatura: float, solo: float, status: int) -> Callable[[Path], None]:
    """Montador do Modo Simulação (dados de teste, aparelho do campo desligado)."""
    def montar(banco: Path) -> None:
        definir_configuracoes({"modo_operacao": "SIMULACAO", "estado_hardware": "DESCONECTADO"}, caminho_banco=banco)
        gravar_leitura(banco, temperatura, solo, status, mensagem_instantanea(banco, temperatura, solo, status),
                       origem="SIMULACAO")
    return montar


def _coletor_parado(banco: Path) -> None:
    """Coletor sem batimento há 2 h e última leitura (Vermelha) de 2 h atrás."""
    definir_configuracoes({"coletor_batimento_em": (_agora_utc() - timedelta(hours=2)).isoformat(timespec="seconds")},
                          caminho_banco=banco)
    gravar_leitura(banco, 20.0, 30.0, 2, mensagem_instantanea(banco, 20.0, 30.0, 2), segundos_atras=7200)


def _sensor_defeito(banco: Path) -> None:
    """Sonda de solo com defeito (espeto solto)."""
    gravar_leitura(banco, 20.0, 5.0, 1, "Atenção. O espeto da terra pode estar solto. Confira o aparelho.",
                   estado_sensor="DEFEITO")


def _sem_leitura(banco: Path) -> None:
    """Banco sem nenhuma leitura (aparelho recém-instalado)."""
    del banco


def _demonstracao_geada(banco: Path) -> None:
    """Leitura Verde real e a demonstração GEADA ligada pela banca (vista por um celular comum)."""
    gravar_leitura(banco, 20.0, 70.0, 0, mensagem_instantanea(banco, 20.0, 70.0, 0))
    modulo_app.app.config["CAMINHO_BANCO"] = banco
    resposta = modulo_app.app.test_client().post("/api/demonstracao", json={"cenario": "GEADA"},
                                                 environ_base={"REMOTE_ADDR": OUTRO_APARELHO})
    if resposta.status_code != 200:
        raise RuntimeError(f"A demonstração GEADA não ligou: HTTP {resposta.status_code}")


ESTADOS: Dict[str, Estado] = {estado.nome: estado for estado in (
    Estado("verde", "Leitura viva Verde (20 °C, terra a 70 %).", _leitura_instantanea(20.0, 70.0, 0)),
    Estado("amarelo", "Leitura viva Amarela (terra a 57 %).", _leitura_instantanea(20.0, 57.0, 1)),
    Estado("vermelho", "Leitura viva Vermelha (terra a 30 %).", _leitura_instantanea(20.0, 30.0, 2)),
    Estado("vermelho_geada", "Geada agora (3 °C em julho), regra fisiológica.",
           _leitura_fisiologica(3.0, 70.0, 7, 12.0, 20.0)),
    Estado("florada_calor", "Calor de 35 °C com a florada de hoje.",
           _leitura_fisiologica(35.0, 70.0, 10, 24.0, 33.0, florada_hoje=True)),
    Estado("dia_frio", "Dia frio sem dano (13 °C).", _leitura_fisiologica(13.0, 70.0, 7, 14.0, 20.0)),
    Estado("seca_programada", "Seca programada ligada com terra a 52 %.",
           _leitura_fisiologica(20.0, 52.0, 7, 20.0, 22.0, seca=True)),
    Estado("encharcado", "Terra encharcada (97 %).", _leitura_instantanea(20.0, 97.0, 2)),
    Estado("antecipa_seca", "Amarelo antecipado: a terra seca em 14 h.", _antecipacao("SECA_CHEGANDO", 14.0, 0.85)),
    Estado("antecipa_geada", "Amarelo antecipado: geada à noite.", _antecipacao("GEADA_NOITE", 9.0, 0.6)),
    Estado("antecipa_calor", "Amarelo antecipado: calor à tarde.", _antecipacao("CALOR_TARDE", 4.0, 0.9)),
    Estado("sensor_defeito", "Sonda de solo com defeito.", _sensor_defeito),
    Estado("leitura_antiga", "Última leitura (Vermelha) de 2 h atrás.",
           _leitura_instantanea(20.0, 30.0, 2, segundos_atras=7200)),
    Estado("coletor_parado", "Coletor parado há 2 h.", _coletor_parado),
    Estado("relogio_errado", "Leitura com relógio 3 dias no futuro.",
           _leitura_instantanea(20.0, 70.0, 0, segundos_atras=-3 * 86400)),
    Estado("simulacao", "Modo Simulação com leitura Vermelha de teste.", _simulacao(20.0, 30.0, 2)),
    Estado("simulacao_verde", "Modo Simulação com leitura Verde de teste.", _simulacao(20.0, 70.0, 0)),
    Estado("anomalia", "Leitura Amarela marcada como anômala.", _leitura_instantanea(20.0, 57.0, 1, anomalia=True)),
    Estado("sem_leitura", "Banco sem leitura.", _sem_leitura),
    Estado("demonstracao_GEADA", "Demonstração GEADA da banca vista por um celular comum.", _demonstracao_geada),
    Estado("troca_cultura", "Verde; depois da 1ª carga outro aparelho troca a cultura para café conilon.",
           _leitura_instantanea(20.0, 70.0, 0), cliente="trocar_cultura"),
    Estado("banca", "Verde visto com ?banca=1 (painel da banca aberto).", _leitura_instantanea(20.0, 70.0, 0),
           cliente="banca"),
    # Estados de rede: o banco é o de um estado vivo; o navegador provoca a falha depois da 1ª carga.
    Estado("offline", "Vermelho; depois da 1ª carga o celular fica sem rede.", _leitura_instantanea(20.0, 30.0, 2),
           cliente="sem_rede"),
    Estado("erro_503", "Verde; depois da 1ª carga /api/status responde 503.", _leitura_instantanea(20.0, 70.0, 0),
           cliente="erro_503"),
    Estado("primeira_503", "1ª carga sem estado embutido e /api/status com 503.", _leitura_instantanea(20.0, 70.0, 0),
           cliente="primeira_503"),
    Estado("sem_voz", "Vermelho num celular sem voz em português.", _leitura_instantanea(20.0, 30.0, 2),
           cliente="sem_voz"),
)}


def _limpar_limitadores() -> None:
    """Zera os limitadores de PIN, Feito, ajuda e eventos (cada estado começa sem bloqueio)."""
    limitador_pin = getattr(modulo_app, "_LIMITADOR_PIN", None)
    if limitador_pin is not None:
        limitador_pin.limpar("127.0.0.1")
    for nome in ("_LIMITADOR_FEITO", "_LIMITADOR_AJUDA", "_LIMITADOR_EVENTOS"):
        limitador = getattr(modulo_app, nome, None)
        if limitador is not None:
            limitador.limpar()


def configurar_estado(nome: str) -> Dict[str, Any]:
    """Monta o banco do estado ``nome``, aponta o app para ele e desliga as falhas provocadas.

    Raises:
        KeyError: estado desconhecido.
    """
    estado = ESTADOS[nome]
    banco = _novo_banco()
    definir_configuracoes({"coletor_batimento_em": _agora_utc().isoformat(timespec="seconds"),
                           "modo_operacao": "CAMPO_USB", "estado_hardware": "CONECTADO",
                           "nome_propriedade": NOME_PROPRIEDADE}, caminho_banco=banco)
    estado.montar(banco)
    modulo_app.app.config["CAMINHO_BANCO"] = banco
    _limpar_limitadores()
    FALHA["status"] = None
    FALHA["pagina"] = estado.cliente == "primeira_503"
    if estado.cliente == "primeira_503":
        FALHA["status"] = 503
    return {"estado": nome, "banco": str(banco), "cliente": estado.cliente}


@modulo_app.app.post("/__ux/estado/<nome>")
def rota_ux_estado(nome: str) -> Tuple[Response, int]:
    """Troca o estado em uso (404 se o nome não existir)."""
    if nome not in ESTADOS:
        return jsonify({"erro": f"Estado desconhecido: {nome}", "estados": sorted(ESTADOS)}), 404
    return jsonify(configurar_estado(nome)), 200


@modulo_app.app.post("/__ux/cultura/<codigo>")
def rota_ux_cultura(codigo: str) -> Tuple[Response, int]:
    """Troca a cultura como OUTRO aparelho faria (``POST /api/cultura`` vindo de ``OUTRO_APARELHO``)."""
    resposta = modulo_app.app.test_client().post("/api/cultura", json={"codigo": codigo},
                                                 environ_base={"REMOTE_ADDR": OUTRO_APARELHO})
    return jsonify(resposta.get_json(silent=True) or {}), resposta.status_code


@modulo_app.app.get("/__ux/estados")
def rota_ux_estados() -> Response:
    """Lista os estados, com a descrição e a ação do navegador."""
    return jsonify([{"nome": e.nome, "descricao": e.descricao, "cliente": e.cliente} for e in ESTADOS.values()])


@modulo_app.app.post("/__ux/falha")
def rota_ux_falha() -> Tuple[Response, int]:
    """Liga (``{"status": 503}``, ``{"pagina": true}``) ou desliga (``{}``) as falhas provocadas."""
    corpo = request.get_json(silent=True) or {}
    status = corpo.get("status")
    if status is not None and (not isinstance(status, int) or not 500 <= status <= 599):
        return jsonify({"erro": "Use um status 5xx (por exemplo 503)."}), 400
    FALHA["status"] = status
    FALHA["pagina"] = bool(corpo.get("pagina"))
    return jsonify(FALHA), 200


@modulo_app.app.before_request
def injetar_falha() -> Optional[Tuple[Response, int]]:
    """Responde ``/api/status`` com o erro 5xx provocado."""
    if request.path == "/api/status" and FALHA["status"]:
        return jsonify({"erro": "O sistema teve um problema. Tente de novo em instantes."}), int(FALHA["status"])
    return None


_MONTAR_STATUS_ORIGINAL: Callable[..., Dict[str, Any]] = modulo_app.montar_status


def montar_status_com_falha(*argumentos: Any, **opcoes: Any) -> Dict[str, Any]:
    """``app.montar_status``; levanta erro na página quando a falha ``pagina`` está ligada."""
    if FALHA["pagina"] and request and request.path == "/":
        raise RuntimeError("Banco indisponível (falha provocada pela suíte de UX).")
    return _MONTAR_STATUS_ORIGINAL(*argumentos, **opcoes)


modulo_app.montar_status = montar_status_com_falha  # type: ignore[assignment]


def ler_porta(argumentos: List[str]) -> int:
    """Porta da linha de comando (1024–65535, nunca 5000).

    Raises:
        SystemExit: porta ausente, inválida ou a do sítio.
    """
    if len(argumentos) != 2 or not argumentos[1].isdigit():
        raise SystemExit("Uso: python tests/e2e/validacao/servidor_estados.py <porta>")
    porta = int(argumentos[1])
    if porta == PORTA_PROIBIDA or not 1024 <= porta <= 65535:
        raise SystemExit(f"Porta {porta} recusada: use 1024–65535, nunca {PORTA_PROIBIDA} (a do sítio).")
    return porta


def principal() -> None:
    """Sobe o app (waitress, só em 127.0.0.1) já no estado Verde."""
    porta = ler_porta(sys.argv)
    configurar_estado("verde")
    from waitress import serve
    print(f"servidor_estados: http://127.0.0.1:{porta} ({len(ESTADOS)} estados; banco em {PASTA_TEMPORARIA})",
          flush=True)
    serve(modulo_app.app, host="127.0.0.1", port=porta, threads=8, _quiet=True)


if __name__ == "__main__":
    principal()
