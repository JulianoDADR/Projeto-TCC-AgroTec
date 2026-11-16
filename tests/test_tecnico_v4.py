"""Testes da Frente C (v4): página do técnico no padrão e suíte permanente de validação UX.

Cobrem, sem rede e com banco SQLite TEMPORÁRIO:

    * C2 página do técnico: meta ``color-scheme`` "only light" e meta ``text-scale``;
      tokens ``--verde``, ``--amarelo`` e ``--vermelho`` iguais aos da página do
      agricultor; nada de ``font-size`` em px; link do topo com 48 px; par
      ``retorno-passo``/``erro-passo`` em cada cartão; variantes ``-texto`` com
      ≥ 4,5:1 sobre o branco; nenhum recurso de outro host;
    * C3 calendário do café: ``#t-seca`` e ``#t-florada`` com confirmação em linha
      (Confirmar/Cancelar), o contrato das rotas da banca (corpo ``{"ativa": ...}``,
      PIN no cabeçalho ``X-PIN-Banca``) e o PIN que barra a mudança;
    * C1/C4 suíte: arquivos presentes, ``metas_v4.json`` bem formado, sintaxe dos
      módulos Python e Node, ``package.json`` com versões exatas, scripts com a
      sintaxe certa e ``rodar_testes_ux.sh`` chamando ``validar_ux.sh``; funções de
      legibilidade (Flesch-PT) e de coerência com exemplos calculados à mão.

Execução (a partir da raiz do projeto): ``python -m unittest tests.test_tecnico_v4 -v``.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import Dict, List

RAIZ_PROJETO: Path = Path(__file__).resolve().parent.parent
PASTA_SRC: Path = RAIZ_PROJETO / "src"
if str(PASTA_SRC) not in sys.path:
    sys.path.insert(0, str(PASTA_SRC))

import app as modulo_app  # noqa: E402
from app import CABECALHO_PIN_BANCA, app  # noqa: E402
from database import definir_configuracao, inicializar_banco, obter_configuracao  # noqa: E402
from seguranca_banca import CHAVE_CONFIGURACAO_PIN, gerar_hash_pin  # noqa: E402

TEMPLATE_TECNICO: Path = PASTA_SRC / "templates" / "tecnico.html"
TEMPLATE_AGRICULTOR: Path = PASTA_SRC / "templates" / "index.html"
CSS_AGRICULTOR: Path = PASTA_SRC / "templates" / "agricultor.css"
PASTA_VALIDACAO: Path = RAIZ_PROJETO / "tests" / "e2e" / "validacao"
ARQUIVOS_SUITE: List[str] = ["servidor_estados.py", "matriz_estados.js", "medidas.js", "pixels.js", "axe_lighthouse.js",
                             "regioes_vivas.js", "coerencia.py", "legibilidade.py", "metas_v4.json"]
TOKENS_ESTADO: List[str] = ["verde", "amarelo", "vermelho"]
IP_TESTE: str = "127.0.0.1"


def ler(caminho: Path) -> str:
    """Texto UTF-8 do arquivo."""
    return caminho.read_text(encoding="utf-8")


def tokens_raiz(texto: str) -> Dict[str, str]:
    """Valores (minúsculos) de ``--verde``, ``--amarelo`` e ``--vermelho`` no primeiro bloco ``:root``."""
    bloco = re.search(r":root\s*\{([^}]*)\}", texto)
    if not bloco:
        return {}
    valores: Dict[str, str] = {}
    for nome in TOKENS_ESTADO:
        achado = re.search(rf"--{nome}\s*:\s*([^;]+);", bloco.group(1))
        if achado:
            valores[nome] = achado.group(1).strip().lower()
    return valores


def luminancia(cor: str) -> float:
    """Luminância relativa WCAG de uma cor #rrggbb."""
    canais = [int(cor[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lineares = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in canais]
    return 0.2126 * lineares[0] + 0.7152 * lineares[1] + 0.0722 * lineares[2]


def razao_contraste(cor_a: str, cor_b: str) -> float:
    """Razão de contraste WCAG entre duas cores #rrggbb."""
    a, b = luminancia(cor_a), luminancia(cor_b)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def carregar_modulo(nome: str, caminho: Path) -> ModuleType:
    """Importa um módulo da suíte pelo caminho do arquivo."""
    especificacao = importlib.util.spec_from_file_location(nome, caminho)
    assert especificacao is not None and especificacao.loader is not None
    modulo = importlib.util.module_from_spec(especificacao)
    sys.modules[nome] = modulo  # dataclasses do módulo procuram o próprio módulo em sys.modules
    especificacao.loader.exec_module(modulo)
    return modulo


def setUpModule() -> None:
    """Silencia os logs informativos/avisos durante os testes."""
    logging.disable(logging.WARNING)


def tearDownModule() -> None:
    """Reativa os logs."""
    logging.disable(logging.NOTSET)


class TesteTemplateTecnico(unittest.TestCase):
    """C2: meta tags, tokens, rem, alvos de 48 px e retorno junto do botão (lidos do template)."""

    @classmethod
    def setUpClass(cls) -> None:
        """Lê os templates uma vez."""
        cls.tecnico = ler(TEMPLATE_TECNICO)
        cls.agricultor = ler(TEMPLATE_AGRICULTOR)

    def test_sem_font_size_em_px(self) -> None:
        """tecnico.html não tem ``font-size: <n>px`` (o texto acompanha a fonte do celular)."""
        self.assertIsNone(re.search(r"font-size:\s*[\d.]+px", self.tecnico))

    def test_tokens_de_estado_iguais_aos_do_agricultor(self) -> None:
        """--verde, --amarelo e --vermelho de :root idênticos nos dois templates (ou no CSS do agricultor)."""
        agricultor = tokens_raiz(self.agricultor)
        if len(agricultor) < 3 and CSS_AGRICULTOR.exists():
            agricultor = {**tokens_raiz(ler(CSS_AGRICULTOR)), **agricultor}
        tecnico = tokens_raiz(self.tecnico)
        self.assertEqual(set(tecnico), set(TOKENS_ESTADO))
        self.assertEqual(tecnico, agricultor)

    def test_variantes_de_texto_legiveis_no_branco(self) -> None:
        """--verde-texto, --amarelo-texto e --vermelho-texto com ≥ 4,5:1 sobre o branco e sobre o fundo claro."""
        bloco = re.search(r":root\s*\{([^}]*)\}", self.tecnico)
        assert bloco is not None
        for nome in TOKENS_ESTADO:
            with self.subTest(token=nome):
                texto = re.search(rf"--{nome}-texto\s*:\s*(#[0-9a-fA-F]{{6}})", bloco.group(1))
                fundo = re.search(rf"--{nome}-fundo\s*:\s*(#[0-9a-fA-F]{{6}})", bloco.group(1))
                self.assertIsNotNone(texto)
                self.assertGreaterEqual(razao_contraste(texto.group(1), "#ffffff"), 4.5)  # type: ignore[union-attr]
                if fundo:
                    self.assertGreaterEqual(razao_contraste(texto.group(1), fundo.group(1)), 4.5)  # type: ignore[union-attr]

    def test_link_do_topo_com_48_px(self) -> None:
        """``.topo a`` com display inline-flex, min-height de 48 px e padding lateral."""
        regra = re.search(r"\.topo a\s*\{([^}]*)\}", self.tecnico)
        self.assertIsNotNone(regra)
        corpo = regra.group(1)  # type: ignore[union-attr]
        self.assertIn("display: inline-flex", corpo)
        self.assertIn("align-items: center", corpo)
        self.assertRegex(corpo, r"min-height:\s*48px")
        self.assertRegex(corpo, r"padding:\s*0 8px")

    def test_retorno_junto_de_cada_cartao_de_passo(self) -> None:
        """Cada ``.passo`` tem seu par ``.retorno-passo`` (role=status) e ``.erro-passo`` (role=alert)."""
        passos = re.findall(r'<div class="passo"[^>]*>([\s\S]*?)(?=<div class="passo"|</section>)', self.tecnico)
        self.assertGreaterEqual(len(passos), 4)
        for passo in passos:
            with self.subTest(passo=passo[:60]):
                self.assertRegex(passo, r'<p class="retorno-passo" role="status">')
                self.assertRegex(passo, r'<p class="erro-passo" role="alert">')
        self.assertIn("scrollIntoView({ block: \"nearest\" })", self.tecnico)

    def test_calendario_do_cafe_com_confirmacao(self) -> None:
        """#t-seca e #t-florada abrem confirmação em linha com Confirmar e Cancelar; consequência dita."""
        self.assertIn('id="t-seca"', self.tecnico)
        self.assertIn('id="t-florada"', self.tecnico)
        for caixa in ("conf-seca", "conf-florada"):
            with self.subTest(caixa=caixa):
                trecho = re.search(rf'<div class="confirmacao" id="{caixa}"[\s\S]*?</div>\s*</div>', self.tecnico)
                self.assertIsNotNone(trecho)
                self.assertIn("data-confirmar>Confirmar<", trecho.group(0))  # type: ignore[union-attr]
                self.assertIn("data-cancelar>Cancelar<", trecho.group(0))  # type: ignore[union-attr]
        self.assertIn("o aparelho não vai mandar regar. Confirmar?", self.tecnico)
        self.assertIn("A data da florada será hoje. Confirmar?", self.tecnico)
        self.assertIn("/api/banca/seca_programada", self.tecnico)
        self.assertIn("/api/banca/florada", self.tecnico)
        self.assertIn('"X-PIN-Banca"', self.tecnico)

    def test_pagina_offline(self) -> None:
        """Nenhum script, folha, imagem ou fonte de outro host (página 100 % offline)."""
        self.assertIsNone(re.search(r'(?:src|href)=["\']https?://', self.tecnico))
        self.assertNotIn("@import", self.tecnico)
        self.assertIsNone(re.search(r"\bTODO\b", self.tecnico))


class BaseTecnico(unittest.TestCase):
    """Banco temporário e cliente de testes do Flask apontado para ele."""

    def setUp(self) -> None:
        """Cria o banco temporário e o cliente."""
        self.pasta = Path(tempfile.mkdtemp(prefix="agro_tecnico_v4_"))
        self.banco = self.pasta / "teste.db"
        inicializar_banco(self.banco)
        self._original = (app.config["CAMINHO_BANCO"], app.config["CAMINHO_MODELO"])
        app.config["CAMINHO_BANCO"] = self.banco
        app.config["CAMINHO_MODELO"] = self.pasta / "sem_modelo.pkl"
        app.config["TESTING"] = True
        self.cliente = app.test_client()
        modulo_app._LIMITADOR_PIN.limpar(IP_TESTE)

    def tearDown(self) -> None:
        """Restaura o app e apaga o banco temporário."""
        app.config["CAMINHO_BANCO"], app.config["CAMINHO_MODELO"] = self._original
        modulo_app._LIMITADOR_PIN.limpar(IP_TESTE)
        shutil.rmtree(self.pasta, ignore_errors=True)


class TestePaginaTecnicoServida(BaseTecnico):
    """C2 e C3 pela rota: GET /tecnico e o contrato das rotas da banca usadas pelo calendário."""

    def test_get_tecnico_com_metas(self) -> None:
        """GET /tecnico contém meta color-scheme "only light" e meta text-scale."""
        resposta = self.cliente.get("/tecnico")
        self.assertEqual(resposta.status_code, 200)
        html = resposta.get_data(as_text=True)
        self.assertRegex(html, r'<meta name="color-scheme" content="only light">')
        self.assertRegex(html, r'<meta name="text-scale" content="scale">')
        self.assertIn('id="t-seca"', html)
        self.assertIn('id="t-florada"', html)

    def test_seca_com_estado_pedido(self) -> None:
        """O corpo ``{"ativa": ...}`` (usado pelo calendário) liga e desliga sem depender do estado anterior."""
        for pedido in (True, True, False, False):
            with self.subTest(pedido=pedido):
                resposta = self.cliente.post("/api/banca/seca_programada", json={"ativa": pedido})
                self.assertEqual(resposta.status_code, 200)
                self.assertIs(resposta.get_json()["seca_programada_ativa"], pedido)
                self.assertIn("Seca programada", resposta.get_json()["fala"])
        cenarios = self.cliente.get("/api/cenarios").get_json()
        self.assertIs(cenarios["banca"]["seca_programada_ativa"], False)

    def test_pin_barra_a_mudanca(self) -> None:
        """Com PIN definido, sem o cabeçalho → 403 e nada muda; com o PIN no X-PIN-Banca → muda."""
        definir_configuracao(CHAVE_CONFIGURACAO_PIN, gerar_hash_pin("4321"), caminho_banco=self.banco)
        self.assertTrue(obter_configuracao(CHAVE_CONFIGURACAO_PIN, "", caminho_banco=self.banco))
        sem_pin = self.cliente.post("/api/banca/seca_programada", json={"ativa": True})
        self.assertEqual(sem_pin.status_code, 403)
        florada = self.cliente.post("/api/banca/florada", json={})
        self.assertEqual(florada.status_code, 403)
        modulo_app._LIMITADOR_PIN.limpar(IP_TESTE)
        cenarios = self.cliente.get("/api/cenarios").get_json()
        self.assertIs(cenarios["banca"]["seca_programada_ativa"], False)
        self.assertEqual(cenarios["banca"]["data_florada_principal"], "")
        com_pin = self.cliente.post("/api/banca/seca_programada", json={"ativa": True}, headers={CABECALHO_PIN_BANCA: "4321"})
        self.assertEqual(com_pin.status_code, 200)
        self.assertIs(com_pin.get_json()["seca_programada_ativa"], True)


class TesteSuiteValidacao(unittest.TestCase):
    """C1 e C4: arquivos da suíte, sintaxe, metas e integração com o fluxo de testes."""

    def test_arquivos_presentes(self) -> None:
        """Todos os módulos da suíte, o script e o package.json existem."""
        for nome in ARQUIVOS_SUITE:
            with self.subTest(arquivo=nome):
                self.assertTrue((PASTA_VALIDACAO / nome).is_file())
        self.assertTrue((RAIZ_PROJETO / "scripts" / "validar_ux.sh").is_file())
        self.assertTrue((RAIZ_PROJETO / "tests" / "e2e" / "tecnico_v4.spec.js").is_file())
        self.assertTrue((RAIZ_PROJETO / "package.json").is_file())

    def test_python_da_suite_compila(self) -> None:
        """``py_compile`` em todos os .py da pasta de validação."""
        destino = Path(tempfile.mkdtemp(prefix="agro_pyc_"))
        try:
            for arquivo in sorted(PASTA_VALIDACAO.glob("*.py")):
                with self.subTest(arquivo=arquivo.name):
                    py_compile.compile(str(arquivo), cfile=str(destino / (arquivo.stem + ".pyc")), doraise=True)
        finally:
            shutil.rmtree(destino, ignore_errors=True)

    @unittest.skipUnless(shutil.which("node"), "Node ausente")
    def test_node_check_nos_js(self) -> None:
        """``node --check`` em todos os .js da suíte e no teste do técnico."""
        arquivos = sorted(PASTA_VALIDACAO.glob("*.js")) + [RAIZ_PROJETO / "tests" / "e2e" / "tecnico_v4.spec.js"]
        for arquivo in arquivos:
            with self.subTest(arquivo=arquivo.name):
                resultado = subprocess.run(["node", "--check", str(arquivo)], capture_output=True, text=True, check=False)
                self.assertEqual(resultado.returncode, 0, resultado.stderr)

    @unittest.skipUnless(shutil.which("bash"), "bash ausente")
    def test_scripts_com_sintaxe_certa(self) -> None:
        """``bash -n`` nos dois scripts e ``rodar_testes_ux.sh`` chamando ``validar_ux.sh``."""
        for nome in ("rodar_testes_ux.sh", "validar_ux.sh"):
            with self.subTest(script=nome):
                resultado = subprocess.run(["bash", "-n", str(RAIZ_PROJETO / "scripts" / nome)], capture_output=True,
                                           text=True, check=False)
                self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertGreaterEqual(ler(RAIZ_PROJETO / "scripts" / "rodar_testes_ux.sh").count("validar_ux.sh"), 1)
        self.assertIn("5000", ler(RAIZ_PROJETO / "scripts" / "validar_ux.sh"))

    def test_package_json_com_versoes_exatas(self) -> None:
        """package.json privado, com playwright 1.63.x, axe-core 4.14.x e lighthouse 13.6.x exatos e os dois scripts."""
        pacote = json.loads(ler(RAIZ_PROJETO / "package.json"))
        self.assertIs(pacote["private"], True)
        dependencias = pacote["devDependencies"]
        self.assertRegex(dependencias["playwright"], r"^1\.63\.\d+$")
        self.assertRegex(dependencias["axe-core"], r"^4\.14\.\d+$")
        self.assertRegex(dependencias["lighthouse"], r"^13\.6\.\d+$")
        self.assertEqual(pacote["scripts"]["test:ux"], "scripts/rodar_testes_ux.sh")
        self.assertEqual(pacote["scripts"]["validar:ux"], "scripts/validar_ux.sh")

    def test_vendor_sem_axe_versionado(self) -> None:
        """Nenhum axe.min.js versionado sem licença: se existir em vendor, a licença está ao lado."""
        vendor = RAIZ_PROJETO / "tests" / "e2e" / "vendor"
        if (vendor / "axe.min.js").exists():
            self.assertTrue((vendor / "axe-core.LICENSE").exists())

    def test_metas_bem_formadas(self) -> None:
        """metas_v4.json: ids únicos, operador conhecido, limiar numérico e os 14 itens cobertos."""
        metas = json.loads(ler(PASTA_VALIDACAO / "metas_v4.json"))["metas"]
        ids = [meta["id"] for meta in metas]
        self.assertEqual(len(ids), len(set(ids)))
        for meta in metas:
            with self.subTest(meta=meta["id"]):
                self.assertIn(meta["operador"], ("<=", ">=", "==", "<", ">"))
                self.assertIsInstance(meta["limiar"], (int, float))
                self.assertTrue(meta["descricao"])
                self.assertIn(meta.get("ferramenta"), (None, "axe-core", "lighthouse"))
        self.assertEqual({meta["item"] for meta in metas}, set(range(1, 15)))
        por_id = {meta["id"]: meta for meta in metas}
        self.assertEqual(por_id["12.html_gzip"]["limiar"], 15000)
        self.assertEqual(por_id["12.css_gzip"]["limiar"], 6144)
        self.assertEqual(por_id["12.html_mais_css"]["limiar"], 21000)
        self.assertEqual(por_id["12.segunda_visita"]["limiar"], 15500)
        self.assertEqual(por_id["12.slow3g"]["limiar"], 2500)
        self.assertEqual(por_id["4.amarelo_escuro_auto"]["limiar"], 9.0)
        self.assertEqual(por_id["2.primeira_tela_razao"]["limiar"], 7)

    def test_servidor_de_estados_recusa_a_porta_5000(self) -> None:
        """``servidor_estados.py 5000`` sai com erro (a porta do sítio nunca é usada)."""
        resultado = subprocess.run([sys.executable, str(PASTA_VALIDACAO / "servidor_estados.py"), "5000"],
                                   capture_output=True, text=True, check=False, cwd=str(RAIZ_PROJETO), timeout=120)
        self.assertNotEqual(resultado.returncode, 0)
        self.assertIn("5000", resultado.stderr + resultado.stdout)


class TesteLegibilidadeECoerencia(unittest.TestCase):
    """Funções de legibilidade.py e coerencia.py com exemplos calculados à mão."""

    @classmethod
    def setUpClass(cls) -> None:
        """Carrega os dois módulos da suíte."""
        cls.legibilidade = carregar_modulo("legibilidade_ux4", PASTA_VALIDACAO / "legibilidade.py")
        cls.coerencia = carregar_modulo("coerencia_ux4", PASTA_VALIDACAO / "coerencia.py")

    def test_flesch_e_indice_da_acao(self) -> None:
        """Flesch-PT de Martins (1996) e o índice da frase com ação."""
        leg = self.legibilidade
        self.assertAlmostEqual(leg.flesch_pt("Sinal vermelho. Regue agora. A terra está seca."), 66.4, delta=0.1)
        self.assertEqual(leg.contar_silabas("água"), 2)
        self.assertEqual(leg.indice_da_acao("Sinal vermelho. Regue agora."), 1)
        self.assertIsNone(leg.indice_da_acao("Sinal verde. Tudo certo."))
        self.assertTrue(leg.PADRAO_GRAUS_ERRADO.search("Está frio, 1 graus."))
        self.assertIsNone(leg.PADRAO_GRAUS_ERRADO.search("Está frio, 1 grau. Faz 21 graus."))
        self.assertTrue(leg.PADRAO_MENOS_N.search("Está frio demais, -2 graus."))
        self.assertTrue(leg.PADRAO_GEIE.search("Pode ser que geie esta noite."))

    def test_coerencia_de_um_estado(self) -> None:
        """Estado coerente sem problemas; simulação com lâmpada vermelha e Feito visível acusa os dois."""
        caso_bom = {"estado": "vermelho", "falas": ["Sinal vermelho. Regue agora. A terra está seca."],
                    "retrato": {"estado": "vermelho", "confianca": "viva", "chip": "Sinal vermelho · Perigo",
                                "titulo": "Perigo para o seu café", "acoes": [{"codigo": "REGAR_AGORA"}],
                                "feito_visivel": True},
                    "api": {"cor": "vermelho", "tem_leitura": True, "acoes": ["REGAR_AGORA"], "causa": "TERRA_SECA",
                            "mensagem_voz": "Urgente! A terra está muito seca. Regue agora, devagar."}}
        self.assertEqual(self.coerencia.problemas_do_estado(caso_bom), [])
        caso_ruim = {"estado": "simulacao", "falas": ["Atenção. Dados de teste."],
                     "retrato": {"estado": "vermelho", "confianca": "", "chip": "Sinal vermelho · Perigo", "titulo": "Teste",
                                 "acoes": [{"codigo": "VERIFICAR_APARELHO"}], "feito_visivel": True},
                     "api": {"cor": "vermelho", "tem_leitura": True, "dados_simulados": True, "modo_operacao": "SIMULACAO",
                             "acoes": ["VERIFICAR_APARELHO"], "causa": "APARELHO",
                             "mensagem_voz": "As cores agora são só um teste. Confira o aparelho."}}
        problemas = " | ".join(self.coerencia.problemas_do_estado(caso_ruim))
        self.assertIn("lâmpada vermelho acesa com dados de teste", problemas)
        self.assertIn("Feito visível", problemas)

    def test_janela_de_desfazer_do_servidor(self) -> None:
        """O contrato da frente B: desfazer aceito a 119 s e recusado a 121 s."""
        resultado = self.coerencia.conferir_janela_desfazer()
        self.assertTrue(resultado["ok"], resultado)


if __name__ == "__main__":
    unittest.main()
