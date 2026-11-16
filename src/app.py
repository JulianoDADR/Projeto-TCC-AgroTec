"""Módulo 6 — Companheiro Virtual do AgroAnálise (servidor Flask).

Entrega ao agricultor familiar, em linguagem simples, o estado da lavoura no
formato de um SEMÁFORO (Verde, Amarelo, Vermelho) com orientação falada. O
servidor apenas CONSOME o banco SQLite3 alimentado pelo coletor 24/7
(``coletor_serial.py``) e expõe uma API JSON para a página ``index.html``.

Rotas (todas JSON, exceto ``/``):
    * ``GET  /``                         → página do Companheiro Virtual;
    * ``GET  /api/status``               → leitura exibida no semáforo;
    * ``GET  /api/historico?limite=24``  → últimas leituras (1 a 500);
    * ``GET  /api/culturas``             → culturas ativas e a cultura monitorada;
    * ``POST /api/cultura``              → troca a cultura monitorada;
    * ``GET  /api/cenarios``             → cenários extremos do Modo Demonstração;
    * ``POST /api/demonstracao``         → injeta um cenário extremo (banca);
    * ``POST /api/demonstracao/encerrar``→ volta ao modo normal;
    * ``GET  /api/saude``                → verificação de funcionamento;
    * ``GET  /static/agro.<hash>.css``   → CSS não crítico da página (cache imutável de 1 ano).

Regras de exibição:
    * Com o Modo Demonstração ativo, o semáforo mostra a leitura mais recente
      de origem ``DEMONSTRACAO`` da cultura monitorada.
    * Fora dele, as leituras de demonstração são IGNORADAS: o semáforo mostra
      a leitura real (Campo USB) ou simulada mais recente, para que "Voltar ao
      normal" tenha efeito imediato mesmo antes da próxima coleta.
    * Histerese (fora da demonstração): piorar aparece NA HORA; melhorar só
      aparece depois de ``LEITURAS_CONFIRMACAO_MELHORA`` leituras seguidas
      melhores (dentro de ``JANELA_CONFIRMACAO_SEGUNDOS``). Com a temperatura
      oscilando perto de uma borda, o semáforo não pisca.
    * Leitura antiga (coletor parado): passado o limite de idade, o semáforo
      fica amarelo com o aviso "Sem medições novas" — nunca repete a última
      orientação como se fosse atual.
    * Modo Simulação: a leitura sintética NÃO vira orientação ao agricultor; o
      título e a mensagem falada avisam que são dados de teste e que o
      aparelho do campo está desligado.
    * Uma demonstração vencida é encerrada pelo próprio servidor (além do
      coletor), para nunca deixar o sistema preso no modo de banca.

Interface de campo (pictogramas, banca protegida, rede fraca e saúde honesta):
    * ``/api/status`` traz ``acoes`` (pictogramas de ação de ``acoes_orientacao``),
      a antecipação do prognóstico (``motivo_principal``, ``prazo_horas``,
      ``horas_em_estresse``, ``antecipacao``; módulo ``prognostico`` opcional,
      importado sob demanda), ``estado_sensor`` (defeito → "Confira o aparelho"),
      ``relogio_inconsistente`` (leitura vinda do "futuro"), ``coletor_vivo``
      (batimento do coletor), a última troca de cultura e o intervalo de
      repetição do alerta Vermelho.
    * POST da banca e da troca de cultura exigem o PIN (cabeçalho
      ``X-PIN-Banca``) quando ``pin_banca_hash`` estiver definido
      (``python src/seguranca_banca.py --definir-pin 1234``); 5 erros do mesmo
      IP bloqueiam por 1 minuto (429). Corpo limitado a 4 KB (413).
    * Respostas HTML/JSON/JS acima de 1 KB vão comprimidas com gzip quando o
      navegador aceita; a página já sai com o estado atual embutido
      (``<script id="estado-inicial">``), útil em rede 2G.
    * ``/api/saude`` detalha ``componentes`` (banco, coletor, disco, relógio,
      prognóstico, serial) e responde 503 com banco fora, coletor parado ou
      disco quase cheio.

Retorno do agricultor e ponte com o técnico (v3):
    * ``/api/status`` traz a CAUSA (``causa``, ``causa_descricao``,
      ``legenda_causa``; ``acoes_orientacao.derivar_causa``) e
      ``causa_silenciada`` (houve "Feito" para ela na última hora);
    * ``POST /api/acao_feita``      → "Feito" de um toque (sem PIN, ≤ 1 KB, 10/min por IP);
    * ``POST /api/pedido_ajuda``    → "Chamar o técnico" (sem PIN, 3 a cada 10 min por IP),
      devolve o link ``https://wa.me/...`` quando o número do técnico está cadastrado;
    * ``POST /api/evento``          → eventos da tela (ouviu, tocou num pictograma...);
    * ``GET  /tecnico``             → página do técnico (``templates/tecnico.html``);
    * ``GET  /api/tecnico/resumo``  → resumo de N dias (PIN);
    * ``POST /api/tecnico/configuracao`` → número do WhatsApp do técnico (PIN);
    * ``POST /api/banca/florada`` e ``POST /api/banca/seca_programada`` (PIN);
    * ``GET /api/calibracao`` e ``POST /api/calibracao/{ponto,concluir,aferir,fabrica}`` (PIN),
      assistente de calibração da sonda de solo (``calibracao_sonda``).
    Tudo funciona sem internet: o link do WhatsApp só é aberto pelo celular.

Página do agricultor (v4):
    * entrega em duas partes: CSS crítico inline (primeira tela) e o resto em
      ``/static/agro.<hash>.css`` (``templates/agricultor.css`` minificado,
      ``Cache-Control: public, max-age=31536000, immutable``, gzip); o JS
      continua inline, sem ``<script src>``. Sem Service Worker (http na rede
      local não é contexto seguro);
    * ``/api/status`` traz ``fala_curta`` (sinal + ação em até 15 palavras,
      ``acoes_orientacao.montar_fala_curta``) e ``aparelho_da_banca`` (este
      aparelho ligou a demonstração: fora dele, a página mostra a demonstração
      como NEUTRA, sem cor da lavoura);
    * ``POST /api/acao_feita`` não grava toque repetido (``repetido``) e
      ``POST /api/acao_feita/desfazer`` desfaz o "Feito" em até 120 s;
    * PIN errado responde 403 com ``codigo`` (``pin_errado``/``pin_necessario``)
      e o bloqueio (429) traz ``segundos_restantes``.

Execução: ``python src/app.py`` (``AGRO_HOST``/``AGRO_PORTA``, padrão 0.0.0.0:5000;
``AGRO_BANCO`` troca o banco) ou ``flask --app src/app.py run``.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib
import itertools
import logging
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple, TypeVar, cast

from flask import Flask, Response, abort, jsonify, render_template, request
from jinja2 import Environment, FileSystemLoader
from werkzeug.exceptions import HTTPException

import acoes_orientacao
import calibracao_sonda
import retorno_agricultor
import seguranca_banca
from acoes_orientacao import (
    ACOES_VERIFICAR_APARELHO,
    CAUSA_APARELHO,
    CAUSA_NENHUMA,
    DESCRICOES_ACAO,
    DESCRICOES_CAUSA,
    ESTADO_SENSOR_OK,
    LEGENDAS_ACAO,
    LEGENDAS_CAUSA,
    derivar_acoes,
    derivar_causa,
)
from coletor_serial import SaneadorTempoReal, carregar_modelo, processar_leitura
from database import (
    CAMINHO_BANCO,
    CAMINHO_MODELO,
    CORES_STATUS,
    CULTURA_PADRAO,
    MODO_CAMPO_USB,
    MODO_DEMONSTRACAO,
    MODO_SIMULACAO,
    NOMES_STATUS,
    STATUS_AMARELO,
    RegistroLeitura,
    buscar_historico,
    buscar_parametros_cultura,
    definir_configuracao,
    encerrar_demonstracao_se_inalterada,
    inicializar_banco,
    listar_culturas,
    obter_conexao,
    obter_configuracao,
    obter_configuracao_tipada,
    obter_todas_configuracoes,
)
from gerador_dataset import CENARIOS_DEMONSTRACAO, gerar_leitura_cenario
from seguranca_banca import CHAVE_CONFIGURACAO_PIN, LimitadorJanela, LimitadorTentativas, verificar_pin

# ---------------------------------------------------------------------------
# Constantes da interface
# ---------------------------------------------------------------------------
LIMITE_HISTORICO_PADRAO: int = 24
LIMITE_HISTORICO_MINIMO: int = 1
LIMITE_HISTORICO_MAXIMO: int = 500
# Quantas leituras recentes examinar para achar a leitura a exibir quando a
# mais recente for de outra origem (ex.: demonstração recém-encerrada).
LIMITE_BUSCA_LEITURA_EXIBIDA: int = 500
# Quantas vezes "aclimatar" o saneador ao valor do cenário antes de gravar.
TENTATIVAS_ACLIMATACAO_SANEADOR: int = 6
DURACAO_DEMONSTRACAO_PADRAO: int = 120
ESTADO_HARDWARE_CONECTADO: str = "CONECTADO"
ESTADO_HARDWARE_DESCONECTADO: str = "DESCONECTADO"
INTERVALO_COLETA_PADRAO: float = 5.0

# Histerese do semáforo: piorar é imediato; melhorar exige N leituras seguidas melhores.
LEITURAS_CONFIRMACAO_MELHORA: int = 24          # ~2 min com o intervalo padrão de 5 s
JANELA_CONFIRMACAO_SEGUNDOS: float = 300.0
LIMITE_BUSCA_HISTERESE: int = 48
# Idade máxima da leitura exibida: max(mínimo, ciclos × intervalo de coleta).
IDADE_MAXIMA_LEITURA_MINIMA_SEGUNDOS: float = 60.0
CICLOS_ATE_LEITURA_ANTIGA: int = 6

MENSAGEM_SEM_LEITURA: str = "Ainda não recebi dados do sensor. Verifique se o aparelho está ligado."
TITULO_SEM_LEITURA: str = "Esperando o sensor"
TITULO_TROCANDO_CULTURA: str = "Trocando de plantação"
TITULO_LEITURA_ANTIGA: str = "Sem medições novas"
TITULO_SIMULACAO: str = "Sensor desligado: dados de teste"
MENSAGEM_SIMULACAO: str = (
    "O aparelho do campo não está mandando medições. As cores agora são só um teste e não mostram a sua lavoura. "
    "Não regue nem mude nada por causa delas. Verifique o cabo do aparelho."
)

TITULO_CONFIRA_APARELHO: str = "Confira o aparelho"
TITULO_RELOGIO_ERRADO: str = "Relógio do aparelho errado"
MENSAGEM_RELOGIO_ERRADO: str = (
    "O relógio do computador está errado: as medições parecem vir do futuro. "
    "Peça ajuda para acertar a data e a hora do computador. A última cor pode não valer mais."
)
TITULO_COLETOR_PARADO: str = "O programa parou"
MENSAGEM_COLETOR_PARADO: str = (
    "O programa do computador parou. Desligue e ligue o computador. A última cor pode não valer mais."
)
MENSAGEM_TROCANDO_CULTURA: str = "Estou preparando as medições para a plantação de {nome}. Aguarde alguns segundos."

# Relógio: leitura mais de 2 minutos "no futuro" denuncia relógio errado no computador do coletor.
TOLERANCIA_FUTURO_SEGUNDOS: float = 120.0
# Coletor vivo: batimento mais novo que max(60 s, 3 × intervalo de coleta).
IDADE_MAXIMA_BATIMENTO_MINIMA_SEGUNDOS: float = 60.0
CICLOS_ATE_COLETOR_PARADO: int = 3
ESPACO_MINIMO_DISCO_MB_PADRAO: float = 200.0
INTERVALO_REPETICAO_ALERTA_PADRAO: int = 600
# Antecipação (prognóstico de 12 h): módulo opcional da frente antecipacao-ia.
NOME_MODULO_ANTECIPACAO: str = "prognostico"
NOVA_TENTATIVA_IMPORTACAO_SEGUNDOS: float = 60.0
MOTIVO_NENHUM: str = "NENHUM"
MOTIVOS_ANTECIPACAO_PADRAO: Tuple[str, ...] = (
    "SECA_CHEGANDO", "SECA_AGORA", "ENCHARCAMENTO", "CALOR_TARDE", "GEADA_NOITE", "FRIO", MOTIVO_NENHUM,
)
# Banca: PIN opcional, limite de tentativas e tamanho máximo do corpo.
CABECALHO_PIN_BANCA: str = "X-PIN-Banca"
MENSAGEM_PIN_NECESSARIO: str = "Peça a senha ao técnico."
MENSAGEM_MUITAS_TENTATIVAS: str = "Muitas tentativas. Espere um minuto."
TAMANHO_MAXIMO_CORPO_BYTES: int = 4096
# Compressão gzip das respostas (rede 2G).
TIPOS_COMPRIMIVEIS: Tuple[str, ...] = ("text/html", "text/css", "application/json", "application/javascript")
TAMANHO_MINIMO_COMPRESSAO: int = 1024
# Nível máximo: as respostas são pequenas (a página tem ~45 KB), custa ~1 ms e cada byte conta na rede 2G.
NIVEL_COMPRESSAO: int = 9

# Campos de /api/status que a página não usa: ficam fora do estado embutido em "/" (orçamento de 16 KB gzip).
CAMPOS_FORA_DA_PAGINA: frozenset = frozenset((
    "deficit_hidrico_solo", "desvio_termico", "origem", "estado_hardware", "versao_modelo", "nome_status",
    "descricao_anomalia", "idade_leitura_segundos", "orientacao_simulada", "status_em_confirmacao",
    "data_hora_ultima_leitura", "motivo_principal", "prazo_horas", "horas_em_estresse", "antecipacao",
    "estado_sensor", "coletor_vivo", "cultura_anterior", "categoria_cultura",
    # v4: os números ficam nos "detalhes" fechados, abaixo da dobra, e o intervalo de repetição só vale depois
    # de 10 min: chegam na primeira consulta a /api/status (5 s), sem pesar na primeira pintura.
    "temperatura_ar", "umidade_ar", "umidade_solo", "intervalo_repeticao_alerta_segundos",
))

# Retorno do agricultor (rotas sem PIN): corpo pequeno e limite de pedidos por IP.
TAMANHO_MAXIMO_CORPO_RETORNO_BYTES: int = 1024
LIMITE_FEITO_POR_MINUTO: int = 10
LIMITE_PEDIDOS_AJUDA: int = 3
JANELA_PEDIDOS_AJUDA_S: float = 600.0
LIMITE_EVENTOS_POR_MINUTO: int = 60
ATRASO_MAXIMO_FEITO_S: int = 86400  # um "Feito" guardado sem rede vale até um dia depois
TIPOS_EVENTO_DA_PAGINA: Tuple[str, ...] = ("ouviu", "tocou_pictograma", "ajuda", "sem_voz", "rede_caida")
MENSAGEM_MUITOS_PEDIDOS: str = "Muitos toques seguidos. Espere um pouco."
# Seca programada do café (Embrapa Cerrados: de 24/06 a 04/09):
# https://sbicafe.ufv.br/items/46703cd6-1804-4062-841a-3af5714ec5ac
SECA_PROGRAMADA_INICIO_PADRAO: str = "06-24"
SECA_PROGRAMADA_FIM_PADRAO: str = "09-04"
DIAS_RESUMO_TECNICO_PADRAO: int = 7

# ---------------------------------------------------------------------------
# Página do agricultor v4: CSS estático com cache imutável
# ---------------------------------------------------------------------------
CAMINHO_CSS_AGRICULTOR: Path = Path(__file__).resolve().parent / "templates" / "agricultor.css"
TAMANHO_HASH_CSS: int = 10
CACHE_IMUTAVEL: str = "public, max-age=31536000, immutable"
CACHE_ICONE: str = "public, max-age=604800"
FAVICON_SVG: str = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 8 24'><circle cx='4' cy='4' r='3.5' fill='red'/>"
    "<circle cx='4' cy='12' r='3.5' fill='orange'/><circle cx='4' cy='20' r='3.5' fill='green'/></svg>"
)

# ---------------------------------------------------------------------------
# Interfaces da Frente B (conteúdo, retorno e PIN), usadas diretamente
# ---------------------------------------------------------------------------
SITUACAO_FALA_NORMAL: str = acoes_orientacao.SITUACAO_NORMAL
SITUACAO_FALA_TESTE: str = acoes_orientacao.SITUACAO_TESTE
SITUACAO_FALA_ANTIGA: str = acoes_orientacao.SITUACAO_ANTIGA
SITUACAO_FALA_SEM_LEITURA: str = acoes_orientacao.SITUACAO_SEM_LEITURA
JANELA_DESFAZER_SEGUNDOS: int = retorno_agricultor.JANELA_DESFAZER_SEGUNDOS
JANELA_REPETICAO_SEGUNDOS: int = retorno_agricultor.JANELA_REPETICAO_SEGUNDOS
CODIGO_PIN_ERRADO: str = seguranca_banca.CODIGO_PIN_ERRADO
CODIGO_PIN_NECESSARIO: str = seguranca_banca.CODIGO_PIN_NECESSARIO
MENSAGEM_PIN_ERRADO: str = seguranca_banca.MENSAGEM_PIN_ERRADO
MENSAGEM_DESFAZER_EXPIRADO: str = "Passou o tempo de desfazer."
MENSAGEM_DESFAZER_INEXISTENTE: str = "Não achei esse registro para desfazer."
FALA_DESFEITO: str = "Pronto, desfeito."
# Maior inteiro que o SQLite guarda (INTEGER de 64 bits): acima disso o id vindo do cliente é inválido
# (400), em vez de estourar no banco (500). Integração v4.
MAIOR_ID_SQLITE: int = 2 ** 63 - 1

# Ícone (símbolo SVG da página) de cada cultura: distinto por código; as demais caem na categoria.
ICONES_CULTURA_CODIGO: Dict[str, str] = {
    "cafe_arabica": "#i-cafe-arabica",
    "cafe_conilon": "#i-cafe-conilon",
}
ICONES_CATEGORIA_SVG: Dict[str, str] = {"cafe": "#i-cafe"}
ICONE_CULTURA_PADRAO: str = "#i-broto"

ROTULOS_MODO: Dict[str, str] = {
    MODO_CAMPO_USB: "Campo USB",
    MODO_SIMULACAO: "Simulação",
    MODO_DEMONSTRACAO: "Demonstração",
}
ICONES_CATEGORIA: Dict[str, str] = {
    "cafe": "☕",
    "fruta": "🍎",
    "grao": "🌾",
    "hortalica": "🥬",
    "leguminosa": "🫘",
    "outra": "🌱",
}
# Símbolo SVG da página para cada cenário da banca (reaproveita os pictogramas de ação).
ICONES_CENARIO: Dict[str, str] = {
    "SECA_EXTREMA": "#i-regador",
    "ONDA_DE_CALOR": "#i-guarda-sol",
    "GEADA": "#i-manta",
    "ENCHARCAMENTO": "#i-valeta",
    "ATENCAO_MODERADA": "#i-atencao",
    "CONDICAO_IDEAL": "#i-check",
}

registrador: logging.Logger = logging.getLogger("agroanalise.app")


class ErroApi(Exception):
    """Erro previsto da API, convertido em ``{"erro": mensagem, **extras}`` com o código HTTP dado."""

    def __init__(self, mensagem: str, codigo_http: int = 400, extras: Optional[Dict[str, Any]] = None) -> None:
        """Guarda a mensagem simples (exibida ao usuário), o código HTTP e campos extras da resposta.

        ``extras`` leva dados que a página usa para reagir sem ler a mensagem
        (ex.: ``{"codigo": "pin_errado"}`` ou ``{"segundos_restantes": 42}``).
        """
        super().__init__(mensagem)
        self.mensagem: str = mensagem
        self.codigo_http: int = codigo_http
        self.extras: Dict[str, Any] = dict(extras or {})


# ---------------------------------------------------------------------------
# Aplicação Flask
# ---------------------------------------------------------------------------
class CarregadorMinificado(FileSystemLoader):
    """Carrega os templates ``.html`` sem comentários nem indentação (página menor em rede 2G).

    A minificação é conservadora e roda uma vez por template (o Jinja guarda o
    resultado compilado): em ``<style>`` tira comentários e espaços sobrando;
    em ``<script>`` comum tira linhas só de comentário e a indentação; no HTML
    tira comentários ``<!-- -->`` e a indentação. Blocos ``<script
    type="application/json">`` e as marcações do Jinja ficam intactos. O
    arquivo-fonte continua legível e comentado.
    """

    _BLOCO: "re.Pattern[str]" = re.compile(r"(<style>.*?</style>|<script(?: [^>]*)?>.*?</script>)", re.S)

    def get_source(self, environment: Environment, template: str) -> Tuple[str, Optional[str], Optional[Callable[[], bool]]]:
        """Fonte do template já minificada quando for ``.html``."""
        fonte, caminho, atualizado = super().get_source(environment, template)
        if template.endswith(".html"):
            fonte = minificar_html(fonte)
        return fonte, caminho, atualizado


def _minificar_css(css: str) -> str:
    """Remove comentários e espaços desnecessários de um bloco CSS.

    Além de comentários e espaços em volta de ``{};,>``, tira o zero à esquerda
    de decimais (``0.5`` → ``.5``) e os espaços em volta de ``(``/``)`` só onde
    a sintaxe não depende deles (nunca dentro de ``calc``, em que ``+`` e ``-``
    exigem espaço). A página do agricultor tem orçamento de 16 KB gzip.
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = re.sub(r"\s+", " ", css)
    css = re.sub(r"\s*([{};,>])\s*", r"\1", css).replace(";}", "}")
    # "propriedade: valor" → "propriedade:valor" (só depois de letra/hífen; seletores como ":focus" não mudam).
    css = re.sub(r"([\w-]):\s+", r"\1:", css)
    css = re.sub(r"(?<![\w.])0\.(\d)", r".\1", css)
    return re.sub(r"\s*!important", "!important", css).strip()


# ---------------------------------------------------------------------------
# Minificação do JavaScript da página (orçamento de 16 KB gzip)
# ---------------------------------------------------------------------------
_JS_TOKEN: "re.Pattern[str]" = re.compile(r"""
     (?P<ws>\s+)
    |(?P<lc>//[^\n]*)
    |(?P<bc>/\*.*?\*/)
    |(?P<str>"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')
    |(?P<num>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
    |(?P<id>[A-Za-z_$][A-Za-z0-9_$]*)
    |(?P<punct>>>>=|===|!==|>>>|<<=|>>=|\.\.\.|=>|==|!=|<=|>=|&&|\|\||\?\?|\+\+|--|\+=|-=|\*=|%=|&=|\|=|\^=|<<|>>
               |[{}()\[\];,<>+\-*%&|^!~?:=.])
    |(?P<barra>/)
""", re.S | re.X)
_JS_REGEX: "re.Pattern[str]" = re.compile(r"/(?:\\.|\[(?:\\.|[^\]\\\n])*\]|[^/\\\n\[])+/[a-z]*")
# Depois destes, "/" começa uma expressão regular (e não uma divisão).
_JS_ANTES_DE_REGEX: frozenset = frozenset(
    "( , = : [ ! & | ? { } ; + - * % < > ~ ^ && || == === != !== => ?? += -= return typeof case do else in of "
    "new delete void throw".split())
_JS_FIM_DE_EXPRESSAO: Tuple[str, ...] = ("id", "num", "str", "re")
_JS_RESERVADAS: frozenset = frozenset(
    "break case catch class const continue debugger default delete do else export extends false finally for "
    "function if import in instanceof let new null of return super switch this throw true try typeof var void "
    "while with yield await async arguments eval undefined NaN Infinity".split())
# Globais do navegador: um nome local igual a um deles nunca é trocado (o script continua certo
# mesmo se, em outra função, o mesmo nome for usado como a global).
_JS_GLOBAIS_DO_NAVEGADOR: frozenset = frozenset(
    "window document navigator location history screen self top parent frames name status event open close "
    "print alert confirm prompt fetch console performance localStorage sessionStorage setTimeout clearTimeout "
    "setInterval clearInterval requestAnimationFrame JSON Math Date String Number Boolean Object Array Promise "
    "Error RegExp Map Set Symbol URL URLSearchParams AbortController AudioContext SpeechSynthesisUtterance "
    "ResizeObserver Event isNaN parseInt parseFloat encodeURIComponent decodeURIComponent".split())


def _tokenizar_js(js: str) -> List[Tuple[str, str, bool]]:
    """Tokens ``(tipo, texto, havia_quebra_de_linha_antes)`` do JS, sem espaços nem comentários.

    Raises:
        ValueError: trecho que o tokenizador não reconhece (o chamador cai na minificação simples).
    """
    tokens: List[Tuple[str, str, bool]] = []
    posicao, quebra = 0, False
    while posicao < len(js):
        achado = _JS_TOKEN.match(js, posicao)
        if achado is None:
            raise ValueError(f"JS não reconhecido na posição {posicao}: {js[posicao:posicao + 20]!r}")
        tipo, texto = str(achado.lastgroup), achado.group()
        if tipo == "barra":
            anterior = tokens[-1][1] if tokens else "("
            if anterior in _JS_ANTES_DE_REGEX:
                regex = _JS_REGEX.match(js, posicao)
                if regex is None:
                    raise ValueError(f"Expressão regular não reconhecida na posição {posicao}.")
                tipo, texto = "re", regex.group()
            else:
                tipo, texto = "punct", ("/=" if js.startswith("/=", posicao) else "/")
        if tipo in ("ws", "lc", "bc"):
            quebra = quebra or "\n" in texto or tipo == "lc"
        else:
            tokens.append((tipo, texto, quebra))
            quebra = False
        posicao += len(texto)
    return tokens


def _nomes_declarados_js(tokens: List[Tuple[str, str, bool]]) -> Set[str]:
    """Nomes declarados no próprio script (``var``, ``function``, parâmetros e ``catch``)."""
    declarados: Set[str] = set()
    for indice, (tipo, texto, _) in enumerate(tokens):
        depois_de_ponto = indice > 0 and tokens[indice - 1][1] == "."
        if tipo == "id" and not depois_de_ponto and texto in ("function", "catch"):
            seguinte = indice + 1
            if texto == "function" and seguinte < len(tokens) and tokens[seguinte][0] == "id":
                declarados.add(tokens[seguinte][1])
                seguinte += 1
            if seguinte < len(tokens) and tokens[seguinte][1] == "(":
                seguinte += 1
                while seguinte < len(tokens) and tokens[seguinte][1] != ")":
                    if tokens[seguinte][0] == "id":
                        declarados.add(tokens[seguinte][1])
                    seguinte += 1
        elif texto == "var" and tipo == "id" and not depois_de_ponto:
            profundidade, seguinte, esperando_nome = 0, indice + 1, True
            while seguinte < len(tokens):
                atual = tokens[seguinte][1]
                if esperando_nome and tokens[seguinte][0] == "id":
                    declarados.add(atual)
                    esperando_nome = False
                elif atual in "([{":
                    profundidade += 1
                elif atual in ")]}":
                    profundidade -= 1
                    if profundidade < 0:
                        break
                elif profundidade == 0 and atual == ",":
                    esperando_nome = True
                elif profundidade == 0 and atual in (";", "in", "of"):
                    break
                seguinte += 1
    return declarados


def _gerador_nomes_curtos(proibidos: Set[str]) -> Iterator[str]:
    """Nomes curtos (``a``, ``b``, …, ``aa``…) que não colidem com nenhum identificador do script."""
    letras = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    tamanho = 1
    while True:
        for combinacao in itertools.product(letras, repeat=tamanho):
            nome = "".join(combinacao)
            if nome not in proibidos and nome not in _JS_RESERVADAS:
                yield nome
        tamanho += 1


def _renomear_locais_js(tokens: List[Tuple[str, str, bool]]) -> List[Tuple[str, str, bool]]:
    """Troca os nomes DECLARADOS no script por nomes curtos (os mais usados ficam com os menores).

    Seguro para o estilo da página (ES5, tudo dentro de uma função, sem ``eval``,
    ``with``, rótulos nem atalhos de objeto ``{nome}``): propriedades (depois de
    ``.``) e chaves de objeto (``{nome: …}`` / ``, nome: …``) nunca mudam, e os
    nomes novos não colidem com nenhum identificador existente.
    """
    if any(texto in ("eval", "with") for _, texto, _ in tokens):
        return tokens
    declarados = _nomes_declarados_js(tokens) - _JS_GLOBAIS_DO_NAVEGADOR

    def renomeavel(indice: int) -> bool:
        """``True`` se o token é um uso de variável (nem propriedade nem chave de objeto)."""
        tipo, texto, _ = tokens[indice]
        if tipo != "id" or texto not in declarados:
            return False
        anterior = tokens[indice - 1][1] if indice else ""
        seguinte = tokens[indice + 1][1] if indice + 1 < len(tokens) else ""
        return anterior != "." and not (seguinte == ":" and anterior in ("{", ","))

    usos: Dict[str, int] = {}
    for indice in range(len(tokens)):
        if renomeavel(indice):
            usos[tokens[indice][1]] = usos.get(tokens[indice][1], 0) + 1
    proibidos = {texto for tipo, texto, _ in tokens if tipo == "id"}
    novos = _gerador_nomes_curtos(proibidos)
    mapa: Dict[str, str] = {}
    for nome in sorted(usos, key=lambda chave: (-usos[chave] * len(chave), chave)):
        candidato = next(novos)
        if len(candidato) < len(nome):
            mapa[nome] = candidato
    return [(tipo, mapa.get(texto, texto) if renomeavel(indice) else texto, quebra)
            for indice, (tipo, texto, quebra) in enumerate(tokens)]


def _juntar_tokens_js(tokens: List[Tuple[str, str, bool]]) -> str:
    """Junta os tokens com o mínimo de separação; mantém a quebra de linha onde a inserção de ``;`` dependeria dela."""
    partes: List[str] = []
    for indice, (tipo, texto, quebra) in enumerate(tokens):
        if indice:
            tipo_anterior, anterior, _ = tokens[indice - 1]
            separador = ""
            if (re.match(r"[\w$]", anterior[-1]) and re.match(r"[\w$]", texto[0])) \
                    or (anterior[-1] in "+-" and texto[0] == anterior[-1]) or (tipo_anterior == "num" and texto[0] == "."):
                separador = " "
            if quebra and (tipo_anterior in _JS_FIM_DE_EXPRESSAO or anterior in (")", "]", "}", "++", "--")) \
                    and (tipo in _JS_FIM_DE_EXPRESSAO or texto in ("(", "[", "{", "++", "--", "+", "-", "!", "~")):
                separador = "\n"
            partes.append(separador)
        partes.append(texto)
    return "".join(partes)


def _minificar_js_simples(js: str) -> str:
    """Minificação de reserva: tira indentação, linhas vazias, linhas só de comentário e blocos ``/* */``."""
    linhas: List[str] = []
    em_comentario = False
    for linha in js.splitlines():
        texto = linha.strip()
        if em_comentario:
            em_comentario = "*/" not in texto
            continue
        if texto.startswith("/*"):
            em_comentario = "*/" not in texto
            continue
        if not texto or texto.startswith("//"):
            continue
        # Comentário /* ... */ no meio da linha (o código desta página não tem "/*" dentro de textos).
        linhas.append(re.sub(r"\s*/\*.*?\*/\s*", " ", texto).strip())
    return "\n".join(linhas)


def _minificar_js(js: str) -> str:
    """Minifica o JS da página: tokens sem espaços/comentários e nomes locais curtos.

    Roda uma vez por template (o Jinja guarda o resultado). Se o tokenizador
    não reconhecer algum trecho, cai na minificação simples por linhas (nunca
    quebra a página). O arquivo-fonte continua legível e comentado.
    """
    try:
        return _juntar_tokens_js(_renomear_locais_js(_tokenizar_js(js)))
    except ValueError as erro:
        registrador.warning("Minificação completa do JS indisponível (%s); usando a simples.", erro)
        return _minificar_js_simples(js)


def _compactar_caminho_svg(caminho: str) -> str:
    """Tira os espaços dispensáveis do atributo ``d`` de um ``<path>`` (antes de ``-`` e em volta das letras de comando)."""
    caminho = re.sub(r"\s+", " ", caminho.strip())
    caminho = re.sub(r" ?([A-Za-z]) ?", r"\1", caminho)
    return re.sub(r" -", "-", caminho)


def minificar_html(fonte: str) -> str:
    """Minificação conservadora do template (ver ``CarregadorMinificado``)."""
    partes: List[str] = []
    for indice, pedaco in enumerate(CarregadorMinificado._BLOCO.split(fonte)):
        if indice % 2 == 0:  # HTML comum
            pedaco = re.sub(r"<!--.*?-->", "", pedaco, flags=re.S)
            pedaco = re.sub(r'(<path d=")([^"]*)(")', lambda achado: achado.group(1) + _compactar_caminho_svg(
                achado.group(2)) + achado.group(3), pedaco)
            partes.append("\n".join(linha.strip() for linha in pedaco.splitlines() if linha.strip()))
        elif pedaco.startswith("<style>"):
            partes.append("<style>" + _minificar_css(pedaco[len("<style>"):-len("</style>")]) + "</style>")
        elif 'type="application/json"' in pedaco.split(">", 1)[0]:
            partes.append(pedaco)
        else:
            abertura, resto = pedaco.split(">", 1)
            partes.append(abertura + ">" + _minificar_js(resto[:-len("</script>")]) + "</script>")
    return "\n".join(parte for parte in partes if parte)


# Sem a rota estática padrão do Flask: o único recurso estático é o CSS versionado (rota própria).
app: Flask = Flask(__name__, static_folder=None)
app.jinja_loader = CarregadorMinificado(str(Path(__file__).resolve().parent / "templates"))  # type: ignore[assignment]
app.json.ensure_ascii = False  # type: ignore[attr-defined]  # acentos legíveis no JSON
app.json.sort_keys = False  # type: ignore[attr-defined]  # mantém a ordem dos campos
# Estado embutido na página ({{ ...|tojson }}) sem espaços: cada byte conta no orçamento de 16 KB gzip.
app.jinja_env.policies["json.dumps_kwargs"] = {"sort_keys": False, "separators": (",", ":")}
# AGRO_BANCO (opcional) aponta outro banco, como faz src/servidor.py; útil nos testes de ponta a ponta.
_CAMINHO_BANCO_INICIAL: Path = Path(os.environ.get("AGRO_BANCO") or CAMINHO_BANCO)
app.config["CAMINHO_BANCO"] = _CAMINHO_BANCO_INICIAL
app.config["CAMINHO_MODELO"] = CAMINHO_MODELO
app.config["MAX_CONTENT_LENGTH"] = TAMANHO_MAXIMO_CORPO_BYTES
inicializar_banco(_CAMINHO_BANCO_INICIAL)

# Cache preguiçoso do pacote do modelo (.pkl), recarregado se o arquivo mudar.
_TRAVA_MODELO: threading.Lock = threading.Lock()
_CACHE_MODELO: Dict[str, Any] = {"chave": None, "pacote": None}
# Cache da importação do módulo de antecipação (falha lembrada por um minuto).
_CACHE_ANTECIPACAO: Dict[str, Any] = {"falhou_em": None, "avisado": False}
# Erros de PIN por IP (5 erros → 1 minuto de bloqueio).
_LIMITADOR_PIN: LimitadorTentativas = LimitadorTentativas(max_erros=5, bloqueio_s=60.0)
# Rotas sem PIN do agricultor: "Feito" (10/min), pedido de ajuda (3 a cada 10 min) e eventos (60/min).
_LIMITADOR_FEITO: LimitadorJanela = LimitadorJanela(LIMITE_FEITO_POR_MINUTO, 60.0)
_LIMITADOR_AJUDA: LimitadorJanela = LimitadorJanela(LIMITE_PEDIDOS_AJUDA, JANELA_PEDIDOS_AJUDA_S)
_LIMITADOR_EVENTOS: LimitadorJanela = LimitadorJanela(LIMITE_EVENTOS_POR_MINUTO, 60.0)
# Endereço do aparelho que ligou a demonstração em andamento (só ele vê a demonstração com cor).
_TRAVA_BANCA: threading.Lock = threading.Lock()
_APARELHO_BANCA: Dict[str, str] = {"ip": ""}


def carregar_css_agricultor(caminho: Path = CAMINHO_CSS_AGRICULTOR) -> Tuple[str, bytes]:
    """Lê o CSS não crítico da página do agricultor, minifica e devolve ``(hash, conteúdo)``.

    O hash (sha256, 10 caracteres hexadecimais) entra no nome do arquivo
    servido (``/static/agro.<hash>.css``): como o nome muda junto com o
    conteúdo, o navegador pode guardá-lo por um ano sem nunca pedir de novo.
    Sem o arquivo, devolve um CSS vazio (a página continua usável só com o CSS
    crítico inline).
    """
    try:
        fonte = caminho.read_text(encoding="utf-8")
    except OSError as erro:
        registrador.warning("CSS da página do agricultor indisponível (%s): só o CSS crítico será usado.", erro)
        fonte = ""
    conteudo = _minificar_css(fonte).encode("utf-8")
    return hashlib.sha256(conteudo).hexdigest()[:TAMANHO_HASH_CSS], conteudo


_HASH_CSS_AGRICULTOR, _CSS_AGRICULTOR = carregar_css_agricultor()
URL_CSS_AGRICULTOR: str = f"/static/agro.{_HASH_CSS_AGRICULTOR}.css"

_Funcao = TypeVar("_Funcao", bound=Callable[..., Any])


def _agora_utc() -> datetime:
    """Instante atual em UTC (ponto único do relógio das rotas novas; substituído nos testes)."""
    return datetime.now(timezone.utc)


def _caminho_banco() -> Path:
    """Caminho do banco em uso (``app.config['CAMINHO_BANCO']``; inicialmente ``AGRO_BANCO`` ou o padrão)."""
    return Path(app.config.get("CAMINHO_BANCO", CAMINHO_BANCO))


def _caminho_modelo() -> Path:
    """Caminho do pacote do modelo (configurável em ``app.config['CAMINHO_MODELO']``)."""
    return Path(app.config.get("CAMINHO_MODELO", CAMINHO_MODELO))


def obter_pacote_modelo() -> Optional[Dict[str, Any]]:
    """Carrega o modelo de forma preguiçosa e o mantém em memória.

    O arquivo só é relido quando muda (caminho ou data de modificação). Se
    estiver ausente ou corrompido, retorna ``None`` e o sistema segue com o
    motor de regras agronômicas.
    """
    caminho = _caminho_modelo()
    try:
        modificado_em: Optional[float] = caminho.stat().st_mtime
    except OSError:
        modificado_em = None
    chave = (str(caminho), modificado_em)
    with _TRAVA_MODELO:
        if _CACHE_MODELO["chave"] != chave:
            pacote = carregar_modelo(caminho) if modificado_em is not None else None
            if pacote is None:
                registrador.warning("Modelo de IA indisponível em %s; usando apenas as regras agronômicas.", caminho)
            else:
                registrador.info("Modelo de IA carregado: %s", pacote.get("versao", "sem versão"))
            _CACHE_MODELO["chave"] = chave
            _CACHE_MODELO["pacote"] = pacote
        pacote_atual: Optional[Dict[str, Any]] = _CACHE_MODELO["pacote"]
    return pacote_atual


def _modulo_antecipacao() -> Optional[ModuleType]:
    """Módulo ``prognostico`` (antecipação de 12 h), importado sob demanda; ``None`` se ausente.

    O módulo é opcional: sem ele o semáforo funciona como antes (motivo
    ``NENHUM``). Uma importação que falhou só é tentada de novo depois de
    ``NOVA_TENTATIVA_IMPORTACAO_SEGUNDOS``; um módulo já presente em
    ``sys.modules`` (inclusive um módulo falso de teste) é usado na hora.
    """
    modulo = sys.modules.get(NOME_MODULO_ANTECIPACAO)
    if modulo is None:
        falhou_em = _CACHE_ANTECIPACAO.get("falhou_em")
        if falhou_em is not None and time.monotonic() - float(falhou_em) < NOVA_TENTATIVA_IMPORTACAO_SEGUNDOS:
            return None
        try:
            modulo = importlib.import_module(NOME_MODULO_ANTECIPACAO)
        except ImportError:
            _CACHE_ANTECIPACAO["falhou_em"] = time.monotonic()
            if not _CACHE_ANTECIPACAO.get("avisado"):
                registrador.info("Módulo de antecipação '%s' ausente; semáforo sem prognóstico.", NOME_MODULO_ANTECIPACAO)
                _CACHE_ANTECIPACAO["avisado"] = True
            return None
        except Exception as erro:  # o módulo existe, mas quebrou ao importar
            _CACHE_ANTECIPACAO["falhou_em"] = time.monotonic()
            registrador.warning("Falha ao importar '%s': %s", NOME_MODULO_ANTECIPACAO, erro)
            return None
    if not callable(getattr(modulo, "obter_analise_antecipacao", None)):
        return None
    _CACHE_ANTECIPACAO["falhou_em"] = None
    return modulo


def _numero_ou_none(valor: Any) -> Optional[float]:
    """Converte para ``float`` arredondado a 2 casas; ``None`` se ausente ou inválido."""
    if valor is None or isinstance(valor, bool):
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    return round(numero, 2) if numero == numero and abs(numero) != float("inf") else None


def obter_antecipacao(id_leitura: int, caminho: Path) -> Optional[Dict[str, Any]]:
    """Análise de antecipação da leitura (``prognostico.obter_analise_antecipacao``), saneada.

    Nunca lança exceção: módulo ausente, erro ou resposta inválida viram ``None``.
    """
    modulo = _modulo_antecipacao()
    if modulo is None:
        return None
    try:
        bruto = modulo.obter_analise_antecipacao(int(id_leitura), caminho)
    except Exception as erro:
        registrador.warning("Antecipação indisponível para a leitura %s: %s", id_leitura, erro)
        return None
    if not isinstance(bruto, dict):
        return None
    motivos = tuple(getattr(modulo, "MOTIVOS", MOTIVOS_ANTECIPACAO_PADRAO))
    motivo = str(bruto.get("motivo_principal") or MOTIVO_NENHUM).strip().upper()
    if motivo not in motivos:
        motivo = MOTIVO_NENHUM
    try:
        status_antecipado = int(bruto.get("status_antecipado", 0))
    except (TypeError, ValueError):
        status_antecipado = 0
    return {
        "probabilidade_risco_futuro": _numero_ou_none(bruto.get("probabilidade_risco_futuro")),
        "horas_ate_secar": _numero_ou_none(bruto.get("horas_ate_secar")),
        "temperatura_minima_prevista": _numero_ou_none(bruto.get("temperatura_minima_prevista")),
        "motivo_principal": motivo,
        "prazo_horas": _numero_ou_none(bruto.get("prazo_horas")),
        "horas_em_estresse": _numero_ou_none(bruto.get("horas_em_estresse")),
        "status_antecipado": status_antecipado if status_antecipado in (0, 1, 2) else 0,
        "versao_prognostico": str(bruto.get("versao_prognostico") or ""),
    }


# ---------------------------------------------------------------------------
# Funções auxiliares de domínio
# ---------------------------------------------------------------------------
def _interpretar_data_hora(texto: Optional[str]) -> Optional[datetime]:
    """Converte texto ISO em ``datetime``; ``None`` se vazio ou inválido.

    Aceita espaço ou ``T`` e os dois formatos gravados no banco: sem fuso
    (hora local, ex.: ``2026-05-10 14:00:00``) e com fuso (ex.:
    ``2026-05-10T17:00:00+00:00``). O resultado é *naive* no primeiro caso e
    *aware* no segundo; use ``_agora_comparavel`` para comparar.
    """
    conteudo = str(texto or "").strip()
    if not conteudo:
        return None
    try:
        return datetime.fromisoformat(conteudo)
    except ValueError:
        registrador.warning("Data/hora inválida: %r", conteudo)
        return None


def _agora_comparavel(referencia: datetime) -> datetime:
    """Instante atual do MESMO tipo de ``referencia`` (com fuso → UTC aware; sem fuso → hora local)."""
    if referencia.tzinfo is not None:
        return datetime.now(timezone.utc)
    return datetime.now()


def _para_utc(instante: datetime) -> datetime:
    """Converte para UTC *aware*; um ``datetime`` sem fuso é tratado como hora local."""
    return instante.astimezone(timezone.utc)


def _instante_leitura(leitura: Dict[str, Any]) -> Optional[datetime]:
    """Instante da leitura em UTC: ``instante_utc`` quando existir, senão ``data_hora`` (local)."""
    instante_utc = leitura.get("instante_utc")
    if instante_utc:
        instante = _interpretar_data_hora(str(instante_utc))
        if instante is not None:
            # instante_utc sem fuso explícito continua sendo UTC por definição.
            return instante.replace(tzinfo=timezone.utc) if instante.tzinfo is None else instante.astimezone(timezone.utc)
    instante_local = _interpretar_data_hora(str(leitura.get("data_hora") or ""))
    return _para_utc(instante_local) if instante_local is not None else None


def _segundos_desde(texto_iso: Optional[str]) -> Optional[float]:
    """Segundos desde um instante ISO (com ou sem fuso); ``None`` se vazio ou inválido."""
    instante = _interpretar_data_hora(texto_iso)
    if instante is None:
        return None
    return (_agora_comparavel(instante) - instante).total_seconds()


def _rotulo_curto_cultura(nome_comum: str, categoria: str) -> str:
    """Nome curto para o botão de troca de cultura (ex.: "Café Arábica" → "Arábica")."""
    if categoria == "cafe" and nome_comum.lower().startswith("café "):
        return nome_comum[len("café "):].strip() or nome_comum
    return nome_comum


def _alvo_cultura(nome_comum: str, categoria: str) -> str:
    """Como o título se refere à lavoura (ex.: "o seu café", "a sua plantação de tomate")."""
    if categoria == "cafe":
        return "o seu café"
    return f"a sua plantação de {nome_comum.lower()}"


def montar_titulo(status: int, nome_comum: str, categoria: str) -> str:
    """Título grande do estado, em linguagem simples, para qualquer cultura."""
    alvo = _alvo_cultura(nome_comum, categoria)
    if status == 0:
        return f"Tudo certo com {alvo}"
    if status == 1:
        return f"Atenção com {alvo}"
    return f"Perigo para {alvo}"


def _culturas_ativas(caminho: Path) -> List[Dict[str, Any]]:
    """Culturas ativas do catálogo, na ordem de cadastro."""
    return listar_culturas(apenas_ativas=True, caminho_banco=caminho)


def _localizar_cultura(codigo: str, culturas: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Procura uma cultura pelo código numa lista já carregada."""
    for cultura in culturas:
        if str(cultura["codigo"]) == codigo:
            return cultura
    return None


def _resolver_cultura_ativa(caminho: Path, culturas: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Cultura monitorada: a da configuração, ou a padrão/primeira ativa se ela tiver sido desativada.

    Raises:
        ErroApi: se não houver nenhuma cultura ativa cadastrada.
    """
    lista = culturas if culturas is not None else _culturas_ativas(caminho)
    if not lista:
        raise ErroApi("Nenhuma cultura está cadastrada no sistema.", 404)
    codigo = str(obter_configuracao("cultura_ativa", CULTURA_PADRAO, caminho_banco=caminho) or CULTURA_PADRAO)
    cultura = _localizar_cultura(codigo, lista) or _localizar_cultura(CULTURA_PADRAO, lista) or lista[0]
    return cultura


def _modo_normal(caminho: Path) -> str:
    """Modo para o qual voltar ao sair da demonstração: Campo USB se o Arduino estiver conectado."""
    estado = obter_configuracao("estado_hardware", ESTADO_HARDWARE_DESCONECTADO, caminho_banco=caminho)
    return MODO_CAMPO_USB if estado == ESTADO_HARDWARE_CONECTADO else MODO_SIMULACAO


def encerrar_demonstracao(caminho: Path) -> str:
    """Limpa o cenário e a expiração e devolve o modo normal; retorna o modo gravado."""
    modo = _modo_normal(caminho)
    definir_configuracao("cenario_demonstracao", "", caminho_banco=caminho)
    definir_configuracao("demonstracao_expira_em", "", caminho_banco=caminho)
    definir_configuracao("modo_operacao", modo, caminho_banco=caminho)
    registrador.info("Modo Demonstração encerrado; modo operante: %s.", modo)
    return modo


def _segundos_restantes_demonstracao(configuracoes: Dict[str, Any]) -> int:
    """Segundos até o fim da demonstração (0 se inativa, vencida ou sem expiração válida)."""
    if configuracoes.get("modo_operacao") != MODO_DEMONSTRACAO:
        return 0
    expira_em = _interpretar_data_hora(configuracoes.get("demonstracao_expira_em"))
    if expira_em is None:
        return 0
    # A expiração é gravada em UTC com fuso; bancos antigos podem tê-la em hora local sem fuso.
    return max(0, int((expira_em - _agora_comparavel(expira_em)).total_seconds() + 0.999))


def _encerrar_demonstracao_vencida(caminho: Path, configuracoes: Dict[str, Any]) -> Dict[str, Any]:
    """Se a demonstração venceu (ou ficou sem expiração), volta ao modo normal; retorna as configurações atuais."""
    if configuracoes.get("modo_operacao") == MODO_DEMONSTRACAO and _segundos_restantes_demonstracao(configuracoes) <= 0:
        # Encerramento condicional e atômico: se outra requisição (ou o coletor)
        # iniciou uma nova demonstração depois desta leitura, ela é preservada.
        encerrou = encerrar_demonstracao_se_inalterada(
            str(configuracoes.get("cenario_demonstracao") or ""),
            str(configuracoes.get("demonstracao_expira_em") or ""),
            _modo_normal(caminho),
            caminho_banco=caminho,
        )
        if encerrou:
            registrador.info("Demonstração vencida encerrada pelo servidor.")
        return obter_todas_configuracoes(caminho_banco=caminho)
    return configuracoes


def _leituras_recentes_exibiveis(
    codigo_cultura: str, demonstracao_ativa: bool, caminho: Path, quantidade: int = 1
) -> List[Dict[str, Any]]:
    """Até ``quantidade`` leituras exibíveis no modo atual, da mais recente para a mais antiga.

    Leituras de demonstração só valem durante a demonstração; as demais, fora
    dela. Com a demonstração ativa e nenhuma leitura própria ainda, devolve a
    mais recente que houver.
    """
    def aceita(leitura: Dict[str, Any]) -> bool:
        """Leituras de demonstração só valem durante a demonstração; as demais, fora dela."""
        origem_demonstracao = leitura.get("origem") == MODO_DEMONSTRACAO
        return origem_demonstracao if demonstracao_ativa else not origem_demonstracao

    limite_inicial = max(1, min(LIMITE_BUSCA_LEITURA_EXIBIDA, max(quantidade * 4, LIMITE_BUSCA_HISTERESE)))
    recentes = buscar_historico(limite=limite_inicial, codigo_cultura=codigo_cultura, caminho_banco=caminho)
    if not recentes:
        return []
    aceitas = [leitura for leitura in recentes if aceita(leitura)]
    if not aceitas and len(recentes) >= limite_inicial:
        mais = buscar_historico(limite=LIMITE_BUSCA_LEITURA_EXIBIDA, codigo_cultura=codigo_cultura, caminho_banco=caminho)
        aceitas = [leitura for leitura in mais if aceita(leitura)]
    if not aceitas:
        return [recentes[0]] if demonstracao_ativa else []
    return aceitas[:max(1, quantidade)]


def _selecionar_leitura_exibida(codigo_cultura: str, demonstracao_ativa: bool, caminho: Path) -> Optional[Dict[str, Any]]:
    """Leitura mais recente exibível conforme o modo (ver docstring do módulo); ``None`` se não houver."""
    leituras = _leituras_recentes_exibiveis(codigo_cultura, demonstracao_ativa, caminho, quantidade=1)
    return leituras[0] if leituras else None


def aplicar_histerese(leituras: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], bool]:
    """Escolhe a leitura que define a cor do semáforo, sem piscar perto das bordas.

    Regra assimétrica (princípio da precaução): PIORAR aparece na hora; MELHORAR
    só aparece quando as ``LEITURAS_CONFIRMACAO_MELHORA`` leituras mais
    recentes da MESMA origem, dentro de ``JANELA_CONFIRMACAO_SEGUNDOS`` contados
    da mais recente, forem todas melhores. Enquanto isso, vale a leitura mais
    recente com o pior status da janela (cor, mensagem e números coerentes).

    Args:
        leituras: leituras exibíveis, da mais recente para a mais antiga (não vazia).

    Returns:
        ``(leitura_exibida, em_confirmacao)``; ``em_confirmacao`` é ``True``
        quando a leitura mais recente já é melhor, mas a melhora ainda não foi
        confirmada.

    Raises:
        ValueError: lista vazia.
    """
    if not leituras:
        raise ValueError("É preciso ao menos uma leitura para aplicar a histerese.")
    mais_recente = leituras[0]
    instante_recente = _instante_leitura(mais_recente)
    janela: List[Dict[str, Any]] = [mais_recente]
    for leitura in leituras[1:LEITURAS_CONFIRMACAO_MELHORA]:
        if leitura.get("origem") != mais_recente.get("origem"):
            break
        instante = _instante_leitura(leitura)
        if instante is None or instante_recente is None:
            break
        distancia = (instante_recente - instante).total_seconds()
        # Fora da janela, ou "mais nova" que a mais recente (salto do relógio): não confirma nada.
        if distancia > JANELA_CONFIRMACAO_SEGUNDOS or distancia < -TOLERANCIA_FUTURO_SEGUNDOS:
            break
        janela.append(leitura)
    pior = max(int(leitura["status_alerta"]) for leitura in janela)
    if pior <= int(mais_recente["status_alerta"]):
        return mais_recente, False
    for leitura in janela:
        if int(leitura["status_alerta"]) == pior:
            return leitura, True
    return mais_recente, False


def _limite_idade_leitura(configuracoes: Dict[str, Any]) -> float:
    """Idade máxima (s) para a leitura ainda valer como atual: ``max(60, 6 × intervalo de coleta)``."""
    try:
        intervalo = float(configuracoes.get("intervalo_coleta_segundos") or INTERVALO_COLETA_PADRAO)
    except (TypeError, ValueError):
        intervalo = INTERVALO_COLETA_PADRAO
    if not intervalo > 0:
        intervalo = INTERVALO_COLETA_PADRAO
    return max(IDADE_MAXIMA_LEITURA_MINIMA_SEGUNDOS, CICLOS_ATE_LEITURA_ANTIGA * intervalo)


def _idade_segundos(leitura: Dict[str, Any]) -> Optional[float]:
    """Idade da leitura em segundos (``None`` se sem data válida; negativa se estiver no futuro).

    Usa ``instante_utc`` (com fuso, comparado a ``datetime.now(timezone.utc)``)
    quando a leitura o tiver; senão, ``data_hora`` em hora local. Assim a idade
    não erra na troca de horário nem quando o fuso do computador muda.
    """
    instante = _instante_leitura(leitura)
    if instante is None:
        return None
    return (datetime.now(timezone.utc) - instante).total_seconds()


def descrever_duracao(segundos: float) -> str:
    """Duração em palavras simples para ser lida em voz alta (ex.: "3 minutos", "2 horas", "3 dias")."""
    total = max(0, int(segundos))
    for tamanho, singular, plural in ((86400, "dia", "dias"), (3600, "hora", "horas"), (60, "minuto", "minutos")):
        if total >= tamanho:
            quantidade = total // tamanho
            return f"{quantidade} {singular if quantidade == 1 else plural}"
    return f"{total} {'segundo' if total == 1 else 'segundos'}"


def _mensagem_leitura_antiga(idade_segundos: float, status_ultima: int) -> str:
    """Aviso falado quando a última leitura é antiga demais para orientar o agricultor."""
    partes = [f"O aparelho não manda medições há {descrever_duracao(idade_segundos)}.",
              "Verifique se ele está ligado e com o cabo no lugar.",
              "A última cor pode não valer mais."]
    if status_ultima == 2:
        partes.append("A última medição mostrava perigo. Olhe a lavoura pessoalmente.")
    return " ".join(partes)


def _coletor_ativo_em_outra_cultura(codigo_cultura: str, configuracoes: Dict[str, Any], caminho: Path) -> bool:
    """Indica se há leitura recente (fora da demonstração) de OUTRA cultura: o coletor está vivo."""
    limite = _limite_idade_leitura(configuracoes)
    for leitura in buscar_historico(limite=12, caminho_banco=caminho):
        if leitura.get("origem") == MODO_DEMONSTRACAO or leitura.get("codigo_cultura") == codigo_cultura:
            continue
        idade = _idade_segundos(leitura)
        return idade is not None and idade <= limite
    return False


def _numero(valor: Any) -> Optional[float]:
    """Converte para float arredondado a 2 casas; ``None`` se ausente."""
    if valor is None:
        return None
    return round(float(valor), 2)


def _inteiro_ou_none(valor: Any) -> Optional[int]:
    """Converte para ``int``; ``None`` se ausente ou inválido."""
    if valor is None or isinstance(valor, bool) or str(valor).strip() == "":
        return None
    try:
        return int(float(str(valor)))
    except (TypeError, ValueError):
        return None


def _intervalo_coleta(configuracoes: Dict[str, Any]) -> float:
    """Intervalo de coleta configurado (s), com o padrão se ausente ou inválido."""
    try:
        intervalo = float(configuracoes.get("intervalo_coleta_segundos") or INTERVALO_COLETA_PADRAO)
    except (TypeError, ValueError):
        intervalo = INTERVALO_COLETA_PADRAO
    return intervalo if intervalo > 0 else INTERVALO_COLETA_PADRAO


def estado_coletor(configuracoes: Dict[str, Any]) -> Dict[str, Any]:
    """Saúde do coletor pelo batimento que ele grava em ``coletor_batimento_em``.

    ``vivo`` é ``None`` quando ainda não há batimento (banco novo ou coletor
    antigo, sem batimento: período de carência, sem alarme), ``True`` com
    batimento até ``max(60 s, 3 × intervalo de coleta)`` e ``False`` depois.
    """
    idade = _segundos_desde(str(configuracoes.get("coletor_batimento_em") or ""))
    limite = max(IDADE_MAXIMA_BATIMENTO_MINIMA_SEGUNDOS, CICLOS_ATE_COLETOR_PARADO * _intervalo_coleta(configuracoes))
    return {
        "vivo": None if idade is None else bool(idade <= limite),
        "idade_batimento_s": None if idade is None else round(idade, 1),
        "pid": _inteiro_ou_none(configuracoes.get("coletor_pid")),
        "ultimo_erro": str(configuracoes.get("coletor_ultimo_erro") or ""),
        "fila_pendente": _inteiro_ou_none(configuracoes.get("coletor_fila_pendente")) or 0,
    }


def estado_disco(caminho: Path, configuracoes: Dict[str, Any]) -> Dict[str, Any]:
    """Espaço livre na pasta do banco contra ``espaco_minimo_mb`` (padrão 200 MB).

    Se o espaço não puder ser medido, ``livre_mb`` é ``None`` e ``ok`` fica
    ``True`` (sem alarme falso).
    """
    minimo = _numero_ou_none(configuracoes.get("espaco_minimo_mb"))
    minimo_mb = minimo if minimo is not None and minimo >= 0 else ESPACO_MINIMO_DISCO_MB_PADRAO
    pasta = Path(caminho).resolve().parent
    try:
        livre_mb: Optional[float] = round(shutil.disk_usage(pasta).free / (1024 * 1024), 1)
    except OSError as erro:
        registrador.warning("Não foi possível medir o espaço livre em %s: %s", pasta, erro)
        livre_mb = None
    return {"livre_mb": livre_mb, "minimo_mb": minimo_mb, "ok": livre_mb is None or livre_mb >= minimo_mb}


def _relogio_inconsistente(idade_segundos: Optional[float]) -> bool:
    """Leitura mais de ``TOLERANCIA_FUTURO_SEGUNDOS`` "no futuro": relógio errado."""
    return idade_segundos is not None and idade_segundos < -TOLERANCIA_FUTURO_SEGUNDOS


def _ultima_leitura_real(caminho: Path) -> Optional[Dict[str, Any]]:
    """Leitura mais recente fora da demonstração (qualquer cultura); ``None`` se não houver."""
    for leitura in buscar_historico(limite=12, caminho_banco=caminho):
        if leitura.get("origem") != MODO_DEMONSTRACAO:
            return leitura
    return None


def informacao_troca_cultura(configuracoes: Dict[str, Any]) -> Dict[str, Any]:
    """Última troca de cultura: anterior, instante (ISO UTC) e segundos desde ela (``None`` se nunca houve)."""
    alterada_em = str(configuracoes.get("cultura_alterada_em") or "")
    segundos = _segundos_desde(alterada_em)
    return {
        "cultura_anterior": str(configuracoes.get("cultura_anterior") or "") or None,
        "cultura_alterada_em": alterada_em or None,
        "segundos_desde_troca_cultura": None if segundos is None else max(0, int(segundos)),
    }


def intervalo_repeticao_alerta(caminho: Path) -> int:
    """Segundos entre as repetições do alerta Vermelho na página (configurável; padrão 600)."""
    valor = obter_configuracao_tipada("intervalo_repeticao_alerta_segundos", INTERVALO_REPETICAO_ALERTA_PADRAO,
                                      caminho_banco=caminho)
    try:
        segundos = int(float(valor))
    except (TypeError, ValueError):
        return INTERVALO_REPETICAO_ALERTA_PADRAO
    return segundos if segundos > 0 else INTERVALO_REPETICAO_ALERTA_PADRAO


def _acoes_seguras(status: int, leitura: Dict[str, Any], codigo_cultura: str, caminho: Path,
                   motivo: str, estado_sensor: str, mensagem_voz: str = "") -> List[str]:
    """``derivar_acoes`` protegido: cultura sem faixa de temperatura/solo vira lista vazia."""
    return _acoes_e_causa_seguras(status, leitura, codigo_cultura, caminho, motivo, estado_sensor, mensagem_voz)[0]


def _acoes_e_causa_seguras(status: int, leitura: Dict[str, Any], codigo_cultura: str, caminho: Path,
                           motivo: str, estado_sensor: str, mensagem_voz: str = "") -> Tuple[List[str], str]:
    """Pictogramas de ação e causa da leitura; cultura sem faixa de temperatura/solo → ``([], NENHUMA)``.

    O ``motivo_regra`` da regra fisiológica (Frente 1) é lido com ``.get``: em
    bancos sem a coluna, vale ``""`` (comportamento da v2).
    """
    try:
        parametros = buscar_parametros_cultura(codigo_cultura, caminho_banco=caminho)
        acoes = derivar_acoes(status, leitura.get("temperatura_ar"), leitura.get("umidade_solo"), parametros,
                              motivo_antecipacao=motivo, estado_sensor=estado_sensor,
                              mensagem_voz=mensagem_voz, motivo_regra=str(leitura.get("motivo_regra") or ""))
        return acoes, derivar_causa(status, leitura, parametros, motivo, estado_sensor, mensagem_voz=mensagem_voz)
    except (KeyError, ValueError) as erro:
        registrador.warning("Sem pictogramas de ação para %s: %s", codigo_cultura, erro)
        return [], CAUSA_NENHUMA


def montar_fala_curta(nivel: int, acoes: Sequence[str], causa: str, situacao: str = SITUACAO_FALA_NORMAL) -> str:
    """Primeira fala do botão de voz: o sinal e o que fazer, antes de qualquer explicação.

    Delegada a ``acoes_orientacao.montar_fala_curta`` (Frente B). Um problema na
    montagem nunca derruba o status: cai na fala de "confira o aparelho" da
    própria Frente B (situação ``sem_leitura``), que não tem números.
    """
    try:
        return acoes_orientacao.montar_fala_curta(nivel, list(acoes), causa, situacao=situacao)
    except (KeyError, ValueError, TypeError) as erro:
        registrador.warning("Fala curta indisponível (%s); usando a de conferir o aparelho.", erro)
        return acoes_orientacao.montar_fala_curta(1, [], CAUSA_NENHUMA, situacao=SITUACAO_FALA_SEM_LEITURA)


def montar_status(caminho: Path) -> Dict[str, Any]:
    """Monta a resposta de ``/api/status`` (estado exibido no semáforo).

    Ordem de precedência do que é mostrado (da mais forte para a mais fraca):
    relógio errado → leitura antiga (ou coletor parado) → dados de simulação →
    defeito de sensor → orientação normal (com a antecipação, quando houver).
    """
    configuracoes = _encerrar_demonstracao_vencida(caminho, obter_todas_configuracoes(caminho_banco=caminho))
    culturas = _culturas_ativas(caminho)
    cultura = _resolver_cultura_ativa(caminho, culturas)
    codigo = str(cultura["codigo"])
    nome_comum = str(cultura["nome_comum"])
    categoria = str(cultura["categoria"])
    modo = str(configuracoes.get("modo_operacao") or MODO_SIMULACAO)
    demonstracao_ativa = modo == MODO_DEMONSTRACAO
    cenario = str(configuracoes.get("cenario_demonstracao") or "") if demonstracao_ativa else ""
    leituras = _leituras_recentes_exibiveis(
        codigo, demonstracao_ativa, caminho, quantidade=1 if demonstracao_ativa else LEITURAS_CONFIRMACAO_MELHORA
    )
    leitura: Optional[Dict[str, Any]] = None
    em_confirmacao = False
    if leituras:
        # Na banca o cenário escolhido aparece na hora; fora dela vale a histerese.
        leitura, em_confirmacao = (leituras[0], False) if demonstracao_ativa else aplicar_histerese(leituras)
    data_hora_mais_recente = str(leituras[0]["data_hora"]) if leituras else ""
    idade_leitura = _idade_segundos(leituras[0]) if leituras else None
    relogio_inconsistente = bool(leitura is not None and not demonstracao_ativa
                                 and _relogio_inconsistente(idade_leitura))
    leitura_desatualizada = bool(
        leitura is not None and not demonstracao_ativa and idade_leitura is not None
        and (idade_leitura > _limite_idade_leitura(configuracoes) or relogio_inconsistente)
    )
    dados_simulados = bool(leitura is not None and not leitura_desatualizada
                           and str(leitura.get("origem")) == MODO_SIMULACAO)
    coletor = estado_coletor(configuracoes)
    estado_sensor = str((leitura or {}).get("estado_sensor") or ESTADO_SENSOR_OK).strip().upper()
    antecipacao: Optional[Dict[str, Any]] = None
    acoes: List[str]
    causa: str = CAUSA_APARELHO

    if leitura is None:
        status = STATUS_AMARELO
        trocando = _coletor_ativo_em_outra_cultura(codigo, configuracoes, caminho)
        titulo = TITULO_TROCANDO_CULTURA if trocando else TITULO_SEM_LEITURA
        # Trocando de plantação não há o que conferir: só esperar alguns segundos.
        acoes = [] if trocando else list(ACOES_VERIFICAR_APARELHO)
        causa = CAUSA_NENHUMA if trocando else CAUSA_APARELHO
        dados_leitura: Dict[str, Any] = {
            "mensagem_voz": (MENSAGEM_TROCANDO_CULTURA.format(nome=nome_comum.lower()) if trocando
                             else MENSAGEM_SEM_LEITURA),
            "temperatura_ar": None,
            "umidade_ar": None,
            "umidade_solo": None,
            "deficit_hidrico_solo": None,
            "desvio_termico": None,
            "data_hora": "",
            "origem": "",
            "flag_anomalia": 0,
            "descricao_anomalia": "",
            "versao_modelo": str(configuracoes.get("versao_modelo") or "REGRAS"),
            "id_leitura": None,
        }
    else:
        status_leitura = int(leitura["status_alerta"])
        if relogio_inconsistente:
            status = STATUS_AMARELO
            titulo = TITULO_RELOGIO_ERRADO
            mensagem = MENSAGEM_RELOGIO_ERRADO
            acoes = list(ACOES_VERIFICAR_APARELHO)
        elif leitura_desatualizada:
            status = STATUS_AMARELO
            if coletor["vivo"] is False:
                titulo = TITULO_COLETOR_PARADO
                mensagem = MENSAGEM_COLETOR_PARADO
                if status_leitura == 2:
                    mensagem += " A última medição mostrava perigo. Olhe a lavoura pessoalmente."
            else:
                titulo = TITULO_LEITURA_ANTIGA
                mensagem = _mensagem_leitura_antiga(float(idade_leitura or 0.0), status_leitura)
            acoes = list(ACOES_VERIFICAR_APARELHO)
        elif dados_simulados:
            status = status_leitura
            titulo = TITULO_SIMULACAO
            mensagem = MENSAGEM_SIMULACAO
            acoes = list(ACOES_VERIFICAR_APARELHO)
        elif estado_sensor != ESTADO_SENSOR_OK:
            status = status_leitura
            titulo = TITULO_CONFIRA_APARELHO
            mensagem = str(leitura["mensagem_voz"])
            acoes = list(ACOES_VERIFICAR_APARELHO)
        else:
            status = status_leitura
            titulo = montar_titulo(status, nome_comum, categoria)
            mensagem = str(leitura["mensagem_voz"])
            if str(leitura.get("origem")) == MODO_CAMPO_USB:
                antecipacao = obter_antecipacao(int(leitura["id_leitura"]), caminho)
            motivo = antecipacao["motivo_principal"] if antecipacao else MOTIVO_NENHUM
            acoes, causa = _acoes_e_causa_seguras(status, leitura, codigo, caminho, motivo, estado_sensor, mensagem)
        dados_leitura = {
            "mensagem_voz": mensagem,
            "temperatura_ar": _numero(leitura["temperatura_ar"]),
            "umidade_ar": _numero(leitura["umidade_ar"]),
            "umidade_solo": _numero(leitura["umidade_solo"]),
            "deficit_hidrico_solo": _numero(leitura["deficit_hidrico_solo"]),
            "desvio_termico": _numero(leitura["desvio_termico"]),
            "data_hora": str(leitura["data_hora"]),
            "origem": str(leitura["origem"]),
            "flag_anomalia": int(leitura["flag_anomalia"]),
            "descricao_anomalia": str(leitura["descricao_anomalia"] or ""),
            "versao_modelo": str(leitura["versao_modelo"]),
            "id_leitura": int(leitura["id_leitura"]),
        }

    resposta: Dict[str, Any] = {
        "tem_leitura": leitura is not None,
        "status_alerta": status,
        "cor": CORES_STATUS[status],
        "titulo": titulo,
        "mensagem_voz": dados_leitura["mensagem_voz"],
        # v4: sinal + ação em até 15 palavras (1º toque no botão de voz); a mensagem longa vem no 2º toque.
        "fala_curta": montar_fala_curta(
            status, acoes, causa,
            situacao=(SITUACAO_FALA_SEM_LEITURA if leitura is None
                      else SITUACAO_FALA_ANTIGA if (leitura_desatualizada or relogio_inconsistente)
                      else SITUACAO_FALA_TESTE if dados_simulados else SITUACAO_FALA_NORMAL),
        ),
        "nome_cultura": nome_comum,
        "codigo_cultura": codigo,
        "temperatura_ar": dados_leitura["temperatura_ar"],
        "umidade_ar": dados_leitura["umidade_ar"],
        "umidade_solo": dados_leitura["umidade_solo"],
        "deficit_hidrico_solo": dados_leitura["deficit_hidrico_solo"],
        "desvio_termico": dados_leitura["desvio_termico"],
        "data_hora": dados_leitura["data_hora"],
        "origem": dados_leitura["origem"],
        "modo_operacao": modo,
        "estado_hardware": str(configuracoes.get("estado_hardware") or ESTADO_HARDWARE_DESCONECTADO),
        "flag_anomalia": dados_leitura["flag_anomalia"],
        "versao_modelo": dados_leitura["versao_modelo"],
        "cenario_demonstracao": cenario,
        "nome_propriedade": str(configuracoes.get("nome_propriedade") or ""),
        # Campos complementares (não exigidos pelo contrato, úteis à interface):
        "nome_status": NOMES_STATUS[status],
        "categoria_cultura": categoria,
        "rotulo_modo": ROTULOS_MODO.get(modo, modo),
        "descricao_anomalia": dados_leitura["descricao_anomalia"],
        "demonstracao_ativa": demonstracao_ativa,
        "rotulo_cenario": CENARIOS_DEMONSTRACAO.get(cenario, {}).get("rotulo", "") if cenario else "",
        "segundos_restantes_demonstracao": _segundos_restantes_demonstracao(configuracoes),
        "id_leitura": dados_leitura["id_leitura"],
        "leitura_desatualizada": leitura_desatualizada,
        "idade_leitura_segundos": (int(idade_leitura) if idade_leitura is not None and idade_leitura >= 0 else None),
        "dados_simulados": dados_simulados,
        "orientacao_simulada": str(leitura["mensagem_voz"]) if (dados_simulados and leitura is not None) else "",
        "status_em_confirmacao": em_confirmacao,
        "data_hora_ultima_leitura": data_hora_mais_recente,
        # Interface de campo: pictogramas, antecipação, sensor, relógio, coletor e troca de cultura.
        "acoes": acoes,
        "acoes_detalhe": [
            {"codigo": acao, "legenda": LEGENDAS_ACAO.get(acao, ""), "descricao": DESCRICOES_ACAO.get(acao, "")}
            for acao in acoes
        ],
        "motivo_principal": antecipacao["motivo_principal"] if antecipacao else MOTIVO_NENHUM,
        "prazo_horas": antecipacao["prazo_horas"] if antecipacao else None,
        "horas_em_estresse": antecipacao["horas_em_estresse"] if antecipacao else None,
        "antecipacao": antecipacao,
        "estado_sensor": estado_sensor,
        "relogio_inconsistente": relogio_inconsistente,
        "coletor_vivo": coletor["vivo"],
        "intervalo_repeticao_alerta_segundos": intervalo_repeticao_alerta(caminho),
        # Retorno do agricultor (v3): causa visível, falada ao toque, e silêncio depois de "Feito".
        "causa": causa,
        "legenda_causa": LEGENDAS_CAUSA.get(causa, ""),
        "causa_descricao": DESCRICOES_CAUSA.get(causa, ""),
        "causa_silenciada": bool(
            status >= STATUS_AMARELO and causa != CAUSA_NENHUMA
            and retorno_agricultor.causa_silenciada(caminho, causa, _agora_utc(),
                                                    incluir_demonstracao=demonstracao_ativa)
        ),
    }
    resposta.update(informacao_troca_cultura(configuracoes))
    return resposta


def _aclimatar_saneador(saneador: SaneadorTempoReal, leitura: Dict[str, float]) -> None:
    """Apresenta o valor do cenário ao saneador até ele aceitá-lo como novo patamar.

    O cenário de banca é uma mudança DELIBERADA e grande (ex.: solo de 70 % para
    30 %). Sem isso, a regra de salto do saneador trocaria o valor pelo último
    válido (ponto médio da faixa ideal) e o semáforo não mostraria o cenário.
    É o mesmo comportamento que o saneador tem diante de uma mudança real e
    persistente, apenas antecipado.
    """
    for _ in range(TENTATIVAS_ACLIMATACAO_SANEADOR):
        _, flag_anomalia, _ = saneador.sanear(leitura["temperatura_ar"], leitura["umidade_ar"], leitura["umidade_solo"])
        if flag_anomalia == 0:
            return
    registrador.warning("O saneador ainda marca o cenário como anomalia após %d tentativas.", TENTATIVAS_ACLIMATACAO_SANEADOR)


def injetar_leitura_cenario(cenario: str, codigo_cultura: str, caminho: Path) -> Tuple[int, RegistroLeitura]:
    """Gera a leitura do cenário para a cultura e a grava IMEDIATAMENTE pelo pipeline completo.

    Raises:
        ValueError: cenário desconhecido ou impossível para as faixas da cultura.
    """
    parametros = buscar_parametros_cultura(codigo_cultura, caminho_banco=caminho)
    leitura = gerar_leitura_cenario(cenario, parametros)
    saneador = SaneadorTempoReal(parametros)
    _aclimatar_saneador(saneador, leitura)
    id_leitura, registro = processar_leitura(
        leitura["temperatura_ar"],
        leitura["umidade_ar"],
        leitura["umidade_solo"],
        MODO_DEMONSTRACAO,
        codigo_cultura,
        saneador,
        obter_pacote_modelo(),
        caminho_banco=caminho,
    )
    registrador.info(
        "Cenário %s injetado para %s (leitura %d, status %d).", cenario, codigo_cultura, id_leitura, registro.status_alerta
    )
    return id_leitura, registro


def reclassificar_medicao_de_campo(codigo_cultura: str, caminho: Path) -> Optional[Tuple[int, RegistroLeitura]]:
    """Reaproveita a medição de CAMPO mais recente para a cultura recém-escolhida.

    O sensor é o mesmo: o ar e a terra medidos agora valem para qualquer
    plantação; só a classificação muda (faixas da nova cultura). Assim, ao
    trocar de cultura, o semáforo mostra na hora a situação real, sem passar
    pelo aviso "Esperando o sensor" até a próxima coleta.

    Só age quando a medição de origem ``CAMPO_USB`` é recente (dentro do
    limite de idade) e a cultura escolhida ainda não tem leitura igualmente
    recente. Leituras de simulação não são reaproveitadas (são de teste).

    Returns:
        ``(id_leitura, registro)`` da leitura gravada, ou ``None`` se nada foi feito.

    Raises:
        ValueError: cultura inexistente.
    """
    configuracoes = obter_todas_configuracoes(caminho_banco=caminho)
    limite = _limite_idade_leitura(configuracoes)
    fonte: Optional[Dict[str, Any]] = None
    for leitura in buscar_historico(limite=24, caminho_banco=caminho):
        if leitura.get("origem") == MODO_DEMONSTRACAO:
            continue
        if leitura.get("codigo_cultura") == codigo_cultura:
            return None  # a cultura escolhida já tem a leitura mais recente
        fonte = leitura
        break
    if fonte is None or fonte.get("origem") != MODO_CAMPO_USB:
        return None
    idade = _idade_segundos(fonte)
    if idade is None or idade > limite:
        return None
    parametros = buscar_parametros_cultura(codigo_cultura, caminho_banco=caminho)
    # v3: no CAMPO_USB o pipeline aplica a calibração da sonda à umidade recebida. A
    # medição reaproveitada precisa entrar com o valor do FIRMWARE (``umidade_solo_firmware``),
    # senão a calibração seria aplicada duas vezes (ex.: +5 pp virariam +10 pp).
    solo_firmware = fonte.get("umidade_solo_firmware")
    solo_entrada = float(solo_firmware) if solo_firmware is not None else float(fonte["umidade_solo"])
    resultado = processar_leitura(
        float(fonte["temperatura_ar"]),
        float(fonte["umidade_ar"]),
        solo_entrada,
        MODO_CAMPO_USB,
        codigo_cultura,
        SaneadorTempoReal(parametros),
        obter_pacote_modelo(),
        caminho_banco=caminho,
    )
    registrador.info("Medição de campo %s reclassificada para %s (leitura %d).", fonte.get("id_leitura"),
                     codigo_cultura, resultado[0])
    return resultado


def _corpo_json() -> Dict[str, Any]:
    """Lê o corpo JSON da requisição como dicionário.

    Raises:
        ErroApi: corpo ausente, inválido ou que não seja um objeto JSON.
    """
    corpo = request.get_json(silent=True)
    if not isinstance(corpo, dict):
        raise ErroApi("Envie os dados no formato JSON.", 400)
    return corpo


def _texto_obrigatorio(corpo: Dict[str, Any], campo: str, mensagem: str) -> str:
    """Extrai um campo de texto não vazio do corpo JSON.

    Raises:
        ErroApi: campo ausente, vazio ou que não seja texto.
    """
    valor = corpo.get(campo)
    if not isinstance(valor, str) or not valor.strip():
        raise ErroApi(mensagem, 400)
    return valor.strip()


def _listar_cenarios() -> List[Dict[str, Any]]:
    """Cenários do Modo Demonstração prontos para a interface."""
    return [
        {
            "codigo": codigo,
            "rotulo": dados["rotulo"],
            "descricao": dados["descricao"],
            "status_esperado": int(dados["status_esperado"]),
            "cor": CORES_STATUS[int(dados["status_esperado"])],
            "icone": ICONES_CENARIO.get(codigo, "#i-atencao"),
        }
        for codigo, dados in CENARIOS_DEMONSTRACAO.items()
    ]


def _ip_cliente() -> str:
    """Endereço do cliente (``request.remote_addr``), para o log e o limitador de tentativas."""
    return str(request.remote_addr or "desconhecido")


def _verificar_pin_banca() -> None:
    """Exige o PIN da banca no cabeçalho ``X-PIN-Banca`` quando ``pin_banca_hash`` estiver definido.

    Sem PIN configurado (string vazia), nada muda. Com PIN: cabeçalho ausente
    → 403 ``{"codigo": "pin_necessario"}``; PIN errado → 403
    ``{"erro": "Senha errada. Tente de novo.", "codigo": "pin_errado"}``;
    depois de 5 erros do mesmo IP → 429 por um minuto, mesmo com o PIN certo,
    com ``segundos_restantes`` (inteiro de 1 a 60) para a contagem na tela.

    Raises:
        ErroApi: 403 (PIN ausente/errado) ou 429 (muitas tentativas).
    """
    hash_pin = str(obter_configuracao(CHAVE_CONFIGURACAO_PIN, "", caminho_banco=_caminho_banco()) or "")
    if not hash_pin:
        return
    ip = _ip_cliente()
    if _LIMITADOR_PIN.bloqueado(ip):
        registrador.warning("Banca: pedido de %s recusado (bloqueado por excesso de tentativas).", ip)
        raise ErroApi(MENSAGEM_MUITAS_TENTATIVAS, 429, {"segundos_restantes": segundos_bloqueio_pin(ip)})
    digitado = request.headers.get(CABECALHO_PIN_BANCA, "")
    if verificar_pin(digitado, hash_pin):
        _LIMITADOR_PIN.limpar(ip)
        return
    _LIMITADOR_PIN.registrar_erro(ip)
    registrador.warning("Banca: PIN ausente ou errado vindo de %s em %s.", ip, request.path)
    if digitado.strip():
        raise ErroApi(MENSAGEM_PIN_ERRADO, 403, {"codigo": CODIGO_PIN_ERRADO})
    raise ErroApi(MENSAGEM_PIN_NECESSARIO, 403, {"codigo": CODIGO_PIN_NECESSARIO})


def segundos_bloqueio_pin(ip: str) -> int:
    """Segundos inteiros que faltam para o IP poder tentar o PIN de novo (0 fora do bloqueio)."""
    return int(_LIMITADOR_PIN.segundos_restantes(ip))


def _exigir_pin_banca(funcao: _Funcao) -> _Funcao:
    """Decorator das rotas que mudam o que TODOS veem (banca e troca de cultura): exige o PIN se houver."""
    @wraps(funcao)
    def envolvida(*argumentos: Any, **nomeados: Any) -> Any:
        """Confere o PIN antes de executar a rota."""
        _verificar_pin_banca()
        return funcao(*argumentos, **nomeados)
    return cast(_Funcao, envolvida)


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------
@app.before_request
def recusar_corpo_grande() -> None:
    """Recusa (413) corpos maiores que ``TAMANHO_MAXIMO_CORPO_BYTES`` antes de qualquer outra verificação."""
    tamanho = request.content_length
    if tamanho is not None and tamanho > TAMANHO_MAXIMO_CORPO_BYTES:
        abort(413)


def registrar_aparelho_banca(ip: str) -> None:
    """Guarda o endereço do aparelho que ligou a demonstração (``""`` esquece)."""
    with _TRAVA_BANCA:
        _APARELHO_BANCA["ip"] = str(ip or "")


def aparelho_da_banca(status: Dict[str, Any], ip: str) -> bool:
    """``True`` se a demonstração em andamento foi ligada por este aparelho (mesmo endereço na rede).

    Nos outros celulares a página mostra a demonstração como NEUTRA (sem cor
    da lavoura, sem alarme): a apresentação da banca não pode parecer perigo
    real. Demonstração ligada direto no banco (sem a rota) não tem aparelho.
    """
    if not status.get("demonstracao_ativa"):
        return False
    with _TRAVA_BANCA:
        guardado = _APARELHO_BANCA["ip"]
    return bool(guardado) and guardado == str(ip or "")


def status_para_aparelho(caminho: Path) -> Dict[str, Any]:
    """``montar_status`` mais ``aparelho_da_banca`` para o aparelho que fez o pedido."""
    status = montar_status(caminho)
    status["aparelho_da_banca"] = aparelho_da_banca(status, _ip_cliente())
    return status


@app.get("/")
def pagina_inicial() -> Response:
    """Página única do Companheiro Virtual (semáforo + voz), já com o estado atual embutido.

    O estado vai num ``<script type="application/json" id="estado-inicial">``:
    em rede 2G a cor aparece sem esperar a primeira consulta à API. Se o banco
    falhar, a página sai mesmo assim (``estado_inicial`` = ``None``). O CSS
    não crítico vem de ``URL_CSS_AGRICULTOR`` (cache imutável).
    """
    try:
        estado_inicial: Optional[Dict[str, Any]] = montar_status(_caminho_banco())
    except Exception as erro:
        registrador.warning("Estado inicial indisponível para a página: %s", erro)
        estado_inicial = None
    if estado_inicial is not None:
        # Só o que a página usa na primeira pintura (a consulta seguinte a /api/status traz o resto);
        # campo nulo não viaja (para a página, ausente e nulo dão no mesmo).
        estado_inicial = {chave: valor for chave, valor in estado_inicial.items()
                          if chave not in CAMPOS_FORA_DA_PAGINA and valor is not None}
        estado_inicial["aparelho_da_banca"] = aparelho_da_banca(estado_inicial, _ip_cliente())
    resposta = Response(render_template("index.html", estado_inicial=estado_inicial,
                                        url_css_agricultor=URL_CSS_AGRICULTOR), mimetype="text/html")
    resposta.headers["Cache-Control"] = "no-cache"
    return resposta


@app.get("/static/agro.<codigo>.css")
def rota_css_agricultor(codigo: str) -> Response:
    """CSS não crítico da página do agricultor, com cache imutável de um ano.

    Só o hash atual existe: outro nome responde 404 (uma página antiga em
    cache pede a sua versão, recebe 404 e continua só com o CSS crítico até
    recarregar).
    """
    if codigo != _HASH_CSS_AGRICULTOR:
        abort(404)
    resposta = Response(_CSS_AGRICULTOR, mimetype="text/css")
    resposta.headers["Cache-Control"] = CACHE_IMUTAVEL
    return resposta


@app.get("/favicon.svg")
def rota_favicon() -> Response:
    """Ícone da aba (semáforo em pé), guardado pelo navegador: sai do HTML para a página caber no orçamento."""
    resposta = Response(FAVICON_SVG, mimetype="image/svg+xml")
    resposta.headers["Cache-Control"] = CACHE_ICONE
    return resposta


@app.get("/api/status")
def rota_status() -> Response:
    """Estado atual exibido no semáforo (com ``aparelho_da_banca`` para quem pediu)."""
    return jsonify(status_para_aparelho(_caminho_banco()))


@app.get("/api/historico")
def rota_historico() -> Response:
    """Últimas leituras, da mais recente para a mais antiga (``limite`` de 1 a 500; ``cultura`` opcional)."""
    caminho = _caminho_banco()
    texto_limite = request.args.get("limite", str(LIMITE_HISTORICO_PADRAO)).strip()
    try:
        limite = int(texto_limite)
    except ValueError:
        raise ErroApi(
            f"O limite precisa ser um número inteiro de {LIMITE_HISTORICO_MINIMO} a {LIMITE_HISTORICO_MAXIMO}.", 400
        ) from None
    if not LIMITE_HISTORICO_MINIMO <= limite <= LIMITE_HISTORICO_MAXIMO:
        raise ErroApi(f"O limite precisa ficar entre {LIMITE_HISTORICO_MINIMO} e {LIMITE_HISTORICO_MAXIMO}.", 400)
    codigo_cultura = request.args.get("cultura")
    if codigo_cultura is not None:
        codigo_cultura = codigo_cultura.strip()
        if _localizar_cultura(codigo_cultura, listar_culturas(apenas_ativas=False, caminho_banco=caminho)) is None:
            raise ErroApi("Cultura não encontrada.", 404)
    leituras = buscar_historico(limite=limite, codigo_cultura=codigo_cultura, caminho_banco=caminho)
    for leitura in leituras:
        leitura["cor"] = CORES_STATUS.get(int(leitura["status_alerta"]), "amarelo")
    return jsonify({"leituras": leituras})


def icone_cultura(codigo: str, categoria: str, culturas: List[Dict[str, Any]]) -> str:
    """Símbolo SVG da cultura: pelo código; senão pela categoria, quando só ela usa a categoria.

    Duas culturas da mesma categoria sem símbolo próprio caem no padrão com o
    código como fragmento (``#i-broto:tomate``): o mapa continua injetivo e a
    página desenha o broto.
    """
    if codigo in ICONES_CULTURA_CODIGO:
        return ICONES_CULTURA_CODIGO[codigo]
    mesma_categoria = [c for c in culturas if str(c["categoria"]) == categoria and str(c["codigo"]) not in ICONES_CULTURA_CODIGO]
    if categoria in ICONES_CATEGORIA_SVG and len(mesma_categoria) <= 1:
        return ICONES_CATEGORIA_SVG[categoria]
    return f"{ICONE_CULTURA_PADRAO}:{codigo}"


@app.get("/api/culturas")
def rota_culturas() -> Response:
    """Culturas ativas (botões de troca) e a cultura monitorada."""
    caminho = _caminho_banco()
    culturas = _culturas_ativas(caminho)
    ativa = _resolver_cultura_ativa(caminho, culturas)
    resposta = [
        {
            "codigo": str(cultura["codigo"]),
            "nome_comum": str(cultura["nome_comum"]),
            "nome_cientifico": str(cultura["nome_cientifico"] or ""),
            "categoria": str(cultura["categoria"]),
            "descricao": str(cultura["descricao"] or ""),
            "rotulo_curto": _rotulo_curto_cultura(str(cultura["nome_comum"]), str(cultura["categoria"])),
            # Símbolo SVG distinto por cultura (Arábica ≠ Conilon); "emoji" mantém o ícone antigo da categoria.
            "icone": icone_cultura(str(cultura["codigo"]), str(cultura["categoria"]), culturas),
            "emoji": ICONES_CATEGORIA.get(str(cultura["categoria"]), "🌱"),
        }
        for cultura in culturas
    ]
    saida = jsonify({"culturas": resposta, "cultura_ativa": str(ativa["codigo"])})
    # Integração v4 (meta 12, 2ª visita ≤ 15.500 B): a lista quase nunca muda; o navegador guarda e
    # sempre confere (ETag + no-cache): sem mudança, a resposta é um 304 sem corpo.
    saida.add_etag(weak=True)
    saida.headers["Cache-Control"] = "no-cache"
    return saida.make_conditional(request)


@app.post("/api/cultura")
@_exigir_pin_banca
def rota_trocar_cultura() -> Response:
    """Troca a cultura monitorada (``{"codigo": "cafe_conilon"}``).

    Grava também ``cultura_anterior`` e ``cultura_alterada_em`` (ISO UTC): os
    outros celulares avisam "A plantação foi trocada para ...".
    """
    caminho = _caminho_banco()
    corpo = _corpo_json()
    codigo = _texto_obrigatorio(corpo, "codigo", "Informe o código da cultura, por exemplo \"cafe_conilon\".")
    culturas = _culturas_ativas(caminho)
    cultura = _localizar_cultura(codigo, culturas)
    if cultura is None:
        raise ErroApi("Cultura não encontrada.", 404)
    anterior = str(_resolver_cultura_ativa(caminho, culturas)["codigo"])
    definir_configuracao("cultura_ativa", codigo, caminho_banco=caminho)
    if anterior != codigo:
        definir_configuracao("cultura_anterior", anterior, caminho_banco=caminho)
        definir_configuracao("cultura_alterada_em", datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             caminho_banco=caminho)
    registrador.info("Cultura monitorada alterada de %s para %s por %s.", anterior, codigo, _ip_cliente())

    # Com a banca em andamento, o cenário é refeito na hora para a nova cultura.
    configuracoes = obter_todas_configuracoes(caminho_banco=caminho)
    cenario = str(configuracoes.get("cenario_demonstracao") or "")
    if configuracoes.get("modo_operacao") == MODO_DEMONSTRACAO and _segundos_restantes_demonstracao(configuracoes) > 0 and cenario:
        try:
            injetar_leitura_cenario(cenario, codigo, caminho)
        except ValueError as erro:
            registrador.warning("Cenário %s impossível para %s (%s); demonstração encerrada.", cenario, codigo, erro)
            encerrar_demonstracao(caminho)
    else:
        # Fora da banca: a medição de campo recente é reclassificada na hora para a nova cultura.
        try:
            reclassificar_medicao_de_campo(codigo, caminho)
        except (ValueError, sqlite3.Error) as erro:
            registrador.warning("Não foi possível reclassificar a medição para %s: %s", codigo, erro)
    return jsonify({
        "ok": True,
        "cultura_ativa": codigo,
        "nome_cultura": str(cultura["nome_comum"]),
        "status": status_para_aparelho(caminho),
    })


@app.get("/api/cenarios")
def rota_cenarios() -> Response:
    """Cenários extremos disponíveis para a demonstração ao vivo."""
    caminho = _caminho_banco()
    configuracoes = _encerrar_demonstracao_vencida(caminho, obter_todas_configuracoes(caminho_banco=caminho))
    ativo = str(configuracoes.get("cenario_demonstracao") or "") if configuracoes.get("modo_operacao") == MODO_DEMONSTRACAO else ""
    return jsonify({
        "cenarios": _listar_cenarios(),
        "cenario_ativo": ativo,
        "duracao_segundos": int(configuracoes.get("duracao_demonstracao_segundos") or DURACAO_DEMONSTRACAO_PADRAO),
        "banca": _estado_banca(caminho),
    })


@app.post("/api/demonstracao")
@_exigir_pin_banca
def rota_iniciar_demonstracao() -> Response:
    """Ativa um cenário extremo (``{"cenario": "SECA_EXTREMA"}``) e grava a leitura na hora."""
    caminho = _caminho_banco()
    corpo = _corpo_json()
    cenario = _texto_obrigatorio(corpo, "cenario", "Informe o cenário, por exemplo \"SECA_EXTREMA\".").upper()
    if cenario not in CENARIOS_DEMONSTRACAO:
        raise ErroApi("Cenário não encontrado.", 404)
    cultura = _resolver_cultura_ativa(caminho)
    codigo = str(cultura["codigo"])
    parametros = buscar_parametros_cultura(codigo, caminho_banco=caminho)
    try:
        gerar_leitura_cenario(cenario, parametros)
    except ValueError as erro:
        registrador.warning("Cenário %s impossível para %s: %s", cenario, codigo, erro)
        raise ErroApi("Este cenário não pode ser simulado para a cultura escolhida.", 400) from None

    duracao = obter_configuracao_inteira("duracao_demonstracao_segundos", DURACAO_DEMONSTRACAO_PADRAO, caminho)
    # Em UTC com fuso: não depende do fuso do computador nem da troca de horário.
    expira_em = (datetime.now(timezone.utc) + timedelta(seconds=duracao)).isoformat(timespec="seconds")
    # Ordem importa: a expiração é gravada PRIMEIRO. Assim, se o coletor estiver
    # encerrando a demonstração anterior (vencida) neste instante, o encerramento
    # condicional dele percebe a mudança e não apaga a nova demonstração.
    definir_configuracao("demonstracao_expira_em", expira_em, caminho_banco=caminho)
    definir_configuracao("cenario_demonstracao", cenario, caminho_banco=caminho)
    definir_configuracao("modo_operacao", MODO_DEMONSTRACAO, caminho_banco=caminho)
    try:
        id_leitura, registro = injetar_leitura_cenario(cenario, codigo, caminho)
    except ValueError as erro:
        encerrar_demonstracao(caminho)
        registrador.error("Falha ao gravar a leitura do cenário %s: %s", cenario, erro)
        raise ErroApi("Não foi possível simular este cenário agora.", 400) from None
    except Exception:
        encerrar_demonstracao(caminho)
        raise
    registrador.info("Modo Demonstração: %s até %s (pedido de %s).", cenario, expira_em, _ip_cliente())
    registrar_aparelho_banca(_ip_cliente())
    status = status_para_aparelho(caminho)
    status["id_leitura_injetada"] = id_leitura
    status["status_regra"] = registro.status_regra
    status["status_predito"] = registro.status_predito
    status["status_esperado"] = int(CENARIOS_DEMONSTRACAO[cenario]["status_esperado"])
    status["demonstracao_expira_em"] = expira_em
    return jsonify(status)


@app.post("/api/demonstracao/encerrar")
@_exigir_pin_banca
def rota_encerrar_demonstracao() -> Response:
    """Sai do Modo Demonstração e volta ao Campo USB (Arduino conectado) ou à Simulação."""
    caminho = _caminho_banco()
    modo = encerrar_demonstracao(caminho)
    registrar_aparelho_banca("")
    registrador.info("Demonstração encerrada a pedido de %s.", _ip_cliente())
    return jsonify({"ok": True, "modo_operacao": modo, "status": montar_status(caminho)})


# ---------------------------------------------------------------------------
# Retorno do agricultor: "Feito", pedido de ajuda e eventos da tela (sem PIN)
# ---------------------------------------------------------------------------
def _limitar_corpo(limite_bytes: int) -> None:
    """Recusa (413) corpos maiores que ``limite_bytes`` nesta rota.

    Raises:
        werkzeug.exceptions.RequestEntityTooLarge: corpo grande demais.
    """
    tamanho = request.content_length
    if tamanho is None:
        tamanho = len(request.get_data(cache=True))
    if tamanho > limite_bytes:
        abort(413)


def _limitar_pedidos(limitador: LimitadorJanela) -> None:
    """Aplica o limite de pedidos por IP da rota.

    Raises:
        ErroApi: 429 quando o IP passou do limite na janela.
    """
    if not limitador.permitir(_ip_cliente()):
        registrador.warning("Pedidos demais de %s em %s.", _ip_cliente(), request.path)
        raise ErroApi(MENSAGEM_MUITOS_PEDIDOS, 429)


def _corpo_json_opcional() -> Dict[str, Any]:
    """Corpo JSON como dicionário; corpo vazio vira ``{}``.

    Raises:
        ErroApi: corpo presente, mas que não é um objeto JSON.
    """
    if not request.get_data(cache=True):
        return {}
    return _corpo_json()


def _id_leitura_do_corpo(corpo: Dict[str, Any]) -> Optional[int]:
    """``id_leitura`` opcional do corpo (inteiro não negativo ou ``null``).

    Raises:
        ErroApi: valor que não é inteiro.
    """
    valor = corpo.get("id_leitura")
    if valor is None:
        return None
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or valor != valor or valor in (float("inf"), float("-inf")) \
            or int(valor) != valor or not 0 <= valor <= MAIOR_ID_SQLITE:
        raise ErroApi("Leitura inválida.", 400)
    return int(valor)


def _leitura_da_demonstracao(caminho: Path) -> Optional[int]:
    """Leitura da banca exibida agora, se uma demonstração está em andamento (senão ``None``).

    Integração v4: um "Feito" sem ``id_leitura`` tocado durante a demonstração
    ficava sem ligação com a leitura da banca e por isso contava como ação real:
    entrava no resumo do técnico e calava por 60 min o alerta real da mesma
    causa. Ligado à leitura da banca, ele fica de fora dos dois, como os toques
    que já trazem a leitura.
    """
    try:
        status = montar_status(caminho)
    except (ErroApi, sqlite3.Error) as erro:
        registrador.warning("Feito sem leitura: estado atual indisponível (%s).", erro)
        return None
    if status.get("demonstracao_ativa") and status.get("id_leitura") is not None:
        return int(status["id_leitura"])
    return None


def registrar_acao_sem_repetir(caminho: Path, codigo_acao: str, causa: str, id_leitura: Optional[int],
                               origem: str, instante: datetime) -> Tuple[int, bool]:
    """Grava o "Feito" sem duplicar toques repetidos: ``(id_acao, repetido)``.

    Com ``id_leitura`` (a página sempre manda a leitura que estava na tela), usa
    ``retorno_agricultor.registrar_acao_sem_repetir``: o mesmo toque (ação,
    causa e leitura) a menos de ``JANELA_REPETICAO_SEGUNDOS`` devolve o registro
    existente. Sem leitura não há como saber se é o mesmo alerta (ex.: toque
    guardado sem rede e reenviado, ou cliente antigo): grava sempre, como na v3.

    Raises:
        ValueError: ação ou causa fora do catálogo.
        sqlite3.Error: falha do banco.
    """
    if id_leitura is None:
        return retorno_agricultor.registrar_acao(caminho, codigo_acao, causa, None, origem, instante_utc=instante), False
    return retorno_agricultor.registrar_acao_sem_repetir(caminho, codigo_acao, causa, id_leitura, origem,
                                                         instante_utc=instante)


@app.post("/api/acao_feita")
def rota_acao_feita() -> Tuple[Response, int]:
    """O agricultor tocou em "Feito": grava a ação e a causa e devolve a confirmação falada.

    Corpo: ``{"codigo_acao": "REGAR_AGORA", "causa": "TERRA_SECA", "id_leitura": 42}``
    e, para um toque guardado sem rede e reenviado depois, ``"atraso_s"``
    (segundos desde o toque, até um dia). Sem PIN; corpo até 1 KB (413);
    10 pedidos por minuto por IP (429). Resposta 201 com ``fala`` =
    "Anotado: você regou às 14 horas.". Toque repetido (mesma ação, causa e
    leitura em menos de 60 s) não vira outro registro: 200 com ``repetido: true`` e o
    ``id_acao`` do primeiro.
    """
    _limitar_corpo(TAMANHO_MAXIMO_CORPO_RETORNO_BYTES)
    _limitar_pedidos(_LIMITADOR_FEITO)
    corpo = _corpo_json()
    codigo = _texto_obrigatorio(corpo, "codigo_acao", "Informe a ação feita.")
    causa = _texto_obrigatorio(corpo, "causa", "Informe o motivo.")
    id_leitura = _id_leitura_do_corpo(corpo)
    atraso = corpo.get("atraso_s", 0)
    if isinstance(atraso, bool) or not isinstance(atraso, (int, float)) or atraso != atraso:
        raise ErroApi("Atraso inválido.", 400)
    instante = _agora_utc() - timedelta(seconds=max(0.0, min(float(ATRASO_MAXIMO_FEITO_S), float(atraso))))
    if id_leitura is None and atraso == 0:
        id_leitura = _leitura_da_demonstracao(_caminho_banco())
    try:
        id_acao, repetido = registrar_acao_sem_repetir(_caminho_banco(), codigo, causa, id_leitura, _ip_cliente(),
                                                       instante)
    except ValueError:
        raise ErroApi("Ação ou motivo desconhecido.", 400) from None
    fala = retorno_agricultor.frase_confirmacao_acao(codigo.strip().upper(), instante)
    registrador.info("Feito%s: %s (%s) às %s, de %s.", " repetido" if repetido else "", codigo, causa,
                     instante.isoformat(timespec="seconds"), _ip_cliente())
    corpo_resposta: Dict[str, Any] = {"ok": True, "id_acao": id_acao, "fala": fala, "causa_silenciada": True,
                                      "segundos_para_desfazer": JANELA_DESFAZER_SEGUNDOS}
    if repetido:
        corpo_resposta["repetido"] = True
        return jsonify(corpo_resposta), 200
    return jsonify(corpo_resposta), 201


@app.post("/api/acao_feita/desfazer")
def rota_desfazer_acao() -> Tuple[Response, int]:
    """Desfaz um "Feito" tocado sem querer: ``{"id_acao": 12}``, até 120 s depois do toque.

    Sem PIN, com o mesmo limite de pedidos do "Feito". 200 ``{"desfeito": true}``;
    404 registro inexistente; 409 passou o tempo de desfazer. O silêncio da
    causa acaba junto (o alerta volta a valer).
    """
    _limitar_corpo(TAMANHO_MAXIMO_CORPO_RETORNO_BYTES)
    _limitar_pedidos(_LIMITADOR_FEITO)
    corpo = _corpo_json()
    valor = corpo.get("id_acao")
    if isinstance(valor, bool) or not isinstance(valor, int) or not 1 <= valor <= MAIOR_ID_SQLITE:
        raise ErroApi("Informe o registro a desfazer.", 400)
    try:
        retorno_agricultor.desfazer_acao(_caminho_banco(), valor, _agora_utc())
    except retorno_agricultor.RegistroInexistente:
        raise ErroApi(MENSAGEM_DESFAZER_INEXISTENTE, 404) from None
    except retorno_agricultor.JanelaDesfazerExpirada:
        raise ErroApi(MENSAGEM_DESFAZER_EXPIRADO, 409) from None
    registrador.info("Feito %d desfeito por %s.", valor, _ip_cliente())
    return jsonify({"ok": True, "desfeito": True, "fala": FALA_DESFEITO}), 200


@app.post("/api/pedido_ajuda")
def rota_pedido_ajuda() -> Tuple[Response, int]:
    """O agricultor tocou em "Chamar o técnico": grava o pedido e devolve o link do WhatsApp.

    Sem PIN; 3 pedidos a cada 10 minutos por IP (429). O link
    ``https://wa.me/<numero_whatsapp_tecnico>?text=<resumo>`` só vem quando o
    número está cadastrado (senão ``link`` = ``""``); quem abre o link é o
    WhatsApp do celular — o servidor continua sem internet.
    """
    _limitar_corpo(TAMANHO_MAXIMO_CORPO_RETORNO_BYTES)
    _limitar_pedidos(_LIMITADOR_AJUDA)
    _corpo_json_opcional()
    caminho = _caminho_banco()
    agora = _agora_utc()
    try:
        status: Optional[Dict[str, Any]] = montar_status(caminho)
    except (ErroApi, sqlite3.Error) as erro:
        registrador.warning("Pedido de ajuda sem o estado atual: %s", erro)
        status = None
    id_pedido = retorno_agricultor.registrar_pedido_ajuda(caminho, status, agora)
    configuracoes = obter_todas_configuracoes(caminho_banco=caminho)
    resumo_curto = ""
    try:
        resumo_curto = retorno_agricultor.resumo_curto_whatsapp(retorno_agricultor.montar_resumo_tecnico(
            caminho, DIAS_RESUMO_TECNICO_PADRAO, agora, configuracoes))
    except (sqlite3.Error, ValueError) as erro:
        registrador.warning("Resumo da semana indisponível para o pedido de ajuda: %s", erro)
    texto = retorno_agricultor.montar_texto_pedido(str(configuracoes.get("nome_propriedade") or ""), status, agora,
                                                   resumo_curto)
    link = retorno_agricultor.montar_link_whatsapp(configuracoes.get("numero_whatsapp_tecnico"), texto)
    fala = ("Vou abrir o WhatsApp com o recado para o técnico. Aperte o botão de enviar." if link else
            "Pedido de ajuda anotado no computador. O número do técnico ainda não foi cadastrado: "
            "peça para alguém avisar o técnico.")
    registrador.info("Pedido de ajuda %d de %s (link %s).", id_pedido, _ip_cliente(), "sim" if link else "não")
    return jsonify({"ok": True, "id_pedido": id_pedido, "link": link, "texto": texto, "fala": fala}), 201


@app.post("/api/evento")
def rota_evento() -> Tuple[Response, int]:
    """Evento da tela (``ouviu``, ``tocou_pictograma``, ``ajuda``, ``sem_voz``, ``rede_caida``) para o técnico.

    Sem PIN; corpo até 1 KB; 60 por minuto por IP. ``feito`` e
    ``chamou_tecnico`` são gravados pelo próprio servidor nas rotas acima.
    """
    _limitar_corpo(TAMANHO_MAXIMO_CORPO_RETORNO_BYTES)
    _limitar_pedidos(_LIMITADOR_EVENTOS)
    corpo = _corpo_json()
    tipo = _texto_obrigatorio(corpo, "tipo", "Informe o tipo do evento.").lower()
    if tipo not in TIPOS_EVENTO_DA_PAGINA:
        raise ErroApi("Tipo de evento desconhecido.", 400)
    detalhe = corpo.get("detalhe", "")
    retorno_agricultor.registrar_evento(_caminho_banco(), tipo, str(detalhe if isinstance(detalhe, str) else ""),
                                        _agora_utc())
    return jsonify({"ok": True}), 201


# ---------------------------------------------------------------------------
# Página e API do técnico (PIN da banca)
# ---------------------------------------------------------------------------
def _configuracoes_tipadas(caminho: Path) -> Dict[str, Any]:
    """Configurações atuais, com as chaves da v3 nos padrões combinados quando ausentes."""
    configuracoes = obter_todas_configuracoes(caminho_banco=caminho)
    for chave, padrao in (("prognostico_suspenso", False), ("prognostico_brier_local_28d", None),
                          ("numero_whatsapp_tecnico", ""), ("data_florada_principal", ""),
                          ("seca_programada_ativa", False), ("regra_temperatura", "instantanea")):
        configuracoes.setdefault(chave, padrao)
    return configuracoes


def _booleano_config(valor: Any) -> bool:
    """Interpreta um valor de configuração como booleano (``"true"``, ``1``, ``True``...)."""
    if isinstance(valor, bool):
        return valor
    return str(valor or "").strip().lower() in ("true", "1", "sim", "yes", "verdadeiro")


@app.get("/tecnico")
def pagina_tecnico() -> Response:
    """Página do técnico de ATER: resumo da semana, WhatsApp e assistente de calibração (pede o PIN)."""
    resposta = Response(render_template("tecnico.html"), mimetype="text/html")
    resposta.headers["Cache-Control"] = "no-cache"
    return resposta


@app.get("/api/tecnico/resumo")
@_exigir_pin_banca
def rota_resumo_tecnico() -> Response:
    """Resumo dos últimos ``dias`` (1 a 90, padrão 7) para o técnico; exige o PIN quando definido."""
    texto_dias = request.args.get("dias", str(DIAS_RESUMO_TECNICO_PADRAO)).strip()
    try:
        dias = int(texto_dias)
    except ValueError:
        raise ErroApi("O número de dias precisa ser um número inteiro de 1 a 90.", 400) from None
    if not 1 <= dias <= retorno_agricultor.DIAS_RESUMO_MAXIMO:
        raise ErroApi("O número de dias precisa ficar entre 1 e 90.", 400)
    caminho = _caminho_banco()
    agora = _agora_utc()
    configuracoes = _configuracoes_tipadas(caminho)
    try:
        retorno_agricultor.limpar_eventos_antigos(caminho, agora_utc=agora)
    except sqlite3.Error as erro:
        registrador.warning("Limpeza dos eventos antigos falhou: %s", erro)
    resumo = retorno_agricultor.montar_resumo_tecnico(
        caminho, dias, agora, configuracoes, prognostico_disponivel=_modulo_antecipacao() is not None,
        calibracao=calibracao_sonda.estado_calibracao(caminho, agora_utc=agora),
    )
    return jsonify(resumo)


@app.post("/api/tecnico/configuracao")
@_exigir_pin_banca
def rota_configuracao_tecnico() -> Response:
    """Cadastra (ou apaga, com ``""``) o número do WhatsApp do técnico: ``{"numero_whatsapp_tecnico": "+55 35 9..."}``."""
    corpo = _corpo_json()
    bruto = corpo.get("numero_whatsapp_tecnico")
    if not isinstance(bruto, str):
        raise ErroApi("Informe o número do WhatsApp do técnico.", 400)
    numero = retorno_agricultor.normalizar_numero_whatsapp(bruto)
    if bruto.strip() and not numero:
        raise ErroApi("Número inválido: use DDD e número, por exemplo 35 99999 0000.", 400)
    definir_configuracao("numero_whatsapp_tecnico", numero, caminho_banco=_caminho_banco())
    registrador.info("Número do WhatsApp do técnico %s por %s.", "cadastrado" if numero else "apagado", _ip_cliente())
    return jsonify({"ok": True, "numero_whatsapp_tecnico": numero})


# ---------------------------------------------------------------------------
# Banca: florada e seca programada (PIN)
# ---------------------------------------------------------------------------
def _estado_banca(caminho: Path) -> Dict[str, Any]:
    """Florada e seca programada atuais (chaves escritas pela banca e lidas pela regra fisiológica)."""
    configuracoes = obter_todas_configuracoes(caminho_banco=caminho)
    return {
        "data_florada_principal": str(configuracoes.get("data_florada_principal") or ""),
        "seca_programada_ativa": _booleano_config(configuracoes.get("seca_programada_ativa")),
        "seca_programada_inicio": str(configuracoes.get("seca_programada_inicio") or SECA_PROGRAMADA_INICIO_PADRAO),
        "seca_programada_fim": str(configuracoes.get("seca_programada_fim") or SECA_PROGRAMADA_FIM_PADRAO),
    }


@app.post("/api/banca/florada")
@_exigir_pin_banca
def rota_florada() -> Response:
    """"O café floriu hoje": grava ``data_florada_principal`` (AAAA-MM-DD) com a data LOCAL.

    Corpo opcional ``{"data": "AAAA-MM-DD"}`` para o técnico corrigir a data
    (não pode ser no futuro). A regra fisiológica usa a data + 45 dias como
    janela da florada (calor ≥ 34 °C aborta flores).
    """
    corpo = _corpo_json_opcional()
    hoje = _agora_utc().astimezone().date()
    texto = corpo.get("data")
    if texto is None:
        data = hoje
    else:
        try:
            data = datetime.strptime(str(texto).strip(), "%Y-%m-%d").date()
        except ValueError:
            raise ErroApi("Data inválida: use AAAA-MM-DD.", 400) from None
        if data > hoje:
            raise ErroApi("A data da florada não pode ser no futuro.", 400)
    caminho = _caminho_banco()
    definir_configuracao("data_florada_principal", data.isoformat(), caminho_banco=caminho)
    registrador.info("Florada principal registrada em %s por %s.", data.isoformat(), _ip_cliente())
    estado = _estado_banca(caminho)
    estado.update({"ok": True, "fala": f"Anotado: o café floriu em {data.strftime('%d/%m')}."})
    return jsonify(estado)


@app.post("/api/banca/seca_programada")
@_exigir_pin_banca
def rota_seca_programada() -> Response:
    """Liga/desliga a seca programada (``{"ativa": true}``; sem corpo, inverte o estado atual).

    Grava também ``seca_programada_inicio``/``seca_programada_fim`` ("MM-DD",
    padrão 24/06–04/09 da Embrapa Cerrados) quando ainda não existirem.
    """
    corpo = _corpo_json_opcional()
    caminho = _caminho_banco()
    atual = _estado_banca(caminho)
    pedido = corpo.get("ativa")
    if pedido is not None and not isinstance(pedido, bool):
        raise ErroApi("Use verdadeiro ou falso em \"ativa\".", 400)
    ativa = (not atual["seca_programada_ativa"]) if pedido is None else bool(pedido)
    configuracoes = obter_todas_configuracoes(caminho_banco=caminho)
    definir_configuracao("seca_programada_ativa", ativa, caminho_banco=caminho)
    for chave, padrao in (("seca_programada_inicio", SECA_PROGRAMADA_INICIO_PADRAO),
                          ("seca_programada_fim", SECA_PROGRAMADA_FIM_PADRAO)):
        if not str(configuracoes.get(chave) or ""):
            definir_configuracao(chave, padrao, caminho_banco=caminho)
    registrador.info("Seca programada %s por %s.", "ligada" if ativa else "desligada", _ip_cliente())
    estado = _estado_banca(caminho)
    estado.update({"ok": True, "fala": "Seca programada ligada." if ativa else "Seca programada desligada."})
    return jsonify(estado)


# ---------------------------------------------------------------------------
# Calibração da sonda de solo (PIN)
# ---------------------------------------------------------------------------
def _numero_do_corpo(corpo: Dict[str, Any], campo: str, padrao: Optional[float]) -> float:
    """Número do corpo (ou ``padrao``).

    Raises:
        ErroApi: campo ausente sem padrão ou que não é número.
    """
    valor = corpo.get(campo, padrao)
    if valor is None or isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise ErroApi(f"Informe {campo} como número.", 400)
    return float(valor)


@app.get("/api/calibracao")
@_exigir_pin_banca
def rota_calibracao() -> Response:
    """Calibração ativa da sonda, método, dias desde ela e pontos já capturados."""
    return jsonify(calibracao_sonda.estado_calibracao(_caminho_banco(), agora_utc=_agora_utc()))


@app.post("/api/calibracao/ponto")
@_exigir_pin_banca
def rota_calibracao_ponto() -> Response:
    """Captura o ponto ``{"tipo": "SECO" | "UMIDO" | "CAMPO"}`` com as leituras do último minuto (422 se instável)."""
    corpo = _corpo_json()
    tipo = _texto_obrigatorio(corpo, "tipo", "Informe o tipo do ponto: SECO, UMIDO ou CAMPO.")
    try:
        ponto = calibracao_sonda.capturar_ponto(_caminho_banco(), tipo, agora_utc=_agora_utc())
    except calibracao_sonda.ErroCalibracao as erro:
        raise ErroApi(str(erro), 422) from None
    return jsonify({"ok": True, "ponto": ponto})


@app.post("/api/calibracao/concluir")
@_exigir_pin_banca
def rota_calibracao_concluir() -> Response:
    """Fecha a calibração de dois pontos (``alvo_seco_pct`` 0 e ``alvo_umido_pct`` 100 por padrão)."""
    corpo = _corpo_json_opcional()
    try:
        calibracao = calibracao_sonda.concluir_dois_pontos(
            _caminho_banco(), alvo_seco_pct=_numero_do_corpo(corpo, "alvo_seco_pct", 0.0),
            alvo_umido_pct=_numero_do_corpo(corpo, "alvo_umido_pct", 100.0), agora_utc=_agora_utc())
    except calibracao_sonda.ErroCalibracao as erro:
        raise ErroApi(str(erro), 422) from None
    registrador.info("Calibração de dois pontos concluída por %s.", _ip_cliente())
    return jsonify({"ok": True, "calibracao": asdict(calibracao)})


@app.post("/api/calibracao/aferir")
@_exigir_pin_banca
def rota_calibracao_aferir() -> Response:
    """Aferição de um ponto no campo: ``{"valor_referencia_pct": 70}``."""
    corpo = _corpo_json()
    referencia = _numero_do_corpo(corpo, "valor_referencia_pct", None)
    try:
        calibracao = calibracao_sonda.aferir_um_ponto(_caminho_banco(), referencia, agora_utc=_agora_utc())
    except calibracao_sonda.ErroCalibracao as erro:
        raise ErroApi(str(erro), 422) from None
    registrador.info("Aferição de um ponto (%.1f %%) por %s.", referencia, _ip_cliente())
    return jsonify({"ok": True, "calibracao": asdict(calibracao)})


@app.post("/api/calibracao/fabrica")
@_exigir_pin_banca
def rota_calibracao_fabrica() -> Response:
    """Volta à reta de fábrica do firmware (sem calibração no sítio)."""
    calibracao = calibracao_sonda.voltar_para_fabrica(_caminho_banco(), agora_utc=_agora_utc())
    return jsonify({"ok": True, "calibracao": asdict(calibracao)})


def _componentes_saude(caminho: Path, banco_ok: bool, configuracoes: Dict[str, Any]) -> Dict[str, Any]:
    """Detalhe por componente de ``/api/saude`` (chaves ausentes do coletor viram ``None``/0)."""
    relogio_inconsistente = False
    if banco_ok:
        try:
            ultima = _ultima_leitura_real(caminho)
        except sqlite3.Error as erro:
            registrador.warning("Não foi possível ler a última leitura para a saúde: %s", erro)
            ultima = None
        relogio_inconsistente = ultima is not None and _relogio_inconsistente(_idade_segundos(ultima))
    return {
        "banco": {"ok": banco_ok},
        "coletor": estado_coletor(configuracoes),
        "disco": estado_disco(caminho, configuracoes),
        "relogio": {
            "inconsistente": relogio_inconsistente,
            "ultimo_salto_em": str(configuracoes.get("relogio_ultimo_salto_em") or "") or None,
        },
        "prognostico": {
            "disponivel": _modulo_antecipacao() is not None,
            # Chaves escritas pela avaliação local da Frente 2 (ausentes = não suspenso, sem Brier).
            "suspenso": _booleano_config(configuracoes.get("prognostico_suspenso")),
            "brier_local_28d": _numero_ou_none(configuracoes.get("prognostico_brier_local_28d")),
        },
        "serial": {
            "linhas_v1": _inteiro_ou_none(configuracoes.get("serial_linhas_v1")) or 0,
            "linhas_v2": _inteiro_ou_none(configuracoes.get("serial_linhas_v2")) or 0,
            "checksum_invalido": _inteiro_ou_none(configuracoes.get("serial_checksum_invalido")) or 0,
            "descartadas": _inteiro_ou_none(configuracoes.get("serial_linhas_descartadas")) or 0,
        },
    }


@app.get("/api/saude")
def rota_saude() -> Tuple[Response, int]:
    """Verificação de funcionamento por componente.

    Mantém as chaves de sempre no topo (``ok``, ``banco``, ``modelo_carregado``,
    ``versao_modelo``, ``modo_operacao``, ``culturas_modelo``) e acrescenta
    ``componentes``. Responde 503 com o banco inacessível, o coletor parado
    (batimento velho) ou o disco abaixo do mínimo; sem batimento gravado o
    coletor fica ``vivo = None`` (carência de banco novo), sem 503.
    """
    caminho = _caminho_banco()
    banco_ok = False
    modo = ""
    versao_configurada = "REGRAS"
    configuracoes: Dict[str, Any] = {}
    try:
        conexao = obter_conexao(caminho)
        try:
            conexao.execute("SELECT COUNT(*) FROM configuracao_sistema").fetchone()
        finally:
            conexao.close()
        configuracoes = obter_todas_configuracoes(caminho_banco=caminho)
        modo = str(configuracoes.get("modo_operacao") or "")
        versao_configurada = str(configuracoes.get("versao_modelo") or "REGRAS")
        banco_ok = True
    except (sqlite3.Error, OSError) as erro:
        registrador.error("Banco de dados inacessível: %s", erro)
    pacote = obter_pacote_modelo()
    versao = str(pacote.get("versao", versao_configurada)) if pacote is not None else versao_configurada
    culturas_modelo = pacote.get("culturas") if pacote is not None else None
    componentes = _componentes_saude(caminho, banco_ok, configuracoes)
    saudavel = banco_ok and componentes["coletor"]["vivo"] is not False and bool(componentes["disco"]["ok"])
    resposta = {
        "ok": saudavel,
        "banco": banco_ok,
        "modelo_carregado": pacote is not None,
        "versao_modelo": versao,
        "modo_operacao": modo,
        # Culturas do treino: nas demais o sistema usa só o motor de regras (lista vazia = sem modelo).
        "culturas_modelo": list(culturas_modelo) if isinstance(culturas_modelo, list) else [],
        "componentes": componentes,
        # Fora de "componentes" de propósito: a sonda sem calibração no sítio não deixa o sistema "doente".
        "calibracao_sonda": {
            "dias_desde_calibracao": calibracao_sonda.dias_desde_calibracao(caminho) if banco_ok else None,
            "metodo": calibracao_sonda.estado_calibracao(caminho)["metodo"] if banco_ok else "FABRICA",
        },
    }
    return jsonify(resposta), (200 if saudavel else 503)


def obter_configuracao_inteira(chave: str, padrao: int, caminho: Path) -> int:
    """Lê uma configuração inteira positiva, com ``padrao`` se ausente ou inválida."""
    texto = obter_configuracao(chave, str(padrao), caminho_banco=caminho)
    try:
        valor = int(float(str(texto)))
    except ValueError:
        return padrao
    return valor if valor > 0 else padrao


# ---------------------------------------------------------------------------
# Tratamento de erros (sempre JSON simples)
# ---------------------------------------------------------------------------
_MENSAGENS_HTTP: Dict[int, str] = {
    400: "Pedido inválido.",
    404: "Endereço não encontrado.",
    405: "Esta ação não é permitida neste endereço.",
    413: "Pedido grande demais.",
    415: "Envie os dados no formato JSON.",
}


@app.errorhandler(ErroApi)
def tratar_erro_api(erro: ErroApi) -> Tuple[Response, int]:
    """Erros previstos da API: ``{"erro": mensagem simples}`` mais os campos extras (``codigo``...)."""
    corpo: Dict[str, Any] = {"erro": erro.mensagem}
    corpo.update(erro.extras)
    return jsonify(corpo), erro.codigo_http


@app.errorhandler(HTTPException)
def tratar_erro_http(erro: HTTPException) -> Tuple[Response, int]:
    """Erros HTTP do Flask/Werkzeug (404, 405, 413...) convertidos para JSON em português."""
    codigo = int(erro.code or 500)
    return jsonify({"erro": _MENSAGENS_HTTP.get(codigo, "Não foi possível atender o pedido.")}), codigo


@app.errorhandler(Exception)
def tratar_erro_inesperado(erro: Exception) -> Tuple[Response, int]:
    """Falhas inesperadas: registra no log e devolve mensagem simples (500)."""
    registrador.exception("Erro inesperado ao atender %s %s: %s", request.method, request.path, erro)
    return jsonify({"erro": "O sistema teve um problema. Tente de novo em instantes."}), 500


@app.after_request
def comprimir_resposta(resposta: Response) -> Response:
    """Comprime com gzip (nível 6) HTML, JSON e JS acima de 1 KB quando o navegador aceitar.

    Em rede 2G a página cai de ~60 KB para ~15 KB. Respostas em fluxo
    (``direct_passthrough``/streaming) ou já codificadas passam intactas.
    """
    if "gzip" not in request.headers.get("Accept-Encoding", "").lower():
        return resposta
    if resposta.direct_passthrough or resposta.is_streamed or "Content-Encoding" in resposta.headers:
        return resposta
    if resposta.mimetype not in TIPOS_COMPRIMIVEIS:
        return resposta
    dados = resposta.get_data()
    if len(dados) <= TAMANHO_MINIMO_COMPRESSAO:
        return resposta
    resposta.set_data(gzip.compress(dados, compresslevel=NIVEL_COMPRESSAO))
    resposta.headers["Content-Encoding"] = "gzip"
    resposta.vary.add("Accept-Encoding")
    return resposta


@app.after_request
def desativar_cache_api(resposta: Response) -> Response:
    """Impede que o navegador guarde respostas da API (o semáforo precisa estar sempre atual)."""
    if request.path.startswith("/api/") and "ETag" not in resposta.headers:
        resposta.headers["Cache-Control"] = "no-store"
    return resposta


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    _host = os.environ.get("AGRO_HOST") or "0.0.0.0"
    try:
        _porta = int(os.environ.get("AGRO_PORTA") or "5000")
    except ValueError:
        registrador.warning("AGRO_PORTA inválida (%r); usando 5000.", os.environ.get("AGRO_PORTA"))
        _porta = 5000
    registrador.info("Companheiro Virtual em http://%s:%d (banco: %s)", _host, _porta, _caminho_banco())
    app.run(host=_host, port=_porta, debug=False)
