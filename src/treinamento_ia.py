"""Módulo 4 — Treinamento e Validação da IA do AgroAnálise.

Pipeline:
    1. ``carregar_dataset``: lê ``data/dataset_limpo.csv`` (gera o dataset se
       ele ainda não existir).
    2. ``separar_treino_teste``: divisão 80/20 ESTRATIFICADA pelo rótulo
       ``status_alerta`` (as três cores mantêm a mesma proporção nos dois lados).
    3. Treina e compara dois modelos supervisionados:
         * ``RandomForestClassifier`` (comitê de árvores de decisão);
         * ``LogisticRegression`` dentro de um ``Pipeline`` com
           ``StandardScaler`` (modelo linear de referência).
    4. ``validacao_cruzada``: 5 dobras estratificadas no conjunto de treino
       (estabilidade das métricas + erros operacionais fora da dobra).
    5. ``avaliar_modelo``: Accuracy, Precision, Recall e F1 (macro, ponderado
       e por classe), matriz de confusão e a PROVA OPERACIONAL:
         * Falso Positivo  = real Verde e predito Amarelo/Vermelho
           (alarme falso → desperdício de água e insumo);
         * Falso Negativo  = real Amarelo/Vermelho e predito Verde
           (risco não detectado → perda de safra);
         * Subestimação crítica = real Vermelho e predito abaixo de Vermelho.
    6. ``selecionar_campeao``: menor (FP + FN) no teste; desempate pelo maior
       F1 macro.
    7. Salva ``reports/matriz_confusao.png``, ``reports/relatorio_treinamento.txt``
       e o pacote ``models/modelo_agroanalise.pkl``; atualiza
       ``configuracao_sistema.versao_modelo``.
    8. IA de PROGNÓSTICO de 12 h (antecipação de estresse, ver ``prognostico.py``):
       ``preparar_dataset_prognostico`` → ``dividir_temporal`` (últimos 30 % de
       cada cultura como teste, intervalo de 12 h) → ``treinar_modelo_prognostico``
       (Random Forest calibrado, limiar por orçamento de alarmes, coeficientes
       da mínima noturna) → avaliação honesta → ``models/modelo_prognostico.pkl``
       e ``reports/curva_confiabilidade.png``.

    9. DADO REAL (``avaliar_em_referencia`` e ``avaliar_geada_referencia``): o
       pacote treinado no simulador é aplicado à reanálise ERA5-Land guardada em
       ``referencia_climatica/`` (Varginha/MG → Arábica; São Gabriel da Palha/ES
       → Conilon) e comparado, fora da amostra (ano de teste 2021), com a
       persistência e com a climatologia mês × hora; o aviso de geada ganha
       coeficientes por região e margem conformal por estação. Tudo vai para
       ``models/complemento_prognostico.json`` (fora do pacote de 9 chaves).

O rótulo do prognóstico segue a regra de temperatura escolhida
(``--regra-temperatura``): ``instantanea`` (v2: cada leitura contra as faixas)
ou ``fisiologica`` (``regras_agronomicas.rotular_status_horario``: conforto
pela média de 24 h limitado a Amarelo; Vermelho só por geada no abrigo ou
calor de dano). Se o módulo da regra fisiológica faltar, o treino avisa no log
e usa o rótulo instantâneo.

O relatório tem seções com rótulo de escopo: (1) fidelidade à regra — não é
desempenho de campo; (2) generalização temporal; (3) robustez a ruído de
sensor; (4) teste de antecipação; (5) calibração; (6) aviso de geada;
(7) dado real (reanálise ERA5-Land).

Execução direta: ``python src/treinamento_ia.py [--regra-temperatura {instantanea,fisiologica}]``
(a partir da raiz do projeto).
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import logging
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import joblib
import matplotlib

matplotlib.use("Agg")  # backend sem janela: funciona em servidor e em terminal sem display

import matplotlib.pyplot as plt  # noqa: E402  (precisa vir depois de matplotlib.use)
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.patches import Patch, Rectangle  # noqa: E402
from sklearn.base import clone  # noqa: E402
from sklearn.calibration import CalibratedClassifierCV  # noqa: E402
from sklearn.ensemble import RandomForestClassifier  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import (  # noqa: E402
    accuracy_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    precision_recall_fscore_support,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import StratifiedKFold, TimeSeriesSplit, train_test_split  # noqa: E402
from sklearn.pipeline import Pipeline  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

from database import (  # noqa: E402
    CAMINHO_BANCO,
    CAMINHO_DATASET_LIMPO,
    CAMINHO_MATRIZ_CONFUSAO,
    CAMINHO_MODELO,
    CAMINHO_RELATORIO_TREINO,
    NOMES_STATUS,
    REPORTS_DIR,
    STATUS_AMARELO,
    STATUS_VERDE,
    STATUS_VERMELHO,
    VARIAVEL_TEMPERATURA_AR,
    VARIAVEL_UMIDADE_SOLO,
    ParametrosCultura,
    buscar_parametros_cultura,
    definir_configuracao,
    obter_conexao,
)
from gerador_dataset import (  # noqa: E402
    COLUNA_ALVO,
    COLUNAS_FEATURES_MODELO,
    DATA_INICIAL_DATASET,
    SEMENTE_ALEATORIA,
    gerar_dataset_completo,
    ponto_orvalho_magnus,
)
from prognostico import (  # noqa: E402
    CAMINHO_COMPLEMENTO_PROGNOSTICO,
    CAMINHO_MODELO_PROGNOSTICO,
    CHAVES_PACOTE_PROGNOSTICO,
    COEF_TMIN_PADRAO,
    COLUNAS_FEATURES_PROGNOSTICO,
    HORIZONTE_PROGNOSTICO_HORAS,
    INCERTEZA_SENSOR_AR_PADRAO_C,
    LIMIAR_GEADA_ABRIGO_PADRAO_C,
    MARGEM_GEADA_C,
    REGRA_TEMPERATURA_FISIOLOGICA,
    REGRA_TEMPERATURA_INSTANTANEA,
    REGRAS_TEMPERATURA,
    status_vetorizado,
    calcular_alvo_risco_futuro,
    calcular_features_temporais,
    prever_temperatura_minima_noite,
)
from referencia_climatica import (  # noqa: E402
    DIRETORIO_REFERENCIA,
    ESTACOES,
    REGIOES_REFERENCIA,
    aplicar_climatologia,
    carregar_referencia,
    climatologia_mes_hora,
    estacao_do_mes,
    minimos_quadrados_tmin,
    referencia_disponivel,
    prever_tmin,
    quantil_conformal,
    residuos_deixa_um_fora,
    resolver_regiao,
    tabela_noites,
)

# ---------------------------------------------------------------------------
# Constantes do módulo
# ---------------------------------------------------------------------------
CLASSES_STATUS: List[int] = [STATUS_VERDE, STATUS_AMARELO, STATUS_VERMELHO]
ROTULOS_CURTOS: List[str] = ["Verde", "Amarelo", "Vermelho"]
NOME_RANDOM_FOREST: str = "RandomForestClassifier"
NOME_REGRESSAO_LOGISTICA: str = "LogisticRegression"
DOBRAS_VALIDACAO: int = 5
LARGURA_RELATORIO: int = 86
ORCAMENTO_AMARELO_ANTECIPADO_HORAS_SEMANA: float = 14.0  # h/semana de Amarelo antecipado falso por talhão
DOBRAS_CALIBRACAO: int = 4
FRACAO_DOBRA_CALIBRACAO: float = 0.05  # cada dobra de calibração = 5 % das horas, no fim do período
CAMINHO_CURVA_CONFIABILIDADE: Path = REPORTS_DIR / "curva_confiabilidade.png"
TITULOS_SECOES_RELATORIO: Tuple[str, ...] = (
    "SEÇÃO 1 — Fidelidade à regra (não é desempenho de campo)",
    "SEÇÃO 2 — Generalização temporal",
    "SEÇÃO 3 — Robustez a ruído de sensor",
    "SEÇÃO 4 — Teste de antecipação",
    "SEÇÃO 5 — Calibração",
)

TITULO_SECAO_REFERENCIA: str = "SEÇÃO 7 — Dado real (reanálise ERA5-Land)"
ANO_TESTE_REFERENCIA: int = 2021  # reanálise 2018–2021: ajusta em 2018–2020, testa em 2021 (fora da amostra)
QUANTIL_CONFORMAL_TMIN: float = 0.9
NOME_MODULO_REGRAS: str = "regras_agronomicas"
NOME_FUNCAO_ROTULO_FISIOLOGICO: str = "rotular_status_horario"
VERSAO_COMPLEMENTO: int = 1

Rotulador = Callable[[pd.DataFrame, ParametrosCultura], pd.Series]

registrador: logging.Logger = logging.getLogger("agroanalise.treinamento_ia")


# ---------------------------------------------------------------------------
# Dados
# ---------------------------------------------------------------------------
def carregar_dataset(caminho: Path = CAMINHO_DATASET_LIMPO) -> pd.DataFrame:
    """Carrega o dataset limpo e rotulado usado no treinamento.

    Se o arquivo não existir, gera o dataset completo com
    ``gerar_dataset_completo()`` (simulação + ruído + saneamento + rótulos).
    Valida as colunas obrigatórias, descarta linhas sem features ou sem rótulo
    (registrando aviso) e converte o alvo para inteiro.

    Raises:
        ValueError: se faltarem colunas obrigatórias, se o dataset ficar vazio
            ou se aparecer um rótulo fora de 0/1/2.
    """
    caminho_arquivo = Path(caminho)
    if not caminho_arquivo.exists():
        registrador.warning("Dataset limpo não encontrado em %s; gerando um novo dataset agora.", caminho_arquivo)
        if caminho_arquivo == Path(CAMINHO_DATASET_LIMPO):
            gerar_dataset_completo(data_inicial=DATA_INICIAL_DATASET)
        else:
            gerar_dataset_completo(caminho_limpo=caminho_arquivo,
                                   caminho_bruto=caminho_arquivo.with_name("dataset_bruto.csv"),
                                   data_inicial=DATA_INICIAL_DATASET)
    df = pd.read_csv(caminho_arquivo)
    obrigatorias = COLUNAS_FEATURES_MODELO + [COLUNA_ALVO]
    faltando = [coluna for coluna in obrigatorias if coluna not in df.columns]
    if faltando:
        raise ValueError(f"O dataset {caminho_arquivo} não tem as colunas obrigatórias: {', '.join(faltando)}.")

    linhas_antes = len(df)
    df = df.dropna(subset=obrigatorias).reset_index(drop=True)
    descartadas = linhas_antes - len(df)
    if descartadas:
        registrador.warning("%d linhas com valores ausentes nas features/rótulo foram descartadas.", descartadas)
    if df.empty:
        raise ValueError(f"O dataset {caminho_arquivo} não tem nenhuma linha utilizável para treinamento.")

    df[COLUNA_ALVO] = df[COLUNA_ALVO].astype(np.int64)
    rotulos_invalidos = sorted(set(df[COLUNA_ALVO].unique()) - set(CLASSES_STATUS))
    if rotulos_invalidos:
        raise ValueError(f"Rótulos inválidos em '{COLUNA_ALVO}': {rotulos_invalidos} (esperado 0, 1 ou 2).")
    for coluna in COLUNAS_FEATURES_MODELO:
        df[coluna] = df[coluna].astype(float)
    registrador.info("Dataset carregado: %s (%d linhas).", caminho_arquivo, len(df))
    return df


def separar_treino_teste(
    df: pd.DataFrame, proporcao_teste: float = 0.2, semente: int = SEMENTE_ALEATORIA
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.Series, pd.Series]:
    """Divide o dataset em treino e teste, estratificando pelo rótulo.

    Usa somente as colunas ``COLUNAS_FEATURES_MODELO`` como X e
    ``COLUNA_ALVO`` como y. Os índices originais do ``df`` são preservados
    (permitem cruzar o teste com a coluna ``cultura``).

    Returns:
        ``(X_treino, X_teste, y_treino, y_teste)``.

    Raises:
        ValueError: se a proporção de teste não estiver entre 0 e 1.
    """
    if not 0.0 < float(proporcao_teste) < 1.0:
        raise ValueError(f"proporcao_teste deve estar entre 0 e 1 (recebido {proporcao_teste}).")
    X = df[COLUNAS_FEATURES_MODELO].astype(float)
    y = df[COLUNA_ALVO].astype(np.int64)
    X_treino, X_teste, y_treino, y_teste = train_test_split(
        X, y, test_size=float(proporcao_teste), random_state=int(semente), stratify=y
    )
    return X_treino, X_teste, y_treino, y_teste


# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------
def _novo_random_forest(semente: int) -> RandomForestClassifier:
    """Instancia (sem treinar) o Random Forest com os hiperparâmetros do projeto."""
    return RandomForestClassifier(
        n_estimators=300,
        max_depth=None,
        min_samples_leaf=1,
        class_weight="balanced",
        n_jobs=-1,
        random_state=int(semente),
    )


def _nova_regressao_logistica(semente: int) -> Pipeline:
    """Instancia (sem treinar) o Pipeline StandardScaler + LogisticRegression."""
    return Pipeline(
        steps=[
            ("padronizacao", StandardScaler()),
            ("regressao_logistica", LogisticRegression(
                C=1.0, class_weight="balanced", max_iter=5000, random_state=int(semente)
            )),
        ]
    )


def treinar_random_forest(
    X_treino: pd.DataFrame, y_treino: pd.Series, semente: int = SEMENTE_ALEATORIA
) -> RandomForestClassifier:
    """Treina um ``RandomForestClassifier`` (300 árvores, classes balanceadas).

    O modelo é ajustado com o DataFrame (guarda os nomes das features), o que
    exige que a predição em tempo real use as mesmas colunas, na mesma ordem.
    """
    modelo = _novo_random_forest(semente)
    modelo.fit(X_treino, y_treino)
    registrador.info("RandomForestClassifier treinado com %d amostras.", len(X_treino))
    return modelo


def treinar_regressao_logistica(
    X_treino: pd.DataFrame, y_treino: pd.Series, semente: int = SEMENTE_ALEATORIA
) -> Pipeline:
    """Treina o ``Pipeline`` StandardScaler + LogisticRegression (multinomial)."""
    modelo = _nova_regressao_logistica(semente)
    modelo.fit(X_treino, y_treino)
    registrador.info("LogisticRegression (Pipeline com StandardScaler) treinada com %d amostras.", len(X_treino))
    return modelo


# ---------------------------------------------------------------------------
# Avaliação
# ---------------------------------------------------------------------------
def calcular_falsos_operacionais(y_real: pd.Series, y_pred: np.ndarray) -> Dict[str, int]:
    """Conta os erros que importam no campo.

    * ``falsos_positivos``: real Verde (0) e predito > 0 — alarme falso,
      desperdício de água/insumo.
    * ``falsos_negativos``: real > 0 e predito Verde (0) — risco não
      detectado, perda de safra.
    * ``subestimacoes_criticas``: real Vermelho (2) e predito < 2.

    Também devolve ``superestimacoes`` (predito acima do real), ``erros_totais``
    e ``total_amostras`` para dar contexto.

    Raises:
        ValueError: se os vetores tiverem tamanhos diferentes.
    """
    real = np.asarray(y_real, dtype=np.int64)
    predito = np.asarray(y_pred, dtype=np.int64)
    if real.shape != predito.shape:
        raise ValueError(f"Tamanhos diferentes: y_real={real.shape}, y_pred={predito.shape}.")
    return {
        "falsos_positivos": int(np.sum((real == STATUS_VERDE) & (predito > STATUS_VERDE))),
        "falsos_negativos": int(np.sum((real > STATUS_VERDE) & (predito == STATUS_VERDE))),
        "subestimacoes_criticas": int(np.sum((real == STATUS_VERMELHO) & (predito < STATUS_VERMELHO))),
        "superestimacoes": int(np.sum(predito > real)),
        "erros_totais": int(np.sum(predito != real)),
        "total_amostras": int(real.size),
    }


def avaliar_modelo(nome: str, modelo: Any, X_teste: pd.DataFrame, y_teste: pd.Series) -> Dict[str, Any]:
    """Avalia um modelo treinado no conjunto de teste.

    Returns:
        Dicionário com ``nome``, ``modelo``, ``accuracy``, ``precision_macro``,
        ``recall_macro``, ``f1_macro``, ``precision_ponderada``,
        ``recall_ponderado``, ``f1_ponderado``, ``por_classe`` (precision,
        recall, f1 e suporte de cada classe), ``matriz_confusao`` (lista de
        listas, linhas = real, colunas = predito, ordem 0/1/2), ``falsos``
        (``calcular_falsos_operacionais``), ``n_teste``, ``distribuicao_teste``,
        ``y_pred`` (lista) e, quando o estimador oferece,
        ``importancia_features``.
    """
    y_pred = np.asarray(modelo.predict(X_teste[COLUNAS_FEATURES_MODELO]), dtype=np.int64)
    y_real = np.asarray(y_teste, dtype=np.int64)
    precisoes, recalls, f1s, suportes = precision_recall_fscore_support(
        y_real, y_pred, labels=CLASSES_STATUS, zero_division=0
    )
    por_classe: Dict[int, Dict[str, float]] = {
        classe: {
            "precision": float(precisoes[posicao]),
            "recall": float(recalls[posicao]),
            "f1": float(f1s[posicao]),
            "suporte": float(suportes[posicao]),
        }
        for posicao, classe in enumerate(CLASSES_STATUS)
    }
    resultado: Dict[str, Any] = {
        "nome": nome,
        "modelo": modelo,
        "accuracy": float(accuracy_score(y_real, y_pred)),
        "precision_macro": float(precision_score(y_real, y_pred, average="macro", labels=CLASSES_STATUS, zero_division=0)),
        "recall_macro": float(recall_score(y_real, y_pred, average="macro", labels=CLASSES_STATUS, zero_division=0)),
        "f1_macro": float(f1_score(y_real, y_pred, average="macro", labels=CLASSES_STATUS, zero_division=0)),
        "precision_ponderada": float(precision_score(y_real, y_pred, average="weighted", labels=CLASSES_STATUS, zero_division=0)),
        "recall_ponderado": float(recall_score(y_real, y_pred, average="weighted", labels=CLASSES_STATUS, zero_division=0)),
        "f1_ponderado": float(f1_score(y_real, y_pred, average="weighted", labels=CLASSES_STATUS, zero_division=0)),
        "por_classe": por_classe,
        "matriz_confusao": confusion_matrix(y_real, y_pred, labels=CLASSES_STATUS).astype(int).tolist(),
        "falsos": calcular_falsos_operacionais(pd.Series(y_real), y_pred),
        "n_teste": int(y_real.size),
        "distribuicao_teste": {classe: int(np.sum(y_real == classe)) for classe in CLASSES_STATUS},
        "y_pred": y_pred.tolist(),
    }
    estimador_final = modelo.steps[-1][1] if isinstance(modelo, Pipeline) else modelo
    if hasattr(estimador_final, "feature_importances_"):
        resultado["importancia_features"] = {
            coluna: float(valor)
            for coluna, valor in zip(COLUNAS_FEATURES_MODELO, estimador_final.feature_importances_)
        }
    registrador.info("%s avaliado: accuracy=%.4f, f1_macro=%.4f, FP=%d, FN=%d.", nome, resultado["accuracy"],
                     resultado["f1_macro"], resultado["falsos"]["falsos_positivos"],
                     resultado["falsos"]["falsos_negativos"])
    return resultado


def validacao_cruzada(modelo: Any, X: pd.DataFrame, y: pd.Series, dobras: int = DOBRAS_VALIDACAO) -> Dict[str, float]:
    """Validação cruzada estratificada (``StratifiedKFold``, embaralhada, semente fixa).

    A cada dobra um CLONE não treinado do modelo é ajustado — o modelo
    recebido não é alterado. Além da média e do desvio-padrão de accuracy,
    precision/recall/F1 macro e F1 ponderado, soma os erros operacionais das
    predições fora da dobra (cada amostra é prevista exatamente uma vez por um
    modelo que não a viu).

    Returns:
        ``{"dobras", "accuracy_media", "accuracy_desvio", "precision_macro_media",
        ..., "falsos_positivos_fora_dobra", "falsos_negativos_fora_dobra",
        "subestimacoes_criticas_fora_dobra", "amostras"}`` (todos ``float``).

    Raises:
        ValueError: se ``dobras`` < 2.
    """
    if int(dobras) < 2:
        raise ValueError(f"A validação cruzada exige pelo menos 2 dobras (recebido {dobras}).")
    X_valores = X[COLUNAS_FEATURES_MODELO].astype(float).reset_index(drop=True)
    y_valores = pd.Series(np.asarray(y, dtype=np.int64))
    divisor = StratifiedKFold(n_splits=int(dobras), shuffle=True, random_state=SEMENTE_ALEATORIA)
    metricas_por_dobra: Dict[str, List[float]] = {
        "accuracy": [], "precision_macro": [], "recall_macro": [], "f1_macro": [], "f1_ponderado": [],
    }
    predicoes_fora_dobra = np.full(len(y_valores), -1, dtype=np.int64)
    for numero, (indices_treino, indices_validacao) in enumerate(divisor.split(X_valores, y_valores), start=1):
        estimador = clone(modelo)
        estimador.fit(X_valores.iloc[indices_treino], y_valores.iloc[indices_treino])
        predito = np.asarray(estimador.predict(X_valores.iloc[indices_validacao]), dtype=np.int64)
        real = y_valores.iloc[indices_validacao].to_numpy()
        predicoes_fora_dobra[indices_validacao] = predito
        metricas_por_dobra["accuracy"].append(float(accuracy_score(real, predito)))
        metricas_por_dobra["precision_macro"].append(
            float(precision_score(real, predito, average="macro", labels=CLASSES_STATUS, zero_division=0)))
        metricas_por_dobra["recall_macro"].append(
            float(recall_score(real, predito, average="macro", labels=CLASSES_STATUS, zero_division=0)))
        metricas_por_dobra["f1_macro"].append(
            float(f1_score(real, predito, average="macro", labels=CLASSES_STATUS, zero_division=0)))
        metricas_por_dobra["f1_ponderado"].append(
            float(f1_score(real, predito, average="weighted", labels=CLASSES_STATUS, zero_division=0)))
        registrador.debug("Dobra %d/%d: accuracy=%.4f", numero, dobras, metricas_por_dobra["accuracy"][-1])

    resultado: Dict[str, float] = {"dobras": float(dobras), "amostras": float(len(y_valores))}
    for chave, valores in metricas_por_dobra.items():
        resultado[f"{chave}_media"] = float(np.mean(valores))
        resultado[f"{chave}_desvio"] = float(np.std(valores))
    falsos = calcular_falsos_operacionais(y_valores, predicoes_fora_dobra)
    resultado["falsos_positivos_fora_dobra"] = float(falsos["falsos_positivos"])
    resultado["falsos_negativos_fora_dobra"] = float(falsos["falsos_negativos"])
    resultado["subestimacoes_criticas_fora_dobra"] = float(falsos["subestimacoes_criticas"])
    return resultado


def selecionar_campeao(resultados: Dict[str, Dict[str, Any]]) -> str:
    """Escolhe o modelo campeão.

    Critério: menor soma de erros operacionais no teste
    (Falsos Positivos + Falsos Negativos); desempate pelo maior F1 macro;
    persistindo o empate, ordem alfabética do nome (determinístico).

    Raises:
        ValueError: se ``resultados`` estiver vazio.
    """
    if not resultados:
        raise ValueError("Nenhum resultado de modelo para selecionar o campeão.")

    def chave_ordenacao(nome: str) -> Tuple[int, float, str]:
        """(FP+FN, -F1 macro, nome): menor é melhor."""
        falsos = resultados[nome]["falsos"]
        return (int(falsos["falsos_positivos"]) + int(falsos["falsos_negativos"]),
                -float(resultados[nome]["f1_macro"]), nome)

    return min(resultados, key=chave_ordenacao)


# ---------------------------------------------------------------------------
# Gráfico
# ---------------------------------------------------------------------------
def gerar_matriz_confusao(
    resultados: Dict[str, Dict[str, Any]], campeao: str, caminho: Path = CAMINHO_MATRIZ_CONFUSAO
) -> Path:
    """Desenha as matrizes de confusão dos modelos lado a lado e salva em PNG.

    Cada célula mostra a contagem e o percentual da linha (classe real). As
    células de Falso Positivo (real Verde, predito Amarelo/Vermelho) ganham
    contorno laranja tracejado e as de Falso Negativo (real Amarelo/Vermelho,
    predito Verde) contorno vermelho. O campeão recebe "CAMPEÃO" no título e
    moldura verde.

    Returns:
        Caminho do arquivo salvo.
    """
    destino = Path(caminho)
    destino.parent.mkdir(parents=True, exist_ok=True)
    nomes = list(resultados)
    figura, eixos = plt.subplots(1, len(nomes), figsize=(7.2 * len(nomes), 6.8), squeeze=False)
    for eixo, nome in zip(eixos[0], nomes):
        matriz = np.asarray(resultados[nome]["matriz_confusao"], dtype=float)
        totais_linha = matriz.sum(axis=1, keepdims=True)
        percentuais = np.divide(matriz, totais_linha, out=np.zeros_like(matriz), where=totais_linha > 0)
        eixo.imshow(percentuais, cmap="Blues", vmin=0.0, vmax=1.0)
        for linha in range(len(CLASSES_STATUS)):
            for coluna in range(len(CLASSES_STATUS)):
                cor_texto = "white" if percentuais[linha, coluna] > 0.55 else "#1a1a1a"
                eixo.text(coluna, linha, f"{int(matriz[linha, coluna])}\n({_decimal(100.0 * percentuais[linha, coluna], 1)}%)",
                          ha="center", va="center", fontsize=13, fontweight="bold", color=cor_texto)
                falso_positivo = linha == STATUS_VERDE and coluna > STATUS_VERDE
                falso_negativo = linha > STATUS_VERDE and coluna == STATUS_VERDE
                if falso_positivo or falso_negativo:
                    eixo.add_patch(Rectangle(
                        (coluna - 0.47, linha - 0.47), 0.94, 0.94, fill=False, linewidth=2.2,
                        linestyle="--" if falso_positivo else "-",
                        edgecolor="#e67e00" if falso_positivo else "#c0392b",
                    ))
        eixo.set_xticks(range(len(CLASSES_STATUS)), labels=ROTULOS_CURTOS, fontsize=11)
        eixo.set_yticks(range(len(CLASSES_STATUS)), labels=ROTULOS_CURTOS, fontsize=11)
        eixo.set_xlabel("Status predito pelo modelo", fontsize=11)
        eixo.set_ylabel("Status real (motor de regras)", fontsize=11)
        falsos = resultados[nome]["falsos"]
        eh_campeao = nome == campeao
        titulo = f"{'CAMPEÃO — ' if eh_campeao else ''}{nome}"
        eixo.set_title(
            f"{titulo}\nAccuracy {_decimal(100.0 * resultados[nome]['accuracy'], 2)}% | F1 macro {_decimal(resultados[nome]['f1_macro'])}\n"
            f"FP {falsos['falsos_positivos']} | FN {falsos['falsos_negativos']} | "
            f"Subestimações críticas {falsos['subestimacoes_criticas']}",
            fontsize=12, fontweight="bold" if eh_campeao else "normal",
            color="#1e7d32" if eh_campeao else "#333333",
        )
        for borda in eixo.spines.values():
            borda.set_edgecolor("#1e7d32" if eh_campeao else "#999999")
            borda.set_linewidth(4.0 if eh_campeao else 1.0)
    legenda = [
        Patch(facecolor="none", edgecolor="#e67e00", linestyle="--", linewidth=2.2,
              label="Falso Positivo: alarme falso (desperdício de água)"),
        Patch(facecolor="none", edgecolor="#c0392b", linewidth=2.2,
              label="Falso Negativo: risco não detectado (perda de safra)"),
    ]
    figura.legend(handles=legenda, loc="lower center", ncol=2, fontsize=11, frameon=False)
    figura.suptitle("AgroAnálise — Matriz de Confusão no teste aleatório (20%) — fidelidade à regra, não é desempenho de campo",
                    fontsize=13, fontweight="bold")
    figura.tight_layout(rect=(0.0, 0.07, 1.0, 0.95))
    figura.savefig(destino, dpi=130)
    plt.close(figura)
    registrador.info("Matriz de confusão salva em %s.", destino)
    return destino


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------
def _decimal(valor: float, casas: int = 4) -> str:
    """Formata número com vírgula decimal (padrão brasileiro)."""
    return f"{float(valor):.{casas}f}".replace(".", ",")


def _percentual(parte: float, total: float) -> str:
    """Percentual com vírgula decimal; '0,00%' quando o total é zero."""
    return f"{_decimal(100.0 * parte / total if total else 0.0, 2)}%"


def _matriz_textual(matriz: List[List[int]]) -> List[str]:
    """Matriz de confusão em texto alinhado (linhas = real, colunas = predito)."""
    largura = 12
    linhas = [" " * 18 + "Predito →",
              f"{'Real ↓':<16}" + "".join(f"{rotulo:>{largura}}" for rotulo in ROTULOS_CURTOS) + f"{'Total':>{largura}}"]
    for posicao, rotulo in enumerate(ROTULOS_CURTOS):
        valores = matriz[posicao]
        linhas.append(f"{rotulo:<16}" + "".join(f"{int(valor):>{largura}}" for valor in valores)
                      + f"{int(sum(valores)):>{largura}}")
    return linhas


def montar_relatorio(resultados: Dict[str, Dict[str, Any]], campeao: str) -> str:
    """Monta o relatório técnico completo (texto) do treinamento.

    Seções: dados, métricas globais, métricas por classe, matriz de confusão
    textual, validação cruzada, importância das features, PROVA OPERACIONAL
    (FP, FN e subestimações críticas de cada modelo, com veredito honesto),
    erros por cultura (se ``resultados[nome]["falsos_por_cultura"]`` existir)
    e a justificativa da escolha do campeão.
    """
    linha_dupla = "=" * LARGURA_RELATORIO
    linha_simples = "-" * LARGURA_RELATORIO
    partes: List[str] = [linha_dupla, "AgroAnálise — RELATÓRIO DE TREINAMENTO DA IA", linha_dupla]
    partes += ["", linha_dupla, TITULOS_SECOES_RELATORIO[0],
               "[escopo: IA do instante × motor de regras, divisão aleatória estratificada 80/20 do dataset simulado;",
               " mede o quanto a IA reproduz a regra do mesmo instante — NÃO mede antecipação nem acerto no campo]",
               linha_dupla]
    referencia = resultados[campeao]
    partes.append(f"Gerado em: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    partes.append(f"Features ({len(COLUNAS_FEATURES_MODELO)}): {', '.join(COLUNAS_FEATURES_MODELO)}")
    partes.append(f"Alvo: {COLUNA_ALVO}  (0 = {NOMES_STATUS[0]}, 1 = {NOMES_STATUS[1]}, 2 = {NOMES_STATUS[2]})")
    if "n_treino" in referencia:
        total = int(referencia["n_treino"]) + int(referencia["n_teste"])
        partes.append(f"Amostras: {total} no total | treino {referencia['n_treino']} (80%) | "
                      f"teste {referencia['n_teste']} (20%) — divisão estratificada, semente {SEMENTE_ALEATORIA}")
    else:
        partes.append(f"Amostras de teste: {referencia['n_teste']}")
    distribuicao = referencia["distribuicao_teste"]
    partes.append("Distribuição no teste: " + " | ".join(
        f"{ROTULOS_CURTOS[c]} {distribuicao[c]} ({_percentual(distribuicao[c], referencia['n_teste'])})"
        for c in CLASSES_STATUS))

    for nome, resultado in resultados.items():
        marcador = "  <<< CAMPEÃO" if nome == campeao else ""
        partes += ["", linha_dupla, f"MODELO: {nome}{marcador}", linha_dupla]
        partes.append("Métricas globais no conjunto de teste:")
        partes.append(f"  Accuracy ............................ {_decimal(resultado['accuracy'])}  "
                      f"({_percentual(resultado['accuracy'], 1.0)})")
        partes.append(f"  Precision  macro / ponderada ........ {_decimal(resultado['precision_macro'])} / "
                      f"{_decimal(resultado['precision_ponderada'])}")
        partes.append(f"  Recall     macro / ponderado ........ {_decimal(resultado['recall_macro'])} / "
                      f"{_decimal(resultado['recall_ponderado'])}")
        partes.append(f"  F1-Score   macro / ponderado ........ {_decimal(resultado['f1_macro'])} / "
                      f"{_decimal(resultado['f1_ponderado'])}")
        partes.append("")
        partes.append("Métricas por classe:")
        partes.append(f"  {'Classe':<26}{'Precision':>11}{'Recall':>11}{'F1-Score':>11}{'Suporte':>10}")
        for classe in CLASSES_STATUS:
            dados = resultado["por_classe"][classe]
            partes.append(f"  {NOMES_STATUS[classe]:<26}{_decimal(dados['precision']):>11}{_decimal(dados['recall']):>11}"
                          f"{_decimal(dados['f1']):>11}{int(dados['suporte']):>10}")
        partes.append("")
        partes.append("Matriz de confusão (teste):")
        partes += ["  " + texto for texto in _matriz_textual(resultado["matriz_confusao"])]
        validacao = resultado.get("validacao_cruzada")
        if validacao:
            partes.append("")
            partes.append(f"Validação cruzada estratificada ({int(validacao['dobras'])} dobras, "
                          f"{int(validacao['amostras'])} amostras de treino) — média ± desvio-padrão:")
            for chave, rotulo in (("accuracy", "Accuracy"), ("precision_macro", "Precision macro"),
                                  ("recall_macro", "Recall macro"), ("f1_macro", "F1 macro"),
                                  ("f1_ponderado", "F1 ponderado")):
                partes.append(f"  {rotulo:<18} {_decimal(validacao[f'{chave}_media'])} ± {_decimal(validacao[f'{chave}_desvio'])}")
            partes.append(f"  Erros operacionais fora da dobra: FP={int(validacao['falsos_positivos_fora_dobra'])}  "
                          f"FN={int(validacao['falsos_negativos_fora_dobra'])}  "
                          f"subestimações críticas={int(validacao['subestimacoes_criticas_fora_dobra'])}")
        importancias = resultado.get("importancia_features")
        if importancias:
            partes.append("")
            partes.append("Importância das features (redução média de impureza):")
            for coluna, valor in sorted(importancias.items(), key=lambda item: item[1], reverse=True):
                partes.append(f"  {coluna:<22} {_decimal(valor)}  {'#' * int(round(40 * valor))}")

    partes += ["", linha_dupla, "PROVA OPERACIONAL — erros que custam dinheiro no campo (conjunto de teste)", linha_dupla]
    partes.append("Falso Positivo (FP)  = real Verde, IA disse Amarelo/Vermelho → alarme falso: o produtor")
    partes.append("                       irriga ou age sem precisar (DESPERDÍCIO DE ÁGUA e insumo).")
    partes.append("Falso Negativo (FN)  = real Amarelo/Vermelho, IA disse Verde → risco não detectado")
    partes.append("                       (PERDA DE SAFRA: seca, encharcamento, calor ou geada sem aviso).")
    partes.append("Subestimação crítica = real Vermelho, IA disse Amarelo ou Verde → urgência rebaixada.")
    partes.append(linha_simples)
    partes.append(f"  {'Modelo':<26}{'FP':>8}{'FN':>8}{'FP+FN':>8}{'Subest.':>9}{'Erros':>8}{'Amostras':>10}")
    for nome, resultado in resultados.items():
        falsos = resultado["falsos"]
        partes.append(f"  {nome:<26}{falsos['falsos_positivos']:>8}{falsos['falsos_negativos']:>8}"
                      f"{falsos['falsos_positivos'] + falsos['falsos_negativos']:>8}"
                      f"{falsos['subestimacoes_criticas']:>9}{falsos['erros_totais']:>8}{falsos['total_amostras']:>10}")
    partes.append(linha_simples)
    for nome, resultado in resultados.items():
        falsos = resultado["falsos"]
        reais_verdes = resultado["distribuicao_teste"][STATUS_VERDE]
        reais_risco = resultado["distribuicao_teste"][STATUS_AMARELO] + resultado["distribuicao_teste"][STATUS_VERMELHO]
        reais_vermelhos = resultado["distribuicao_teste"][STATUS_VERMELHO]
        fp, fn, sub = falsos["falsos_positivos"], falsos["falsos_negativos"], falsos["subestimacoes_criticas"]
        partes.append(f"{nome}:")
        partes.append(f"  FP = {fp} de {reais_verdes} momentos realmente Verdes ({_percentual(fp, reais_verdes)})")
        partes.append(f"  FN = {fn} de {reais_risco} momentos realmente em risco ({_percentual(fn, reais_risco)})")
        partes.append(f"  Subestimações críticas = {sub} de {reais_vermelhos} momentos Vermelhos "
                      f"({_percentual(sub, reais_vermelhos)})")
        if fp == 0 and fn == 0 and sub == 0:
            veredito = ("APROVADO — zerou Falsos Positivos, Falsos Negativos e subestimações críticas "
                        "neste conjunto de teste.")
        elif fp == 0 and fn == 0:
            veredito = (f"APROVADO COM RESSALVA — zerou FP e FN, mas rebaixou {sub} situação(ões) Vermelha(s) "
                        "para Amarelo (o produtor é avisado, porém sem o tom de urgência).")
        else:
            veredito = (f"NÃO ZEROU — {fp} alarme(s) falso(s) e {fn} risco(s) não detectado(s) no teste. "
                        "Os números acima são os reais, sem ajuste.")
        partes.append(f"  VEREDITO: {veredito}")
        validacao = resultado.get("validacao_cruzada")
        if validacao:
            partes.append(f"  Confirmação na validação cruzada (fora da dobra): FP={int(validacao['falsos_positivos_fora_dobra'])}, "
                          f"FN={int(validacao['falsos_negativos_fora_dobra'])}, "
                          f"subestimações críticas={int(validacao['subestimacoes_criticas_fora_dobra'])}")
        por_cultura = resultado.get("falsos_por_cultura")
        if por_cultura:
            for cultura, falsos_cultura in por_cultura.items():
                partes.append(f"    [{cultura}] FP={falsos_cultura['falsos_positivos']}  "
                              f"FN={falsos_cultura['falsos_negativos']}  "
                              f"subestimações críticas={falsos_cultura['subestimacoes_criticas']}  "
                              f"(amostras={falsos_cultura['total_amostras']})")

    falsos_campeao = resultados[campeao]["falsos"]
    partes += ["", linha_dupla, "ESCOLHA DO CAMPEÃO", linha_dupla]
    partes.append("Critério: menor (FP + FN) no teste; desempate pelo maior F1 macro.")
    for nome, resultado in sorted(resultados.items(),
                                  key=lambda item: (item[1]["falsos"]["falsos_positivos"] + item[1]["falsos"]["falsos_negativos"],
                                                    -item[1]["f1_macro"])):
        falsos = resultado["falsos"]
        partes.append(f"  {nome:<26} FP+FN = {falsos['falsos_positivos'] + falsos['falsos_negativos']:<6} "
                      f"F1 macro = {_decimal(resultado['f1_macro'])}")
    partes.append(f"CAMPEÃO: {campeao}")
    if falsos_campeao["falsos_positivos"] == 0 and falsos_campeao["falsos_negativos"] == 0:
        partes.append("Conclusão: o modelo campeão zerou Falsos Positivos e Falsos Negativos no conjunto de teste.")
    else:
        partes.append(f"Conclusão: o modelo campeão NÃO zerou os erros operacionais "
                      f"(FP={falsos_campeao['falsos_positivos']}, FN={falsos_campeao['falsos_negativos']}); "
                      "é o melhor dos modelos comparados, não um modelo perfeito.")
    partes.append("")
    partes.append("Observações metodológicas (para leitura honesta dos números):")
    partes.append("  * O rótulo status_alerta é produzido pelo motor de regras agronômicas (faixas da Embrapa/")
    partes.append("    Incaper) sobre os dados saneados; a IA aprende a reproduzir e generalizar esse critério.")
    partes.append("    Métricas próximas de 100% significam fidelidade às regras, não uma verdade de campo.")
    partes.append("  * O dataset é uma série horária simulada; a divisão aleatória coloca horas vizinhas em treino")
    partes.append("    e teste. Como o rótulo depende só das leituras do mesmo instante, isso não vaza o rótulo,")
    partes.append("    mas o teste definitivo será com leituras reais do sensor no campo.")
    partes.append("  * Em operação o sistema exibe status_alerta = max(status_regra, status_predito) (princípio da")
    partes.append("    precaução): um Falso Negativo isolado da IA é coberto pelo motor de regras; um Falso")
    partes.append("    Positivo da IA, porém, chega ao produtor como alarme falso.")
    partes.append(linha_dupla)
    return "\n".join(partes)


# ---------------------------------------------------------------------------
# Persistência
# ---------------------------------------------------------------------------
def _metricas_serializaveis(metricas: Dict[str, Any]) -> Dict[str, Any]:
    """Remove o estimador e as predições brutas e converte chaves numéricas para texto."""
    limpas: Dict[str, Any] = {}
    for chave, valor in metricas.items():
        if chave in ("modelo", "y_pred"):
            continue
        if isinstance(valor, dict):
            limpas[chave] = {str(sub_chave): (_metricas_serializaveis(sub_valor) if isinstance(sub_valor, dict) else sub_valor)
                             for sub_chave, sub_valor in valor.items()}
        else:
            limpas[chave] = valor
    return limpas


def salvar_modelo(
    modelo: Any,
    nome_modelo: str,
    metricas: Dict[str, Any],
    caminho: Path = CAMINHO_MODELO,
    culturas: Optional[List[str]] = None,
) -> Path:
    """Salva o pacote do modelo campeão com ``joblib.dump``.

    Formato do pacote (dict)::

        {"modelo": estimador, "nome_modelo": "RandomForestClassifier",
         "versao": "RandomForestClassifier-AAAAMMDDHHMMSS",
         "features": COLUNAS_FEATURES_MODELO, "classes": [0, 1, 2],
         "metricas": {...}, "treinado_em": "AAAA-MM-DDTHH:MM:SS",
         "culturas": ["cafe_arabica", "cafe_conilon"]}

    A chave ``culturas`` registra as culturas presentes no treino. Três das
    cinco features são valores ABSOLUTOS (temperatura e umidades), então o
    modelo só vale para as culturas que viu: para uma cultura cadastrada
    depois do treino, o coletor usa o motor de regras (``REGRAS``) até o
    modelo ser retreinado — sem isso, a IA geraria alarmes falsos.

    Args:
        modelo: estimador treinado.
        nome_modelo: nome do algoritmo (prefixo da versão).
        metricas: resultado de ``avaliar_modelo`` (o estimador e as predições
            brutas são removidos antes de gravar).
        caminho: arquivo ``.pkl`` de destino.
        culturas: códigos das culturas do treino; ``None`` usa as chaves de
            ``metricas["falsos_por_cultura"]`` (se houver).

    Returns:
        Caminho do arquivo ``.pkl`` gravado.
    """
    destino = Path(caminho)
    destino.parent.mkdir(parents=True, exist_ok=True)
    agora = datetime.now()
    classes_modelo = getattr(modelo, "classes_", np.asarray(CLASSES_STATUS))
    pacote: Dict[str, Any] = {
        "modelo": modelo,
        "nome_modelo": nome_modelo,
        "versao": f"{nome_modelo}-{agora.strftime('%Y%m%d%H%M%S')}",
        "features": list(COLUNAS_FEATURES_MODELO),
        "classes": [int(classe) for classe in classes_modelo],
        "metricas": _metricas_serializaveis(metricas),
        "treinado_em": agora.isoformat(timespec="seconds"),
        "culturas": _culturas_do_treino(culturas, metricas),
    }
    joblib.dump(pacote, destino)
    registrador.info("Pacote do modelo salvo em %s (versão %s; culturas do treino: %s).", destino, pacote["versao"],
                     ", ".join(pacote["culturas"]) or "nenhuma informada")
    return destino


def _culturas_do_treino(culturas: Optional[List[str]], metricas: Dict[str, Any]) -> List[str]:
    """Lista ordenada e sem repetição das culturas do treino (parâmetro explícito ou métricas por cultura)."""
    if culturas is None:
        por_cultura = metricas.get("falsos_por_cultura")
        culturas = list(por_cultura) if isinstance(por_cultura, dict) else []
    return sorted({str(codigo).strip() for codigo in culturas if str(codigo).strip()})


# ---------------------------------------------------------------------------
# Prognóstico de 12 h (antecipação de estresse)
# ---------------------------------------------------------------------------
def _parametros_das_culturas(
    df: pd.DataFrame, parametros_por_cultura: Optional[Dict[str, ParametrosCultura]] = None,
    caminho_banco: Path = CAMINHO_BANCO,
) -> Dict[str, ParametrosCultura]:
    """Faixas de cada cultura presente no dataset (as informadas ou as do banco)."""
    resultado: Dict[str, ParametrosCultura] = dict(parametros_por_cultura or {})
    for cultura in sorted(df["cultura"].astype(str).unique()):
        if cultura not in resultado:
            resultado[cultura] = buscar_parametros_cultura(cultura, caminho_banco=caminho_banco)
    return resultado


def rotular_instantaneo(df_horario: pd.DataFrame, parametros: ParametrosCultura) -> pd.Series:
    """Rótulo da regra INSTANTÂNEA (v2): ``max(status da temperatura, status do solo)`` de cada leitura.

    Usa ``prognostico.status_vetorizado`` (idêntico a ``classificar_variavel``)
    sobre ``temperatura_ar`` e ``umidade_solo``. Uma variável ausente não
    conta; sem nenhuma das duas a hora fica Verde.

    Returns:
        Série inteira 0/1/2 com o mesmo índice de ``df_horario``.
    """
    temperatura = pd.to_numeric(df_horario["temperatura_ar"], errors="coerce").to_numpy(dtype=float)
    solo = pd.to_numeric(df_horario["umidade_solo"], errors="coerce").to_numpy(dtype=float)
    status = np.fmax(status_vetorizado(temperatura, parametros.faixa(VARIAVEL_TEMPERATURA_AR)),
                     status_vetorizado(solo, parametros.faixa(VARIAVEL_UMIDADE_SOLO)))
    return pd.Series(np.nan_to_num(status, nan=float(STATUS_VERDE)).astype(np.int64), index=df_horario.index)


def _rotulador_com_modo(funcao: Callable[..., Any], modo: str) -> Rotulador:
    """Embrulha uma função de rótulo: confere a saída (0/1/2, mesmo índice) e marca o modo efetivo."""

    def rotular(df_horario: pd.DataFrame, parametros: ParametrosCultura) -> pd.Series:
        """Rótulo horário 0/1/2 (``modo_efetivo`` diz qual regra produziu)."""
        entrada = df_horario.copy()
        bruto = funcao(entrada, parametros)
        serie = pd.Series(bruto)
        if len(serie) != len(df_horario):
            raise ValueError(f"O rotulador '{modo}' devolveu {len(serie)} valores para {len(df_horario)} horas.")
        valores = pd.to_numeric(serie, errors="coerce").to_numpy(dtype=float)
        valores = np.clip(np.nan_to_num(valores, nan=float(STATUS_VERDE)), STATUS_VERDE, STATUS_VERMELHO)
        return pd.Series(np.rint(valores).astype(np.int64), index=df_horario.index)

    rotular.modo_efetivo = modo  # type: ignore[attr-defined]
    return rotular


def carregar_rotulador(modo: str) -> Rotulador:
    """Função de rótulo do prognóstico para a regra de temperatura ``modo``.

    * ``'instantanea'``: ``rotular_instantaneo`` (rótulo da v2).
    * ``'fisiologica'``: ``regras_agronomicas.rotular_status_horario``
      (Frente 1), carregada por ``importlib.import_module`` — conforto pela
      média de 24 h limitado a Amarelo, Vermelho só por geada no abrigo e
      calor de dano. Se o módulo ou a função faltarem, registra um WARNING e
      volta ao rótulo instantâneo.

    O callable devolvido tem o atributo ``modo_efetivo`` (``'instantanea'``
    ou ``'fisiologica'``), que é o que de fato rotulou.

    Raises:
        ValueError: ``modo`` fora de ``REGRAS_TEMPERATURA``.
    """
    escolhido = str(modo or "").strip().lower()
    if escolhido not in REGRAS_TEMPERATURA:
        raise ValueError(f"Regra de temperatura desconhecida: {modo!r}. Válidas: {', '.join(REGRAS_TEMPERATURA)}.")
    if escolhido == REGRA_TEMPERATURA_FISIOLOGICA:
        try:
            modulo = importlib.import_module(NOME_MODULO_REGRAS)
            funcao = getattr(modulo, NOME_FUNCAO_ROTULO_FISIOLOGICO)
            if not callable(funcao):
                raise AttributeError(f"{NOME_MODULO_REGRAS}.{NOME_FUNCAO_ROTULO_FISIOLOGICO} não é chamável")
        except (ImportError, AttributeError) as erro:
            registrador.warning("Regra fisiológica indisponível (%s); o prognóstico será rotulado com a regra "
                                "instantânea.", erro)
        else:
            return _rotulador_com_modo(lambda df, parametros: funcao(df, parametros), REGRA_TEMPERATURA_FISIOLOGICA)
    return _rotulador_com_modo(rotular_instantaneo, REGRA_TEMPERATURA_INSTANTANEA)


def modo_do_rotulador(rotulador: Optional[Rotulador]) -> str:
    """Regra efetiva de um rotulador (``None`` = rótulo do dataset, que é o instantâneo da v2)."""
    if rotulador is None:
        return REGRA_TEMPERATURA_INSTANTANEA
    return str(getattr(rotulador, "modo_efetivo", REGRA_TEMPERATURA_INSTANTANEA))


def _entrada_rotulador(dados: pd.DataFrame) -> pd.DataFrame:
    """Colunas do contrato do rotulador (``data_hora``, ``temperatura_ar``, ``umidade_solo`` e ``umidade_ar``)."""
    colunas = ["data_hora", "temperatura_ar", "umidade_solo"] + (["umidade_ar"] if "umidade_ar" in dados else [])
    entrada = dados[colunas].copy()
    entrada["data_hora"] = pd.to_datetime(entrada["data_hora"])
    return entrada


def _serie_prognostico_completa(
    df_limpo: pd.DataFrame, parametros_por_cultura: Optional[Dict[str, ParametrosCultura]] = None,
    rotulador: Optional[Rotulador] = None,
) -> pd.DataFrame:
    """Todas as horas de cada cultura com features temporais, status atual, alvo e persistência.

    Colunas extras: ``data_hora`` (Timestamp), ``cultura``, ``status_atual``
    (motor de regras — a coluna ``status_alerta`` do dataset, ou o
    ``rotulador`` quando informado), ``alvo`` (Vermelho nas próximas 12 h;
    NaN no fim), ``persistencia_12h`` (houve Vermelho nas últimas 12 h,
    inclusive agora) e as leituras de temperatura, umidade do ar e do solo.
    """
    parametros = _parametros_das_culturas(df_limpo, parametros_por_cultura)
    partes: List[pd.DataFrame] = []
    for cultura in sorted(df_limpo["cultura"].astype(str).unique()):
        dados = df_limpo.loc[df_limpo["cultura"].astype(str) == cultura].copy()
        dados["data_hora"] = pd.to_datetime(dados["data_hora"])
        dados = dados.sort_values("data_hora").drop_duplicates("data_hora", keep="last").reset_index(drop=True)
        features = calcular_features_temporais(dados, parametros[cultura])
        if rotulador is None:
            status = dados[COLUNA_ALVO].astype(float)
        else:
            status = rotulador(_entrada_rotulador(dados), parametros[cultura]).astype(float).reset_index(drop=True)
        bloco = pd.DataFrame({
            "data_hora": dados["data_hora"],
            "cultura": cultura,
            "temperatura_ar": dados["temperatura_ar"].astype(float),
            "umidade_ar": dados["umidade_ar"].astype(float),
            "umidade_solo": dados["umidade_solo"].astype(float),
            "status_atual": status.astype(np.int64),
            "alvo": calcular_alvo_risco_futuro(status, HORIZONTE_PROGNOSTICO_HORAS),
            "persistencia_12h": (status.rolling(HORIZONTE_PROGNOSTICO_HORAS, min_periods=1).max()
                                 >= STATUS_VERMELHO).astype(np.int64),
        })
        partes.append(pd.concat([bloco, features.reset_index(drop=True)], axis=1))
    completo = pd.concat(partes, ignore_index=True)
    return completo.sort_values(["data_hora", "cultura"], kind="mergesort").reset_index(drop=True)


def preparar_dataset_prognostico(
    df_limpo: pd.DataFrame, parametros_por_cultura: Optional[Dict[str, ParametrosCultura]] = None,
    rotulador: Optional[Rotulador] = None,
) -> pd.DataFrame:
    """Dataset do prognóstico: por cultura, em ordem de ``data_hora``, features temporais + alvo.

    Mantém só as horas com status atual < 2 (o prognóstico só existe para
    antecipar: quem já está Vermelho é tratado pelo motor de regras), com as
    features completas (primeiras 24 h descartadas) e com futuro conhecido
    (últimas 12 h descartadas). As linhas ficam intercaladas por hora entre
    as culturas, prontas para divisões temporais. ``rotulador`` (ver
    ``carregar_rotulador``) troca o rótulo do dataset pelo da regra escolhida;
    ``None`` mantém o rótulo do dataset (comportamento da v2).
    """
    completo = _serie_prognostico_completa(df_limpo, parametros_por_cultura, rotulador)
    filtro = (completo["status_atual"] < STATUS_VERMELHO) & completo["alvo"].notna()
    filtro &= completo[COLUNAS_FEATURES_PROGNOSTICO].notna().all(axis=1)
    resultado = completo.loc[filtro].copy()
    resultado["alvo"] = resultado["alvo"].astype(np.int64)
    return resultado.reset_index(drop=True)


def dividir_temporal(
    df: pd.DataFrame, fracao_teste: float = 0.3, intervalo_horas: int = HORIZONTE_PROGNOSTICO_HORAS
) -> Tuple[pd.Index, pd.Index]:
    """Divisão temporal por cultura: as últimas ``fracao_teste`` horas viram teste.

    Entre o fim do treino e o começo do teste fica um intervalo de
    ``intervalo_horas`` + 1 h sem uso, para que o alvo do treino (que olha 12 h
    à frente) não enxergue o período de teste: ``max(t_treino) + 12 h <
    min(t_teste)`` em cada cultura.

    Returns:
        ``(indices_treino, indices_teste)`` do ``df`` (sem sobreposição).
    """
    if not 0.0 < float(fracao_teste) < 1.0:
        raise ValueError(f"fracao_teste deve estar entre 0 e 1 (recebido {fracao_teste}).")
    momentos = pd.to_datetime(df["data_hora"])
    culturas = df["cultura"].astype(str) if "cultura" in df.columns else pd.Series("unica", index=df.index)
    treino: List[Any] = []
    teste: List[Any] = []
    for cultura in sorted(culturas.unique()):
        filtro = (culturas == cultura).to_numpy()
        horas = np.sort(momentos[filtro].unique())
        if horas.size < 2:
            continue
        corte = pd.Timestamp(horas[min(horas.size - 1, int(math.ceil(horas.size * (1.0 - float(fracao_teste)))))])
        limite_treino = corte - pd.Timedelta(hours=int(intervalo_horas) + 1)
        treino += df.index[filtro & (momentos <= limite_treino).to_numpy()].tolist()
        teste += df.index[filtro & (momentos >= corte).to_numpy()].tolist()
    return pd.Index(treino), pd.Index(teste)


def divisoes_temporais(
    momentos: pd.Series, dobras: int = 4, intervalo_horas: int = HORIZONTE_PROGNOSTICO_HORAS,
    fracao_validacao: Optional[float] = None,
) -> List[Tuple[np.ndarray, np.ndarray]]:
    """Dobras ``TimeSeriesSplit(gap=intervalo_horas)`` sobre as HORAS únicas (vale para várias culturas juntas).

    Com ``fracao_validacao`` (ex.: 0,05), cada validação tem esse tamanho e as
    dobras ficam no fim do período: todo modelo de dobra vê pelo menos
    ``1 − dobras × fracao_validacao`` do histórico (importante com estações do
    ano — uma dobra treinada só no verão não sabe nada de geada).

    Returns:
        Lista de ``(posicoes_treino, posicoes_validacao)`` (posições 0..n−1 da
        série ``momentos``); o treino sempre termina mais de ``intervalo_horas``
        antes da validação.
    """
    valores = pd.to_datetime(pd.Series(momentos)).to_numpy()
    horas_unicas = np.unique(valores)
    tamanho = None if fracao_validacao is None else max(1, int(len(horas_unicas) * float(fracao_validacao)))
    divisor = TimeSeriesSplit(n_splits=int(dobras), gap=int(intervalo_horas), test_size=tamanho)
    resultado: List[Tuple[np.ndarray, np.ndarray]] = []
    for posicoes_treino, posicoes_validacao in divisor.split(horas_unicas):
        horas_treino = horas_unicas[posicoes_treino]
        horas_validacao = horas_unicas[posicoes_validacao]
        resultado.append((np.flatnonzero(np.isin(valores, horas_treino)),
                          np.flatnonzero(np.isin(valores, horas_validacao))))
    return resultado


def _novo_random_forest_prognostico(semente: int) -> RandomForestClassifier:
    """Random Forest do prognóstico (folhas maiores = probabilidades mais suaves)."""
    return RandomForestClassifier(n_estimators=200, min_samples_leaf=15, max_features="sqrt",
                                  n_jobs=-1, random_state=int(semente))


def escolher_limiar_por_orcamento(
    probabilidades: np.ndarray,
    alvo: np.ndarray,
    horas_por_semana_max: float = ORCAMENTO_AMARELO_ANTECIPADO_HORAS_SEMANA,
) -> float:
    """Menor limiar (= maior recall de antecipação) cujo Amarelo antecipado FALSO cabe no orçamento.

    Amarelo antecipado falso = hora com ``P ≥ limiar`` e alvo 0 (não veio
    Vermelho nas 12 h seguintes). O orçamento é em horas por semana por
    talhão: ``falsos / (n_horas / 168) ≤ horas_por_semana_max``.

    Returns:
        O limiar escolhido (entre 0 e 1); ``1.0`` se nenhum limiar couber.
    """
    probabilidade = np.asarray(probabilidades, dtype=float)
    real = np.asarray(alvo, dtype=np.int64)
    if probabilidade.shape != real.shape or probabilidade.size == 0:
        raise ValueError("probabilidades e alvo precisam ter o mesmo tamanho (não vazio).")
    semanas = probabilidade.size / 168.0
    candidatos = np.unique(np.round(probabilidade, 4))
    melhor = 1.0
    for limiar in sorted(candidatos, reverse=True):
        if limiar <= 0.0:
            break
        falsos = float(np.sum((probabilidade >= limiar) & (real == 0)))
        if falsos / semanas <= float(horas_por_semana_max):
            melhor = float(limiar)
        else:
            break
    return melhor


def _tabela_noites(
    df_horario: pd.DataFrame, parametros_por_cultura: Dict[str, ParametrosCultura],
    coef_tmin: Optional[Tuple[float, float, float]] = None, hora_previsao: int = 19,
) -> pd.DataFrame:
    """Uma linha por noite e cultura: tarde (17 h–19 h), ponto de orvalho, mínima real (20 h–8 h) e previsão às 19 h."""
    linhas: List[Dict[str, Any]] = []
    for cultura in sorted(df_horario["cultura"].astype(str).unique()):
        dados = df_horario.loc[df_horario["cultura"].astype(str) == cultura,
                               ["data_hora", "temperatura_ar", "umidade_ar"]].copy()
        dados["data_hora"] = pd.to_datetime(dados["data_hora"])
        serie = dados.set_index("data_hora").sort_index()
        serie = serie[~serie.index.duplicated(keep="last")]
        critico_min = parametros_por_cultura[cultura].faixa(VARIAVEL_TEMPERATURA_AR).critico_min
        for dia in sorted(set(serie.index.normalize())):
            tarde = serie.loc[dia + pd.Timedelta(hours=17): dia + pd.Timedelta(hours=19)]
            noite = serie.loc[dia + pd.Timedelta(hours=20): dia + pd.Timedelta(hours=32)]
            if len(tarde) < 3 or len(noite) < 13:
                continue
            temperatura_tarde = float(tarde["temperatura_ar"].mean())
            umidade_tarde = float(tarde["umidade_ar"].mean())
            janela = tarde.reset_index()
            instante = (dia + pd.Timedelta(hours=hora_previsao)).to_pydatetime()
            linhas.append({
                "cultura": cultura, "data": dia,
                "temperatura_tarde": temperatura_tarde, "umidade_tarde": umidade_tarde,
                "orvalho_tarde": float(ponto_orvalho_magnus(temperatura_tarde, umidade_tarde)),
                "temperatura_minima": float(noite["temperatura_ar"].min()),
                "previsao": prever_temperatura_minima_noite(janela, instante, coef_tmin),
                "critico_min": float(critico_min),
            })
    return pd.DataFrame(linhas)


def ajustar_coef_tmin(
    df_horario: pd.DataFrame, parametros_por_cultura: Dict[str, ParametrosCultura]
) -> Tuple[Tuple[float, float, float], float, int]:
    """Ajusta ``Tmin = a + b·T_tarde + c·Td_tarde`` por mínimos quadrados nas noites da série.

    Returns:
        ``((a, b, c), erro_padrao_residual, numero_de_noites)``; com menos de
        10 noites devolve ``COEF_TMIN_PADRAO``.
    """
    noites = _tabela_noites(df_horario, parametros_por_cultura)
    if len(noites) < 10:
        return COEF_TMIN_PADRAO, float("nan"), int(len(noites))
    matriz = np.column_stack([np.ones(len(noites)), noites["temperatura_tarde"], noites["orvalho_tarde"]])
    alvo = noites["temperatura_minima"].to_numpy(dtype=float)
    coeficientes, *_ = np.linalg.lstsq(matriz, alvo, rcond=None)
    residuos = alvo - matriz @ coeficientes
    erro_padrao = float(np.sqrt(np.sum(residuos ** 2) / max(1, len(alvo) - 3)))
    return (float(coeficientes[0]), float(coeficientes[1]), float(coeficientes[2])), erro_padrao, int(len(noites))


def treinar_modelo_prognostico(
    df_prog: pd.DataFrame,
    semente: int = SEMENTE_ALEATORIA,
    df_horario: Optional[pd.DataFrame] = None,
    parametros_por_cultura: Optional[Dict[str, ParametrosCultura]] = None,
) -> Dict[str, Any]:
    """Treina a IA de prognóstico de 12 h e devolve o pacote pronto para ``salvar_modelo_prognostico``.

    * Modelo: ``RandomForestClassifier`` dentro de ``CalibratedClassifierCV``
      (``method='isotonic'``), com as dobras de ``TimeSeriesSplit(gap=12)``
      sobre as horas (a calibração nunca vê o futuro do seu treino). São 4
      dobras recentes de 5 % das horas cada (``FRACAO_DOBRA_CALIBRACAO``): com
      dobras iguais, a primeira treinaria só com o verão e puxaria as
      probabilidades de inverno para baixo.
    * Limiar: um modelo calibrado treinado nos primeiros 80 % do período é
      aplicado aos últimos 20 % (com intervalo de 12 h) e
      ``escolher_limiar_por_orcamento`` escolhe o limiar nesse trecho.
    * ``coef_tmin``: ajustados por mínimos quadrados nas noites de
      ``df_horario`` (série horária completa do mesmo período), com o
      erro-padrão em ``metricas``; sem ``df_horario``, ``COEF_TMIN_PADRAO``.

    Returns:
        ``{'modelo','features','limiar','horizonte_horas','culturas','coef_tmin','metricas','versao','treinado_em'}``.
    """
    dados = df_prog.sort_values(["data_hora", "cultura"], kind="mergesort").reset_index(drop=True)
    X = dados[COLUNAS_FEATURES_PROGNOSTICO].astype(float)
    y = dados["alvo"].astype(np.int64).to_numpy()
    if len(np.unique(y)) < 2:
        raise ValueError("O dataset do prognóstico precisa ter as duas classes (vira e não vira Vermelho).")

    def calibrado(X_parcial: pd.DataFrame, y_parcial: np.ndarray, momentos: pd.Series) -> CalibratedClassifierCV:
        """Random Forest calibrado (isotônico) com dobras temporais."""
        dobras = divisoes_temporais(momentos.reset_index(drop=True), dobras=DOBRAS_CALIBRACAO,
                                    fracao_validacao=FRACAO_DOBRA_CALIBRACAO)
        modelo = CalibratedClassifierCV(_novo_random_forest_prognostico(semente), method="isotonic", cv=dobras)
        modelo.fit(X_parcial.reset_index(drop=True), y_parcial)
        return modelo

    horas = np.unique(dados["data_hora"].to_numpy())
    corte = horas[int(len(horas) * 0.8)]
    filtro_cabeca = (dados["data_hora"] <= corte - np.timedelta64(HORIZONTE_PROGNOSTICO_HORAS + 1, "h")).to_numpy()
    filtro_cauda = (dados["data_hora"] >= corte).to_numpy()
    modelo_limiar = calibrado(X[filtro_cabeca], y[filtro_cabeca], dados.loc[filtro_cabeca, "data_hora"])
    probabilidades_cauda = modelo_limiar.predict_proba(X[filtro_cauda])[:, 1]
    limiar = escolher_limiar_por_orcamento(probabilidades_cauda, y[filtro_cauda])
    previsto_cauda = probabilidades_cauda >= limiar

    modelo = calibrado(X, y, dados["data_hora"])
    for par_calibrado in getattr(modelo, "calibrated_classifiers_", []):
        # Em tempo real a predição é de UMA linha a cada leitura: sem threads, a latência cai.
        estimador = getattr(par_calibrado, "estimator", None)
        if estimador is not None and hasattr(estimador, "n_jobs"):
            estimador.n_jobs = 1
    parametros = _parametros_das_culturas(dados, parametros_por_cultura)
    if df_horario is not None:
        coef_tmin, erro_padrao_tmin, noites = ajustar_coef_tmin(df_horario, parametros)
    else:
        coef_tmin, erro_padrao_tmin, noites = COEF_TMIN_PADRAO, float("nan"), 0
    agora = datetime.now()
    metricas: Dict[str, Any] = {
        "n_treino": int(len(dados)),
        "taxa_alvo_treino": float(np.mean(y)),
        "periodo_treino": [str(dados["data_hora"].min()), str(dados["data_hora"].max())],
        "limiar_recall_validacao": float(np.mean(previsto_cauda[y[filtro_cauda] == 1]))
        if np.any(y[filtro_cauda] == 1) else float("nan"),
        "limiar_falsos_hora_semana_validacao": float(np.sum(previsto_cauda & (y[filtro_cauda] == 0))
                                                     / max(1e-9, filtro_cauda.sum() / 168.0)),
        "orcamento_horas_semana": float(ORCAMENTO_AMARELO_ANTECIPADO_HORAS_SEMANA),
        "coef_tmin_erro_padrao": float(erro_padrao_tmin),
        "coef_tmin_noites": int(noites),
        "medianas_features": {coluna: float(X[coluna].median()) for coluna in COLUNAS_FEATURES_PROGNOSTICO},
    }
    return {
        "modelo": modelo,
        "features": list(COLUNAS_FEATURES_PROGNOSTICO),
        "limiar": float(limiar),
        "horizonte_horas": int(HORIZONTE_PROGNOSTICO_HORAS),
        "culturas": sorted(dados["cultura"].astype(str).unique().tolist()),
        "coef_tmin": tuple(float(valor) for valor in coef_tmin),
        "metricas": metricas,
        "versao": f"Prognostico12h-RandomForestCalibrado-{agora.strftime('%Y%m%d%H%M%S')}",
        "treinado_em": agora.isoformat(timespec="seconds"),
    }


def salvar_modelo_prognostico(pacote: Dict[str, Any], caminho: Path = CAMINHO_MODELO_PROGNOSTICO) -> Path:
    """Grava o pacote do prognóstico com ``joblib.dump``.

    Raises:
        ValueError: se o pacote não tiver exatamente as chaves de ``CHAVES_PACOTE_PROGNOSTICO``.
    """
    if set(pacote) != set(CHAVES_PACOTE_PROGNOSTICO):
        raise ValueError(f"Pacote de prognóstico com chaves inesperadas: {sorted(pacote)}.")
    destino = Path(caminho)
    destino.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(pacote, destino)
    registrador.info("Pacote do prognóstico salvo em %s (versão %s, limiar %.3f).", destino, pacote["versao"],
                     pacote["limiar"])
    return destino


# ---------------------------------------------------------------------------
# Avaliação honesta do prognóstico
# ---------------------------------------------------------------------------
def _auc_seguro(alvo: np.ndarray, pontuacao: np.ndarray) -> float:
    """AUC ROC; ``nan`` se só houver uma classe."""
    real = np.asarray(alvo, dtype=np.int64)
    if len(np.unique(real)) < 2:
        return float("nan")
    return float(roc_auc_score(real, np.asarray(pontuacao, dtype=float)))


def _metricas_binarias(alvo: np.ndarray, pontuacao: np.ndarray, previsto: np.ndarray) -> Dict[str, float]:
    """F1 macro, AUC, Brier e recall da classe 'vira Vermelho' de uma previsão binária."""
    real = np.asarray(alvo, dtype=np.int64)
    predito = np.asarray(previsto, dtype=np.int64)
    return {
        "f1_macro": float(f1_score(real, predito, average="macro", labels=[0, 1], zero_division=0)),
        "auc": _auc_seguro(real, pontuacao),
        "brier": float(brier_score_loss(real, np.clip(np.asarray(pontuacao, dtype=float), 0.0, 1.0))),
        "recall_vira_vermelho": float(recall_score(real, predito, labels=[1], average="macro", zero_division=0)),
        "precisao_vira_vermelho": float(precision_score(real, predito, labels=[1], average="macro", zero_division=0)),
        "amostras": float(real.size),
        "taxa_alvo": float(np.mean(real)) if real.size else float("nan"),
    }


def validacao_temporal(df_prog: pd.DataFrame, dobras: int = 4, semente: int = SEMENTE_ALEATORIA) -> Dict[str, Any]:
    """Validação temporal (``TimeSeriesSplit(gap=12)`` sobre as horas): prognóstico × persistência × regra pura.

    Em cada dobra: o Random Forest é treinado nos primeiros 75 % do treino da
    dobra, o limiar é escolhido por orçamento nos últimos 25 % (com intervalo
    de 12 h) e o modelo é avaliado na validação da dobra. Persistência = "o
    que aconteceu nas últimas 12 h se repete nas próximas 12 h"; regra pura =
    "Amarelo agora vira Vermelho".

    Returns:
        ``{"dobras": [...], "media": {...}, "divisoes": [(treino, validacao), ...]}``
        com F1 macro, AUC e recall de "vira Vermelho" de cada abordagem.
    """
    dados = df_prog.sort_values(["data_hora", "cultura"], kind="mergesort").reset_index(drop=True)
    X = dados[COLUNAS_FEATURES_PROGNOSTICO].astype(float)
    y = dados["alvo"].astype(np.int64).to_numpy()
    divisoes = divisoes_temporais(dados["data_hora"], dobras=int(dobras))
    resultados: List[Dict[str, Dict[str, float]]] = []
    for posicoes_treino, posicoes_validacao in divisoes:
        momentos_treino = dados["data_hora"].iloc[posicoes_treino]
        horas = np.unique(momentos_treino.to_numpy())
        corte = horas[int(len(horas) * 0.75)]
        interno = posicoes_treino[(momentos_treino <= corte - np.timedelta64(HORIZONTE_PROGNOSTICO_HORAS + 1, "h")).to_numpy()]
        calibracao = posicoes_treino[(momentos_treino >= corte).to_numpy()]
        resultado_dobra: Dict[str, Dict[str, float]] = {}
        y_validacao = y[posicoes_validacao]
        if len(np.unique(y[interno])) < 2 or calibracao.size == 0:
            continue
        modelo = _novo_random_forest_prognostico(semente).fit(X.iloc[interno], y[interno])
        limiar = escolher_limiar_por_orcamento(modelo.predict_proba(X.iloc[calibracao])[:, 1], y[calibracao])
        probabilidade = modelo.predict_proba(X.iloc[posicoes_validacao])[:, 1]
        resultado_dobra["prognostico"] = _metricas_binarias(y_validacao, probabilidade, probabilidade >= limiar)
        persistencia = dados["persistencia_12h"].to_numpy()[posicoes_validacao]
        resultado_dobra["persistencia"] = _metricas_binarias(y_validacao, persistencia, persistencia)
        regra = (dados["status_atual"].to_numpy()[posicoes_validacao] == STATUS_AMARELO).astype(np.int64)
        resultado_dobra["regra_pura"] = _metricas_binarias(y_validacao, regra, regra)
        resultado_dobra["limiar"] = {"valor": float(limiar)}
        resultados.append(resultado_dobra)
    media: Dict[str, Dict[str, float]] = {}
    for abordagem in ("prognostico", "persistencia", "regra_pura"):
        media[abordagem] = {
            chave: float(np.nanmean([dobra[abordagem][chave] for dobra in resultados]))
            for chave in ("f1_macro", "auc", "recall_vira_vermelho", "brier")
        } if resultados else {}
    return {"dobras": resultados, "media": media, "divisoes": divisoes}


def avaliar_antecipacao(
    alertas: pd.Series, status: pd.Series, horas_sem_vermelho_antes: int = 6,
    horizonte: int = HORIZONTE_PROGNOSTICO_HORAS,
) -> Dict[str, float]:
    """Mede a antecipação de UMA série horária (uma cultura, horas consecutivas).

    * Início de Vermelho = hora Vermelha precedida de pelo menos
      ``horas_sem_vermelho_antes`` horas sem Vermelho (oscilação na borda não
      conta como novo episódio).
    * Aviso prévio = horas seguidas de alerta imediatamente ANTES do início
      (para no primeiro buraco). Ex.: Vermelho às 10 h, alerta das 6 h às
      10 h → 4 h (6, 7, 8 e 9 h).
    * Alarme falso = hora não Vermelha com alerta e sem Vermelho nas
      ``horizonte`` horas seguintes; reportado em horas por semana.

    Returns:
        ``{"inicios_vermelho", "mediana_horas_aviso", "pct_inicios_com_3h",
        "horas_alarme_falso_semana", "horas_avaliadas"}``.
    """
    alerta = pd.Series(alertas).fillna(False).astype(bool).to_numpy()
    estado = pd.Series(status).astype(float).to_numpy()
    if alerta.shape != estado.shape:
        raise ValueError("alertas e status precisam ter o mesmo tamanho.")
    vermelho = estado >= STATUS_VERMELHO
    avisos: List[int] = []
    for posicao in np.flatnonzero(vermelho):
        anteriores = vermelho[max(0, posicao - int(horas_sem_vermelho_antes)):posicao]
        if posicao == 0 or anteriores.any():
            continue  # começo da série (sem passado) ou continuação do mesmo episódio
        horas_aviso = 0
        anterior = posicao - 1
        while anterior >= 0 and alerta[anterior] and not vermelho[anterior]:
            horas_aviso += 1
            anterior -= 1
        avisos.append(horas_aviso)
    futuro = calcular_alvo_risco_futuro(pd.Series(estado), int(horizonte)).to_numpy()
    falsos = alerta & ~vermelho & (futuro == 0)
    semanas = max(1e-9, estado.size / 168.0)
    return {
        "inicios_vermelho": float(len(avisos)),
        "mediana_horas_aviso": float(np.median(avisos)) if avisos else float("nan"),
        "pct_inicios_com_3h": float(100.0 * np.mean(np.asarray(avisos) >= 3)) if avisos else float("nan"),
        "horas_alarme_falso_semana": float(falsos.sum() / semanas),
        "horas_avaliadas": float(estado.size),
    }


def _avaliar_antecipacao_por_cultura(
    serie: pd.DataFrame, coluna_alerta: str, horas_sem_vermelho_antes: int = 6
) -> Dict[str, float]:
    """``avaliar_antecipacao`` em cada cultura e agregado (inícios somados, mediana e % sobre todos os inícios)."""
    avisos: List[float] = []
    falsos_total = 0.0
    horas_total = 0.0
    inicios_total = 0.0
    com_3h_total = 0.0
    for _, dados in serie.groupby("cultura", sort=True):
        dados = dados.sort_values("data_hora")
        resultado = avaliar_antecipacao(dados[coluna_alerta], dados["status_atual"], horas_sem_vermelho_antes)
        inicios_total += resultado["inicios_vermelho"]
        if resultado["inicios_vermelho"]:
            com_3h_total += resultado["pct_inicios_com_3h"] / 100.0 * resultado["inicios_vermelho"]
            avisos.append(resultado["mediana_horas_aviso"])
        falsos_total += resultado["horas_alarme_falso_semana"] * resultado["horas_avaliadas"] / 168.0
        horas_total += resultado["horas_avaliadas"]
    return {
        "inicios_vermelho": inicios_total,
        "mediana_horas_aviso": float(np.median(avisos)) if avisos else float("nan"),
        "pct_inicios_com_3h": float(100.0 * com_3h_total / inicios_total) if inicios_total else float("nan"),
        "horas_alarme_falso_semana": float(falsos_total / max(1e-9, horas_total / 168.0)),
    }


def _mediana_subhoraria(valores: np.ndarray, sigma: float, gerador: np.random.Generator, amostras: int = 12) -> np.ndarray:
    """Mediana de ``amostras`` leituras sub-horárias com ruído gaussiano (filtro implementado localmente)."""
    if sigma <= 0.0:
        return valores.copy()
    ruido = gerador.normal(0.0, sigma, size=(valores.size, amostras))
    return np.median(valores[:, None] + ruido, axis=1)


def avaliar_robustez_ruido(
    df_limpo: pd.DataFrame,
    parametros_por_cultura: Dict[str, ParametrosCultura],
    sigmas: Sequence[Tuple[float, float]] = ((0.0, 0.0), (0.5, 1.0), (1.0, 2.0), (2.0, 3.0)),
    semente: int = SEMENTE_ALEATORIA,
) -> List[Dict[str, float]]:
    """Quanto o semáforo do motor de regras erra com ruído de sensor (σ de temperatura em °C, σ de solo em pp).

    Para cada par de σ, soma ruído gaussiano às leituras limpas e reclassifica:
    sem filtro (uma leitura por hora) e com a mediana de 12 amostras
    sub-horárias. Mede % de cor errada, % de alarme a mais (cor mais grave
    que a real) e % de risco perdido (entre as horas realmente Amarelas ou
    Vermelhas, as mostradas com cor mais branda).

    Returns:
        Uma linha por par de σ com ``sigma_temperatura``, ``sigma_solo`` e as
        três taxas ``_sem_filtro`` e ``_mediana12``.
    """
    gerador = np.random.default_rng(int(semente))
    culturas = df_limpo["cultura"].astype(str).to_numpy()
    temperatura = df_limpo["temperatura_ar"].astype(float).to_numpy()
    solo = df_limpo["umidade_solo"].astype(float).to_numpy()

    def status(temp: np.ndarray, umid: np.ndarray) -> np.ndarray:
        """Status do motor de regras (vetorizado por cultura)."""
        resultado = np.zeros(temp.size, dtype=np.int64)
        for codigo, parametros in parametros_por_cultura.items():
            filtro = culturas == codigo
            if not filtro.any():
                continue
            resultado[filtro] = np.maximum(
                status_vetorizado(temp[filtro], parametros.faixa(VARIAVEL_TEMPERATURA_AR)),
                status_vetorizado(umid[filtro], parametros.faixa(VARIAVEL_UMIDADE_SOLO))).astype(np.int64)
        return resultado

    real = status(temperatura, solo)
    em_risco = real > STATUS_VERDE
    linhas: List[Dict[str, float]] = []
    for sigma_temperatura, sigma_solo in sigmas:
        linha: Dict[str, float] = {"sigma_temperatura": float(sigma_temperatura), "sigma_solo": float(sigma_solo)}
        sem_filtro = status(
            temperatura + (gerador.normal(0.0, sigma_temperatura, temperatura.size) if sigma_temperatura > 0 else 0.0),
            solo + (gerador.normal(0.0, sigma_solo, solo.size) if sigma_solo > 0 else 0.0))
        filtrado = status(_mediana_subhoraria(temperatura, float(sigma_temperatura), gerador),
                          _mediana_subhoraria(solo, float(sigma_solo), gerador))
        for sufixo, previsto in (("sem_filtro", sem_filtro), ("mediana12", filtrado)):
            linha[f"cor_errada_pct_{sufixo}"] = float(100.0 * np.mean(previsto != real))
            linha[f"alarme_a_mais_pct_{sufixo}"] = float(100.0 * np.mean(previsto > real))
            linha[f"risco_perdido_pct_{sufixo}"] = (float(100.0 * np.mean(previsto[em_risco] < real[em_risco]))
                                                    if em_risco.any() else 0.0)
        linhas.append(linha)
    return linhas


def curva_confiabilidade(alvo: np.ndarray, probabilidades: np.ndarray, bins: int = 5) -> List[Dict[str, float]]:
    """Curva de confiabilidade em ``bins`` faixas iguais de probabilidade: média prevista × frequência observada."""
    real = np.asarray(alvo, dtype=float)
    probabilidade = np.clip(np.asarray(probabilidades, dtype=float), 0.0, 1.0)
    bordas = np.linspace(0.0, 1.0, int(bins) + 1)
    posicao = np.clip(np.digitize(probabilidade, bordas[1:-1], right=False), 0, int(bins) - 1)
    linhas: List[Dict[str, float]] = []
    for indice in range(int(bins)):
        filtro = posicao == indice
        linhas.append({
            "inicio": float(bordas[indice]), "fim": float(bordas[indice + 1]), "amostras": float(filtro.sum()),
            "media_prevista": float(probabilidade[filtro].mean()) if filtro.any() else float("nan"),
            "frequencia_observada": float(real[filtro].mean()) if filtro.any() else float("nan"),
        })
    return linhas


def gerar_grafico_confiabilidade(
    curvas: Dict[str, List[Dict[str, float]]], caminho: Path = CAMINHO_CURVA_CONFIABILIDADE
) -> Path:
    """Desenha as curvas de confiabilidade (calibrado × não calibrado) e salva em PNG (backend Agg)."""
    destino = Path(caminho)
    destino.parent.mkdir(parents=True, exist_ok=True)
    figura, eixo = plt.subplots(figsize=(7.0, 6.2))
    eixo.plot([0, 1], [0, 1], linestyle="--", color="#888888", linewidth=1.2, label="Calibração perfeita")
    estilos = {"calibrado": ("#1e7d32", "o"), "nao_calibrado": ("#c0392b", "s")}
    for nome, linhas in curvas.items():
        pontos = [(linha["media_prevista"], linha["frequencia_observada"]) for linha in linhas
                  if linha["amostras"] > 0 and math.isfinite(linha["media_prevista"])]
        if not pontos:
            continue
        cor, marcador = estilos.get(nome, ("#333333", "^"))
        eixo.plot([p[0] for p in pontos], [p[1] for p in pontos], marker=marcador, color=cor, linewidth=2.0,
                  label="Calibrado (isotônico)" if nome == "calibrado" else "Não calibrado (Random Forest)")
    eixo.set_xlim(0.0, 1.0)
    eixo.set_ylim(0.0, 1.0)
    eixo.set_xlabel("Probabilidade prevista de Vermelho em 12 h", fontsize=11)
    eixo.set_ylabel("Frequência observada", fontsize=11)
    eixo.set_title("AgroAnálise — Curva de confiabilidade do prognóstico\n(teste temporal do simulador, 5 faixas)",
                   fontsize=12)
    eixo.grid(alpha=0.3)
    eixo.legend(loc="upper left", fontsize=10)
    figura.tight_layout()
    figura.savefig(destino, dpi=120)
    plt.close(figura)
    return destino


def avaliar_geada(
    df_horario: pd.DataFrame, parametros_por_cultura: Dict[str, ParametrosCultura],
    coef_tmin: Tuple[float, float, float],
) -> Dict[str, float]:
    """Aviso de geada às 19 h nas noites de ``df_horario``: acerto, alarmes falsos e erro da mínima prevista."""
    noites = _tabela_noites(df_horario, parametros_por_cultura, coef_tmin, hora_previsao=19)
    if noites.empty:
        return {"noites": 0.0, "noites_geada": 0.0, "pct_geadas_avisadas": float("nan"),
                "pct_avisos_sem_frio": float("nan"), "erro_absoluto_medio": float("nan"), "noites_avisadas": 0.0}
    previsao = noites["previsao"].astype(float)
    geada = noites["temperatura_minima"] < noites["critico_min"]
    avisada = previsao < noites["critico_min"] + MARGEM_GEADA_C
    sem_frio = noites["temperatura_minima"] >= noites["critico_min"] + MARGEM_GEADA_C
    return {
        "noites": float(len(noites)),
        "noites_geada": float(geada.sum()),
        "noites_avisadas": float(avisada.sum()),
        "pct_geadas_avisadas": float(100.0 * (avisada & geada).sum() / geada.sum()) if geada.any() else float("nan"),
        "pct_avisos_sem_frio": float(100.0 * (avisada & sem_frio).sum() / avisada.sum()) if avisada.any() else float("nan"),
        "erro_absoluto_medio": float(np.nanmean(np.abs(previsao - noites["temperatura_minima"]))),
    }


def _alertas_de_geada(serie: pd.DataFrame, parametros_por_cultura: Dict[str, ParametrosCultura],
                      coef_tmin: Tuple[float, float, float]) -> pd.Series:
    """Horas com aviso de geada ativo (previsão das 18 h/19 h abaixo de critico_min + margem, até as 6 h)."""
    alerta = pd.Series(False, index=serie.index)
    for cultura, dados in serie.groupby("cultura", sort=True):
        critico_min = parametros_por_cultura[str(cultura)].faixa(VARIAVEL_TEMPERATURA_AR).critico_min
        momentos = pd.to_datetime(dados["data_hora"])
        indexado = dados.assign(data_hora=momentos).set_index("data_hora")
        for dia in sorted(set(momentos.dt.normalize())):
            tarde = indexado.loc[dia + pd.Timedelta(hours=17): dia + pd.Timedelta(hours=19)].reset_index()
            for hora in (18, 19):
                previsao = prever_temperatura_minima_noite(tarde, (dia + pd.Timedelta(hours=hora)).to_pydatetime(),
                                                           coef_tmin)
                if previsao is not None and previsao < critico_min + MARGEM_GEADA_C:
                    janela = (momentos >= dia + pd.Timedelta(hours=hora)) & (momentos < dia + pd.Timedelta(hours=30))
                    alerta.loc[dados.index[janela.to_numpy()]] = True
                    break
    return alerta


def avaliar_prognostico_completo(
    df_limpo: pd.DataFrame, parametros_por_cultura: Optional[Dict[str, ParametrosCultura]] = None,
    semente: int = SEMENTE_ALEATORIA, *, rotulador: Optional[Rotulador] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Treina o prognóstico no período inicial e faz toda a avaliação honesta no teste temporal.

    ``rotulador`` (ver ``carregar_rotulador``) define o rótulo "Vermelho" do
    alvo; ``None`` usa o rótulo do dataset (instantâneo, comportamento da v2).

    Returns:
        ``(pacote, avaliacao)``: o pacote treinado nos 70 % iniciais (o mesmo
        avaliado) e um dicionário com as métricas das seções 2 a 5 e da geada.
    """
    parametros = _parametros_das_culturas(df_limpo, parametros_por_cultura)
    completo = _serie_prognostico_completa(df_limpo, parametros, rotulador)
    indices_treino, indices_teste = dividir_temporal(completo, 0.3, HORIZONTE_PROGNOSTICO_HORAS)
    utilizavel = (completo["status_atual"] < STATUS_VERMELHO) & completo["alvo"].notna()
    utilizavel &= completo[COLUNAS_FEATURES_PROGNOSTICO].notna().all(axis=1)
    treino = completo.loc[indices_treino][utilizavel.loc[indices_treino]].copy()
    teste_completo = completo.loc[indices_teste].copy()
    teste = teste_completo[utilizavel.loc[indices_teste]].copy()
    treino["alvo"] = treino["alvo"].astype(np.int64)
    teste["alvo"] = teste["alvo"].astype(np.int64)
    horario_treino = completo.loc[indices_treino, ["data_hora", "cultura", "temperatura_ar", "umidade_ar"]]
    horario_teste = completo.loc[indices_teste, ["data_hora", "cultura", "temperatura_ar", "umidade_ar"]]

    registrador.info("Prognóstico: treino temporal com %d horas, teste com %d horas.", len(treino), len(teste))
    pacote = treinar_modelo_prognostico(treino, semente=semente, df_horario=horario_treino,
                                        parametros_por_cultura=parametros)
    X_treino = treino[COLUNAS_FEATURES_PROGNOSTICO].astype(float)
    X_teste = teste[COLUNAS_FEATURES_PROGNOSTICO].astype(float)
    y_teste = teste["alvo"].to_numpy()
    nao_calibrado = _novo_random_forest_prognostico(semente).fit(X_treino, treino["alvo"].to_numpy())
    probabilidade = pacote["modelo"].predict_proba(X_teste)[:, 1]
    probabilidade_bruta = nao_calibrado.predict_proba(X_teste)[:, 1]
    limiar = float(pacote["limiar"])
    persistencia = teste["persistencia_12h"].to_numpy()
    regra = (teste["status_atual"].to_numpy() == STATUS_AMARELO).astype(np.int64)
    holdout = {
        "prognostico": _metricas_binarias(y_teste, probabilidade, probabilidade >= limiar),
        "persistencia": _metricas_binarias(y_teste, persistencia, persistencia),
        "regra_pura": _metricas_binarias(y_teste, regra, regra),
    }
    registrador.info("Validação temporal (4 dobras)...")
    validacao = validacao_temporal(pd.concat([treino, teste]).reset_index(drop=True), dobras=4, semente=semente)

    # Antecipação na série horária contínua do teste.
    serie = teste_completo.copy()
    serie["p"] = np.nan
    serie.loc[teste.index, "p"] = probabilidade
    serie["alerta_modelo"] = (serie["p"] >= limiar) & (serie["status_atual"] < STATUS_VERMELHO)
    serie["alerta_persistencia"] = (serie["persistencia_12h"] == 1) & (serie["status_atual"] < STATUS_VERMELHO)
    serie["alerta_regra"] = serie["status_atual"] == STATUS_AMARELO
    serie["alerta_secagem"] = serie["horas_ate_secar"] <= HORIZONTE_PROGNOSTICO_HORAS
    serie["alerta_geada"] = _alertas_de_geada(serie, parametros, pacote["coef_tmin"])
    serie["alerta_combinado"] = serie["alerta_modelo"] | (
        (serie["alerta_secagem"] | serie["alerta_geada"]) & (serie["status_atual"] < STATUS_VERMELHO))
    antecipacao = {nome: _avaliar_antecipacao_por_cultura(serie, f"alerta_{nome}")
                   for nome in ("modelo", "combinado", "persistencia", "regra")}

    robustez = avaliar_robustez_ruido(df_limpo, parametros)
    calibracao = {
        "brier_calibrado": float(brier_score_loss(y_teste, probabilidade)),
        "brier_nao_calibrado": float(brier_score_loss(y_teste, probabilidade_bruta)),
        "curva_calibrado": curva_confiabilidade(y_teste, probabilidade, 5),
        "curva_nao_calibrado": curva_confiabilidade(y_teste, probabilidade_bruta, 5),
    }
    geada = avaliar_geada(horario_teste, parametros, pacote["coef_tmin"])
    geada_treino = avaliar_geada(horario_treino, parametros, pacote["coef_tmin"])
    avaliacao: Dict[str, Any] = {
        "periodo_treino": (str(treino["data_hora"].min()), str(treino["data_hora"].max())),
        "periodo_teste": (str(teste["data_hora"].min()), str(teste["data_hora"].max())),
        "n_treino": int(len(treino)), "n_teste": int(len(teste)),
        "limiar": limiar, "holdout": holdout, "validacao": validacao, "antecipacao": antecipacao,
        "robustez": robustez, "calibracao": calibracao, "geada": geada, "geada_treino": geada_treino,
        "coef_tmin": tuple(pacote["coef_tmin"]), "metricas_treino": pacote["metricas"],
    }
    pacote["metricas"] = dict(pacote["metricas"])
    pacote["metricas"]["teste_temporal"] = {
        "auc": holdout["prognostico"]["auc"], "brier": holdout["prognostico"]["brier"],
        "brier_persistencia": holdout["persistencia"]["brier"], "f1_macro": holdout["prognostico"]["f1_macro"],
        "f1_macro_persistencia": holdout["persistencia"]["f1_macro"],
        "pct_inicios_vermelho_com_3h": antecipacao["modelo"]["pct_inicios_com_3h"],
        "pct_inicios_vermelho_com_3h_combinado": antecipacao["combinado"]["pct_inicios_com_3h"],
        "geada_pct_avisadas": geada["pct_geadas_avisadas"], "geada_pct_avisos_sem_frio": geada["pct_avisos_sem_frio"],
        "geada_erro_absoluto_medio": geada["erro_absoluto_medio"],
        "periodo": list(avaliacao["periodo_teste"]),
    }
    return pacote, avaliacao


def _numero(valor: float, casas: int = 3) -> str:
    """Número com vírgula decimal; 'n/d' para NaN."""
    return "n/d" if valor is None or not math.isfinite(float(valor)) else _decimal(float(valor), casas)


def montar_relatorio_prognostico(avaliacao: Dict[str, Any], caminho_grafico: Path) -> str:
    """Seções 2 a 5 (e a seção de geada) do relatório, cada uma com o rótulo de escopo."""
    linha_dupla = "=" * LARGURA_RELATORIO
    linha_simples = "-" * LARGURA_RELATORIO
    partes: List[str] = []
    holdout = avaliacao["holdout"]
    escopo_simulador = (f"[escopo: prognóstico de {HORIZONTE_PROGNOSTICO_HORAS} h no SIMULADOR climático; "
                        "não é desempenho de campo]")

    partes += ["", linha_dupla, "SEÇÃO 2 — Generalização temporal", escopo_simulador, linha_dupla]
    partes.append(f"Treino: {avaliacao['periodo_treino'][0]} a {avaliacao['periodo_treino'][1]} "
                  f"({avaliacao['n_treino']} horas não vermelhas)")
    partes.append(f"Teste : {avaliacao['periodo_teste'][0]} a {avaliacao['periodo_teste'][1]} "
                  f"({avaliacao['n_teste']} horas não vermelhas) — últimos 30 % de cada cultura, "
                  f"intervalo de {HORIZONTE_PROGNOSTICO_HORAS} h")
    partes.append(f"Alvo: entrar no Vermelho nas próximas {HORIZONTE_PROGNOSTICO_HORAS} h "
                  f"(taxa no teste {_percentual(holdout['prognostico']['taxa_alvo'], 1.0)}). "
                  f"Limiar por orçamento: {_numero(avaliacao['limiar'])} "
                  f"(≤ {_decimal(ORCAMENTO_AMARELO_ANTECIPADO_HORAS_SEMANA, 1)} h/semana de Amarelo antecipado falso).")
    partes.append("Persistência = 'as próximas 12 h repetem as últimas 12 h'; regra pura = 'Amarelo agora vira Vermelho'.")
    partes.append(linha_simples)
    partes.append(f"  {'Abordagem (teste temporal)':<30}{'F1 macro':>10}{'AUC':>8}{'Brier':>8}{'Recall':>8}{'Precisão':>10}")
    rotulos = {"prognostico": "IA de prognóstico (calibrada)", "persistencia": "Persistência", "regra_pura": "Regra pura"}
    for chave, rotulo in rotulos.items():
        m = holdout[chave]
        partes.append(f"  {rotulo:<30}{_numero(m['f1_macro']):>10}{_numero(m['auc']):>8}{_numero(m['brier']):>8}"
                      f"{_numero(m['recall_vira_vermelho']):>8}{_numero(m['precisao_vira_vermelho']):>10}")
    media = avaliacao["validacao"]["media"]
    partes.append(linha_simples)
    partes.append(f"Validação temporal (TimeSeriesSplit, 4 dobras, gap de {HORIZONTE_PROGNOSTICO_HORAS} h) — média das dobras")
    partes.append("(em cada dobra, Random Forest sem calibração e limiar por orçamento escolhido dentro do treino da dobra):")
    rotulos_validacao = {"prognostico": "IA de prognóstico (RF da dobra)", "persistencia": "Persistência",
                         "regra_pura": "Regra pura"}
    for chave, rotulo in rotulos_validacao.items():
        m = media.get(chave) or {}
        if m:
            partes.append(f"  {rotulo:<30} F1 macro {_numero(m['f1_macro'])}  AUC {_numero(m['auc'])}  "
                          f"recall 'vira Vermelho' {_numero(m['recall_vira_vermelho'])}")
    ganho = holdout["prognostico"]["f1_macro"] - holdout["persistencia"]["f1_macro"]
    partes.append(f"Ganho de F1 macro sobre a persistência no teste: {_numero(ganho)}")

    partes += ["", linha_dupla, "SEÇÃO 3 — Robustez a ruído de sensor",
               "[escopo: motor de regras sobre o dataset simulado + ruído gaussiano; filtro = mediana de 12 amostras]",
               linha_dupla]
    partes.append(f"  {'σ temp. / σ solo':<18}{'cor errada':>22}{'alarme a mais':>22}{'risco perdido':>22}")
    partes.append(f"  {'':<18}{'sem filtro | mediana':>22}{'sem filtro | mediana':>22}{'sem filtro | mediana':>22}")
    for linha in avaliacao["robustez"]:
        rotulo = f"{_decimal(linha['sigma_temperatura'], 1)} °C / {_decimal(linha['sigma_solo'], 1)} pp"
        celulas = [f"{_decimal(linha[f'{m}_sem_filtro'], 2)}% | {_decimal(linha[f'{m}_mediana12'], 2)}%"
                   for m in ("cor_errada_pct", "alarme_a_mais_pct", "risco_perdido_pct")]
        partes.append(f"  {rotulo:<18}" + "".join(f"{celula:>22}" for celula in celulas))

    partes += ["", linha_dupla, "SEÇÃO 4 — Teste de antecipação", escopo_simulador, linha_dupla]
    partes.append("Início de Vermelho = hora Vermelha depois de ≥ 6 h sem Vermelho. Aviso prévio = horas seguidas de")
    partes.append("alerta antes do início. Alarme falso = alerta sem Vermelho nas 12 h seguintes.")
    partes.append(f"  {'Alerta':<34}{'Inícios':>9}{'Mediana aviso':>15}{'% com ≥ 3 h':>13}{'Falsos h/sem.':>15}")
    nomes = {"modelo": "IA de prognóstico", "combinado": "IA + secagem (ETA ≤ 12 h) + geada",
             "persistencia": "Persistência", "regra": "Regra pura (Amarelo)"}
    for chave, rotulo in nomes.items():
        m = avaliacao["antecipacao"][chave]
        partes.append(f"  {rotulo:<34}{int(m['inicios_vermelho']):>9}{_numero(m['mediana_horas_aviso'], 1):>13} h"
                      f"{_numero(m['pct_inicios_com_3h'], 1):>12}%{_numero(m['horas_alarme_falso_semana'], 1):>15}")

    calibracao = avaliacao["calibracao"]
    partes += ["", linha_dupla, "SEÇÃO 5 — Calibração", escopo_simulador, linha_dupla]
    partes.append(f"Brier no teste temporal: calibrado {_numero(calibracao['brier_calibrado'], 4)} | "
                  f"não calibrado {_numero(calibracao['brier_nao_calibrado'], 4)} | "
                  f"persistência {_numero(holdout['persistencia']['brier'], 4)}")
    partes.append("Curva de confiabilidade (5 faixas): faixa → média prevista / frequência observada (amostras)")
    for nome, chave in (("Calibrado", "curva_calibrado"), ("Não calibrado", "curva_nao_calibrado")):
        partes.append(f"  {nome}:")
        for linha in calibracao[chave]:
            partes.append(f"    {_decimal(linha['inicio'], 1)}–{_decimal(linha['fim'], 1)}: "
                          f"{_numero(linha['media_prevista'])} / {_numero(linha['frequencia_observada'])} "
                          f"({int(linha['amostras'])})")
    partes.append(f"Gráfico: {caminho_grafico}")

    geada = avaliacao["geada"]
    a, b, c = avaliacao["coef_tmin"]
    metricas_treino = avaliacao["metricas_treino"]
    partes += ["", linha_dupla, "SEÇÃO 6 — Aviso de geada ainda à tarde",
               "[escopo: noites do teste temporal do simulador; previsão às 19 h pelo ponto de orvalho]", linha_dupla]
    partes.append(f"Tmin = {_numero(a)} + {_numero(b)}·T_tarde + {_numero(c)}·Td_tarde (ajuste em "
                  f"{metricas_treino.get('coef_tmin_noites', 0)} noites do treino; erro-padrão "
                  f"{_numero(metricas_treino.get('coef_tmin_erro_padrao', float('nan')), 2)} °C)")
    partes.append(f"Noites no teste: {int(geada['noites'])} | com mínima < critico_min: {int(geada['noites_geada'])} | "
                  f"avisadas: {int(geada['noites_avisadas'])}")
    partes.append(f"  Geadas avisadas até as 19 h ........ {_numero(geada['pct_geadas_avisadas'], 1)}%")
    partes.append(f"  Avisos sem mínima < crítico + 2 °C .. {_numero(geada['pct_avisos_sem_frio'], 1)}%")
    partes.append(f"  Erro absoluto médio da Tmin ......... {_numero(geada['erro_absoluto_medio'], 2)} °C")
    geada_treino = avaliacao.get("geada_treino") or {}
    if geada_treino:
        partes.append(f"  (no treino: {int(geada_treino['noites_geada'])} geadas, "
                      f"{_numero(geada_treino['pct_geadas_avisadas'], 1)}% avisadas, erro médio "
                      f"{_numero(geada_treino['erro_absoluto_medio'], 2)} °C)")
    partes.append("")
    partes.append("Leitura honesta: todas as métricas das seções 2 a 6 vêm do simulador climático (a IA aprende a")
    partes.append("física que o próprio simulador impõe). Elas provam que o método antecipa sem olhar o futuro e que o")
    partes.append("limiar respeita o orçamento de alarmes; o desempenho de campo só será conhecido com leituras reais.")
    partes.append(linha_dupla)
    return "\n".join(partes)


# ---------------------------------------------------------------------------
# Dado real: reanálise ERA5-Land (referência climática offline)
# ---------------------------------------------------------------------------
def ajustar_coef_tmin_referencia(
    noites: pd.DataFrame, ano_teste: int = ANO_TESTE_REFERENCIA
) -> Tuple[float, float, float]:
    """Coeficientes ``tmin_noite = a + b·t_tarde + c·td_tarde`` ajustados nas noites ANTERIORES a ``ano_teste``.

    ``noites`` vem de ``referencia_climatica.tabela_noites``. Em Varginha
    (ERA5-Land 2018–2020) o ajuste dá cerca de (−3,8; 0,54; 0,49), bem longe
    dos coeficientes do simulador: o comitê mediu MAE de 3,30 °C e viés de
    −2,93 °C com os coeficientes do simulador no clima real (persistência:
    1,20 °C). Ideia de prever a mínima com o ar do fim da tarde em IoT:
    Talsma et al., https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2022.963781/full

    Raises:
        ValueError: menos de 3 noites antes de ``ano_teste``.
    """
    anteriores = noites.loc[pd.to_datetime(noites["data"]).dt.year < int(ano_teste)]
    return minimos_quadrados_tmin(anteriores)


def quantis_conformal_tmin(residuos: pd.Series, meses: pd.Series, q: float = QUANTIL_CONFORMAL_TMIN) -> Dict[str, float]:
    """Margem conformal da mínima por estação: quantil ``q`` de ``|resíduo|`` com correção de amostra finita.

    Estação ``'seca'`` = maio a setembro; ``'aguas'`` = outubro a abril
    (``referencia_climatica.estacao_do_mes``). O quantil é o
    ``⌈(n+1)·q⌉``-ésimo menor ``|resíduo|`` da estação
    (``referencia_climatica.quantil_conformal``); estação sem resíduos fica de fora.

    Returns:
        ``{'seca': q_seca, 'aguas': q_aguas}`` (°C).
    """
    valores = pd.to_numeric(pd.Series(residuos).reset_index(drop=True), errors="coerce").astype(float)
    estacoes = pd.Series(meses).reset_index(drop=True).map(lambda mes: estacao_do_mes(int(mes)))
    resultado: Dict[str, float] = {}
    for estacao in ESTACOES:
        selecionados = valores[(estacoes == estacao).to_numpy()].dropna().to_numpy()
        if selecionados.size:
            quantil = quantil_conformal(np.abs(selecionados), float(q))
            if math.isfinite(quantil):
                resultado[estacao] = float(quantil)
    return resultado


def _residuos_por_ano(noites: pd.DataFrame) -> pd.Series:
    """Resíduos fora da amostra por ano (ajusta nos outros anos, prevê o ano deixado de fora)."""
    anos = pd.to_datetime(noites["data"]).dt.year
    residuos = pd.Series(np.nan, index=noites.index)
    if anos.nunique() < 2:
        return residuos_deixa_um_fora(noites).reindex(noites.index)
    for ano in sorted(anos.unique()):
        teste = noites.loc[(anos == ano).to_numpy()]
        treino = noites.loc[(anos != ano).to_numpy()]
        coef = minimos_quadrados_tmin(treino)
        residuos.loc[teste.index] = teste["tmin_noite"].to_numpy() - prever_tmin(teste, coef)
    return residuos


def ajustar_geada_regiao(regiao: str, ano_teste: int = ANO_TESTE_REFERENCIA,
                         diretorio: Path = DIRETORIO_REFERENCIA) -> Dict[str, Any]:
    """Coeficientes da mínima e margens conformais de UMA região, ajustados antes de ``ano_teste``.

    As margens usam resíduos FORA DA AMOSTRA (cada ano previsto pelos
    coeficientes ajustados nos outros anos anteriores ao teste), para o
    intervalo não ficar estreito demais.

    Returns:
        ``{"coef": (a, b, c), "quantis": {"seca": q, "aguas": q}, "noites": DataFrame}``.
    """
    noites = tabela_noites(carregar_referencia(regiao, diretorio))
    anteriores = noites.loc[pd.to_datetime(noites["data"]).dt.year < int(ano_teste)].dropna(
        subset=["t_tarde", "td_tarde", "tmin_noite"])
    coef = ajustar_coef_tmin_referencia(noites, ano_teste)
    residuos = _residuos_por_ano(anteriores)
    quantis = quantis_conformal_tmin(residuos, pd.to_datetime(anteriores["data"]).dt.month)
    return {"coef": coef, "quantis": quantis, "noites": noites}


def _margens_por_noite(noites: pd.DataFrame, quantis: Dict[str, float], padrao: float) -> np.ndarray:
    """Margem (°C) de cada noite pela estação do mês; ``padrao`` se a estação não tiver quantil."""
    meses = pd.to_datetime(noites["data"]).dt.month.to_numpy()
    return np.array([float(quantis.get(estacao_do_mes(int(mes)), padrao)) for mes in meses], dtype=float)


def avaliar_geada_referencia(
    coef_simulador: Optional[Tuple[float, float, float]] = None,
    ano_teste: int = ANO_TESTE_REFERENCIA,
    diretorio: Path = DIRETORIO_REFERENCIA,
    limiar_alarme_c: float = LIMIAR_GEADA_ABRIGO_PADRAO_C + INCERTEZA_SENSOR_AR_PADRAO_C,
    regioes: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Aviso de geada em DADO REAL: erro da mínima e alarmes no ano de teste, por região e por método.

    Métodos comparados nas noites de ``ano_teste`` (nenhum vê esse ano no ajuste):

    * ``regiao``: coeficientes da região (``ajustar_geada_regiao``) com a
      margem conformal q90 da estação;
    * ``simulador``: ``coef_simulador`` (do pacote) com ``MARGEM_GEADA_C``;
    * ``padrao``: ``COEF_TMIN_PADRAO`` com ``MARGEM_GEADA_C``;
    * ``persistencia``: a mínima da noite anterior, sem margem.

    Alarme = ``previsão − margem ≤ limiar_alarme_c`` (padrão 2,0 °C de geada no
    abrigo + 2,0 °C de incerteza do DHT11 = 4,0 °C). Evento = mínima real
    ≤ ``limiar_alarme_c``. Alarme falso = alarme sem evento.

    Returns:
        ``{regiao: {"coef", "quantis", "cobertura_q90": {estacao: fração},
        "noites_teste", "eventos", "metodos": {metodo: {"mae", "vies",
        "alarmes", "alarmes_falsos", "eventos_avisados"}}}}``.
    """
    resultado: Dict[str, Any] = {}
    for regiao in (regioes or list(REGIOES_REFERENCIA)):
        if not referencia_disponivel(regiao, diretorio):
            registrador.warning("Referência climática de %s ausente em %s; região fora da avaliação.", regiao, diretorio)
            continue
        ajuste = ajustar_geada_regiao(regiao, ano_teste, diretorio)
        noites = ajuste["noites"]
        teste = noites.loc[pd.to_datetime(noites["data"]).dt.year == int(ano_teste)].dropna(
            subset=["t_tarde", "td_tarde", "tmin_noite"]).reset_index(drop=True)
        real = teste["tmin_noite"].to_numpy(dtype=float)
        evento = real <= float(limiar_alarme_c)
        previsoes: Dict[str, Tuple[np.ndarray, np.ndarray]] = {
            "regiao": (prever_tmin(teste, ajuste["coef"]), _margens_por_noite(teste, ajuste["quantis"], MARGEM_GEADA_C)),
            "simulador": (prever_tmin(teste, coef_simulador or COEF_TMIN_PADRAO), np.full(len(teste), MARGEM_GEADA_C)),
            "padrao": (prever_tmin(teste, COEF_TMIN_PADRAO), np.full(len(teste), MARGEM_GEADA_C)),
            "persistencia": (teste["tmin_noite_anterior"].to_numpy(dtype=float), np.zeros(len(teste))),
        }
        metodos: Dict[str, Dict[str, float]] = {}
        for nome, (previsto, margem) in previsoes.items():
            valido = np.isfinite(previsto)
            erro = previsto[valido] - real[valido]
            alarme = valido & ((previsto - margem) <= float(limiar_alarme_c))
            metodos[nome] = {
                "mae": float(np.mean(np.abs(erro))) if erro.size else float("nan"),
                "vies": float(np.mean(erro)) if erro.size else float("nan"),
                "alarmes": float(alarme.sum()),
                "alarmes_falsos": float((alarme & ~evento).sum()),
                "eventos_avisados": float((alarme & evento).sum()),
            }
        erro_regiao = np.abs(previsoes["regiao"][0] - real)
        meses = pd.to_datetime(teste["data"]).dt.month
        cobertura: Dict[str, float] = {}
        for estacao in ESTACOES:
            filtro = (meses.map(lambda mes: estacao_do_mes(int(mes))) == estacao).to_numpy()
            if filtro.any() and estacao in ajuste["quantis"]:
                cobertura[estacao] = float(np.mean(erro_regiao[filtro] <= ajuste["quantis"][estacao]))
        resultado[regiao] = {
            "coef": tuple(float(valor) for valor in ajuste["coef"]), "quantis": dict(ajuste["quantis"]),
            "cobertura_q90": cobertura, "noites_teste": float(len(teste)), "eventos": float(evento.sum()),
            "minima_real_teste": float(np.min(real)) if real.size else float("nan"), "metodos": metodos,
        }
    return resultado


def _entrada_referencia(regiao: str, parametros: ParametrosCultura, diretorio: Path) -> pd.DataFrame:
    """Série da reanálise pronta para features e rótulo: solo FIXO no meio da faixa ideal (rótulo só térmico)."""
    referencia = carregar_referencia(regiao, diretorio)
    faixa_solo = parametros.faixa(VARIAVEL_UMIDADE_SOLO)
    return pd.DataFrame({
        "data_hora": referencia["data_hora"],
        "temperatura_ar": referencia["temperatura_ar"],
        "umidade_ar": referencia["umidade_ar"],
        "umidade_solo": (faixa_solo.ideal_min + faixa_solo.ideal_max) / 2.0,
    })


def _metricas_referencia(alvo: np.ndarray, probabilidade: np.ndarray, persistencia: np.ndarray,
                         climatologia: np.ndarray, limiar: float) -> Dict[str, float]:
    """Métricas do prognóstico em dado real contra persistência e climatologia (mesmas horas)."""
    real = np.asarray(alvo, dtype=np.int64)
    semanas = max(1e-9, real.size / 168.0)
    previsto = probabilidade >= float(limiar)
    alarme_persistencia = persistencia >= 0.5
    positivos = real == 1
    return {
        "horas": float(real.size), "positivos": float(positivos.sum()),
        "taxa_alvo": float(real.mean()) if real.size else float("nan"),
        "auc": _auc_seguro(real, probabilidade),
        "brier": float(brier_score_loss(real, np.clip(probabilidade, 0.0, 1.0))) if real.size else float("nan"),
        "recall": float(previsto[positivos].mean()) if positivos.any() else float("nan"),
        "precisao": float(real[previsto].mean()) if previsto.any() else float("nan"),
        "falso_amarelo_h_semana": float(np.sum(previsto & ~positivos) / semanas),
        "auc_persistencia": _auc_seguro(real, persistencia),
        "brier_persistencia": float(brier_score_loss(real, persistencia)) if real.size else float("nan"),
        "recall_persistencia": float(alarme_persistencia[positivos].mean()) if positivos.any() else float("nan"),
        "falso_amarelo_persistencia_h_semana": float(np.sum(alarme_persistencia & ~positivos) / semanas),
        "auc_climatologia": _auc_seguro(real, climatologia),
        "brier_climatologia": float(brier_score_loss(real, np.clip(climatologia, 0.0, 1.0))) if real.size else float("nan"),
    }


def avaliar_em_referencia(
    pacote: Dict[str, Any],
    parametros_por_cultura: Dict[str, ParametrosCultura],
    rotulador: Callable[[pd.DataFrame, ParametrosCultura], pd.Series],
    ano_teste: int = ANO_TESTE_REFERENCIA,
    diretorio: Path = DIRETORIO_REFERENCIA,
) -> Dict[str, Any]:
    """Aplica o pacote de prognóstico (treinado no simulador) à reanálise ERA5-Land real.

    Para cada cultura com região de referência (``resolver_regiao``): a
    temperatura e a umidade do ar vêm da reanálise, o solo fica FIXO no meio
    da faixa ideal (o rótulo é só térmico — a umidade do solo do modelo, em
    m³/m³, não é a do higrômetro em %). O rótulo de cada hora vem de
    ``rotulador``; o alvo é "Vermelho nas próximas 12 h". São avaliadas as
    horas não vermelhas de ``ano_teste``, contra:

    * persistência ("as próximas 12 h repetem as últimas 12 h");
    * climatologia mês × hora AJUSTADA NOS ANOS ANTERIORES a ``ano_teste``
      (fora da amostra; ``referencia_climatica.climatologia_mes_hora``).

    O comitê reproduziu a degradação da v2 em dado real (AUC 0,793 e 0,716;
    Brier pior que o da climatologia, 0,155 e 0,168) — esta função a deixa
    documentada a cada treino.

    Returns:
        Métricas agregadas das culturas (chaves ``auc``, ``brier``, ``recall``,
        ``precisao``, ``falso_amarelo_h_semana``, ``brier_persistencia``,
        ``auc_persistencia``, ``brier_climatologia``, ``auc_climatologia``,
        ``horas``, ``positivos``, ``taxa_alvo``), ``limiar``, ``ano_teste``,
        ``regra`` e ``por_cultura`` ``{cultura: {... , "regiao",
        "media_anual_c", "vermelho_pct_ano"}}``.
    """
    limiar = float(pacote["limiar"])
    features_pacote = list(pacote["features"])
    por_cultura: Dict[str, Dict[str, Any]] = {}
    acumulado: Dict[str, List[np.ndarray]] = {"alvo": [], "p": [], "pers": [], "clim": []}
    for codigo in sorted(parametros_por_cultura):
        parametros = parametros_por_cultura[codigo]
        regiao = resolver_regiao(codigo)
        if regiao is None or not referencia_disponivel(regiao, diretorio):
            continue
        entrada = _entrada_referencia(regiao, parametros, diretorio)
        features = calcular_features_temporais(entrada, parametros)
        status = rotulador(entrada.copy(), parametros).astype(float).reset_index(drop=True)
        alvo = calcular_alvo_risco_futuro(status, HORIZONTE_PROGNOSTICO_HORAS)
        persistencia = (status.rolling(HORIZONTE_PROGNOSTICO_HORAS, min_periods=1).max() >= STATUS_VERMELHO).astype(float)
        anos = pd.to_datetime(entrada["data_hora"]).dt.year.to_numpy()
        utilizavel = (status < STATUS_VERMELHO).to_numpy() & alvo.notna().to_numpy()
        utilizavel &= features[features_pacote].notna().all(axis=1).to_numpy()
        ajuste = utilizavel & (anos < int(ano_teste))
        teste = utilizavel & (anos == int(ano_teste))
        if not teste.any() or not ajuste.any():
            continue
        climatologia = climatologia_mes_hora(entrada.loc[ajuste, "data_hora"], alvo[ajuste])
        X_teste = features.loc[teste, features_pacote].astype(float)
        probabilidade = pacote["modelo"].predict_proba(X_teste)[:, list(pacote["modelo"].classes_).index(1)]
        y_teste = alvo[teste].to_numpy().astype(np.int64)
        p_pers = persistencia[teste].to_numpy(dtype=float)
        p_clim = aplicar_climatologia(entrada.loc[teste, "data_hora"], climatologia)
        metricas = _metricas_referencia(y_teste, probabilidade, p_pers, p_clim, limiar)
        do_ano = anos == int(ano_teste)
        metricas.update({
            "regiao": regiao,
            "media_anual_c": float(entrada.loc[do_ano, "temperatura_ar"].mean()),
            "vermelho_pct_ano": float(100.0 * np.mean(status.to_numpy()[do_ano] >= STATUS_VERMELHO)),
        })
        por_cultura[codigo] = metricas
        acumulado["alvo"].append(y_teste)
        acumulado["p"].append(probabilidade)
        acumulado["pers"].append(p_pers)
        acumulado["clim"].append(p_clim)
    if acumulado["alvo"]:
        agregado = _metricas_referencia(np.concatenate(acumulado["alvo"]), np.concatenate(acumulado["p"]),
                                        np.concatenate(acumulado["pers"]), np.concatenate(acumulado["clim"]), limiar)
    else:
        agregado = _metricas_referencia(np.array([], dtype=np.int64), np.array([]), np.array([]), np.array([]), limiar)
    agregado.update({"limiar": limiar, "ano_teste": int(ano_teste), "regra": modo_do_rotulador(rotulador),
                     "por_cultura": por_cultura})
    return agregado


# ---------------------------------------------------------------------------
# Complemento do prognóstico (fora do pacote de 9 chaves)
# ---------------------------------------------------------------------------
def _climatologia_combinada(
    df_limpo: pd.DataFrame, codigo: str, parametros: ParametrosCultura, rotulador: Rotulador, diretorio: Path,
) -> Dict[str, float]:
    """Climatologia mês × hora do alvo "Vermelho em 12 h" de UMA cultura, para o portão de segurança local.

    * Parte TÉRMICA: reanálise real da região da cultura (todos os anos), com
      o solo fixo no ideal — o clima de verdade, não o do simulador.
    * Parte do SOLO: dataset do simulador com a temperatura fixa no meio do
      ideal — a umidade do solo depende do manejo e da chuva do sítio, e a
      reanálise não a mede na mesma grandeza do higrômetro.
    * Combinação supondo independência: ``p = 1 − (1 − p_térmica)·(1 − p_solo)``.

    Sem região de referência (ou sem o arquivo dela), usa só o simulador com o rótulo completo.
    """
    dados = df_limpo.loc[df_limpo["cultura"].astype(str) == codigo].copy()
    dados["data_hora"] = pd.to_datetime(dados["data_hora"])
    dados = dados.sort_values("data_hora").drop_duplicates("data_hora", keep="last").reset_index(drop=True)
    faixa_t = parametros.faixa(VARIAVEL_TEMPERATURA_AR)

    def climatologia_de(entrada: pd.DataFrame) -> Dict[str, float]:
        """Climatologia do alvo (horas não vermelhas com futuro conhecido) de uma série rotulada."""
        status = rotulador(entrada.copy(), parametros).astype(float).reset_index(drop=True)
        alvo = calcular_alvo_risco_futuro(status, HORIZONTE_PROGNOSTICO_HORAS)
        filtro = (status < STATUS_VERMELHO).to_numpy() & alvo.notna().to_numpy()
        return climatologia_mes_hora(entrada.loc[filtro, "data_hora"].reset_index(drop=True),
                                     alvo[filtro].reset_index(drop=True))

    regiao = resolver_regiao(codigo)
    if regiao is None or dados.empty or not referencia_disponivel(regiao, diretorio):
        return climatologia_de(_entrada_rotulador(dados)) if not dados.empty else climatologia_mes_hora(
            pd.Series([], dtype="datetime64[ns]"), pd.Series([], dtype=float))
    termica = climatologia_de(_entrada_referencia(regiao, parametros, diretorio))
    so_solo = _entrada_rotulador(dados).assign(temperatura_ar=(faixa_t.ideal_min + faixa_t.ideal_max) / 2.0)
    solo = climatologia_de(so_solo)
    return {chave: float(1.0 - (1.0 - termica[chave]) * (1.0 - solo[chave])) for chave in termica}


def montar_complemento_prognostico(
    df_limpo: pd.DataFrame,
    parametros_por_cultura: Dict[str, ParametrosCultura],
    rotulador: Rotulador,
    versao: str,
    ano_teste: int = ANO_TESTE_REFERENCIA,
    diretorio: Path = DIRETORIO_REFERENCIA,
    geada_referencia: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Monta o complemento do prognóstico (``models/complemento_prognostico.json``).

    Chaves: ``versao`` (a do pacote), ``regra_temperatura`` (regra efetiva do
    rótulo), ``gerado_em``, ``climatologia_por_cultura`` ``{cultura: {'MM-HH':
    p}}`` (``_climatologia_combinada``), ``coef_tmin_por_regiao`` ``{regiao:
    [a, b, c]}`` e ``quantis_tmin_por_regiao`` ``{regiao: {'seca': q,
    'aguas': q}}``, ajustados nas noites ANTERIORES a ``ano_teste`` — os
    mesmos avaliados na seção 7. ``geada_referencia`` (saída de
    ``avaliar_geada_referencia``) evita refazer o ajuste; sem ela e sem os
    arquivos da referência, os dicionários por região ficam vazios (o
    prognóstico usa então os coeficientes do pacote e ``MARGEM_GEADA_C``).
    """
    if geada_referencia is not None:
        geada = geada_referencia
    elif referencia_disponivel(None, diretorio):
        geada = avaliar_geada_referencia(None, ano_teste, diretorio)
    else:
        geada = {}
    climatologias: Dict[str, Dict[str, float]] = {}
    for codigo in sorted(df_limpo["cultura"].astype(str).unique()):
        if codigo in parametros_por_cultura:
            climatologias[codigo] = _climatologia_combinada(df_limpo, codigo, parametros_por_cultura[codigo],
                                                            rotulador, diretorio)
    return {
        "versao": str(versao),
        "regra_temperatura": modo_do_rotulador(rotulador),
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "climatologia_por_cultura": climatologias,
        "coef_tmin_por_regiao": {regiao: [float(valor) for valor in dados["coef"]] for regiao, dados in geada.items()},
        "quantis_tmin_por_regiao": {regiao: {estacao: float(valor) for estacao, valor in dados["quantis"].items()}
                                    for regiao, dados in geada.items()},
    }


CHAVES_COMPLEMENTO: Tuple[str, ...] = (
    "versao", "regra_temperatura", "gerado_em", "climatologia_por_cultura", "coef_tmin_por_regiao",
    "quantis_tmin_por_regiao",
)


def salvar_complemento_prognostico(
    complemento: Dict[str, Any], caminho: Path = CAMINHO_COMPLEMENTO_PROGNOSTICO
) -> Path:
    """Grava o complemento em JSON (UTF-8, escrita atômica por arquivo temporário + ``replace``).

    Raises:
        ValueError: faltam chaves de ``CHAVES_COMPLEMENTO``.
    """
    faltando = [chave for chave in CHAVES_COMPLEMENTO if chave not in complemento]
    if faltando:
        raise ValueError(f"Complemento do prognóstico sem as chaves: {', '.join(faltando)}.")
    destino = Path(caminho)
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporario = destino.with_suffix(destino.suffix + ".tmp")
    temporario.write_text(json.dumps(complemento, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    temporario.replace(destino)
    registrador.info("Complemento do prognóstico salvo em %s (regra %s).", destino, complemento["regra_temperatura"])
    return destino


def montar_relatorio_referencia(avaliacao: Dict[str, Any], geada: Dict[str, Any]) -> str:
    """Texto da SEÇÃO 7 — Dado real (reanálise ERA5-Land), com rótulo de escopo e leitura honesta."""
    linha_dupla = "=" * LARGURA_RELATORIO
    linha_simples = "-" * LARGURA_RELATORIO
    ano = int(avaliacao["ano_teste"])
    partes: List[str] = ["", linha_dupla, TITULO_SECAO_REFERENCIA,
                         f"[escopo: reanálise ERA5-Land real (Open-Meteo/Copernicus, CC BY 4.0), pontos de grade de "
                         f"~11 km; teste fora da amostra em {ano}; solo fixo no ideal (rótulo só térmico); "
                         "não é medição do talhão]", linha_dupla]
    partes.append(f"Regra do rótulo: {avaliacao['regra']} | limiar do pacote: {_numero(avaliacao['limiar'])} | "
                  f"climatologia mês × hora ajustada nos anos < {ano}")
    rotulos_cultura = {"cafe_arabica": "Café Arábica", "cafe_conilon": "Café Conilon"}
    for codigo, m in avaliacao["por_cultura"].items():
        regiao = REGIOES_REFERENCIA[m["regiao"]]
        partes.append(linha_simples)
        partes.append(f"{rotulos_cultura.get(codigo, codigo)} — {m['regiao']} (lat {_decimal(regiao.latitude, 1)}, "
                      f"lon {_decimal(regiao.longitude, 1)}); média de {ano}: {_decimal(m['media_anual_c'], 1)} °C; "
                      f"Vermelho no ano: {_decimal(m['vermelho_pct_ano'], 2)}% das horas")
        partes.append(f"  Horas não vermelhas avaliadas: {int(m['horas'])} | viram Vermelho em 12 h: "
                      f"{int(m['positivos'])} ({_numero(100.0 * m['taxa_alvo'], 2)}%)")
        partes.append(f"  {'Abordagem':<26}{'AUC':>8}{'Brier':>9}{'Recall':>9}{'Falso Amarelo h/sem.':>23}")
        partes.append(f"  {'IA de prognóstico':<26}{_numero(m['auc']):>8}{_numero(m['brier'], 4):>9}"
                      f"{_numero(m['recall']):>9}{_numero(m['falso_amarelo_h_semana'], 1):>23}")
        partes.append(f"  {'Persistência':<26}{_numero(m['auc_persistencia']):>8}{_numero(m['brier_persistencia'], 4):>9}"
                      f"{_numero(m['recall_persistencia']):>9}{_numero(m['falso_amarelo_persistencia_h_semana'], 1):>23}")
        partes.append(f"  {'Climatologia mês × hora':<26}{_numero(m['auc_climatologia']):>8}"
                      f"{_numero(m['brier_climatologia'], 4):>9}{'—':>9}{'—':>23}")
        vence = math.isfinite(m["brier"]) and m["brier"] < m["brier_climatologia"]
        partes.append("  Brier da IA " + ("MENOR que o da climatologia: a antecipação agrega informação aqui."
                                          if vence else "NÃO é menor que o da climatologia: aqui a antecipação "
                                          "pelo modelo não se paga (o portão local a suspenderia)."))
        orcamento = ORCAMENTO_AMARELO_ANTECIPADO_HORAS_SEMANA
        if m["falso_amarelo_h_semana"] <= orcamento:
            partes.append(f"  Falso Amarelo antecipado: {_numero(m['falso_amarelo_h_semana'], 1)} h/semana — DENTRO do "
                          f"orçamento de {_decimal(orcamento, 1)} h/semana.")
        else:
            partes.append(f"  ATENÇÃO: falso Amarelo antecipado de {_numero(m['falso_amarelo_h_semana'], 1)} h/semana — "
                          f"ACIMA do orçamento de {_decimal(orcamento, 1)} h/semana neste clima real.")
    partes.append(linha_simples)
    partes.append(f"Aviso de geada em dado real ({ano}; alarme = previsão − margem ≤ "
                  f"{_decimal(LIMIAR_GEADA_ABRIGO_PADRAO_C + INCERTEZA_SENSOR_AR_PADRAO_C, 1)} °C, ou seja, geada no "
                  "abrigo < 2 °C + incerteza do DHT11):")
    nomes = {"regiao": "Coeficientes da região + q90", "simulador": "Coeficientes do simulador",
             "padrao": "COEF_TMIN_PADRAO", "persistencia": "Persistência (noite anterior)"}
    for regiao, dados in geada.items():
        a, b, c = dados["coef"]
        quantis = ", ".join(f"{estacao} {_decimal(valor, 2)} °C" for estacao, valor in dados["quantis"].items())
        cobertura = ", ".join(f"{estacao} {_numero(100.0 * valor, 1)}%" for estacao, valor in dados["cobertura_q90"].items())
        partes.append(f"  {regiao}: Tmin = {_numero(a)} + {_numero(b)}·T_tarde + {_numero(c)}·Td_tarde (ajuste < {ano}); "
                      f"margem q90: {quantis}")
        partes.append(f"    cobertura real do intervalo q90 em {ano}: {cobertura} | noites: {int(dados['noites_teste'])}"
                      f" | noites com mínima ≤ alarme: {int(dados['eventos'])} | menor mínima: "
                      f"{_numero(dados['minima_real_teste'], 1)} °C")
        partes.append(f"    {'Método':<32}{'MAE °C':>8}{'Viés °C':>9}{'Alarmes':>9}{'Falsos':>8}{'Acertos':>9}")
        for chave, rotulo in nomes.items():
            m = dados["metodos"][chave]
            partes.append(f"    {rotulo:<32}{_numero(m['mae'], 2):>8}{_numero(m['vies'], 2):>9}{int(m['alarmes']):>9}"
                          f"{int(m['alarmes_falsos']):>8}{int(m['eventos_avisados']):>9}")
    partes.append("")
    partes.append("Leitura honesta: a reanálise de ~11 km suaviza a geada radiativa do fundo de vale (fica mais quente que")
    partes.append("no talhão) e não tem o ruído do DHT11; serve para medir viés e erro dos métodos no clima real, não para")
    partes.append("contar geadas no sítio. O desempenho no sítio é medido em campo pelo portão de segurança local")
    partes.append("(src/desempenho_local.py), que suspende a antecipação pelo modelo se ela não vencer a climatologia.")
    partes.append(linha_dupla)
    return "\n".join(partes)


def _regra_padrao_treino(caminho_banco: Path = CAMINHO_BANCO) -> str:
    """Regra do rótulo quando ``--regra-temperatura`` não é informado.

    O valor de ``configuracao_sistema.regra_temperatura`` no banco, quando
    existe e é válido; senão ``'fisiologica'`` se o módulo
    ``regras_agronomicas`` existir, ou ``'instantanea'``.
    """
    caminho = Path(caminho_banco)
    if caminho.is_file():
        try:
            conexao = obter_conexao(caminho)
            try:
                linha = conexao.execute("SELECT valor FROM configuracao_sistema WHERE chave = 'regra_temperatura'"
                                        ).fetchone()
            finally:
                conexao.close()
            if linha is not None and str(linha[0]).strip().lower() in REGRAS_TEMPERATURA:
                return str(linha[0]).strip().lower()
        except Exception as erro:  # noqa: BLE001 — sem banco legível, decide pelo módulo
            registrador.warning("Não foi possível ler regra_temperatura em %s (%s).", caminho, erro)
    try:
        existe = importlib.util.find_spec(NOME_MODULO_REGRAS) is not None
    except (ImportError, ValueError):
        existe = False
    return REGRA_TEMPERATURA_FISIOLOGICA if existe else REGRA_TEMPERATURA_INSTANTANEA


def _criar_parser() -> argparse.ArgumentParser:
    """Argumentos da linha de comando do treinamento."""
    parser = argparse.ArgumentParser(description="Treina e avalia as IAs do AgroAnálise (instante e prognóstico).")
    parser.add_argument("--regra-temperatura", choices=list(REGRAS_TEMPERATURA), default=None,
                        help="Regra de temperatura do rótulo do prognóstico (padrão: a do banco; sem ela, "
                             "'fisiologica' se o módulo regras_agronomicas existir, senão 'instantanea').")
    return parser


# ---------------------------------------------------------------------------
# Orquestração
# ---------------------------------------------------------------------------
def _falsos_por_cultura(
    df: pd.DataFrame, y_teste: pd.Series, y_pred: List[int]
) -> Dict[str, Dict[str, int]]:
    """Erros operacionais do teste separados por cultura (usa o índice original do df)."""
    if "cultura" not in df.columns:
        return {}
    culturas_teste = df.loc[y_teste.index, "cultura"].astype(str).to_numpy()
    predito = np.asarray(y_pred, dtype=np.int64)
    real = np.asarray(y_teste, dtype=np.int64)
    return {
        cultura: calcular_falsos_operacionais(pd.Series(real[culturas_teste == cultura]),
                                              predito[culturas_teste == cultura])
        for cultura in sorted(set(culturas_teste))
    }


def main(argv: Optional[List[str]] = None) -> int:
    """Executa o treinamento completo e retorna o código de saída (0 = sucesso).

    Carrega o dataset → separa 80/20 → treina RandomForest e Regressão
    Logística → validação cruzada (5 dobras) → avalia no teste → escolhe o
    campeão → salva matriz de confusão e ``.pkl`` → atualiza ``versao_modelo``
    no banco → treina e avalia a IA de prognóstico de 12 h com o rótulo da
    regra de temperatura escolhida (divisão temporal, validação temporal,
    robustez a ruído, antecipação, calibração e geada) → avalia o pacote na
    reanálise ERA5-Land real (seção 7) → salva ``modelo_prognostico.pkl``
    (9 chaves; ``versao`` com o sufixo da regra), o complemento JSON, a curva
    de confiabilidade e o relatório com as seções de escopo.

    Args:
        argv: argumentos da linha de comando (``None`` = ``sys.argv[1:]``).
    """
    argumentos = _criar_parser().parse_args(argv)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        regra_pedida = argumentos.regra_temperatura or _regra_padrao_treino()
        rotulador = carregar_rotulador(regra_pedida)
        regra_efetiva = modo_do_rotulador(rotulador)
        registrador.info("Rótulo do prognóstico: regra de temperatura '%s' (pedida: '%s').", regra_efetiva,
                         regra_pedida)
        df = carregar_dataset()
        X_treino, X_teste, y_treino, y_teste = separar_treino_teste(df)
        registrador.info("Divisão estratificada: treino=%d, teste=%d.", len(X_treino), len(X_teste))

        modelos: Dict[str, Any] = {
            NOME_RANDOM_FOREST: treinar_random_forest(X_treino, y_treino),
            NOME_REGRESSAO_LOGISTICA: treinar_regressao_logistica(X_treino, y_treino),
        }
        resultados: Dict[str, Dict[str, Any]] = {}
        for nome, modelo in modelos.items():
            resultado = avaliar_modelo(nome, modelo, X_teste, y_teste)
            registrador.info("Validação cruzada de %s (%d dobras)...", nome, DOBRAS_VALIDACAO)
            resultado["validacao_cruzada"] = validacao_cruzada(modelo, X_treino, y_treino, dobras=DOBRAS_VALIDACAO)
            resultado["n_treino"] = int(len(X_treino))
            resultado["falsos_por_cultura"] = _falsos_por_cultura(df, y_teste, resultado["y_pred"])
            resultados[nome] = resultado

        campeao = selecionar_campeao(resultados)
        caminho_grafico = gerar_matriz_confusao(resultados, campeao)
        relatorio = montar_relatorio(resultados, campeao)

        culturas_treino = sorted(df["cultura"].astype(str).unique().tolist()) if "cultura" in df.columns else None
        caminho_modelo = salvar_modelo(resultados[campeao]["modelo"], campeao, resultados[campeao],
                                       culturas=culturas_treino)
        versao = str(joblib.load(caminho_modelo)["versao"])
        definir_configuracao("versao_modelo", versao)
        registrador.info("configuracao_sistema.versao_modelo atualizada para %s.", versao)

        relatorio += (f"\nCulturas cobertas pelo modelo: {', '.join(culturas_treino or []) or 'não informadas'}"
                      "\n  (cultura cadastrada depois deste treino usa o motor de regras até um novo treino)\n")

        registrador.info("Treinando e avaliando a IA de prognóstico de %d h...", HORIZONTE_PROGNOSTICO_HORAS)
        parametros_culturas = _parametros_das_culturas(df)
        pacote_prognostico, avaliacao = avaliar_prognostico_completo(df, parametros_culturas, rotulador=rotulador)
        pacote_prognostico["versao"] = f"{pacote_prognostico['versao']}-{regra_efetiva}"
        caminho_prognostico = salvar_modelo_prognostico(pacote_prognostico)
        caminho_curva = gerar_grafico_confiabilidade({
            "calibrado": avaliacao["calibracao"]["curva_calibrado"],
            "nao_calibrado": avaliacao["calibracao"]["curva_nao_calibrado"],
        })
        relatorio += montar_relatorio_prognostico(avaliacao, caminho_curva)

        if referencia_disponivel():
            registrador.info("Avaliando o prognóstico na reanálise ERA5-Land (dado real)...")
            geada_referencia = avaliar_geada_referencia(tuple(pacote_prognostico["coef_tmin"]))
            avaliacao_referencia = avaliar_em_referencia(pacote_prognostico, parametros_culturas, rotulador)
            relatorio += montar_relatorio_referencia(avaliacao_referencia, geada_referencia)
        else:
            registrador.warning("Referência climática ausente em %s: seção 7 sem avaliação em dado real.",
                                DIRETORIO_REFERENCIA)
            geada_referencia = {}
            relatorio += "\n".join(["", "=" * LARGURA_RELATORIO, TITULO_SECAO_REFERENCIA,
                                     "[escopo: reanálise ERA5-Land — arquivos AUSENTES nesta instalação]",
                                     "=" * LARGURA_RELATORIO,
                                     f"Os arquivos de {DIRETORIO_REFERENCIA} não foram encontrados: não há avaliação em "
                                     "dado real neste treino, e o aviso de geada usa os coeficientes do pacote.",
                                     "=" * LARGURA_RELATORIO])
        complemento = montar_complemento_prognostico(df, parametros_culturas, rotulador, pacote_prognostico["versao"],
                                                     geada_referencia=geada_referencia)
        caminho_complemento = salvar_complemento_prognostico(complemento)
        if regra_efetiva != regra_pedida:
            relatorio += (f"\nAVISO: foi pedida a regra '{regra_pedida}', mas o módulo {NOME_MODULO_REGRAS} não está "
                          f"disponível; o prognóstico foi rotulado com a regra '{regra_efetiva}'.\n")
        relatorio += (f"\nArquivos gerados:\n  Modelo campeão (instante) .. {caminho_modelo}  (versão {versao})"
                      f"\n  Modelo de prognóstico ..... {caminho_prognostico}  (versão {pacote_prognostico['versao']})"
                      f"\n  Complemento (JSON) ........ {caminho_complemento}"
                      f"\n  Matriz de confusão ........ {caminho_grafico}"
                      f"\n  Curva de confiabilidade ... {caminho_curva}"
                      f"\n  Relatório ................. {CAMINHO_RELATORIO_TREINO}\n")
        Path(CAMINHO_RELATORIO_TREINO).parent.mkdir(parents=True, exist_ok=True)
        Path(CAMINHO_RELATORIO_TREINO).write_text(relatorio, encoding="utf-8")
        print(relatorio)
        return 0
    except Exception as erro:  # noqa: BLE001 — ponto de entrada: registra e devolve código de erro
        registrador.exception("Falha no treinamento da IA: %s", erro)
        return 1


if __name__ == "__main__":
    sys.exit(main())
