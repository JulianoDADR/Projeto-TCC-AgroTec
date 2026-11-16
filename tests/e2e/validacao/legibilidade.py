"""Legibilidade e conteúdo da voz sobre o espaço combinatório da auditoria (suíte UX v4).

Enumera, como a auditoria de conteúdo do comitê de UX da v4 e os testes da
frente B, as mensagens do semáforo para as duas cultivares de café:

    * regra instantânea (v2) numa grade de temperatura × umidade do solo, com o
      nível também subido a Amarelo (IA);
    * regra fisiológica (v3): motivo da temperatura × motivo do solo × intensidade;
    * antecipação (Amarelo antecipado): motivo × prazo × estresse × hora × probabilidade;

cada forma de frase (números trocados por "N") contada uma vez por tipo, cultivar
e nível, como na auditoria (cerca de 841 alertas na v3).

Para cada alerta (nível ≥ 1 com pelo menos um pictograma) mede na ``fala_curta``
(``acoes_orientacao.montar_fala_curta`` da frente B; na v3, a fala inteira que o
celular dizia, "Sinal <cor>." + mensagem) e na ``mensagem_voz``:

    * palavras, dígitos e índice da 1ª frase com ação (0 = 1ª frase);
    * Flesch adaptado ao português (Martins, Ghiraldelo, Nunes e Oliveira Jr., 1996):
      248,835 − 1,015 × (palavras/frases) − 84,6 × (sílabas/palavras);
    * erros de "N graus" ("1 graus", "0 graus", "2 grau"), "-N" e "geie";
    * duas ordens de rega e "Regue agora" junto de "gelando agora";
    * coerência causa × voz, ação × voz e voz × ação (``acoes_cobrem_mensagem``).

Uso (a partir da raiz do projeto)::

    python tests/e2e/validacao/legibilidade.py <pasta_do_relatorio>

Grava ``<pasta>/legibilidade.json`` (``indicadores``, ``detalhes`` e um resumo).
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# AGRO_UX_PROJETO aponta para outra cópia do projeto (por exemplo a v3, para a linha de base).
RAIZ_PROJETO: Path = Path(os.environ.get("AGRO_UX_PROJETO") or Path(__file__).resolve().parents[3]).resolve()
PASTA_SRC: Path = RAIZ_PROJETO / "src"
if str(PASTA_SRC) not in sys.path:
    sys.path.insert(0, str(PASTA_SRC))

import acoes_orientacao as ao  # noqa: E402
import gerador_dataset as gd  # noqa: E402
import prognostico as pg  # noqa: E402
import regras_agronomicas as ra  # noqa: E402
from database import FaixaParametro, ParametrosCultura, RegistroLeitura, buscar_parametros_cultura, inicializar_banco  # noqa: E402

CULTIVARES: Tuple[str, ...] = ("cafe_arabica", "cafe_conilon")
TEMPERATURAS_V2: Tuple[int, ...] = (-2, 1, 3, 6, 10, 14, 16, 18, 20, 22, 24, 26, 28, 30, 33, 36, 40)
SOLOS_V2: Tuple[int, ...] = (5, 15, 25, 35, 45, 52, 58, 62, 70, 78, 82, 86, 90, 95, 99)
MOTIVOS_TEMPERATURA: Tuple[str, ...] = ("NENHUM", "GEADA_AGORA", "CALOR_FLORADA", "CALOR_SEGUIDO", "CALOR_DIA",
                                        "DIA_FRIO", "DIA_QUENTE")
MOTIVOS_SOLO: Tuple[str, ...] = ("NENHUM", "SECA_AGORA", "SECA_PROGRAMADA", "ENCHARCAMENTO")
PRAZOS: Tuple[Optional[int], ...] = (2, 6, 14, 30, 50, None)
ESTRESSES: Tuple[Optional[int], ...] = (None, 0, 1, 5)
HORAS: Tuple[int, ...] = (3, 8, 15, 17, 19, 22)
PROBABILIDADES: Tuple[Optional[float], ...] = (None, 0.5, 0.9)
ABERTURA: Dict[int, str] = {0: "Sinal verde.", 1: "Sinal amarelo.", 2: "Sinal vermelho."}

# Ação falada: verbos no imperativo do comitê e "Não precisa fazer nada" (Amarelo informativo).
PADRAO_ACAO: "re.Pattern[str]" = re.compile(
    r"Regue|Não regue|Pare|Abra|Faça|Cubra|Chegue|Proteja|Confira|Verifique|Olhe|Não precisa fazer nada")
PADRAO_REGA: "re.Pattern[str]" = re.compile(r"\b[Rr]egue\b(?! à noite)")
PADRAO_GRAUS_ERRADO: "re.Pattern[str]" = re.compile(r"(?<![\d,])(?:1|um) graus\b|(?<![\d,])0 graus\b|(?<![\d,])(?:[2-9]|\d{2,}) grau\b")
PADRAO_MENOS_N: "re.Pattern[str]" = re.compile(r"(?<![\w])-\d")
PADRAO_GEIE: "re.Pattern[str]" = re.compile(r"\bgeie\b", re.IGNORECASE)
_VOGAIS: str = "aeiouáéíóúâêôãõàü"
_DITONGOS: "re.Pattern[str]" = re.compile(r"(ão|õe|ãe|ai|ei|oi|ui|au|eu|iu|ou)")
_U_MUDO: "re.Pattern[str]" = re.compile(r"(?<=[qg])u(?=[eiéí])")


def contar_silabas(palavra: str) -> int:
    """Sílabas aproximadas em pt-BR: núcleos vocálicos, ditongo comum = 1, "u" mudo de gue/gui/que/qui fora."""
    texto = _DITONGOS.sub("a", _U_MUDO.sub("", palavra.lower()))
    return max(1, len(re.findall(f"[{_VOGAIS}]+", texto)))


def dividir_frases(texto: str) -> List[str]:
    """Frases terminadas por ponto, exclamação ou interrogação."""
    return [frase for frase in re.split(r"(?<=[.!?])\s+", texto.strip()) if frase]


def palavras(texto: str) -> List[str]:
    """Palavras (letras e números) do texto."""
    return re.findall(r"[A-Za-zÀ-ÿ0-9]+", texto)


def flesch_pt(texto: str) -> float:
    """Flesch-PT (Martins et al., 1996); um número conta duas sílabas, como na auditoria de conteúdo."""
    frases = dividir_frases(texto)
    lista = palavras(texto)
    silabas = sum(2 if p.isdigit() else contar_silabas(p) for p in lista)
    return 248.835 - 1.015 * (len(lista) / max(1, len(frases))) - 84.6 * (silabas / max(1, len(lista)))


def indice_da_acao(texto: str) -> Optional[int]:
    """Índice (0 = 1ª) da primeira frase com ação, ou None."""
    return next((i for i, frase in enumerate(dividir_frases(texto)) if PADRAO_ACAO.search(frase)), None)


@dataclass(frozen=True)
class Alerta:
    """Uma mensagem do espaço da auditoria, com pictogramas, causa e fala curta."""

    origem: str
    nivel: int
    mensagem: str
    acoes: Tuple[str, ...]
    causa: str
    fala_curta: str


def fala_curta_v3(nivel: int, mensagem: str) -> str:
    """Fala da v3 (sem ``montar_fala_curta``): "Sinal <cor>." seguido da mensagem inteira."""
    return f"{ABERTURA[nivel]} {mensagem}"


def montar_fala(nivel: int, acoes: Sequence[str], causa: str, mensagem: str) -> str:
    """``montar_fala_curta`` da frente B, ou a fala da v3 se a função não existir."""
    funcao: Optional[Callable[..., str]] = getattr(ao, "montar_fala_curta", None)
    if funcao is None:
        return fala_curta_v3(nivel, mensagem)
    return funcao(nivel, list(acoes), causa)


def voz_cobre_acoes(acoes: Sequence[str], mensagem: str) -> bool:
    """Toda ação pedida na voz tem pictograma (``acoes_cobrem_mensagem``, ou as palavras-chave da v3)."""
    funcao: Optional[Callable[[Sequence[str], str], bool]] = getattr(ao, "acoes_cobrem_mensagem", None)
    if funcao is not None:
        return bool(funcao(list(acoes), mensagem))
    for acao, chaves in getattr(ao, "PALAVRAS_CHAVE_ACAO", {}).items():
        if acao == "NADA_A_FAZER":
            continue
        if any(chave in mensagem for chave in chaves) and acao not in acoes \
                and not any(a.startswith(acao.split("_")[0]) for a in acoes):
            return False
    return True


def _ponto_medio(faixa: FaixaParametro) -> float:
    """Meio da faixa ideal."""
    return (faixa.ideal_min + faixa.ideal_max) / 2.0


def _registro(parametros: ParametrosCultura, temperatura: float, solo: float, status_regra: int) -> RegistroLeitura:
    """Leitura mínima para ``prognostico._mensagem_antecipada``."""
    deficit, desvio = gd.calcular_features(temperatura, solo, parametros)
    return RegistroLeitura(
        data_hora="2026-07-10 08:00:00", codigo_cultura=parametros.codigo, origem="CAMPO_USB",
        temperatura_ar_bruta=temperatura, umidade_ar_bruta=70.0, umidade_solo_bruta=solo, temperatura_ar=temperatura,
        umidade_ar=70.0, umidade_solo=solo, deficit_hidrico_solo=deficit, desvio_termico=desvio,
        status_regra=status_regra, status_predito=max(1, status_regra), status_alerta=max(1, status_regra),
        probabilidade_predicao=0.9, flag_anomalia=0, descricao_anomalia="", mensagem_voz="", versao_modelo="UX4")


def _alerta(origem: str, nivel: int, mensagem: str, temperatura: float, solo: float, parametros: ParametrosCultura,
            motivo_antecipacao: str = "NENHUM", motivo_regra: str = "") -> Alerta:
    """Deriva pictogramas, causa e fala curta como ``app.montar_status``."""
    acoes = ao.derivar_acoes(nivel, temperatura, solo, parametros, motivo_antecipacao=motivo_antecipacao,
                             mensagem_voz=mensagem, motivo_regra=motivo_regra)
    causa = ao.derivar_causa(nivel, {"temperatura_ar": temperatura, "umidade_solo": solo, "motivo_regra": motivo_regra},
                             parametros, motivo_antecipacao, mensagem_voz=mensagem)
    return Alerta(origem, nivel, mensagem, tuple(acoes), causa, montar_fala(nivel, acoes, causa, mensagem))


def _gabarito(texto: str) -> str:
    """Mensagem com os números trocados por "N" (a auditoria conta uma vez cada forma de frase)."""
    return re.sub(r"\d+", "N", texto)


def gerar_espaco(culturas: Sequence[ParametrosCultura]) -> List[Alerta]:
    """Espaço combinatório da auditoria de conteúdo (uma vez cada forma de frase por tipo, cultivar e nível)."""
    itens: List[Alerta] = []
    vistos: set = set()

    def incluir(chave: Tuple[Any, ...], alerta_por_construir: Callable[[], Alerta]) -> None:
        """Inclui o alerta se a forma da frase ainda não apareceu."""
        if chave in vistos:
            return
        vistos.add(chave)
        itens.append(alerta_por_construir())

    for parametros in culturas:
        # A. Regra instantânea (v2) numa grade de temperatura × solo; o nível também sobe a Amarelo (IA).
        for temperatura, solo in itertools.product(TEMPERATURAS_V2, SOLOS_V2):
            status = gd.calcular_status_regra(temperatura, solo, parametros)
            deficit, desvio = gd.calcular_features(temperatura, solo, parametros)
            for nivel in sorted({status, max(status, 1)}):
                mensagem = gd.gerar_mensagem_orientacao(nivel, deficit, desvio, parametros, temperatura, solo)
                incluir(("v2", parametros.codigo, nivel, _gabarito(mensagem)),
                        lambda: _alerta(f"v2 {parametros.codigo} T={temperatura} S={solo}", nivel, mensagem,
                                        float(temperatura), float(solo), parametros))
        # B. Regra fisiológica (v3): motivo da temperatura × motivo do solo × intensidade do solo.
        faixa_t = parametros.faixa("temperatura_ar")
        temperaturas = {"NENHUM": _ponto_medio(faixa_t), "GEADA_AGORA": 1.0, "CALOR_FLORADA": 35.0,
                        "CALOR_SEGUIDO": 35.0, "CALOR_DIA": 31.0, "DIA_FRIO": 15.0, "DIA_QUENTE": 25.0}
        for motivo_t, motivo_s, intensidade in itertools.product(MOTIVOS_TEMPERATURA, MOTIVOS_SOLO, (0, 1, 2)):
            if motivo_s == "NENHUM" and intensidade:
                continue
            solo = {"NENHUM": 70.0, "SECA_AGORA": 50.0 if intensidade < 2 else 20.0, "SECA_PROGRAMADA": 40.0,
                    "ENCHARCAMENTO": 85.0 if intensidade < 2 else 97.0}[motivo_s]
            status_t = 2 if motivo_t in ra.MOTIVOS_CALOR_VERMELHO + ("GEADA_AGORA",) else int(motivo_t != "NENHUM")
            status_s = intensidade if motivo_s in ("SECA_AGORA", "ENCHARCAMENTO") else int(motivo_s == "SECA_PROGRAMADA")
            motivo_regra = motivo_t if motivo_t != "NENHUM" else (motivo_s if motivo_s != "NENHUM" else "")
            temperatura = temperaturas[motivo_t]
            deficit, _ = gd.calcular_features(temperatura, solo, parametros)
            for nivel in sorted({max(status_t, status_s), max(status_t, status_s, 1)}):
                avaliacao = SimpleNamespace(status=nivel, motivo_temperatura=motivo_t, motivo_solo=motivo_s,
                                            status_solo=status_s)
                mensagem = gd.gerar_mensagem_fisiologica(avaliacao, deficit, parametros, temperatura, solo)
                incluir(("fis", parametros.codigo, nivel, _gabarito(mensagem)),
                        lambda: _alerta(f"fis {parametros.codigo} {motivo_t}/{motivo_s}/{intensidade}", nivel, mensagem,
                                        temperatura, solo, parametros, motivo_regra=motivo_regra))
        # C. Antecipação (Amarelo antecipado) com a leitura no meio da faixa ideal.
        temperatura, solo = _ponto_medio(faixa_t), _ponto_medio(parametros.faixa("umidade_solo"))
        status_regra = gd.calcular_status_regra(temperatura, solo, parametros)
        for motivo, prazo, estresse, hora, probabilidade in itertools.product(
                [m for m in pg.MOTIVOS if m != "NENHUM"], PRAZOS, ESTRESSES, HORAS, PROBABILIDADES):
            explicacao = {"motivo_principal": motivo, "prazo_horas": prazo, "horas_em_estresse": estresse}
            mensagem = pg._mensagem_antecipada(_registro(parametros, temperatura, solo, status_regra), parametros,
                                               explicacao, datetime(2026, 7, 10, hora, 0), probabilidade)
            incluir(("ant", parametros.codigo, _gabarito(mensagem)),
                    lambda: _alerta(f"ant {parametros.codigo} {motivo} prazo={prazo} estresse={estresse} h={hora} "
                                    f"p={probabilidade}", 1, mensagem, temperatura, solo, parametros,
                                    motivo_antecipacao=motivo))
    return itens


def _pct(parte: int, total: int) -> Optional[float]:
    """Porcentagem com 1 casa (None sem total)."""
    return round(100.0 * parte / total, 1) if total else None


def _origens(lista: Sequence[Alerta]) -> List[str]:
    """Origem dos 8 primeiros alertas (exemplos no relatório)."""
    return [alerta.origem for alerta in list(lista)[:8]]


def avaliar(espaco: Sequence[Alerta]) -> Dict[str, Any]:
    """Indicadores e detalhes do conteúdo da voz."""
    alertas = [a for a in espaco if a.nivel >= 1 and a.acoes]
    acao_cedo = [a for a in alertas if (indice_da_acao(a.fala_curta) is not None and indice_da_acao(a.fala_curta) <= 1)]
    curtas_ok = [a for a in alertas if len(palavras(a.fala_curta)) <= 15 and not re.search(r"\d", a.fala_curta)]
    com_digito = [a for a in alertas if re.search(r"\d", a.fala_curta)]
    textos = [a.mensagem + " " + a.fala_curta for a in espaco]
    graus = [t for t in textos if PADRAO_GRAUS_ERRADO.search(t) or PADRAO_MENOS_N.search(t)]
    geie = [t for t in textos if PADRAO_GEIE.search(t)]
    duas_regas = [a for a in espaco if len(PADRAO_REGA.findall(a.mensagem)) > 1]
    regue_gelando = [a for a in espaco if "gelando agora" in a.mensagem and "Regue agora" in a.mensagem]
    incoerentes: List[Dict[str, Any]] = []
    for alerta in alertas:
        problemas: List[str] = []
        if alerta.causa not in ("", "NENHUMA") and not ao.causa_coerente_com_mensagem(alerta.causa, alerta.mensagem):
            problemas.append("causa × voz")
        if not ao.acoes_coerentes_com_mensagem(list(alerta.acoes), alerta.mensagem):
            problemas.append("ação × voz")
        if not voz_cobre_acoes(alerta.acoes, alerta.mensagem):
            problemas.append("voz × ação")
        if problemas:
            incoerentes.append({"origem": alerta.origem, "problemas": problemas, "acoes": list(alerta.acoes),
                                "causa": alerta.causa, "mensagem": alerta.mensagem[:160]})
    palavras_curtas = [len(palavras(a.fala_curta)) for a in alertas]
    flesch = [flesch_pt(a.fala_curta) for a in alertas]
    indicadores: Dict[str, Any] = {
        "voz.alertas": len(alertas),
        "voz.acao_ate_2a_frase_pct": _pct(len(acao_cedo), len(alertas)),
        "voz.curta_ate_15_palavras_sem_digito_pct": _pct(len(curtas_ok), len(alertas)),
        "voz.mediana_palavras_curta": statistics.median(palavras_curtas) if palavras_curtas else None,
        "voz.falas_curtas_com_digito": len(com_digito),
        "voz.flesch_mediano_curta": round(statistics.median(flesch), 1) if flesch else None,
        "voz.flesch_mediano_mensagem": round(statistics.median(flesch_pt(a.mensagem) for a in alertas), 1) if alertas else None,
        "voz.graus_e_menos_n_erros": len(graus),
        "voz.geie": len(geie),
        "voz.duas_ordens_de_rega": len(duas_regas),
        "voz.regue_agora_com_gelando": len(regue_gelando),
        "voz.coerencia_pct": _pct(len(alertas) - len(incoerentes), len(alertas)),
        "voz.coerencia_falhas": len(incoerentes),
    }
    detalhes: Dict[str, Any] = {
        "voz.acao_ate_2a_frase_pct": [{"origem": a.origem, "fala": a.fala_curta} for a in alertas if a not in acao_cedo][:8],
        "voz.curta_ate_15_palavras_sem_digito_pct": [{"origem": a.origem, "fala": a.fala_curta} for a in alertas if a not in curtas_ok][:8],
        "voz.graus_e_menos_n_erros": [t[:160] for t in graus[:8]],
        "voz.geie": [t[:160] for t in geie[:8]],
        "voz.duas_ordens_de_rega": _origens(duas_regas),
        "voz.regue_agora_com_gelando": _origens(regue_gelando),
        "voz.coerencia_pct": incoerentes[:12],
    }
    return {"indicadores": indicadores, "detalhes": detalhes,
            "fala_curta_da_frente_b": hasattr(ao, "montar_fala_curta"),
            "acoes_cobrem_mensagem": hasattr(ao, "acoes_cobrem_mensagem")}


def principal(argumentos: List[str]) -> int:
    """Gera o espaço num banco temporário e grava ``<pasta>/legibilidade.json``."""
    if len(argumentos) != 2:
        print("Uso: python tests/e2e/validacao/legibilidade.py <pasta_do_relatorio>", file=sys.stderr)
        return 2
    pasta = Path(argumentos[1])
    pasta.mkdir(parents=True, exist_ok=True)
    temporaria = Path(tempfile.mkdtemp(prefix="agro_ux4_voz_"))
    try:
        banco = temporaria / "agro.db"
        inicializar_banco(banco)
        culturas = [buscar_parametros_cultura(codigo, caminho_banco=banco) for codigo in CULTIVARES]
        resultado = avaliar(gerar_espaco(culturas))
    finally:
        shutil.rmtree(temporaria, ignore_errors=True)
    (pasta / "legibilidade.json").write_text(json.dumps(resultado, ensure_ascii=False, indent=1), encoding="utf-8")
    for chave, valor in resultado["indicadores"].items():
        print(f"{chave:44s} {valor}")
    return 0


if __name__ == "__main__":
    sys.exit(principal(sys.argv))
