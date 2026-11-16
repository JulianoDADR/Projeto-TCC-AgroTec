"""Coerência cor × chip × título × voz × pictograma × causa em cada estado da matriz (suíte UX v4).

Lê o ``medidas_node.json`` gravado por ``matriz_estados.js medir`` (grupo ``grade``):
para cada estado, a 360×740, o retrato da tela (``data-estado``, chip, título,
``li[data-acao]``, causa, "Feito"), o JSON de ``/api/status`` e a fala ouvida ao
tocar no botão de voz. Confere, com as funções do próprio projeto
(``acoes_coerentes_com_mensagem``, ``causa_coerente_com_mensagem`` e, quando
existir, ``acoes_cobrem_mensagem`` da frente B):

    * a cor da tela é a da API (ou "neutro" quando os dados são de teste);
    * o chip e o título não dizem outra cor nem o contrário do estado;
    * a voz abre com "Sinal <cor>." (ou "Atenção." em teste, antiga e sem leitura);
    * os pictogramas são os da API, estão na voz, e toda ação pedida na voz tem pictograma;
    * a causa aparece na voz (curta ou longa);
    * "Feito" não aparece com dado que não vale (teste ou leitura antiga).

Confere também o contrato do retorno da frente B: o desfazer é aceito pelo
servidor até 120 s depois do toque e recusado depois disso (banco temporário).

Uso (a partir da raiz do projeto)::

    python tests/e2e/validacao/coerencia.py <pasta_do_relatorio>

Grava ``<pasta>/coerencia.json`` com ``indicadores`` (números comparados com
``metas_v4.json``), ``detalhes`` e ``estados``.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

# AGRO_UX_PROJETO aponta para outra cópia do projeto (por exemplo a v3, para a linha de base).
RAIZ_PROJETO: Path = Path(os.environ.get("AGRO_UX_PROJETO") or Path(__file__).resolve().parents[3]).resolve()
PASTA_SRC: Path = RAIZ_PROJETO / "src"
if str(PASTA_SRC) not in sys.path:
    sys.path.insert(0, str(PASTA_SRC))

import acoes_orientacao as ao  # noqa: E402

CORES: Dict[str, str] = {"verde": "Verde", "amarelo": "Amarelo", "vermelho": "Vermelho"}
ABERTURA: Dict[str, str] = {"verde": "Sinal verde.", "amarelo": "Sinal amarelo.", "vermelho": "Sinal vermelho."}
# Palavras de título que contradizem cada cor da lâmpada.
TITULO_CONTRARIO: Dict[str, Sequence[str]] = {
    "verde": ("perigo", "urgente", "atenção"),
    "amarelo": ("tudo certo", "tudo bem"),
    "vermelho": ("tudo certo", "tudo bem"),
}
CONFIANCA_ANTIGA: Sequence[str] = ("antiga", "sem-ligacao", "erro-servidor")
CONFIANCA_TESTE: Sequence[str] = ("teste", "demonstracao")
VIEWPORT_COERENCIA: str = "360x740"
# Estados da matriz em que a cor mostrada é a ÚLTIMA conhecida (vale como leitura antiga).
ESTADOS_ANTIGOS: Sequence[str] = ("leitura_antiga", "coletor_parado", "relogio_errado", "offline", "erro_503")


def _cobertura_por_palavras(acoes: Sequence[str], mensagem: str) -> List[str]:
    """Ações pedidas na mensagem sem pictograma (palavras-chave da v3, quando falta ``acoes_cobrem_mensagem``)."""
    faltando: List[str] = []
    palavras: Dict[str, Sequence[str]] = getattr(ao, "PALAVRAS_CHAVE_ACAO", {})
    for acao, chaves in palavras.items():
        if acao == "NADA_A_FAZER":
            continue
        if any(chave in mensagem for chave in chaves) and acao not in acoes:
            irmas = [a for a in acoes if a.startswith(acao.split("_")[0])]
            if not irmas:
                faltando.append(acao)
    return faltando


def voz_cobre_acoes(acoes: Sequence[str], mensagem: str) -> bool:
    """Toda ação pedida na voz tem pictograma (``acoes_cobrem_mensagem`` da frente B, ou a regra da v3)."""
    funcao: Optional[Callable[[Sequence[str], str], bool]] = getattr(ao, "acoes_cobrem_mensagem", None)
    if funcao is not None:
        return bool(funcao(list(acoes), mensagem))
    return not _cobertura_por_palavras(acoes, mensagem)


def problemas_do_estado(caso: Dict[str, Any]) -> List[str]:
    """Lista de incoerências de um estado da grade (vazia = coerente)."""
    retrato: Dict[str, Any] = caso.get("retrato") or {}
    api: Dict[str, Any] = caso.get("api") or {}
    problemas: List[str] = []
    if "cor" not in api:
        return problemas
    cor_tela = str(retrato.get("estado") or "")
    confianca = str(retrato.get("confianca") or "")
    banca = caso.get("estado") == "banca"
    teste = bool(api.get("dados_simulados")) or api.get("modo_operacao") == "SIMULACAO" \
        or (bool(api.get("demonstracao_ativa")) and not banca) or confianca in CONFIANCA_TESTE
    antiga = bool(api.get("leitura_desatualizada")) or bool(api.get("relogio_inconsistente")) \
        or confianca in CONFIANCA_ANTIGA or caso.get("estado") in ESTADOS_ANTIGOS
    sem_leitura = not api.get("tem_leitura", True)
    # Cor da tela.
    if teste:
        if cor_tela in CORES:
            problemas.append(f"lâmpada {cor_tela} acesa com dados de teste")
    elif cor_tela != api["cor"]:
        problemas.append(f"cor da tela {cor_tela or '—'} ≠ API {api['cor']}")
    # Chip e título.
    chip = str(retrato.get("chip") or "")
    cores_no_chip = [c for c, nome in CORES.items() if nome.lower() in chip.lower()]
    if cor_tela in CORES and cores_no_chip and cores_no_chip != [cor_tela]:
        problemas.append(f"chip '{chip}' com outra cor que a lâmpada {cor_tela}")
    if cor_tela not in CORES and cores_no_chip:
        problemas.append(f"chip '{chip}' diz cor com a lâmpada apagada")
    if teste and cores_no_chip:
        problemas.append(f"chip '{chip}' diz cor com dados de teste")
    titulo = str(retrato.get("titulo") or "").lower()
    if cor_tela in TITULO_CONTRARIO and not (teste or antiga or sem_leitura):
        contrarios = [p for p in TITULO_CONTRARIO[cor_tela] if p in titulo]
        if contrarios:
            problemas.append(f"título '{retrato.get('titulo')}' contradiz o {cor_tela}")
    # Voz.
    # Sem voz pt-BR o celular bipa: a fala ouvida não vale; confere a fala que a API manda dizer.
    falas: List[str] = [] if caso.get("cliente") == "sem_voz" else [f for f in caso.get("falas") or [] if f]
    # Sem fala ouvida: a fala curta da API (v4) ou, na v3, a abertura da cor + a mensagem (como o celular dizia).
    fala = falas[-1] if falas else str(api.get("fala_curta") or (
        f"{ABERTURA.get(cor_tela, 'Atenção.')} {api.get('mensagem_voz') or ''}".strip()))
    mensagem = str(api.get("mensagem_voz") or "")
    if teste or antiga:
        if not fala.startswith("Atenção."):
            problemas.append(f"voz começa '{fala[:24]}' (esperado 'Atenção.')")
    elif sem_leitura:
        if not (fala.startswith("Atenção.") or fala.startswith(ABERTURA["amarelo"])):
            problemas.append(f"voz começa '{fala[:24]}' sem leitura")
    elif cor_tela in ABERTURA and not fala.startswith(ABERTURA[cor_tela]):
        problemas.append(f"voz começa '{fala[:24]}' (esperado '{ABERTURA[cor_tela]}')")
    # Pictogramas.
    todas_tela = [str(a.get("codigo")) for a in retrato.get("acoes") or []]
    acoes_tela = [str(a.get("codigo")) for a in retrato.get("acoes") or [] if not a.get("antiga")]
    acoes_api = [str(a) for a in api.get("acoes") or []]
    if acoes_api and todas_tela and sorted(todas_tela) != sorted(acoes_api):
        problemas.append(f"pictogramas da tela {todas_tela} ≠ API {acoes_api}")
    # Só contam os pictogramas mostrados (os marcados data-antiga são da última notícia, não da voz atual).
    acoes = acoes_tela
    if acoes and not ao.acoes_coerentes_com_mensagem(list(acoes), fala):
        problemas.append(f"pictograma sem palavra na voz: {acoes}")
    # Em teste e leitura antiga só a fala vale: a mensagem longa da API é da leitura, não do que o celular diz.
    for texto in ((fala,) if (teste or antiga) else (fala, mensagem)):
        if texto and acoes and not voz_cobre_acoes(acoes, texto):
            problemas.append(f"voz pede ação sem pictograma: {acoes} × '{texto[:60]}'")
            break
    # Causa.
    causa = str(api.get("causa") or "")
    if causa and causa not in ("NENHUMA",):
        if not (ao.causa_coerente_com_mensagem(causa, fala) or ao.causa_coerente_com_mensagem(causa, mensagem)):
            problemas.append(f"causa {causa} não aparece na voz")
    # "Feito" com dado que não vale.
    if retrato.get("feito_visivel") and (teste or antiga):
        problemas.append("botão Feito visível com dado que não vale (teste/antigo)")
    return problemas


def conferir_janela_desfazer() -> Dict[str, Any]:
    """Desfazer aceito a 119 s e recusado a 121 s do toque (``retorno_agricultor`` da frente B)."""
    import retorno_agricultor as ra
    from database import inicializar_banco

    funcao = getattr(ra, "desfazer_acao", None)
    registrar = getattr(ra, "registrar_acao_sem_repetir", None)
    if funcao is None or registrar is None:
        return {"ok": False, "motivo": "retorno_agricultor sem desfazer_acao/registrar_acao_sem_repetir"}
    pasta = Path(tempfile.mkdtemp(prefix="agro_ux4_desfazer_"))
    try:
        banco = pasta / "agro.db"
        inicializar_banco(banco)
        toque = datetime(2026, 7, 10, 14, 0, tzinfo=timezone.utc)
        primeiro, _ = registrar(banco, "REGAR_AGORA", "TERRA_SECA", None, "suite-ux", toque)
        segundo, _ = registrar(banco, "COBRIR_MUDAS", "FRIO_GEADA", None, "suite-ux", toque)
        funcao(banco, primeiro, toque + timedelta(seconds=119))
        recusado = False
        try:
            funcao(banco, segundo, toque + timedelta(seconds=121))
        except ra.JanelaDesfazerExpirada:
            recusado = True
        return {"ok": recusado, "aceito_119s": True, "recusado_121s": recusado,
                "janela_s": getattr(ra, "JANELA_DESFAZER_SEGUNDOS", None)}
    except Exception as erro:  # noqa: BLE001 - qualquer erro é falha do contrato, registrada no relatório
        return {"ok": False, "motivo": f"{type(erro).__name__}: {erro}"}
    finally:
        shutil.rmtree(pasta, ignore_errors=True)


def avaliar(medidas: Dict[str, Any]) -> Dict[str, Any]:
    """Indicadores, detalhes e estados a partir das medidas do Node."""
    grade = ((medidas.get("grupos") or {}).get("grade") or {}).get("casos") or []
    estados: List[Dict[str, Any]] = []
    for caso in grade:
        if caso.get("viewport") != VIEWPORT_COERENCIA or not (caso.get("api") or {}).get("cor"):
            continue
        retrato = caso.get("retrato") or {}
        estados.append({"estado": caso["estado"], "problemas": problemas_do_estado(caso), "cor_tela": retrato.get("estado"),
                        "chip": retrato.get("chip"), "titulo": retrato.get("titulo"),
                        "fala": ((caso.get("falas") or [""])[-1:] or [""])[0],
                        "acoes": [a.get("codigo") for a in retrato.get("acoes") or []],
                        "causa": (caso.get("api") or {}).get("causa")})
    incoerentes = [e for e in estados if e["problemas"]]
    janela = conferir_janela_desfazer()
    indicadores: Dict[str, Any] = {
        "coerencia.estados_avaliados": len(estados),
        "coerencia.estados_incoerentes": len(incoerentes) if estados else None,
        "feito.desfazer_janela_servidor_120s": 1 if janela.get("ok") else 0,
    }
    detalhes: Dict[str, Any] = {
        "coerencia.estados_incoerentes": [{"estado": e["estado"], "problemas": e["problemas"]} for e in incoerentes],
        "feito.desfazer_janela_servidor_120s": janela,
    }
    return {"indicadores": indicadores, "detalhes": detalhes, "estados": estados}


def principal(argumentos: List[str]) -> int:
    """Lê ``<pasta>/medidas_node.json`` e grava ``<pasta>/coerencia.json``."""
    if len(argumentos) != 2:
        print("Uso: python tests/e2e/validacao/coerencia.py <pasta_do_relatorio>", file=sys.stderr)
        return 2
    pasta = Path(argumentos[1])
    arquivo = pasta / "medidas_node.json"
    medidas: Dict[str, Any] = json.loads(arquivo.read_text(encoding="utf-8")) if arquivo.exists() else {}
    resultado = avaliar(medidas)
    (pasta / "coerencia.json").write_text(json.dumps(resultado, ensure_ascii=False, indent=1), encoding="utf-8")
    for estado in resultado["estados"]:
        print(f"{estado['estado']:22s} {'OK' if not estado['problemas'] else '; '.join(estado['problemas'])}")
    print(f"Estados: {len(resultado['estados'])}; incoerentes: {resultado['indicadores']['coerencia.estados_incoerentes']}; "
          f"desfazer até 120 s: {'ok' if resultado['indicadores']['feito.desfazer_janela_servidor_120s'] else 'FALHA'}")
    return 0


if __name__ == "__main__":
    sys.exit(principal(sys.argv))
