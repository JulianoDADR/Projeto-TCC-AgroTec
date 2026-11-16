"""Testes da Frente B (v4) — conteúdo e voz: a voz diz primeiro o que fazer.

Cobrem, sempre com banco SQLite TEMPORÁRIO e sem rede, sobre o espaço
combinatório da auditoria de conteúdo (regra v2 em grade de temperatura × solo;
regra fisiológica com os motivos de temperatura e de solo; antecipação com
motivo × prazo × estresse × hora × probabilidade; as duas cultivares):

    * B1 ``montar_fala_curta``: ação na 1ª ou 2ª frase, ≤ 15 palavras, sem
      números, começa por "Sinal ", coerente com os pictogramas, Flesch-PT
      mediano ≥ 80; as quatro situações especiais começam por "Atenção.";
    * B2 ``falar_graus`` e "tenha geada": nada de "1 graus", "-2" ou "geie";
      causa e ações da antecipação coerentes com a voz;
    * B3 uma ordem por ação: no máximo uma ordem de rega por mensagem, nunca
      "Regue agora" junto de "gelando agora", e o status de cada leitura igual
      ao do commit 5c66abe (resumo SHA-256 da tabela de status);
    * B4 voz → figura (``acoes_cobrem_mensagem``) e a sombra do calor antecipado.

Execução (a partir da raiz do projeto): ``python -m unittest tests.test_conteudo_voz_v4 -v``.
"""

from __future__ import annotations

import hashlib
import itertools
import re
import shutil
import statistics
import sys
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Sequence, Tuple

RAIZ_PROJETO: Path = Path(__file__).resolve().parent.parent
PASTA_SRC: Path = RAIZ_PROJETO / "src"
if str(PASTA_SRC) not in sys.path:
    sys.path.insert(0, str(PASTA_SRC))

import acoes_orientacao as ao  # noqa: E402
import gerador_dataset as gd  # noqa: E402
import prognostico as pg  # noqa: E402
import regras_agronomicas as ra  # noqa: E402
from database import (  # noqa: E402
    FaixaParametro,
    ParametrosCultura,
    RegistroLeitura,
    buscar_parametros_cultura,
    inicializar_banco,
)

CULTIVARES: Tuple[str, ...] = ("cafe_arabica", "cafe_conilon")
# Grade da auditoria de conteúdo (comitê de UX da v4).
TEMPERATURAS_V2: Tuple[int, ...] = (-5, -2, -1, 0, 1, 2, 3, 6, 10, 14, 16, 18, 20, 22, 24, 26, 28, 30, 33, 36, 40)
SOLOS_V2: Tuple[int, ...] = (5, 15, 25, 35, 45, 52, 58, 62, 70, 78, 82, 86, 90, 95, 99)
MOTIVOS_TEMPERATURA: Tuple[str, ...] = ("NENHUM", "GEADA_AGORA", "CALOR_FLORADA", "CALOR_SEGUIDO", "CALOR_DIA",
                                        "DIA_FRIO", "DIA_QUENTE")
MOTIVOS_SOLO: Tuple[str, ...] = ("NENHUM", "SECA_AGORA", "SECA_PROGRAMADA", "ENCHARCAMENTO")
PRAZOS: Tuple[Optional[int], ...] = (2, 6, 14, 30, 50, None)
ESTRESSES: Tuple[Optional[int], ...] = (None, 0, 1, 5)
HORAS: Tuple[int, ...] = (3, 8, 15, 17, 19, 22)
PROBABILIDADES: Tuple[Optional[float], ...] = (None, 0.5, 0.9)
MOTIVOS_ANTECIPACAO: Tuple[str, ...] = tuple(motivo for motivo in pg.MOTIVOS if motivo != "NENHUM")

# Critério B1: a 1ª frase com ordem tem índice ≤ 1 (contando de 0). O critério do comitê lista os
# verbos no imperativo; "Não precisa fazer nada" é a ação do Amarelo informativo (dia frio ou quente
# sem dano, regra fisiológica da v3) e também conta, como na auditoria de conteúdo do comitê.
PADRAO_ACAO_COMITE: "re.Pattern[str]" = re.compile(
    r"Regue|Não regue|Pare|Abra|Faça|Cubra|Chegue|Proteja|Confira|Verifique|Olhe")
PADRAO_ACAO_FALADA: "re.Pattern[str]" = re.compile(PADRAO_ACAO_COMITE.pattern + r"|Não precisa fazer nada")
PADRAO_REGA: "re.Pattern[str]" = re.compile(r"\b[Rr]egue\b(?! à noite)")
# Resumo SHA-256 da tabela de status (regra v2 + regra fisiológica) calculado no commit 5c66abe,
# antes de qualquer mudança da v4 (13.122 linhas; ver ``tabela_de_status``).
RESUMO_STATUS_5C66ABE: str = "1dd8147b56a5e9955d20145e3bd54886faeadf66c6adf2b514a258a89b24b7ab"
LINHAS_STATUS_5C66ABE: int = 13122

# ---------------------------------------------------------------------------
# Legibilidade: Flesch adaptado ao português (Martins, Ghiraldelo, Nunes e Oliveira Jr., 1996)
# ---------------------------------------------------------------------------
_VOGAIS: str = "aeiouáéíóúâêôãõàü"
_DITONGOS: "re.Pattern[str]" = re.compile(r"(ão|õe|ãe|ai|ei|oi|ui|au|eu|iu|ou)")
_U_MUDO: "re.Pattern[str]" = re.compile(r"(?<=[qg])u(?=[eiéí])")


def contar_silabas(palavra: str) -> int:
    """Sílabas aproximadas em pt-BR: núcleos vocálicos, ditongo comum = 1, "u" mudo de gue/gui/que/qui fora."""
    texto = _DITONGOS.sub("a", _U_MUDO.sub("", palavra.lower()))
    return max(1, len(re.findall(f"[{_VOGAIS}]+", texto)))


def flesch_pt(texto: str) -> float:
    """Índice de Flesch para o português: 248,835 − 1,015 × (palavras/frases) − 84,6 × (sílabas/palavras).

    Mesmo contador da auditoria de conteúdo do comitê (um número conta duas sílabas).
    """
    frases = [frase for frase in re.split(r"(?<=[.!?])\s+", texto.strip()) if frase.strip()]
    palavras = re.findall(r"[A-Za-zÀ-ÿ0-9]+", texto)
    silabas = sum(2 if palavra.isdigit() else contar_silabas(palavra) for palavra in palavras)
    return (248.835 - 1.015 * (len(palavras) / max(1, len(frases)))
            - 84.6 * (silabas / max(1, len(palavras))))


def dividir_frases(texto: str) -> List[str]:
    """Frases terminadas por ponto, exclamação ou interrogação."""
    return [frase for frase in re.split(r"(?<=[.!?])\s+", texto.strip()) if frase]


# ---------------------------------------------------------------------------
# Espaço combinatório da auditoria
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ItemFala:
    """Uma mensagem do espaço da auditoria, com as figuras, a causa e a fala curta."""

    origem: str
    nivel: int
    mensagem: str
    acoes: Tuple[str, ...]
    causa: str
    fala_curta: str
    temperatura: float
    solo: float


def _ponto_medio(faixa: FaixaParametro) -> float:
    """Meio da faixa ideal."""
    return (faixa.ideal_min + faixa.ideal_max) / 2.0


def _registro(parametros: ParametrosCultura, temperatura: float, solo: float, status_regra: int) -> RegistroLeitura:
    """Leitura mínima para ``prognostico._mensagem_antecipada`` (só status da regra, ar e terra são lidos)."""
    deficit, desvio = gd.calcular_features(temperatura, solo, parametros)
    return RegistroLeitura(
        data_hora="2026-07-10 08:00:00", codigo_cultura=parametros.codigo, origem="CAMPO_USB",
        temperatura_ar_bruta=temperatura, umidade_ar_bruta=70.0, umidade_solo_bruta=solo,
        temperatura_ar=temperatura, umidade_ar=70.0, umidade_solo=solo, deficit_hidrico_solo=deficit,
        desvio_termico=desvio, status_regra=status_regra, status_predito=max(1, status_regra),
        status_alerta=max(1, status_regra), probabilidade_predicao=0.9, flag_anomalia=0, descricao_anomalia="",
        mensagem_voz="", versao_modelo="TESTE")


def mensagem_antecipada(parametros: ParametrosCultura, motivo: str, prazo: Optional[int], estresse: Optional[int],
                        hora: int, probabilidade: Optional[float], temperatura: float, solo: float,
                        fisiologica: bool = False) -> str:
    """``prognostico._mensagem_antecipada`` para uma leitura e um motivo da grade."""
    status_regra = gd.calcular_status_regra(temperatura, solo, parametros)
    explicacao = {"motivo_principal": motivo, "prazo_horas": prazo, "horas_em_estresse": estresse}
    return pg._mensagem_antecipada(_registro(parametros, temperatura, solo, status_regra), parametros, explicacao,
                                   datetime(2026, 7, 10, hora, 0), probabilidade, fisiologica)


def _item(origem: str, nivel: int, mensagem: str, temperatura: float, solo: float, parametros: ParametrosCultura,
          motivo_antecipacao: str = "NENHUM", motivo_regra: str = "") -> ItemFala:
    """Deriva figuras, causa e fala curta como ``app.montar_status`` faz (com a mensagem falada)."""
    acoes = ao.derivar_acoes(nivel, temperatura, solo, parametros, motivo_antecipacao=motivo_antecipacao,
                             mensagem_voz=mensagem, motivo_regra=motivo_regra)
    causa = ao.derivar_causa(nivel, {"temperatura_ar": temperatura, "umidade_solo": solo, "motivo_regra": motivo_regra},
                             parametros, motivo_antecipacao, mensagem_voz=mensagem)
    return ItemFala(origem, nivel, mensagem, tuple(acoes), causa, ao.montar_fala_curta(nivel, acoes, causa),
                    float(temperatura), float(solo))


def gerar_espaco_auditoria(culturas: Sequence[ParametrosCultura]) -> List[ItemFala]:
    """Espaço da auditoria: regra v2, regra fisiológica e antecipação (com leitura na faixa ideal), sem repetição."""
    itens: List[ItemFala] = []
    for parametros in culturas:
        # A. Regra instantânea (v2) em grade de temperatura × solo; o nível também sobe a Amarelo (IA).
        for temperatura, solo in itertools.product(TEMPERATURAS_V2, SOLOS_V2):
            status = gd.calcular_status_regra(temperatura, solo, parametros)
            deficit, desvio = gd.calcular_features(temperatura, solo, parametros)
            for nivel in sorted({status, max(status, 1)}):
                mensagem = gd.gerar_mensagem_orientacao(nivel, deficit, desvio, parametros, temperatura, solo)
                itens.append(_item(f"v2 {parametros.codigo} T={temperatura} S={solo}", nivel, mensagem,
                                   temperatura, solo, parametros))
        # B. Regra fisiológica (v3): motivo da temperatura × motivo do solo × intensidade do solo.
        faixa_t = parametros.faixa("temperatura_ar")
        temperaturas = {"NENHUM": [_ponto_medio(faixa_t)], "GEADA_AGORA": [-2.0, 1.0, 3.0], "CALOR_FLORADA": [35.0],
                        "CALOR_SEGUIDO": [35.0], "CALOR_DIA": [31.0], "DIA_FRIO": [15.0], "DIA_QUENTE": [25.0]}
        for motivo_t, motivo_s, intensidade in itertools.product(MOTIVOS_TEMPERATURA, MOTIVOS_SOLO, (0, 1, 2)):
            if motivo_s == "NENHUM" and intensidade:
                continue
            solo = {"NENHUM": 70.0, "SECA_AGORA": 50.0 if intensidade < 2 else 20.0, "SECA_PROGRAMADA": 40.0,
                    "ENCHARCAMENTO": 85.0 if intensidade < 2 else 97.0}[motivo_s]
            status_t = 2 if motivo_t in ra.MOTIVOS_CALOR_VERMELHO + ("GEADA_AGORA",) else int(motivo_t != "NENHUM")
            status_s = intensidade if motivo_s in ("SECA_AGORA", "ENCHARCAMENTO") else int(motivo_s == "SECA_PROGRAMADA")
            motivo_regra = motivo_t if motivo_t != "NENHUM" else (motivo_s if motivo_s != "NENHUM" else "")
            for temperatura in temperaturas[motivo_t]:
                deficit, _ = gd.calcular_features(temperatura, solo, parametros)
                for nivel in sorted({max(status_t, status_s), max(status_t, status_s, 1)}):
                    avaliacao = SimpleNamespace(status=nivel, motivo_temperatura=motivo_t, motivo_solo=motivo_s,
                                                status_solo=status_s)
                    mensagem = gd.gerar_mensagem_fisiologica(avaliacao, deficit, parametros, temperatura, solo)
                    itens.append(_item(f"fis {parametros.codigo} {motivo_t}/{motivo_s}/{intensidade} T={temperatura}",
                                       nivel, mensagem, temperatura, solo, parametros, motivo_regra=motivo_regra))
        # C. Antecipação (Amarelo antecipado) com a leitura no meio da faixa ideal.
        temperatura, solo = _ponto_medio(faixa_t), _ponto_medio(parametros.faixa("umidade_solo"))
        for motivo, prazo, estresse, hora, probabilidade in itertools.product(
                MOTIVOS_ANTECIPACAO, PRAZOS, ESTRESSES, HORAS, PROBABILIDADES):
            mensagem = mensagem_antecipada(parametros, motivo, prazo, estresse, hora, probabilidade, temperatura, solo)
            itens.append(_item(f"ant {parametros.codigo} {motivo} prazo={prazo} estresse={estresse} h={hora} "
                               f"p={probabilidade}", 1, mensagem, temperatura, solo, parametros,
                               motivo_antecipacao=motivo))
    unicos: Dict[Tuple[int, str, Tuple[str, ...], str], ItemFala] = {}
    for item in itens:
        unicos.setdefault((item.nivel, item.mensagem, item.acoes, item.causa), item)
    return list(unicos.values())


def tabela_de_status(culturas: Sequence[ParametrosCultura]) -> List[str]:
    """Status da regra v2 e da regra fisiológica numa grade fixa (para comparar com o commit 5c66abe)."""
    linhas: List[str] = []
    for parametros in culturas:
        for temperatura in range(-5, 46):
            for solo in range(0, 101, 2):
                status = gd.calcular_status_regra(float(temperatura), float(solo), parametros)
                linhas.append(f"v2|{parametros.codigo}|{temperatura}|{solo}|{status}")
        resumos = [ra.RESUMO_VAZIO, ra.ResumoTermico(19.0, 24, 30.0, 30.0), ra.ResumoTermico(14.0, 24, None, None),
                   ra.ResumoTermico(24.0, 24, 35.0, 35.0)]
        configuracoes = [ra.ConfiguracaoTermica(modo=ra.MODO_FISIOLOGICO),
                         ra.ConfiguracaoTermica(modo=ra.MODO_FISIOLOGICO, seca_programada_ativa=True)]
        instantes = [datetime(2026, 7, 10, 14), datetime(2026, 10, 10, 14), datetime(2026, 2, 10, 14)]
        for indice_resumo, resumo in enumerate(resumos):
            for indice_cfg, cfg in enumerate(configuracoes):
                for agora in instantes:
                    for temperatura in range(-3, 42, 3):
                        for solo in range(10, 101, 9):
                            avaliacao = ra.avaliar_status_campo(float(temperatura), float(solo), parametros,
                                                                resumo, cfg, agora)
                            linhas.append(f"fis|{parametros.codigo}|{indice_resumo}|{indice_cfg}|{agora:%m}|"
                                          f"{temperatura}|{solo}|{avaliacao.status}|{avaliacao.motivo}")
    return linhas


class BaseConteudo(unittest.TestCase):
    """Banco temporário com as duas cultivares e o espaço da auditoria calculado uma vez."""

    pasta: Path
    culturas: List[ParametrosCultura]
    espaco: List[ItemFala]

    @classmethod
    def setUpClass(cls) -> None:
        """Cria o banco temporário, lê as faixas e gera o espaço da auditoria."""
        cls.pasta = Path(tempfile.mkdtemp(prefix="agro_voz_v4_"))
        banco = cls.pasta / "agro.db"
        inicializar_banco(banco)
        cls.culturas = [buscar_parametros_cultura(codigo, caminho_banco=banco) for codigo in CULTIVARES]
        cls.espaco = gerar_espaco_auditoria(cls.culturas)

    @classmethod
    def tearDownClass(cls) -> None:
        """Apaga o banco temporário."""
        shutil.rmtree(cls.pasta, ignore_errors=True)

    def alvo_b1(self) -> List[ItemFala]:
        """Itens com status ≥ 1 e pelo menos uma ação (o universo do critério B1)."""
        return [item for item in self.espaco if item.nivel >= 1 and item.acoes]


# ---------------------------------------------------------------------------
# B1. Fala curta
# ---------------------------------------------------------------------------
class TesteFalaCurta(BaseConteudo):
    """``montar_fala_curta``: a ação logo depois da cor, curta e sem números."""

    def test_constantes_e_catalogos(self) -> None:
        """Situações, cores, uma frase curta por ação e por causa, sem dígitos."""
        self.assertEqual(ao.SITUACAO_NORMAL, "normal")
        self.assertEqual(ao.SITUACOES_FALA, ("normal", "teste", "antiga", "sem_leitura", "demonstracao"))
        self.assertEqual(ao.CORES_FALADAS, {0: "verde", 1: "amarelo", 2: "vermelho"})
        self.assertEqual(set(ao.FRASES_ACAO_CURTAS), set(ao.ACOES_VALIDAS))
        self.assertEqual(set(ao.FRASES_CAUSA_CURTAS), set(ao.CAUSAS_VALIDAS))
        self.assertEqual(ao.FRASES_ACAO_CURTAS["REGAR_AGORA"], "Regue agora")
        self.assertEqual(ao.FRASES_ACAO_CURTAS["REGAR_FIM_DA_TARDE"], "Regue no fim da tarde")
        self.assertEqual(ao.FRASES_ACAO_CURTAS["COBRIR_MUDAS"], "Cubra as mudas")
        self.assertEqual(ao.FRASES_ACAO_CURTAS["NADA_A_FAZER"], "Não precisa fazer nada")
        for codigo, frase in list(ao.FRASES_ACAO_CURTAS.items()) + list(ao.FRASES_CAUSA_CURTAS.items()):
            self.assertIsNone(re.search(r"\d", frase), codigo)
        for codigo, frase in ao.FRASES_ACAO_CURTAS.items():
            # Cada frase curta comprova a própria ação (como primeira ou como seguinte, em minúscula).
            self.assertTrue(ao.acoes_coerentes_com_mensagem([codigo], frase), codigo)
            if codigo != "NADA_A_FAZER":  # "nada a fazer" só aparece sozinho, nunca juntado a outra ordem
                self.assertTrue(ao.acoes_coerentes_com_mensagem([codigo], "x, " + frase[:1].lower() + frase[1:]),
                                codigo)
        for codigo in ("TERRA_SECA", "ENCHARCADO", "FRIO_GEADA", "CALOR"):
            self.assertTrue(ao.causa_coerente_com_mensagem(codigo, ao.FRASES_CAUSA_CURTAS[codigo]), codigo)

    def test_exemplos(self) -> None:
        """Cor, ordem e motivo; até três ordens juntadas com vírgula e "e"; verde sem nada a fazer."""
        self.assertEqual(ao.montar_fala_curta(2, ["REGAR_AGORA"], "TERRA_SECA"),
                         "Sinal vermelho. Regue agora. A terra está seca.")
        self.assertEqual(ao.montar_fala_curta(2, ["COBRIR_MUDAS", "CHEGAR_TERRA"], "FRIO_GEADA"),
                         "Sinal vermelho. Cubra as mudas e chegue terra no pé. Faz frio.")
        self.assertEqual(ao.montar_fala_curta(0, ["NADA_A_FAZER"], "NENHUMA"), "Sinal verde. Não precisa fazer nada.")
        self.assertEqual(ao.montar_fala_curta(1, ["VERIFICAR_APARELHO"], "APARELHO"),
                         "Sinal amarelo. Confira o aparelho.")
        # Integração v4 (ajuste justificado): sem pictograma, a fala curta repete o "olhe" da mensagem longa
        # para a 2ª frase ser uma ação (meta 8: ação até a 2ª frase em 100 % das falas curtas).
        self.assertEqual(ao.montar_fala_curta(1, [], "TERRA_SECA"), "Sinal amarelo. Olhe a terra hoje. A terra está seca.")
        # Repetidos, desconhecidos e "nada a fazer" ao lado de outra ação são ignorados.
        self.assertEqual(ao.montar_fala_curta(1, ["NAO_REGAR", "NAO_REGAR", "XYZ", "NADA_A_FAZER"], "ENCHARCADO"),
                         "Sinal amarelo. Não regue. Tem água demais na terra.")

    def test_causa_cai_quando_passa_de_15_palavras(self) -> None:
        """Três ordens longas: a causa sai para a fala caber em 15 palavras."""
        fala = ao.montar_fala_curta(2, ["REGAR_FIM_DA_TARDE", "FAZER_SOMBRA", "CHEGAR_TERRA"], "CALOR")
        self.assertEqual(fala, "Sinal vermelho. Regue no fim da tarde, faça sombra e chegue terra no pé.")
        self.assertLessEqual(len(fala.split()), ao.MAXIMO_PALAVRAS_FALA_CURTA)
        for combinacao in itertools.permutations(ao.ACOES_VALIDAS, 3):
            for nivel in (0, 1, 2):
                for causa in ao.CAUSAS_VALIDAS:
                    fala = ao.montar_fala_curta(nivel, combinacao, causa)
                    self.assertLessEqual(len(fala.split()), 15, fala)
                    self.assertIsNone(re.search(r"\d", fala), fala)

    def test_alerta_sem_pictograma_ainda_diz_o_que_fazer(self) -> None:
        """Integração v4: Amarelo sem ação (IA acima da regra, terra perto da borda) fala o "olhe" da mensagem.

        Na auditoria (regra v2 forçada a Amarelo com a terra a 62 %), a voz longa
        diz "Olhe a terra hoje e prepare a rega", mas ``derivar_acoes`` não dá
        pictograma: sem este caso a fala curta ficava "Sinal amarelo. A terra
        está seca.", sem nenhuma ação.
        """
        for nivel in (1, 2):
            for causa in ao.CAUSAS_VALIDAS:
                with self.subTest(nivel=nivel, causa=causa):
                    fala = ao.montar_fala_curta(nivel, [], causa)
                    frases = [f for f in re.split(r"(?<=\.)\s+", fala) if f]
                    self.assertRegex(frases[1], r"^(Olhe|Confira)")
                    self.assertLessEqual(len(fala.split()), ao.MAXIMO_PALAVRAS_FALA_CURTA)
        self.assertEqual(ao.montar_fala_curta(1, [], "TERRA_SECA"), "Sinal amarelo. Olhe a terra hoje. A terra está seca.")
        self.assertEqual(ao.montar_fala_curta(0, [], "NENHUMA"), "Sinal verde.")

    def test_situacoes_especiais(self) -> None:
        """Teste, medição antiga, sem medição e demonstração: "Atenção." e nunca "Sinal"."""
        esperado = {
            # Integração v4 (ajuste justificado): a simulação mostra o pictograma "Aparelho"; a voz o cita.
            "teste": "Atenção. Dados de teste. Não siga esta cor. Confira o aparelho.",
            "antiga": "Atenção. Medição antiga. Confira o aparelho.",
            "sem_leitura": "Atenção. Sem medição. Confira o aparelho.",
            "demonstracao": "Atenção. Demonstração. Não é a sua lavoura.",
        }
        for situacao, texto in esperado.items():
            for nivel in (0, 1, 2):
                with self.subTest(situacao=situacao, nivel=nivel):
                    fala = ao.montar_fala_curta(nivel, ["REGAR_AGORA"], "TERRA_SECA", situacao=situacao)
                    self.assertEqual(fala, texto)
                    self.assertTrue(fala.startswith("Atenção."))
                    self.assertNotIn("Sinal", fala)
                    self.assertIsNone(re.search(r"\d", fala))

    def test_valores_invalidos(self) -> None:
        """Nível fora de 0–2 (ou booleano/texto) e situação desconhecida levantam ValueError."""
        for nivel in (-1, 3, True, "1", None, 1.0):
            with self.subTest(nivel=nivel), self.assertRaises(ValueError):
                ao.montar_fala_curta(nivel, ["REGAR_AGORA"], "TERRA_SECA")  # type: ignore[arg-type]
        for situacao in ("", "NORMAL", "simulacao"):
            with self.subTest(situacao=situacao), self.assertRaises(ValueError):
                ao.montar_fala_curta(1, ["REGAR_AGORA"], "TERRA_SECA", situacao=situacao)

    def test_espaco_da_auditoria(self) -> None:
        """Status ≥ 1 com ações: ≤ 15 palavras, sem dígito, ação na frase 0 ou 1, "Sinal ", coerente, Flesch ≥ 80."""
        alvo = self.alvo_b1()
        self.assertGreater(len(alvo), 800)
        for item in alvo:
            fala = item.fala_curta
            with self.subTest(origem=item.origem, fala=fala):
                self.assertLessEqual(len(fala.split()), 15)
                self.assertIsNone(re.search(r"\d", fala))
                self.assertTrue(fala.startswith("Sinal "))
                frases = dividir_frases(fala)
                indice = next((i for i, frase in enumerate(frases) if PADRAO_ACAO_FALADA.search(frase)), None)
                self.assertIsNotNone(indice)
                self.assertLessEqual(indice, 1)  # type: ignore[arg-type]
                if item.acoes != ("NADA_A_FAZER",):
                    indice_comite = next((i for i, frase in enumerate(frases) if PADRAO_ACAO_COMITE.search(frase)), 9)
                    self.assertLessEqual(indice_comite, 1)
                self.assertTrue(ao.acoes_coerentes_com_mensagem(list(item.acoes), fala))
        mediana = statistics.median(flesch_pt(item.fala_curta) for item in alvo)
        self.assertGreaterEqual(mediana, 80.0, f"Flesch-PT mediano da fala curta: {mediana:.1f}")
        # A fala curta encurta de verdade: mediana ≤ 15 (a v3 tinha 31 palavras na fala longa).
        self.assertLessEqual(statistics.median(len(item.fala_curta.split()) for item in alvo), 15)

    def test_flesch_pt_referencia(self) -> None:
        """O contador de sílabas e a fórmula reproduzem valores calculados à mão."""
        self.assertEqual(contar_silabas("Regue"), 2)
        self.assertEqual(contar_silabas("vermelho"), 3)
        self.assertEqual(contar_silabas("água"), 2)
        self.assertAlmostEqual(flesch_pt("Sinal vermelho. Regue agora. A terra está seca."), 66.4, delta=0.1)

    def test_nao_muda_a_mensagem_longa(self) -> None:
        """A fala curta é montada à parte: a mensagem longa da v2 continua a mesma estrutura (estado + causa + ação)."""
        parametros = self.culturas[0]
        deficit, desvio = gd.calcular_features(20.0, 30.0, parametros)
        mensagem = gd.gerar_mensagem_orientacao(2, deficit, desvio, parametros, 20.0, 30.0)
        self.assertTrue(mensagem.startswith("Urgente! Perigo para a plantação de café arábica."))
        self.assertIn("Regue agora, devagar, até a terra ficar úmida.", mensagem)
        self.assertNotIn("Sinal", mensagem)


# ---------------------------------------------------------------------------
# B2. Números e palavras familiares
# ---------------------------------------------------------------------------
class TesteGrausEGeada(BaseConteudo):
    """``falar_graus`` nas frases de temperatura e "tenha geada" no lugar de "geie"."""

    def test_falar_graus(self) -> None:
        """Concordância e sinal falado; arredondamento igual ao de ``_numero_falado``."""
        casos = {0: "zero grau", 0.4: "zero grau", -0.4: "zero grau", 1: "1 grau", 0.5: "1 grau", -1: "menos 1 grau",
                 -1.6: "menos 2 graus", -2: "menos 2 graus", -5: "menos 5 graus", 2: "2 graus", 30.5: "31 graus"}
        for valor, esperado in casos.items():
            with self.subTest(valor=valor):
                self.assertEqual(gd.falar_graus(valor), esperado)
        for decimo in range(-80, 450):
            valor = decimo / 10.0
            numero = int(gd._numero_falado(valor))
            self.assertIn(str(abs(numero)) if numero else "zero", gd.falar_graus(valor))

    def _mensagens_frias(self, parametros: ParametrosCultura, temperatura: float) -> List[str]:
        """Mensagens da v2 e da regra fisiológica (geada e dia frio) a uma temperatura, com vários solos."""
        mensagens: List[str] = []
        for solo in (20.0, 45.0, 55.0, 70.0, 85.0, 97.0):
            deficit, desvio = gd.calcular_features(temperatura, solo, parametros)
            for nivel in (1, 2):
                mensagens.append(gd.gerar_mensagem_orientacao(nivel, deficit, desvio, parametros, temperatura, solo))
            for motivo, status in (("GEADA_AGORA", 2), ("DIA_FRIO", 1)):
                avaliacao = SimpleNamespace(status=status, motivo_temperatura=motivo, motivo_solo="NENHUM",
                                            status_solo=0)
                mensagens.append(gd.gerar_mensagem_fisiologica(avaliacao, deficit, parametros, temperatura, solo))
        return mensagens

    def test_temperaturas_faladas_sem_erro(self) -> None:
        """De −5 a 5 °C: nada de "1 graus", "-N" nem "0 graus"; −2 diz "menos 2 graus" e 1 diz "1 grau"."""
        for parametros in self.culturas:
            for temperatura in range(-5, 6):
                for mensagem in self._mensagens_frias(parametros, float(temperatura)):
                    with self.subTest(cultura=parametros.codigo, temperatura=temperatura, mensagem=mensagem):
                        self.assertIsNone(re.search(r"(?<![\d,])1 graus", mensagem))
                        self.assertIsNone(re.search(r"-\d", mensagem))
                        self.assertIsNone(re.search(r"\b0 graus", mensagem))
            geada_menos_2 = gd.gerar_mensagem_fisiologica(
                SimpleNamespace(status=2, motivo_temperatura="GEADA_AGORA", motivo_solo="NENHUM", status_solo=0),
                0.0, parametros, -2.0, 70.0)
            self.assertIn("menos 2 graus", geada_menos_2)
            deficit, desvio = gd.calcular_features(-2.0, 70.0, parametros)
            self.assertIn("menos 2 graus", gd.gerar_mensagem_orientacao(2, deficit, desvio, parametros, -2.0, 70.0))
            deficit, desvio = gd.calcular_features(1.0, 70.0, parametros)
            mensagem_1 = gd.gerar_mensagem_orientacao(2, deficit, desvio, parametros, 1.0, 70.0)
            self.assertIn("1 grau.", mensagem_1)
            self.assertNotIn("1 graus", mensagem_1)
            self.assertIn("zero grau", gd.gerar_mensagem_fisiologica(
                SimpleNamespace(status=2, motivo_temperatura="GEADA_AGORA", motivo_solo="NENHUM", status_solo=0),
                0.0, parametros, 0.2, 70.0))

    def test_borda_proxima_usa_falar_graus(self) -> None:
        """A frase da borda de calor/frio também usa ``falar_graus``."""
        faixa_t = FaixaParametro("temperatura_ar", "°C", 0.6, 1.4, -1.0, 3.0, -2.0, 4.0)
        faixa_s = FaixaParametro("umidade_solo", "%", 60.0, 80.0, 50.0, 90.0, 40.0, 95.0)
        frase = gd._frase_borda_proxima(1.0, 70.0, faixa_t, faixa_s)
        self.assertIn("Está 1 grau,", frase)

    def test_certeza_da_geada_sem_geie(self) -> None:
        """FRASES_CERTEZA da geada: "tenha geada"; nenhuma frase de certeza usa "geie"."""
        self.assertEqual(pg.FRASES_CERTEZA["GEADA_NOITE"], {"quase_certo": "É quase certo que tenha geada {prazo}.",
                                                            "pode_ser": "Pode ser que tenha geada {prazo}."})
        for frases in pg.FRASES_CERTEZA.values():
            for frase in frases.values():
                self.assertNotIn("geie", frase)
        self.assertTrue(ao.causa_coerente_com_mensagem("FRIO_GEADA", "Pode ser que tenha geada esta noite."))

    def test_antecipacao_coerente_na_grade(self) -> None:
        """Todo motivo × prazo × hora × probabilidade (leitura ideal): sem "geie"; causa e ações coerentes."""
        total = 0
        for parametros in self.culturas:
            temperatura = _ponto_medio(parametros.faixa("temperatura_ar"))
            solo = _ponto_medio(parametros.faixa("umidade_solo"))
            for motivo, prazo, hora, probabilidade in itertools.product(MOTIVOS_ANTECIPACAO, PRAZOS, HORAS,
                                                                       PROBABILIDADES):
                mensagem = mensagem_antecipada(parametros, motivo, prazo, None, hora, probabilidade, temperatura, solo)
                causa = ao.derivar_causa(1, {"temperatura_ar": temperatura, "umidade_solo": solo}, parametros, motivo,
                                         mensagem_voz=mensagem)
                acoes = ao.derivar_acoes(1, temperatura, solo, parametros, motivo_antecipacao=motivo,
                                         mensagem_voz=mensagem)
                total += 1
                with self.subTest(cultura=parametros.codigo, motivo=motivo, prazo=prazo, hora=hora,
                                  probabilidade=probabilidade, mensagem=mensagem):
                    self.assertNotIn("geie", mensagem)
                    self.assertTrue(ao.causa_coerente_com_mensagem(causa, mensagem), causa)
                    self.assertTrue(ao.acoes_coerentes_com_mensagem(acoes, mensagem), acoes)
                    self.assertTrue(acoes)
        self.assertEqual(total, 2 * len(MOTIVOS_ANTECIPACAO) * len(PRAZOS) * len(HORAS) * len(PROBABILIDADES))


# ---------------------------------------------------------------------------
# B3. Uma ordem por ação
# ---------------------------------------------------------------------------
class TesteUmaOrdemPorAcao(BaseConteudo):
    """Sem rega repetida, sem "Regue agora" na geada e sem mudar nenhum status."""

    def test_espaco_sem_rega_repetida_nem_contraditoria(self) -> None:
        """No espaço de B1: ≤ 1 ordem de rega, nada de "gelando agora" + "Regue agora", ações coerentes."""
        for item in self.espaco:
            with self.subTest(origem=item.origem, mensagem=item.mensagem):
                self.assertLessEqual(len(PADRAO_REGA.findall(item.mensagem)), 1)
                self.assertFalse("gelando agora" in item.mensagem and "Regue agora" in item.mensagem)
                self.assertTrue(ao.acoes_coerentes_com_mensagem(list(item.acoes), item.mensagem), item.acoes)

    def test_complementos_da_antecipacao(self) -> None:
        """Amarelo da regra + antecipação: a rega do complemento não repete nem contradiz a do motivo."""
        for parametros in self.culturas:
            faixa_t, faixa_s = parametros.faixa("temperatura_ar"), parametros.faixa("umidade_solo")
            meio_t, meio_s = _ponto_medio(faixa_t), _ponto_medio(faixa_s)
            casos = [("CALOR_TARDE", meio_t, faixa_s.ideal_min - 1.0, False),
                     ("CALOR_TARDE", meio_t, faixa_s.ideal_max + 1.0, False),
                     ("SECA_CHEGANDO", faixa_t.ideal_max + 1.0, meio_s, False),
                     ("ENCHARCAMENTO", faixa_t.ideal_max + 1.0, meio_s, False),
                     ("GEADA_NOITE", meio_t, faixa_s.ideal_min - 1.0, True)]
            for (motivo, temperatura, solo, fisiologica), hora, probabilidade in itertools.product(
                    casos, HORAS, PROBABILIDADES):
                mensagem = mensagem_antecipada(parametros, motivo, 14, 5, hora, probabilidade, temperatura, solo,
                                               fisiologica)
                acoes = ao.derivar_acoes(1, temperatura, solo, parametros, motivo_antecipacao=motivo,
                                         mensagem_voz=mensagem)
                with self.subTest(cultura=parametros.codigo, motivo=motivo, mensagem=mensagem):
                    self.assertLessEqual(len(PADRAO_REGA.findall(mensagem)), 1)
                    self.assertTrue(ao.acoes_coerentes_com_mensagem(acoes, mensagem), acoes)
                    self.assertTrue(ao.acoes_cobrem_mensagem(acoes, mensagem), acoes)
            # Calor chegando com a terra molhada demais: a sombra fica, a rega para refrescar sai.
            molhada = mensagem_antecipada(parametros, "CALOR_TARDE", 14, None, 8, None, meio_t,
                                          faixa_s.ideal_max + 1.0)
            self.assertIn("Faça sombra nas mudas novas.", molhada)
            self.assertIn("Não regue hoje.", molhada)
            self.assertIsNone(re.search(r"(?<![Nn]ão )\b[Rr]egue\b", molhada))

    def test_regra_fisiologica_com_calor_e_solo(self) -> None:
        """Seca + calor na florada: uma rega (com a sombra); encharcado + calor: só a sombra e "Pare de regar"."""
        parametros = self.culturas[0]
        seca = gd.gerar_mensagem_fisiologica(
            SimpleNamespace(status=2, motivo_temperatura="CALOR_FLORADA", motivo_solo="SECA_AGORA", status_solo=2),
            10.0, parametros, 35.0, 20.0)
        self.assertEqual(len(PADRAO_REGA.findall(seca)), 1)
        self.assertIn("Regue no fim da tarde e faça sombra nas mudas.", seca)
        self.assertNotIn("Prefira o fim da tarde", seca)
        encharcado = gd.gerar_mensagem_fisiologica(
            SimpleNamespace(status=2, motivo_temperatura="CALOR_FLORADA", motivo_solo="ENCHARCAMENTO", status_solo=2),
            -10.0, parametros, 35.0, 97.0)
        self.assertIn("Pare de regar e abra valetas para a água escorrer.", encharcado)
        self.assertIn("Faça sombra nas mudas.", encharcado)
        self.assertIsNone(re.search(r"(?<![Nn]ão )\b[Rr]egue\b", encharcado))
        acoes = ao.derivar_acoes(2, 35.0, 97.0, parametros, mensagem_voz=encharcado, motivo_regra="CALOR_FLORADA")
        self.assertEqual(acoes, ["NAO_REGAR", "ABRIR_VALETAS", "FAZER_SOMBRA"])
        seguido = gd.gerar_mensagem_fisiologica(
            SimpleNamespace(status=2, motivo_temperatura="CALOR_SEGUIDO", motivo_solo="SECA_AGORA", status_solo=1),
            5.0, parametros, 35.0, 50.0)
        self.assertEqual(len(PADRAO_REGA.findall(seguido)), 1)

    def test_geada_com_terra_seca(self) -> None:
        """Geada + terra seca: proteção primeiro, "Regue de dia, quando o sol sair." e a figura da rega da manhã."""
        self.assertIn("quando o sol sair", ao.PALAVRAS_CHAVE_ACAO["REGAR_MANHA"])
        for parametros in self.culturas:
            for temperatura, solo in ((1.0, 20.0), (3.0, 30.0), (-2.0, 25.0)):
                status = gd.calcular_status_regra(temperatura, solo, parametros)
                deficit, desvio = gd.calcular_features(temperatura, solo, parametros)
                v2 = gd.gerar_mensagem_orientacao(status, deficit, desvio, parametros, temperatura, solo)
                fisiologica = gd.gerar_mensagem_fisiologica(
                    SimpleNamespace(status=2, motivo_temperatura="GEADA_AGORA", motivo_solo="SECA_AGORA",
                                    status_solo=gd.classificar_variavel(solo, parametros.faixa("umidade_solo"))),
                    deficit, parametros, temperatura, solo)
                for mensagem in (v2, fisiologica):
                    with self.subTest(cultura=parametros.codigo, temperatura=temperatura, mensagem=mensagem):
                        self.assertIn("gelando agora", mensagem)
                        self.assertNotIn("Regue agora", mensagem)
                        self.assertIn(gd.FRASE_REGA_NA_GEADA, mensagem)
                        self.assertLess(mensagem.index("Cubra as mudas"), mensagem.index(gd.FRASE_REGA_NA_GEADA))
                        self.assertNotIn("Não regue à noite", mensagem)
                acoes = ao.derivar_acoes(status, temperatura, solo, parametros, mensagem_voz=v2)
                self.assertEqual(acoes, ["COBRIR_MUDAS", "CHEGAR_TERRA", "REGAR_MANHA"])
                # Sem a mensagem (como o teste de propriedade da interface), as figuras também seguem a voz.
                self.assertEqual(ao.derivar_acoes(status, temperatura, solo, parametros), acoes)

    def test_juntar_acoes_unitario(self) -> None:
        """Os três casos de ``_juntar_acoes`` e a frase que não muda."""
        self.assertEqual(gd._juntar_acoes(["Atenção.", "Regue hoje no fim da tarde."]),
                         ["Atenção.", "Regue hoje no fim da tarde."])
        self.assertEqual(
            gd._juntar_acoes(["Atenção.", "Faça sombra nas mudas novas e regue no fim da tarde para refrescar.",
                              "Regue hoje no fim da tarde."]),
            ["Atenção.", "Faça sombra nas mudas novas e regue no fim da tarde para refrescar."])
        self.assertEqual(
            gd._juntar_acoes(["Atenção.", "Calor forte há dois dias. Regue no fim da tarde.",
                              "Não regue hoje. Espere a terra secar um pouco."]),
            ["Atenção.", "Calor forte há dois dias.", "Não regue hoje.", "Espere a terra secar um pouco."])
        self.assertEqual(
            gd._juntar_acoes(["Perigo.", "Regue agora, de dia, até a terra ficar úmida. Não regue à noite.",
                              "Está gelando agora. Cubra as mudas e chegue terra no pé das plantas assim que puder."]),
            ["Perigo.", "Está gelando agora.", "Cubra as mudas e chegue terra no pé das plantas assim que puder.",
             "Regue de dia, quando o sol sair."])
        # Frio sem geada (acima do crítico): "Regue agora, de dia" fica como na v2.
        sem_geada = ["Perigo.", "Regue agora, de dia, até a terra ficar úmida. Não regue à noite.",
                     "Cubra as mudas novas e chegue terra no pé das plantas antes de escurecer."]
        self.assertEqual(" ".join(gd._juntar_acoes(sem_geada)), " ".join(sem_geada))

    def test_status_igual_ao_commit_5c66abe(self) -> None:
        """A tabela de status (v2 + fisiológica) é idêntica à calculada no commit 5c66abe."""
        linhas = tabela_de_status(self.culturas)
        self.assertEqual(len(linhas), LINHAS_STATUS_5C66ABE)
        self.assertEqual(hashlib.sha256("\n".join(linhas).encode("utf-8")).hexdigest(), RESUMO_STATUS_5C66ABE)
        for item in self.espaco:
            if item.origem.startswith("v2 "):
                status = gd.calcular_status_regra(item.temperatura, item.solo,
                                                  self.culturas[0] if "arabica" in item.origem else self.culturas[1])
                self.assertIn(item.nivel, (status, max(status, 1)))


# ---------------------------------------------------------------------------
# B4. Voz → figura
# ---------------------------------------------------------------------------
class TesteVozViraFigura(BaseConteudo):
    """``acoes_cobrem_mensagem``: toda ação que a voz pede tem pictograma."""

    def test_calor_antecipado_tem_sombra(self) -> None:
        """CALOR_TARDE inclui FAZER_SOMBRA antes da rega; a 20 °C e 70 % a figura da sombra aparece."""
        self.assertEqual(ao.ACOES_POR_MOTIVO["CALOR_TARDE"], ("FAZER_SOMBRA", "REGAR_FIM_DA_TARDE"))
        self.assertLessEqual(max(len(acoes) for acoes in ao.ACOES_POR_MOTIVO.values()), ao.MAXIMO_ACOES)
        for parametros in self.culturas:
            frase = mensagem_antecipada(parametros, "CALOR_TARDE", 14, None, 8, None, 20.0, 70.0)
            self.assertIn("Faça sombra", frase)
            com_voz = ao.derivar_acoes(1, 20.0, 70.0, parametros, motivo_antecipacao="CALOR_TARDE", mensagem_voz=frase)
            sem_voz = ao.derivar_acoes(1, 20.0, 70.0, parametros, motivo_antecipacao="CALOR_TARDE")
            for acoes in (com_voz, sem_voz):
                self.assertIn("FAZER_SOMBRA", acoes)
                self.assertTrue(ao.acoes_cobrem_mensagem(acoes, frase))

    def test_acoes_cobrem_mensagem_unitario(self) -> None:
        """Família de rega, prazo que não é ordem, ressalva "à noite" e no máximo três ações."""
        mensagem = "Atenção. Faça sombra nas mudas novas e regue no fim da tarde para refrescar."
        self.assertTrue(ao.acoes_cobrem_mensagem(["FAZER_SOMBRA", "REGAR_FIM_DA_TARDE"], mensagem))
        self.assertTrue(ao.acoes_cobrem_mensagem(["FAZER_SOMBRA", "REGAR_AGORA"], mensagem))  # mesma família
        self.assertFalse(ao.acoes_cobrem_mensagem(["REGAR_FIM_DA_TARDE"], mensagem))
        self.assertFalse(ao.acoes_cobrem_mensagem([], mensagem))
        # "amanhã de manhã" é o prazo, não uma ordem de regar.
        self.assertTrue(ao.acoes_cobrem_mensagem(
            ["COBRIR_MUDAS", "CHEGAR_TERRA"],
            "É quase certo que tenha geada amanhã de manhã. Cubra as mudas novas e chegue terra no pé das plantas."))
        # "Não regue à noite" é o horário da rega, não a ordem de não regar.
        self.assertEqual(ao.acoes_faladas("Regue hoje de manhã. Não regue à noite."), ["REGAR_MANHA"])
        self.assertEqual(ao.acoes_faladas("Não regue hoje. Espere a terra secar um pouco."), ["NAO_REGAR"])
        # Nada a fazer e conferir o aparelho não pedem figura própria.
        self.assertTrue(ao.acoes_cobrem_mensagem([], "Tudo bem. Não precisa fazer nada agora."))
        self.assertTrue(ao.acoes_cobrem_mensagem([], "Confira o cabo do aparelho."))
        # Mais de três ordens: bastam as três primeiras ditas.
        quatro = "Pare de regar e abra valetas. Faça sombra nas mudas. Cubra as mudas."
        self.assertEqual(ao.acoes_faladas(quatro), ["NAO_REGAR", "ABRIR_VALETAS", "FAZER_SOMBRA"])
        self.assertTrue(ao.acoes_cobrem_mensagem(["NAO_REGAR", "ABRIR_VALETAS", "FAZER_SOMBRA"], quatro))
        self.assertFalse(ao.acoes_cobrem_mensagem(["NAO_REGAR", "ABRIR_VALETAS", "COBRIR_MUDAS"], quatro))

    def test_grade_da_antecipacao(self) -> None:
        """Todo motivo × temperatura 0–38 × solo 20–98 × cultivar: as figuras cobrem a voz da antecipação."""
        verificados = 0
        for parametros in self.culturas:
            for motivo, temperatura, solo in itertools.product(pg.MOTIVOS, range(0, 40, 2), range(20, 100, 6)):
                status = max(1, gd.calcular_status_regra(float(temperatura), float(solo), parametros))
                for hora, probabilidade, fisiologica in ((8, None, False), (19, 0.9, True)):
                    frase = mensagem_antecipada(parametros, motivo, 14, 5, hora, probabilidade, float(temperatura),
                                                float(solo), fisiologica)
                    acoes = ao.derivar_acoes(status, float(temperatura), float(solo), parametros,
                                             motivo_antecipacao=motivo, mensagem_voz=frase)
                    verificados += 1
                    if not ao.acoes_cobrem_mensagem(acoes, frase):
                        self.fail(f"{parametros.codigo} {motivo} T={temperatura} S={solo}: {acoes} × {frase}")
        self.assertEqual(verificados, 2 * len(pg.MOTIVOS) * 20 * 14 * 2)

    def test_espaco_da_regra_v2_e_fisiologica(self) -> None:
        """No espaço de B1 inteiro (v2, fisiológica e antecipação), toda ordem falada tem figura."""
        for item in self.espaco:
            with self.subTest(origem=item.origem, mensagem=item.mensagem):
                self.assertTrue(ao.acoes_cobrem_mensagem(list(item.acoes), item.mensagem), item.acoes)

    def test_subjuntivo_depois_de_que_nao_e_ordem(self) -> None:
        """"Que faça muito calor amanhã de manhã" é a previsão: a figura e a fala curta seguem a ordem dita depois.

        Achado na verificação final da v4 (estado ``antecipa_calor`` da suíte): o padrão de ordem
        aceitava o subjuntivo "faça" e o prazo "de manhã" virava a figura "Manhã" e a fala curta
        "Regue de manhã", enquanto a voz completa pede "regue no fim da tarde" (24 itens do espaço).
        """
        parametros = self.culturas[0]
        for certeza in ("É quase certo que", "Pode ser que"):
            frase = (f"Atenção com a plantação de café arábica. {certeza} faça muito calor amanhã de manhã. "
                     "Faça sombra nas mudas novas e regue no fim da tarde para refrescar.")
            self.assertEqual(ao.acoes_faladas(frase), ["FAZER_SOMBRA", "REGAR_FIM_DA_TARDE"])
            acoes = ao.derivar_acoes(1, 20.0, 70.0, parametros, motivo_antecipacao="CALOR_TARDE", mensagem_voz=frase)
            self.assertEqual(acoes, ["FAZER_SOMBRA", "REGAR_FIM_DA_TARDE"])
            self.assertNotIn("de manhã", ao.montar_fala_curta(1, acoes, "CALOR"))
        # O imperativo continua valendo, inclusive no começo da frase.
        self.assertEqual(ao.acoes_faladas("Faça sombra nas mudas."), ["FAZER_SOMBRA"])
        self.assertEqual(ao.acoes_faladas("Regue hoje de manhã."), ["REGAR_MANHA"])
        # No espaço inteiro da auditoria, a rega da figura é a mesma da ordem falada na voz completa.
        for item in self.espaco:
            regas_figura = [acao for acao in item.acoes if acao in ao.ACOES_DE_REGA]
            regas_voz = [acao for acao in ao.acoes_faladas(item.mensagem) if acao in ao.ACOES_DE_REGA]
            if regas_figura and regas_voz:
                with self.subTest(origem=item.origem):
                    self.assertEqual(regas_figura, regas_voz, item.mensagem)
            if "que faça muito calor" in item.mensagem and "fim da tarde" in item.mensagem:
                with self.subTest(origem=item.origem):
                    self.assertNotIn("REGAR_MANHA", item.acoes, item.mensagem)

    def test_figuras_na_ordem_da_voz(self) -> None:
        """Com a mensagem falada, as figuras vêm na ordem das ordens ditas."""
        parametros = self.culturas[0]
        frase = "Atenção. Faça sombra nas mudas novas e regue no fim da tarde para refrescar."
        self.assertEqual(ao.derivar_acoes(1, 20.0, 70.0, parametros, motivo_antecipacao="CALOR_TARDE",
                                          mensagem_voz=frase), ["FAZER_SOMBRA", "REGAR_FIM_DA_TARDE"])


if __name__ == "__main__":
    unittest.main()
