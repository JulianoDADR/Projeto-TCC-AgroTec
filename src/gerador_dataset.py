"""Módulo 3 — Geração e Saneamento de Dados do AgroAnálise.

Pipeline:
    1. ``simular_serie_temporal``: para cada cultura ativa do banco, simula
       um ano (8.760 horas) de microclima do hemisfério sul — estações do ano,
       ciclo dia/noite com a mínima da madrugada guiada pelo ponto de orvalho
       (``ponto_orvalho_magnus``), chuva por mês (águas de outubro a março,
       seca de maio a setembro), veranico, geada radiativa e escaldadura — e a
       umidade do solo por BALANÇO HÍDRICO horário (evapotranspiração, chuva,
       irrigação por gotejamento do produtor, panes de irrigação e drenagem).
    2. ``injetar_ruidos``: corrompe ~3% das células de cada sensor com NaN,
       picos elétricos e valores negativos, guardando um gabarito.
    3. ``detectar_anomalias`` + ``sanear_dataset``: detector cego ao gabarito
       (NaN, faixa física, salto vs. mediana móvel), ``flag_anomalia`` e
       interpolação linear por cultura.
    4. ``calcular_features_dataframe``: features ``deficit_hidrico_solo`` e
       ``desvio_termico`` + rótulo ``status_alerta`` pelo motor de regras.
    5. ``avaliar_detector``: recall/precisão do detector contra o gabarito.

O módulo também concentra as regras agronômicas usadas em tempo real
(classificação, features, mensagem falada ao agricultor), o simulador de
tempo real do Modo Simulação e os cenários extremos do Modo Demonstração.

Execução direta: ``python src/gerador_dataset.py`` gera ``data/dataset_bruto.csv``
e ``data/dataset_limpo.csv``, imprime o relatório e grava o relatório de
realismo do simulador em ``reports/realismo_simulador.txt``.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Set, Tuple, Union

import numpy as np
import pandas as pd

from database import (
    CAMINHO_BANCO,
    CAMINHO_DATASET_BRUTO,
    CAMINHO_DATASET_LIMPO,
    NOMES_STATUS,
    REPORTS_DIR,
    STATUS_AMARELO,
    STATUS_VERDE,
    STATUS_VERMELHO,
    VARIAVEL_TEMPERATURA_AR,
    VARIAVEL_UMIDADE_AR,
    VARIAVEL_UMIDADE_SOLO,
    FaixaParametro,
    ParametrosCultura,
    buscar_parametros_cultura,
    listar_culturas,
)

# ---------------------------------------------------------------------------
# Constantes públicas (contrato técnico, seção 5)
# ---------------------------------------------------------------------------
COLUNAS_SENSORES: List[str] = ["temperatura_ar", "umidade_ar", "umidade_solo"]
COLUNAS_FEATURES_MODELO: List[str] = ["temperatura_ar", "umidade_ar", "umidade_solo", "deficit_hidrico_solo", "desvio_termico"]
COLUNA_ALVO: str = "status_alerta"
HORAS_SIMULACAO: int = 8760  # por cultura (talhão): um ano; dataset final = 8760 × nº culturas ativas
TAXA_RUIDO: float = 0.03
SEMENTE_ALEATORIA: int = 42
LIMITES_FISICOS_SENSORES: Dict[str, Tuple[float, float]] = {
    "temperatura_ar": (0.0, 50.0),
    "umidade_ar": (0.0, 100.0),
    "umidade_solo": (0.0, 100.0),
}
LIMITES_SALTO: Dict[str, float] = {"temperatura_ar": 8.0, "umidade_ar": 30.0, "umidade_solo": 15.0}

COLUNAS_CSV_BRUTO: List[str] = ["data_hora", "cultura", "temperatura_ar", "umidade_ar", "umidade_solo", "tipo_ruido_injetado"]
COLUNAS_CSV_LIMPO: List[str] = [
    "data_hora", "cultura", "temperatura_ar", "umidade_ar", "umidade_solo",
    "flag_anomalia", "descricao_anomalia", "deficit_hidrico_solo", "desvio_termico", "status_alerta",
]
TIPOS_RUIDO: Tuple[str, ...] = ("nan", "pico", "negativo")
JANELA_MEDIANA_MOVEL: int = 5
PROPORCAO_MINIMA_CLASSE: float = 0.15
FORMATO_DATA_HORA: str = "%Y-%m-%d %H:%M:%S"

# ---------------------------------------------------------------------------
# Clima do simulador (hemisfério sul; valores relativos às faixas da cultura)
# ---------------------------------------------------------------------------
AMPLITUDE_ANUAL_RELATIVA: float = 0.55  # × largura ideal de temperatura (verão − inverno ≈ 2 × isto)
DIA_MAIS_FRIO_DO_ANO: float = 196.0  # dia do ano (≈ 15 de julho) com a menor temperatura média
MESES_CHUVOSOS: Tuple[int, ...] = (10, 11, 12, 1, 2, 3)
MESES_SECOS: Tuple[int, ...] = (5, 6, 7, 8, 9)
MESES_GEADA: Tuple[int, ...] = (5, 6, 7, 8)
MESES_ESCALDADURA: Tuple[int, ...] = (12, 1, 2, 3)
PROBABILIDADE_CHUVA_MES: Dict[int, float] = {
    1: 0.50, 2: 0.45, 3: 0.40, 4: 0.22, 5: 0.08, 6: 0.04,
    7: 0.03, 8: 0.04, 9: 0.08, 10: 0.30, 11: 0.42, 12: 0.50,
}  # probabilidade de chover em um dia do mês
PROB_VERANICO_ANO: float = 0.85  # veranico de 10+ dias em jan./fev. acontece na maioria dos anos no Sudeste
DURACAO_VERANICO_DIAS: Tuple[int, int] = (10, 20)
PROB_ESCALDADURA_ONDA_CALOR: float = 0.35  # fração das ondas de calor de verão que passam de critico_max
INTERVALO_MEDIO_AR_POLAR_HORAS: float = 330.0  # massas de ar polar (só valem de maio a agosto)
UMIDADE_AR_NOITE_RADIATIVA: float = 55.0  # % no fim da tarde: abaixo disso, noite de céu limpo (geada radiativa)
COEF_RESFRIAMENTO_NOTURNO: float = 0.15  # Tmin = Td_tarde − (0,15 × depressão do ponto de orvalho ± ruído)
TAXA_MAXIMA_GOTEJAMENTO: float = 1.5  # pp/h de umidade do solo na irrigação por gotejamento
LIMIAR_SUBIDA_CHUVA_PP: float = 2.0  # subida horária de solo acima disto = chuva (o gotejamento é mais lento)
MAGNUS_A: float = 17.62
MAGNUS_B: float = 243.12
DATA_INICIAL_DATASET: str = "2025-11-01 00:00:00"  # o teste temporal (últimos 30 %) cobre o inverno com geada
CAMINHO_RELATORIO_REALISMO: Path = REPORTS_DIR / "realismo_simulador.txt"

if TYPE_CHECKING:  # só para as anotações: regras_agronomicas importa este módulo
    from regras_agronomicas import AvaliacaoRegra

registrador: logging.Logger = logging.getLogger("agroanalise.gerador_dataset")


# ---------------------------------------------------------------------------
# Regras agronômicas
# ---------------------------------------------------------------------------
def classificar_variavel(valor: float, faixa: FaixaParametro) -> int:
    """Classifica UMA variável: 0 Verde (ideal), 1 Amarelo (atenção), 2 Vermelho.

    * ``ideal_min <= v <= ideal_max`` → 0
    * ``atencao_min <= v < ideal_min`` ou ``ideal_max < v <= atencao_max`` → 1
    * ``v < atencao_min`` ou ``v > atencao_max`` → 2

    Raises:
        ValueError: se o valor não for um número finito.
    """
    numero = float(valor)
    if not math.isfinite(numero):
        raise ValueError(f"Não é possível classificar '{faixa.variavel}' com valor não finito ({valor!r}).")
    if faixa.ideal_min <= numero <= faixa.ideal_max:
        return STATUS_VERDE
    if faixa.atencao_min <= numero <= faixa.atencao_max:
        return STATUS_AMARELO
    return STATUS_VERMELHO


def calcular_desvio_faixa(valor: float, minimo: float, maximo: float) -> float:
    """Distância do valor até a faixa ``[minimo, maximo]``.

    Retorna 0 dentro da faixa, ``valor - maximo`` (> 0) acima e
    ``valor - minimo`` (< 0) abaixo.
    """
    if valor > maximo:
        return float(valor - maximo)
    if valor < minimo:
        return float(valor - minimo)
    return 0.0


def _resolver_parametros(cultura: Union[str, ParametrosCultura]) -> ParametrosCultura:
    """Aceita o código da cultura ou o objeto já carregado."""
    if isinstance(cultura, ParametrosCultura):
        return cultura
    return buscar_parametros_cultura(str(cultura))


def calcular_features(
    temperatura_ar: float, umidade_solo: float, cultura: Union[str, ParametrosCultura]
) -> Tuple[float, float]:
    """Calcula as features derivadas usadas pela IA.

    * ``deficit_hidrico_solo`` (pontos percentuais): positivo = falta de água
      (``ideal_min - umidade_solo``), negativo = excesso/encharcamento
      (``ideal_max - umidade_solo``), 0 dentro da faixa ideal.
    * ``desvio_termico`` (graus): positivo = calor acima do ideal, negativo =
      frio abaixo do ideal, 0 dentro da faixa ideal.

    Returns:
        ``(deficit_hidrico_solo, desvio_termico)`` arredondados a 2 casas.
    """
    parametros = _resolver_parametros(cultura)
    faixa_solo = parametros.faixa(VARIAVEL_UMIDADE_SOLO)
    faixa_temperatura = parametros.faixa(VARIAVEL_TEMPERATURA_AR)
    deficit = -calcular_desvio_faixa(float(umidade_solo), faixa_solo.ideal_min, faixa_solo.ideal_max)
    desvio = calcular_desvio_faixa(float(temperatura_ar), faixa_temperatura.ideal_min, faixa_temperatura.ideal_max)
    return round(deficit + 0.0, 2), round(desvio + 0.0, 2)


def calcular_status_regra(temperatura_ar: float, umidade_solo: float, parametros: ParametrosCultura) -> int:
    """Status do motor de regras (rótulo-verdade): pior classe entre temperatura e solo."""
    return max(
        classificar_variavel(temperatura_ar, parametros.faixa(VARIAVEL_TEMPERATURA_AR)),
        classificar_variavel(umidade_solo, parametros.faixa(VARIAVEL_UMIDADE_SOLO)),
    )


def _numero_falado(valor: float) -> str:
    """Número inteiro (arredondamento comercial: 0,5 sobe), para ser lido em voz alta."""
    return str(int(math.floor(float(valor) + 0.5)))


def falar_graus(valor: float) -> str:
    """Temperatura falada com a concordância certa (v4): "zero grau", "1 grau", "menos 2 graus".

    Usa o mesmo arredondamento de ``_numero_falado`` (0,5 sobe). O sinal de menos
    vira a palavra "menos": a voz pt-BR do celular lê "-2" de jeitos diferentes
    (às vezes "traço dois"), e "1 graus" é erro de concordância que o agricultor
    percebe na hora.

    Args:
        valor: temperatura em °C.

    Returns:
        ``'zero grau'`` para 0; ``'1 grau'`` para 1; ``'menos 1 grau'`` para -1;
        ``'menos N graus'`` para N < -1; ``'N graus'`` para os demais.
    """
    inteiro = int(math.floor(float(valor) + 0.5))
    if inteiro == 0:
        return "zero grau"
    if inteiro == 1:
        return "1 grau"
    if inteiro == -1:
        return "menos 1 grau"
    if inteiro < -1:
        return f"menos {-inteiro} graus"
    return f"{inteiro} graus"


# ---------------------------------------------------------------------------
# Montagem final das ordens (v4): uma ordem por ação
# ---------------------------------------------------------------------------
# Rega pedida ("Regue", "regue"), sem contar a ressalva "Não regue à noite" nem a ordem de NÃO regar.
_PADRAO_REGA_POSITIVA: "re.Pattern[str]" = re.compile(r"(?<![Nn]ão )\b[Rr]egue\b(?! à noite)")
# Ordem de não regar ("Não regue hoje", "Não regue ainda", "Pare de regar"); a ressalva "à noite" não conta.
_PADRAO_NAO_REGAR: "re.Pattern[str]" = re.compile(r"\b(?:[Nn]ão regue\b(?! à noite)|[Pp]are de regar\b)")
# Momento da rega que já vem na própria ordem.
_PADRAO_MOMENTO_REGA: "re.Pattern[str]" = re.compile(r"fim da tarde|de manhã|quando o sol")
# Outras ordens que podem dividir a frase com a rega ("Faça sombra ... e regue ...").
_PADRAO_OUTRA_ORDEM: "re.Pattern[str]" = re.compile(r"\b(?:[Ff]aça|[Cc]ubra|[Cc]hegue|[Pp]roteja|[Aa]bra)\b")
_PADRAO_FIM_DE_FRASE: "re.Pattern[str]" = re.compile(r"(?<=[.!?])\s+")
# Frases fixas da montagem.
FRASE_GELANDO_AGORA: str = "gelando agora"
FRASE_REGA_NA_GEADA: str = "Regue de dia, quando o sol sair."
FRASE_NAO_REGUE_A_NOITE: str = "Não regue à noite."
FRASE_PREFIRA_FIM_DA_TARDE: str = "Prefira o fim da tarde."


def _dividir_sentencas(texto: str) -> List[str]:
    """Separa um texto nas frases terminadas por ponto, exclamação ou interrogação."""
    return [parte.strip() for parte in _PADRAO_FIM_DE_FRASE.split(texto.strip()) if parte.strip()]


def _tirar_rega_da_frase(frase: str) -> str:
    """Tira a ordem de rega de uma frase com outra ordem; ``""`` se a frase for só a rega.

    Exemplos: ``'Faça sombra nas mudas novas e regue no fim da tarde para
    refrescar.'`` → ``'Faça sombra nas mudas novas.'``; ``'Regue no fim da
    tarde e faça sombra nas mudas.'`` → ``'Faça sombra nas mudas.'``.
    """
    antes = re.match(r"^(.*?\S)\s+e\s+regue\b.*$", frase)
    if antes and _PADRAO_OUTRA_ORDEM.search(antes.group(1)):
        return antes.group(1).rstrip(",") + "."
    depois = re.match(r"^Regue\b[^.!?]*?\s+e\s+(\w)(.*)$", frase)
    if depois and _PADRAO_OUTRA_ORDEM.search(depois.group(1) + depois.group(2)):
        return depois.group(1).upper() + depois.group(2)
    return ""


def _juntar_acoes(frases: List[str]) -> List[str]:
    """Passo final das mensagens faladas: uma ordem por ação, sem ordens contraditórias.

    Só muda o texto e a ordem das frases; status, limiares e regras não mudam.

    1. Geada acontecendo ("Está gelando agora") com terra seca: a proteção vem
       primeiro e a rega vira ``FRASE_REGA_NA_GEADA`` ("Regue de dia, quando o
       sol sair."), que reaproveita o "de dia" da v2 sem acrescentar
       recomendação nova; "Regue agora" nunca fica ao lado de "gelando agora".
    2. Terra molhada demais ("Não regue hoje", "Pare de regar") ganha da rega
       para refrescar do calor: a ordem de regar sai da frase do calor (a
       sombra fica).
    3. Duas ordens de rega: fica a primeira que divide a frase com outra ordem
       ("Regue no fim da tarde e faça sombra"), ou senão a primeira; as outras
       (e o "Prefira o fim da tarde." que as acompanha) saem por redundância.

    Args:
        frases: frases da mensagem, na ordem (cada item pode ter mais de uma frase).

    Returns:
        Lista de frases simples (uma por item), prontas para ``" ".join``.
    """
    sentencas = [sentenca for frase in frases for sentenca in _dividir_sentencas(frase)]
    regas = [indice for indice, sentenca in enumerate(sentencas) if _PADRAO_REGA_POSITIVA.search(sentenca)]
    if not regas:
        return sentencas

    # 1) Geada acontecendo: proteção primeiro, rega de dia.
    if any(FRASE_GELANDO_AGORA in sentenca for sentenca in sentencas):
        protecao = [sentenca for indice, sentenca in enumerate(sentencas)
                    if indice not in regas and sentenca != FRASE_NAO_REGUE_A_NOITE]
        ultima_protecao = max((indice for indice, sentenca in enumerate(protecao)
                               if _PADRAO_OUTRA_ORDEM.search(sentenca)), default=len(protecao) - 1)
        return protecao[:ultima_protecao + 1] + [FRASE_REGA_NA_GEADA] + protecao[ultima_protecao + 1:]

    # 2) Terra molhada demais: a ordem de não regar ganha da rega para refrescar.
    if any(_PADRAO_NAO_REGAR.search(sentenca) for sentenca in sentencas):
        resultado: List[str] = []
        for indice, sentenca in enumerate(sentencas):
            if indice in regas:
                sentenca = _tirar_rega_da_frase(sentenca)
            if sentenca:
                resultado.append(sentenca)
        return resultado

    # 3) Duas ou mais regas: fica uma.
    if len(regas) == 1:
        return sentencas
    principal = next((indice for indice in regas if _PADRAO_OUTRA_ORDEM.search(sentencas[indice])), regas[0])
    resultado = []
    removida_anterior = False
    for indice, sentenca in enumerate(sentencas):
        if sentenca == FRASE_PREFIRA_FIM_DA_TARDE and removida_anterior:
            continue
        removida_anterior = False
        if indice in regas and indice != principal:
            restante = _tirar_rega_da_frase(sentenca)
            removida_anterior = True
            if restante:
                resultado.append(restante)
            continue
        resultado.append(sentenca)
    return resultado


def gerar_mensagem_orientacao(
    status: int,
    deficit_hidrico_solo: float,
    desvio_termico: float,
    parametros: ParametrosCultura,
    temperatura_ar: float,
    umidade_solo: float,
) -> str:
    """Monta a orientação falada ao agricultor (frases curtas, sem jargão nem símbolos).

    Estrutura: ESTADO (tudo bem / atenção / perigo) + CAUSA (falta de água,
    excesso de água, calor, frio ou combinações) + AÇÃO concreta. Começa com
    "Urgente!" quando temperatura ou umidade do solo passa do limite crítico da
    cultura (dano severo: geada, escaldadura, murcha permanente, raiz afogada).
    """
    try:
        nivel = int(status)
    except (TypeError, ValueError):
        nivel = STATUS_VERMELHO
    if nivel not in NOMES_STATUS:
        nivel = STATUS_VERMELHO
    faixa_temperatura = parametros.faixa(VARIAVEL_TEMPERATURA_AR)
    faixa_solo = parametros.faixa(VARIAVEL_UMIDADE_SOLO)
    plantacao = f"a plantação de {parametros.nome_comum.lower()}"
    agua_solo = _numero_falado(umidade_solo)

    # Causas pelo sinal das features; intensidade pela classe de CADA variável.
    falta_agua = deficit_hidrico_solo > 0
    excesso_agua = deficit_hidrico_solo < 0
    calor = desvio_termico > 0
    frio = desvio_termico < 0
    solo_vermelho = classificar_variavel(umidade_solo, faixa_solo) == STATUS_VERMELHO
    temperatura_vermelha = classificar_variavel(temperatura_ar, faixa_temperatura) == STATUS_VERMELHO
    urgente = (umidade_solo < faixa_solo.critico_min or umidade_solo > faixa_solo.critico_max
               or temperatura_ar < faixa_temperatura.critico_min or temperatura_ar > faixa_temperatura.critico_max)

    if nivel == STATUS_VERDE and not urgente:
        return (f"Tudo bem com {plantacao}. A terra está molhada na medida e o tempo está bom. "
                "Não precisa fazer nada agora.")

    if urgente:
        inicio = f"Urgente! Perigo para {plantacao}."
    elif nivel == STATUS_VERMELHO:
        inicio = f"Perigo para {plantacao}."
    else:
        inicio = f"Atenção com {plantacao}."

    frases: List[str] = [inicio]
    if falta_agua:
        frases.append(f"A terra está {'muito seca' if solo_vermelho else 'secando'}, com só {agua_solo} por cento de água.")
    elif excesso_agua:
        frases.append(f"A terra está {'encharcada' if solo_vermelho else 'molhada demais'}, com {agua_solo} por cento de água.")
    if calor:
        frases.append(f"Está {'quente demais' if temperatura_vermelha else 'quente'}, {falar_graus(temperatura_ar)}.")
    elif frio:
        frases.append(f"Está {'frio demais' if temperatura_vermelha else 'frio'}, {falar_graus(temperatura_ar)}.")

    # Ação para a água (o horário da rega muda com frio ou calor).
    if falta_agua and frio:
        frases.append("Regue agora, de dia, até a terra ficar úmida. Não regue à noite." if solo_vermelho
                      else "Regue hoje de manhã. Não regue à noite.")
    elif falta_agua and calor:
        frases.append("Regue hoje, devagar, até a terra ficar úmida. Prefira o fim da tarde." if solo_vermelho
                      else "Regue hoje no fim da tarde, quando o sol baixar.")
    elif falta_agua:
        frases.append("Regue agora, devagar, até a terra ficar úmida." if solo_vermelho
                      else "Regue hoje no fim da tarde.")
    elif excesso_agua:
        frases.append("Pare de regar e abra valetas para a água escorrer." if solo_vermelho
                      else "Não regue hoje. Espere a terra secar um pouco.")

    # Ação para a temperatura.
    if calor and temperatura_vermelha:
        frases.append("Faça sombra nas mudas novas." if (falta_agua or excesso_agua)
                      else "Faça sombra nas mudas novas e regue no fim da tarde para refrescar.")
    elif calor and not (falta_agua or excesso_agua):
        frases.append("Regue no fim da tarde para refrescar as plantas.")
    elif frio and temperatura_ar < faixa_temperatura.critico_min:
        # A leitura já está abaixo do limite crítico: a geada está acontecendo
        # AGORA (o aviso com antecedência é do prognóstico, em prognostico.py).
        frases.append("Está gelando agora. Cubra as mudas e chegue terra no pé das plantas assim que puder.")
    elif frio and temperatura_vermelha:
        frases.append("Cubra as mudas novas e chegue terra no pé das plantas antes de escurecer.")
    elif frio:
        frases.append("Proteja as mudas do frio hoje à noite.")

    if not (falta_agua or excesso_agua or calor or frio):
        # Alerta sem causa fora da faixa ideal (a IA elevou o status): aponta a
        # borda mais próxima, para a orientação continuar concreta.
        frases.append(_frase_borda_proxima(temperatura_ar, umidade_solo, faixa_temperatura, faixa_solo))
    return " ".join(_juntar_acoes(frases))


def _frase_borda_proxima(
    temperatura_ar: float, umidade_solo: float, faixa_temperatura: FaixaParametro, faixa_solo: FaixaParametro
) -> str:
    """Orientação concreta quando tudo está na faixa ideal, mas perto de uma borda.

    Escolhe, entre calor, frio, seca e excesso de água, a borda ideal mais
    próxima (distância relativa à largura da faixa ideal) e diz o que observar.
    """
    largura_temperatura = max(1e-6, faixa_temperatura.ideal_max - faixa_temperatura.ideal_min)
    largura_solo = max(1e-6, faixa_solo.ideal_max - faixa_solo.ideal_min)
    margens: Dict[str, float] = {
        "calor": (faixa_temperatura.ideal_max - temperatura_ar) / largura_temperatura,
        "frio": (temperatura_ar - faixa_temperatura.ideal_min) / largura_temperatura,
        "seca": (umidade_solo - faixa_solo.ideal_min) / largura_solo,
        "excesso": (faixa_solo.ideal_max - umidade_solo) / largura_solo,
    }
    borda = min(margens, key=lambda chave: margens[chave])
    graus = falar_graus(temperatura_ar)
    agua_solo = _numero_falado(umidade_solo)
    if borda == "calor":
        return (f"Está {graus}, perto do limite de calor. "
                "Olhe as folhas hoje e regue no fim da tarde se esquentar mais.")
    if borda == "frio":
        return f"Está {graus}, perto do limite de frio. Proteja as mudas se esfriar à noite."
    if borda == "seca":
        return (f"A terra tem {agua_solo} por cento de água e está quase secando. "
                "Olhe a terra hoje e prepare a rega.")
    return (f"A terra tem {agua_solo} por cento de água e está quase molhada demais. "
            "Não regue hoje.")


def gerar_mensagem_fisiologica(
    avaliacao: "AvaliacaoRegra",
    deficit_hidrico_solo: float,
    parametros: ParametrosCultura,
    temperatura_ar: float,
    umidade_solo: float,
) -> str:
    """Orientação falada da regra fisiológica (v3): ESTADO + CAUSA + AÇÃO.

    Usa os componentes de ``regras_agronomicas.AvaliacaoRegra`` (motivo da
    temperatura e do solo), e não a classe instantânea da temperatura: o frio
    ou o calor do dia sem dano é Amarelo INFORMATIVO ("Não precisa fazer nada
    agora"); só geada e calor fisiológico pedem ação de proteção. Contém as
    palavras-chave combinadas com os pictogramas (``acoes_orientacao``):
    ``GEADA_AGORA`` → "Cubra as mudas" e "chegue terra"; ``CALOR_FLORADA`` →
    "fim da tarde" e "sombra"; ``CALOR_SEGUIDO`` → "fim da tarde";
    ``DIA_FRIO``/``DIA_QUENTE``/``CALOR_DIA`` → "Não precisa fazer nada";
    ``SECA_PROGRAMADA`` → "Não regue" (nunca "Regue agora"). As frases do solo
    são as mesmas de ``gerar_mensagem_orientacao``.

    Args:
        avaliacao: resultado de ``regras_agronomicas.avaliar_status_campo``
            (``status`` pode vir elevado pela IA; sem motivo, a voz aponta a
            borda do solo mais próxima).
        deficit_hidrico_solo: feature do solo (positivo = falta de água). A causa
            vem de ``motivo_solo``; o déficit só escolhe o sentido da frase de
            borda quando a IA elevou o status sem motivo na regra.
        parametros: faixas da cultura.
        temperatura_ar, umidade_solo: leitura saneada (números falados).
    """
    # Importação tardia: regras_agronomicas importa este módulo.
    from regras_agronomicas import (
        MOTIVO_CALOR_DIA,
        MOTIVO_CALOR_FLORADA,
        MOTIVO_CALOR_SEGUIDO,
        MOTIVO_DIA_FRIO,
        MOTIVO_DIA_QUENTE,
        MOTIVO_ENCHARCAMENTO,
        MOTIVO_GEADA_AGORA,
        MOTIVO_NENHUM,
        MOTIVO_SECA_AGORA,
        MOTIVO_SECA_PROGRAMADA,
    )

    try:
        nivel = int(avaliacao.status)
    except (TypeError, ValueError):
        nivel = STATUS_VERMELHO
    if nivel not in NOMES_STATUS:
        nivel = STATUS_VERMELHO
    faixa_temperatura = parametros.faixa(VARIAVEL_TEMPERATURA_AR)
    faixa_solo = parametros.faixa(VARIAVEL_UMIDADE_SOLO)
    plantacao = f"a plantação de {parametros.nome_comum.lower()}"
    para_cultura = "para o café" if parametros.categoria == "cafe" else "para a plantação"
    graus = falar_graus(temperatura_ar)
    agua_solo = _numero_falado(umidade_solo)
    motivo_temperatura = str(getattr(avaliacao, "motivo_temperatura", MOTIVO_NENHUM) or MOTIVO_NENHUM)
    motivo_solo = str(getattr(avaliacao, "motivo_solo", MOTIVO_NENHUM) or MOTIVO_NENHUM)
    solo_vermelho = int(getattr(avaliacao, "status_solo", 0)) == STATUS_VERMELHO

    if nivel == STATUS_VERDE and motivo_temperatura == MOTIVO_NENHUM and motivo_solo == MOTIVO_NENHUM:
        return (f"Tudo bem com {plantacao}. A terra está molhada na medida e o tempo está bom. "
                "Não precisa fazer nada agora.")

    calor_perigoso = motivo_temperatura in (MOTIVO_CALOR_FLORADA, MOTIVO_CALOR_SEGUIDO)
    frio = motivo_temperatura in (MOTIVO_GEADA_AGORA, MOTIVO_DIA_FRIO)
    quente = motivo_temperatura in (MOTIVO_CALOR_FLORADA, MOTIVO_CALOR_SEGUIDO, MOTIVO_CALOR_DIA, MOTIVO_DIA_QUENTE)
    urgente = (motivo_temperatura == MOTIVO_GEADA_AGORA or calor_perigoso
               or (motivo_solo == MOTIVO_SECA_AGORA and umidade_solo < faixa_solo.critico_min)
               or (motivo_solo == MOTIVO_ENCHARCAMENTO and umidade_solo > faixa_solo.critico_max))
    if urgente and nivel == STATUS_VERMELHO:
        inicio = f"Urgente! Perigo para {plantacao}."
    elif nivel == STATUS_VERMELHO:
        inicio = f"Perigo para {plantacao}."
    else:
        inicio = f"Atenção com {plantacao}."
    frases: List[str] = [inicio]

    # CAUSA — solo.
    if motivo_solo == MOTIVO_SECA_PROGRAMADA:
        frases.append("A terra está seca de propósito para a florada.")
    elif motivo_solo == MOTIVO_SECA_AGORA:
        frases.append(f"A terra está {'muito seca' if solo_vermelho else 'secando'}, com só {agua_solo} por cento de água.")
    elif motivo_solo == MOTIVO_ENCHARCAMENTO:
        frases.append(f"A terra está {'encharcada' if solo_vermelho else 'molhada demais'}, com {agua_solo} por cento de água.")
    # CAUSA — temperatura.
    if motivo_temperatura == MOTIVO_GEADA_AGORA:
        frases.append(f"Está muito frio, {graus}.")
    elif calor_perigoso:
        frases.append(f"Está quente demais, {graus}.")
    elif motivo_temperatura == MOTIVO_CALOR_DIA:
        frases.append(f"O dia esquentou demais {para_cultura}.")
    elif motivo_temperatura == MOTIVO_DIA_FRIO:
        frases.append(f"O dia está frio {para_cultura}.")
    elif motivo_temperatura == MOTIVO_DIA_QUENTE:
        frases.append(f"O dia está quente {para_cultura}.")

    # AÇÃO — água (o horário da rega muda com frio ou calor, como na v2).
    acao_agua = False
    if motivo_solo == MOTIVO_SECA_PROGRAMADA:
        if not calor_perigoso:
            frases.append("Não regue ainda.")
        acao_agua = True
    elif motivo_solo == MOTIVO_SECA_AGORA:
        if frio:
            frases.append("Regue agora, de dia, até a terra ficar úmida. Não regue à noite." if solo_vermelho
                          else "Regue hoje de manhã. Não regue à noite.")
        elif quente:
            frases.append("Regue hoje, devagar, até a terra ficar úmida. Prefira o fim da tarde." if solo_vermelho
                          else "Regue hoje no fim da tarde, quando o sol baixar.")
        else:
            frases.append("Regue agora, devagar, até a terra ficar úmida." if solo_vermelho
                          else "Regue hoje no fim da tarde.")
        acao_agua = True
    elif motivo_solo == MOTIVO_ENCHARCAMENTO:
        frases.append("Pare de regar e abra valetas para a água escorrer." if solo_vermelho
                      else "Não regue hoje. Espere a terra secar um pouco.")
        acao_agua = True

    # AÇÃO — temperatura (só geada e calor fisiológico pedem proteção).
    if motivo_temperatura == MOTIVO_GEADA_AGORA:
        frases.append("Está gelando agora. Cubra as mudas e chegue terra no pé das plantas assim que puder.")
    elif motivo_temperatura == MOTIVO_CALOR_FLORADA:
        frases.append("Calor forte na florada. Regue no fim da tarde e faça sombra nas mudas.")
    elif motivo_temperatura == MOTIVO_CALOR_SEGUIDO:
        frases.append("Calor forte há dois dias. Regue no fim da tarde.")
    elif motivo_temperatura in (MOTIVO_CALOR_DIA, MOTIVO_DIA_FRIO, MOTIVO_DIA_QUENTE) and not acao_agua:
        frases.append("Não precisa fazer nada agora.")

    if motivo_temperatura == MOTIVO_NENHUM and motivo_solo == MOTIVO_NENHUM:
        # Alerta sem causa na regra (a IA, que vê a temperatura neutralizada,
        # elevou o status): aponta a borda do SOLO mais próxima.
        if deficit_hidrico_solo > 0:
            frases.append(f"A terra tem {agua_solo} por cento de água e está quase secando. "
                          "Olhe a terra hoje e prepare a rega.")
        elif deficit_hidrico_solo < 0:
            frases.append(f"A terra tem {agua_solo} por cento de água e está quase molhada demais. Não regue hoje.")
        else:
            ponto_medio = (faixa_temperatura.ideal_min + faixa_temperatura.ideal_max) / 2.0
            frases.append(_frase_borda_proxima(ponto_medio, umidade_solo, faixa_temperatura, faixa_solo))
    return " ".join(_juntar_acoes(frases))


# ---------------------------------------------------------------------------
# Simulador em tempo real (Modo Simulação do coletor)
# ---------------------------------------------------------------------------
class SimuladorTempoReal:
    """Gera leituras sintéticas plausíveis em tempo real quando o Arduino falha.

    * Temperatura: ponto médio da faixa ideal + senoide diária pelo relógio
      (pico às 15 h) + deriva lenta (passeio aleatório limitado) + ruído pequeno.
    * Umidade do ar: inversamente correlacionada à temperatura.
    * Umidade do solo com MEMÓRIA: perde água por evapotranspiração e recebe
      irrigação gradual quando cai abaixo da faixa ideal. A dinâmica do solo é
      acelerada (``FATOR_ACELERACAO_SOLO``) para que a mudança seja visível em
      uma demonstração de poucos minutos, sem saltos que o saneador recusaria.
    """

    FATOR_ACELERACAO_SOLO: float = 60.0
    PASSO_MAXIMO_HORAS: float = 1.0
    TAXA_IRRIGACAO_POR_HORA: float = 8.0
    HORAS_MEMORIA_DERIVA: float = 6.0

    def __init__(self, parametros: ParametrosCultura, semente: Optional[int] = None) -> None:
        """Prepara o estado inicial a partir das faixas da cultura."""
        self.parametros: ParametrosCultura = parametros
        self._gerador: np.random.Generator = np.random.default_rng(semente)
        faixa_temperatura = parametros.faixa(VARIAVEL_TEMPERATURA_AR)
        faixa_solo = parametros.faixa(VARIAVEL_UMIDADE_SOLO)
        faixa_ar = parametros.faixa(VARIAVEL_UMIDADE_AR)
        self._temperatura_base: float = (faixa_temperatura.ideal_min + faixa_temperatura.ideal_max) / 2.0
        self._amplitude_diaria: float = max(1.0, 0.45 * (faixa_temperatura.ideal_max - faixa_temperatura.ideal_min))
        self._umidade_ar_base: float = (faixa_ar.ideal_min + faixa_ar.ideal_max) / 2.0
        self._solo_minimo_irrigacao: float = faixa_solo.ideal_min + 0.1 * (faixa_solo.ideal_max - faixa_solo.ideal_min)
        self._solo_alvo_irrigacao: float = faixa_solo.ideal_max - 0.15 * (faixa_solo.ideal_max - faixa_solo.ideal_min)
        self._umidade_solo: float = (faixa_solo.ideal_min + faixa_solo.ideal_max) / 2.0 + float(self._gerador.normal(0.0, 2.0))
        self._deriva_temperatura: float = 0.0
        self._irrigando: bool = False
        self._ultimo_instante: Optional[datetime] = None

    def proxima_leitura(self, instante: Optional[datetime] = None) -> Dict[str, float]:
        """Retorna ``{"temperatura_ar", "umidade_ar", "umidade_solo"}`` para o instante dado (padrão: agora)."""
        momento = instante if instante is not None else datetime.now()
        if self._ultimo_instante is None:
            passo_horas = 5.0 / 3600.0
        else:
            passo_horas = max(0.0, (momento - self._ultimo_instante).total_seconds() / 3600.0)
        self._ultimo_instante = momento
        passo_solo = min(self.PASSO_MAXIMO_HORAS, passo_horas * self.FATOR_ACELERACAO_SOLO)

        hora_decimal = momento.hour + momento.minute / 60.0 + momento.second / 3600.0
        ciclo = math.sin(2.0 * math.pi * (hora_decimal - 9.0) / 24.0)
        fator_solar = max(0.0, math.sin(2.0 * math.pi * (hora_decimal - 6.0) / 24.0))

        # Deriva lenta da temperatura em tempo REAL (processo de Ornstein-Uhlenbeck:
        # memória de ~6 h, desvio estacionário de 0,6 grau, limitado a ±2 graus),
        # independente da frequência de chamadas.
        persistencia = math.exp(-passo_horas / self.HORAS_MEMORIA_DERIVA)
        self._deriva_temperatura = float(np.clip(
            persistencia * self._deriva_temperatura
            + self._gerador.normal(0.0, 0.6 * math.sqrt(max(0.0, 1.0 - persistencia ** 2))), -2.0, 2.0))
        temperatura = (self._temperatura_base + self._amplitude_diaria * ciclo + self._deriva_temperatura
                       + float(self._gerador.normal(0.0, 0.15)))
        umidade_ar = (self._umidade_ar_base - 3.0 * (temperatura - self._temperatura_base)
                      + float(self._gerador.normal(0.0, 0.8)))

        # Balanço hídrico do solo (evapotranspiração diurna x irrigação gradual).
        evapotranspiracao = 0.09 * max(0.0, temperatura - 5.0) * fator_solar * (1.0 - 0.5 * umidade_ar / 100.0)
        self._umidade_solo -= evapotranspiracao * passo_solo
        if self._umidade_solo < self._solo_minimo_irrigacao:
            self._irrigando = True
        if self._irrigando:
            self._umidade_solo += self.TAXA_IRRIGACAO_POR_HORA * passo_solo
            if self._umidade_solo >= self._solo_alvo_irrigacao:
                self._irrigando = False

        limites = LIMITES_FISICOS_SENSORES
        temperatura = float(np.clip(temperatura, *limites["temperatura_ar"]))
        umidade_ar = float(np.clip(umidade_ar, 5.0, 99.0))
        self._umidade_solo = float(np.clip(self._umidade_solo, *limites["umidade_solo"]))
        return {
            "temperatura_ar": round(temperatura, 1),
            "umidade_ar": round(umidade_ar, 1),
            "umidade_solo": round(self._umidade_solo, 1),
        }


# ---------------------------------------------------------------------------
# Cenários do Modo Demonstração (banca)
# ---------------------------------------------------------------------------
CENARIOS_DEMONSTRACAO: Dict[str, Dict[str, str]] = {
    "SECA_EXTREMA": {
        "rotulo": "Simular Seca Extrema",
        "descricao": "Solo muito abaixo da umidade mínima da cultura, ar seco e tarde quente.",
        "status_esperado": "2",
    },
    "ONDA_DE_CALOR": {
        "rotulo": "Simular Onda de Calor",
        "descricao": "Temperatura do ar acima do limite crítico da cultura, com o solo ainda na faixa ideal.",
        "status_esperado": "2",
    },
    "GEADA": {
        "rotulo": "Simular Geada",
        "descricao": "Madrugada fria abaixo do limite crítico da cultura, ar úmido e solo na faixa ideal.",
        "status_esperado": "2",
    },
    "ENCHARCAMENTO": {
        "rotulo": "Simular Encharcamento",
        "descricao": "Chuva forte deixa o solo acima do limite de atenção (raiz sem ar), temperatura ideal.",
        "status_esperado": "2",
    },
    "ATENCAO_MODERADA": {
        "rotulo": "Simular Atenção Moderada",
        "descricao": "Leve calor e solo começando a secar: ambos na faixa de atenção, sem risco crítico.",
        "status_esperado": "1",
    },
    "CONDICAO_IDEAL": {
        "rotulo": "Simular Condição Ideal",
        "descricao": "Temperatura, umidade do ar e umidade do solo no meio da faixa ideal da cultura.",
        "status_esperado": "0",
    },
}


def _valor_abaixo(faixa: FaixaParametro, limite_fisico: Tuple[float, float], margem_critica: float) -> float:
    """Valor Vermelho abaixo de ``atencao_min``, preferindo passar do crítico, dentro da faixa física."""
    fisico_min = limite_fisico[0]
    alvo = faixa.critico_min - margem_critica
    if alvo < fisico_min + 0.5:
        alvo = (fisico_min + faixa.atencao_min) / 2.0
    valor = round(alvo, 1)
    if not (fisico_min <= valor < faixa.atencao_min):
        raise ValueError(
            f"Não há valor medível abaixo da borda de atenção de '{faixa.variavel}' "
            f"({faixa.atencao_min}) dentro da faixa física {limite_fisico}."
        )
    return valor


def _valor_acima(faixa: FaixaParametro, limite_fisico: Tuple[float, float], margem_critica: float) -> float:
    """Valor Vermelho acima de ``atencao_max``, preferindo passar do crítico, dentro da faixa física."""
    fisico_max = limite_fisico[1]
    alvo = faixa.critico_max + margem_critica
    if alvo > fisico_max - 0.5:
        alvo = (fisico_max + faixa.atencao_max) / 2.0
    valor = round(alvo, 1)
    if not (faixa.atencao_max < valor <= fisico_max):
        raise ValueError(
            f"Não há valor medível acima da borda de atenção de '{faixa.variavel}' "
            f"({faixa.atencao_max}) dentro da faixa física {limite_fisico}."
        )
    return valor


def _ponto_ideal(faixa: FaixaParametro, fracao: float) -> float:
    """Ponto dentro da faixa ideal: ``fracao`` 0 = ideal_min, 1 = ideal_max (arredondado e garantido)."""
    valor = round(faixa.ideal_min + fracao * (faixa.ideal_max - faixa.ideal_min), 1)
    return float(min(max(valor, faixa.ideal_min), faixa.ideal_max))


def _ponto_atencao(faixa: FaixaParametro, lado: str) -> Optional[float]:
    """Ponto no meio da faixa amarela do lado pedido (``"acima"``/``"abaixo"``); ``None`` se ela não existir."""
    if lado == "acima":
        inicio, fim = faixa.ideal_max, faixa.atencao_max
    else:
        inicio, fim = faixa.atencao_min, faixa.ideal_min
    if fim - inicio < 0.2:
        return None
    valor = round((inicio + fim) / 2.0, 1)
    if classificar_variavel(valor, faixa) != STATUS_AMARELO:
        return None
    return valor


def _umidade_ar_relativa(faixa: FaixaParametro, posicao: str) -> float:
    """Umidade do ar coerente com o cenário: ``"seca"``, ``"ideal"`` ou ``"umida"``."""
    limite_min, limite_max = LIMITES_FISICOS_SENSORES[VARIAVEL_UMIDADE_AR]
    if posicao == "seca":
        valor = faixa.atencao_min - 0.5 * (faixa.atencao_min - faixa.critico_min)
    elif posicao == "umida":
        valor = (faixa.atencao_max + faixa.critico_max) / 2.0
    else:
        valor = (faixa.ideal_min + faixa.ideal_max) / 2.0
    return round(float(min(max(valor, limite_min + 1.0), limite_max - 1.0)), 1)


def gerar_leitura_cenario(cenario: str, parametros: ParametrosCultura) -> Dict[str, float]:
    """Gera uma leitura extrema para o Modo Demonstração.

    Os valores são calculados RELATIVOS às faixas da cultura (funciona para
    qualquer cultura cadastrada), ficam sempre dentro de
    ``LIMITES_FISICOS_SENSORES`` e produzem ``calcular_status_regra`` igual ao
    ``status_esperado`` do cenário.

    Raises:
        ValueError: cenário desconhecido ou cultura cujas faixas não permitem o cenário.
    """
    if cenario not in CENARIOS_DEMONSTRACAO:
        raise ValueError(f"Cenário desconhecido: {cenario!r}. Disponíveis: {', '.join(CENARIOS_DEMONSTRACAO)}.")
    faixa_temperatura = parametros.faixa(VARIAVEL_TEMPERATURA_AR)
    faixa_solo = parametros.faixa(VARIAVEL_UMIDADE_SOLO)
    faixa_ar = parametros.faixa(VARIAVEL_UMIDADE_AR)

    if cenario == "SECA_EXTREMA":
        leitura = {
            "temperatura_ar": _ponto_ideal(faixa_temperatura, 0.9),
            "umidade_ar": _umidade_ar_relativa(faixa_ar, "seca"),
            "umidade_solo": _valor_abaixo(faixa_solo, LIMITES_FISICOS_SENSORES["umidade_solo"], 5.0),
        }
    elif cenario == "ONDA_DE_CALOR":
        leitura = {
            "temperatura_ar": _valor_acima(faixa_temperatura, LIMITES_FISICOS_SENSORES["temperatura_ar"], 3.0),
            "umidade_ar": _umidade_ar_relativa(faixa_ar, "seca"),
            "umidade_solo": _ponto_ideal(faixa_solo, 0.3),
        }
    elif cenario == "GEADA":
        leitura = {
            "temperatura_ar": _valor_abaixo(faixa_temperatura, LIMITES_FISICOS_SENSORES["temperatura_ar"], 3.0),
            "umidade_ar": _umidade_ar_relativa(faixa_ar, "umida"),
            "umidade_solo": _ponto_ideal(faixa_solo, 0.5),
        }
    elif cenario == "ENCHARCAMENTO":
        leitura = {
            "temperatura_ar": _ponto_ideal(faixa_temperatura, 0.5),
            "umidade_ar": _umidade_ar_relativa(faixa_ar, "umida"),
            "umidade_solo": _valor_acima(faixa_solo, LIMITES_FISICOS_SENSORES["umidade_solo"], 3.0),
        }
    elif cenario == "ATENCAO_MODERADA":
        temperatura_atencao = _ponto_atencao(faixa_temperatura, "acima") or _ponto_atencao(faixa_temperatura, "abaixo")
        solo_atencao = _ponto_atencao(faixa_solo, "abaixo") or _ponto_atencao(faixa_solo, "acima")
        if temperatura_atencao is None and solo_atencao is None:
            raise ValueError(f"A cultura '{parametros.codigo}' não possui faixa de atenção para temperatura nem solo.")
        leitura = {
            "temperatura_ar": temperatura_atencao if temperatura_atencao is not None else _ponto_ideal(faixa_temperatura, 0.5),
            "umidade_ar": _umidade_ar_relativa(faixa_ar, "ideal"),
            "umidade_solo": solo_atencao if solo_atencao is not None else _ponto_ideal(faixa_solo, 0.5),
        }
    else:  # CONDICAO_IDEAL
        leitura = {
            "temperatura_ar": _ponto_ideal(faixa_temperatura, 0.5),
            "umidade_ar": _umidade_ar_relativa(faixa_ar, "ideal"),
            "umidade_solo": _ponto_ideal(faixa_solo, 0.5),
        }

    for coluna, valor in leitura.items():
        minimo, maximo = LIMITES_FISICOS_SENSORES[coluna]
        if not minimo <= valor <= maximo:
            raise ValueError(f"Cenário {cenario} gerou {coluna}={valor} fora da faixa física {minimo}–{maximo}.")
    esperado = int(CENARIOS_DEMONSTRACAO[cenario]["status_esperado"])
    obtido = calcular_status_regra(leitura["temperatura_ar"], leitura["umidade_solo"], parametros)
    if obtido != esperado:
        raise ValueError(
            f"As faixas da cultura '{parametros.codigo}' não permitem montar o cenário {cenario} "
            f"(status obtido {obtido}, esperado {esperado})."
        )
    return {chave: float(valor) for chave, valor in leitura.items()}


# ---------------------------------------------------------------------------
# Simulação da série temporal (dataset de treino)
# ---------------------------------------------------------------------------
def ponto_orvalho_magnus(
    temperatura_c: Union[float, np.ndarray], umidade_relativa: Union[float, np.ndarray]
) -> Union[float, np.ndarray]:
    """Ponto de orvalho (°C) pela fórmula de Magnus (coeficientes de Sonntag, 17,62 e 243,12 °C).

    ``γ = ln(UR/100) + a·T/(b + T)`` e ``Td = b·γ/(a − γ)``. A umidade relativa
    é limitada a ``[1, 100]`` % para evitar ``ln(0)``. Aceita números ou
    vetores numpy (devolve o mesmo tipo).

    Exemplo: ``ponto_orvalho_magnus(12, 40)`` ≈ −1,2 °C (ar frio e seco do fim
    da tarde, típico de noite de geada).
    """
    temperatura = np.asarray(temperatura_c, dtype=float)
    umidade = np.clip(np.asarray(umidade_relativa, dtype=float), 1.0, 100.0)
    gama = np.log(umidade / 100.0) + MAGNUS_A * temperatura / (MAGNUS_B + temperatura)
    orvalho = MAGNUS_B * gama / (MAGNUS_A - gama)
    if np.ndim(orvalho) == 0:
        return float(orvalho)
    return orvalho


def umidade_relativa_magnus(
    temperatura_c: Union[float, np.ndarray], ponto_orvalho_c: Union[float, np.ndarray]
) -> Union[float, np.ndarray]:
    """Inversa de ``ponto_orvalho_magnus``: umidade relativa (%) do ar com temperatura e ponto de orvalho dados.

    ``UR = 100·exp(a·Td/(b+Td)) / exp(a·T/(b+T))``, sem limite superior (o
    chamador limita a 99 % quando o ar satura).
    """
    temperatura = np.asarray(temperatura_c, dtype=float)
    orvalho = np.asarray(ponto_orvalho_c, dtype=float)
    umidade = 100.0 * np.exp(MAGNUS_A * orvalho / (MAGNUS_B + orvalho) - MAGNUS_A * temperatura / (MAGNUS_B + temperatura))
    if np.ndim(umidade) == 0:
        return float(umidade)
    return umidade


def _envelope_evento(duracao: int) -> np.ndarray:
    """Envelope suave (seno) para ondas de calor/frentes frias, sem degraus bruscos."""
    posicoes = np.arange(duracao, dtype=float)
    return np.sin(np.pi * (posicoes + 0.5) / duracao) ** 0.6


def _sortear_eventos(
    gerador: np.random.Generator, horas: int, intervalo_medio: float, duracao_min: int, duracao_max: int
) -> List[Tuple[int, int]]:
    """Sorteia eventos (início, duração) com intervalos exponenciais, sem sobreposição."""
    eventos: List[Tuple[int, int]] = []
    instante = int(gerador.exponential(intervalo_medio / 2.0))
    while instante < horas:
        duracao = int(gerador.integers(duracao_min, duracao_max + 1))
        eventos.append((instante, duracao))
        instante += duracao + int(gerador.exponential(intervalo_medio)) + 12
    return eventos


def _sortear_veranicos(gerador: np.random.Generator, instantes: pd.DatetimeIndex) -> np.ndarray:
    """Marca as horas de veranico: em cada ano, com probabilidade ``PROB_VERANICO_ANO``,
    um bloco de ``DURACAO_VERANICO_DIAS`` dias seguidos sem chuva que começa em janeiro ou fevereiro."""
    veranico = np.zeros(len(instantes), dtype=bool)
    for ano in sorted(set(int(valor) for valor in instantes.year)):
        sorteio = gerador.random()
        inicio_dia = int(gerador.integers(0, 49))  # 1º de janeiro + 0..48 dias → começa em jan. ou fev.
        duracao_dias = int(gerador.integers(DURACAO_VERANICO_DIAS[0], DURACAO_VERANICO_DIAS[1] + 1))
        if sorteio >= PROB_VERANICO_ANO:
            continue
        inicio = pd.Timestamp(year=ano, month=1, day=1) + pd.Timedelta(days=inicio_dia)
        fim = inicio + pd.Timedelta(days=duracao_dias)
        veranico |= np.asarray((instantes >= inicio) & (instantes < fim))
    return veranico


def _sortear_chuvas(
    gerador: np.random.Generator, instantes: pd.DatetimeIndex, veranico: np.ndarray, largura_solo: float
) -> np.ndarray:
    """Lâmina de chuva (pontos percentuais de umidade do solo por hora).

    Para cada dia, chove com a probabilidade do mês (``PROBABILIDADE_CHUVA_MES``),
    exceto nos dias de veranico. Na estação chuvosa a chuva costuma ser de
    pancada à tarde (13 h–20 h); na seca, garoa de frente fria em qualquer hora.
    """
    horas = len(instantes)
    chuva = np.zeros(horas)
    _, primeiro_indice = np.unique(np.asarray(instantes.normalize()), return_index=True)
    for inicio_dia in primeiro_indice:
        momento = instantes[inicio_dia]
        probabilidade = PROBABILIDADE_CHUVA_MES[int(momento.month)]
        sorteio = gerador.random()
        convectiva = gerador.random() < 0.7
        hora_inicio = int(gerador.integers(13, 21)) if convectiva and momento.month in MESES_CHUVOSOS else int(gerador.integers(0, 24))
        duracao = int(gerador.integers(1, 6))
        total = gerador.uniform(0.25, 1.1) * largura_solo
        if sorteio >= probabilidade:
            continue
        deslocamento = hora_inicio - int(momento.hour)
        inicio = int(inicio_dia) + max(0, deslocamento)
        if inicio >= horas or veranico[inicio]:
            continue
        fim = min(horas, inicio + duracao)
        chuva[inicio:fim] += total / duracao
    chuva[veranico] = 0.0
    return chuva


def simular_serie_temporal(
    parametros: ParametrosCultura,
    horas: int = HORAS_SIMULACAO,
    semente: int = SEMENTE_ALEATORIA,
    data_inicial: str = "2026-01-01 00:00:00",
) -> pd.DataFrame:
    """Simula ``horas`` leituras horárias do talhão de uma cultura, com o clima de um ano no hemisfério sul.

    Componentes (todos RELATIVOS às faixas da cultura, para que qualquer
    cultura cadastrada funcione):

    * **Estações do ano**: componente anual de 8.760 h com mínimo em meados de
      julho (``AMPLITUDE_ANUAL_RELATIVA`` × largura ideal de temperatura) e uma
      deriva lenta de 45 dias, de amplitude menor.
    * **Ciclo diário**: senoide com pico às 15 h durante o dia; à noite a
      temperatura cai até a mínima da madrugada, que segue o ponto de orvalho
      do fim da tarde (``ponto_orvalho_magnus``): ``Tmin = Td − k``, com ``k``
      maior quanto mais seco o ar. Em noite comum (ar úmido) ``k`` é pequeno.
    * **Umidade do ar**: derivada do ponto de orvalho (fórmula de Magnus) — sobe
      à noite até perto da saturação, cai à tarde, fica alta com chuva e baixa
      na estação seca e nas massas de ar polar.
    * **Chuva**: probabilidade diária por mês (``PROBABILIDADE_CHUVA_MES``:
      estação chuvosa de outubro a março, seca de maio a setembro).
    * **Veranico**: com probabilidade ``PROB_VERANICO_ANO``, de 10 a 20 dias
      seguidos sem chuva em janeiro/fevereiro, com a máxima ``+k`` (``k``
      relativo a ``atencao_max − ideal_max``); o reservatório do produtor
      seca e não há irrigação nesses dias.
    * **Geada radiativa**: massas de ar polar (maio a agosto) esfriam e secam o
      ar; em noite com umidade do fim da tarde abaixo de
      ``UMIDADE_AR_NOITE_RADIATIVA``, a mínima entre 4 h e 7 h fica de 1 a 3 °C
      abaixo do ponto de orvalho, o que leva a temperatura abaixo de
      ``critico_min``. Temperatura abaixo de ``critico_min`` só acontece nessas
      madrugadas (0 h a 8 h, de maio a agosto).
    * **Escaldadura**: algumas ondas de calor de verão (dezembro a março)
      passam de ``critico_max`` à tarde.
    * **Umidade do solo**: balanço hídrico horário — evapotranspiração
      (∝ max(0, T−5) × fator solar × (1 − 0,5·UR/100) × coeficiente de
      estresse), chuva, irrigação por gotejamento do produtor (lenta, com
      atraso, panes de bomba e excesso ocasional) e drenagem acima da
      capacidade de campo; limitada a [0, 100].

    Returns:
        DataFrame com ``data_hora, cultura, temperatura_ar, umidade_ar, umidade_solo``.
    """
    if horas < 1:
        raise ValueError("horas deve ser >= 1.")
    gerador = np.random.default_rng(semente)
    faixa_temperatura = parametros.faixa(VARIAVEL_TEMPERATURA_AR)
    faixa_solo = parametros.faixa(VARIAVEL_UMIDADE_SOLO)
    indices_hora = np.arange(horas, dtype=float)
    instantes = pd.date_range(start=pd.Timestamp(data_inicial), periods=horas, freq="h")
    hora_do_dia = instantes.hour.to_numpy().astype(float)
    meses = instantes.month.to_numpy()
    dia_do_ano = instantes.day_of_year.to_numpy().astype(float) + hora_do_dia / 24.0
    estacao_seca = np.isin(meses, MESES_SECOS)
    meses_geada = np.isin(meses, MESES_GEADA)

    # ----- Temperatura: estações, deriva e ciclo diário -----------------------
    largura_ideal = faixa_temperatura.ideal_max - faixa_temperatura.ideal_min
    folga_quente = faixa_temperatura.atencao_max - faixa_temperatura.ideal_max
    folga_fria = faixa_temperatura.ideal_min - faixa_temperatura.atencao_min
    temperatura_base = (faixa_temperatura.ideal_min + faixa_temperatura.ideal_max) / 2.0
    componente_anual = -AMPLITUDE_ANUAL_RELATIVA * largura_ideal * np.cos(
        2.0 * np.pi * (dia_do_ano - DIA_MAIS_FRIO_DO_ANO) / 365.25)
    fase_deriva = gerador.uniform(0.0, 2.0 * np.pi)
    deriva = 0.12 * largura_ideal * np.sin(2.0 * np.pi * indices_hora / (24.0 * 45.0) + fase_deriva)
    dias_relativos = ((instantes.normalize() - instantes[0].normalize()) // pd.Timedelta(days=1)).to_numpy().astype(int)
    total_dias = int(dias_relativos.max()) + 2
    amplitude_media = 0.55 * largura_ideal * 0.925
    amplitude_dia = (0.55 * largura_ideal) * gerador.uniform(0.6, 1.25, size=total_dias)
    amplitude = amplitude_dia[dias_relativos] * np.where(estacao_seca, 1.1, 1.0)
    ciclo_diario = np.sin(2.0 * np.pi * (hora_do_dia - 9.0) / 24.0)
    pico_tarde = np.maximum(0.0, ciclo_diario)

    # ----- Ondas de calor, frentes frias, escaldadura --------------------------
    eventos_termicos = np.zeros(horas)
    for inicio, duracao in _sortear_eventos(gerador, horas, intervalo_medio=230.0, duracao_min=36, duracao_max=110):
        fim = min(horas, inicio + duracao)
        mes_inicio = int(meses[inicio])
        probabilidade_calor = 0.7 if mes_inicio in MESES_CHUVOSOS else 0.3
        sorteio_tipo, sorteio_intensidade, sorteio_escaldadura = gerador.random(), gerador.uniform(0.4, 1.4), gerador.random()
        excesso_escaldadura = gerador.uniform(0.4, 2.0)
        if sorteio_tipo < probabilidade_calor:
            intensidade = largura_ideal / 2.0 + folga_quente * sorteio_intensidade
            if mes_inicio in MESES_ESCALDADURA and sorteio_escaldadura < PROB_ESCALDADURA_ONDA_CALOR:
                # Pico da tarde passa do limite crítico (escaldadura das folhas e frutos).
                pico_normal = temperatura_base + componente_anual[inicio] + 1.25 * 0.55 * largura_ideal
                intensidade = max(intensidade, faixa_temperatura.critico_max - pico_normal + excesso_escaldadura)
        else:
            intensidade = -(largura_ideal / 2.0 + folga_fria * sorteio_intensidade)
        eventos_termicos[inicio:fim] += intensidade * _envelope_evento(duracao)[: fim - inicio]

    # ----- Massas de ar polar (maio a agosto): frio + ar seco ------------------
    queda_polar = np.zeros(horas)
    secura_polar = np.zeros(horas)
    for inicio, duracao in _sortear_eventos(gerador, horas, intervalo_medio=INTERVALO_MEDIO_AR_POLAR_HORAS,
                                            duracao_min=48, duracao_max=96):
        fim = min(horas, inicio + duracao)
        intensidade_frio = largura_ideal / 2.0 + folga_fria * gerador.uniform(0.2, 0.7)
        intensidade_secura = gerador.uniform(4.5, 7.5)
        if not meses_geada[inicio]:
            continue
        envelope = _envelope_evento(duracao)[: fim - inicio]
        queda_polar[inicio:fim] -= intensidade_frio * envelope
        secura_polar[inicio:fim] += intensidade_secura * envelope

    # ----- Veranico ------------------------------------------------------------
    veranico = _sortear_veranicos(gerador, instantes)
    aquecimento_veranico = gerador.uniform(0.5, 1.0) * max(0.5, folga_quente)

    ruido_termico = np.zeros(horas)
    choques = gerador.normal(0.0, 0.25, size=horas)
    for indice in range(1, horas):
        ruido_termico[indice] = 0.75 * ruido_termico[indice - 1] + choques[indice]

    temperatura_media = temperatura_base + componente_anual + deriva + eventos_termicos + queda_polar
    temperatura = (temperatura_media + amplitude * ciclo_diario + ruido_termico
                   + np.where(veranico, aquecimento_veranico * pico_tarde, 0.0))

    # ----- Chuva ------------------------------------------------------------------
    largura_solo = faixa_solo.ideal_max - faixa_solo.ideal_min
    chuva = _sortear_chuvas(gerador, instantes, veranico, largura_solo)
    chuva_recente = pd.Series(chuva).ewm(halflife=6.0).mean().to_numpy()
    umidade_por_chuva = np.minimum(1.0, chuva_recente / max(0.1, 0.1 * largura_solo))

    # ----- Ponto de orvalho e umidade do ar --------------------------------------
    # Depressão do ponto de orvalho no fim da tarde típica da cultura: faz a
    # mínima de uma noite comum ficar ~uma amplitude abaixo da média diária.
    depressao_tipica = 1.71 * amplitude_media / (1.0 + COEF_RESFRIAMENTO_NOTURNO)
    ruido_depressao = pd.Series(gerador.normal(0.0, 0.6, size=horas)).ewm(halflife=8.0).mean().to_numpy()
    depressao = (depressao_tipica * np.where(estacao_seca, 1.5, 1.0) + secura_polar
                 + np.where(veranico, 1.2, 0.0) + ruido_depressao)
    depressao = np.maximum(0.3, depressao * (1.0 - 0.85 * umidade_por_chuva))
    ponto_orvalho = temperatura_media + 0.71 * amplitude - depressao

    def umidade_do_ar(temperaturas: np.ndarray) -> np.ndarray:
        """Umidade relativa (%) a partir da temperatura e do ponto de orvalho horário."""
        return np.clip(np.asarray(umidade_relativa_magnus(temperaturas, ponto_orvalho)), 8.0, 99.0)

    umidade_ar = umidade_do_ar(temperatura)

    # ----- Noites: mínima pelo ponto de orvalho do fim da tarde -----------------
    posicao_por_instante = {instante: posicao for posicao, instante in enumerate(instantes)}
    datas = instantes.normalize().unique()
    for data in datas:
        indices_tarde = [posicao_por_instante.get(data + pd.Timedelta(hours=h)) for h in (17, 18, 19)]
        if any(indice is None for indice in indices_tarde):
            continue
        indice_19 = int(indices_tarde[-1])
        temperatura_tarde = float(np.mean(temperatura[indices_tarde]))
        umidade_tarde = float(np.mean(umidade_ar[indices_tarde]))
        orvalho_tarde = float(ponto_orvalho_magnus(temperatura_tarde, umidade_tarde))
        depressao_tarde = max(0.0, temperatura_tarde - orvalho_tarde)
        radiativa = bool(meses_geada[indice_19]) and umidade_tarde < UMIDADE_AR_NOITE_RADIATIVA
        perda = COEF_RESFRIAMENTO_NOTURNO * depressao_tarde + float(gerador.normal(0.0, 0.35))
        hora_minima = int(gerador.integers(4, 8)) if radiativa else int(gerador.integers(4, 7))
        if radiativa:
            perda = float(np.clip(perda, 1.0, 3.0))  # geada radiativa: Td menos 1 a 3 °C
        temperatura_minima = orvalho_tarde - perda
        indice_minimo = posicao_por_instante.get(data + pd.Timedelta(hours=24 + hora_minima))
        indice_10 = posicao_por_instante.get(data + pd.Timedelta(hours=34))
        ultimo = horas - 1
        fim_resfriamento = int(indice_minimo) if indice_minimo is not None else ultimo
        temperatura_19 = float(temperatura[indice_19])
        passos_resfriamento = (24 + hora_minima) - 19
        for indice in range(indice_19 + 1, fim_resfriamento + 1):
            fracao = (indice - indice_19) / passos_resfriamento
            temperatura[indice] = temperatura_minima + (temperatura_19 - temperatura_minima) * (1.0 - fracao) ** 1.4
        if indice_minimo is None:
            continue
        alvo_manha = float(temperatura[indice_10]) if indice_10 is not None else None
        passos_aquecimento = 10 - hora_minima
        fim_aquecimento = int(indice_10) if indice_10 is not None else ultimo + 1
        for indice in range(int(indice_minimo) + 1, min(fim_aquecimento, horas)):
            if alvo_manha is None:
                break
            fracao = (indice - int(indice_minimo)) / passos_aquecimento
            temperatura[indice] = temperatura_minima + (alvo_manha - temperatura_minima) * fracao ** 1.3

    # Temperatura abaixo do limite crítico de frio só na madrugada de geada
    # radiativa (0 h a 8 h, de maio a agosto); no resto do tempo o ar não chega lá.
    janela_geada = meses_geada & (hora_do_dia <= 8.0)
    piso = faixa_temperatura.critico_min + 0.3
    temperatura = np.where(~janela_geada & (temperatura < piso), piso + 0.1 * gerador.random(horas), temperatura)
    temperatura = np.clip(temperatura, *LIMITES_FISICOS_SENSORES["temperatura_ar"])
    umidade_ar = np.clip(umidade_do_ar(temperatura) + gerador.normal(0.0, 1.0, size=horas), 8.0, 99.0)

    # ----- Panes de irrigação ------------------------------------------------------
    pane_irrigacao = np.zeros(horas, dtype=bool)
    for inicio, duracao in _sortear_eventos(gerador, horas, intervalo_medio=420.0, duracao_min=96, duracao_max=230):
        pane_irrigacao[inicio:min(horas, inicio + duracao)] = True
    sem_agua = pane_irrigacao | veranico  # no veranico o reservatório do produtor seca

    # ----- Balanço hídrico do solo ------------------------------------------
    fator_solar = np.maximum(0.0, np.sin(2.0 * np.pi * (hora_do_dia - 6.0) / 24.0))
    coeficiente_et = 0.085 * largura_solo / 20.0
    capacidade_campo = faixa_solo.ideal_max
    ponto_murcha = max(1.0, faixa_solo.critico_min - 0.75 * largura_solo)
    limiar_irrigacao = faixa_solo.ideal_min + 0.1 * largura_solo
    alvo_irrigacao = faixa_solo.ideal_max - 0.2 * largura_solo
    taxa_gotejamento = min(TAXA_MAXIMA_GOTEJAMENTO, 0.07 * largura_solo)
    umidade_solo = np.zeros(horas)
    nivel = (faixa_solo.ideal_min + faixa_solo.ideal_max) / 2.0
    irrigacao_restante = 0.0
    sorteios_reacao = gerador.random(horas)
    sorteios_excesso = gerador.random(horas)
    for indice in range(horas):
        # Coeficiente de estresse: com a terra secando a planta fecha os estômatos e perde menos água.
        estresse = min(1.0, max(0.05, (nivel - ponto_murcha) / (faixa_solo.ideal_min - ponto_murcha)))
        evapotranspiracao = (coeficiente_et * max(0.0, temperatura[indice] - 5.0) * fator_solar[indice]
                             * (1.0 - 0.5 * umidade_ar[indice] / 100.0) * estresse)
        nivel -= evapotranspiracao
        nivel += chuva[indice]
        if sem_agua[indice]:
            irrigacao_restante = 0.0
        elif irrigacao_restante <= 0.0 and nivel < limiar_irrigacao and sorteios_reacao[indice] < 0.06:
            # O produtor nem sempre percebe na hora: reage com probabilidade por hora; às vezes rega demais.
            excesso = sorteios_excesso[indice] < 0.2
            destino = faixa_solo.ideal_max + 0.5 * largura_solo if excesso else alvo_irrigacao
            irrigacao_restante = max(0.0, destino - nivel)
        if irrigacao_restante > 0.0:
            lamina = min(irrigacao_restante, taxa_gotejamento)
            nivel += lamina
            irrigacao_restante -= lamina
        if nivel > capacidade_campo:
            nivel -= 0.05 * (nivel - capacidade_campo)
        nivel = float(min(max(nivel, 0.0), 100.0))
        umidade_solo[indice] = nivel
    umidade_solo = np.clip(umidade_solo + gerador.normal(0.0, 0.2, size=horas), *LIMITES_FISICOS_SENSORES["umidade_solo"])

    return pd.DataFrame({
        "data_hora": instantes.strftime(FORMATO_DATA_HORA),
        "cultura": parametros.codigo,
        "temperatura_ar": np.round(temperatura, 1),
        "umidade_ar": np.round(umidade_ar, 1),
        "umidade_solo": np.round(umidade_solo, 1),
    })


def _subidas_de_solo(serie_solo: pd.Series, limiar: float = LIMIAR_SUBIDA_CHUVA_PP) -> pd.Series:
    """Subidas horárias da umidade do solo acima de ``limiar`` pp/h (aproximação da chuva; o gotejamento é mais lento)."""
    diferencas = pd.to_numeric(serie_solo, errors="coerce").astype(float).diff()
    return diferencas.where(diferencas > limiar, 0.0).fillna(0.0)


def _maior_estiagem_dias(instantes: pd.Series, subidas: pd.Series, meses: Sequence[int]) -> Tuple[float, Optional[pd.Timestamp]]:
    """Maior sequência (em dias) sem subida de solo de chuva, contada só nos meses pedidos, e seu início."""
    momentos = pd.to_datetime(instantes).reset_index(drop=True)
    chuvoso = (subidas.reset_index(drop=True) > 0.0).to_numpy()
    no_periodo = momentos.dt.month.isin(list(meses)).to_numpy()
    melhor, inicio_melhor = 0.0, None
    corrente_inicio: Optional[pd.Timestamp] = None
    for posicao in range(len(momentos)):
        if not no_periodo[posicao] or chuvoso[posicao]:
            corrente_inicio = None
            continue
        if corrente_inicio is None:
            corrente_inicio = momentos.iloc[posicao]
        duracao = (momentos.iloc[posicao] - corrente_inicio).total_seconds() / 86400.0 + 1.0 / 24.0
        if duracao > melhor:
            melhor, inicio_melhor = duracao, corrente_inicio
    return melhor, inicio_melhor


def gerar_relatorio_realismo(df: pd.DataFrame, parametros: ParametrosCultura) -> str:
    """Relatório de realismo do simulador para UMA cultura (texto pt-BR).

    Mostra, mês a mês: temperatura média, horas abaixo de ``critico_min``
    (geada/frio crítico), horas acima de ``critico_max`` (escaldadura), chuva
    aproximada (soma das subidas de umidade do solo acima de
    ``LIMIAR_SUBIDA_CHUVA_PP`` pp/h) e a distribuição das cores do motor de
    regras; depois, a maior estiagem de janeiro a março (veranico) e a razão
    chuva da seca / chuva das águas.
    """
    dados = df.loc[df["cultura"] == parametros.codigo] if "cultura" in df.columns else df
    dados = dados.sort_values("data_hora").reset_index(drop=True)
    faixa_temperatura = parametros.faixa(VARIAVEL_TEMPERATURA_AR)
    momentos = pd.to_datetime(dados["data_hora"])
    temperatura = dados["temperatura_ar"].astype(float)
    subidas = _subidas_de_solo(dados["umidade_solo"])
    status = np.array([calcular_status_regra(t, s, parametros)
                       for t, s in zip(temperatura, dados["umidade_solo"].astype(float))])
    linhas: List[str] = [
        f"Realismo do simulador — {parametros.nome_comum} ({parametros.codigo})",
        f"Período: {momentos.min()} a {momentos.max()} ({len(dados)} horas)",
        f"Limites de temperatura: crítico {faixa_temperatura.critico_min:g}–{faixa_temperatura.critico_max:g} °C, "
        f"ideal {faixa_temperatura.ideal_min:g}–{faixa_temperatura.ideal_max:g} °C",
        "",
        f"{'Mês':<5}{'T média °C':>11}{'h<crít.mín':>12}{'h>crít.máx':>12}{'Chuva (pp)':>12}"
        f"{'Verde %':>9}{'Amarelo %':>11}{'Vermelho %':>12}",
    ]
    for mes in range(1, 13):
        filtro = (momentos.dt.month == mes).to_numpy()
        if not filtro.any():
            continue
        status_mes = status[filtro]
        linhas.append(
            f"{mes:<5}{temperatura[filtro].mean():>11.1f}{int((temperatura[filtro] < faixa_temperatura.critico_min).sum()):>12}"
            f"{int((temperatura[filtro] > faixa_temperatura.critico_max).sum()):>12}{subidas[filtro].sum():>12.0f}"
            f"{100.0 * np.mean(status_mes == STATUS_VERDE):>9.1f}{100.0 * np.mean(status_mes == STATUS_AMARELO):>11.1f}"
            f"{100.0 * np.mean(status_mes == STATUS_VERMELHO):>12.1f}")
    abaixo = temperatura < faixa_temperatura.critico_min
    horas_abaixo = momentos[abaixo.to_numpy()]
    linhas.append("")
    linhas.append(f"Horas abaixo de critico_min: {int(abaixo.sum())} "
                  f"(meses {sorted(set(horas_abaixo.dt.month.tolist())) or '-'}; "
                  f"horas do dia {sorted(set(horas_abaixo.dt.hour.tolist())) or '-'})")
    noites_geada = sorted(set((horas_abaixo - pd.Timedelta(hours=12)).dt.date.tolist()))
    linhas.append(f"Noites com geada/frio crítico: {len(noites_geada)}")
    linhas.append(f"Horas acima de critico_max (escaldadura): {int((temperatura > faixa_temperatura.critico_max).sum())}")
    dias_veranico, inicio_veranico = _maior_estiagem_dias(dados["data_hora"], subidas, (1, 2, 3))
    linhas.append(f"Maior estiagem de jan. a mar. (veranico): {dias_veranico:.1f} dias"
                  + (f", a partir de {inicio_veranico:%d/%m/%Y}" if inicio_veranico is not None else ""))
    chuva_seca = float(subidas[momentos.dt.month.isin(MESES_SECOS).to_numpy()].sum())
    chuva_aguas = float(subidas[momentos.dt.month.isin(MESES_CHUVOSOS).to_numpy()].sum())
    razao = chuva_seca / chuva_aguas if chuva_aguas > 0 else float("nan")
    linhas.append(f"Chuva (subidas de solo > {LIMIAR_SUBIDA_CHUVA_PP:g} pp/h): seca mai.–set. = {chuva_seca:.0f} pp, "
                  f"águas out.–mar. = {chuva_aguas:.0f} pp (razão {razao:.1%})")
    linhas.append(f"Maior queda horária do solo: {(-dados['umidade_solo'].astype(float).diff()).max():.2f} pp/h")
    distribuicao = {classe: float(np.mean(status == classe)) for classe in (STATUS_VERDE, STATUS_AMARELO, STATUS_VERMELHO)}
    linhas.append("Distribuição das cores no ano: " + ", ".join(
        f"{NOMES_STATUS[c]} {100.0 * p:.1f}%" for c, p in distribuicao.items()))
    return "\n".join(linhas)


# ---------------------------------------------------------------------------
# Ruídos, detecção e saneamento
# ---------------------------------------------------------------------------
def injetar_ruidos(df: pd.DataFrame, taxa_ruido: float = TAXA_RUIDO, semente: int = SEMENTE_ALEATORIA) -> pd.DataFrame:
    """Corrompe ``taxa_ruido`` das células de CADA coluna de sensor.

    As células sorteadas são divididas igualmente entre três tipos:
        * ``nan``: leitura perdida (sensor não respondeu);
        * ``pico``: pico elétrico de ±40 a 80 unidades (umidades acima de 100%
          viram leituras impossíveis, como no higrômetro em curto);
        * ``negativo``: valor negativo impossível (falha de conversão do ADC).

    Adiciona a coluna ``tipo_ruido_injetado`` (gabarito, ex.:
    ``"umidade_solo:pico;temperatura_ar:nan;"``; vazio quando a linha está
    limpa), usada SOMENTE para avaliar o detector.
    """
    if not 0.0 <= taxa_ruido < 1.0:
        raise ValueError("taxa_ruido deve estar em [0, 1).")
    resultado = df.copy().reset_index(drop=True)
    gerador = np.random.default_rng(semente)
    total_linhas = len(resultado)
    gabarito: List[str] = [""] * total_linhas
    for coluna in COLUNAS_SENSORES:
        valores = resultado[coluna].to_numpy(dtype=float, copy=True)
        quantidade = int(round(taxa_ruido * total_linhas))
        if quantidade == 0:
            continue
        linhas = gerador.choice(total_linhas, size=quantidade, replace=False)
        tipos = np.array([TIPOS_RUIDO[posicao % len(TIPOS_RUIDO)] for posicao in range(quantidade)])
        gerador.shuffle(tipos)
        for linha, tipo in zip(linhas, tipos):
            original = valores[linha]
            if tipo == "nan":
                valores[linha] = np.nan
            elif tipo == "pico":
                magnitude = gerador.uniform(40.0, 80.0)
                sinal = 1.0 if gerador.random() < 0.6 else -1.0
                valores[linha] = round(original + sinal * magnitude, 1)
            else:
                valores[linha] = round(-gerador.uniform(0.5, 30.0), 1)
            gabarito[linha] += f"{coluna}:{tipo};"
        resultado[coluna] = valores
    resultado["tipo_ruido_injetado"] = pd.Series(gabarito, index=resultado.index, dtype="str")
    return resultado


def detectar_anomalias(df: pd.DataFrame) -> pd.DataFrame:
    """Detecta leituras impossíveis SEM olhar o gabarito ``tipo_ruido_injetado``.

    Regras por célula de sensor:
        * ``nan``: leitura ausente;
        * ``negativo``: abaixo do mínimo físico do sensor (ex.: umidade < 0);
        * ``pico``: acima do máximo físico OU desvio maior que ``LIMITES_SALTO``
          em relação à mediana móvel (janela 5, centrada, por cultura) calculada
          apenas com valores fisicamente possíveis.

    Adiciona ``flag_anomalia`` (0/1 por linha) e ``descricao_anomalia``
    (ex.: ``"umidade_solo:negativo;temperatura_ar:pico"``) e troca os valores
    anômalos por NaN.
    """
    resultado = df.copy()
    descricoes = np.full(len(resultado), "", dtype=object)
    agrupador = resultado["cultura"] if "cultura" in resultado.columns else pd.Series("unica", index=resultado.index)
    for coluna in COLUNAS_SENSORES:
        valores = pd.to_numeric(resultado[coluna], errors="coerce").astype(float)
        minimo, maximo = LIMITES_FISICOS_SENSORES[coluna]
        ausente = valores.isna()
        abaixo = valores < minimo
        acima = valores > maximo
        plausivel = valores.where(~(ausente | abaixo | acima))
        mediana = plausivel.groupby(agrupador, sort=False).transform(
            lambda serie: serie.rolling(JANELA_MEDIANA_MOVEL, center=True, min_periods=1).median()
        )
        salto = (plausivel - mediana).abs() > LIMITES_SALTO[coluna]
        salto = salto.fillna(False).astype(bool) & ~(ausente | abaixo | acima)
        rotulos = np.select(
            [ausente.to_numpy(), abaixo.to_numpy(), (acima | salto).to_numpy()],
            [f"{coluna}:nan;", f"{coluna}:negativo;", f"{coluna}:pico;"],
            default="",
        )
        descricoes = descricoes + rotulos.astype(object)
        anomalo = ausente | abaixo | acima | salto
        resultado[coluna] = valores.mask(anomalo)
    descricoes_limpas = [texto.rstrip(";") for texto in descricoes]
    resultado["flag_anomalia"] = np.array([1 if texto else 0 for texto in descricoes_limpas], dtype=np.int64)
    resultado["descricao_anomalia"] = pd.Series(descricoes_limpas, index=resultado.index, dtype="str")
    return resultado


def sanear_dataset(df_bruto: pd.DataFrame) -> pd.DataFrame:
    """Detecta anomalias, interpola linearmente por cultura e arredonda a 1 casa.

    ``detectar_anomalias`` → ``interpolate(method="linear", limit_direction="both")``
    por cultura → arredonda → remove ``tipo_ruido_injetado``.

    Raises:
        ValueError: se alguma cultura ficar sem nenhum valor válido em um sensor.
    """
    detectado = detectar_anomalias(df_bruto)
    interpolado = detectado.groupby("cultura", sort=False)[COLUNAS_SENSORES].transform(
        lambda serie: serie.interpolate(method="linear", limit_direction="both")
    )
    for coluna in COLUNAS_SENSORES:
        detectado[coluna] = interpolado[coluna].round(1)
    if detectado[COLUNAS_SENSORES].isna().any().any():
        raise ValueError("Saneamento falhou: restaram valores ausentes (sensor sem nenhuma leitura válida).")
    if "tipo_ruido_injetado" in detectado.columns:
        detectado = detectado.drop(columns=["tipo_ruido_injetado"])
    return detectado.reset_index(drop=True)


def calcular_features_dataframe(df: pd.DataFrame, parametros_por_cultura: Dict[str, ParametrosCultura]) -> pd.DataFrame:
    """Versão vetorizada (numpy.where) de ``calcular_features`` e ``calcular_status_regra``.

    Adiciona ``deficit_hidrico_solo``, ``desvio_termico`` (2 casas) e
    ``status_alerta`` (0/1/2) usando as faixas de cada linha conforme a cultura.

    Raises:
        ValueError: se houver cultura sem parâmetros informados.
    """
    resultado = df.copy()
    culturas_sem_parametro = sorted(set(resultado["cultura"].unique()) - set(parametros_por_cultura))
    if culturas_sem_parametro:
        raise ValueError(f"Faltam parâmetros para as culturas: {', '.join(map(str, culturas_sem_parametro))}.")

    def limite(variavel: str, atributo: str) -> np.ndarray:
        """Vetor com o limite ``atributo`` da ``variavel`` para cada linha."""
        mapa = {codigo: float(getattr(parametros.faixa(variavel), atributo))
                for codigo, parametros in parametros_por_cultura.items()}
        return resultado["cultura"].map(mapa).to_numpy(dtype=float)

    temperatura = resultado["temperatura_ar"].to_numpy(dtype=float)
    solo = resultado["umidade_solo"].to_numpy(dtype=float)

    solo_min, solo_max = limite(VARIAVEL_UMIDADE_SOLO, "ideal_min"), limite(VARIAVEL_UMIDADE_SOLO, "ideal_max")
    temp_min, temp_max = limite(VARIAVEL_TEMPERATURA_AR, "ideal_min"), limite(VARIAVEL_TEMPERATURA_AR, "ideal_max")
    deficit = np.where(solo < solo_min, solo_min - solo, np.where(solo > solo_max, solo_max - solo, 0.0))
    desvio = np.where(temperatura > temp_max, temperatura - temp_max,
                      np.where(temperatura < temp_min, temperatura - temp_min, 0.0))

    def classes(valores: np.ndarray, variavel: str) -> np.ndarray:
        """Classificação vetorizada idêntica a ``classificar_variavel``."""
        ideal_min, ideal_max = limite(variavel, "ideal_min"), limite(variavel, "ideal_max")
        atencao_min, atencao_max = limite(variavel, "atencao_min"), limite(variavel, "atencao_max")
        return np.where((valores >= ideal_min) & (valores <= ideal_max), STATUS_VERDE,
                        np.where((valores >= atencao_min) & (valores <= atencao_max), STATUS_AMARELO, STATUS_VERMELHO))

    resultado["deficit_hidrico_solo"] = np.round(deficit + 0.0, 2)
    resultado["desvio_termico"] = np.round(desvio + 0.0, 2)
    resultado[COLUNA_ALVO] = np.maximum(classes(temperatura, VARIAVEL_TEMPERATURA_AR),
                                        classes(solo, VARIAVEL_UMIDADE_SOLO)).astype(np.int64)
    return resultado


def _celulas_por_linha(textos: pd.Series) -> List[Set[str]]:
    """Converte textos ``"coluna:tipo;..."`` em conjuntos de colunas afetadas por linha."""
    conjuntos: List[Set[str]] = []
    for texto in textos.fillna("").astype(str):
        conjuntos.append({parte.split(":", 1)[0] for parte in texto.split(";") if parte.strip()})
    return conjuntos


def avaliar_detector(df_bruto: pd.DataFrame, df_limpo: pd.DataFrame) -> Dict[str, float]:
    """Compara o detector com o gabarito de ruídos injetados.

    Alinha as tabelas por (cultura, data_hora) e mede, linha a linha, recall,
    precisão e F1 (positivo = linha com algum ruído). Também informa o recall
    por célula e por tipo de ruído.

    Returns:
        Dicionário de métricas (todas como ``float``).
    """
    chaves = ["cultura", "data_hora"]
    juntos = df_bruto[chaves + ["tipo_ruido_injetado"]].merge(
        df_limpo[chaves + ["flag_anomalia", "descricao_anomalia"]], on=chaves, how="inner", validate="one_to_one"
    )
    gabarito = juntos["tipo_ruido_injetado"].fillna("").astype(str)
    real = gabarito.str.len() > 0
    sinalizado = juntos["flag_anomalia"].astype(int) == 1
    verdadeiros_positivos = int((real & sinalizado).sum())
    falsos_positivos = int((~real & sinalizado).sum())
    falsos_negativos = int((real & ~sinalizado).sum())
    recall = verdadeiros_positivos / (verdadeiros_positivos + falsos_negativos) if real.any() else 1.0
    precisao = verdadeiros_positivos / (verdadeiros_positivos + falsos_positivos) if sinalizado.any() else 1.0
    f1 = 2 * precisao * recall / (precisao + recall) if (precisao + recall) > 0 else 0.0

    celulas_reais = _celulas_por_linha(gabarito)
    celulas_detectadas = _celulas_por_linha(juntos["descricao_anomalia"])
    total_celulas = sum(len(conjunto) for conjunto in celulas_reais)
    celulas_acertadas = sum(len(real_c & det_c) for real_c, det_c in zip(celulas_reais, celulas_detectadas))

    metricas: Dict[str, float] = {
        "linhas_avaliadas": float(len(juntos)),
        "linhas_com_ruido": float(real.sum()),
        "linhas_sinalizadas": float(sinalizado.sum()),
        "verdadeiros_positivos": float(verdadeiros_positivos),
        "falsos_positivos": float(falsos_positivos),
        "falsos_negativos": float(falsos_negativos),
        "recall": float(recall),
        "precisao": float(precisao),
        "f1": float(f1),
        "celulas_com_ruido": float(total_celulas),
        "recall_celulas": float(celulas_acertadas / total_celulas) if total_celulas else 1.0,
        "taxa_flag_anomalia": float(sinalizado.mean()) if len(juntos) else 0.0,
    }
    for tipo in TIPOS_RUIDO:
        total_tipo = 0
        acertos_tipo = 0
        for texto, detectadas in zip(gabarito, celulas_detectadas):
            for parte in texto.split(";"):
                if parte.endswith(f":{tipo}"):
                    total_tipo += 1
                    acertos_tipo += int(parte.split(":", 1)[0] in detectadas)
        metricas[f"recall_{tipo}"] = float(acertos_tipo / total_tipo) if total_tipo else 1.0
    return metricas


# ---------------------------------------------------------------------------
# Pipeline completo
# ---------------------------------------------------------------------------
def _distribuicao_classes(serie: pd.Series) -> Dict[int, float]:
    """Proporção de cada classe 0/1/2 (classes ausentes = 0)."""
    proporcoes = serie.value_counts(normalize=True)
    return {classe: float(proporcoes.get(classe, 0.0)) for classe in (STATUS_VERDE, STATUS_AMARELO, STATUS_VERMELHO)}


def gerar_dataset_completo(
    horas: int = HORAS_SIMULACAO,
    semente: int = SEMENTE_ALEATORIA,
    caminho_bruto: Path = CAMINHO_DATASET_BRUTO,
    caminho_limpo: Path = CAMINHO_DATASET_LIMPO,
    caminho_banco: Path = CAMINHO_BANCO,
    data_inicial: str = "2026-01-01 00:00:00",
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Gera, suja, saneia e rotula o dataset de todas as culturas ativas do banco.

    Etapas: simula cada cultura → concatena → injeta ruído → salva bruto →
    saneia → features e rótulo → salva limpo → imprime relatório (linhas,
    % de anomalias, desempenho do detector e distribuição de classes).

    A série começa em ``data_inicial`` (padrão 1º de janeiro, como o
    simulador). O dataset oficial (``python src/gerador_dataset.py`` e
    ``treinamento_ia.carregar_dataset``) usa ``DATA_INICIAL_DATASET``
    (1º de novembro): assim os últimos 30 % de cada cultura — o conjunto de
    teste temporal do prognóstico — atravessam o inverno com geada, e o
    treino também vê o começo do inverno e o veranico de janeiro/fevereiro.

    Returns:
        ``(df_bruto, df_limpo)`` com as colunas exatas dos CSVs do contrato.

    Raises:
        RuntimeError: se não houver cultura ativa cadastrada.
    """
    culturas = listar_culturas(apenas_ativas=True, caminho_banco=caminho_banco)
    if not culturas:
        raise RuntimeError("Nenhuma cultura ativa no banco; cadastre ao menos uma antes de gerar o dataset.")
    parametros_por_cultura: Dict[str, ParametrosCultura] = {}
    series: List[pd.DataFrame] = []
    for posicao, cultura in enumerate(culturas):
        parametros = buscar_parametros_cultura(str(cultura["codigo"]), caminho_banco=caminho_banco)
        parametros_por_cultura[parametros.codigo] = parametros
        series.append(simular_serie_temporal(parametros, horas=horas, semente=semente + 1000 * (posicao + 1),
                                             data_inicial=data_inicial))
    df_simulado = pd.concat(series, ignore_index=True)

    df_bruto = injetar_ruidos(df_simulado, taxa_ruido=TAXA_RUIDO, semente=semente)[COLUNAS_CSV_BRUTO]
    Path(caminho_bruto).parent.mkdir(parents=True, exist_ok=True)
    df_bruto.to_csv(caminho_bruto, index=False)

    df_saneado = sanear_dataset(df_bruto)
    df_limpo = calcular_features_dataframe(df_saneado, parametros_por_cultura)[COLUNAS_CSV_LIMPO]
    Path(caminho_limpo).parent.mkdir(parents=True, exist_ok=True)
    df_limpo.to_csv(caminho_limpo, index=False)

    metricas = avaliar_detector(df_bruto, df_limpo)
    _imprimir_relatorio(df_bruto, df_limpo, metricas, Path(caminho_bruto), Path(caminho_limpo))
    return df_bruto, df_limpo


def _imprimir_relatorio(
    df_bruto: pd.DataFrame, df_limpo: pd.DataFrame, metricas: Dict[str, float], caminho_bruto: Path, caminho_limpo: Path
) -> None:
    """Imprime o relatório do pipeline de dados no terminal."""
    print("=" * 78)
    print("AgroAnálise — Geração e Saneamento do Dataset")
    print("=" * 78)
    culturas = list(df_limpo["cultura"].unique())
    print(f"Culturas simuladas: {', '.join(culturas)} ({len(culturas)} × {len(df_limpo) // max(1, len(culturas))} horas)")
    print(f"Dataset bruto : {caminho_bruto}  ({len(df_bruto)} linhas)")
    print(f"Dataset limpo : {caminho_limpo}  ({len(df_limpo)} linhas)")
    celulas_ruidosas = int(metricas["celulas_com_ruido"])
    total_celulas = len(df_bruto) * len(COLUNAS_SENSORES)
    print(f"\nRuídos injetados: {celulas_ruidosas} células de {total_celulas} "
          f"({100.0 * celulas_ruidosas / total_celulas:.2f}% por sensor) em {int(metricas['linhas_com_ruido'])} linhas")
    for coluna in COLUNAS_SENSORES:
        gabarito = df_bruto["tipo_ruido_injetado"].fillna("")
        contagens = {tipo: int(gabarito.str.contains(f"{coluna}:{tipo};", regex=False).sum()) for tipo in TIPOS_RUIDO}
        print(f"  - {coluna:<15} " + ", ".join(f"{tipo}={quantidade}" for tipo, quantidade in contagens.items()))
    print(f"\nflag_anomalia = 1 em {int(df_limpo['flag_anomalia'].sum())} linhas "
          f"({100.0 * metricas['taxa_flag_anomalia']:.2f}% do dataset)")
    print("Detector de anomalias vs. gabarito (linha a linha):")
    print(f"  recall={metricas['recall']:.4f}  precisão={metricas['precisao']:.4f}  F1={metricas['f1']:.4f}  "
          f"(VP={int(metricas['verdadeiros_positivos'])}, FP={int(metricas['falsos_positivos'])}, "
          f"FN={int(metricas['falsos_negativos'])})")
    print(f"  recall por célula={metricas['recall_celulas']:.4f}  | por tipo: "
          + ", ".join(f"{tipo}={metricas[f'recall_{tipo}']:.4f}" for tipo in TIPOS_RUIDO))
    print(f"Valores ausentes no dataset limpo: {int(df_limpo[COLUNAS_SENSORES].isna().sum().sum())}")

    print("\nDistribuição de classes (status_alerta):")
    geral = _distribuicao_classes(df_limpo[COLUNA_ALVO])
    for classe, proporcao in geral.items():
        quantidade = int((df_limpo[COLUNA_ALVO] == classe).sum())
        print(f"  {classe} {NOMES_STATUS[classe]:<26} {quantidade:>6}  ({100.0 * proporcao:5.2f}%)")
    for cultura in culturas:
        parcial = _distribuicao_classes(df_limpo.loc[df_limpo["cultura"] == cultura, COLUNA_ALVO])
        print(f"  [{cultura}] " + "  ".join(f"{NOMES_STATUS[c].split()[0]}={100.0 * p:.1f}%" for c, p in parcial.items()))
    abaixo = [NOMES_STATUS[c] for c, p in geral.items() if p < PROPORCAO_MINIMA_CLASSE]
    if abaixo:
        print(f"AVISO: classes abaixo de {PROPORCAO_MINIMA_CLASSE:.0%}: {', '.join(abaixo)}")
        registrador.warning("Distribuição desequilibrada: %s abaixo de %.0f%%.", ", ".join(abaixo), 100 * PROPORCAO_MINIMA_CLASSE)
    else:
        print(f"OK: todas as classes com pelo menos {PROPORCAO_MINIMA_CLASSE:.0%} do dataset.")
    if metricas["recall"] < 0.9:
        registrador.warning("Recall do detector abaixo de 0,90 (%.3f).", metricas["recall"])
    print("=" * 78)


def gravar_relatorio_realismo(
    df_limpo: pd.DataFrame, caminho: Path = CAMINHO_RELATORIO_REALISMO, caminho_banco: Path = CAMINHO_BANCO
) -> Path:
    """Grava ``gerar_relatorio_realismo`` de cada cultura do dataset (saneado) em ``caminho``."""
    partes: List[str] = ["=" * 78, "AgroAnálise — Realismo do simulador climático (dataset saneado)", "=" * 78]
    for cultura in sorted(df_limpo["cultura"].astype(str).unique()):
        parametros = buscar_parametros_cultura(cultura, caminho_banco=caminho_banco)
        partes += [gerar_relatorio_realismo(df_limpo, parametros), "-" * 78]
    destino = Path(caminho)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text("\n".join(partes) + "\n", encoding="utf-8")
    return destino


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    _, df_limpo_gerado = gerar_dataset_completo(data_inicial=DATA_INICIAL_DATASET)
    caminho_realismo = gravar_relatorio_realismo(df_limpo_gerado)
    print(Path(caminho_realismo).read_text(encoding="utf-8"))
    print(f"Relatório de realismo gravado em {caminho_realismo}")
