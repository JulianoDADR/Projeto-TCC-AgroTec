"""Ações de orientação em pictogramas para o agricultor que não lê.

A mensagem falada (``gerador_dataset.gerar_mensagem_orientacao``) diz O QUE
FAZER em frases curtas. Este módulo traduz as MESMAS condições em códigos de
ação (``REGAR_AGORA``, ``COBRIR_MUDAS``...), que a página desenha como
pictogramas grandes logo abaixo do título do semáforo. Assim, quem não lê vê
a cor, o motivo e a ação na primeira tela, sem rolar.

Regras de coerência (garantidas por um teste de propriedade):
    * As condições reproduzem as de ``gerar_mensagem_orientacao`` (causa pelo
      sinal das features; intensidade pela classe de cada variável), usando
      ``calcular_features`` e ``classificar_variavel`` de ``gerador_dataset``
      sem alterá-lo.
    * Toda ação devolvida tem pelo menos uma das suas ``PALAVRAS_CHAVE_ACAO``
      na mensagem falada gerada para a mesma leitura.
    * No máximo ``MAXIMO_ACOES`` ações, sem repetição, na ordem de
      importância (primeiro a água, depois a temperatura, depois a
      antecipação).

Causa visível (v3): ``derivar_causa`` resume o MOTIVO do alerta num código de
``CAUSAS_VALIDAS`` (terra seca, encharcado, frio/geada, calor, aparelho ou
nenhuma), que a página desenha ao lado do título e fala ao toque com uma dica
sensorial (``DESCRICOES_CAUSA``: "Aperte a terra na mão..."). A anotação por
voz em cada elemento segue o estudo de interfaces para quem não lê de Medhi et
al. (ACM TOCHI 2011), em que a interface gráfica com áudio teve a maior taxa de
conclusão: https://www.microsoft.com/en-us/research/wp-content/uploads/2016/02/ToCHI2711_Medhi.pdf

Regra fisiológica (Frente 1): quando a leitura traz ``motivo_regra`` (ex.:
``GEADA_AGORA``, ``DIA_FRIO``, ``CALOR_FLORADA``, ``SECA_PROGRAMADA``), a causa
e as ações saem desse motivo (``CAUSA_POR_MOTIVO_REGRA`` e
``ACOES_POR_MOTIVO_REGRA``); com o motivo vazio o comportamento é idêntico ao
da v2.

Voz curta (v4): ``montar_fala_curta`` monta a fala que diz PRIMEIRO o que
fazer ("Sinal vermelho. Regue agora. A terra está seca."), em até 15
palavras e sem números, a partir das MESMAS ações dos pictogramas. E
``acoes_cobrem_mensagem`` garante o sentido inverso da coerência: toda ação
que a voz pede tem a sua figura (voz → figura), e ``derivar_acoes``, quando
recebe a mensagem falada, põe as figuras na ordem em que a voz as pede.

Uso típico (em ``app.montar_status``)::

    acoes = derivar_acoes(status, temperatura, umidade_solo, parametros,
                          motivo_antecipacao="SECA_CHEGANDO", estado_sensor="OK")
    causa = derivar_causa(status, leitura, parametros, "SECA_CHEGANDO", "OK")
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from database import (
    STATUS_VERDE,
    STATUS_VERMELHO,
    VARIAVEL_TEMPERATURA_AR,
    VARIAVEL_UMIDADE_SOLO,
    FaixaParametro,
    ParametrosCultura,
)
from gerador_dataset import calcular_features, classificar_variavel

# ---------------------------------------------------------------------------
# Catálogo de ações
# ---------------------------------------------------------------------------
ACOES_VALIDAS: Tuple[str, ...] = (
    "REGAR_AGORA",
    "REGAR_FIM_DA_TARDE",
    "REGAR_MANHA",
    "NAO_REGAR",
    "ABRIR_VALETAS",
    "FAZER_SOMBRA",
    "COBRIR_MUDAS",
    "CHEGAR_TERRA",
    "NADA_A_FAZER",
    "VERIFICAR_APARELHO",
)

# Trechos da mensagem falada que comprovam cada ação (ao menos um deve aparecer).
# v4: as formas em minúscula/maiúscula que a fala curta usa ao juntar ações ("Cubra as
# mudas e chegue terra", "Chegue terra no pé"), e "quando o sol sair" (rega de dia
# depois da geada, ``gerador_dataset.FRASE_REGA_NA_GEADA``) para a rega da manhã.
PALAVRAS_CHAVE_ACAO: Dict[str, Tuple[str, ...]] = {
    "REGAR_AGORA": ("Regue agora", "regue agora"),
    "REGAR_FIM_DA_TARDE": ("fim da tarde",),
    "REGAR_MANHA": ("de manhã", "quando o sol sair"),
    "NAO_REGAR": ("Não regue", "Pare de regar", "não regue", "pare de regar"),
    "ABRIR_VALETAS": ("valetas",),
    "FAZER_SOMBRA": ("sombra",),
    "COBRIR_MUDAS": ("Cubra as mudas", "cubra as mudas", "Proteja as mudas"),
    "CHEGAR_TERRA": ("chegue terra", "Chegue terra"),
    "NADA_A_FAZER": ("Não precisa fazer nada",),
    "VERIFICAR_APARELHO": ("aparelho", "sensor", "cabo", "computador", "relógio"),
}

# Legenda de UMA palavra e descrição falada/aria-label de cada pictograma.
LEGENDAS_ACAO: Dict[str, str] = {
    "REGAR_AGORA": "Regar",
    "REGAR_FIM_DA_TARDE": "Tardinha",
    "REGAR_MANHA": "Manhã",
    "NAO_REGAR": "Parar",
    "ABRIR_VALETAS": "Valetas",
    "FAZER_SOMBRA": "Sombra",
    "COBRIR_MUDAS": "Cobrir",
    "CHEGAR_TERRA": "Terra",
    "NADA_A_FAZER": "Nada",
    "VERIFICAR_APARELHO": "Aparelho",
}
DESCRICOES_ACAO: Dict[str, str] = {
    "REGAR_AGORA": "Regue agora",
    "REGAR_FIM_DA_TARDE": "Regue no fim da tarde",
    "REGAR_MANHA": "Regue de manhã",
    "NAO_REGAR": "Não regue",
    "ABRIR_VALETAS": "Abra valetas para a água escorrer",
    "FAZER_SOMBRA": "Faça sombra nas mudas",
    "COBRIR_MUDAS": "Cubra as mudas",
    "CHEGAR_TERRA": "Chegue terra no pé das plantas",
    "NADA_A_FAZER": "Não precisa fazer nada",
    "VERIFICAR_APARELHO": "Confira o aparelho",
}

MAXIMO_ACOES: int = 3
ACOES_DE_REGA: Tuple[str, ...] = ("REGAR_AGORA", "REGAR_FIM_DA_TARDE", "REGAR_MANHA")

# ---------------------------------------------------------------------------
# Fala curta (v4): a voz diz primeiro o que fazer
# ---------------------------------------------------------------------------
# Quem não lê decide pelo que ouve primeiro: a ordem vem logo depois da cor, em
# frases curtas e sem números (diretrizes de conteúdo para baixo letramento, ver
# docs/PESQUISA_REFERENCIAS.md, U1 e U9; a IDinsight limitou os avisos de voz a agricultores a
# mensagens curtas e testou a ordem do conteúdo: https://medium.com/idinsight-blog/ivr-and-text-message-interventions-to-provide-fertilizer-information-to-farmers-16e651402be6
# — lido só o resumo). As frases usam as MESMAS palavras-chave de
# ``PALAVRAS_CHAVE_ACAO``, então ``acoes_coerentes_com_mensagem`` vale para a fala curta.
SITUACAO_NORMAL: str = "normal"
SITUACAO_TESTE: str = "teste"
SITUACAO_ANTIGA: str = "antiga"
SITUACAO_SEM_LEITURA: str = "sem_leitura"
SITUACAO_DEMONSTRACAO: str = "demonstracao"
SITUACOES_FALA: Tuple[str, ...] = (SITUACAO_NORMAL, SITUACAO_TESTE, SITUACAO_ANTIGA, SITUACAO_SEM_LEITURA,
                                   SITUACAO_DEMONSTRACAO)
MAXIMO_PALAVRAS_FALA_CURTA: int = 15
CORES_FALADAS: Dict[int, str] = {0: "verde", 1: "amarelo", 2: "vermelho"}
# Imperativo curto de cada ação (a 1ª letra vira minúscula quando a ação não abre a frase).
FRASES_ACAO_CURTAS: Dict[str, str] = {
    "REGAR_AGORA": "Regue agora",
    "REGAR_FIM_DA_TARDE": "Regue no fim da tarde",
    "REGAR_MANHA": "Regue de manhã",
    "NAO_REGAR": "Não regue",
    "ABRIR_VALETAS": "Abra valetas",
    "FAZER_SOMBRA": "Faça sombra",
    "COBRIR_MUDAS": "Cubra as mudas",
    "CHEGAR_TERRA": "Chegue terra no pé",
    "NADA_A_FAZER": "Não precisa fazer nada",
    "VERIFICAR_APARELHO": "Confira o aparelho",
}
# Causa dita depois da ordem, sem números (cai primeiro se a fala passar de 15 palavras).
FRASES_CAUSA_CURTAS: Dict[str, str] = {
    "TERRA_SECA": "A terra está seca",
    "ENCHARCADO": "Tem água demais na terra",
    "FRIO_GEADA": "Faz frio",
    "CALOR": "Faz calor",
    "APARELHO": "O problema é no aparelho",
    "NENHUMA": "A lavoura está bem",
}
# Amarelo/Vermelho sem pictograma de ação (ex.: a IA sobe para Amarelo com a terra ainda no ideal, perto da
# borda: a voz longa diz "Olhe a terra hoje e prepare a rega"): a fala curta repete esse "olhe" da própria
# mensagem, para a ação continuar na 2ª frase. Não é recomendação nova (integração v4).
FRASES_OLHAR_CURTAS: Dict[str, str] = {
    "TERRA_SECA": "Olhe a terra hoje",
    "ENCHARCADO": "Olhe a terra hoje",
    "FRIO_GEADA": "Olhe as folhas hoje",
    "CALOR": "Olhe as folhas hoje",
    "APARELHO": "Confira o aparelho",
    "NENHUMA": "Olhe a lavoura hoje",
}
# Falas fixas quando a cor NÃO deve ser seguida: começam por "Atenção." e nunca dizem "Sinal".
FALAS_SITUACAO: Dict[str, str] = {
    # Integração v4: na simulação o pictograma é "Confira o aparelho"; a voz diz o mesmo (voz × figura).
    SITUACAO_TESTE: "Atenção. Dados de teste. Não siga esta cor. Confira o aparelho.",
    SITUACAO_ANTIGA: "Atenção. Medição antiga. Confira o aparelho.",
    SITUACAO_SEM_LEITURA: "Atenção. Sem medição. Confira o aparelho.",
    SITUACAO_DEMONSTRACAO: "Atenção. Demonstração. Não é a sua lavoura.",
}
# Sem medição confiável (sensor com defeito, sem leitura, leitura antiga, simulação): só conferir o aparelho.
ACOES_VERIFICAR_APARELHO: Tuple[str, ...] = ("VERIFICAR_APARELHO",)
ESTADO_SENSOR_OK: str = "OK"
# Estados que a interface usa quando não há leitura confiável para orientar.
ESTADO_SEM_LEITURA: str = "SEM_LEITURA"
ESTADO_LEITURA_ANTIGA: str = "LEITURA_ANTIGA"
ESTADO_SIMULACAO: str = "SIMULACAO"

# Ações extras sugeridas pela antecipação (prognóstico de 12 h), uma figura para cada ordem que
# ``prognostico.gerar_frase_antecipacao`` fala. v4: o calor antecipado pede "Faça sombra nas
# mudas novas e regue no fim da tarde" (sombra primeiro, como em CALOR_FLORADA), e os
# motivos SECA_AGORA ("Regue agora"), ENCHARCAMENTO ("Não regue hoje e abra valetas") e
# FRIO ("Proteja as mudas novas") ganham as figuras das ordens que já falavam.
ACOES_POR_MOTIVO: Dict[str, Tuple[str, ...]] = {
    "SECA_CHEGANDO": ("REGAR_FIM_DA_TARDE",),
    "SECA_AGORA": ("REGAR_AGORA",),
    "ENCHARCAMENTO": ("NAO_REGAR", "ABRIR_VALETAS"),
    "CALOR_TARDE": ("FAZER_SOMBRA", "REGAR_FIM_DA_TARDE"),
    "GEADA_NOITE": ("COBRIR_MUDAS", "CHEGAR_TERRA"),
    "FRIO": ("COBRIR_MUDAS",),
}

# ---------------------------------------------------------------------------
# Regra fisiológica (Frente 1): ações e causa pelo motivo da regra
# ---------------------------------------------------------------------------
# Ações de cada ``motivo_regra`` gravado pela regra fisiológica. As palavras-chave
# das frases da Frente 1 garantem a coerência: GEADA_AGORA → "Cubra as mudas" e
# "chegue terra"; CALOR_FLORADA → "fim da tarde" e "sombra"; CALOR_SEGUIDO → "fim
# da tarde"; DIA_FRIO, DIA_QUENTE e CALOR_DIA → "Não precisa fazer nada";
# SECA_PROGRAMADA → "Não regue". Motivos ausentes deste mapa (SECA_AGORA,
# ENCHARCAMENTO) seguem as ações pela leitura, como na v2.
# Geada com abrigo < 2 °C (Epamig/Embrapa Café):
# https://revistacultivar.com.br/noticias/pesquisadores-da-epamig-e-embrapa-cafe-orientam-cafeicultores-sobre-o-que-fazer-apos-as-geadas
# Máxima ≥ 34 °C na florada aborta flores (Embrapa, CBAGRO 2003):
# https://www.alice.cnptia.embrapa.br/alice/bitstream/doc/8420/1/PC-Temperaturas-elevadas-CBAGRO-2003.pdf
# Seca programada de 24/06 a 04/09 (Embrapa Cerrados):
# https://sbicafe.ufv.br/items/46703cd6-1804-4062-841a-3af5714ec5ac
ACOES_POR_MOTIVO_REGRA: Dict[str, Tuple[str, ...]] = {
    "GEADA_AGORA": ("COBRIR_MUDAS", "CHEGAR_TERRA"),
    "CALOR_FLORADA": ("REGAR_FIM_DA_TARDE", "FAZER_SOMBRA"),
    "CALOR_SEGUIDO": ("REGAR_FIM_DA_TARDE",),
    "DIA_FRIO": ("NADA_A_FAZER",),
    "DIA_QUENTE": ("NADA_A_FAZER",),
    "CALOR_DIA": ("NADA_A_FAZER",),
    "SECA_PROGRAMADA": ("NAO_REGAR",),
}
# Frase da seca programada em ``gerador_dataset.gerar_mensagem_fisiologica`` (Frente 1).
FRASE_SECA_PROGRAMADA: str = "Não regue ainda"
# Motivos "informativos": o frio ou o calor do dia sem dano não manda fazer nada urgente.
MOTIVOS_REGRA_INFORMATIVOS: Tuple[str, ...] = ("DIA_FRIO", "DIA_QUENTE", "CALOR_DIA")

# ---------------------------------------------------------------------------
# Catálogo de causas (pictograma ao lado do título)
# ---------------------------------------------------------------------------
CAUSAS_VALIDAS: Tuple[str, ...] = ("TERRA_SECA", "ENCHARCADO", "FRIO_GEADA", "CALOR", "APARELHO", "NENHUMA")
CAUSA_NENHUMA: str = "NENHUMA"
CAUSA_APARELHO: str = "APARELHO"
# Legenda de UMA palavra de cada causa.
LEGENDAS_CAUSA: Dict[str, str] = {
    "TERRA_SECA": "Seca",
    "ENCHARCADO": "Encharcado",
    "FRIO_GEADA": "Frio",
    "CALOR": "Calor",
    "APARELHO": "Aparelho",
    "NENHUMA": "Normal",
}
# Frases curtas e FIXAS, faladas ao tocar no pictograma da causa, com a dica sensorial
# (o agricultor confere com a mão e com os olhos, sem depender do aparelho).
DESCRICOES_CAUSA: Dict[str, str] = {
    "TERRA_SECA": "A terra está seca. Aperte a terra na mão: se esfarelar, está seca.",
    "ENCHARCADO": "A terra está encharcada. Aperte a terra na mão: se escorrer água, está encharcada.",
    "FRIO_GEADA": "O motivo é o frio. De manhã cedo, olhe as folhas de cima: a geada deixa as folhas escuras, "
                  "como queimadas.",
    "CALOR": "O motivo é o calor. No meio do dia, olhe as folhas: se estiverem murchas ou queimadas, é o calor.",
    "APARELHO": "O motivo é o aparelho, não a lavoura. Confira o cabo e o espeto na terra.",
    "NENHUMA": "A lavoura está bem. Não precisa fazer nada agora.",
}
# Trechos da mensagem falada que comprovam cada causa (ao menos um deve aparecer).
PALAVRAS_CHAVE_CAUSA: Dict[str, Tuple[str, ...]] = {
    "TERRA_SECA": ("seca", "secando", "secar", "Não regue"),
    "ENCHARCADO": ("encharcad", "molhada demais", "água demais"),
    "FRIO_GEADA": ("frio", "gela", "gear", "geada"),
    "CALOR": ("quente", "calor", "esquent"),
    "APARELHO": ("aparelho", "sensor", "cabo", "computador", "relógio"),
    "NENHUMA": ("Tudo bem", "Não precisa fazer nada"),
}
# Causa de cada motivo da regra fisiológica (Frente 1) e de cada motivo da antecipação.
CAUSA_POR_MOTIVO_REGRA: Dict[str, str] = {
    "GEADA_AGORA": "FRIO_GEADA",
    "DIA_FRIO": "FRIO_GEADA",
    "CALOR_FLORADA": "CALOR",
    "CALOR_SEGUIDO": "CALOR",
    "CALOR_DIA": "CALOR",
    "DIA_QUENTE": "CALOR",
    "SECA_AGORA": "TERRA_SECA",
    "SECA_PROGRAMADA": "TERRA_SECA",
    "ENCHARCAMENTO": "ENCHARCADO",
}
CAUSA_POR_MOTIVO_ANTECIPACAO: Dict[str, str] = {
    "SECA_CHEGANDO": "TERRA_SECA",
    "SECA_AGORA": "TERRA_SECA",
    "ENCHARCAMENTO": "ENCHARCADO",
    "CALOR_TARDE": "CALOR",
    "GEADA_NOITE": "FRIO_GEADA",
    "FRIO": "FRIO_GEADA",
}


def _numero_valido(valor: Optional[float]) -> bool:
    """``True`` se o valor for um número finito."""
    if valor is None:
        return False
    try:
        return math.isfinite(float(valor))
    except (TypeError, ValueError):
        return False


def _borda_mais_proxima(
    temperatura_ar: float, umidade_solo: float, faixa_temperatura: FaixaParametro, faixa_solo: FaixaParametro
) -> str:
    """Mesma escolha de ``gerador_dataset._frase_borda_proxima`` (calor, frio, seca ou excesso)."""
    largura_temperatura = max(1e-6, faixa_temperatura.ideal_max - faixa_temperatura.ideal_min)
    largura_solo = max(1e-6, faixa_solo.ideal_max - faixa_solo.ideal_min)
    margens: Dict[str, float] = {
        "calor": (faixa_temperatura.ideal_max - temperatura_ar) / largura_temperatura,
        "frio": (temperatura_ar - faixa_temperatura.ideal_min) / largura_temperatura,
        "seca": (umidade_solo - faixa_solo.ideal_min) / largura_solo,
        "excesso": (faixa_solo.ideal_max - umidade_solo) / largura_solo,
    }
    return min(margens, key=lambda chave: margens[chave])


def _acoes_da_leitura(
    status_alerta: int, temperatura_ar: float, umidade_solo: float, parametros: ParametrosCultura,
    usar_borda: bool = True,
) -> List[str]:
    """Ações pela leitura atual, nas mesmas condições de ``gerar_mensagem_orientacao``.

    ``usar_borda=False`` dispensa a ação da "borda mais próxima" quando nada
    está fora da faixa ideal: com a antecipação em jogo, o alerta vem do
    prognóstico (e a mensagem falada também), não da borda.
    """
    faixa_temperatura = parametros.faixa(VARIAVEL_TEMPERATURA_AR)
    faixa_solo = parametros.faixa(VARIAVEL_UMIDADE_SOLO)
    deficit, desvio = calcular_features(temperatura_ar, umidade_solo, parametros)
    falta_agua = deficit > 0
    excesso_agua = deficit < 0
    calor = desvio > 0
    frio = desvio < 0
    solo_vermelho = classificar_variavel(umidade_solo, faixa_solo) == STATUS_VERMELHO
    temperatura_vermelha = classificar_variavel(temperatura_ar, faixa_temperatura) == STATUS_VERMELHO
    urgente = (umidade_solo < faixa_solo.critico_min or umidade_solo > faixa_solo.critico_max
               or temperatura_ar < faixa_temperatura.critico_min or temperatura_ar > faixa_temperatura.critico_max)

    if status_alerta == STATUS_VERDE and not urgente:
        return ["NADA_A_FAZER"]

    acoes: List[str] = []
    # Geada acontecendo (abaixo do crítico): a voz manda proteger primeiro e regar "de dia,
    # quando o sol sair" (v4, ``gerador_dataset._juntar_acoes``), nunca "Regue agora".
    geada_agora = frio and temperatura_ar < faixa_temperatura.critico_min
    # Água (o horário da rega muda com frio ou calor).
    if falta_agua and frio:
        acoes.append("REGAR_AGORA" if solo_vermelho and not geada_agora else "REGAR_MANHA")
    elif falta_agua and calor:
        acoes.append("REGAR_FIM_DA_TARDE")
    elif falta_agua:
        acoes.append("REGAR_AGORA" if solo_vermelho else "REGAR_FIM_DA_TARDE")
    elif excesso_agua:
        acoes.append("NAO_REGAR")
        if solo_vermelho:
            acoes.append("ABRIR_VALETAS")

    # Temperatura.
    causa_agua = falta_agua or excesso_agua
    if calor and temperatura_vermelha:
        acoes.append("FAZER_SOMBRA")
        if not causa_agua:
            acoes.append("REGAR_FIM_DA_TARDE")
    elif calor and not causa_agua:
        acoes.append("REGAR_FIM_DA_TARDE")
    elif frio and (temperatura_ar < faixa_temperatura.critico_min or temperatura_vermelha):
        acoes.extend(("COBRIR_MUDAS", "CHEGAR_TERRA"))
    elif frio:
        acoes.append("COBRIR_MUDAS")
    if geada_agora and falta_agua:
        acoes = [acao for acao in acoes if acao not in ACOES_DE_REGA] + [acao for acao in acoes if acao in ACOES_DE_REGA]

    if usar_borda and not (causa_agua or calor or frio):
        # A IA elevou o status sem causa fora da faixa ideal: a mensagem aponta a
        # borda mais próxima. "Olhe a terra e prepare a rega" (seca) não tem
        # pictograma de ação imediata, então nada é acrescentado nesse caso.
        borda = _borda_mais_proxima(temperatura_ar, umidade_solo, faixa_temperatura, faixa_solo)
        if borda == "calor":
            acoes.append("REGAR_FIM_DA_TARDE")
        elif borda == "frio":
            acoes.append("COBRIR_MUDAS")
        elif borda == "excesso":
            acoes.append("NAO_REGAR")
    return acoes



def _rega_conforme_mensagem(acao: str, mensagem_voz: str) -> str:
    """Troca uma ação de rega pela ação de rega cujo momento aparece na mensagem falada.

    Args:
        acao: código de ação sugerido pela antecipação.
        mensagem_voz: frase que será falada ao agricultor.

    Returns:
        A ação de rega coerente com a mensagem; a própria ``acao`` se ela não for
        de rega ou se a mensagem não citar nenhum momento de rega.
    """
    if acao not in ACOES_DE_REGA:
        return acao
    for candidata in ACOES_DE_REGA:
        if any(chave in mensagem_voz for chave in PALAVRAS_CHAVE_ACAO.get(candidata, ())):
            return candidata
    return acao


def _normalizar_motivo(motivo: Any) -> str:
    """Motivo em maiúsculas, sem espaços; ``""`` para ausente, ``None`` ou ``NENHUM``."""
    texto = str(motivo or "").strip().upper()
    return "" if texto in ("", "NENHUM", "NONE") else texto


def _acoes_do_motivo_regra(
    motivo: str, temperatura_ar: float, umidade_solo: float, parametros: ParametrosCultura, mensagem_voz: str
) -> List[str]:
    """Ações pelo motivo da regra fisiológica (``ACOES_POR_MOTIVO_REGRA``).

    Se a terra estiver fora da faixa ideal e a mensagem falada também pedir uma
    ação de água ("Regue...", "Não regue", "abra valetas"), essa ação acompanha a
    voz: nos motivos informativos (``DIA_FRIO``, ``DIA_QUENTE``, ``CALOR_DIA``) ela
    substitui "nada a fazer"; nos de proteção (geada, calor fisiológico) ela vem
    PRIMEIRO, como na v2 (a frase também começa pela água). A figura acompanha o
    que a voz diz.
    """
    acoes = list(ACOES_POR_MOTIVO_REGRA[motivo])
    if not mensagem_voz:
        return acoes
    faixa_solo = parametros.faixa(VARIAVEL_UMIDADE_SOLO)
    if classificar_variavel(umidade_solo, faixa_solo) == STATUS_VERDE:
        return acoes
    agua = [acao for acao in _acoes_da_leitura(1, temperatura_ar, umidade_solo, parametros, usar_borda=False)
            if acao in ACOES_DE_REGA + ("NAO_REGAR", "ABRIR_VALETAS")]
    falada = [_rega_conforme_mensagem(acao, mensagem_voz) for acao in agua]
    falada = [acao for acao in falada if any(chave in mensagem_voz for chave in PALAVRAS_CHAVE_ACAO.get(acao, ()))]
    if FRASE_SECA_PROGRAMADA in mensagem_voz:
        # Seca programada empatada com o calor do dia (o motivo principal é CALOR_DIA):
        # a voz diz "Não regue ainda", e a figura também.
        falada = ["NAO_REGAR"]
    if not falada:
        return acoes
    if motivo in MOTIVOS_REGRA_INFORMATIVOS:
        return falada
    return falada + [acao for acao in acoes if acao not in falada]


def derivar_acoes(
    status_alerta: int,
    temperatura_ar: Optional[float],
    umidade_solo: Optional[float],
    parametros: ParametrosCultura,
    motivo_antecipacao: str = "NENHUM",
    estado_sensor: str = ESTADO_SENSOR_OK,
    mensagem_voz: str = "",
    motivo_regra: str = "",
) -> List[str]:
    """Lista de 0 a 3 ações (códigos de ``ACOES_VALIDAS``) para os pictogramas.

    Args:
        status_alerta: status final da leitura (0 Verde, 1 Amarelo, 2 Vermelho).
        temperatura_ar: temperatura saneada (°C); ``None`` = sem leitura.
        umidade_solo: umidade do solo saneada (%); ``None`` = sem leitura.
        parametros: faixas da cultura da leitura.
        motivo_antecipacao: motivo principal do prognóstico (``prognostico.MOTIVOS``);
            ``SECA_CHEGANDO`` acrescenta a rega no fim da tarde e ``GEADA_NOITE``
            acrescenta cobrir as mudas e chegar terra.
        estado_sensor: ``OK`` ou qualquer outro valor (defeito do sensor,
            ``SEM_LEITURA``, ``LEITURA_ANTIGA``, ``SIMULACAO``), que vira
            ``["VERIFICAR_APARELHO"]``: sem medição confiável não há ação de campo.
        mensagem_voz: frase falada da leitura (opcional). Quando informada, o
            pictograma de rega acrescentado pela antecipação segue o MOMENTO que a
            voz realmente pediu ("Regue agora", "de manhã" ou "fim da tarde"),
            para a figura nunca contradizer o que o agricultor ouve.
        motivo_regra: motivo gravado pela regra fisiológica (Frente 1). Vazio
            (padrão) = comportamento da v2, idêntico. Com um motivo de
            ``ACOES_POR_MOTIVO_REGRA``, as ações saem do mapa; nos motivos
            informativos (dia frio/quente), uma ação de água que a voz também
            pede ("Regue...", "Não regue") é mantida no lugar de "nada a fazer".

    Quando ``mensagem_voz`` pede ações (``acoes_faladas``), a lista é a dessas
    ordens, na ordem da voz (v4): toda ordem falada tem a sua figura.

    Returns:
        Até ``MAXIMO_ACOES`` ações, sem repetição. ``NADA_A_FAZER`` só aparece
        sozinho. Pode ser vazia no único caso em que a mensagem não pede ação
        concreta (alerta da IA perto da borda de seca: "olhe a terra").
    """
    estado = str(estado_sensor or ESTADO_SENSOR_OK).strip().upper()
    if estado != ESTADO_SENSOR_OK or not (_numero_valido(temperatura_ar) and _numero_valido(umidade_solo)):
        return list(ACOES_VERIFICAR_APARELHO)
    try:
        nivel = int(status_alerta)
    except (TypeError, ValueError):
        nivel = STATUS_VERMELHO
    if nivel not in (0, 1, 2):
        nivel = STATUS_VERMELHO

    extras = ACOES_POR_MOTIVO.get(str(motivo_antecipacao or "NENHUM").strip().upper(), ())
    if mensagem_voz:
        extras = tuple(_rega_conforme_mensagem(acao, mensagem_voz) for acao in extras)
    motivo = _normalizar_motivo(motivo_regra)
    acoes = _acoes_da_leitura(nivel, float(temperatura_ar), float(umidade_solo), parametros,  # type: ignore[arg-type]
                              usar_borda=not extras)
    if motivo in ACOES_POR_MOTIVO_REGRA:
        mapeadas = _acoes_do_motivo_regra(motivo, float(temperatura_ar), float(umidade_solo),  # type: ignore[arg-type]
                                          parametros, mensagem_voz)
        # Integração v3: o ``motivo_regra`` também é gravado na regra 'instantanea'
        # (banca/DEMONSTRACAO, DATASET, ou o técnico voltou ao modo v2), cuja voz é a
        # da v2 ("Faça sombra", "Proteja as mudas"). Nesses registros as figuras da v2
        # é que acompanham a voz; o mapa só vale quando a frase o confirma e a v2 não
        # cobre mais do que ele. Assim nunca aparece "nada a fazer" num Vermelho de calor.
        legado_coerente = bool(mensagem_voz) and acoes_coerentes_com_mensagem(acoes, mensagem_voz)
        if not (legado_coerente and (not acoes_coerentes_com_mensagem(mapeadas, mensagem_voz)
                                     or set(mapeadas) < set(acoes))):
            acoes = mapeadas
    elif str(motivo_regra or "").strip() and mensagem_voz:
        # Motivo do solo (SECA_AGORA, ENCHARCAMENTO) ou NENHUM gravado pela v3: na regra
        # fisiológica a voz não pede proteção térmica (o frio/calor do dia é informativo;
        # a noite fria com média ideal é Verde), mas a classe instantânea da temperatura
        # pediria "cubra as mudas". Fica só o que a voz diz (na voz da v2, tudo coincide).
        faladas = [acao for acao in (_rega_conforme_mensagem(acao, mensagem_voz) for acao in acoes)
                   if acoes_coerentes_com_mensagem([acao], mensagem_voz)]
        if not faladas and acoes_coerentes_com_mensagem(["NADA_A_FAZER"], mensagem_voz):
            faladas = ["NADA_A_FAZER"]
        acoes = faladas
    if extras:
        acoes = [acao for acao in acoes if acao != "NADA_A_FAZER"] + list(extras)
    if mensagem_voz:
        # v4 (voz → figura): quando a voz pede ações, as figuras são exatamente essas
        # ordens, na ordem em que são faladas (até ``MAXIMO_ACOES``). Assim nenhuma ordem
        # fica sem pictograma e nenhum pictograma mostra o que a voz não pediu.
        faladas = acoes_faladas(mensagem_voz)
        if faladas:
            acoes = faladas

    resultado: List[str] = []
    for acao in acoes:
        if acao in ACOES_VALIDAS and acao not in resultado:
            resultado.append(acao)
        if len(resultado) == MAXIMO_ACOES:
            break
    return resultado


def acoes_coerentes_com_mensagem(acoes: List[str], mensagem: str) -> bool:
    """``True`` se cada ação tiver ao menos uma das suas palavras-chave na mensagem falada."""
    return all(any(chave in mensagem for chave in PALAVRAS_CHAVE_ACAO.get(acao, ())) for acao in acoes)


# Frase de ORDEM: tem um verbo no imperativo. Só nelas uma palavra-chave conta como ação
# pedida ("Regue hoje de manhã" é ordem; "pode ter geada amanhã de manhã" é só o prazo).
# O mesmo verbo no subjuntivo, depois de "que", não é ordem: "É quase certo que faça muito
# calor amanhã de manhã" é a previsão (o "de manhã" é o prazo), não "regue de manhã".
_PADRAO_ORDEM: "re.Pattern[str]" = re.compile(
    r"\b(?<![Qq]ue )(?:[Rr]egue|[Nn]ão regue|[Pp]are|[Aa]bra|[Ff]aça|[Cc]ubra|[Cc]hegue|[Pp]roteja|[Pp]refira)\b")
# Ressalva de horário da rega, não uma ordem de "não regar".
_PADRAO_RESSALVA_NOITE: "re.Pattern[str]" = re.compile(r"[Nn]ão regue à noite")
_PADRAO_SENTENCA: "re.Pattern[str]" = re.compile(r"[^.!?]+[.!?]?")
# Ações que não pedem figura própria quando faladas: "nada a fazer" e "confira o aparelho".
ACOES_SEM_COBERTURA: Tuple[str, ...] = ("NADA_A_FAZER", "VERIFICAR_APARELHO")


def acoes_faladas(mensagem: str) -> List[str]:
    """Ações que a mensagem falada PEDE, na ordem em que são ditas (no máximo ``MAXIMO_ACOES``).

    Uma ação é pedida quando uma das suas ``PALAVRAS_CHAVE_ACAO`` aparece numa
    frase de ordem (com verbo no imperativo: "Regue", "Cubra", "Faça",
    "Prefira"...). Não contam: o prazo ("amanhã de manhã" numa frase sem ordem),
    a ressalva "Não regue à noite" (é o horário da rega, não a ordem de parar)
    nem ``ACOES_SEM_COBERTURA``. A família de rega (``ACOES_DE_REGA``) conta uma
    vez só, com o momento dito primeiro.

    Args:
        mensagem: frase falada ao agricultor.

    Returns:
        Lista de códigos de ``ACOES_VALIDAS``, sem repetição.
    """
    texto = _PADRAO_RESSALVA_NOITE.sub(lambda achado: " " * len(achado.group(0)), str(mensagem or ""))
    posicoes: Dict[str, int] = {}
    for sentenca in _PADRAO_SENTENCA.finditer(texto):
        trecho = sentenca.group(0)
        if not _PADRAO_ORDEM.search(trecho):
            continue
        for acao, chaves in PALAVRAS_CHAVE_ACAO.items():
            if acao in ACOES_SEM_COBERTURA:
                continue
            achados = [trecho.find(chave) for chave in chaves if chave in trecho]
            if achados:
                posicao = sentenca.start() + min(achados)
                posicoes[acao] = min(posicao, posicoes.get(acao, posicao))
    resultado: List[str] = []
    for acao in sorted(posicoes, key=lambda codigo: (posicoes[codigo], ACOES_VALIDAS.index(codigo))):
        if acao in ACOES_DE_REGA and any(outra in ACOES_DE_REGA for outra in resultado):
            continue
        resultado.append(acao)
        if len(resultado) == MAXIMO_ACOES:
            break
    return resultado


def acoes_cobrem_mensagem(acoes: Sequence[str], mensagem: str) -> bool:
    """``True`` se toda ação que a voz pede tem pictograma (voz → figura).

    Toda ação cuja palavra-chave aparece numa frase de ordem da mensagem
    (``acoes_faladas``; menos ``NADA_A_FAZER`` e ``VERIFICAR_APARELHO``) precisa
    estar em ``acoes`` com o mesmo código, ou com um código da mesma família de
    rega (``ACOES_DE_REGA``). Se a voz pedir mais de ``MAXIMO_ACOES`` ações,
    basta cobrir as três primeiras ditas.

    Args:
        acoes: códigos mostrados como pictogramas.
        mensagem: frase falada ao agricultor.
    """
    mostradas = set(acoes)
    tem_rega = any(acao in ACOES_DE_REGA for acao in mostradas)
    for acao in acoes_faladas(mensagem):
        if acao in mostradas or (acao in ACOES_DE_REGA and tem_rega):
            continue
        return False
    return True


def _contar_palavras(texto: str) -> int:
    """Palavras separadas por espaço (o mesmo critério de ``len(texto.split())``)."""
    return len(texto.split())


def _juntar_ordens(frases: Sequence[str]) -> str:
    """Junta até três ordens: "A", "A e b", "A, b e c" (só a 1ª com maiúscula)."""
    partes = [frases[0]] + [frase[:1].lower() + frase[1:] for frase in frases[1:]]
    if len(partes) == 1:
        return partes[0]
    return ", ".join(partes[:-1]) + " e " + partes[-1]


def montar_fala_curta(nivel: int, acoes: Sequence[str], causa: str, *, situacao: str = SITUACAO_NORMAL) -> str:
    """Fala curta do semáforo: a cor, logo depois O QUE FAZER e, se couber, o motivo.

    Na situação normal: ``'Sinal <cor>. '`` + uma frase com até três ações
    (``FRASES_ACAO_CURTAS``, juntadas com ", " e " e ") + ``'. '`` + a frase da
    causa (``FRASES_CAUSA_CURTAS``, só se não for ``NENHUMA`` nem ``APARELHO``).
    Amarelo ou Vermelho sem nenhuma ação usa o "olhe" da própria mensagem
    longa (``FRASES_OLHAR_CURTAS``), para a 2ª frase continuar sendo uma ação.
    Exemplo: ``montar_fala_curta(2, ["REGAR_AGORA"], "TERRA_SECA")`` →
    ``'Sinal vermelho. Regue agora. A terra está seca.'``. Sem números e com no
    máximo ``MAXIMO_PALAVRAS_FALA_CURTA`` (15) palavras: se passar, a causa cai.

    Nas situações em que a cor NÃO deve ser seguida, a fala é fixa
    (``FALAS_SITUACAO``), começa por "Atenção." e não diz "Sinal":
    ``'teste'`` (dados simulados), ``'antiga'`` (medição antiga ou relógio
    errado), ``'sem_leitura'`` e ``'demonstracao'``.

    Args:
        nivel: 0 verde, 1 amarelo ou 2 vermelho.
        acoes: códigos de ``ACOES_VALIDAS`` na ordem dos pictogramas (os
            desconhecidos e repetidos são ignorados; ``NADA_A_FAZER`` só vale sozinho).
        causa: código de ``CAUSAS_VALIDAS``.
        situacao: um de ``SITUACOES_FALA`` (padrão ``SITUACAO_NORMAL``).

    Returns:
        A fala curta, terminada em ponto.

    Raises:
        ValueError: ``nivel`` fora de 0, 1 e 2 ou ``situacao`` fora de ``SITUACOES_FALA``.
    """
    if isinstance(nivel, bool) or not isinstance(nivel, int) or nivel not in CORES_FALADAS:
        raise ValueError(f"Nível inválido: {nivel!r}. Válidos: 0, 1 e 2.")
    if situacao not in SITUACOES_FALA:
        raise ValueError(f"Situação inválida: {situacao!r}. Válidas: {', '.join(SITUACOES_FALA)}.")
    if situacao != SITUACAO_NORMAL:
        return FALAS_SITUACAO[situacao]
    codigos: List[str] = []
    for acao in acoes:
        codigo = str(acao or "").strip().upper()
        if codigo in FRASES_ACAO_CURTAS and codigo not in codigos:
            codigos.append(codigo)
    if len(codigos) > 1:
        codigos = [codigo for codigo in codigos if codigo != "NADA_A_FAZER"]
    codigos = codigos[:MAXIMO_ACOES]
    abertura = f"Sinal {CORES_FALADAS[nivel]}."
    codigo_causa = str(causa or "").strip().upper()
    frase_causa = (FRASES_CAUSA_CURTAS[codigo_causa] + "."
                   if codigo_causa in FRASES_CAUSA_CURTAS and codigo_causa not in (CAUSA_NENHUMA, CAUSA_APARELHO)
                   else "")
    olhar = (FRASES_OLHAR_CURTAS.get(codigo_causa, FRASES_OLHAR_CURTAS[CAUSA_NENHUMA]) + "."
             if nivel > 0 and not codigos else "")
    while True:
        ordem = _juntar_ordens([FRASES_ACAO_CURTAS[codigo] for codigo in codigos]) + "." if codigos else olhar
        fala = " ".join(parte for parte in (abertura, ordem, frase_causa) if parte)
        if _contar_palavras(fala) <= MAXIMO_PALAVRAS_FALA_CURTA:
            return fala
        if frase_causa:
            frase_causa = ""
        else:
            codigos = codigos[:-1]


# ---------------------------------------------------------------------------
# Causa do alerta (pictograma ao lado do título)
# ---------------------------------------------------------------------------
def _causa_da_leitura(status_alerta: int, temperatura_ar: float, umidade_solo: float,
                      parametros: ParametrosCultura) -> str:
    """Causa pela leitura, nas mesmas condições de ``gerar_mensagem_orientacao`` (``NENHUMA`` se nada fora do ideal).

    A variável com a classe pior manda; no empate vale a água (a mensagem e os
    pictogramas também começam pela água).
    """
    faixa_temperatura = parametros.faixa(VARIAVEL_TEMPERATURA_AR)
    faixa_solo = parametros.faixa(VARIAVEL_UMIDADE_SOLO)
    deficit, desvio = calcular_features(temperatura_ar, umidade_solo, parametros)
    causa_agua = "TERRA_SECA" if deficit > 0 else ("ENCHARCADO" if deficit < 0 else "")
    causa_temperatura = "CALOR" if desvio > 0 else ("FRIO_GEADA" if desvio < 0 else "")
    if causa_agua and causa_temperatura:
        classe_agua = classificar_variavel(umidade_solo, faixa_solo)
        classe_temperatura = classificar_variavel(temperatura_ar, faixa_temperatura)
        return causa_temperatura if classe_temperatura > classe_agua else causa_agua
    if causa_agua or causa_temperatura:
        return causa_agua or causa_temperatura
    if int(status_alerta) == STATUS_VERDE:
        return CAUSA_NENHUMA
    # A IA elevou o status com tudo no ideal: a mensagem fala da borda mais próxima.
    borda = _borda_mais_proxima(temperatura_ar, umidade_solo, faixa_temperatura, faixa_solo)
    return {"calor": "CALOR", "frio": "FRIO_GEADA", "seca": "TERRA_SECA", "excesso": "ENCHARCADO"}[borda]


def derivar_causa(
    status: int,
    leitura: Dict[str, Any],
    parametros: ParametrosCultura,
    motivo_antecipacao: str = "NENHUM",
    estado_sensor: str = ESTADO_SENSOR_OK,
    mensagem_voz: str = "",
) -> str:
    """Código de ``CAUSAS_VALIDAS`` que explica a cor do semáforo (pictograma ao lado do título).

    Ordem de decisão:
        1. sensor com defeito ou sem números válidos → ``APARELHO``;
        2. ``leitura["motivo_regra"]`` não vazio (regra fisiológica da Frente 1)
           → ``CAUSA_POR_MOTIVO_REGRA`` (motivo desconhecido segue adiante);
        3. Verde sem nada fora do limite crítico e sem antecipação → ``NENHUMA``;
        4. sinal do déficit hídrico e do desvio térmico da leitura (como a
           mensagem falada da v2);
        5. leitura no ideal com antecipação → causa do motivo da antecipação;
        6. senão, a borda mais próxima (alerta elevado pela IA).

    Args:
        status: status final exibido (0, 1 ou 2).
        leitura: dicionário da leitura (``temperatura_ar``, ``umidade_solo`` e,
            depois da Frente 1, ``motivo_regra``); lido sempre com ``.get``.
        parametros: faixas da cultura.
        motivo_antecipacao: motivo do prognóstico de 12 h (``NENHUM`` se não houver).
        estado_sensor: ``OK`` ou qualquer outro estado (defeito, sem leitura...).
        mensagem_voz: frase falada da leitura (opcional). Só é usada em leituras
            com ``motivo_regra`` gravado (v3): se a causa calculada pela leitura
            instantânea não aparecer na frase, vale a causa que a frase diz (na
            regra fisiológica a noite fria com média de 24 h ideal é Verde e a voz
            diz "Tudo bem": a figura não pode dizer "Frio").

    Returns:
        Um código de ``CAUSAS_VALIDAS``. Nunca lança exceção por dado ruim.
    """
    estado = str(estado_sensor or ESTADO_SENSOR_OK).strip().upper()
    temperatura_ar = leitura.get("temperatura_ar")
    umidade_solo = leitura.get("umidade_solo")
    if estado != ESTADO_SENSOR_OK or not (_numero_valido(temperatura_ar) and _numero_valido(umidade_solo)):
        return CAUSA_APARELHO
    motivo_regra = _normalizar_motivo(leitura.get("motivo_regra"))
    if motivo_regra in CAUSA_POR_MOTIVO_REGRA:
        return CAUSA_POR_MOTIVO_REGRA[motivo_regra]
    try:
        nivel = int(status)
    except (TypeError, ValueError):
        nivel = STATUS_VERMELHO
    regra_gravada = str(leitura.get("motivo_regra") or "").strip().upper() == "NENHUM"
    if regra_gravada and nivel == STATUS_VERDE and _normalizar_motivo(motivo_antecipacao) == "":
        # A regra da v3 gravou "nada fora do lugar" e a tela está Verde: a causa é NENHUMA,
        # mesmo que a leitura instantânea esteja fora do ideal (noite fria com média ideal).
        return CAUSA_NENHUMA
    causa = _derivar_causa_da_leitura(nivel, leitura, parametros, motivo_antecipacao)
    if regra_gravada and mensagem_voz and not causa_coerente_com_mensagem(causa, mensagem_voz):
        for candidata in ("TERRA_SECA", "ENCHARCADO", "FRIO_GEADA", "CALOR", CAUSA_NENHUMA):
            if causa_coerente_com_mensagem(candidata, mensagem_voz):
                return candidata
    return causa


def _derivar_causa_da_leitura(nivel: int, leitura: Dict[str, Any], parametros: ParametrosCultura,
                              motivo_antecipacao: str) -> str:
    """Passos 3 a 6 de ``derivar_causa`` (leitura com números válidos; comportamento da v2)."""
    temperatura_ar = leitura.get("temperatura_ar")
    umidade_solo = leitura.get("umidade_solo")
    temperatura = float(temperatura_ar)  # type: ignore[arg-type]
    solo = float(umidade_solo)  # type: ignore[arg-type]
    motivo = _normalizar_motivo(motivo_antecipacao)
    try:
        causa = _causa_da_leitura(nivel, temperatura, solo, parametros)
    except (KeyError, ValueError):
        return CAUSA_NENHUMA if nivel == STATUS_VERDE else CAUSA_APARELHO
    faixa_temperatura = parametros.faixa(VARIAVEL_TEMPERATURA_AR)
    faixa_solo = parametros.faixa(VARIAVEL_UMIDADE_SOLO)
    dentro_do_ideal = (classificar_variavel(temperatura, faixa_temperatura) == STATUS_VERDE
                       and classificar_variavel(solo, faixa_solo) == STATUS_VERDE)
    if motivo in CAUSA_POR_MOTIVO_ANTECIPACAO and (dentro_do_ideal or causa == CAUSA_NENHUMA):
        return CAUSA_POR_MOTIVO_ANTECIPACAO[motivo]
    if nivel == STATUS_VERDE and causa != CAUSA_NENHUMA:
        urgente = (solo < faixa_solo.critico_min or solo > faixa_solo.critico_max
                   or temperatura < faixa_temperatura.critico_min or temperatura > faixa_temperatura.critico_max)
        # Verde sem urgência: a voz diz "Tudo bem", então a causa também.
        return causa if urgente else CAUSA_NENHUMA
    return causa


def causa_coerente_com_mensagem(causa: str, mensagem: str) -> bool:
    """``True`` se a mensagem falada tiver ao menos uma palavra-chave da causa."""
    return any(chave.lower() in mensagem.lower() for chave in PALAVRAS_CHAVE_CAUSA.get(causa, ()))
