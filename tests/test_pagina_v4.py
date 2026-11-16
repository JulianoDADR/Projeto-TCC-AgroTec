"""Testes da página do agricultor v4 (Frente A: entrega da página e API da página).

Cobrem, sempre com banco SQLite TEMPORÁRIO:

    * A1 — entrega em duas partes: ``GET /`` com gzip ≤ 15.000 B no pior caso,
      um único link para ``/static/agro.<hash>.css`` (cache imutável de um
      ano, gzip, ≤ 6.144 B), HTML + CSS ≤ 21.000 B, outro hash → 404 e nenhum
      recurso de outro host;
    * A4/A5 — ``meta color-scheme`` "only light", ``meta text-scale``,
      ``overscroll-behavior-y:contain`` e nenhum ``font-size`` em px no CSS
      (inline e estático);
    * A6 — ``fala_curta`` em ``/api/status`` (≤ 15 palavras, sem números, com a
      ação na 2ª frase) e no estado embutido;
    * A7 — "Feito" sem duplicata, desfazer (200/404/409) e as respostas do PIN
      com ``codigo`` e ``segundos_restantes``;
    * A8 — ícone distinto por cultura em ``/api/culturas`` e o gabarito ISO
      9186-1 dos pictogramas com uma linha por código.

Execução (a partir da raiz do projeto): ``python -m unittest tests.test_pagina_v4 -v``.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

RAIZ_PROJETO: Path = Path(__file__).resolve().parent.parent
PASTA_SRC: Path = RAIZ_PROJETO / "src"
if str(PASTA_SRC) not in sys.path:
    sys.path.insert(0, str(PASTA_SRC))

import acoes_orientacao  # noqa: E402
import app as modulo_app  # noqa: E402
import calibracao_sonda  # noqa: E402
import retorno_agricultor  # noqa: E402
import seguranca_banca  # noqa: E402
from app import app  # noqa: E402
from database import (  # noqa: E402
    RegistroLeitura,
    buscar_parametros_cultura,
    inicializar_banco,
    inserir_leitura,
    obter_conexao,
)
from gerador_dataset import calcular_features  # noqa: E402

IP_TESTE: str = "127.0.0.1"
META_PAGINA_GZIP_V4: int = 15_000
META_CSS_GZIP: int = 6_144
META_PAGINA_MAIS_CSS: int = 21_000
CENARIOS_PIOR_CASO = (None, "SECA_EXTREMA", "ONDA_DE_CALOR", "GEADA")
PADRAO_LINK_CSS = re.compile(r'href="(/static/agro\.[0-9a-f]{10}\.css)"')
CAMINHO_GABARITO: Path = RAIZ_PROJETO / "tests" / "dados" / "pictogramas_iso9186.csv"
COLUNAS_GABARITO: List[str] = ["codigo", "simbolo", "significado_pretendido", "resposta_correta_exemplo",
                               "resposta_oposta_critica", "n_amostra", "pct_correta", "pct_oposta", "aprovado"]
SELOS_QUANDO: tuple = ("QUANDO_AGORA", "QUANDO_MANHA", "QUANDO_FIM_DA_TARDE")
VERBOS_ACAO = re.compile(r"Regue|Não regue|Pare|Abra|Faça|Cubra|Chegue|Proteja|Confira|Verifique|Olhe|Nada|Não precisa")


def setUpModule() -> None:
    """Silencia os logs informativos/avisos durante os testes."""
    logging.disable(logging.WARNING)


def tearDownModule() -> None:
    """Reativa os logs."""
    logging.disable(logging.NOTSET)


class BasePagina(unittest.TestCase):
    """Banco temporário, app apontado para ele (sem modelo), limitadores e caches limpos."""

    def setUp(self) -> None:
        """Cria o banco temporário e o cliente de testes."""
        self._diretorio = tempfile.mkdtemp(prefix="agro_pagina_v4_")
        self.pasta = Path(self._diretorio)
        self.banco: Path = self.pasta / "teste.db"
        inicializar_banco(self.banco)
        self._config_original = (app.config["CAMINHO_BANCO"], app.config["CAMINHO_MODELO"])
        app.config["CAMINHO_BANCO"] = self.banco
        app.config["CAMINHO_MODELO"] = self.pasta / "sem_modelo.pkl"
        app.config["TESTING"] = True
        self.cliente = app.test_client()
        self.limpar_limitadores()
        calibracao_sonda.limpar_cache()
        modulo_app.registrar_aparelho_banca("")

    def tearDown(self) -> None:
        """Restaura o app e remove o diretório temporário."""
        app.config["CAMINHO_BANCO"], app.config["CAMINHO_MODELO"] = self._config_original
        self.limpar_limitadores()
        calibracao_sonda.limpar_cache()
        modulo_app.registrar_aparelho_banca("")
        shutil.rmtree(self._diretorio, ignore_errors=True)

    @staticmethod
    def limpar_limitadores() -> None:
        """Esquece os pedidos e os erros de PIN do IP dos testes."""
        modulo_app._LIMITADOR_PIN.limpar(IP_TESTE)
        for limitador in (modulo_app._LIMITADOR_FEITO, modulo_app._LIMITADOR_AJUDA, modulo_app._LIMITADOR_EVENTOS):
            limitador.limpar()

    def inserir_leitura(self, status: int = 2, origem: str = "CAMPO_USB", temperatura: float = 36.0,
                        umidade_solo: float = 15.0) -> int:
        """Grava uma leitura recente do Arábica (padrão: vermelho por terra seca) e devolve o id."""
        parametros = buscar_parametros_cultura("cafe_arabica", caminho_banco=self.banco)
        deficit, desvio = calcular_features(temperatura, umidade_solo, parametros)
        agora = datetime.now(timezone.utc)
        return inserir_leitura(RegistroLeitura(
            data_hora=agora.astimezone().replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S"),
            codigo_cultura="cafe_arabica", origem=origem, temperatura_ar_bruta=temperatura, umidade_ar_bruta=60.0,
            umidade_solo_bruta=umidade_solo, temperatura_ar=temperatura, umidade_ar=60.0, umidade_solo=umidade_solo,
            deficit_hidrico_solo=deficit, desvio_termico=desvio, status_regra=status, status_predito=status,
            status_alerta=status, probabilidade_predicao=1.0, flag_anomalia=0, descricao_anomalia="",
            mensagem_voz="Perigo. A terra está seca. Regue agora.", versao_modelo="REGRAS", estado_sensor="OK",
            instante_utc=agora.isoformat(timespec="seconds"),
        ), caminho_banco=self.banco)

    def definir_pin(self) -> None:
        """Grava o PIN '1234' pela linha de comando da segurança da banca."""
        with redirect_stdout(io.StringIO()):
            self.assertEqual(seguranca_banca.main(["--definir-pin", "1234", "--banco", str(self.banco)]), 0)

    def pagina(self, cenario: Optional[str] = None) -> bytes:
        """Corpo gzip de ``GET /`` (com o cenário de demonstração pedido ligado antes)."""
        if cenario:
            self.assertEqual(self.cliente.post("/api/demonstracao", json={"cenario": cenario}).status_code, 200)
        resposta = self.cliente.get("/", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.headers.get("Content-Encoding"), "gzip")
        return resposta.data

    def html(self) -> str:
        """HTML de ``GET /`` sem compressão."""
        return self.cliente.get("/").get_data(as_text=True)

    def css_estatico(self) -> str:
        """CSS servido no link da página (sem compressão)."""
        url = PADRAO_LINK_CSS.search(self.html())
        self.assertIsNotNone(url)
        return self.cliente.get(url.group(1)).get_data(as_text=True)


# ---------------------------------------------------------------------------
# A1 — entrega da página
# ---------------------------------------------------------------------------
class TesteEntregaPagina(BasePagina):
    """CSS crítico inline + CSS estático imutável; orçamento de bytes da v4."""

    def test_a_pagina_gzip_cabe_na_meta_v4(self) -> None:
        """(a) GET / com gzip ≤ 15.000 B nos cenários None, SECA_EXTREMA, ONDA_DE_CALOR e GEADA."""
        self.inserir_leitura()
        for cenario in CENARIOS_PIOR_CASO:
            with self.subTest(cenario=cenario):
                self.assertLessEqual(len(self.pagina(cenario)), META_PAGINA_GZIP_V4)

    def test_b_um_link_css_e_nada_de_outro_host(self) -> None:
        """(b) Exatamente um link para /static/agro.<hash>.css e nenhum href/src para outro host."""
        html = self.html()
        self.assertEqual(len(PADRAO_LINK_CSS.findall(html)), 2)  # o <link> e a cópia dentro do <noscript>
        self.assertEqual(len(set(PADRAO_LINK_CSS.findall(html))), 1)
        self.assertEqual(len(re.findall(r'<link rel="stylesheet"', html)), 2)
        self.assertIn('media="print" onload="this.media=\'all\'"', html)
        self.assertRegex(html, r"<noscript><link rel=\"stylesheet\" href=\"/static/agro\.[0-9a-f]{10}\.css\"></noscript>")
        # Atributos da marcação (fora dos <script>, onde só há montagem de ícones com "#i-...").
        marcacao = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.S)
        externos = [url for url in re.findall(r'(?:href|src)="([^"#]+)"', marcacao) if not url.startswith("/")]
        self.assertEqual(externos, [])
        # Nos scripts, nenhum endereço de outro host (o link do WhatsApp vem do servidor, na hora do toque).
        self.assertIsNone(re.search(r"https?://", "".join(re.findall(r"<script\b[^>]*>(.*?)</script>", html, flags=re.S))))
        self.assertNotRegex(html, r"(?:href|src)=\"//")
        self.assertNotIn("<script src=", html)

    def test_c_css_estatico_imutavel_e_comprimido(self) -> None:
        """(c) 200, Cache-Control immutable + max-age=31536000, gzip e ≤ 6.144 B."""
        url = PADRAO_LINK_CSS.search(self.html()).group(1)
        resposta = self.cliente.get(url, headers={"Accept-Encoding": "gzip"})
        self.assertEqual(resposta.status_code, 200)
        controle = resposta.headers.get("Cache-Control", "")
        self.assertIn("immutable", controle)
        self.assertIn("max-age=31536000", controle)
        self.assertIn("public", controle)
        self.assertEqual(resposta.headers.get("Content-Encoding"), "gzip")
        self.assertTrue(resposta.headers["Content-Type"].startswith("text/css"))
        self.assertIn("charset=utf-8", resposta.headers["Content-Type"])
        self.assertLessEqual(len(resposta.data), META_CSS_GZIP)
        self.assertIn(".dialogo", gzip.decompress(resposta.data).decode("utf-8"))
        self.assertEqual(url, modulo_app.URL_CSS_AGRICULTOR)

    def test_d_pagina_mais_css_cabe_no_total(self) -> None:
        """(d) HTML gzip + CSS gzip ≤ 21.000 B no pior caso."""
        self.inserir_leitura()
        css = self.cliente.get(modulo_app.URL_CSS_AGRICULTOR, headers={"Accept-Encoding": "gzip"}).data
        for cenario in CENARIOS_PIOR_CASO:
            with self.subTest(cenario=cenario):
                self.assertLessEqual(len(self.pagina(cenario)) + len(css), META_PAGINA_MAIS_CSS)

    def test_e_outro_hash_da_404(self) -> None:
        """(e) /static/agro.0000000000.css → 404; o hash é o sha256 (10 caracteres) do CSS minificado."""
        self.assertEqual(self.cliente.get("/static/agro.0000000000.css").status_code, 404)
        self.assertEqual(self.cliente.get("/static/outro.css").status_code, 404)
        hash_css, conteudo = modulo_app.carregar_css_agricultor()
        self.assertRegex(hash_css, r"^[0-9a-f]{10}$")
        self.assertEqual(modulo_app.URL_CSS_AGRICULTOR, f"/static/agro.{hash_css}.css")
        self.assertEqual(self.cliente.get(modulo_app.URL_CSS_AGRICULTOR).data, conteudo)

    def test_css_ausente_nao_derruba_a_pagina(self) -> None:
        """Sem o arquivo de CSS, o carregamento devolve CSS vazio (a página vive só do CSS crítico)."""
        hash_css, conteudo = modulo_app.carregar_css_agricultor(self.pasta / "nao_existe.css")
        self.assertEqual(conteudo, b"")
        self.assertRegex(hash_css, r"^[0-9a-f]{10}$")

    def test_css_critico_inline_pequeno(self) -> None:
        """O <style> inline (só a primeira tela) fica em até 2.000 B gzip; o JS continua inline."""
        html = self.html()
        estilos = re.findall(r"<style>(.*?)</style>", html, flags=re.S)
        self.assertEqual(len(estilos), 1)
        self.assertLessEqual(len(gzip.compress(estilos[0].encode("utf-8"))), 2_000)
        self.assertIn("speechSynthesis", html)
        self.assertNotIn("serviceWorker", html)

    def test_text_css_comprimivel(self) -> None:
        """text/css entrou nos tipos comprimidos."""
        self.assertIn("text/css", modulo_app.TIPOS_COMPRIMIVEIS)


# ---------------------------------------------------------------------------
# A4/A5 — robustez e texto
# ---------------------------------------------------------------------------
class TesteRobustezETexto(BasePagina):
    """Metas de cor, rolagem e tamanho de texto verificáveis no HTML/CSS servidos."""

    def test_meta_color_scheme_e_overscroll(self) -> None:
        """meta color-scheme 'only light' e overscroll-behavior-y:contain (inline ou estático)."""
        html = self.html()
        self.assertIn('<meta name="color-scheme" content="only light">', html)
        self.assertIn("overscroll-behavior-y:contain", html + self.css_estatico())

    def test_meta_text_scale_e_sem_font_size_em_px(self) -> None:
        """meta text-scale presente e nenhum font-size em px no CSS minificado (inline e estático)."""
        html = self.html()
        self.assertIn('<meta name="text-scale" content="scale">', html)
        inline = "".join(re.findall(r"<style>(.*?)</style>", html, flags=re.S))
        estatico = self.css_estatico()
        for nome, css in (("inline", inline), ("estático", estatico)):
            with self.subTest(css=nome):
                self.assertRegex(css, r"font-size:[\d.]+rem")
                self.assertIsNone(re.search(r"font-size:[\d.]+px", css))
                self.assertIsNone(re.search(r"font-size:clamp\([^)]*\d+px", css))

    def test_forced_colors_no_css(self) -> None:
        """Cores forçadas: o semáforo não é repintado e a barra de voz ganha fundo Canvas."""
        css = self.css_estatico()
        self.assertIn("@media (forced-colors:active)", css)
        self.assertIn("forced-color-adjust:none", css)
        self.assertIn("background:Canvas", css)


# ---------------------------------------------------------------------------
# A6 — fala curta
# ---------------------------------------------------------------------------
class TesteFalaCurta(BasePagina):
    """``fala_curta``: sinal e ação primeiro, até 15 palavras, sem números."""

    def test_fala_curta_nos_cenarios(self) -> None:
        """Em cada cenário da banca: ≤ 15 palavras, sem dígitos, 'Sinal' e a ação até a 2ª frase."""
        for cenario in modulo_app.CENARIOS_DEMONSTRACAO:
            with self.subTest(cenario=cenario):
                dados = self.cliente.post("/api/demonstracao", json={"cenario": cenario}).get_json()
                fala = self.cliente.get("/api/status").get_json()["fala_curta"]
                self.assertEqual(fala, dados["fala_curta"])
                self.assertLessEqual(len(fala.split()), 15, fala)
                self.assertIsNone(re.search(r"\d", fala), fala)
                frases = [frase for frase in re.split(r"(?<=\.)\s+", fala) if frase]
                self.assertTrue(frases[0].startswith("Sinal "), fala)
                self.assertRegex(" ".join(frases[:2]), VERBOS_ACAO, fala)

    def test_fala_curta_sem_leitura_e_teste(self) -> None:
        """Sem leitura e com dados de teste a fala começa por 'Atenção.' e não diz 'Sinal'."""
        sem_leitura = self.cliente.get("/api/status").get_json()["fala_curta"]
        self.assertTrue(sem_leitura.startswith("Atenção."), sem_leitura)
        self.inserir_leitura(origem="SIMULACAO")
        teste = self.cliente.get("/api/status").get_json()
        self.assertTrue(teste["dados_simulados"])
        self.assertEqual(teste["status_alerta"], 2)  # a API continua com o status da leitura simulada
        self.assertTrue(teste["fala_curta"].startswith("Atenção."), teste["fala_curta"])
        self.assertNotIn("Sinal", teste["fala_curta"])

    def test_fala_curta_no_estado_embutido(self) -> None:
        """O estado embutido na página leva a fala curta (1º toque sem esperar a rede)."""
        self.cliente.post("/api/demonstracao", json={"cenario": "GEADA"})
        html = self.html()
        inicio = html.index('id="estado-inicial">') + len('id="estado-inicial">')
        estado = json.loads(html[inicio:html.index("</script>", inicio)])
        self.assertTrue(estado["fala_curta"].startswith("Sinal vermelho."))
        self.assertTrue(estado["aparelho_da_banca"])

    def test_demonstracao_de_outro_aparelho(self) -> None:
        """Quem ligou a demonstração é a banca; outro aparelho da rede recebe aparelho_da_banca=false."""
        self.cliente.post("/api/demonstracao", json={"cenario": "GEADA"})
        self.assertTrue(self.cliente.get("/api/status").get_json()["aparelho_da_banca"])
        outro = self.cliente.get("/api/status", environ_base={"REMOTE_ADDR": "192.168.0.50"}).get_json()
        self.assertFalse(outro["aparelho_da_banca"])
        self.assertEqual(outro["status_alerta"], 2)
        self.cliente.post("/api/demonstracao/encerrar", json={})
        self.assertFalse(self.cliente.get("/api/status").get_json()["aparelho_da_banca"])

    def test_fala_curta_vem_da_frente_b(self) -> None:
        """A fala curta do status é a de ``acoes_orientacao.montar_fala_curta`` com a situação certa."""
        self.inserir_leitura(origem="SIMULACAO")
        status = modulo_app.montar_status(self.banco)
        esperada = acoes_orientacao.montar_fala_curta(status["status_alerta"], status["acoes"], status["causa"],
                                                      situacao=acoes_orientacao.SITUACAO_TESTE)
        self.assertEqual(status["fala_curta"], esperada)
        # Nível fora de 0–2 não derruba o status: sai a fala de conferir o aparelho, sem números.
        self.assertEqual(modulo_app.montar_fala_curta(7, ["COBRIR_MUDAS"], "FRIO_GEADA"),
                         acoes_orientacao.FALAS_SITUACAO[acoes_orientacao.SITUACAO_SEM_LEITURA])


# ---------------------------------------------------------------------------
# A7 — "Feito" seguro e PIN claro
# ---------------------------------------------------------------------------
class TesteFeitoEPin(BasePagina):
    """Desfazer, sem duplicata e respostas do PIN com código."""

    def _feito(self, id_leitura: int) -> Dict[str, object]:
        """Toca "Feito" (rega por terra seca) na leitura dada e devolve {status, json}."""
        resposta = self.cliente.post("/api/acao_feita", json={"codigo_acao": "REGAR_AGORA", "causa": "TERRA_SECA",
                                                              "id_leitura": id_leitura})
        return {"status": resposta.status_code, "json": resposta.get_json()}

    def _linhas_registro(self) -> int:
        """Quantidade de linhas em registro_acoes."""
        conexao = obter_conexao(self.banco)
        try:
            return int(conexao.execute("SELECT COUNT(*) FROM registro_acoes").fetchone()[0])
        finally:
            conexao.close()

    def test_feito_e_desfazer(self) -> None:
        """POST /api/acao_feita e depois /api/acao_feita/desfazer → 200 e causa_silenciada volta a false."""
        id_leitura = self.inserir_leitura()
        feito = self._feito(id_leitura)
        self.assertEqual(feito["status"], 201)
        self.assertTrue(self.cliente.get("/api/status").get_json()["causa_silenciada"])
        desfeito = self.cliente.post("/api/acao_feita/desfazer", json={"id_acao": feito["json"]["id_acao"]})
        self.assertEqual(desfeito.status_code, 200)
        self.assertTrue(desfeito.get_json()["desfeito"])
        self.assertFalse(self.cliente.get("/api/status").get_json()["causa_silenciada"])
        self.assertEqual(self._linhas_registro(), 0)

    def test_desfazer_inexistente_e_fora_do_prazo(self) -> None:
        """Id inexistente → 404; registro com mais de 120 s → 409 'Passou o tempo de desfazer.'."""
        self.assertEqual(self.cliente.post("/api/acao_feita/desfazer", json={"id_acao": 999}).status_code, 404)
        self.assertEqual(self.cliente.post("/api/acao_feita/desfazer", json={"id_acao": "x"}).status_code, 400)
        id_leitura = self.inserir_leitura()
        antigo = retorno_agricultor.registrar_acao(self.banco, "REGAR_AGORA", "TERRA_SECA", id_leitura, IP_TESTE,
                                                   instante_utc=datetime.now(timezone.utc) - timedelta(seconds=200))
        resposta = self.cliente.post("/api/acao_feita/desfazer", json={"id_acao": antigo})
        self.assertEqual(resposta.status_code, 409)
        self.assertEqual(resposta.get_json()["erro"], "Passou o tempo de desfazer.")
        self.assertEqual(self._linhas_registro(), 1)

    def test_id_enorme_e_400_e_nao_500(self) -> None:
        """Integração v4: id acima do INTEGER do SQLite (2⁶³) era 500 (OverflowError); agora é 400."""
        for valor in (2 ** 63, 10 ** 30):
            with self.subTest(valor=valor):
                self.limpar_limitadores()
                self.assertEqual(self.cliente.post("/api/acao_feita/desfazer", json={"id_acao": valor}).status_code,
                                 400)
                self.limpar_limitadores()
                corpo = {"codigo_acao": "REGAR_AGORA", "causa": "TERRA_SECA", "id_leitura": valor}
                self.assertEqual(self.cliente.post("/api/acao_feita", json=corpo).status_code, 400)

    def test_tres_toques_um_registro(self) -> None:
        """3 POST iguais em menos de 60 s → 1 linha; as 2 últimas respostas trazem repetido=true."""
        id_leitura = self.inserir_leitura()
        respostas = [self._feito(id_leitura) for _ in range(3)]
        self.assertEqual(self._linhas_registro(), 1)
        self.assertEqual(respostas[0]["status"], 201)
        self.assertNotIn("repetido", respostas[0]["json"])
        for resposta in respostas[1:]:
            self.assertEqual(resposta["status"], 200)
            self.assertTrue(resposta["json"]["repetido"])
            self.assertEqual(resposta["json"]["id_acao"], respostas[0]["json"]["id_acao"])

    def test_feito_sem_leitura_continua_gravando(self) -> None:
        """Sem ``id_leitura`` não há como saber se é o mesmo alerta: cada toque grava (contrato da v3)."""
        corpo = {"codigo_acao": "COBRIR_MUDAS", "causa": "FRIO_GEADA", "id_leitura": None}
        self.assertEqual([self.cliente.post("/api/acao_feita", json=corpo).status_code for _ in range(2)], [201, 201])
        self.assertEqual(self._linhas_registro(), 2)

    def test_feito_sem_leitura_na_demonstracao_nao_vale_como_real(self) -> None:
        """Integração v4: "Feito" sem ``id_leitura`` durante a demonstração fica ligado à leitura da banca.

        Antes ele contava no resumo do técnico (meta 11: 0 da demonstração) e
        calava por 60 min o alerta real da mesma causa depois da apresentação.
        """
        self.inserir_leitura()
        self.assertEqual(self.cliente.post("/api/demonstracao", json={"cenario": "SECA_EXTREMA"}).status_code, 200)
        status_demo = self.cliente.get("/api/status").get_json()
        corpo = {"codigo_acao": status_demo["acoes"][0], "causa": status_demo["causa"], "id_leitura": None}
        self.assertEqual(self.cliente.post("/api/acao_feita", json=corpo).status_code, 201)
        self.assertEqual(self.cliente.post("/api/demonstracao/encerrar", json={}).status_code, 200)
        status_real = self.cliente.get("/api/status").get_json()
        self.assertEqual(status_real["causa"], status_demo["causa"])
        self.assertFalse(status_real["causa_silenciada"])
        resumo = retorno_agricultor.montar_resumo_tecnico(self.banco, 1, datetime.now(timezone.utc), {})
        self.assertEqual(resumo["acoes_feitas"]["total"], 0)

    def test_repeticao_depois_da_janela_grava_de_novo(self) -> None:
        """Um toque igual 61 s depois do primeiro vira outro registro (janela de 60 s da Frente B)."""
        id_leitura = self.inserir_leitura()
        agora = datetime.now(timezone.utc)
        primeiro = modulo_app.registrar_acao_sem_repetir(self.banco, "REGAR_AGORA", "TERRA_SECA", id_leitura,
                                                         IP_TESTE, agora)
        segundo = modulo_app.registrar_acao_sem_repetir(self.banco, "REGAR_AGORA", "TERRA_SECA", id_leitura,
                                                        IP_TESTE, agora + timedelta(seconds=20))
        terceiro = modulo_app.registrar_acao_sem_repetir(self.banco, "REGAR_AGORA", "TERRA_SECA", id_leitura,
                                                         IP_TESTE, agora + timedelta(seconds=61))
        self.assertEqual(segundo, (primeiro[0], True))
        self.assertFalse(terceiro[1])
        self.assertEqual(self._linhas_registro(), 2)

    def test_pin_errado_necessario_e_bloqueio(self) -> None:
        """PIN errado → 403 pin_errado; sem cabeçalho → pin_necessario; 6º erro → 429 com segundos_restantes."""
        self.definir_pin()
        sem = self.cliente.post("/api/demonstracao", json={"cenario": "GEADA"})
        self.assertEqual(sem.status_code, 403)
        self.assertEqual(sem.get_json()["codigo"], seguranca_banca.CODIGO_PIN_NECESSARIO)
        self.limpar_limitadores()
        errado = {"X-PIN-Banca": "9999"}
        for tentativa in range(5):
            resposta = self.cliente.post("/api/demonstracao", json={"cenario": "GEADA"}, headers=errado)
            self.assertEqual(resposta.status_code, 403, tentativa)
            self.assertEqual(resposta.get_json()["codigo"], "pin_errado")
            self.assertEqual(resposta.get_json()["erro"], "Senha errada. Tente de novo.")
        bloqueado = self.cliente.post("/api/demonstracao", json={"cenario": "GEADA"}, headers=errado)
        self.assertEqual(bloqueado.status_code, 429)
        segundos = bloqueado.get_json()["segundos_restantes"]
        self.assertIsInstance(segundos, int)
        self.assertIn(segundos, range(1, 61))
        self.assertEqual(modulo_app.segundos_bloqueio_pin("10.9.9.9"), 0)


# ---------------------------------------------------------------------------
# A8 — culturas e gabarito dos pictogramas
# ---------------------------------------------------------------------------
class TestePictogramas(BasePagina):
    """Ícone por cultura e gabarito ISO 9186-1."""

    def test_icone_distinto_por_cultura(self) -> None:
        """GET /api/culturas devolve um 'icone' distinto por cultura (Arábica ≠ Conilon)."""
        culturas = self.cliente.get("/api/culturas").get_json()["culturas"]
        self.assertGreaterEqual(len(culturas), 2)
        icones = [cultura["icone"] for cultura in culturas]
        self.assertEqual(len(set(icones)), len(icones))
        por_codigo = {cultura["codigo"]: cultura["icone"] for cultura in culturas}
        self.assertEqual(por_codigo["cafe_arabica"], "#i-cafe-arabica")
        self.assertEqual(por_codigo["cafe_conilon"], "#i-cafe-conilon")
        html = self.html()
        for icone in set(por_codigo.values()):
            self.assertIn(f'id="{icone[1:].split(":")[0]}"', html)

    def test_icone_cultura_por_categoria(self) -> None:
        """Fora do mapa por código, a categoria decide; duas da mesma categoria ficam distintas."""
        culturas = [{"codigo": "tomate", "categoria": "hortalica"}, {"codigo": "alface", "categoria": "hortalica"},
                    {"codigo": "cafe_x", "categoria": "cafe"}]
        icones = [modulo_app.icone_cultura(c["codigo"], c["categoria"], culturas) for c in culturas]
        self.assertEqual(len(set(icones)), 3)
        self.assertEqual(icones[2], "#i-cafe")

    def test_gabarito_iso9186_uma_linha_por_codigo(self) -> None:
        """Toda ação de ACOES_VALIDAS, todo selo de 'quando' e toda causa têm exatamente uma linha."""
        with CAMINHO_GABARITO.open(encoding="utf-8", newline="") as arquivo:
            leitor = csv.DictReader(arquivo)
            self.assertEqual(leitor.fieldnames, COLUNAS_GABARITO)
            linhas = list(leitor)
        codigos = [linha["codigo"] for linha in linhas]
        self.assertEqual(len(codigos), len(set(codigos)))
        esperados = set(acoes_orientacao.ACOES_VALIDAS) | set(acoes_orientacao.CAUSAS_VALIDAS) | set(SELOS_QUANDO)
        self.assertEqual(set(codigos), esperados)
        for linha in linhas:
            with self.subTest(codigo=linha["codigo"]):
                self.assertTrue(linha["simbolo"].startswith("#i-"))
                self.assertTrue(linha["significado_pretendido"] and linha["resposta_oposta_critica"])
                # Resultados vazios até o teste de campo (N ≥ 50, ≥ 85 % de acerto, ≤ 5 % de leitura oposta).
                self.assertEqual((linha["n_amostra"], linha["pct_correta"], linha["pct_oposta"], linha["aprovado"]),
                                 ("", "", "", ""))
        html = self.html()
        for linha in linhas:
            for simbolo in re.findall(r"#(i-[a-z-]+)", linha["simbolo"]):
                self.assertIn(f'id="{simbolo}"', html, linha["codigo"])

    def test_banca_sem_florada_nem_seca(self) -> None:
        """A área da banca é só de demonstração: florada e seca programada foram para /tecnico."""
        html = self.html()
        self.assertNotIn('id="b-seca"', html)
        self.assertNotIn('id="b-florada"', html)
        self.assertIn('href="/tecnico"', html)


if __name__ == "__main__":
    unittest.main()
