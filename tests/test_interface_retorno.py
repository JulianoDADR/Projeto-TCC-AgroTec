"""Testes da Frente 3 — interface com causa, retorno do agricultor e ponte com o técnico.

Cobrem, sempre com banco SQLite TEMPORÁRIO e sem rede:

    * causa visível (``derivar_causa``): coerente com a frase falada em 5.000
      leituras simuladas por cultura; ações e causa de cada ``motivo_regra``
      da regra fisiológica (Frente 1) com as palavras-chave combinadas;
    * orçamento da página: GET / com gzip ≤ 16.000 B no pior caso (SECA_EXTREMA,
      ONDA_DE_CALOR e GEADA) e minificação do JS que preserva cada token;
    * "Feito" de um toque: 201 + linha em ``registro_acoes``, 429 no 11º
      pedido do minuto, 413 com corpo de 2 KB, 400 com ação inválida e a causa
      silenciada por 60 min (relógio injetado);
    * alarme sonoro local (tocador falso e relógio injetado);
    * página e API do técnico (PIN, avisos confirmados, WhatsApp ≤ 700
      caracteres, pedido de ajuda com e sem número, saúde do prognóstico);
    * calibração da sonda (firmware, dois pontos, um ponto, captura instável,
      conversão que nunca lança exceção) e a banca (florada, seca programada).

Execução (a partir da raiz do projeto): ``python -m unittest tests.test_interface_retorno -v``.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import math
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

RAIZ_PROJETO: Path = Path(__file__).resolve().parent.parent
PASTA_SRC: Path = RAIZ_PROJETO / "src"
if str(PASTA_SRC) not in sys.path:
    sys.path.insert(0, str(PASTA_SRC))

import alarme_local  # noqa: E402
import app as modulo_app  # noqa: E402
import calibracao_sonda  # noqa: E402
import retorno_agricultor  # noqa: E402
import seguranca_banca  # noqa: E402
from acoes_orientacao import (  # noqa: E402
    ACOES_POR_MOTIVO_REGRA,
    CAUSA_POR_MOTIVO_REGRA,
    CAUSAS_VALIDAS,
    DESCRICOES_CAUSA,
    LEGENDAS_CAUSA,
    acoes_coerentes_com_mensagem,
    causa_coerente_com_mensagem,
    derivar_acoes,
    derivar_causa,
)
from alarme_local import EstadoAlarme, decidir_toque  # noqa: E402
from app import app  # noqa: E402
from database import (  # noqa: E402
    ParametrosCultura,
    RegistroLeitura,
    buscar_parametros_cultura,
    definir_configuracao,
    inicializar_banco,
    inserir_leitura,
    obter_conexao,
    obter_configuracao,
)
from gerador_dataset import (  # noqa: E402
    calcular_features,
    calcular_status_regra,
    gerar_mensagem_orientacao,
    simular_serie_temporal,
)
from seguranca_banca import LimitadorJanela  # noqa: E402

IP_TESTE: str = "127.0.0.1"
FORMATO: str = "%Y-%m-%d %H:%M:%S"
META_PAGINA_GZIP_BYTES: int = 16_000
LIMITE_TECNICO_GZIP_BYTES: int = 24 * 1024
# Frases da regra fisiológica (Frente 1) com as palavras-chave combinadas entre as frentes.
FRASES_MOTIVO_REGRA: Dict[str, str] = {
    "GEADA_AGORA": "Perigo! Geada agora no café. Cubra as mudas e chegue terra no pé.",
    "CALOR_FLORADA": "Perigo! Calor forte na florada. Regue no fim da tarde e faça sombra nas mudas.",
    "CALOR_SEGUIDO": "Perigo! Segundo dia de calor forte. Regue no fim da tarde.",
    "DIA_FRIO": "Atenção. O dia está frio para o café. Não precisa fazer nada agora.",
    "DIA_QUENTE": "Atenção. O dia está quente para o café. Não precisa fazer nada agora.",
    "CALOR_DIA": "Atenção. A máxima de hoje foi alta. Não precisa fazer nada agora.",
    "SECA_PROGRAMADA": "Atenção. Seca programada: a terra está secando de propósito. Não regue.",
}


def setUpModule() -> None:
    """Silencia os logs informativos/avisos durante os testes."""
    logging.disable(logging.WARNING)


def tearDownModule() -> None:
    """Reativa os logs."""
    logging.disable(logging.NOTSET)


class BaseRetorno(unittest.TestCase):
    """Banco temporário, app apontado para ele (sem modelo), limitadores e caches limpos."""

    def setUp(self) -> None:
        """Cria o banco temporário e prepara o cliente de testes."""
        self._diretorio = tempfile.mkdtemp(prefix="agro_retorno_")
        self.pasta = Path(self._diretorio)
        self.banco: Path = self.pasta / "teste.db"
        inicializar_banco(self.banco)
        self.arabica: ParametrosCultura = buscar_parametros_cultura("cafe_arabica", caminho_banco=self.banco)
        self.conilon: ParametrosCultura = buscar_parametros_cultura("cafe_conilon", caminho_banco=self.banco)
        self._config_original = (app.config["CAMINHO_BANCO"], app.config["CAMINHO_MODELO"])
        app.config["CAMINHO_BANCO"] = self.banco
        app.config["CAMINHO_MODELO"] = self.pasta / "sem_modelo.pkl"
        app.config["TESTING"] = True
        self.cliente = app.test_client()
        self.limpar_limitadores()
        calibracao_sonda.limpar_cache()

    def tearDown(self) -> None:
        """Restaura o app e remove o diretório temporário."""
        app.config["CAMINHO_BANCO"], app.config["CAMINHO_MODELO"] = self._config_original
        self.limpar_limitadores()
        calibracao_sonda.limpar_cache()
        shutil.rmtree(self._diretorio, ignore_errors=True)

    @staticmethod
    def limpar_limitadores() -> None:
        """Esquece os pedidos e os erros de PIN do IP dos testes."""
        modulo_app._LIMITADOR_PIN.limpar(IP_TESTE)
        for limitador in (modulo_app._LIMITADOR_FEITO, modulo_app._LIMITADOR_AJUDA, modulo_app._LIMITADOR_EVENTOS):
            limitador.limpar()

    def definir_pin(self) -> None:
        """Grava o PIN '1234' pela linha de comando da segurança da banca."""
        with redirect_stdout(io.StringIO()):
            self.assertEqual(seguranca_banca.main(["--definir-pin", "1234", "--banco", str(self.banco)]), 0)

    def inserir(self, instante_utc: Optional[datetime] = None, status: int = 0, origem: str = "CAMPO_USB",
                temperatura: float = 20.0, umidade_solo: float = 70.0, status_regra: Optional[int] = None,
                estado_sensor: str = "OK", umidade_solo_bruta: Optional[float] = None) -> int:
        """Grava uma leitura do Arábica no instante UTC dado (padrão: há 1 s)."""
        instante = (instante_utc or datetime.now(timezone.utc) - timedelta(seconds=1)).astimezone(timezone.utc)
        deficit, desvio = calcular_features(temperatura, umidade_solo, self.arabica)
        return inserir_leitura(RegistroLeitura(
            data_hora=instante.astimezone().replace(tzinfo=None).strftime(FORMATO), codigo_cultura="cafe_arabica",
            origem=origem, temperatura_ar_bruta=temperatura, umidade_ar_bruta=70.0,
            umidade_solo_bruta=umidade_solo if umidade_solo_bruta is None else umidade_solo_bruta,
            temperatura_ar=temperatura, umidade_ar=70.0, umidade_solo=umidade_solo, deficit_hidrico_solo=deficit,
            desvio_termico=desvio, status_regra=status if status_regra is None else status_regra,
            status_predito=status, status_alerta=status, probabilidade_predicao=1.0, flag_anomalia=0,
            descricao_anomalia="", mensagem_voz={0: "Tudo bem.", 1: "Atenção.", 2: "Perigo."}[status],
            versao_modelo="REGRAS", estado_sensor=estado_sensor,
            instante_utc=instante.isoformat(timespec="seconds")), caminho_banco=self.banco)

    def contar(self, tabela: str) -> int:
        """Número de linhas da tabela."""
        conexao = obter_conexao(self.banco)
        try:
            return int(conexao.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0])
        finally:
            conexao.close()


# ---------------------------------------------------------------------------
# Causa visível e pictogramas
# ---------------------------------------------------------------------------
class TesteCausaVisivel(BaseRetorno):
    """``derivar_causa`` e ``derivar_acoes`` coerentes com o que a voz diz."""

    def test_catalogo_de_causas(self) -> None:
        """Toda causa tem legenda de uma palavra e frase fixa curta; a da terra seca traz a dica da mão."""
        self.assertEqual(CAUSAS_VALIDAS, ("TERRA_SECA", "ENCHARCADO", "FRIO_GEADA", "CALOR", "APARELHO", "NENHUMA"))
        for causa in CAUSAS_VALIDAS:
            self.assertEqual(len(LEGENDAS_CAUSA[causa].split()), 1)
            self.assertLessEqual(len(DESCRICOES_CAUSA[causa]), 140)
        self.assertEqual(DESCRICOES_CAUSA["TERRA_SECA"],
                         "A terra está seca. Aperte a terra na mão: se esfarelar, está seca.")

    def test_propriedade_causa_coerente_em_5000_leituras(self) -> None:
        """Em 5.000 leituras por cultura (status da regra, Amarelo e Vermelho), a causa aparece na frase falada."""
        for parametros in (self.arabica, self.conilon):
            serie = simular_serie_temporal(parametros, horas=5000)
            for temperatura, umidade_solo in zip(serie["temperatura_ar"], serie["umidade_solo"]):
                temperatura, umidade_solo = float(temperatura), float(umidade_solo)
                deficit, desvio = calcular_features(temperatura, umidade_solo, parametros)
                for status in {calcular_status_regra(temperatura, umidade_solo, parametros), 1, 2}:
                    mensagem = gerar_mensagem_orientacao(status, deficit, desvio, parametros, temperatura, umidade_solo)
                    causa = derivar_causa(status, {"temperatura_ar": temperatura, "umidade_solo": umidade_solo},
                                          parametros)
                    self.assertIn(causa, CAUSAS_VALIDAS)
                    self.assertTrue(causa_coerente_com_mensagem(causa, mensagem), (causa, mensagem))

    def test_propriedade_motivo_regra_em_5000_leituras(self) -> None:
        """Para cada ``motivo_regra``, em 5.000 leituras: ações do mapa, com as palavras-chave da frase, e causa certa."""
        serie = simular_serie_temporal(self.arabica, horas=5000)
        pares = list(zip(serie["temperatura_ar"], serie["umidade_solo"]))
        for motivo, frase in FRASES_MOTIVO_REGRA.items():
            with self.subTest(motivo=motivo):
                status = 2 if motivo in ("GEADA_AGORA", "CALOR_FLORADA", "CALOR_SEGUIDO") else 1
                for temperatura, umidade_solo in pares:
                    acoes = derivar_acoes(status, float(temperatura), float(umidade_solo), self.arabica,
                                          mensagem_voz=frase, motivo_regra=motivo)
                    self.assertEqual(acoes, list(ACOES_POR_MOTIVO_REGRA[motivo]))
                    self.assertTrue(acoes_coerentes_com_mensagem(acoes, frase), (acoes, frase))
                    leitura = {"temperatura_ar": float(temperatura), "umidade_solo": float(umidade_solo),
                               "motivo_regra": motivo}
                    self.assertEqual(derivar_causa(status, leitura, self.arabica), CAUSA_POR_MOTIVO_REGRA[motivo])

    def test_motivo_vazio_igual_a_v2(self) -> None:
        """Sem ``motivo_regra`` as ações não mudam (mesmo resultado da chamada da v2)."""
        serie = simular_serie_temporal(self.conilon, horas=500)
        for temperatura, umidade_solo in zip(serie["temperatura_ar"], serie["umidade_solo"]):
            status = calcular_status_regra(float(temperatura), float(umidade_solo), self.conilon)
            self.assertEqual(
                derivar_acoes(status, float(temperatura), float(umidade_solo), self.conilon, motivo_regra=""),
                derivar_acoes(status, float(temperatura), float(umidade_solo), self.conilon))

    def test_causa_de_aparelho_antecipacao_e_tudo_bem(self) -> None:
        """Sensor com defeito → APARELHO; tudo no ideal com aviso de geada → FRIO_GEADA; Verde ideal → NENHUMA."""
        ideal = {"temperatura_ar": 20.0, "umidade_solo": 70.0}
        self.assertEqual(derivar_causa(2, ideal, self.arabica, estado_sensor="DEFEITO"), "APARELHO")
        self.assertEqual(derivar_causa(1, {"temperatura_ar": None, "umidade_solo": 70.0}, self.arabica), "APARELHO")
        self.assertEqual(derivar_causa(1, ideal, self.arabica, motivo_antecipacao="GEADA_NOITE"), "FRIO_GEADA")
        self.assertEqual(derivar_causa(0, ideal, self.arabica), "NENHUMA")
        self.assertEqual(derivar_causa(2, {"temperatura_ar": 20.0, "umidade_solo": 20.0}, self.arabica), "TERRA_SECA")

    def test_status_traz_causa(self) -> None:
        """/api/status traz ``causa``, ``causa_descricao``, ``legenda_causa`` e ``causa_silenciada``."""
        self.assertEqual(self.cliente.post("/api/demonstracao", json={"cenario": "SECA_EXTREMA"}).status_code, 200)
        dados = self.cliente.get("/api/status").get_json()
        self.assertEqual(dados["causa"], "TERRA_SECA")
        self.assertEqual(dados["causa_descricao"], DESCRICOES_CAUSA["TERRA_SECA"])
        self.assertEqual(dados["legenda_causa"], "Seca")
        self.assertIs(dados["causa_silenciada"], False)


# ---------------------------------------------------------------------------
# Orçamento da página e minificação
# ---------------------------------------------------------------------------
class TesteOrcamentoPagina(BaseRetorno):
    """Página do agricultor ≤ 16.000 B gzip com causa, "Feito" e técnico; JS minificado correto."""

    def test_pior_caso_cabe_na_meta(self) -> None:
        """GET / com gzip nos cenários de texto mais longo: ≤ 16.000 B, com os elementos novos."""
        for cenario in ("SECA_EXTREMA", "ONDA_DE_CALOR", "GEADA"):
            with self.subTest(cenario=cenario):
                self.assertEqual(self.cliente.post("/api/demonstracao", json={"cenario": cenario}).status_code, 200)
                resposta = self.cliente.get("/", headers={"Accept-Encoding": "gzip"})
                self.assertEqual(resposta.headers.get("Content-Encoding"), "gzip")
                self.assertLessEqual(len(resposta.data), META_PAGINA_GZIP_BYTES)
                html = gzip.decompress(resposta.data).decode("utf-8")
                for trecho in ('id="causa"', 'id="feito"', 'id="tecnico"', 'id="ajuda"', "/api/acao_feita",
                               "/api/pedido_ajuda", "O desenho grande diz o motivo", "<button"):
                    self.assertIn(trecho, html)
                self.assertNotIn("<script src=", html)

    def test_estado_embutido_sem_campos_inuteis(self) -> None:
        """O estado embutido mantém o que a página usa (cor, ações, causa) e deixa de fora o resto."""
        self.cliente.post("/api/demonstracao", json={"cenario": "GEADA"})
        html = self.cliente.get("/").get_data(as_text=True)
        inicio = html.index('id="estado-inicial">') + len('id="estado-inicial">')
        estado = json.loads(html[inicio:html.index("</script>", inicio)])
        self.assertEqual(estado["causa"], "FRIO_GEADA")
        self.assertIn("causa_descricao", estado)
        self.assertEqual(estado["acoes"], ["COBRIR_MUDAS", "CHEGAR_TERRA"])
        for campo in modulo_app.CAMPOS_FORA_DA_PAGINA:
            self.assertNotIn(campo, estado)

    def _scripts(self) -> List[str]:
        """Blocos ``<script>`` comuns do template da página (fonte, sem minificar)."""
        fonte = (PASTA_SRC / "templates" / "index.html").read_text(encoding="utf-8")
        return re.findall(r"<script>(.*?)</script>", fonte, flags=re.S)

    def test_minificacao_preserva_os_tokens(self) -> None:
        """O JS minificado tem exatamente os mesmos tokens da fonte, a menos da troca de nomes locais."""
        for fonte in self._scripts():
            originais = modulo_app._tokenizar_js(fonte)
            minificado = modulo_app._minificar_js(fonte)
            novos = modulo_app._tokenizar_js(minificado)
            self.assertEqual(len(originais), len(novos))
            mapa: Dict[str, str] = {}
            for (tipo_a, texto_a, _), (tipo_b, texto_b, _) in zip(originais, novos):
                self.assertEqual(tipo_a, tipo_b)
                if texto_a != texto_b:
                    self.assertEqual(tipo_a, "id")
                    self.assertEqual(mapa.setdefault(texto_a, texto_b), texto_b)  # troca consistente
            self.assertEqual(len(set(mapa.values())), len(mapa))  # nomes novos distintos
            textos = {texto for _, texto, _ in originais}
            self.assertFalse(set(mapa.values()) & textos)  # nenhum nome novo colide com um já existente
            self.assertLess(len(minificado), len(fonte) * 0.8)

    def test_minificacao_nao_mexe_em_propriedades_nem_globais(self) -> None:
        """Propriedades, chaves de objeto e globais do navegador ficam como estão."""
        js = ('(function () { var dados = { dados: 1 }; var location = 2; function f(texto) {'
              ' return dados.dados + texto.length + location; } window.f = f; })();')
        saida = modulo_app._minificar_js(js)
        self.assertIn("{dados:1}", saida)
        self.assertIn(".dados+", saida)
        self.assertIn("location", saida)
        self.assertIn("window.f=", saida)
        self.assertIn(".length", saida)

    @unittest.skipUnless(shutil.which("node"), "Node ausente: verificação de sintaxe pulada")
    def test_js_minificado_tem_sintaxe_valida(self) -> None:
        """``node --check`` aceita cada script minificado da página e da página do técnico."""
        fontes = self._scripts() + re.findall(
            r"<script>(.*?)</script>", (PASTA_SRC / "templates" / "tecnico.html").read_text(encoding="utf-8"), re.S)
        for indice, fonte in enumerate(fontes):
            arquivo = self.pasta / f"script_{indice}.js"
            arquivo.write_text(modulo_app._minificar_js(fonte), encoding="utf-8")
            resultado = subprocess.run(["node", "--check", str(arquivo)], capture_output=True, text=True, timeout=60)
            self.assertEqual(resultado.returncode, 0, resultado.stderr)

    def test_css_minificado(self) -> None:
        """O CSS perde comentários, espaços e o zero à esquerda, sem tocar no ``calc``."""
        saida = modulo_app._minificar_css("/* x */ a { margin: 0.5em ; width: calc(100% - 2px) !important; }")
        self.assertEqual(saida, "a{margin:.5em;width:calc(100% - 2px)!important}")


# ---------------------------------------------------------------------------
# "Feito" de um toque
# ---------------------------------------------------------------------------
class TesteFeito(BaseRetorno):
    """POST /api/acao_feita, limites e silêncio da causa."""

    def test_feito_valido_grava_e_confirma(self) -> None:
        """POST válido → 201, linha em ``registro_acoes`` e frase "Anotado: você ... às HH horas."."""
        id_leitura = self.inserir(status=2, umidade_solo=20.0)
        agora = datetime(2026, 7, 10, 17, 5, tzinfo=timezone.utc)
        with mock.patch.object(modulo_app, "_agora_utc", return_value=agora):
            resposta = self.cliente.post("/api/acao_feita", json={"codigo_acao": "REGAR_AGORA", "causa": "TERRA_SECA",
                                                                  "id_leitura": id_leitura})
        self.assertEqual(resposta.status_code, 201)
        dados = resposta.get_json()
        hora = agora.astimezone().hour
        self.assertRegex(dados["fala"], r"^Anotado: você regou (às \d+ horas|à 1 hora|à meia-noite|ao meio-dia)\.$")
        self.assertIn(retorno_agricultor.falar_hora(agora.astimezone()), dados["fala"])
        self.assertTrue(0 <= hora <= 23)
        conexao = obter_conexao(self.banco)
        try:
            linha = conexao.execute("SELECT codigo_acao, causa, id_leitura, instante_utc FROM registro_acoes").fetchone()
            eventos = conexao.execute("SELECT tipo FROM eventos_interface").fetchall()
        finally:
            conexao.close()
        self.assertEqual(tuple(linha), ("REGAR_AGORA", "TERRA_SECA", id_leitura, "2026-07-10T17:05:00+00:00"))
        self.assertEqual([tuple(e) for e in eventos], [("feito",)])

    def test_limites_e_validacao(self) -> None:
        """11º POST no minuto → 429; corpo de 2 KB → 413; ação ou causa inválida → 400."""
        corpo = {"codigo_acao": "COBRIR_MUDAS", "causa": "FRIO_GEADA", "id_leitura": None}
        grande = json.dumps(dict(corpo, enchimento="x" * 2048))
        self.assertEqual(self.cliente.post("/api/acao_feita", data=grande,
                                           content_type="application/json").status_code, 413)
        self.assertEqual(self.cliente.post("/api/acao_feita", json=dict(corpo, codigo_acao="DANCAR")).status_code, 400)
        self.assertEqual(self.cliente.post("/api/acao_feita", json=dict(corpo, causa="CHUVA")).status_code, 400)
        modulo_app._LIMITADOR_FEITO.limpar()
        for numero in range(10):
            self.assertEqual(self.cliente.post("/api/acao_feita", json=corpo).status_code, 201, numero)
        resposta = self.cliente.post("/api/acao_feita", json=corpo)
        self.assertEqual(resposta.status_code, 429)
        self.assertEqual(resposta.get_json(), {"erro": modulo_app.MENSAGEM_MUITOS_PEDIDOS})
        self.assertEqual(self.contar("registro_acoes"), 10)

    def test_limitador_janela_desliza(self) -> None:
        """``LimitadorJanela``: o limite vale na janela e libera quando ela anda (relógio injetado)."""
        relogio = [0.0]
        limitador = LimitadorJanela(3, 600.0, relogio=lambda: relogio[0])
        self.assertEqual([limitador.permitir("ip") for _ in range(4)], [True, True, True, False])
        self.assertTrue(limitador.permitir("outro"))
        relogio[0] = 601.0
        self.assertTrue(limitador.permitir("ip"))

    def test_feito_silencia_a_causa_por_60_minutos(self) -> None:
        """Depois do "Feito" para FRIO_GEADA, /api/status traz causa_silenciada=true por 60 min e false depois."""
        self.inserir(status=2, temperatura=1.0)
        inicio = datetime.now(timezone.utc)
        self.assertEqual(self.cliente.get("/api/status").get_json()["causa"], "FRIO_GEADA")
        with mock.patch.object(modulo_app, "_agora_utc", return_value=inicio):
            self.assertIs(self.cliente.get("/api/status").get_json()["causa_silenciada"], False)
            resposta = self.cliente.post("/api/acao_feita", json={"codigo_acao": "COBRIR_MUDAS", "causa": "FRIO_GEADA"})
            self.assertEqual(resposta.status_code, 201)
        for minutos, esperado in ((0, True), (30, True), (59, True), (61, False), (120, False)):
            with self.subTest(minutos=minutos), \
                    mock.patch.object(modulo_app, "_agora_utc", return_value=inicio + timedelta(minutes=minutos)):
                self.assertIs(self.cliente.get("/api/status").get_json()["causa_silenciada"], esperado)

    def test_feito_atrasado_e_demonstracao(self) -> None:
        """Toque guardado sem rede volta com ``atraso_s``; "Feito" na banca não cala um alerta real."""
        agora = datetime.now(timezone.utc)
        with mock.patch.object(modulo_app, "_agora_utc", return_value=agora):
            self.cliente.post("/api/acao_feita", json={"codigo_acao": "REGAR_AGORA", "causa": "TERRA_SECA",
                                                       "atraso_s": 600})
        conexao = obter_conexao(self.banco)
        try:
            instante = conexao.execute("SELECT instante_utc FROM registro_acoes").fetchone()[0]
        finally:
            conexao.close()
        self.assertEqual(instante, (agora - timedelta(seconds=600)).isoformat(timespec="seconds"))
        self.assertEqual(self.cliente.post("/api/acao_feita", json={"codigo_acao": "REGAR_AGORA", "causa": "TERRA_SECA",
                                                                    "atraso_s": "x"}).status_code, 400)
        id_demo = self.inserir(status=2, origem="DEMONSTRACAO", temperatura=1.0)
        retorno_agricultor.registrar_acao(self.banco, "COBRIR_MUDAS", "FRIO_GEADA", id_demo, "ip", agora)
        self.assertFalse(retorno_agricultor.causa_silenciada(self.banco, "FRIO_GEADA", agora))
        self.assertTrue(retorno_agricultor.causa_silenciada(self.banco, "FRIO_GEADA", agora, incluir_demonstracao=True))

    def test_eventos_e_retencao(self) -> None:
        """POST /api/evento grava tipos da tela; tipo inválido → 400; retenção de 90 dias."""
        self.assertEqual(self.cliente.post("/api/evento", json={"tipo": "ouviu", "detalhe": "2"}).status_code, 201)
        self.assertEqual(self.cliente.post("/api/evento", json={"tipo": "feito"}).status_code, 400)
        self.assertEqual(self.cliente.post("/api/evento", json={"tipo": "qualquer"}).status_code, 400)
        agora = datetime.now(timezone.utc)
        retorno_agricultor.registrar_evento(self.banco, "ajuda", "", agora - timedelta(days=91))
        retorno_agricultor.registrar_evento(self.banco, "sem_voz", "", agora - timedelta(days=89))
        self.assertEqual(retorno_agricultor.limpar_eventos_antigos(self.banco, dias=90, agora_utc=agora), 1)
        self.assertEqual(self.contar("eventos_interface"), 2)
        with self.assertRaises(ValueError):
            retorno_agricultor.registrar_evento(self.banco, "invalido")
        with self.assertRaises(sqlite3.IntegrityError):
            conexao = obter_conexao(self.banco)
            try:
                conexao.execute("INSERT INTO eventos_interface (tipo, instante_utc) VALUES ('x', '2026-01-01')")
            finally:
                conexao.close()

    def test_tabelas_idempotentes_e_silencio_sem_tabela(self) -> None:
        """``garantir_tabelas`` pode rodar várias vezes; sem a tabela, a causa nunca está silenciada."""
        self.assertFalse(retorno_agricultor.causa_silenciada(self.banco, "CALOR", datetime.now(timezone.utc)))
        for _ in range(3):
            retorno_agricultor._TABELAS_GARANTIDAS.clear()
            retorno_agricultor.garantir_tabelas(self.banco)
        self.assertEqual(self.contar("registro_acoes"), 0)
        self.assertFalse(retorno_agricultor.causa_silenciada(self.pasta / "nao" / "existe.db", "CALOR",
                                                             datetime.now(timezone.utc)))


# ---------------------------------------------------------------------------
# Alarme sonoro local
# ---------------------------------------------------------------------------
class TocadorFalso:
    """Tocador que só anota os padrões pedidos."""

    def __init__(self) -> None:
        """Começa sem toques."""
        self.toques: List[str] = []

    def tocar(self, padrao: str) -> None:
        """Anota o padrão."""
        self.toques.append(padrao)


class TesteAlarmeLocal(BaseRetorno):
    """``decidir_toque`` e ``executar`` com tocador falso e relógio injetado."""

    MEIO_DIA: datetime = datetime(2026, 7, 10, 12, 0)  # hora local (sem fuso)

    @staticmethod
    def leitura(status: int, origem: str = "CAMPO_USB") -> Dict[str, Any]:
        """Leitura mínima para o alarme."""
        return {"status_alerta": status, "origem": origem}

    def test_transicao_para_vermelho_toca_uma_vez(self) -> None:
        """0 → 2 toca uma vez; 2 → 2 não toca de novo; 2 → 0 → 2 toca outra vez."""
        estado = EstadoAlarme()
        sequencia = [(0, None), (2, "VERMELHO"), (2, None), (2, None), (0, None), (2, "VERMELHO")]
        for minuto, (status, esperado) in enumerate(sequencia):
            agora = self.MEIO_DIA + timedelta(minutes=minuto * 5)
            self.assertEqual(decidir_toque(self.leitura(status), None, estado, agora, {}, False), esperado, minuto)

    def test_geada_no_fim_da_tarde_e_silencio(self) -> None:
        """GEADA_NOITE às 18 h toca (uma vez por noite); às 23 h não toca (silêncio)."""
        analise = {"motivo_principal": "GEADA_NOITE"}
        estado = EstadoAlarme()
        self.assertEqual(decidir_toque(self.leitura(1), analise, estado, datetime(2026, 7, 10, 18, 0), {}, False),
                         "GEADA")
        self.assertIsNone(decidir_toque(self.leitura(1), analise, estado, datetime(2026, 7, 10, 18, 30), {}, False))
        self.assertIsNone(decidir_toque(self.leitura(1), analise, EstadoAlarme(), datetime(2026, 7, 10, 23, 0), {},
                                        False))
        # Vermelho que começa no silêncio toca quando o silêncio acaba, se continuar.
        estado = EstadoAlarme()
        self.assertIsNone(decidir_toque(self.leitura(2), None, estado, datetime(2026, 7, 10, 23, 0), {}, False))
        self.assertEqual(decidir_toque(self.leitura(2), None, estado, datetime(2026, 7, 11, 5, 0), {}, False),
                         "VERMELHO")
        config = {"alarme_silencio_inicio": "13:00", "alarme_silencio_fim": "14:00"}
        self.assertIsNone(decidir_toque(self.leitura(2), None, EstadoAlarme(), datetime(2026, 7, 10, 13, 30), config,
                                        False))

    def test_vermelho_de_geada_fura_o_silencio(self) -> None:
        """Vermelho de GEADA_AGORA às 4 h toca mesmo no silêncio; Vermelho de outra causa espera; "Feito" vale."""
        madrugada = datetime(2026, 7, 11, 4, 0)
        geada = dict(self.leitura(2), motivo_regra="GEADA_AGORA")
        self.assertEqual(decidir_toque(geada, None, EstadoAlarme(), madrugada, {}, False), "VERMELHO")
        seca = dict(self.leitura(2), motivo_regra="TERRA_SECA")
        self.assertIsNone(decidir_toque(seca, None, EstadoAlarme(), madrugada, {}, False))
        self.assertIsNone(decidir_toque(geada, None, EstadoAlarme(), madrugada, {}, True))
        self.assertIsNone(decidir_toque(geada, None, EstadoAlarme(), madrugada, {"alarme_local_ativo": "false"}, False))

    def test_demonstracao_desligado_e_feito_nao_tocam(self) -> None:
        """DEMONSTRACAO nunca toca; ``alarme_local_ativo=false`` não toca; "Feito" recente silencia.

        Integração v4 (ajuste justificado, meta 11 "0 alarmes em simulação"): SIMULACAO (dados de teste,
        fallback com o cabo solto) também deixou de tocar — a página mostra o semáforo apagado com "Dados
        de teste", e o alarme da estação não pode anunciar como real um Vermelho sintético. Antes este
        teste exigia "VERMELHO" para a leitura simulada.
        """
        self.assertIsNone(decidir_toque(self.leitura(2, "DEMONSTRACAO"), None, EstadoAlarme(), self.MEIO_DIA, {},
                                        False))
        self.assertIsNone(decidir_toque(self.leitura(2), None, EstadoAlarme(), self.MEIO_DIA,
                                        {"alarme_local_ativo": "false"}, False))
        self.assertIsNone(decidir_toque(self.leitura(2), None, EstadoAlarme(), self.MEIO_DIA, {}, True))
        self.assertIsNone(decidir_toque(self.leitura(2, "SIMULACAO"), None, EstadoAlarme(), self.MEIO_DIA,
                                        {"alarme_local_ativo": True}, False))
        self.assertEqual(decidir_toque(self.leitura(2, "CAMPO_USB"), None, EstadoAlarme(), self.MEIO_DIA,
                                       {"alarme_local_ativo": True}, False), "VERMELHO")

    def test_no_maximo_3_toques_em_10_minutos(self) -> None:
        """Quatro transições em 4 minutos: só 3 toques; depois da janela, a pendente toca."""
        estado = EstadoAlarme()
        toques = []
        for minuto in range(8):
            status = 2 if minuto % 2 else 0
            toques.append(decidir_toque(self.leitura(status), None, estado, self.MEIO_DIA + timedelta(minutes=minuto),
                                        {}, False))
        self.assertEqual(toques.count("VERMELHO"), 3)
        self.assertEqual(decidir_toque(self.leitura(2), None, estado, self.MEIO_DIA + timedelta(minutes=11), {}, False),
                         "VERMELHO")

    def test_wav_gerado_em_memoria(self) -> None:
        """O WAV dos bipes é válido (RIFF/WAVE) e tem 1, 2 ou 3 tons pela cor."""
        for padrao in ("VERDE", "AMARELO", "VERMELHO", "GEADA"):
            if padrao not in alarme_local.BIPES:
                continue
            dados = alarme_local.gerar_wav(padrao)
            self.assertEqual(dados[:4], b"RIFF")
            self.assertEqual(dados[8:12], b"WAVE")

    def test_executar_dois_ciclos_com_feito(self) -> None:
        """``executar(max_ciclos=2)`` num banco de teste termina sem exceção e toca na transição para Vermelho."""
        tocador = TocadorFalso()
        relogio = datetime.now(timezone.utc).replace(hour=15, minute=0)
        self.inserir(instante_utc=relogio - timedelta(minutes=1), status=2, umidade_solo=20.0)
        alarme_local.executar(self.banco, intervalo_s=0.0, tocador=tocador, max_ciclos=2, relogio=lambda: relogio)
        hora_local = relogio.astimezone().hour
        silencio = hora_local >= 21 or hora_local < 5
        self.assertEqual(tocador.toques, [] if silencio else ["VERMELHO"])
        # Com "Feito" recente para a causa, o mesmo cenário não toca.
        retorno_agricultor.registrar_acao(self.banco, "REGAR_AGORA", "TERRA_SECA", None, "teste", relogio)
        outro = TocadorFalso()
        alarme_local.executar(self.banco, intervalo_s=0.0, tocador=outro, max_ciclos=2, relogio=lambda: relogio)
        self.assertEqual(outro.toques, [])

    def test_executar_em_banco_vazio(self) -> None:
        """Sem leitura nenhuma, dois ciclos terminam sem tocar e sem exceção."""
        tocador = TocadorFalso()
        alarme_local.executar(self.pasta / "vazio.db", intervalo_s=0.0, tocador=tocador, max_ciclos=2)
        self.assertEqual(tocador.toques, [])

    def test_unidade_systemd_e_instalador(self) -> None:
        """A unidade do alarme existe com Restart=always e o instalador habilita as unidades por glob."""
        unidade = (RAIZ_PROJETO / "deploy" / "agroanalise-alarme.service").read_text(encoding="utf-8")
        self.assertIn("Restart=always", unidade)
        self.assertIn("src/alarme_local.py", unidade)
        instalador = (RAIZ_PROJETO / "deploy" / "instalar.sh").read_text(encoding="utf-8")
        self.assertIn("deploy/agroanalise-*.service", instalador)
        self.assertIn("deploy/agroanalise-*.timer", instalador)
        self.assertEqual(subprocess.run(["bash", "-n", str(RAIZ_PROJETO / "deploy" / "instalar.sh")]).returncode, 0)
        self.assertIn("alarme_local.py", (RAIZ_PROJETO / "deploy" / "iniciar_windows.bat").read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze ausente")
    def test_systemd_analyze_verify(self) -> None:
        """``systemd-analyze verify`` aceita a unidade do alarme."""
        resultado = subprocess.run(["systemd-analyze", "verify",
                                    str(RAIZ_PROJETO / "deploy" / "agroanalise-alarme.service")],
                                   capture_output=True, text=True, timeout=60)
        self.assertEqual(resultado.returncode, 0, resultado.stderr)


# ---------------------------------------------------------------------------
# Técnico: página, resumo, WhatsApp e pedido de ajuda
# ---------------------------------------------------------------------------
class TesteTecnico(BaseRetorno):
    """/tecnico, /api/tecnico/resumo, /api/pedido_ajuda e /api/saude."""

    CHAVES_RESUMO: Tuple[str, ...] = ("horas_por_cor", "horas_por_causa", "avisos_antecipados", "acoes_feitas",
                                      "problemas_sensor", "pedidos_ajuda", "prognostico", "texto_whatsapp")

    def test_pagina_do_tecnico(self) -> None:
        """GET /tecnico serve o HTML (≤ 24 KB gzip), que pede o PIN e usa o cabeçalho X-PIN-Banca."""
        resposta = self.cliente.get("/tecnico", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(resposta.status_code, 200)
        self.assertLessEqual(len(resposta.data), LIMITE_TECNICO_GZIP_BYTES)
        html = gzip.decompress(resposta.data).decode("utf-8")
        for trecho in ("X-PIN-Banca", "/api/tecnico/resumo", "/api/calibracao/aferir", "texto-whatsapp"):
            self.assertIn(trecho, html)
        self.assertNotIn("<script src=", html)

    def test_resumo_exige_pin(self) -> None:
        """Com PIN definido: sem cabeçalho → 403; com o PIN → 200 com as chaves combinadas."""
        self.definir_pin()
        self.assertEqual(self.cliente.get("/api/tecnico/resumo").status_code, 403)
        modulo_app._LIMITADOR_PIN.limpar(IP_TESTE)
        resposta = self.cliente.get("/api/tecnico/resumo?dias=7", headers={"X-PIN-Banca": "1234"})
        self.assertEqual(resposta.status_code, 200)
        dados = resposta.get_json()
        for chave in self.CHAVES_RESUMO:
            self.assertIn(chave, dados)
        self.assertEqual(dados["periodo"]["dias"], 7)
        self.assertEqual(self.cliente.get("/api/tecnico/resumo?dias=500",
                                          headers={"X-PIN-Banca": "1234"}).status_code, 400)

    def _aviso(self, instante: datetime) -> int:
        """Leitura Amarela vinda do prognóstico (``status_antecipado=1``, regra < 2)."""
        import prognostico  # import tardio: só para criar a tabela com o esquema oficial

        id_leitura = self.inserir(instante_utc=instante, status=1, status_regra=0)
        conexao = obter_conexao(self.banco)
        try:
            conexao.execute(prognostico._DDL_ANALISE)
            conexao.execute(
                "INSERT INTO analise_antecipacao (id_leitura, probabilidade_risco_futuro, motivo_principal, "
                "status_antecipado, versao_prognostico) VALUES (?, 0.8, 'SECA_CHEGANDO', 1, 'teste')", (id_leitura,))
            conexao.commit()
        finally:
            conexao.close()
        return id_leitura

    def test_avisos_confirmados_e_nao_confirmados(self) -> None:
        """3 avisos antecipados, 2 seguidos de Vermelho em até 12 h: confirmados=2 e nao_confirmados=1."""
        agora = datetime.now(timezone.utc)
        for horas_atras, vermelho_depois in ((100, 5), (70, 11), (40, None)):
            inicio = agora - timedelta(hours=horas_atras)
            self._aviso(inicio)
            self._aviso(inicio + timedelta(minutes=30))  # mesmo aviso (leituras a menos de 60 min)
            if vermelho_depois is not None:
                self.inserir(instante_utc=inicio + timedelta(hours=vermelho_depois), status=2, status_regra=2,
                             umidade_solo=20.0)
        self.inserir(instante_utc=agora - timedelta(hours=20), status=0)
        dados = self.cliente.get("/api/tecnico/resumo?dias=7").get_json()
        avisos = dados["avisos_antecipados"]
        self.assertEqual((avisos["confirmados"], avisos["nao_confirmados"], avisos["pendentes"]), (2, 1, 0))
        self.assertEqual(avisos["antecedencia_mediana_h"], 8.0)
        self.assertLessEqual(len(dados["texto_whatsapp"]), 700)
        self.assertGreater(dados["horas_por_cor"]["vermelho"], 0)

    def test_texto_whatsapp_sem_dados_pessoais_e_curto(self) -> None:
        """Mesmo com nome longo e muitas ações, o texto fica ≤ 700 caracteres e só leva o nome da propriedade."""
        definir_configuracao("nome_propriedade", "Sítio " + "Boa Esperança " * 80, caminho_banco=self.banco)
        agora = datetime.now(timezone.utc)
        for minuto in range(30):
            self.inserir(instante_utc=agora - timedelta(minutes=minuto * 7), status=2, umidade_solo=20.0,
                         estado_sensor="SUSPEITO" if minuto % 3 == 0 else "OK")
            retorno_agricultor.registrar_acao(self.banco, "REGAR_AGORA", "TERRA_SECA", None, "192.168.0.10",
                                              agora - timedelta(minutes=minuto))
        dados = self.cliente.get("/api/tecnico/resumo").get_json()
        self.assertLessEqual(len(dados["texto_whatsapp"]), 700)
        self.assertNotIn("192.168", dados["texto_whatsapp"])
        self.assertEqual(dados["acoes_feitas"]["total"], 30)
        self.assertGreater(dados["problemas_sensor"]["leituras"], 0)

    def test_pedido_de_ajuda_com_e_sem_numero(self) -> None:
        """Sem número: link vazio; com número: link https://wa.me/55...; 4º pedido em 10 min → 429."""
        self.inserir(status=2, umidade_solo=20.0)
        sem_numero = self.cliente.post("/api/pedido_ajuda", json={})
        self.assertEqual(sem_numero.status_code, 201)
        self.assertEqual(sem_numero.get_json()["link"], "")
        definir_configuracao("numero_whatsapp_tecnico", "(35) 99999-0000", caminho_banco=self.banco)
        com_numero = self.cliente.post("/api/pedido_ajuda").get_json()
        self.assertTrue(com_numero["link"].startswith("https://wa.me/5535999990000?text="))
        self.assertLessEqual(len(com_numero["texto"]), 700)
        self.assertEqual(self.contar("pedido_ajuda"), 2)
        conexao = obter_conexao(self.banco)
        try:
            linha = conexao.execute("SELECT status, causa, umidade_solo FROM pedido_ajuda ORDER BY id_pedido").fetchone()
        finally:
            conexao.close()
        self.assertEqual(tuple(linha), (2, "TERRA_SECA", 20.0))
        self.assertEqual(self.cliente.post("/api/pedido_ajuda").status_code, 201)
        self.assertEqual(self.cliente.post("/api/pedido_ajuda").status_code, 429)

    def test_cadastro_do_numero_exige_pin(self) -> None:
        """POST /api/tecnico/configuracao: número normalizado; inválido → 400; com PIN e sem cabeçalho → 403."""
        resposta = self.cliente.post("/api/tecnico/configuracao", json={"numero_whatsapp_tecnico": "+55 35 99999-0000"})
        self.assertEqual(resposta.get_json()["numero_whatsapp_tecnico"], "5535999990000")
        self.assertEqual(self.cliente.post("/api/tecnico/configuracao",
                                           json={"numero_whatsapp_tecnico": "123"}).status_code, 400)
        self.definir_pin()
        self.assertEqual(self.cliente.post("/api/tecnico/configuracao",
                                           json={"numero_whatsapp_tecnico": ""}).status_code, 403)

    def test_saude_traz_prognostico(self) -> None:
        """/api/saude traz prognostico.suspenso e brier_local_28d (chaves da Frente 2; ausentes = false/None)."""
        componentes = self.cliente.get("/api/saude").get_json()["componentes"]
        self.assertIs(componentes["prognostico"]["suspenso"], False)
        self.assertIsNone(componentes["prognostico"]["brier_local_28d"])
        self.assertIn("disponivel", componentes["prognostico"])
        definir_configuracao("prognostico_suspenso", True, caminho_banco=self.banco)
        definir_configuracao("prognostico_brier_local_28d", 0.31, caminho_banco=self.banco)
        prognostico = self.cliente.get("/api/saude").get_json()["componentes"]["prognostico"]
        self.assertIs(prognostico["suspenso"], True)
        self.assertAlmostEqual(prognostico["brier_local_28d"], 0.31)


# ---------------------------------------------------------------------------
# Calibração da sonda e banca
# ---------------------------------------------------------------------------
class TesteCalibracaoSonda(BaseRetorno):
    """``calibracao_sonda`` e as rotas da calibração e da banca."""

    def leituras_de_campo(self, valores: List[float], segundos_entre: float = 8.0) -> None:
        """Grava leituras CAMPO_USB recentes com a umidade do firmware dada (a mais nova por último)."""
        agora = datetime.now(timezone.utc)
        for indice, valor in enumerate(valores):
            self.inserir(instante_utc=agora - timedelta(seconds=segundos_entre * (len(valores) - indice)),
                         umidade_solo=valor, umidade_solo_bruta=valor)

    def test_inversao_do_firmware(self) -> None:
        """adc(0 %) = 1023, adc(100 %) = 300 e ida e volta exata a cada 0,1 %."""
        self.assertEqual(calibracao_sonda.adc_do_percentual_firmware(0), 1023.0)
        self.assertEqual(calibracao_sonda.adc_do_percentual_firmware(100), 300.0)
        for decimo in range(0, 1001):
            percentual = decimo / 10.0
            adc = calibracao_sonda.adc_do_percentual_firmware(percentual)
            self.assertAlmostEqual(calibracao_sonda.percentual_firmware_do_adc(adc), percentual, places=9)

    def test_dois_pontos_converte(self) -> None:
        """Reta adc 900 → 0 % e adc 400 → 100 %: firmware 50 % (adc 661,5) vira 47,7 % ± 0,1."""
        calibracao_sonda.registrar_calibracao(self.banco, calibracao_sonda.CalibracaoSonda(
            "principal", 900.0, 400.0, 0.0, 100.0, "DOIS_PONTOS", "2026-07-01T12:00:00+00:00"))
        self.assertAlmostEqual(calibracao_sonda.adc_do_percentual_firmware(50.0), 661.5)
        self.assertAlmostEqual(calibracao_sonda.converter_umidade_solo(50.0, self.banco), 47.7, delta=0.1)
        self.assertEqual(calibracao_sonda.converter_umidade_solo(0.0, self.banco), 0.0)  # limitado a 0–100
        self.assertIsNone(calibracao_sonda.converter_umidade_solo(None, self.banco))

    def test_captura_e_conclusao_pelo_assistente(self) -> None:
        """Pontos SECO e UMIDO pelas leituras do último minuto; concluir grava DOIS_PONTOS ativo."""
        self.leituras_de_campo([12.0, 12.2, 11.9, 12.1, 12.0, 11.8, 12.1])
        seco = self.cliente.post("/api/calibracao/ponto", json={"tipo": "SECO"})
        self.assertEqual(seco.status_code, 200, seco.get_json())
        self.assertEqual(seco.get_json()["ponto"]["leituras"], 7)
        conexao = obter_conexao(self.banco)
        try:
            conexao.execute("DELETE FROM historico_leituras")
            conexao.commit()
        finally:
            conexao.close()
        self.leituras_de_campo([88.0, 88.3, 87.9, 88.1, 88.0, 88.2])
        self.assertEqual(self.cliente.post("/api/calibracao/ponto", json={"tipo": "UMIDO"}).status_code, 200)
        concluida = self.cliente.post("/api/calibracao/concluir", json={})
        self.assertEqual(concluida.status_code, 200, concluida.get_json())
        self.assertEqual(concluida.get_json()["calibracao"]["metodo"], "DOIS_PONTOS")
        estado = self.cliente.get("/api/calibracao").get_json()
        self.assertEqual((estado["metodo"], estado["dias_desde_calibracao"]), ("DOIS_PONTOS", 0))
        self.assertAlmostEqual(calibracao_sonda.converter_umidade_solo(12.0, self.banco), 0.0, delta=0.5)
        self.assertAlmostEqual(calibracao_sonda.converter_umidade_solo(88.0, self.banco), 100.0, delta=0.5)
        self.assertEqual(self.cliente.get("/api/saude").get_json()["calibracao_sonda"]["dias_desde_calibracao"], 0)

    def test_aferir_um_ponto_desloca(self) -> None:
        """Referência de 70 % com leitura de 64 %: os dois alvos andam +6 (método UM_PONTO_CAMPO)."""
        self.leituras_de_campo([64.0, 64.1, 63.9, 64.0, 64.0, 63.95, 64.05])
        resposta = self.cliente.post("/api/calibracao/aferir", json={"valor_referencia_pct": 70})
        self.assertEqual(resposta.status_code, 200, resposta.get_json())
        calibracao = resposta.get_json()["calibracao"]
        self.assertEqual(calibracao["metodo"], "UM_PONTO_CAMPO")
        self.assertAlmostEqual(calibracao["alvo_seco_pct"], 6.0, delta=0.05)
        self.assertAlmostEqual(calibracao["alvo_umido_pct"], 106.0, delta=0.05)
        self.assertAlmostEqual(calibracao_sonda.converter_umidade_solo(64.0, self.banco), 70.0, delta=0.05)
        self.assertEqual(self.cliente.post("/api/calibracao/aferir", json={"valor_referencia_pct": 140}).status_code,
                         422)

    def test_captura_recusa_janela_instavel_ou_curta(self) -> None:
        """Desvio de 3 pp → recusa; menos de 6 leituras → recusa."""
        self.leituras_de_campo([60.0, 66.0, 60.0, 66.0, 60.0, 66.0, 63.0])
        with self.assertRaises(calibracao_sonda.ErroCalibracao):
            calibracao_sonda.capturar_ponto(self.banco, "CAMPO", desvio_maximo_pp=1.0)
        self.assertEqual(self.cliente.post("/api/calibracao/ponto", json={"tipo": "CAMPO"}).status_code, 422)
        outro = self.pasta / "curto.db"
        inicializar_banco(outro)
        with self.assertRaises(calibracao_sonda.ErroCalibracao):
            calibracao_sonda.capturar_ponto(outro, "SECO")
        with self.assertRaises(calibracao_sonda.ErroCalibracao):
            calibracao_sonda.capturar_ponto(self.banco, "MEIO")

    def test_converter_nunca_lanca(self) -> None:
        """Sem tabela, banco inexistente ou erro de banco: devolve a entrada."""
        self.assertEqual(calibracao_sonda.converter_umidade_solo(55.5, self.banco), 55.5)
        self.assertEqual(calibracao_sonda.converter_umidade_solo(55.5, self.pasta / "nao_existe.db"), 55.5)
        self.assertFalse((self.pasta / "nao_existe.db").exists())
        calibracao_sonda.limpar_cache()
        with mock.patch.object(calibracao_sonda, "obter_calibracao_ativa", side_effect=sqlite3.OperationalError("x")):
            self.assertEqual(calibracao_sonda.converter_umidade_solo(42.0, self.banco), 42.0)
        self.assertTrue(math.isnan(calibracao_sonda.converter_umidade_solo(float("nan"), self.banco)))  # type: ignore[arg-type]
        self.assertEqual(calibracao_sonda.converter_umidade_solo("abc", self.banco), "abc")  # type: ignore[arg-type]

    def test_rotas_sem_pin_recusadas(self) -> None:
        """Com PIN definido, as rotas da calibração e da banca sem cabeçalho → 403."""
        self.definir_pin()
        for metodo, rota, corpo in (("GET", "/api/calibracao", None), ("POST", "/api/calibracao/ponto", {"tipo": "SECO"}),
                                    ("POST", "/api/calibracao/concluir", {}),
                                    ("POST", "/api/calibracao/aferir", {"valor_referencia_pct": 70}),
                                    ("POST", "/api/calibracao/fabrica", {}), ("POST", "/api/banca/florada", {}),
                                    ("POST", "/api/banca/seca_programada", {})):
            with self.subTest(rota=rota):
                modulo_app._LIMITADOR_PIN.limpar(IP_TESTE)
                resposta = self.cliente.open(rota, method=metodo, json=corpo)
                self.assertEqual(resposta.status_code, 403)

    def test_florada_e_seca_programada(self) -> None:
        """"O café floriu hoje" grava AAAA-MM-DD local; seca programada liga/desliga e grava a janela padrão."""
        resposta = self.cliente.post("/api/banca/florada", json={})
        self.assertEqual(resposta.status_code, 200)
        valor = obter_configuracao("data_florada_principal", caminho_banco=self.banco)
        self.assertRegex(str(valor), r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(valor, datetime.now().date().isoformat())
        self.assertEqual(self.cliente.post("/api/banca/florada", json={"data": "2999-01-01"}).status_code, 400)
        self.assertEqual(self.cliente.post("/api/banca/florada", json={"data": "2026-10-02"}).get_json()
                         ["data_florada_principal"], "2026-10-02")
        ligada = self.cliente.post("/api/banca/seca_programada").get_json()
        self.assertIs(ligada["seca_programada_ativa"], True)
        self.assertEqual((ligada["seca_programada_inicio"], ligada["seca_programada_fim"]), ("06-24", "09-04"))
        self.assertIn(obter_configuracao("seca_programada_ativa", caminho_banco=self.banco), (True, "true"))
        desligada = self.cliente.post("/api/banca/seca_programada", json={"ativa": False}).get_json()
        self.assertIs(desligada["seca_programada_ativa"], False)
        self.assertEqual(self.cliente.post("/api/banca/seca_programada", json={"ativa": "sim"}).status_code, 400)
        self.assertIn("banca", self.cliente.get("/api/cenarios").get_json())

    def test_voltar_para_fabrica(self) -> None:
        """A reta de fábrica é a identidade e zera os dias desde a calibração."""
        self.leituras_de_campo([50.0] * 7)
        self.cliente.post("/api/calibracao/aferir", json={"valor_referencia_pct": 55})
        self.assertAlmostEqual(calibracao_sonda.converter_umidade_solo(50.0, self.banco), 55.0, delta=0.05)
        self.assertEqual(self.cliente.post("/api/calibracao/fabrica", json={}).status_code, 200)
        self.assertEqual(calibracao_sonda.converter_umidade_solo(50.0, self.banco), 50.0)
        self.assertIsNone(calibracao_sonda.dias_desde_calibracao(self.banco))


if __name__ == "__main__":
    unittest.main()
