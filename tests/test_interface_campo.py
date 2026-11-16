"""Testes da frente "interface de campo" do AgroAnálise (unittest da biblioteca padrão).

Cobrem, sempre com banco SQLite TEMPORÁRIO:

    * ``acoes_orientacao``: pictogramas de ação por cenário (Arábica e
      Conilon), antecipação, defeito de sensor e a propriedade "toda ação
      tem a sua palavra-chave na mensagem falada" em 5.000 linhas simuladas;
    * ``seguranca_banca``: hash PBKDF2 do PIN, limitador de tentativas e a
      linha de comando;
    * rotas: PIN da banca (403/429), limite de corpo (413), registro da troca
      de cultura, gzip, estado embutido na página e intervalo de repetição;
    * status e saúde honestos: antecipação (módulo ``prognostico`` falso),
      relógio errado, ordenação por id (depois do merge), coletor parado,
      disco cheio, demonstração em UTC e sensor com defeito.

Execução (a partir da raiz do projeto): ``python -m unittest discover -s tests -v``.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from collections import namedtuple
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

RAIZ_PROJETO: Path = Path(__file__).resolve().parent.parent
PASTA_SRC: Path = RAIZ_PROJETO / "src"
if str(PASTA_SRC) not in sys.path:
    sys.path.insert(0, str(PASTA_SRC))

import acoes_orientacao  # noqa: E402
import app as modulo_app  # noqa: E402
import seguranca_banca  # noqa: E402
from acoes_orientacao import (  # noqa: E402
    ACOES_VALIDAS,
    PALAVRAS_CHAVE_ACAO,
    acoes_coerentes_com_mensagem,
    derivar_acoes,
)
from app import app  # noqa: E402
from coletor_serial import ColetorAgroAnalise  # noqa: E402
from database import (  # noqa: E402
    ParametrosCultura,
    RegistroLeitura,
    buscar_parametros_cultura,
    definir_configuracao,
    inicializar_banco,
    inserir_leitura,
    obter_configuracao,
    obter_conexao,
)
from gerador_dataset import (  # noqa: E402
    calcular_features,
    calcular_status_regra,
    gerar_leitura_cenario,
    gerar_mensagem_orientacao,
    simular_serie_temporal,
)
from seguranca_banca import LimitadorTentativas, gerar_hash_pin, verificar_pin  # noqa: E402

FORMATO: str = "%Y-%m-%d %H:%M:%S"
IP_TESTE: str = "127.0.0.1"
LIMITE_PAGINA_GZIP_BYTES: int = 16 * 1024


def setUpModule() -> None:
    """Silencia os logs informativos/avisos durante os testes."""
    logging.disable(logging.WARNING)


def tearDownModule() -> None:
    """Reativa os logs."""
    logging.disable(logging.NOTSET)


def _colunas_historico(banco: Path) -> List[str]:
    """Nomes das colunas de ``historico_leituras``."""
    conexao = obter_conexao(banco)
    try:
        return [str(linha["name"]) for linha in conexao.execute("PRAGMA table_info(historico_leituras)")]
    finally:
        conexao.close()


def _garantir_coluna(banco: Path, coluna: str) -> None:
    """Cria a coluna de texto em ``historico_leituras`` se ela ainda não existir (antes do merge da confiabilidade)."""
    if coluna in _colunas_historico(banco):
        return
    conexao = obter_conexao(banco)
    try:
        conexao.execute(f"ALTER TABLE historico_leituras ADD COLUMN {coluna} TEXT")
        conexao.commit()
    finally:
        conexao.close()


def _atualizar_leitura(banco: Path, id_leitura: int, **valores: Any) -> None:
    """UPDATE direto de colunas de uma leitura (para simular dados da confiabilidade-campo)."""
    conexao = obter_conexao(banco)
    try:
        for coluna, valor in valores.items():
            conexao.execute(f"UPDATE historico_leituras SET {coluna} = ? WHERE id_leitura = ?", (valor, id_leitura))
        conexao.commit()
    finally:
        conexao.close()


def _esquema_tem_instante_utc() -> bool:
    """``True`` quando o esquema oficial (após o merge da confiabilidade-campo) já cria ``instante_utc``."""
    pasta = tempfile.mkdtemp(prefix="agro_esquema_")
    try:
        banco = Path(pasta) / "esquema.db"
        inicializar_banco(banco)
        return "instante_utc" in _colunas_historico(banco)
    finally:
        shutil.rmtree(pasta, ignore_errors=True)


class BaseInterface(unittest.TestCase):
    """Banco temporário, app apontado para ele (sem modelo) e estado global limpo."""

    def setUp(self) -> None:
        """Cria o banco temporário e prepara o cliente de testes."""
        self._diretorio = tempfile.mkdtemp(prefix="agro_interface_")
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
        modulo_app._LIMITADOR_PIN.limpar(IP_TESTE)
        self._prognostico_original = sys.modules.get("prognostico", "ausente")
        modulo_app._CACHE_ANTECIPACAO["falhou_em"] = None

    def tearDown(self) -> None:
        """Restaura o app, o módulo de antecipação e remove o diretório temporário."""
        app.config["CAMINHO_BANCO"], app.config["CAMINHO_MODELO"] = self._config_original
        modulo_app._LIMITADOR_PIN.limpar(IP_TESTE)
        if self._prognostico_original == "ausente":
            sys.modules.pop("prognostico", None)
        else:
            sys.modules["prognostico"] = self._prognostico_original  # type: ignore[assignment]
        modulo_app._CACHE_ANTECIPACAO["falhou_em"] = None
        shutil.rmtree(self._diretorio, ignore_errors=True)

    def inserir(self, segundos_atras: float = 1.0, status: int = 0, origem: str = "CAMPO_USB",
                temperatura: float = 20.0, umidade_solo: float = 70.0, data_hora: Optional[datetime] = None,
                mensagem: Optional[str] = None) -> int:
        """Grava uma leitura com data/hora local relativa a agora."""
        instante = data_hora or (datetime.now() - timedelta(seconds=segundos_atras))
        deficit, desvio = calcular_features(temperatura, umidade_solo, self.arabica)
        return inserir_leitura(RegistroLeitura(
            data_hora=instante.strftime(FORMATO), codigo_cultura="cafe_arabica", origem=origem,
            temperatura_ar_bruta=temperatura, umidade_ar_bruta=70.0, umidade_solo_bruta=umidade_solo,
            temperatura_ar=temperatura, umidade_ar=70.0, umidade_solo=umidade_solo, deficit_hidrico_solo=deficit,
            desvio_termico=desvio, status_regra=status, status_predito=status, status_alerta=status,
            probabilidade_predicao=1.0, flag_anomalia=0, descricao_anomalia="",
            mensagem_voz=mensagem or {0: "Tudo bem.", 1: "Atenção.", 2: "Perigo."}[status],
            versao_modelo="REGRAS"), caminho_banco=self.banco)

    def status(self) -> Dict[str, Any]:
        """GET /api/status."""
        return self.cliente.get("/api/status").get_json()


# ---------------------------------------------------------------------------
# Pictogramas de ação
# ---------------------------------------------------------------------------
class TesteAcoesOrientacao(BaseInterface):
    """``derivar_acoes`` coerente com a mensagem falada."""

    ESPERADO: Dict[str, List[str]] = {
        "SECA_EXTREMA": ["REGAR_AGORA"],
        "GEADA": ["COBRIR_MUDAS", "CHEGAR_TERRA"],
        "ENCHARCAMENTO": ["NAO_REGAR", "ABRIR_VALETAS"],
        "ONDA_DE_CALOR": ["FAZER_SOMBRA", "REGAR_FIM_DA_TARDE"],
        "CONDICAO_IDEAL": ["NADA_A_FAZER"],
        # Calor e solo começando a secar, ambos amarelos: "Regue hoje no fim da tarde, quando o sol baixar."
        "ATENCAO_MODERADA": ["REGAR_FIM_DA_TARDE"],
    }

    def _mensagem_e_acoes(self, temperatura: float, umidade_solo: float, parametros: ParametrosCultura,
                          status: Optional[int] = None) -> Any:
        """Mensagem falada e ações para a mesma leitura (status da regra se não for dado)."""
        nivel = calcular_status_regra(temperatura, umidade_solo, parametros) if status is None else status
        deficit, desvio = calcular_features(temperatura, umidade_solo, parametros)
        mensagem = gerar_mensagem_orientacao(nivel, deficit, desvio, parametros, temperatura, umidade_solo)
        return mensagem, derivar_acoes(nivel, temperatura, umidade_solo, parametros)

    def test_tabela_de_cenarios_arabica_e_conilon(self) -> None:
        """Cada cenário da banca vira os pictogramas certos, nas duas culturas."""
        for parametros in (self.arabica, self.conilon):
            for cenario, esperado in self.ESPERADO.items():
                with self.subTest(cultura=parametros.codigo, cenario=cenario):
                    leitura = gerar_leitura_cenario(cenario, parametros)
                    mensagem, acoes = self._mensagem_e_acoes(leitura["temperatura_ar"], leitura["umidade_solo"], parametros)
                    self.assertEqual(acoes, esperado)
                    self.assertTrue(acoes_coerentes_com_mensagem(acoes, mensagem), mensagem)

    def test_propriedade_palavras_chave_em_5000_linhas(self) -> None:
        """Em 5.000 horas simuladas por cultura, toda ação aparece na mensagem por uma palavra-chave."""
        for parametros in (self.arabica, self.conilon):
            serie = simular_serie_temporal(parametros, horas=5000)
            for temperatura, umidade_solo in zip(serie["temperatura_ar"], serie["umidade_solo"]):
                mensagem, acoes = self._mensagem_e_acoes(float(temperatura), float(umidade_solo), parametros)
                self.assertTrue(1 <= len(acoes) <= 3, (temperatura, umidade_solo, acoes))
                self.assertEqual(len(acoes), len(set(acoes)))
                for acao in acoes:
                    self.assertIn(acao, ACOES_VALIDAS)
                    self.assertTrue(any(chave in mensagem for chave in PALAVRAS_CHAVE_ACAO[acao]),
                                    f"{acao} sem palavra-chave em: {mensagem}")

    def test_alerta_da_ia_sem_causa_continua_coerente(self) -> None:
        """IA elevou o status com tudo na faixa ideal: a ação segue a borda mais próxima da mensagem."""
        for temperatura, umidade_solo in ((21.8, 70.0), (18.2, 70.0), (20.0, 79.0), (20.0, 61.0)):
            with self.subTest(temperatura=temperatura, umidade_solo=umidade_solo):
                mensagem, acoes = self._mensagem_e_acoes(temperatura, umidade_solo, self.arabica, status=1)
                self.assertTrue(acoes_coerentes_com_mensagem(acoes, mensagem), (acoes, mensagem))
                self.assertLessEqual(len(acoes), 3)

    def test_antecipacao_e_sensor(self) -> None:
        """Seca chegando acrescenta a rega no fim da tarde; geada à noite, cobrir e chegar terra; defeito → aparelho."""
        self.assertEqual(derivar_acoes(0, 20.0, 70.0, self.arabica, motivo_antecipacao="SECA_CHEGANDO"),
                         ["REGAR_FIM_DA_TARDE"])
        self.assertEqual(derivar_acoes(1, 20.0, 70.0, self.arabica, motivo_antecipacao="GEADA_NOITE"),
                         ["COBRIR_MUDAS", "CHEGAR_TERRA"])
        self.assertEqual(derivar_acoes(2, 20.0, 30.0, self.arabica, motivo_antecipacao="GEADA_NOITE"),
                         ["REGAR_AGORA", "COBRIR_MUDAS", "CHEGAR_TERRA"])
        self.assertEqual(derivar_acoes(0, 20.0, 70.0, self.arabica, motivo_antecipacao="NENHUM"), ["NADA_A_FAZER"])
        self.assertEqual(derivar_acoes(2, 20.0, 30.0, self.arabica, estado_sensor="DEFEITO"), ["VERIFICAR_APARELHO"])
        self.assertEqual(derivar_acoes(1, None, 70.0, self.arabica), ["VERIFICAR_APARELHO"])
        self.assertEqual(derivar_acoes(1, float("nan"), 70.0, self.arabica), ["VERIFICAR_APARELHO"])
        # Encharcado e geando: no máximo 3 ações, sem repetição.
        self.assertEqual(derivar_acoes(2, 2.0, 97.0, self.arabica), ["NAO_REGAR", "ABRIR_VALETAS", "COBRIR_MUDAS"])

    def test_catalogo_completo(self) -> None:
        """Toda ação válida tem palavra-chave, legenda de uma palavra e descrição."""
        for acao in ACOES_VALIDAS:
            self.assertTrue(PALAVRAS_CHAVE_ACAO[acao])
            self.assertEqual(len(acoes_orientacao.LEGENDAS_ACAO[acao].split()), 1)
            self.assertTrue(acoes_orientacao.DESCRICOES_ACAO[acao])

    def test_status_com_acoes(self) -> None:
        """/api/status traz as ações do cenário e as da simulação (dados de teste → conferir o aparelho)."""
        dados = self.cliente.post("/api/demonstracao", json={"cenario": "GEADA"}).get_json()
        self.assertEqual(dados["acoes"], ["COBRIR_MUDAS", "CHEGAR_TERRA"])
        self.assertEqual([item["legenda"] for item in dados["acoes_detalhe"]], ["Cobrir", "Terra"])
        self.cliente.post("/api/demonstracao/encerrar", json={})
        ColetorAgroAnalise(caminho_banco=self.banco, forcar_simulacao=True, intervalo_segundos=0.2,
                           max_iteracoes=1).executar()
        dados = self.status()
        self.assertEqual(dados["origem"], "SIMULACAO")
        self.assertEqual(dados["acoes"], ["VERIFICAR_APARELHO"])
        self.assertEqual(self.cliente.post("/api/cultura", json={"codigo": "cafe_conilon"}).status_code, 200)
        self.assertEqual(self.status()["acoes"], [])  # trocando de plantação: só esperar


# ---------------------------------------------------------------------------
# PIN da banca
# ---------------------------------------------------------------------------
class TesteSegurancaBanca(unittest.TestCase):
    """Hash do PIN, limitador e linha de comando."""

    def test_hash_e_verificacao(self) -> None:
        """PIN certo confere; errado, vazio ou hash malformado não."""
        hash_pin = gerar_hash_pin("1234")
        self.assertTrue(hash_pin.startswith("pbkdf2_sha256$200000$"))
        self.assertEqual(len(hash_pin.split("$")), 4)
        self.assertTrue(verificar_pin("1234", hash_pin))
        self.assertFalse(verificar_pin("0000", hash_pin))
        self.assertFalse(verificar_pin("", hash_pin))
        self.assertFalse(verificar_pin("1234", "qualquer coisa"))
        self.assertFalse(verificar_pin("1234", "pbkdf2_sha256$abc$zz$yy"))
        self.assertNotEqual(gerar_hash_pin("1234", iteracoes=1000), gerar_hash_pin("1234", iteracoes=1000))
        with self.assertRaises(ValueError):
            gerar_hash_pin("")

    def test_limitador(self) -> None:
        """5 erros bloqueiam o IP por 60 s; outro IP segue livre; limpar desbloqueia."""
        agora = [100.0]
        limitador = LimitadorTentativas(max_erros=5, bloqueio_s=60.0, relogio=lambda: agora[0])
        for _ in range(4):
            limitador.registrar_erro("1.1.1.1")
        self.assertFalse(limitador.bloqueado("1.1.1.1"))
        limitador.registrar_erro("1.1.1.1")
        self.assertTrue(limitador.bloqueado("1.1.1.1"))
        self.assertFalse(limitador.bloqueado("2.2.2.2"))
        agora[0] += 61.0
        self.assertFalse(limitador.bloqueado("1.1.1.1"))
        limitador.registrar_erro("1.1.1.1")
        self.assertFalse(limitador.bloqueado("1.1.1.1"))  # a contagem recomeçou
        for _ in range(5):
            limitador.registrar_erro("3.3.3.3")
        limitador.limpar("3.3.3.3")
        self.assertFalse(limitador.bloqueado("3.3.3.3"))

    def test_linha_de_comando(self) -> None:
        """--definir-pin grava o hash; --remover-pin grava vazio; PIN inválido é recusado."""
        pasta = tempfile.mkdtemp(prefix="agro_pin_")
        try:
            banco = Path(pasta) / "pin.db"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(seguranca_banca.main(["--definir-pin", "1234", "--banco", str(banco)]), 0)
                self.assertTrue(verificar_pin("1234", str(obter_configuracao("pin_banca_hash", caminho_banco=banco))))
                self.assertEqual(seguranca_banca.main(["--definir-pin", "12a", "--banco", str(banco)]), 2)
                self.assertEqual(seguranca_banca.main(["--remover-pin", "--banco", str(banco)]), 0)
            self.assertEqual(obter_configuracao("pin_banca_hash", caminho_banco=banco), "")
        finally:
            shutil.rmtree(pasta, ignore_errors=True)


class TesteRotasBanca(BaseInterface):
    """PIN nas rotas, limite de corpo e registro da troca de cultura."""

    def _definir_pin(self) -> None:
        """Grava o PIN '1234' pela linha de comando da segurança da banca."""
        with redirect_stdout(io.StringIO()):
            self.assertEqual(seguranca_banca.main(["--definir-pin", "1234", "--banco", str(self.banco)]), 0)

    def test_sem_pin_funciona_como_antes(self) -> None:
        """Sem pin_banca_hash as três rotas respondem sem cabeçalho."""
        self.assertEqual(self.cliente.post("/api/demonstracao", json={"cenario": "GEADA"}).status_code, 200)
        self.assertEqual(self.cliente.post("/api/demonstracao/encerrar", json={}).status_code, 200)
        self.assertEqual(self.cliente.post("/api/cultura", json={"codigo": "cafe_conilon"}).status_code, 200)

    def test_pin_exigido_e_limite_de_tentativas(self) -> None:
        """Com PIN: sem cabeçalho 403 (nada muda); certo 200; 5 erros e depois o certo → 429.

        v4 (Frente A, melhoria A7): as respostas de erro do PIN ganharam campos
        para a página reagir sem ler a mensagem — ``codigo`` no 403
        (``pin_necessario``/``pin_errado``) e ``segundos_restantes`` no 429.
        Por isso a comparação do corpo inteiro virou comparação da mensagem
        (que não mudou) mais a dos campos novos. O resto do teste não mudou.
        """
        self._definir_pin()
        resposta = self.cliente.post("/api/demonstracao", json={"cenario": "GEADA"})
        self.assertEqual(resposta.status_code, 403)
        self.assertEqual(resposta.get_json()["erro"], "Peça a senha ao técnico.")
        self.assertEqual(resposta.get_json()["codigo"], "pin_necessario")
        self.assertEqual(obter_configuracao("modo_operacao", caminho_banco=self.banco), "SIMULACAO")
        self.assertEqual(self.cliente.post("/api/cultura", json={"codigo": "cafe_conilon"}).status_code, 403)
        self.assertEqual(self.cliente.post("/api/demonstracao/encerrar", json={}).status_code, 403)
        modulo_app._LIMITADOR_PIN.limpar(IP_TESTE)
        certo = {"X-PIN-Banca": "1234"}
        self.assertEqual(self.cliente.post("/api/demonstracao", json={"cenario": "GEADA"}, headers=certo).status_code, 200)
        self.assertEqual(obter_configuracao("modo_operacao", caminho_banco=self.banco), "DEMONSTRACAO")
        for _ in range(5):
            errado = self.cliente.post("/api/demonstracao/encerrar", json={}, headers={"X-PIN-Banca": "0000"})
            self.assertEqual(errado.status_code, 403)
        bloqueado = self.cliente.post("/api/demonstracao/encerrar", json={}, headers=certo)
        self.assertEqual(bloqueado.status_code, 429)
        self.assertEqual(bloqueado.get_json()["erro"], "Muitas tentativas. Espere um minuto.")
        self.assertIn(bloqueado.get_json()["segundos_restantes"], range(1, 61))
        self.assertEqual(obter_configuracao("modo_operacao", caminho_banco=self.banco), "DEMONSTRACAO")

    def test_corpo_grande_demais(self) -> None:
        """Corpo de 10 KB → 413 com JSON em português."""
        resposta = self.cliente.post("/api/cultura", json={"codigo": "cafe_conilon", "lixo": "x" * 10240})
        self.assertEqual(resposta.status_code, 413)
        self.assertEqual(resposta.get_json(), {"erro": "Pedido grande demais."})
        self.assertEqual(obter_configuracao("cultura_ativa", caminho_banco=self.banco), "cafe_arabica")

    def test_troca_de_cultura_registrada(self) -> None:
        """A troca grava cultura_anterior e cultura_alterada_em (UTC '+00:00'); o status informa os segundos."""
        self.assertIsNone(self.status()["segundos_desde_troca_cultura"])
        self.assertEqual(self.cliente.post("/api/cultura", json={"codigo": "cafe_conilon"}).status_code, 200)
        alterada_em = str(obter_configuracao("cultura_alterada_em", caminho_banco=self.banco))
        self.assertTrue(alterada_em.endswith("+00:00"), alterada_em)
        dados = self.status()
        self.assertEqual(dados["cultura_anterior"], "cafe_arabica")
        self.assertEqual(dados["cultura_alterada_em"], alterada_em)
        self.assertTrue(0 <= dados["segundos_desde_troca_cultura"] <= 5)
        # Escolher de novo a mesma cultura não é troca.
        self.cliente.post("/api/cultura", json={"codigo": "cafe_conilon"})
        self.assertEqual(obter_configuracao("cultura_anterior", caminho_banco=self.banco), "cafe_arabica")


# ---------------------------------------------------------------------------
# Rede fraca e voz
# ---------------------------------------------------------------------------
class TesteRedeFraca(BaseInterface):
    """gzip, estado embutido na página e intervalo de repetição do alerta."""

    def test_pagina_comprimida_e_pequena(self) -> None:
        """GET / com gzip: Content-Encoding gzip e corpo ≤ 16 KB, também com leitura de cenário longa."""
        for cenario in (None, "SECA_EXTREMA", "ONDA_DE_CALOR"):
            with self.subTest(cenario=cenario):
                if cenario:
                    self.cliente.post("/api/demonstracao", json={"cenario": cenario})
                resposta = self.cliente.get("/", headers={"Accept-Encoding": "gzip, deflate"})
                self.assertEqual(resposta.status_code, 200)
                self.assertEqual(resposta.headers.get("Content-Encoding"), "gzip")
                self.assertIn("Accept-Encoding", resposta.headers.get("Vary", ""))
                self.assertEqual(int(resposta.headers["Content-Length"]), len(resposta.data))
                self.assertLessEqual(len(resposta.data), LIMITE_PAGINA_GZIP_BYTES)
                self.assertIn("estado-inicial", gzip.decompress(resposta.data).decode("utf-8"))

    def test_pagina_sem_gzip_com_estado_inicial(self) -> None:
        """Sem Accept-Encoding: HTML puro, com o estado embutido, sem CDN nem script externo."""
        self.cliente.post("/api/demonstracao", json={"cenario": "GEADA"})
        resposta = self.cliente.get("/")
        self.assertIsNone(resposta.headers.get("Content-Encoding"))
        self.assertEqual(resposta.headers.get("Cache-Control"), "no-cache")
        html = resposta.get_data(as_text=True)
        self.assertIn('id="estado-inicial"', html)
        self.assertNotIn("<script src=", html)
        self.assertNotIn("cdn.", html)
        inicio = html.index('id="estado-inicial">') + len('id="estado-inicial">')
        estado = json.loads(html[inicio:html.index("</script>", inicio)])
        self.assertEqual((estado["cor"], estado["acoes"]), ("vermelho", ["COBRIR_MUDAS", "CHEGAR_TERRA"]))

    def test_estado_inicial_nulo_se_o_banco_falhar(self) -> None:
        """Se montar o status falhar, a página sai mesmo assim com estado nulo."""
        with mock.patch.object(modulo_app, "montar_status", side_effect=sqlite3.OperationalError("falha")):
            html = self.cliente.get("/").get_data(as_text=True)
        self.assertIn('id="estado-inicial">null</script>', html)

    def test_api_comprimida(self) -> None:
        """GET /api/status com gzip descomprime para JSON válido; respostas pequenas não são comprimidas."""
        self.cliente.post("/api/demonstracao", json={"cenario": "SECA_EXTREMA"})
        resposta = self.cliente.get("/api/status", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(resposta.headers.get("Content-Encoding"), "gzip")
        dados = json.loads(gzip.decompress(resposta.data))
        self.assertEqual(dados["cor"], "vermelho")
        pequena = self.cliente.get("/api/nao_existe", headers={"Accept-Encoding": "gzip"})
        self.assertIsNone(pequena.headers.get("Content-Encoding"))
        self.assertIn("erro", pequena.get_json())

    def test_intervalo_repeticao_alerta(self) -> None:
        """Padrão de 600 s sem semear o banco; a chave criada depois passa a valer."""
        self.assertEqual(self.status()["intervalo_repeticao_alerta_segundos"], 600)
        self.assertIsNone(obter_configuracao("intervalo_repeticao_alerta_segundos", caminho_banco=self.banco))
        definir_configuracao("intervalo_repeticao_alerta_segundos", 300, caminho_banco=self.banco)
        self.assertEqual(self.status()["intervalo_repeticao_alerta_segundos"], 300)

    def test_minificacao_preserva_o_essencial(self) -> None:
        """A página servida continua com as funções exigidas e o JSON intacto.

        v3 (Frente 3): o minificador passou a encurtar os nomes LOCAIS do JS e a
        tirar os espaços entre tokens (orçamento de 16 KB gzip com causa,
        "Feito" e técnico). Por isso "function chaveAnuncio(dados)" virou o
        texto fixo que só essa função produz ("sem-leitura|") e o exemplo
        mínimo espera "var a=1;" em vez de "var a = 1;". O resto não mudou.
        """
        html = self.cliente.get("/").get_data(as_text=True)
        for trecho in ('"sem-leitura|"', "speechSynthesis", "pt-BR", "agro_voz_automatica",
                       "agro_ultimo_status", "createOscillator", "ResizeObserver", 'aria-label="O que fazer"',
                       'id="area-banca"', "Área da banca", "X-PIN-Banca"):
            self.assertIn(trecho, html)
        self.assertEqual(modulo_app.minificar_html('<script type="application/json" id="x">{"a": "/* b */"}</script>'),
                         '<script type="application/json" id="x">{"a": "/* b */"}</script>')
        self.assertEqual(modulo_app.minificar_html("<script>\n  // nota\n  var a = 1; /* x */\n</script>"),
                         "<script>var a=1;</script>")


# ---------------------------------------------------------------------------
# Status e saúde honestos
# ---------------------------------------------------------------------------
Uso = namedtuple("Uso", "total used free")


class TesteStatusHonesto(BaseInterface):
    """Antecipação, relógio, coletor, disco, demonstração em UTC e sensor com defeito."""

    def _prognostico_falso(self, motivo: str = "SECA_CHEGANDO") -> types.ModuleType:
        """Módulo 'prognostico' falso com a análise pedida."""
        modulo = types.ModuleType("prognostico")
        modulo.MOTIVOS = ("SECA_CHEGANDO", "SECA_AGORA", "ENCHARCAMENTO", "CALOR_TARDE", "GEADA_NOITE", "FRIO", "NENHUM")  # type: ignore[attr-defined]
        chamadas: List[int] = []

        def obter_analise_antecipacao(id_leitura: int, caminho_banco: Path) -> Dict[str, Any]:
            """Análise fixa, como a frente antecipacao-ia devolveria."""
            chamadas.append(id_leitura)
            return {"probabilidade_risco_futuro": 0.81, "horas_ate_secar": 10.0, "temperatura_minima_prevista": 14.2,
                    "motivo_principal": motivo, "prazo_horas": 10.0, "horas_em_estresse": 0.0,
                    "status_antecipado": 1, "versao_prognostico": "teste-1"}

        modulo.obter_analise_antecipacao = obter_analise_antecipacao  # type: ignore[attr-defined]
        modulo.chamadas = chamadas  # type: ignore[attr-defined]
        sys.modules["prognostico"] = modulo
        return modulo

    def test_antecipacao_com_e_sem_modulo(self) -> None:
        """Com o módulo falso: motivo e prazo no status e rega no fim da tarde; sem ele: NENHUM."""
        modulo = self._prognostico_falso()
        id_leitura = self.inserir()
        dados = self.status()
        self.assertEqual(dados["motivo_principal"], "SECA_CHEGANDO")
        self.assertEqual(dados["prazo_horas"], 10.0)
        self.assertEqual(dados["horas_em_estresse"], 0.0)
        self.assertEqual(dados["antecipacao"]["versao_prognostico"], "teste-1")
        self.assertIn("REGAR_FIM_DA_TARDE", dados["acoes"])
        self.assertEqual(modulo.chamadas, [id_leitura])  # type: ignore[attr-defined]
        self.assertTrue(self.cliente.get("/api/saude").get_json()["componentes"]["prognostico"]["disponivel"])
        # Simulação e demonstração não consultam a antecipação.
        self.cliente.post("/api/demonstracao", json={"cenario": "CONDICAO_IDEAL"})
        self.assertEqual(self.status()["motivo_principal"], "NENHUM")
        self.assertEqual(len(modulo.chamadas), 1)  # type: ignore[attr-defined]
        self.cliente.post("/api/demonstracao/encerrar", json={})
        # Sem o módulo: comportamento de antes.
        sys.modules["prognostico"] = None  # type: ignore[assignment]
        dados = self.status()
        self.assertEqual(dados["motivo_principal"], "NENHUM")
        self.assertIsNone(dados["antecipacao"])
        self.assertIsNone(dados["prazo_horas"])
        self.assertEqual(dados["acoes"], ["NADA_A_FAZER"])
        self.assertFalse(self.cliente.get("/api/saude").get_json()["componentes"]["prognostico"]["disponivel"])

    def test_antecipacao_com_erro_nao_derruba_o_status(self) -> None:
        """Exceção ou motivo desconhecido do prognóstico viram NENHUM, sem erro 500."""
        modulo = self._prognostico_falso("COISA_ESTRANHA")
        self.inserir()
        self.assertEqual(self.status()["motivo_principal"], "NENHUM")
        modulo.obter_analise_antecipacao = mock.Mock(side_effect=RuntimeError("quebrou"))  # type: ignore[attr-defined]
        resposta = self.cliente.get("/api/status")
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.get_json()["motivo_principal"], "NENHUM")

    def test_relogio_no_futuro(self) -> None:
        """instante_utc 1 h no futuro: amarelo, leitura desatualizada e relógio inconsistente (status e saúde)."""
        _garantir_coluna(self.banco, "instante_utc")
        id_leitura = self.inserir(status=0)
        futuro = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
        _atualizar_leitura(self.banco, id_leitura, instante_utc=futuro)
        dados = self.status()
        self.assertEqual((dados["status_alerta"], dados["cor"]), (1, "amarelo"))
        self.assertTrue(dados["leitura_desatualizada"])
        self.assertTrue(dados["relogio_inconsistente"])
        self.assertEqual(dados["titulo"], "Relógio do aparelho errado")
        self.assertEqual(dados["acoes"], ["VERIFICAR_APARELHO"])
        saude = self.cliente.get("/api/saude").get_json()
        self.assertTrue(saude["componentes"]["relogio"]["inconsistente"])
        # instante_utc correto: volta ao normal, mesmo com o relógio local de data_hora.
        _atualizar_leitura(self.banco, id_leitura, instante_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        dados = self.status()
        self.assertFalse(dados["relogio_inconsistente"])
        self.assertEqual(dados["cor"], "verde")

    @unittest.skipUnless(_esquema_tem_instante_utc(),
                         "depende da coluna instante_utc e da ordenação por id da frente confiabilidade-campo")
    def test_salto_de_relogio_nao_prende_o_semaforo(self) -> None:
        """Vermelha gravada com data_hora +3 h e depois 30 verdes corretas: vale a última (verde)."""
        agora_utc = datetime.now(timezone.utc)
        vermelha = self.inserir(status=2, data_hora=datetime.now() + timedelta(hours=3))
        _atualizar_leitura(self.banco, vermelha, instante_utc=(agora_utc - timedelta(seconds=40)).isoformat(timespec="seconds"))
        ultima = vermelha
        for indice in range(30):
            ultima = self.inserir(status=0, segundos_atras=30 - indice)
            _atualizar_leitura(self.banco, ultima,
                               instante_utc=(agora_utc - timedelta(seconds=30 - indice)).isoformat(timespec="seconds"))
        dados = self.status()
        self.assertEqual(dados["id_leitura"], ultima)
        self.assertEqual(dados["cor"], "verde")

    def test_coletor_parado(self) -> None:
        """Batimento de 10 min atrás: saúde 503 (coletor.vivo False) e, com leitura velha, a mensagem do programa parado."""
        self.assertEqual(self.cliente.get("/api/saude").status_code, 200)  # sem batimento: carência
        self.inserir(segundos_atras=3 * 86400)
        definir_configuracao("coletor_batimento_em",
                             (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds"),
                             caminho_banco=self.banco)
        definir_configuracao("coletor_pid", 4321, caminho_banco=self.banco)
        resposta = self.cliente.get("/api/saude")
        self.assertEqual(resposta.status_code, 503)
        saude = resposta.get_json()
        self.assertFalse(saude["ok"])
        self.assertTrue(saude["banco"])
        self.assertFalse(saude["componentes"]["coletor"]["vivo"])
        self.assertEqual(saude["componentes"]["coletor"]["pid"], 4321)
        self.assertGreater(saude["componentes"]["coletor"]["idade_batimento_s"], 500)
        dados = self.status()
        self.assertFalse(dados["coletor_vivo"])
        self.assertIn("programa do computador parou", dados["mensagem_voz"])
        self.assertNotIn("cabo", dados["mensagem_voz"])
        self.assertEqual(dados["acoes"], ["VERIFICAR_APARELHO"])
        # Batimento recente: vivo e 200.
        definir_configuracao("coletor_batimento_em", datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             caminho_banco=self.banco)
        resposta = self.cliente.get("/api/saude")
        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.get_json()["componentes"]["coletor"]["vivo"])
        self.assertIn("cabo", self.status()["mensagem_voz"])

    def test_disco_quase_cheio(self) -> None:
        """50 MB livres (mínimo 200): 503 e componentes.disco.ok False; mínimo configurável."""
        with mock.patch.object(modulo_app.shutil, "disk_usage", return_value=Uso(10**10, 10**10, 50 * 1024 * 1024)):
            resposta = self.cliente.get("/api/saude")
            self.assertEqual(resposta.status_code, 503)
            disco = resposta.get_json()["componentes"]["disco"]
            self.assertEqual((disco["livre_mb"], disco["minimo_mb"], disco["ok"]), (50.0, 200.0, False))
            definir_configuracao("espaco_minimo_mb", 20.0, caminho_banco=self.banco)
            self.assertEqual(self.cliente.get("/api/saude").status_code, 200)

    def test_saude_por_componente(self) -> None:
        """Chaves de sempre no topo e todos os componentes, com padrões quando o coletor não escreveu nada."""
        definir_configuracao("serial_linhas_v2", 17, caminho_banco=self.banco)
        saude = self.cliente.get("/api/saude").get_json()
        for chave in ("ok", "banco", "modelo_carregado", "versao_modelo", "modo_operacao", "culturas_modelo"):
            self.assertIn(chave, saude)
        componentes = saude["componentes"]
        self.assertEqual(set(componentes), {"banco", "coletor", "disco", "relogio", "prognostico", "serial"})
        self.assertEqual(componentes["banco"], {"ok": True})
        self.assertIsNone(componentes["coletor"]["vivo"])
        self.assertEqual(componentes["serial"], {"linhas_v1": 0, "linhas_v2": 17, "checksum_invalido": 0, "descartadas": 0})
        self.assertEqual(componentes["relogio"], {"inconsistente": False, "ultimo_salto_em": None})

    def test_demonstracao_em_utc(self) -> None:
        """A expiração é gravada com '+00:00', conta o tempo certo e expira normalmente."""
        dados = self.cliente.post("/api/demonstracao", json={"cenario": "ENCHARCAMENTO"}).get_json()
        expira = str(obter_configuracao("demonstracao_expira_em", caminho_banco=self.banco))
        self.assertTrue(expira.endswith("+00:00"), expira)
        self.assertEqual(dados["demonstracao_expira_em"], expira)
        self.assertTrue(115 <= dados["segundos_restantes_demonstracao"] <= 120)
        definir_configuracao("demonstracao_expira_em",
                             (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(timespec="seconds"),
                             caminho_banco=self.banco)
        dados = self.status()
        self.assertEqual((dados["modo_operacao"], dados["cenario_demonstracao"]), ("SIMULACAO", ""))

    def test_sensor_com_defeito(self) -> None:
        """estado_sensor diferente de OK: 'Confira o aparelho' e só o pictograma do aparelho."""
        _garantir_coluna(self.banco, "estado_sensor")
        id_leitura = self.inserir(status=1, mensagem="Atenção. O sensor da terra parece com defeito. Confira o aparelho.")
        _atualizar_leitura(self.banco, id_leitura, estado_sensor="DEFEITO")
        dados = self.status()
        self.assertEqual(dados["estado_sensor"], "DEFEITO")
        self.assertEqual(dados["titulo"], "Confira o aparelho")
        self.assertEqual(dados["acoes"], ["VERIFICAR_APARELHO"])
        self.assertIn("sensor da terra", dados["mensagem_voz"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestePictogramaRegaCoerenteComVoz(unittest.TestCase):
    """O pictograma de rega da antecipação segue o momento que a voz realmente pediu."""

    def setUp(self) -> None:
        """Cria um banco temporário com os parâmetros do Café Arábica."""
        self.pasta = Path(tempfile.mkdtemp(prefix="agro_rega_"))
        self.banco = self.pasta / "agro.db"
        import database
        database.inicializar_banco(self.banco)
        self.parametros = database.buscar_parametros_cultura("cafe_arabica", caminho_banco=self.banco)

    def tearDown(self) -> None:
        """Remove o banco temporário."""
        shutil.rmtree(self.pasta, ignore_errors=True)

    def _acoes(self, mensagem: str) -> List[str]:
        """Ações para um Amarelo antecipado por seca chegando, com a mensagem dada."""
        return acoes_orientacao.derivar_acoes(1, 20.0, 62.0, self.parametros,
                                              motivo_antecipacao="SECA_CHEGANDO", mensagem_voz=mensagem)

    def test_voz_regue_agora_vira_pictograma_regar_agora(self) -> None:
        """Voz "Regue agora" → pictograma REGAR_AGORA, nunca "fim da tarde"."""
        acoes = self._acoes("Atenção com a plantação. A terra está secando. Regue agora, devagar.")
        self.assertIn("REGAR_AGORA", acoes)
        self.assertNotIn("REGAR_FIM_DA_TARDE", acoes)

    def test_voz_fim_da_tarde_mantem_pictograma(self) -> None:
        """Voz "fim da tarde" → pictograma REGAR_FIM_DA_TARDE."""
        acoes = self._acoes("Atenção. A terra está secando. Regue hoje no fim da tarde.")
        self.assertIn("REGAR_FIM_DA_TARDE", acoes)

    def test_sem_mensagem_comportamento_anterior(self) -> None:
        """Sem mensagem informada, o comportamento da v2 é preservado."""
        acoes = acoes_orientacao.derivar_acoes(1, 20.0, 62.0, self.parametros, motivo_antecipacao="SECA_CHEGANDO")
        self.assertIn("REGAR_FIM_DA_TARDE", acoes)
