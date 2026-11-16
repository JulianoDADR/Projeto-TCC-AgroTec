"""Testes da integração v3: costuras entre as três frentes (regra fisiológica, IA e interface).

Cada teste aqui cobre um defeito encontrado SÓ depois do merge (as frentes,
isoladas, passavam nos próprios testes):

* pictogramas e causa seguem a voz nas DUAS regras de temperatura — com a regra
  ``instantanea`` (banca/DEMONSTRACAO, DATASET ou o técnico de volta à v2) o
  ``motivo_regra`` também é gravado, e o mapa da regra fisiológica mostrava
  "nada a fazer" num Vermelho de calor cuja voz pede sombra e rega;
* na regra fisiológica, a noite fria com média de 24 h ideal é Verde ("Tudo
  bem"), mas a causa e as ações vinham da classe instantânea ("Frio", "cubra as
  mudas");
* ``app.reclassificar_medicao_de_campo`` (troca de cultura) reaproveitava a
  umidade JÁ calibrada como se viesse do firmware: a calibração da sonda era
  aplicada duas vezes;
* as chaves que a Frente 2 escreve (``prognostico_brier_local_28d``,
  ``prognostico_avaliado_em``, ``quantis_tmin_local``) passam a ser semeadas com
  o tipo certo;
* o instalador sem ``rsync`` copia a referência climática e os scripts.
"""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

RAIZ_PROJETO: Path = Path(__file__).resolve().parents[1]
PASTA_SRC: Path = RAIZ_PROJETO / "src"
if str(PASTA_SRC) not in sys.path:
    sys.path.insert(0, str(PASTA_SRC))

import app as modulo_app  # noqa: E402
import calibracao_sonda  # noqa: E402
from acoes_orientacao import (  # noqa: E402
    acoes_coerentes_com_mensagem,
    causa_coerente_com_mensagem,
    derivar_acoes,
    derivar_causa,
)
from coletor_serial import SaneadorTempoReal, processar_leitura  # noqa: E402
from database import (  # noqa: E402
    ParametrosCultura,
    buscar_parametros_cultura,
    buscar_ultima_leitura,
    definir_configuracoes,
    inicializar_banco,
    obter_configuracao_tipada,
    semear_configuracoes_v3,
)
from gerador_dataset import (  # noqa: E402
    calcular_features,
    gerar_mensagem_fisiologica,
    gerar_mensagem_orientacao,
)
from regras_agronomicas import (  # noqa: E402
    RESUMO_VAZIO,
    ConfiguracaoTermica,
    ResumoTermico,
    avaliar_status_campo,
)

# Cenários do resumo térmico: (mês, média de 24 h, máxima de ontem), cobrindo dia frio,
# florada com calor, calor seguido no verão, média ideal e dia quente.
CENARIOS_RESUMO: Tuple[Tuple[int, float, float], ...] = (
    (7, 14.0, 20.0), (10, 20.0, 35.0), (2, 27.0, 35.0), (7, 20.0, 22.0), (10, 30.0, 25.0),
)


def setUpModule() -> None:
    """Silencia os avisos esperados durante os testes."""
    logging.disable(logging.WARNING)


def tearDownModule() -> None:
    """Reativa os logs."""
    logging.disable(logging.NOTSET)


class BaseIntegracao(unittest.TestCase):
    """Banco temporário com as chaves da v3 e os parâmetros dos dois cafés."""

    def setUp(self) -> None:
        """Cria o banco temporário e semeia as chaves da v3."""
        self._pasta = tempfile.mkdtemp(prefix="agro_integ3_")
        self.banco: Path = Path(self._pasta) / "integ.db"
        inicializar_banco(self.banco)
        semear_configuracoes_v3(self.banco)
        self.culturas: List[ParametrosCultura] = [
            buscar_parametros_cultura(codigo, caminho_banco=self.banco) for codigo in ("cafe_arabica", "cafe_conilon")
        ]
        calibracao_sonda.limpar_cache()

    def tearDown(self) -> None:
        """Remove o banco temporário e o cache da calibração."""
        calibracao_sonda.limpar_cache()
        shutil.rmtree(self._pasta, ignore_errors=True)


class TesteFigurasSeguemAVoz(BaseIntegracao):
    """Ações e causa coerentes com a voz nas duas regras de temperatura (grade de leituras)."""

    def test_regra_instantanea_com_motivo_gravado_igual_a_v2(self) -> None:
        """Regra instantânea: com ``motivo_regra`` gravado as ações são EXATAMENTE as da v2 e a causa está na voz."""
        for parametros in self.culturas:
            for decimo in range(0, 90, 3):
                temperatura = decimo / 2.0
                for solo in range(20, 100, 6):
                    avaliacao = avaliar_status_campo(temperatura, solo, parametros, RESUMO_VAZIO,
                                                     ConfiguracaoTermica(), datetime(2026, 7, 1, 12))
                    deficit, desvio = calcular_features(temperatura, solo, parametros)
                    voz = gerar_mensagem_orientacao(avaliacao.status, deficit, desvio, parametros, temperatura, solo)
                    com_motivo = derivar_acoes(avaliacao.status, temperatura, solo, parametros, mensagem_voz=voz,
                                               motivo_regra=avaliacao.motivo)
                    self.assertEqual(com_motivo, derivar_acoes(avaliacao.status, temperatura, solo, parametros,
                                                               mensagem_voz=voz), (temperatura, solo, voz))
                    leitura = {"temperatura_ar": temperatura, "umidade_solo": solo, "motivo_regra": avaliacao.motivo}
                    causa = derivar_causa(avaliacao.status, leitura, parametros, mensagem_voz=voz)
                    self.assertTrue(causa_coerente_com_mensagem(causa, voz), (causa, voz))

    def test_vermelho_de_calor_da_banca_nao_mostra_nada_a_fazer(self) -> None:
        """35 °C na regra instantânea (cenário ONDA_DE_CALOR): Vermelho com sombra e rega, nunca "nada a fazer"."""
        arabica = self.culturas[0]
        avaliacao = avaliar_status_campo(35.0, 66.0, arabica, RESUMO_VAZIO, ConfiguracaoTermica(),
                                         datetime(2026, 1, 10, 14))
        deficit, desvio = calcular_features(35.0, 66.0, arabica)
        voz = gerar_mensagem_orientacao(avaliacao.status, deficit, desvio, arabica, 35.0, 66.0)
        acoes = derivar_acoes(avaliacao.status, 35.0, 66.0, arabica, mensagem_voz=voz, motivo_regra=avaliacao.motivo)
        self.assertEqual((avaliacao.status, avaliacao.motivo), (2, "CALOR_DIA"))
        self.assertNotIn("NADA_A_FAZER", acoes)
        self.assertIn("FAZER_SOMBRA", acoes)

    def test_regra_fisiologica_figuras_e_causa_na_voz(self) -> None:
        """Regra fisiológica (com e sem seca programada): toda ação e a causa aparecem na frase falada."""
        for parametros in self.culturas:
            for decimo in range(0, 90, 3):
                temperatura = decimo / 2.0
                for solo in range(20, 100, 6):
                    deficit, _ = calcular_features(temperatura, solo, parametros)
                    for mes, media, maxima_ontem in CENARIOS_RESUMO:
                        for seca in (False, True):
                            cfg = ConfiguracaoTermica(modo="fisiologica", seca_programada_ativa=seca)
                            avaliacao = avaliar_status_campo(temperatura, solo, parametros,
                                                             ResumoTermico(media, 24, None, maxima_ontem), cfg,
                                                             datetime(2026, mes, 15, 14))
                            voz = gerar_mensagem_fisiologica(avaliacao, deficit, parametros, temperatura, solo)
                            acoes = derivar_acoes(avaliacao.status, temperatura, solo, parametros, mensagem_voz=voz,
                                                  motivo_regra=avaliacao.motivo)
                            leitura = {"temperatura_ar": temperatura, "umidade_solo": solo,
                                       "motivo_regra": avaliacao.motivo}
                            causa = derivar_causa(avaliacao.status, leitura, parametros, mensagem_voz=voz)
                            contexto = (parametros.codigo, temperatura, solo, mes, avaliacao.motivo, acoes, voz)
                            self.assertTrue(acoes_coerentes_com_mensagem(acoes, voz), contexto)
                            self.assertTrue(causa_coerente_com_mensagem(causa, voz), (causa,) + contexto)
                            if avaliacao.status > 0:
                                self.assertTrue(acoes, contexto)
                            if avaliacao.status == 2:
                                self.assertNotEqual(acoes, ["NADA_A_FAZER"], contexto)

    def test_noite_fria_com_media_ideal_e_tudo_bem(self) -> None:
        """6 °C com média de 24 h de 19 °C: Verde, causa NENHUMA e "nada a fazer" (sem "Frio" nem "cubra")."""
        arabica = self.culturas[0]
        avaliacao = avaliar_status_campo(6.0, 70.0, arabica, ResumoTermico(19.0, 24, 21.0, 22.0),
                                         ConfiguracaoTermica(modo="fisiologica"), datetime(2026, 7, 15, 5))
        deficit, _ = calcular_features(6.0, 70.0, arabica)
        voz = gerar_mensagem_fisiologica(avaliacao, deficit, arabica, 6.0, 70.0)
        leitura = {"temperatura_ar": 6.0, "umidade_solo": 70.0, "motivo_regra": avaliacao.motivo}
        self.assertEqual((avaliacao.status, avaliacao.motivo), (0, "NENHUM"))
        self.assertEqual(derivar_causa(0, leitura, arabica), "NENHUMA")
        self.assertEqual(derivar_acoes(0, 6.0, 70.0, arabica, mensagem_voz=voz, motivo_regra=avaliacao.motivo),
                         ["NADA_A_FAZER"])

    def test_geada_com_terra_seca_mostra_a_rega_que_a_voz_pede(self) -> None:
        """Geada (3 °C) com terra muito seca: a figura da rega acompanha a voz, depois de cobrir e chegar terra.

        Ajustado na v4 (frente B3, "uma ordem por ação"): até a v3 a voz dizia
        "Regue agora, de dia" ao lado de "Está gelando agora" e a figura da rega
        vinha primeiro. Agora a proteção vem primeiro e a rega é "Regue de dia,
        quando o sol sair." (reaproveita o "de dia" da v2, sem recomendação nova),
        então a figura é a da rega da manhã, na terceira posição, como a voz diz.
        Status, motivo e limiares não mudam.
        """
        arabica = self.culturas[0]
        avaliacao = avaliar_status_campo(3.0, 30.0, arabica, ResumoTermico(12.0, 24, 18.0, 20.0),
                                         ConfiguracaoTermica(modo="fisiologica"), datetime(2026, 7, 15, 5))
        deficit, _ = calcular_features(3.0, 30.0, arabica)
        voz = gerar_mensagem_fisiologica(avaliacao, deficit, arabica, 3.0, 30.0)
        acoes = derivar_acoes(avaliacao.status, 3.0, 30.0, arabica, mensagem_voz=voz, motivo_regra=avaliacao.motivo)
        self.assertEqual(avaliacao.motivo, "GEADA_AGORA")
        self.assertEqual(acoes, ["COBRIR_MUDAS", "CHEGAR_TERRA", "REGAR_MANHA"])
        self.assertNotIn("Regue agora", voz)
        self.assertIn("Regue de dia, quando o sol sair.", voz)
        self.assertTrue(acoes_coerentes_com_mensagem(acoes, voz))


class TesteTrocaDeCulturaComCalibracao(BaseIntegracao):
    """A troca de cultura reaproveita a medição de campo sem calibrar duas vezes."""

    def test_reclassificacao_usa_o_valor_do_firmware(self) -> None:
        """Firmware 64 % com aferição para 70 %: a leitura reclassificada para o Conilon grava 70 % (não 76 %)."""
        arabica = self.culturas[0]
        saneador = SaneadorTempoReal(arabica)
        for _ in range(8):
            processar_leitura(20.0, 70.0, 64.0, "CAMPO_USB", "cafe_arabica", saneador, None, caminho_banco=self.banco)
        calibracao_sonda.aferir_um_ponto(self.banco, 70.0)
        calibracao_sonda.limpar_cache()
        for _ in range(3):
            processar_leitura(20.0, 70.0, 64.0, "CAMPO_USB", "cafe_arabica", saneador, None, caminho_banco=self.banco)
        ultima = buscar_ultima_leitura(caminho_banco=self.banco)
        assert ultima is not None
        self.assertEqual((ultima["umidade_solo"], ultima["umidade_solo_firmware"]), (70.0, 64.0))
        resultado = modulo_app.reclassificar_medicao_de_campo("cafe_conilon", self.banco)
        self.assertIsNotNone(resultado)
        reclassificada = buscar_ultima_leitura(caminho_banco=self.banco)
        assert reclassificada is not None
        self.assertEqual(reclassificada["codigo_cultura"], "cafe_conilon")
        self.assertEqual((reclassificada["umidade_solo"], reclassificada["umidade_solo_firmware"]), (70.0, 64.0))


class TesteChavesEInstalador(BaseIntegracao):
    """Chaves semeadas para a Frente 2 e cópia do instalador sem rsync."""

    def test_chaves_da_frente_2_semeadas_com_tipo(self) -> None:
        """As chaves escritas pelo portão local existem desde o início e guardam o tipo certo."""
        self.assertIsNone(obter_configuracao_tipada("prognostico_brier_local_28d", None, caminho_banco=self.banco))
        definir_configuracoes({"prognostico_brier_local_28d": 0.1234, "prognostico_avaliado_em": "2026-11-15T00:00:00",
                               "quantis_tmin_local": '{"seca": 1.5, "aguas": 1.2}'}, self.banco)
        self.assertEqual(obter_configuracao_tipada("prognostico_brier_local_28d", None, caminho_banco=self.banco),
                         0.1234)
        self.assertEqual(obter_configuracao_tipada("quantis_tmin_local", "", caminho_banco=self.banco),
                         '{"seca": 1.5, "aguas": 1.2}')

    def test_instalador_sem_rsync_copia_referencia_e_scripts(self) -> None:
        """O laço de cópia sem rsync inclui ``referencia_climatica`` e ``scripts`` (seção 7 e complemento)."""
        instalador = (RAIZ_PROJETO / "deploy" / "instalar.sh").read_text(encoding="utf-8")
        laco = next(linha for linha in instalador.splitlines() if linha.strip().startswith("for item in"))
        for item in ("referencia_climatica", "scripts", "src", "requirements.txt"):
            self.assertIn(item, laco.split())


if __name__ == "__main__":
    unittest.main()
