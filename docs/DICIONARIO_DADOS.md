# Dicionário de Dados — AgroAnálise v4

> Fonte: esquema **real** lido do banco `data/agroanalise.db` gerado do zero (v2; conferido de novo na v3 por `PRAGMA`
> num banco criado do zero com todos os módulos — ver [seção 20.8](#208-esquema-real-conferido-por-pragma-v3)) (`sqlite_master`,
> `PRAGMA table_info`, `PRAGMA foreign_key_list`, `PRAGMA index_list`), do DDL em `src/database.py`
> (`_DDL_ESQUEMA`, `_COLUNAS_MIGRACAO_HISTORICO`, `_DDL_MIGRACAO`) e em `src/prognostico.py` (`_DDL_ANALISE`).
> Os exemplos são valores reais do banco, dos CSVs, dos dois `.pkl` e da API, gerados pelo pipeline com semente 42
> (execução de 14/11/2026). Diagrama ER: [`diagramas/der.mmd`](diagramas/der.mmd) ([SVG](diagramas/der.svg)).
>
> **O que mudou na v2:** colunas `estado_sensor`, `diagnostico_sensor` e `instante_utc` em `historico_leituras`;
> tabelas novas `analise_antecipacao` (prognóstico de 12 h) e `historico_agregado_hora` (retenção); 17 chaves novas
> semeadas em `configuracao_sistema` + 4 gravadas sob demanda pela interface; índices por `id_leitura`; dataset de
> 8.760 h por cultura; pacote novo `models/modelo_prognostico.pkl`; 13 campos novos em `/api/status` e o bloco
> `componentes` em `/api/saude`.
>
> **O que mudou na v3** ([seção 20](#20-mudanças-da-v3)): 4 colunas novas em `historico_leituras` (`motivo_regra`,
> `base_temperatura`, `temperatura_media_24h`, `umidade_solo_firmware`); 4 tabelas novas criadas pelos próprios
> módulos (`registro_acoes`, `eventos_interface`, `pedido_ajuda`, `calibracao_sonda`) — o banco passa a ter **10
> tabelas da aplicação** + `sqlite_sequence`; 24 chaves novas em `configuracao_sistema` (`semear_configuracoes_v3`);
> `models/complemento_prognostico.json`; `referencia_climatica/` (ERA5-Land); linha de perfil do firmware; campos e
> rotas novas na API. **Nenhuma linha de `parametros_cultura` mudou**: a faixa do cliente continua a mesma, só a forma
> de aplicá-la à temperatura mudou (seção 20.1). As fontes de cada número estão em
> [PESQUISA_REFERENCIAS.md](PESQUISA_REFERENCIAS.md).
>
> **O que mudou na v4** ([seção 21](#21-mudanças-da-v4)): coluna `demonstracao` (migração idempotente) em
> `pedido_ajuda` e `eventos_interface`; 2 campos novos em `/api/status` (`fala_curta`, `aparelho_da_banca`; 53 no
> total); rotas `POST /api/acao_feita/desfazer`, `GET /static/agro.<hash>.css` e `GET /favicon.svg`; códigos de erro do
> PIN; chaves novas no navegador (`agro_voz_lenta`, `agro_feitos`); gabarito `tests/dados/pictogramas_iso9186.csv` e
> metas `tests/e2e/validacao/metas_v4.json`. **Nenhuma tabela, chave de configuração ou linha de
> `parametros_cultura` nova**: as regras e os números do cliente não mudaram.

## Sumário

1. [Visão geral e convenções](#1-visão-geral-e-convenções)
2. [Tabela `culturas`](#2-tabela-culturas)
3. [Tabela `parametros_cultura`](#3-tabela-parametros_cultura)
4. [Tabela `historico_leituras`](#4-tabela-historico_leituras)
5. [Tabela `analise_antecipacao` (nova)](#5-tabela-analise_antecipacao-nova)
6. [Tabela `historico_agregado_hora` (nova)](#6-tabela-historico_agregado_hora-nova)
7. [Tabela `configuracao_sistema`](#7-tabela-configuracao_sistema)
8. [Tabela interna `sqlite_sequence`](#8-tabela-interna-sqlite_sequence)
9. [Índices, chaves, integridade e migração](#9-índices-chaves-integridade-e-migração)
10. [Domínios compartilhados (enumerações)](#10-domínios-compartilhados-enumerações)
11. [CSV `data/dataset_bruto.csv`](#11-csv-datadataset_brutocsv)
12. [CSV `data/dataset_limpo.csv`](#12-csv-datadataset_limpocsv)
13. [Pacote `models/modelo_agroanalise.pkl` (IA do instante)](#13-pacote-modelsmodelo_agroanalisepkl-ia-do-instante)
14. [Pacote `models/modelo_prognostico.pkl` (IA de prognóstico, novo)](#14-pacote-modelsmodelo_prognosticopkl-ia-de-prognóstico-novo)
15. [Protocolo serial v2](#15-protocolo-serial-v2)
16. [Contrato JSON de `GET /api/status`](#16-contrato-json-de-get-apistatus)
17. [Contrato JSON de `GET /api/saude`](#17-contrato-json-de-get-apisaude)
18. [Armazenamento no navegador e arquivos de operação](#18-armazenamento-no-navegador-e-arquivos-de-operação)
19. [Consultas úteis](#19-consultas-úteis)
20. [Mudanças da v3](#20-mudanças-da-v3)
21. [Mudanças da v4](#21-mudanças-da-v4)

---

## 1. Visão geral e convenções

| Item | Valor |
|---|---|
| SGBD | SQLite 3 (arquivo único `data/agroanalise.db`; outro arquivo via `--banco` ou `AGRO_BANCO`) |
| Modo de journal | `WAL` (leitura do Flask simultânea à escrita do coletor) |
| Espaço livre | `PRAGMA auto_vacuum = INCREMENTAL` em bancos novos (antes do DDL); `aplicar_retencao` termina com `wal_checkpoint(TRUNCATE)` + `incremental_vacuum`; `python src/database.py --compactar` faz `VACUUM` manual |
| Conexão | `database.obter_conexao`: `sqlite3.connect(..., timeout=10, check_same_thread=False)`, `row_factory = sqlite3.Row`, `PRAGMA busy_timeout = 10000`, `PRAGMA foreign_keys = ON` (é o que faz o `ON DELETE CASCADE` de `analise_antecipacao` funcionar), `PRAGMA journal_mode = WAL` |
| Criação e migração | `database.inicializar_banco()` — idempotente (`CREATE ... IF NOT EXISTS`, sementes com `INSERT OR IGNORE`) e chama `migrar_esquema()`, que acrescenta as colunas v2 a bancos v1 (`PRAGMA table_info` + `ALTER TABLE ADD COLUMN`) e preenche `instante_utc` das linhas antigas. `analise_antecipacao` é criada sob demanda por `prognostico.gravar_analise_antecipacao` |
| Datas | `data_hora`: texto `YYYY-MM-DD HH:MM:SS` no horário **local** (compatível com a v1). `instante_utc` e as chaves `*_em` novas: ISO-8601 **UTC com `+00:00`** (ex.: `2026-11-14T14:19:17+00:00`), imunes a fuso e a horário de verão |
| Ordem oficial do histórico | `id_leitura` (ordem de gravação). `buscar_historico` e `buscar_ultima_leitura` usam `ORDER BY h.id_leitura DESC`, para que um salto do relógio não "congele" o semáforo numa leitura com data no futuro |
| Unidades | Temperatura em °C; umidades em % (0–100); features em °C ou pontos percentuais (p.p.); probabilidades 0–1; prazos em horas |
| Nulos | Só `*_bruta`, `nome_cientifico`, `descricao`, `fonte_referencia`, `instante_utc`, as colunas numéricas de `analise_antecipacao` e as estatísticas de `historico_agregado_hora` aceitam `NULL` |
| Escalabilidade | Nova cultura = 1 linha em `culturas` + N linhas em `parametros_cultura` (**sem ALTER TABLE e sem mudar código**). As features do prognóstico são todas relativas às faixas da cultura |

Legenda das colunas "Chave": **PK** chave primária · **FK** chave estrangeira · **UK** unicidade.

---

## 2. Tabela `culturas`

Catálogo dos vegetais, frutas e grãos monitorados. Cada linha é uma cultura; o `codigo` (slug) é o
identificador usado em toda a aplicação (`cultura_ativa`, CSVs, API).

| Coluna | Tipo SQLite | Nulo? | Chave | Restrição / padrão | Domínio / unidade | Descrição | Exemplo |
|---|---|---|---|---|---|---|---|
| `id_cultura` | INTEGER | não | **PK** | `AUTOINCREMENT` | inteiro ≥ 1 | Identificador interno | `1` |
| `codigo` | TEXT | não | **UK** | `UNIQUE`; validado por `cadastrar_cultura` com `^[a-z][a-z0-9_]{1,49}$` | slug minúsculo, sem acento | Código estável da cultura | `cafe_arabica` |
| `nome_comum` | TEXT | não | | | texto livre | Nome falado ao agricultor (título e voz) | `Café Arábica` |
| `nome_cientifico` | TEXT | sim | | | binomial | Nome botânico | `Coffea arabica` |
| `categoria` | TEXT | não | | `CHECK (categoria IN ('cafe','fruta','grao','hortalica','leguminosa','outra'))` | enumeração | Define o ícone do botão (☕ 🍎 🌾 🥬 🫘 🌱) e a forma do título | `cafe` |
| `descricao` | TEXT | sim | | | texto livre | Observação agronômica | `Café de altitude e clima ameno; sensível a calor excessivo e a geadas.` |
| `ativo` | INTEGER | não | | `DEFAULT 1`, `CHECK (ativo IN (0, 1))` | 0/1 | Só culturas ativas aparecem na tela e entram no dataset | `1` |
| `criado_em` | TEXT | não | | `DEFAULT (datetime('now','localtime'))` | ISO local | Data de cadastro | `2026-11-14 01:13:15` |

**Valores semeados** (`popular_parametros_iniciais`):

| id_cultura | codigo | nome_comum | nome_cientifico | categoria | descricao |
|---|---|---|---|---|---|
| 1 | `cafe_arabica` | Café Arábica | Coffea arabica | cafe | Café de altitude e clima ameno; sensível a calor excessivo e a geadas. |
| 2 | `cafe_conilon` | Café Conilon | Coffea canephora | cafe | Café robusta de regiões baixas e quentes; mais tolerante ao calor, sensível ao frio. |

---

## 3. Tabela `parametros_cultura`

Limites agronômicos em **formato longo**: uma linha por cultura × variável. É a "régua" do semáforo
e a base das features da IA. Aceita variáveis novas (pH, luminosidade...) sem alterar o esquema.

| Coluna | Tipo SQLite | Nulo? | Chave | Restrição / padrão | Domínio / unidade | Descrição | Exemplo |
|---|---|---|---|---|---|---|---|
| `id_parametro` | INTEGER | não | **PK** | `AUTOINCREMENT` | inteiro ≥ 1 | Identificador da faixa | `2` |
| `id_cultura` | INTEGER | não | **FK**, **UK**(composta) | `REFERENCES culturas(id_cultura) ON DELETE CASCADE`; `UNIQUE (id_cultura, variavel)`; índice `idx_parametros_cultura_id` | id existente | Cultura dona da faixa | `1` |
| `variavel` | TEXT | não | **UK**(composta) | slug `^[a-z][a-z0-9_]{1,49}$` (validado em `validar_faixa`) | `temperatura_ar`, `umidade_ar`, `umidade_solo` (obrigatórias) ou nova variável | Grandeza medida | `umidade_solo` |
| `unidade` | TEXT | não | | não vazia (validada) | `°C`, `%`, `pH`... | Unidade da variável | `%` |
| `ideal_min` | REAL | não | | ver CHECK de ordem | unidade da variável | Início da faixa **VERDE** | `60.0` |
| `ideal_max` | REAL | não | | `ideal_min < ideal_max` | idem | Fim da faixa **VERDE** | `80.0` |
| `atencao_min` | REAL | não | | `atencao_min <= ideal_min` | idem | Borda inferior da faixa **AMARELA**; abaixo dela é **VERMELHO** | `50.0` |
| `atencao_max` | REAL | não | | `ideal_max <= atencao_max` | idem | Borda superior da faixa **AMARELA**; acima dela é **VERMELHO** | `88.0` |
| `critico_min` | REAL | não | | `critico_min <= atencao_min` | idem | Dano fisiológico severo abaixo (geada, murcha permanente): mensagem começa com "Urgente!" | `35.0` |
| `critico_max` | REAL | não | | `atencao_max <= critico_max` | idem | Dano severo acima (escaldadura, asfixia radicular): "Urgente!" | `95.0` |
| `fonte_referencia` | TEXT | sim | | | texto | Origem técnica da faixa; `cadastrar_cultura` grava `Cadastro via cadastrar_cultura` | `Embrapa Café / Incaper` |
| `atualizado_em` | TEXT | não | | `DEFAULT (datetime('now','localtime'))`; renovado no upsert | ISO local | Última alteração | `2026-11-14 01:13:15` |

Restrição de tabela (CHECK de ordem, garantida pelo SQLite **e** por `database.validar_faixa`):

```
critico_min <= atencao_min <= ideal_min < ideal_max <= atencao_max <= critico_max
```

Validações adicionais da aplicação (`validar_faixa`, só em `cadastrar_cultura`):
- a faixa ideal das variáveis de sensor precisa caber na faixa física (temperatura 0–50 °C; umidades 0–100 %);
- para `temperatura_ar` e `umidade_solo` (as que definem o semáforo), `atencao_min` e `atencao_max` precisam ficar
  **estritamente** dentro da faixa física, senão o Vermelho daquela borda nunca poderia ser medido.

**Semântica de classificação** de UMA variável (`gerador_dataset.classificar_variavel`):

| Condição | Classe |
|---|---|
| `ideal_min <= v <= ideal_max` | 0 — Verde (Ideal) |
| `atencao_min <= v < ideal_min` ou `ideal_max < v <= atencao_max` | 1 — Amarelo (Atenção) |
| `v < atencao_min` ou `v > atencao_max` | 2 — Vermelho (Risco Crítico) |

`status_regra = max(classe(temperatura_ar), classe(umidade_solo))`. A faixa de `umidade_ar` é usada pelo simulador,
pelos cenários e como feature do modelo, mas **não** entra no motor de regras (decisão do contrato técnico).

**Valores semeados** (6 linhas, exatamente as do contrato):

| id | cultura | variável | unidade | ideal | atenção | crítico | fonte |
|---|---|---|---|---|---|---|---|
| 1 | cafe_arabica | temperatura_ar | °C | 18–22 | 15–25 | 8–32 | Embrapa Café / Incaper |
| 2 | cafe_arabica | umidade_solo | % | 60–80 | 50–88 | 35–95 | Embrapa Café / Incaper |
| 3 | cafe_arabica | umidade_ar | % | 60–80 | 50–90 | 35–98 | Embrapa Café / Incaper |
| 4 | cafe_conilon | temperatura_ar | °C | 22–26 | 19–29 | 12–34 | Incaper / Embrapa Rondônia |
| 5 | cafe_conilon | umidade_solo | % | 55–75 | 45–83 | 30–95 | Incaper / Embrapa Rondônia |
| 6 | cafe_conilon | umidade_ar | % | 60–85 | 50–90 | 35–98 | Incaper / Embrapa Rondônia |

As bordas de atenção têm o **mesmo deslocamento relativo** ao ideal nas duas culturas (temperatura ±3 °C; solo −10/+8 p.p.),
para que as features relativas à faixa sejam comparáveis entre culturas no mesmo modelo.

Cardinalidade: o esquema permite 0..N faixas por cultura; a aplicação exige ao menos as 3 obrigatórias
(`cadastrar_cultura` recusa menos, e `buscar_parametros_cultura` levanta `ValueError` para cultura sem faixa).

---

## 4. Tabela `historico_leituras`

Log append-only de cada leitura processada: valores brutos, valores saneados (e filtrados pela mediana no campo),
features, decisão do motor de regras, decisão da IA, status final, anomalia, estado do sensor, instante UTC e a frase
falada. Gravada por `database.inserir_leitura` / `inserir_leituras_em_lote` (chamadas por
`coletor_serial.gravar_registro_leitura`).

| Coluna | Tipo SQLite | Nulo? | Chave | Restrição / padrão | Domínio / unidade | Descrição | Exemplo |
|---|---|---|---|---|---|---|---|
| `id_leitura` | INTEGER | não | **PK** | `AUTOINCREMENT`; índices `idx_historico_cultura_id`, `idx_historico_cultura_origem_id` | inteiro ≥ 1 | Identificador **e ordem oficial** de gravação | `3` |
| `data_hora` | TEXT | não | | não vazio; índices `idx_historico_data_hora`, `idx_historico_cultura_data` | `YYYY-MM-DD HH:MM:SS` local | Momento do processamento (relógio de parede do computador). Na fila de gravação, mantém a hora original | `2026-11-14 11:19:17` |
| `id_cultura` | INTEGER | não | **FK** | `REFERENCES culturas(id_cultura)` (sem cascade) | id existente | Cultura monitorada no momento | `1` |
| `origem` | TEXT | não | | `CHECK (origem IN ('CAMPO_USB','SIMULACAO','DEMONSTRACAO','DATASET'))` | enumeração | De onde veio a leitura | `SIMULACAO` |
| `temperatura_ar_bruta` | REAL | **sim** | | NaN/`nan` do firmware/`None` → `NULL` | °C | Valor como chegou do sensor/simulador | `21.2` |
| `umidade_ar_bruta` | REAL | **sim** | | idem | % | Valor como chegou | `66.4` |
| `umidade_solo_bruta` | REAL | **sim** | | idem | % | Valor como chegou (ex.: `0.0` de um espeto solto) | `66.6` |
| `temperatura_ar` | REAL | não | | finito | °C, 0–50 | Valor **saneado** (`SaneadorTempoReal`) e, no campo, **filtrado** pela mediana de 12 leituras (`FiltroMedianaMovel`; valor além do crítico passa direto) | `21.2` |
| `umidade_ar` | REAL | não | | finito | %, 0–100 | Saneado/filtrado | `66.4` |
| `umidade_solo` | REAL | não | | finito | %, 0–100 | Saneado/filtrado | `66.6` |
| `deficit_hidrico_solo` | REAL | não | | finito | p.p.; > 0 falta de água, < 0 excesso | Feature 1 (`calcular_features`) | `0.0` |
| `desvio_termico` | REAL | não | | finito | °C; > 0 calor, < 0 frio | Feature 2 | `0.0` |
| `status_regra` | INTEGER | não | | `CHECK (status_regra IN (0, 1, 2))` | 0/1/2 | Motor de regras agronômicas (nunca alterado pela antecipação) | `0` |
| `status_predito` | INTEGER | não | | `CHECK (status_predito IN (0, 1, 2))` | 0/1/2 | IA do instante; a antecipação pode elevá-lo para `max(·, 1)` | `0` |
| `status_alerta` | INTEGER | não | | `CHECK (status_alerta IN (0, 1, 2))` | 0/1/2 | **Cor gravada** = `max(status_regra, status_predito)`; antecipação: `max(·, 1)`; sensor suspeito: `max(1, variável confiável)` | `0` |
| `probabilidade_predicao` | REAL | não | | `CHECK (0.0 <= probabilidade_predicao <= 1.0)` | 0–1 | Probabilidade da classe predita pela IA do instante; `1.0` com `REGRAS` | `1.0` |
| `flag_anomalia` | INTEGER | não | | `DEFAULT 0`, `CHECK (0, 1)` | 0/1 | 1 se algum valor foi corrigido pelo saneador | `0` |
| `descricao_anomalia` | TEXT | não | | `DEFAULT ''` | `variavel:tipo` separados por `;`, tipo ∈ {`nan`, `negativo`, `pico`} | Quais valores foram corrigidos | `temperatura_ar:pico` |
| `mensagem_voz` | TEXT | não | | não vazio | pt-BR sem símbolos | Orientação falada; pode trazer a frase de antecipação (no máximo 4 frases) ou `MENSAGEM_SENSOR_SOLO`/`MENSAGEM_SENSOR_AR` | `Tudo bem com a plantação de café arábica. ...` |
| `versao_modelo` | TEXT | não | | não vazio | `<NomeModelo>-<AAAAMMDDHHMMSS>` ou `REGRAS` | Qual IA do instante decidiu | `RandomForestClassifier-20261114111842` |
| `motivo_regra` | TEXT | não | | **v3** `DEFAULT ''` | `NENHUM`, `GEADA_AGORA`, `CALOR_FLORADA`, `CALOR_SEGUIDO`, `CALOR_DIA`, `DIA_FRIO`, `DIA_QUENTE`, `SECA_AGORA`, `SECA_PROGRAMADA`, `ENCHARCAMENTO` ou `''` | Motivo do pior componente da regra (seção 20.2) | `DIA_FRIO` |
| `base_temperatura` | TEXT | não | | **v3** `DEFAULT ''` | `instantanea`, `media_24h` ou `''` | Em que valor o conforto térmico foi julgado | `media_24h` |
| `temperatura_media_24h` | REAL | **sim** | | **v3** | °C | Média de 24 h usada pela regra fisiológica | `16.8` |
| `umidade_solo_firmware` | REAL | **sim** | | **v3** | % do firmware | Valor do Arduino antes da calibração da sonda (só `CAMPO_USB`) | `70.0` |
| `estado_sensor` | TEXT | não | | **v2** `DEFAULT 'OK'`, `CHECK (estado_sensor IN ('OK','SUSPEITO','DEFEITO'))` | enumeração | Pior estado entre o sensor da terra e o do ar (`EstadoSensores.estado_geral`) | `OK` |
| `diagnostico_sensor` | TEXT | não | | **v2** `DEFAULT ''` | texto livre | Motivo do estado (ex.: solo idêntico por 60 min, `AVISO:HIGROMETRO_DESCONECTADO`, DHT sem resposta) | `''` |
| `instante_utc` | TEXT | **sim** | | **v2**; preenchido por `inserir_leitura` a partir de `data_hora` quando ausente; linhas v1 preenchidas na migração com `strftime('%Y-%m-%dT%H:%M:%S+00:00', data_hora, 'utc')` | ISO-8601 UTC `+00:00` | Instante absoluto da leitura; usado por `app._idade_segundos` (relógio inconsistente quando a leitura vem mais de 120 s "do futuro") | `2026-11-14T11:19:17+00:00` |

Ordem física: num banco criado pela v3 as quatro colunas v3 ficam **antes** de `estado_sensor` (ordem acima, conferida
por `PRAGMA table_info`); num banco v2 migrado elas entram no fim. O código lê por nome, então a ordem não importa.

Regras de negócio da tabela:
- `status_alerta >= status_regra` sempre; a antecipação **nunca** leva para 2 o que era < 2 e nunca rebaixa a cor.
- Com `estado_sensor` ≠ `OK`, a cor nunca é Vermelho **por causa** da variável suspeita (a outra variável, confiável, ainda pode dar 2).
- Leituras `DEMONSTRACAO` ficam no histórico (auditoria da banca) por 1 dia; o Flask não as exibe fora do Modo Demonstração e a retenção as apaga sem agregar.
- A gravação aceita `configuracoes_extras` na **mesma transação** (batimento do coletor); com o banco travado, o coletor guarda até 720 leituras numa fila e grava em lote (`inserir_leituras_em_lote`, uma transação) no ciclo seguinte, com as `data_hora` originais.

---

## 5. Tabela `analise_antecipacao` (nova)

Resultado do prognóstico de 12 h de **uma** leitura de campo (relação 1:0..1 com `historico_leituras`). Criada sob
demanda por `prognostico.gravar_analise_antecipacao` (`CREATE TABLE IF NOT EXISTS` + `INSERT OR REPLACE`) e lida por
`prognostico.obter_analise_antecipacao` (devolve `None` se a tabela ou a linha não existirem). Só existe linha quando
`aplicar_antecipacao` produziu um resultado: origem `CAMPO_USB`, sensores `OK` e pelo menos 18 das últimas 24 h com
dados de campo.

| Coluna | Tipo SQLite | Nulo? | Chave | Restrição / padrão | Domínio / unidade | Descrição | Exemplo real |
|---|---|---|---|---|---|---|---|
| `id_leitura` | INTEGER | não | **PK**, **FK** | `REFERENCES historico_leituras(id_leitura) ON DELETE CASCADE` | id existente | Leitura analisada; apagar a leitura (retenção) apaga a análise | `25` |
| `probabilidade_risco_futuro` | REAL | sim | | arredondada a 3 casas | 0–1 | P(Vermelho nas próximas 12 h) do modelo calibrado; `NULL` sem modelo ou sem features | `0.648` |
| `horas_ate_secar` | REAL | sim | | teto 72 h | horas | ETA até `atencao_min` do solo pela perda líquida das últimas 24 h; `NULL` se houve subida > 1 pp/h na janela, perda ≤ 0,02 pp/h ou ETA > 72 h | `1.0` |
| `temperatura_minima_prevista` | REAL | sim | | | °C | Mínima prevista da madrugada (`a + b·T_tarde + c·Td_tarde`); só entre 17 h e 6 h | `NULL` |
| `motivo_principal` | TEXT | não | | `CHECK (motivo_principal IN ('SECA_CHEGANDO','SECA_AGORA','ENCHARCAMENTO','CALOR_TARDE','GEADA_NOITE','FRIO','NENHUM'))` | `prognostico.MOTIVOS` | Por que o aviso (contrafactual determinístico quando o Amarelo vem do modelo) | `SECA_CHEGANDO` |
| `prazo_horas` | REAL | sim | | | horas | Quando o problema deve chegar (dito por `falar_prazo`: "daqui a pouco", "amanhã de manhã"...) | `1.0` |
| `horas_em_estresse` | REAL | sim | | | horas | Horas seguidas fora da faixa ideal no fim da série ("a terra está seca há 9 horas") | `9.0` |
| `status_antecipado` | INTEGER | não | | `CHECK (status_antecipado IN (0, 1))` | 0/1 | 1 quando a antecipação elevou (ou manteria elevada) a cor para Amarelo | `1` |
| `versao_prognostico` | TEXT | não | | | versão do pacote ou `SEM_PROGNOSTICO` | Rastreabilidade do modelo de prognóstico | `Prognostico12h-RandomForestCalibrado-20261114111851` |
| `criado_em` | TEXT | não | | `DEFAULT (datetime('now','localtime'))` | ISO local | Momento da gravação | — |

Exemplo real (banco temporário com 24 h de leituras `CAMPO_USB` do Arábica secando 1 p.p./h, depois uma leitura a 51 %
processada por `processar_leitura(..., modulo_antecipacao=prognostico)` com os dois modelos publicados): a linha acima,
com a voz "Atenção com a plantação de café arábica. A terra está seca há 9 horas e deve ficar muito seca daqui a pouco.
Regue agora, devagar, até a terra ficar úmida."

---

## 6. Tabela `historico_agregado_hora` (nova)

Memória de longo prazo do histórico. `database.aplicar_retencao(dias_brutos)` (chamada pelo coletor 1 vez por hora,
pela linha de comando `--reter DIAS` e, em emergência de disco, com 7 dias) agrega as **horas completas** mais antigas
que o corte com `INSERT ... SELECT ... GROUP BY` (upsert que **soma** se a hora já existir), apaga as brutas em lotes de
5.000 por transação, apaga leituras `DEMONSTRACAO` com mais de 1 dia **sem** agregar e termina com
`wal_checkpoint(TRUNCATE)` + `incremental_vacuum`.

| Coluna | Tipo SQLite | Nulo? | Chave | Descrição |
|---|---|---|---|---|
| `id_cultura` | INTEGER | não | **PK**(composta), **FK** `REFERENCES culturas(id_cultura)` | Cultura |
| `hora` | TEXT | não | **PK**(composta) | Início da hora, `YYYY-MM-DD HH:00:00` local |
| `origem` | TEXT | não | **PK**(composta) | `CAMPO_USB`, `SIMULACAO` ou `DATASET` (demonstração não é agregada) |
| `n` | INTEGER | não | | Quantidade de leituras brutas agregadas (SUM(n) = linhas apagadas, verificado nos testes) |
| `temperatura_min` / `temperatura_med` / `temperatura_max` | REAL | sim | | Mínimo, média e máximo da temperatura saneada |
| `umidade_ar_min` / `umidade_ar_med` / `umidade_ar_max` | REAL | sim | | Idem para a umidade do ar |
| `umidade_solo_min` / `umidade_solo_med` / `umidade_solo_max` | REAL | sim | | Idem para a umidade do solo |
| `status_alerta_max` | INTEGER | sim | | Pior cor da hora (igual ao `MAX(status_alerta)` das brutas) |
| `n_verde` / `n_amarelo` / `n_vermelho` | INTEGER | sim | | Contagem por cor |
| `n_anomalias` | INTEGER | sim | | Leituras com `flag_anomalia = 1` |

Volume: a 5 s por leitura são 17.280 linhas/dia; com `retencao_dias_brutos = 30` o banco guarda ≈ 518 mil linhas
brutas e, depois disso, 24 linhas por dia por cultura e origem.

---

## 7. Tabela `configuracao_sistema`

Chave–valor **tipado**: estado compartilhado entre o coletor e o Flask (o "quadro de avisos" da máquina de estados) e,
na v2, o batimento do coletor e os contadores de saúde.

| Coluna | Tipo SQLite | Nulo? | Chave | Restrição / padrão | Descrição | Exemplo |
|---|---|---|---|---|---|---|
| `chave` | TEXT | não¹ | **PK** | `PRIMARY KEY` | Nome da configuração | `modo_operacao` |
| `valor` | TEXT | não | | serializado por `_serializar_valor_configuracao` | Valor atual; bool = `true`/`false` | `SIMULACAO` |
| `tipo` | TEXT | não | | `CHECK (tipo IN ('str','int','float','bool'))` | Conversão em `obter_configuracao_tipada`; chave nova recebe o tipo inferido do valor Python | `str` |
| `descricao` | TEXT | não | | | Explicação da chave | `Modo operante atual: ...` |
| `atualizado_em` | TEXT | não | | `DEFAULT (datetime('now','localtime'))`; renovado na escrita | Última escrita | `2026-11-14 11:19:17` |

¹ A aplicação nunca grava chave vazia (`definir_configuracao` levanta `ValueError`).

**Chaves da v1** (12, semeadas por `popular_configuracao_inicial`, `INSERT OR IGNORE`):

| chave | valor inicial | tipo | Quem escreve | Quem lê | Descrição |
|---|---|---|---|---|---|
| `cultura_ativa` | `cafe_arabica` | str | `POST /api/cultura` (com confirmação na tela), `coletor --cultura` | coletor (troca a quente), Flask | Código da cultura monitorada |
| `modo_operacao` | `SIMULACAO` | str | coletor (`definir_configuracao_condicional`), Flask (banca) | coletor, Flask | `CAMPO_USB`, `SIMULACAO` ou `DEMONSTRACAO` |
| `estado_hardware` | `DESCONECTADO` | str | somente o coletor | Flask | `CONECTADO` / `DESCONECTADO` |
| `porta_serial` | `AUTO` | str | operador | coletor | Porta do Arduino; `AUTO` = autodetecção |
| `baud_rate` | `9600` | int | operador | coletor | Velocidade serial (igual ao firmware) |
| `intervalo_coleta_segundos` | `5` | float | operador | coletor, Flask (limites de idade e de batimento) | Intervalo entre leituras gravadas |
| `intervalo_reconexao_segundos` | `15` | float | operador | coletor | Intervalo entre tentativas de reconectar o Arduino |
| `cenario_demonstracao` | *(vazio)* | str | Flask; limpa: coletor/Flask | coletor, Flask | Cenário da banca em exibição |
| `demonstracao_expira_em` | *(vazio)* | str | Flask — **v2 grava em ISO UTC `+00:00`**; o coletor e o Flask aceitam com e sem fuso | coletor, Flask | Fim da banca |
| `duracao_demonstracao_segundos` | `120` | int | operador | Flask | Duração de cada cenário |
| `versao_modelo` | `REGRAS` | str | `treinamento_ia.main` | `/api/saude`, `/api/status` | Versão do `.pkl` da IA do instante (real: `RandomForestClassifier-20261114111842`) |
| `nome_propriedade` | `Sítio Modelo` | str | operador | tela | Nome da propriedade exibido |

**Chaves novas da v2 — confiabilidade de campo** (17, semeadas por `semear_configuracoes_confiabilidade` e por
`inicializar_banco`, `INSERT OR IGNORE`; descrição real gravada no banco):

| chave | inicial | tipo | Quem escreve | Quem lê | Descrição |
|---|---|---|---|---|---|
| `janela_mediana_leituras` | `12` | int | operador | coletor | Leituras na mediana móvel aplicada ao campo antes da regra |
| `taxa_maxima_queda_solo_pp_h` | `8.0` | float | operador | coletor | Queda máxima plausível da umidade do solo (evaporação), em p.p./h |
| `minutos_travamento_solo` | `60` | int | operador | coletor | Minutos com o solo idêntico para suspeitar de sensor travado |
| `horas_travamento_ar` | `3` | int | operador | coletor | Horas com temperatura e umidade do ar idênticas para suspeitar do DHT11 |
| `exigir_protocolo_v2` | `false` | bool | operador | coletor | Se verdadeiro, descarta linhas seriais sem checksum (v1) |
| `espaco_minimo_mb` | `200` | float | operador | coletor, `/api/saude` | Espaço livre mínimo antes da retenção de emergência (e do 503) |
| `retencao_dias_brutos` | `30` | int | operador | coletor | Dias de leituras brutas mantidas |
| `coletor_batimento_em` | *(vazio)* | str | coletor, a cada ciclo, na transação da leitura | `/api/saude`, `/api/status` | Instante ISO UTC do último ciclo (coletor vivo se a idade ≤ `max(60, 3 × intervalo)`) |
| `coletor_pid` | `0` | int | coletor | `/api/saude` | PID do processo coletor |
| `coletor_ultimo_erro` | *(vazio)* | str | coletor | `/api/saude` | Último erro de ciclo |
| `coletor_fila_pendente` | `0` | int | coletor | `/api/saude` | Leituras em memória aguardando gravação |
| `disco_livre_mb` | *(vazio)* | float | coletor (`shutil.disk_usage`) | `/api/saude` (que também mede por conta própria) | Espaço livre no disco do banco |
| `relogio_ultimo_salto_em` | *(vazio)* | str | coletor (Δ`time.time()` × Δ`time.monotonic()` > 60 s) | `/api/saude` | Último salto de relógio detectado |
| `serial_linhas_v1` | `0` | int | coletor | `/api/saude` | Linhas de dados do protocolo v1 |
| `serial_linhas_v2` | `0` | int | coletor | `/api/saude` | Linhas de dados do protocolo v2 |
| `serial_checksum_invalido` | `0` | int | coletor | `/api/saude` | Linhas v2 descartadas por checksum |
| `serial_linhas_descartadas` | `0` | int | coletor | `/api/saude` | 1ª linha após conectar/reiniciar a placa e linhas v1 recusadas |

**Chaves da v2 gravadas sob demanda pela interface** (não semeadas; `definir_configuracao` cria com tipo inferido):

| chave | tipo | Quem escreve | Quem lê | Descrição |
|---|---|---|---|---|
| `pin_banca_hash` | str | `python src/seguranca_banca.py --definir-pin 1234` / `--remover-pin` | `app._exigir_pin_banca` | `pbkdf2_sha256$200000$<sal_hex>$<hash_hex>`; vazio ou ausente = sem PIN (comportamento da v1) |
| `cultura_anterior` | str | `POST /api/cultura` | `/api/status` | Cultura antes da última troca |
| `cultura_alterada_em` | str | `POST /api/cultura` | `/api/status` (`segundos_desde_troca_cultura`) | Instante ISO UTC da última troca; a tela avisa os outros aparelhos por 10 min |
| `intervalo_repeticao_alerta_segundos` | int | operador (opcional) | `/api/status` (padrão 600) | Intervalo de repetição da fala/bipes no Vermelho |

Escritas concorrentes relevantes: o fim de uma demonstração é feito por `encerrar_demonstracao_se_inalterada` em
`BEGIN IMMEDIATE`; o `POST /api/demonstracao` grava **expiração → cenário → modo**; o batimento vai na mesma
transação da leitura (sem escrita extra por ciclo).

---

## 8. Tabela interna `sqlite_sequence`

Criada automaticamente pelo SQLite por causa do `AUTOINCREMENT`. Não é escrita pela aplicação.

| Coluna | Tipo declarado | Descrição | Exemplo |
|---|---|---|---|
| `name` | *(nenhum)* | Nome da tabela com AUTOINCREMENT | `historico_leituras` |
| `seq` | *(nenhum)* | Maior id já gerado (não volta atrás depois da retenção) | `3` |

---

## 9. Índices, chaves, integridade e migração

| Índice | Tabela | Colunas | Único? | Origem | Uso |
|---|---|---|---|---|---|
| `sqlite_autoindex_culturas_1` | culturas | `codigo` | sim | `UNIQUE` | Busca por código |
| `idx_parametros_cultura_id` | parametros_cultura | `id_cultura` | não | DDL | Faixas de uma cultura |
| `sqlite_autoindex_parametros_cultura_1` | parametros_cultura | `id_cultura, variavel` | sim | `UNIQUE` | Upsert de faixa |
| `idx_historico_data_hora` | historico_leituras | `data_hora` | não | DDL | Corte da retenção, consultas por período |
| `idx_historico_cultura_data` | historico_leituras | `id_cultura, data_hora` | não | DDL | Série horária do prognóstico (`buscar_serie_horaria`) |
| `idx_historico_cultura_id` | historico_leituras | `id_cultura, id_leitura` | não | **v2** `_DDL_MIGRACAO` | Última leitura por cultura em ordem de gravação (`/api/status`) |
| `idx_historico_cultura_origem_id` | historico_leituras | `id_cultura, origem, id_leitura` | não | **v2** `_DDL_MIGRACAO` | `buscar_ultima_leitura_origem` (semente do saneador ao reconectar) |
| `sqlite_autoindex_historico_agregado_hora_1` | historico_agregado_hora | `id_cultura, hora, origem` | sim | `PRIMARY KEY` | Upsert da agregação |
| `sqlite_autoindex_configuracao_sistema_1` | configuracao_sistema | `chave` | sim | `PRIMARY KEY` | Leitura de configuração |
| `idx_registro_acoes_causa_instante` | registro_acoes | `causa, instante_utc` | não | **v3** `retorno_agricultor` | `causa_silenciada` (último "Feito" da causa) |
| `idx_eventos_interface_instante` | eventos_interface | `instante_utc` | não | **v3** `retorno_agricultor` | Retenção de 90 dias e resumo do técnico |
| `idx_pedido_ajuda_instante` | pedido_ajuda | `instante_utc` | não | **v3** `retorno_agricultor` | Pedidos dos últimos 7 dias |
| `idx_calibracao_sonda_no_ativa` | calibracao_sonda | `id_no, ativa` | não | **v3** `calibracao_sonda` | Calibração ativa da sonda |

`analise_antecipacao` usa `id_leitura INTEGER PRIMARY KEY` (alias do rowid; sem índice extra).

| Relacionamento | Cardinalidade | Ação ON DELETE | Observação |
|---|---|---|---|
| `culturas` → `parametros_cultura` | 1 : 1..N (aplicação) | `CASCADE` | Apagar uma cultura apaga suas faixas |
| `culturas` → `historico_leituras` | 1 : 0..N | `NO ACTION` | Cultura com histórico não pode ser apagada; desative com `ativo = 0` |
| `culturas` → `historico_agregado_hora` | 1 : 0..N | `NO ACTION` | Idem |
| `historico_leituras` → `analise_antecipacao` | 1 : 0..1 | `CASCADE` | A retenção apaga as análises junto, sem conhecer a tabela (exige `foreign_keys = ON`, ligado em `obter_conexao`) |
| `configuracao_sistema['cultura_ativa'/'cultura_anterior']` → `culturas.codigo` | 0..1 : 1 | — | Vínculo lógico |
| `historico_leituras` → `registro_acoes` | 0..1 : 0..N | `SET NULL` | **v3:** o "Feito" sobrevive à retenção das leituras brutas |
| `historico_leituras` → `pedido_ajuda` | 0..1 : 0..N | `SET NULL` | **v3:** idem; o pedido guarda cor, causa e leituras |

**Migração v2 → v3** (`migrar_esquema`, idempotente): acrescenta `motivo_regra`, `base_temperatura`,
`temperatura_media_24h` e `umidade_solo_firmware` por `ALTER TABLE ADD COLUMN` (linhas antigas ficam `''`, `''`,
`NULL`, `NULL`). As tabelas v3 são criadas por `retorno_agricultor.garantir_tabelas` e
`calibracao_sonda.garantir_tabela` (chamadas pelo Flask, pelo alarme e pelo coletor) e as chaves v3 por
`semear_configuracoes_v3` (CLI do banco e `_preparar` do coletor), sempre com `INSERT OR IGNORE`.

**Migração v1 → v2** (`migrar_esquema`, idempotente, chamada por `inicializar_banco` e por `_garantir_banco`): acrescenta
`estado_sensor`, `diagnostico_sensor` e `instante_utc` com `ALTER TABLE ADD COLUMN` (linhas antigas ficam `OK`, `''` e
com `instante_utc` calculado de `data_hora`), cria os dois índices novos e `historico_agregado_hora`. Testado
duas vezes seguidas sobre um banco criado com o DDL da baseline (`tests/test_confiabilidade_campo.py`). O log da
primeira execução mostra "Migração do esquema: coluna historico_leituras.estado_sensor criada." mesmo num banco novo,
porque o DDL base continua o da v1 e a migração acrescenta as colunas.

---

## 10. Domínios compartilhados (enumerações)

| Domínio | Valores | Definido em |
|---|---|---|
| Status | `0` Verde (Ideal) · `1` Amarelo (Atenção) · `2` Vermelho (Risco Crítico) | `database.NOMES_STATUS`, `CORES_STATUS` |
| Modo operante | `CAMPO_USB`, `SIMULACAO`, `DEMONSTRACAO` | `database.MODOS_OPERACAO` |
| Origem da leitura | modos + `DATASET` | `database.ORIGENS_LEITURA` |
| Estado do sensor | `OK` < `SUSPEITO` < `DEFEITO` | `database.ESTADOS_SENSOR` |
| Motivo da antecipação | `SECA_CHEGANDO`, `SECA_AGORA`, `ENCHARCAMENTO`, `CALOR_TARDE`, `GEADA_NOITE`, `FRIO`, `NENHUM` | `prognostico.MOTIVOS` |
| Ações (pictogramas) | `REGAR_AGORA`, `REGAR_FIM_DA_TARDE`, `REGAR_MANHA`, `NAO_REGAR`, `ABRIR_VALETAS`, `FAZER_SOMBRA`, `COBRIR_MUDAS`, `CHEGAR_TERRA`, `NADA_A_FAZER`, `VERIFICAR_APARELHO` (no máximo 3 por status) | `acoes_orientacao.ACOES_VALIDAS` |
| Tipo de linha serial | `DADOS`, `DIAGNOSTICO`, `INVALIDA`; protocolo `1` ou `2` | `coletor_serial.TIPO_LINHA_*`, `VERSAO_PROTOCOLO_V*` |
| Categoria de cultura | `cafe`, `fruta`, `grao`, `hortalica`, `leguminosa`, `outra` | `database.CATEGORIAS_CULTURA` |
| Tipo de configuração | `str`, `int`, `float`, `bool` | `database.TIPOS_CONFIGURACAO` |
| Tipo de anomalia | `nan`, `pico`, `negativo` | `gerador_dataset.TIPOS_RUIDO` |
| Estado do hardware | `CONECTADO`, `DESCONECTADO` | `coletor_serial.ESTADO_CONECTADO/ESTADO_DESCONECTADO` |
| Versões sem modelo | `REGRAS` (IA do instante), `SEM_PROGNOSTICO` (prognóstico) | `coletor_serial.VERSAO_SEM_MODELO`, `prognostico.VERSAO_SEM_PROGNOSTICO` |
| Limites físicos dos sensores | temperatura 0–50 °C; umidades 0–100 % | `gerador_dataset.LIMITES_FISICOS_SENSORES` |
| Salto máximo plausível | temperatura 8 °C; umidade do ar 30 p.p.; solo 15 p.p. (no campo, a **queda** do solo também respeita `taxa_maxima_queda_solo_pp_h`) | `gerador_dataset.LIMITES_SALTO` |
| Cenários de demonstração | `SECA_EXTREMA`(2), `ONDA_DE_CALOR`(2), `GEADA`(2), `ENCHARCAMENTO`(2), `ATENCAO_MODERADA`(1), `CONDICAO_IDEAL`(0) | `gerador_dataset.CENARIOS_DEMONSTRACAO` (inalterados) |

---

## 11. CSV `data/dataset_bruto.csv`

Saída de `injetar_ruidos` (série do **simulador realista** com 3 % de ruído por sensor). **17.520 linhas** = 8.760 h
(1 ano, de `2025-11-01 00:00:00` a `2026-10-31 23:00:00`, `DATA_INICIAL_DATASET`) × 2 culturas ativas. Separador `,`,
cabeçalho na 1ª linha, UTF-8, NaN gravado como campo vazio.

| Coluna | Tipo | Nulo? | Domínio / unidade | Descrição | Exemplo |
|---|---|---|---|---|---|
| `data_hora` | texto | não | `YYYY-MM-DD HH:MM:SS`, horária | Instante simulado | `2025-11-01 00:00:00` |
| `cultura` | texto | não | `culturas.codigo` | Talhão simulado | `cafe_arabica` |
| `temperatura_ar` | real | sim (ruído `nan`) | °C; pode conter picos e negativos | Valor com ruído | `19.7` |
| `umidade_ar` | real | sim | % | Valor com ruído | `99.0` |
| `umidade_solo` | real | sim | % | Valor com ruído | *(vazio)* |
| `tipo_ruido_injetado` | texto | vazio = limpa | `coluna:tipo;` concatenados | **Gabarito** — usado só por `avaliar_detector` | `umidade_solo:nan;` |

Medido no dataset atual: 526 células corrompidas por sensor (176 `nan`, 175 `pico`, 175 `negativo`) = 3,00 %, em 1.532 linhas.

---

## 12. CSV `data/dataset_limpo.csv`

Saída de `sanear_dataset` + `calcular_features_dataframe`; entrada dos dois treinamentos. **17.520 linhas**, sem valores ausentes.

| Coluna | Tipo | Nulo? | Domínio / unidade (faixa real) | Descrição | Exemplo |
|---|---|---|---|---|---|
| `data_hora` | texto | não | ISO | Instante simulado | `2025-11-03 04:00:00` |
| `cultura` | texto | não | `culturas.codigo` | Talhão | `cafe_arabica` |
| `temperatura_ar` | real (1 casa) | não | °C (0,9 a 37,0 — geadas de inverno e escaldadura de verão) | Saneada e interpolada | `24.9` |
| `umidade_ar` | real (1 casa) | não | % (40,6 a 99,0) | Saneada | `99.0` |
| `umidade_solo` | real (1 casa) | não | % (13,3 a 100,0) | Saneada | `70.0` |
| `flag_anomalia` | inteiro | não | 0/1 (1.533 linhas = 1, 8,75 %) | Linha teve alguma célula corrigida | `1` |
| `descricao_anomalia` | texto | vazio = limpa | `coluna:tipo` separados por `;` | O que o detector encontrou | `umidade_solo:pico` |
| `deficit_hidrico_solo` | real (2 casas) | não | p.p. (−20,0 a 41,7) | Feature 1 | `0.0` |
| `desvio_termico` | real (2 casas) | não | °C (−21,1 a 11,0) | Feature 2 | `2.9` |
| `status_alerta` | inteiro | não | Verde 4.631 (26,43 %), Amarelo 7.393 (42,20 %), Vermelho 5.496 (31,37 %) | **Rótulo** (motor de regras) | `1` |

Detector de anomalias contra o gabarito: recall 1,0000, precisão 0,9993, F1 0,9997 (VP 1.532, FP 1, FN 0). O relatório
mensal de realismo (geadas, veranico, chuva por mês, cores por mês) fica em `reports/realismo_simulador.txt`
(`gerar_relatorio_realismo`).

Features da IA do instante (`COLUNAS_FEATURES_MODELO`): `temperatura_ar, umidade_ar, umidade_solo, deficit_hidrico_solo,
desvio_termico`. Alvo (`COLUNA_ALVO`): `status_alerta`. A IA de prognóstico usa outras 36 features (seção 14).

---

## 13. Pacote `models/modelo_agroanalise.pkl` (IA do instante)

`joblib.dump` de um `dict` (gerado por `treinamento_ia.salvar_modelo`, lido por `coletor_serial.carregar_modelo`).
**Conjunto de chaves igual ao da v1** (requisito da v2), 2,6 MB.

| Chave | Tipo Python | Descrição | Valor real |
|---|---|---|---|
| `modelo` | `RandomForestClassifier` | Estimador treinado com DataFrame | `RandomForestClassifier(class_weight='balanced', n_estimators=300, n_jobs=-1, random_state=42)` |
| `nome_modelo` | `str` | Nome do campeão | `RandomForestClassifier` |
| `versao` | `str` | `<nome>-<AAAAMMDDHHMMSS>` | `RandomForestClassifier-20261114111842` |
| `features` | `List[str]` | Colunas e ordem exigidas | as 5 de `COLUNAS_FEATURES_MODELO` |
| `classes` | `List[int]` | Classes do estimador | `[0, 1, 2]` |
| `metricas` | `Dict[str, Any]` | Resultado de `avaliar_modelo` | matriz `[[926, 0, 0], [0, 1479, 0], [0, 0, 1099]]`, `n_teste` 3.504, `n_treino` 14.016, importâncias: desvio_termico 0,379; deficit_hidrico_solo 0,320; umidade_solo 0,173; temperatura_ar 0,124; umidade_ar 0,004; `falsos_por_cultura` Arábica 1.754 e Conilon 1.750 amostras, todos com FP = FN = 0 |
| `treinado_em` | `str` | ISO com segundos | `2026-11-14T11:18:42` |
| `culturas` | `List[str]` | Culturas do treino; fora delas vale `REGRAS` | `['cafe_arabica', 'cafe_conilon']` |

---

## 14. Pacote `models/modelo_prognostico.pkl` (IA de prognóstico, novo)

`joblib.dump` de um `dict` com **exatamente 9 chaves** (`prognostico.CHAVES_PACOTE_PROGNOSTICO`), gerado por
`treinamento_ia.salvar_modelo_prognostico` e lido por `prognostico.carregar_modelo_prognostico` (cache por `mtime`;
`None` se ausente ou corrompido — a antecipação continua só com o ETA de secagem e a geada). 17,2 MB.

| Chave | Tipo Python | Descrição | Valor real |
|---|---|---|---|
| `modelo` | `CalibratedClassifierCV` | `RandomForestClassifier` calibrado (`method='isotonic'`, dobras temporais com intervalo de 12 h) | `CalibratedClassifierCV` |
| `features` | `List[str]` | 36 features temporais (abaixo) | — |
| `limiar` | `float` | Limiar de P para o Amarelo antecipado, escolhido por `escolher_limiar_por_orcamento` (maior recall com ≤ 14 h/semana de Amarelo antecipado falso, na validação dentro do treino) | `0.3165` |
| `horizonte_horas` | `int` | Horizonte do alvo "entra no Vermelho nas próximas h horas" | `12` |
| `culturas` | `List[str]` | Culturas do treino | `['cafe_arabica', 'cafe_conilon']` |
| `coef_tmin` | `Tuple[float, float, float]` | `(a, b, c)` de `Tmin = a + b·T_tarde + c·Td_tarde`, ajustados por mínimos quadrados em 508 noites do treino (padrão sem pacote: `COEF_TMIN_PADRAO = (-1.0, 0.25, 0.75)`) | `(0.116, -0.153, 1.147)` |
| `metricas` | `Dict[str, Any]` | `n_treino` 8.581, `taxa_alvo_treino` 0,195, `periodo_treino`, `limiar_recall_validacao` 0,466, `limiar_falsos_hora_semana_validacao` 13,8, `orcamento_horas_semana` 14,0, `coef_tmin_erro_padrao` 0,43 °C, `coef_tmin_noites` 508, `medianas_features` (usadas no contrafactual de `explicar_alerta`), `teste_temporal` (AUC 0,919, Brier 0,094 contra 0,343 da persistência, F1 macro 0,848 contra 0,595, 86,7 % dos inícios de Vermelho com ≥ 3 h de aviso, métricas de geada) | — |
| `versao` | `str` | Versão do pacote, gravada em `analise_antecipacao.versao_prognostico` | `Prognostico12h-RandomForestCalibrado-20261114111851` |
| `treinado_em` | `str` | ISO com segundos | `2026-11-14T11:18:51` |

**Features temporais** (`COLUNAS_FEATURES_PROGNOSTICO`, `calcular_features_temporais`, todas relativas às faixas da
cultura e calculadas só com o passado — `shift`/`rolling` para trás):

| Grupo | Features |
|---|---|
| Temperatura (17) | `temp_var_1h`, `temp_var_3h`, `temp_var_6h`, `temp_var_12h`; `temp_media_6h`, `temp_min_6h`, `temp_max_6h`, `temp_media_24h`, `temp_min_24h`, `temp_max_24h`; margens `temp_margem_ideal_min/ideal_max/atencao_min/atencao_max` (normalizadas pela largura ideal); `vpd_kpa` (déficit de pressão de vapor); `orvalho_margem_critico` (ponto de orvalho de Magnus × `critico_min`); `horas_estresse_temperatura_24` |
| Solo (16) | `solo_var_1h/3h/6h/12h`; `solo_media/min/max_6h` e `_24h`; margens `solo_margem_ideal_min/ideal_max/atencao_min/atencao_max`; `horas_ate_secar` (teto 72); `horas_estresse_solo_24` |
| Comuns (3) | `horas_estresse_24`, `hora_sen`, `hora_cos` |

Alvo de treino (`calcular_alvo_risco_futuro`): 1 se `max(status[t+1..t+12]) == 2`, NaN nas 12 últimas horas; só entram
horas com status atual < 2 (`preparar_dataset_prognostico`).

---

## 15. Protocolo serial v2

| Tipo de linha | Formato | Exemplo | Tratamento (`interpretar_linha_serial_detalhada`) |
|---|---|---|---|
| Dado v2 | `$2,<t>,<ur>,<solo>*XX` — `XX` = XOR, em hexadecimal maiúsculo, dos bytes entre `$` e `*` (`calcular_checksum_nmea`) | `$2,21.0,65.0,72.4*` + checksum | `LinhaSerial(tipo='DADOS', valores=(21.0, 65.0, 72.4), versao_protocolo=2, checksum_valido=True)` |
| Dado v2 com DHT11 falho | `nan` no ar | `$2,nan,nan,70.0*XX` | valores `(None, None, 70.0)`: conta como ciclo com dados (não desconecta); o saneador segura o ar e, após 30 leituras, o ar vira `DEFEITO` |
| Checksum errado | — | `$2,71.0,65.0,72.4*` + checksum de `21.0` | `checksum_valido=False`, descartada, `serial_checksum_invalido += 1` |
| Dado v1 (legado) | `t,ur,solo` | `21.0,65.0,72.4` | aceito (`versao_protocolo=1`), a menos que `exigir_protocolo_v2 = true` |
| Diagnóstico | começa com `#` | `#AGROANALISE_PRONTO;PROTOCOLO=2`, `#ERRO:DHT11_LEITURA_INVALIDA`, `#AVISO:HIGROMETRO_DESCONECTADO` | `tipo='DIAGNOSTICO'`; vai para `DiagnosticoSensores.registrar_diagnostico`; o banner faz a próxima linha de dados ser descartada |
| Malformada | campos ≠ 3, não numérico, > 256 bytes | `21.0;65` | `tipo='INVALIDA'` |

`interpretar_linha_serial(linha)` mantém a assinatura e o retorno da v1 (tupla ou `None`) e aceita v1 e v2. Depois de
cada abertura da porta, a **1ª linha de dados é descartada** e contada em `serial_linhas_descartadas` (é a linha
cortada pelo reset da placa, ex.: `1.0,65.0,70.2`).

---

## 16. Contrato JSON de `GET /api/status`

Exemplo real (Flask `test_client` sobre o banco gerado do zero, depois de 3 ciclos do coletor em `--simular`;
`Cache-Control: no-store`; com `Accept-Encoding: gzip` a resposta sai comprimida):

```json
{
  "tem_leitura": true,
  "status_alerta": 0,
  "cor": "verde",
  "titulo": "Sensor desligado: dados de teste",
  "mensagem_voz": "O aparelho do campo não está mandando medições. As cores agora são só um teste e não mostram a sua lavoura. Não regue nem mude nada por causa delas. Verifique o cabo do aparelho.",
  "nome_cultura": "Café Arábica",
  "codigo_cultura": "cafe_arabica",
  "temperatura_ar": 21.2,
  "umidade_ar": 66.4,
  "umidade_solo": 66.6,
  "deficit_hidrico_solo": 0.0,
  "desvio_termico": 0.0,
  "data_hora": "2026-11-14 11:19:17",
  "origem": "SIMULACAO",
  "modo_operacao": "SIMULACAO",
  "estado_hardware": "DESCONECTADO",
  "flag_anomalia": 0,
  "versao_modelo": "RandomForestClassifier-20261114111842",
  "cenario_demonstracao": "",
  "nome_propriedade": "Sítio Modelo",
  "nome_status": "Verde (Ideal)",
  "categoria_cultura": "cafe",
  "rotulo_modo": "Simulação",
  "descricao_anomalia": "",
  "demonstracao_ativa": false,
  "rotulo_cenario": "",
  "segundos_restantes_demonstracao": 0,
  "id_leitura": 3,
  "leitura_desatualizada": false,
  "idade_leitura_segundos": 1,
  "dados_simulados": true,
  "orientacao_simulada": "Tudo bem com a plantação de café arábica. A terra está molhada na medida e o tempo está bom. Não precisa fazer nada agora.",
  "status_em_confirmacao": false,
  "data_hora_ultima_leitura": "2026-11-14 11:19:17",
  "acoes": ["VERIFICAR_APARELHO"],
  "acoes_detalhe": [{"codigo": "VERIFICAR_APARELHO", "legenda": "Aparelho", "descricao": "Confira o aparelho"}],
  "motivo_principal": "NENHUM",
  "prazo_horas": null,
  "horas_em_estresse": null,
  "antecipacao": null,
  "estado_sensor": "OK",
  "relogio_inconsistente": false,
  "coletor_vivo": true,
  "intervalo_repeticao_alerta_segundos": 600,
  "cultura_anterior": null,
  "cultura_alterada_em": null,
  "segundos_desde_troca_cultura": null
}
```

São **47 campos**: os 20 do contrato técnico, os 14 complementares da v1 e **13 novos da v2**:

| Campo novo | Tipo | Origem | Significado |
|---|---|---|---|
| `acoes` | `List[str]` (0 a 3) | `acoes_orientacao.derivar_acoes` | Pictogramas de AÇÃO desenhados abaixo do título |
| `acoes_detalhe` | `List[{codigo, legenda, descricao}]` | `LEGENDAS_ACAO`, `DESCRICOES_ACAO` | Legenda de uma palavra e `aria-label` de cada pictograma |
| `motivo_principal` | `str` em `MOTIVOS` | `obter_analise_antecipacao` | Motivo da antecipação; `NENHUM` sem análise |
| `prazo_horas` | `float \| null` | idem | Prazo até o problema |
| `horas_em_estresse` | `float \| null` | idem | Horas seguidas fora do ideal |
| `antecipacao` | `dict \| null` | idem | Linha completa de `analise_antecipacao` (só para leitura `CAMPO_USB` atual, nunca para simulação, demonstração ou leitura velha) |
| `estado_sensor` | `str` | `historico_leituras.estado_sensor` | `OK`/`SUSPEITO`/`DEFEITO`; diferente de OK → título "Confira o aparelho" e `acoes = ['VERIFICAR_APARELHO']` |
| `relogio_inconsistente` | `bool` | `_idade_segundos` com `instante_utc` | Leitura mais de 120 s "no futuro" → Amarelo, "Relógio do aparelho errado" |
| `coletor_vivo` | `bool \| null` | `coletor_batimento_em` | `null` sem batimento (banco novo); `false` se o batimento passou de `max(60 s, 3 × intervalo)` |
| `intervalo_repeticao_alerta_segundos` | `int` | configuração (padrão 600) | Repetição da fala/bipes no Vermelho |
| `cultura_anterior` | `str \| null` | configuração | Cultura antes da última troca |
| `cultura_alterada_em` | `str \| null` | configuração (ISO UTC) | Quando foi a troca |
| `segundos_desde_troca_cultura` | `int \| null` | calculado no servidor | A tela mostra "A plantação foi trocada para ... às HH:MM" quando < 600 |

Exemplo real com antecipação (banco temporário da seção 5, leitura `CAMPO_USB` a 51 % de solo depois de 24 h secando):

```json
{
  "status_alerta": 1,
  "cor": "amarelo",
  "titulo": "Atenção com o seu café",
  "mensagem_voz": "Atenção com a plantação de café arábica. A terra está seca há 9 horas e deve ficar muito seca daqui a pouco. Regue agora, devagar, até a terra ficar úmida.",
  "acoes": ["REGAR_FIM_DA_TARDE"],
  "motivo_principal": "SECA_CHEGANDO",
  "prazo_horas": 1.0,
  "horas_em_estresse": 9.0,
  "antecipacao": {"probabilidade_risco_futuro": 0.65, "horas_ate_secar": 1.0, "temperatura_minima_prevista": null,
                  "motivo_principal": "SECA_CHEGANDO", "prazo_horas": 1.0, "horas_em_estresse": 9.0,
                  "status_antecipado": 1, "versao_prognostico": "Prognostico12h-RandomForestCalibrado-20261114111851"},
  "estado_sensor": "OK",
  "coletor_vivo": true
}
```

Observação honesta sobre este exemplo: a voz diz "Regue agora" (frase da antecipação, porque a terra deve secar em
1 h), mas o pictograma é "Regue no fim da tarde" (`ACOES_POR_MOTIVO['SECA_CHEGANDO']`). As duas orientações apontam para
regar, mas o momento diverge; ver as limitações conhecidas no [CHANGELOG](../CHANGELOG.md).

Precedência do que é exibido (`montar_status`), da mais forte para a mais fraca:

| Situação | `status_alerta` / `cor` | `titulo` | `mensagem_voz` | `acoes` |
|---|---|---|---|---|
| Sem nenhuma leitura | 1 / amarelo | `Esperando o sensor` (ou `Trocando de plantação`) | aviso de aparelho desligado (ou "aguarde alguns segundos") | `VERIFICAR_APARELHO` (ou nenhuma) |
| Relógio inconsistente (`instante_utc` > 120 s no futuro) | 1 / amarelo | `Relógio do aparelho errado` | "O relógio do computador está errado: as medições parecem vir do futuro..." | `VERIFICAR_APARELHO` |
| Leitura velha **e** coletor parado (`coletor_vivo = false`) | 1 / amarelo | `O programa parou` | "O programa do computador parou. Desligue e ligue o computador. A última cor pode não valer mais." | `VERIFICAR_APARELHO` |
| Leitura velha (> `max(60 s, 6 × intervalo)`), coletor vivo ou sem batimento | 1 / amarelo | `Sem medições novas` | "O aparelho não manda medições há ..." | `VERIFICAR_APARELHO` |
| Origem `SIMULACAO` | a da leitura | `Sensor desligado: dados de teste` | aviso de que as cores são só um teste | `VERIFICAR_APARELHO` |
| `estado_sensor` ≠ `OK` | a da leitura (nunca 2 pelo sensor) | `Confira o aparelho` | a da leitura (com "sensor da terra"/"sensor do ar") | `VERIFICAR_APARELHO` |
| Demais casos | a da leitura (histerese fora da banca) | `Tudo certo com...` / `Atenção com...` / `Perigo para...` | a da leitura (com a frase de antecipação, quando houver) | `derivar_acoes(..., motivo_antecipacao)` |

---

## 17. Contrato JSON de `GET /api/saude`

Mantém as chaves da v1 no topo e acrescenta `componentes`. Responde **503** se o banco falhar, se `coletor.vivo` for
`false` ou se o disco estiver abaixo de `espaco_minimo_mb`; sem batimento gravado, `vivo = null` e não há 503 (carência
para banco novo). Exemplo real (coletor rodando há 2 s):

```json
{
  "ok": true,
  "banco": true,
  "modelo_carregado": true,
  "versao_modelo": "RandomForestClassifier-20261114111842",
  "modo_operacao": "SIMULACAO",
  "culturas_modelo": ["cafe_arabica", "cafe_conilon"],
  "componentes": {
    "banco": {"ok": true},
    "coletor": {"vivo": true, "idade_batimento_s": 2.3, "pid": 11060, "ultimo_erro": "", "fila_pendente": 0},
    "disco": {"livre_mb": 30009.9, "minimo_mb": 200.0, "ok": true},
    "relogio": {"inconsistente": false, "ultimo_salto_em": null},
    "prognostico": {"disponivel": true},
    "serial": {"linhas_v1": 0, "linhas_v2": 0, "checksum_invalido": 0, "descartadas": 0}
  }
}
```

Outros contratos HTTP novos: `POST /api/demonstracao`, `/api/demonstracao/encerrar` e `/api/cultura` exigem o
cabeçalho `X-PIN-Banca` quando `pin_banca_hash` existe → `403 {"erro": "Peça a senha ao técnico."}`; 5 erros do mesmo IP
→ `429 {"erro": "Muitas tentativas. Espere um minuto."}`; corpo acima de 4.096 bytes → `413 {"erro": "Pedido grande demais."}`.
`GET /` devolve o HTML com o estado atual embutido em `<script type="application/json" id="estado-inicial">` e
`Cache-Control: no-cache`.

---

## 18. Armazenamento no navegador e arquivos de operação

| Item | Onde | Conteúdo | Observação |
|---|---|---|---|
| `agro_ultimo_status` | `localStorage` | último `/api/status` válido + instante de recebimento | Mostrado só como "Última notícia de hoje às HH:MM — sem ligação agora" (`body.desatualizada`), nunca como dado atual |
| `agro_voz_automatica` | `localStorage` | preferência de voz automática | Sobrevive ao recarregar |
| `agro_troca_conhecida` | `localStorage` | última `cultura_alterada_em` vista neste aparelho | Evita repetir o aviso de troca |
| `agro_pin_banca` | `sessionStorage` | PIN digitado pelo técnico | Some ao fechar a aba |
| `agro_voz_lenta` | `localStorage` | `"1"` depois de a pessoa repetir a fala 2 vezes seguidas (v4) | As falas seguintes saem com taxa 0,85 |
| `agro_feitos` | `localStorage` | "Feito" tocados sem rede (até 20), reenviados quando a ligação volta (v4) | Não guarda o que o servidor recusou |
| `logs/coletor.log`, `logs/web.log` | disco | logs rotativos (5 arquivos × 5 MB) | `registro.configurar_logs`; pasta trocável por `AGRO_PASTA_LOGS` |
| `reports/ux/*.png` | disco | capturas 360×740 do teste e2e | gerado por `tests/e2e/ux_campo.spec.js` |

Todo acesso a `localStorage`/`sessionStorage` é protegido por `try/catch` (navegador em modo privado continua funcionando).

---

## 19. Consultas úteis

```sql
-- Última leitura exibível de uma cultura (ordem de gravação, imune a salto de relógio)
SELECT h.*, c.codigo AS codigo_cultura, c.nome_comum AS nome_cultura
  FROM historico_leituras h JOIN culturas c ON c.id_cultura = h.id_cultura
 WHERE c.codigo = 'cafe_arabica' AND h.origem <> 'DEMONSTRACAO'
 ORDER BY h.id_leitura DESC LIMIT 1;

-- Leituras com o prognóstico (Amarelos antecipados por motivo nas últimas 24 h)
SELECT a.motivo_principal, COUNT(*) AS leituras, ROUND(AVG(a.probabilidade_risco_futuro), 3) AS p_media
  FROM analise_antecipacao a JOIN historico_leituras h ON h.id_leitura = a.id_leitura
 WHERE a.status_antecipado = 1 AND h.data_hora >= datetime('now', 'localtime', '-1 day')
 GROUP BY a.motivo_principal;

-- Saúde dos sensores: leituras por estado nas últimas 24 h
SELECT estado_sensor, COUNT(*) FROM historico_leituras
 WHERE data_hora >= datetime('now', 'localtime', '-1 day') GROUP BY estado_sensor;

-- Qualidade do protocolo serial (contadores do coletor)
SELECT chave, valor FROM configuracao_sistema WHERE chave LIKE 'serial_%' OR chave LIKE 'coletor_%';

-- Histórico de longo prazo (depois da retenção): pior cor por dia
SELECT substr(hora, 1, 10) AS dia, MAX(status_alerta_max) AS pior, SUM(n) AS leituras
  FROM historico_agregado_hora WHERE origem = 'CAMPO_USB' GROUP BY dia ORDER BY dia;

-- Divergência IA × regras (monitoramento do modelo do instante)
SELECT versao_modelo, COUNT(*) AS total, SUM(status_regra <> status_predito) AS divergencias
  FROM historico_leituras GROUP BY versao_modelo;
```

---

## 20. Mudanças da v3

### 20.1 Decisão: como a faixa do cliente é aplicada à temperatura

Os números do cliente continuam **cadastrados como estão** em `parametros_cultura` e continuam sendo a **faixa
ideal** (Arábica 18–22 / 15–25 / 8–32 °C e solo 60–80 / 50–88 / 35–95 %; Conilon 22–26 / 19–29 / 12–34 °C e solo
55–75 / 45–83 / 30–95 %). O que mudou é a **forma de aplicar a temperatura** (chave `regra_temperatura`):

| Componente | Regra `instantanea` (v2; chave ausente) | Regra `fisiologica` (v3; semeada pelo coletor) | Fonte |
|---|---|---|---|
| Conforto (faixa do cliente) | leitura instantânea, até Vermelho | **média móvel de 24 h** (≥ 18 h com dado; senão a leitura), **no máximo Amarelo** (`DIA_FRIO`/`DIA_QUENTE`) | faixas de aptidão são de temperatura média anual: [Portaria 58/2005](https://licenciamento.cetesb.sp.gov.br/legislacao/federal/portarias/2005_Port_Fed_58.pdf), [Incaper](https://biblioteca.incaper.es.gov.br/digital/bitstream/item/5052/1/tecnologiasconilon.pdf) |
| Geada | leitura < `critico_min` (8 / 12 °C) | abrigo ≤ `limiar_geada_abrigo_c` + `incerteza_sensor_ar_c` (DHT11: **≤ 4,0 °C**) → Vermelho `GEADA_AGORA`, sem banda | [Epamig/Embrapa Café](https://revistacultivar.com.br/noticias/pesquisadores-da-epamig-e-embrapa-cafe-orientam-cafeicultores-sobre-o-que-fazer-apos-as-geadas); [DHT11 ±2 °C](https://ece-classes.usc.edu/ee459/library/datasheets/DHT11.pdf) |
| Calor | leitura > `critico_max` | máxima do dia ≥ `critico_max` → Amarelo `CALOR_DIA`; máxima ≥ max(34, `critico_max`) e leitura ≥ `critico_max` → Vermelho `CALOR_FLORADA` (janela de florada) ou `CALOR_SEGUIDO` (2º dia) | [Embrapa, CBAGRO 2003](https://www.alice.cnptia.embrapa.br/alice/bitstream/doc/8420/1/PC-Temperaturas-elevadas-CBAGRO-2003.pdf) |
| Solo | instantâneo | instantâneo; com `seca_programada_ativa`, entre `critico_min` e o ideal → Amarelo `SECA_PROGRAMADA` | [Embrapa Cerrados](https://sbicafe.ufv.br/items/46703cd6-1804-4062-841a-3af5714ec5ac) |
| Status gravado | sem histerese | banda morta (3 % da faixa crítica) + off-delay de 60 s na melhora (conforto e solo) | [ISA-18.2](https://blog.isa.org/solve-common-problems-industrial-automation-alarm-systems) |

O sensor de ar deve ficar **em abrigo a 1,5 m do chão**. `DEMONSTRACAO` e `DATASET` usam sempre a regra
instantânea. Efeito medido em `docs/WORKFLOW.md` (seção 11.2) e no `CHANGELOG.md`.

### 20.2 Colunas novas de `historico_leituras`

Em bancos novos ou v1 entram **antes** de `estado_sensor`/`diagnostico_sensor`/`instante_utc` (ordem conferida por
teste); em bancos v2 entram no fim por `ALTER TABLE ... ADD COLUMN` (`migrar_esquema`, idempotente; linhas antigas
ficam com o padrão).

| Coluna | Tipo SQLite | Nulo? | Padrão | Domínio | Descrição |
|---|---|---|---|---|---|
| `motivo_regra` | TEXT | não | `''` | `NENHUM`, `GEADA_AGORA`, `CALOR_FLORADA`, `CALOR_SEGUIDO`, `CALOR_DIA`, `DIA_FRIO`, `DIA_QUENTE`, `SECA_AGORA`, `SECA_PROGRAMADA`, `ENCHARCAMENTO` ou `''` (v2) | Motivo do pior componente da regra (gravado nas duas regras; na `instantanea` as figuras seguem a voz da v2) |
| `base_temperatura` | TEXT | não | `''` | `instantanea`, `media_24h` ou `''` | Em que valor o conforto térmico foi julgado |
| `temperatura_media_24h` | REAL | sim | `NULL` | °C | Média de 24 h usada (só quando `base_temperatura = media_24h`) |
| `umidade_solo_firmware` | REAL | sim | `NULL` | % do firmware | Valor recebido do Arduino **antes** da calibração da sonda (só `CAMPO_USB`); `umidade_solo_bruta` passa a ser o valor já calibrado que entra no saneador |

### 20.3 Chaves novas de `configuracao_sistema` (`semear_configuracoes_v3`, `INSERT OR IGNORE`)

Semeadas pela CLI `python src/database.py` e pelo coletor ao iniciar (`inicializar_banco` não muda). Leitores usam
o padrão da v2 quando a chave falta (`regra_temperatura` ausente = `instantanea`). Também em tempo de execução:
`calibracao_pontos_pendentes` (assistente de calibração de dois pontos).

| chave | valor inicial | tipo | Descrição |
|---|---|---|---|
| `regra_temperatura` | `fisiologica` | str | Regra térmica: 'fisiologica' (faixa do cliente na média de 24 h + geada e calor) ou 'instantanea' (v2). |
| `limiar_geada_abrigo_c` | `2.0` | float | Temperatura do abrigo (1,5 m) com dano de geada ao cafeeiro, em °C (Epamig/Embrapa Café). |
| `incerteza_sensor_ar_c` | `2.0` | float | Incerteza do sensor de ar em °C (DHT11 ±2; DHT22 ±0,5; SHT3x ±0,2), somada ao limiar de geada. |
| `limiar_calor_dano_c` | `34.0` | float | Máxima do dia, em °C, que aborta flores do café (Embrapa, 2003). |
| `dias_janela_florada` | `45` | int | Dias da janela de florada a partir de data_florada_principal. |
| `data_florada_principal` | *(vazio)* | str | Data da florada principal (AAAA-MM-DD); vazio = setembro a novembro. |
| `seca_programada_ativa` | `false` | bool | Se verdadeiro, solo abaixo do ideal na janela da seca programada é só Amarelo (estresse hídrico controlado). |
| `seca_programada_inicio` | `06-24` | str | Início (MM-DD) da seca programada (Embrapa Cerrados: 24/jun). |
| `seca_programada_fim` | `09-04` | str | Fim (MM-DD) da seca programada (Embrapa Cerrados: 4/set). |
| `banda_morta_ativa` | `true` | bool | Banda morta no status gravado: piorar é imediato, melhorar exige folga. |
| `banda_morta_fracao` | `0.03` | float | Banda morta como fração da faixa crítica (0,03 = 0,72 °C no Arábica). |
| `banda_morta_atraso_s` | `60` | float | Off-delay da banda morta: segundos de melhora contínua antes de gravar a cor melhor (ISA-18.2). |
| `sensor_ar_modelo` | `DHT11` | str | Sensor de ar informado pelo firmware no banner (DHT11, DHT22 ou SHT3X). |
| `prognostico_suspenso` | `false` | bool | Prognóstico de 12 h suspenso pela avaliação em dado local (Frente 2). |
| `prognostico_brier_local_28d` | *(vazio)* | float | Brier do prognóstico nas últimas 4 semanas do próprio sítio (vazio = ainda não avaliado; Frente 2). |
| `prognostico_avaliado_em` | *(vazio)* | str | Instante ISO da última avaliação local do prognóstico (Frente 2). |
| `coef_tmin_local` | *(vazio)* | str | Coeficientes locais 'a,b,c' da mínima noturna (Tmin = a + b·T_tarde + c·Td_tarde), escritos pela Frente 2. |
| `quantis_tmin_local` | *(vazio)* | str | Margens conformais locais da mínima por estação, JSON {'seca': q, 'aguas': q} (Frente 2). |
| `platt_local` | *(vazio)* | str | Calibração de Platt local 'a,b' do prognóstico: p' = σ(a·logit(p)+b) (Frente 2). |
| `regiao_referencia` | *(vazio)* | str | Região climática de referência (ERA5-Land) usada pela Frente 2. |
| `alarme_local_ativo` | `true` | bool | Alarme sonoro local para Vermelho (Frente 3). |
| `alarme_silencio_inicio` | `21:30` | str | Início (HH:MM) do silêncio do alarme local (Frente 3); o Vermelho fica pendente até o fim do silêncio, exceto o Vermelho de geada (`GEADA_AGORA`), que toca sempre. |
| `alarme_silencio_fim` | `05:00` | str | Fim (HH:MM) do silêncio do alarme local (Frente 3). |
| `numero_whatsapp_tecnico` | *(vazio)* | str | Número de WhatsApp do técnico para o pedido de ajuda (Frente 3). |

### 20.4 Tabelas novas (criadas pelos módulos da v3 com `CREATE TABLE IF NOT EXISTS`)

`retorno_agricultor.garantir_tabelas` cria `registro_acoes`, `eventos_interface` (retenção de 90 dias em
`limpar_eventos_antigos`) e `pedido_ajuda`; `calibracao_sonda.garantir_tabela` cria `calibracao_sonda` (no máximo uma
linha `ativa = 1` por nó).

#### `registro_acoes`

```sql
CREATE TABLE registro_acoes (
    id_acao         INTEGER PRIMARY KEY AUTOINCREMENT,
    id_leitura      INTEGER REFERENCES historico_leituras(id_leitura) ON DELETE SET NULL,
    codigo_acao     TEXT    NOT NULL,
    causa           TEXT    NOT NULL,
    instante_utc    TEXT    NOT NULL,
    origem_aparelho TEXT    NOT NULL DEFAULT ''
)
```

#### `eventos_interface`

```sql
CREATE TABLE eventos_interface (
    id_evento    INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo         TEXT    NOT NULL
                 CHECK (tipo IN ('ouviu','tocou_pictograma','feito','ajuda','sem_voz','rede_caida','chamou_tecnico')),
    instante_utc TEXT    NOT NULL,
    detalhe      TEXT    NOT NULL DEFAULT ''
)
```

#### `pedido_ajuda`

```sql
CREATE TABLE pedido_ajuda (
    id_pedido      INTEGER PRIMARY KEY AUTOINCREMENT,
    instante_utc   TEXT    NOT NULL,
    status         INTEGER CHECK (status IS NULL OR status IN (0, 1, 2)),
    causa          TEXT    NOT NULL DEFAULT '',
    temperatura_ar REAL,
    umidade_solo   REAL,
    id_leitura     INTEGER REFERENCES historico_leituras(id_leitura) ON DELETE SET NULL
)
```

#### `calibracao_sonda`

```sql
CREATE TABLE calibracao_sonda (
    id_calibracao  INTEGER PRIMARY KEY AUTOINCREMENT,
    id_no          TEXT    NOT NULL DEFAULT 'principal',
    adc_seco       REAL    NOT NULL,
    adc_umido      REAL    NOT NULL,
    alvo_seco_pct  REAL    NOT NULL,
    alvo_umido_pct REAL    NOT NULL,
    metodo         TEXT    NOT NULL CHECK (metodo IN ('FABRICA','DOIS_PONTOS','UM_PONTO_CAMPO')),
    registrada_em  TEXT    NOT NULL,
    ativa          INTEGER NOT NULL DEFAULT 1 CHECK (ativa IN (0, 1)),
    observacao     TEXT    NOT NULL DEFAULT ''
)
```

### 20.5 Artefatos e arquivos novos

| Arquivo | Conteúdo |
|---|---|
| `models/complemento_prognostico.json` | `versao`, `regra_temperatura`, `gerado_em`, `climatologia_por_cultura` (`{cultura: {"MM-HH": p}}`, 288 chaves; parte térmica do ERA5-Land combinada com a do solo do simulador, p = 1 − (1 − p_t)(1 − p_s)), `coef_tmin_por_regiao` (`[a, b, c]`) e `quantis_tmin_por_regiao` (`{"seca": q, "aguas": q}`). Fora do `.pkl`, que continua com 9 chaves |
| `referencia_climatica/era5land_*_2018_2021.csv.gz` | ERA5-Land horário (35.064 linhas por região; CC BY 4.0, Open-Meteo/Copernicus); ver `LICENCA_E_FONTE.txt` |
| `reports/relatorio_treinamento.txt` | nova **SEÇÃO 7 — Dado real (reanálise ERA5-Land)** |

### 20.6 Protocolo serial: linha de perfil

Depois do banner v2 intacto (`#AGROANALISE_PRONTO;PROTOCOLO=2`), o firmware v3 manda
`#AGROANALISE_PERFIL;PROTOCOLO=2;AR=DHT11|DHT22;SOLO=RESISTIVO;PULSO=0|1`. O coletor também aceita `AR=` no banner de
uma linha só. Perfil desconhecido = DHT11. `PERFIS_SENSOR_AR`: DHT11 0–50 °C ±2,0; DHT22 −40–80 °C ±0,5; SHT3X
−40–125 °C ±0,2.

### 20.7 Contrato JSON: campos novos e rotas

`GET /api/status` ganha `causa` (`TERRA_SECA`, `ENCHARCADO`, `FRIO_GEADA`, `CALOR`, `APARELHO`, `NENHUMA`),
`causa_descricao`, `legenda_causa` e `causa_silenciada`. `GET /api/saude` ganha `componentes.prognostico`
(`disponivel`, `suspenso`, `brier_local_28d`) e `calibracao_sonda`. Rotas novas: `POST /api/acao_feita`,
`POST /api/pedido_ajuda`, `POST /api/evento` (sem PIN, com limite por IP e corpo ≤ 1 KB); `GET /tecnico`,
`GET /api/tecnico/resumo`, `POST /api/tecnico/configuracao`, `POST /api/banca/florada`,
`POST /api/banca/seca_programada`, `GET /api/calibracao`, `POST /api/calibracao/{ponto,concluir,aferir,fabrica}`
(com PIN, cabeçalho `X-PIN-Banca`).

### 20.8 Esquema real conferido por PRAGMA (v3)

Gerado em 15/11/2026 a partir de um banco **criado do zero** com `database.inicializar_banco` +
`semear_configuracoes_v3` + `retorno_agricultor.garantir_tabelas` + `calibracao_sonda.garantir_tabela` + o DDL de
`prognostico._DDL_ANALISE`, lendo `PRAGMA table_info`, `PRAGMA foreign_key_list` e `PRAGMA index_list`/`index_info`.
Resultado: **10 tabelas da aplicação** + `sqlite_sequence`, `historico_leituras` com **27 colunas** e
`configuracao_sistema` com **36 chaves** logo depois da criação (12 da v1 + 24 da v3; as chaves de confiabilidade da v2
e as da interface entram quando o coletor e o Flask rodam pela primeira vez; a CLI `python src/database.py`, que
semeia também as chaves de confiabilidade da v2, deixa **53 chaves**). Os `CHECK` não aparecem no `PRAGMA`;
estão no DDL das seções 2 a 7 e 20.4.

| Tabela | Criada por | Colunas (ordem real; `*` = NOT NULL; `PK`; padrão entre parênteses) | FKs | Índices |
|---|---|---|---|---|
| `analise_antecipacao` | `prognostico` | `id_leitura` INTEGER PK · `probabilidade_risco_futuro` REAL · `horas_ate_secar` REAL · `temperatura_minima_prevista` REAL · `motivo_principal` TEXT* · `prazo_horas` REAL · `horas_em_estresse` REAL · `status_antecipado` INTEGER* · `versao_prognostico` TEXT* · `criado_em` TEXT* (datetime('now','localtime')) | `id_leitura` → `historico_leituras.id_leitura` ON DELETE CASCADE | — |
| `calibracao_sonda` | `calibracao_sonda` | `id_calibracao` INTEGER PK · `id_no` TEXT* ('principal') · `adc_seco` REAL* · `adc_umido` REAL* · `alvo_seco_pct` REAL* · `alvo_umido_pct` REAL* · `metodo` TEXT* · `registrada_em` TEXT* · `ativa` INTEGER* (1) · `observacao` TEXT* ('') | — | `idx_calibracao_sonda_no_ativa` (id_no, ativa) |
| `configuracao_sistema` | `database` | `chave` TEXT PK · `valor` TEXT* · `tipo` TEXT* · `descricao` TEXT* · `atualizado_em` TEXT* (datetime('now','localtime')) | — | `sqlite_autoindex_configuracao_sistema_1` (chave; único) |
| `culturas` | `database` | `id_cultura` INTEGER PK · `codigo` TEXT* · `nome_comum` TEXT* · `nome_cientifico` TEXT · `categoria` TEXT* · `descricao` TEXT · `ativo` INTEGER* (1) · `criado_em` TEXT* (datetime('now','localtime')) | — | `sqlite_autoindex_culturas_1` (codigo; único) |
| `eventos_interface` | `retorno_agricultor` | `id_evento` INTEGER PK · `tipo` TEXT* · `instante_utc` TEXT* · `detalhe` TEXT* ('') | — | `idx_eventos_interface_instante` (instante_utc) |
| `historico_agregado_hora` | `database` | `id_cultura` INTEGER* PK · `hora` TEXT* PK · `origem` TEXT* PK · `n` INTEGER* · `temperatura_min` REAL · `temperatura_med` REAL · `temperatura_max` REAL · `umidade_ar_min` REAL · `umidade_ar_med` REAL · `umidade_ar_max` REAL · `umidade_solo_min` REAL · `umidade_solo_med` REAL · `umidade_solo_max` REAL · `status_alerta_max` INTEGER · `n_verde` INTEGER · `n_amarelo` INTEGER · `n_vermelho` INTEGER · `n_anomalias` INTEGER | `id_cultura` → `culturas.id_cultura` ON DELETE NO ACTION | `sqlite_autoindex_historico_agregado_hora_1` (id_cultura, hora, origem; único) |
| `historico_leituras` | `database` | `id_leitura` INTEGER PK · `data_hora` TEXT* · `id_cultura` INTEGER* · `origem` TEXT* · `temperatura_ar_bruta` REAL · `umidade_ar_bruta` REAL · `umidade_solo_bruta` REAL · `temperatura_ar` REAL* · `umidade_ar` REAL* · `umidade_solo` REAL* · `deficit_hidrico_solo` REAL* · `desvio_termico` REAL* · `status_regra` INTEGER* · `status_predito` INTEGER* · `status_alerta` INTEGER* · `probabilidade_predicao` REAL* · `flag_anomalia` INTEGER* (0) · `descricao_anomalia` TEXT* ('') · `mensagem_voz` TEXT* · `versao_modelo` TEXT* · `motivo_regra` TEXT* ('') · `base_temperatura` TEXT* ('') · `temperatura_media_24h` REAL · `umidade_solo_firmware` REAL · `estado_sensor` TEXT* ('OK') · `diagnostico_sensor` TEXT* ('') · `instante_utc` TEXT | `id_cultura` → `culturas.id_cultura` ON DELETE NO ACTION | `idx_historico_cultura_origem_id` (id_cultura, origem, id_leitura)<br/>`idx_historico_cultura_id` (id_cultura, id_leitura)<br/>`idx_historico_cultura_data` (id_cultura, data_hora)<br/>`idx_historico_data_hora` (data_hora) |
| `parametros_cultura` | `database` | `id_parametro` INTEGER PK · `id_cultura` INTEGER* · `variavel` TEXT* · `unidade` TEXT* · `ideal_min` REAL* · `ideal_max` REAL* · `atencao_min` REAL* · `atencao_max` REAL* · `critico_min` REAL* · `critico_max` REAL* · `fonte_referencia` TEXT · `atualizado_em` TEXT* (datetime('now','localtime')) | `id_cultura` → `culturas.id_cultura` ON DELETE CASCADE | `idx_parametros_cultura_id` (id_cultura)<br/>`sqlite_autoindex_parametros_cultura_1` (id_cultura, variavel; único) |
| `pedido_ajuda` | `retorno_agricultor` | `id_pedido` INTEGER PK · `instante_utc` TEXT* · `status` INTEGER · `causa` TEXT* ('') · `temperatura_ar` REAL · `umidade_solo` REAL · `id_leitura` INTEGER | `id_leitura` → `historico_leituras.id_leitura` ON DELETE SET NULL | `idx_pedido_ajuda_instante` (instante_utc) |
| `registro_acoes` | `retorno_agricultor` | `id_acao` INTEGER PK · `id_leitura` INTEGER · `codigo_acao` TEXT* · `causa` TEXT* · `instante_utc` TEXT* · `origem_aparelho` TEXT* ('') | `id_leitura` → `historico_leituras.id_leitura` ON DELETE SET NULL | `idx_registro_acoes_causa_instante` (causa, instante_utc) |

---

## 21. Mudanças da v4

A v4 é de **UX de campo** ([AVALIACAO_UX.md](AVALIACAO_UX.md)). Banco, CSVs, `.pkl` e protocolo serial ficam iguais
aos da v3, com duas colunas a mais.

### 21.1 Coluna `demonstracao` em `pedido_ajuda` e `eventos_interface`

`database.migrar_colunas_demonstracao` (chamada por `inicializar_banco` e por `retorno_agricultor.garantir_tabelas`)
acrescenta, se ainda não existir, `demonstracao INTEGER NOT NULL DEFAULT 0`. Rodar duas vezes num banco da v3 não
falha (`tests/test_retorno_v4.py`).

| Tabela | Coluna | Valor | Quem grava |
|---|---|---|---|
| `pedido_ajuda` | `demonstracao` | 1 quando o pedido foi feito com `modo_operacao = 'DEMONSTRACAO'` | `registrar_pedido_ajuda` |
| `eventos_interface` | `demonstracao` | 1 quando a configuração `modo_operacao` do banco é `DEMONSTRACAO` (inclui o evento `feito` da banca) | `registrar_evento` |

`montar_resumo_tecnico(..., incluir_demonstracao=False)` e `montar_texto_whatsapp` excluem essas linhas por padrão e
as contam em `demonstracao_excluida`. `registro_acoes` **não** ganhou coluna: o "Feito" da demonstração é reconhecido
pela leitura a que está ligado (`historico_leituras.origem = 'DEMONSTRACAO'`, via `LEFT JOIN`), e o "Feito" sem
`id_leitura` tocado durante a demonstração passa a ser ligado à leitura da banca (correção da integração da v4).

Esquema conferido por `PRAGMA table_info` num banco criado do zero (16/11/2026): 10 tabelas da aplicação +
`sqlite_sequence`; `pedido_ajuda` com 8 colunas e `eventos_interface` com 5; `configuracao_sistema` com 36 chaves logo
depois da criação (as mesmas da v3).

### 21.2 `registro_acoes`: desfazer e proteção contra duplicata

| Regra | Constante | Efeito |
|---|---|---|
| Desfazer | `JANELA_DESFAZER_SEGUNDOS = 120` | `desfazer_acao` apaga a linha (e com ela a pausa de `causa_silenciada`); depois de 120 s levanta `JanelaDesfazerExpirada` (HTTP 409); id inexistente levanta `RegistroInexistente` (HTTP 404) |
| Sem duplicata | `JANELA_REPETICAO_SEGUNDOS = 60` | `registrar_acao_sem_repetir` devolve `(id_existente, True)` para a mesma causa, `id_leitura` e `codigo_acao` em menos de 60 s (HTTP 200 `{"repetido": true}`); senão grava (HTTP 201) |
| Ids válidos | `MAIOR_ID_SQLITE = 2**63 − 1` | `id_acao`/`id_leitura` acima disso dão 400, não 500 |

### 21.3 Contrato JSON: campos novos e rotas

`GET /api/status` (53 campos, eram 51):

| Campo | Tipo | Exemplo | Significado |
|---|---|---|---|
| `fala_curta` | `str` | `"Sinal vermelho. Regue agora. A terra está seca."` | O que o 1º toque fala: cor, até 3 ações e a causa, ≤ 15 palavras, sem números (`acoes_orientacao.montar_fala_curta`). Com dado de teste: `"Atenção. Dados de teste. Não siga esta cor. Confira o aparelho."`; antiga: `"Atenção. Medição antiga. Confira o aparelho."`; sem leitura: `"Atenção. Sem medição. Confira o aparelho."`. `mensagem_voz` não mudou |
| `aparelho_da_banca` | `bool` | `false` | `true` só no aparelho (mesmo IP) que ligou a demonstração; nos outros a página mostra a demonstração como **neutra** (semáforo apagado, sem alarme) |

A cor (`status_alerta` 0/1/2) continua a mesma para a mesma leitura: o estado "neutro" é só apresentação no navegador.

Rotas e respostas novas ou alteradas:

| Rota | Mudança |
|---|---|
| `POST /api/acao_feita` | `201 {"id_acao"}` na 1ª vez; `200 {"id_acao", "repetido": true}` para o mesmo toque em < 60 s; `400` para id fora do intervalo do SQLite |
| `POST /api/acao_feita/desfazer` | **nova**. Corpo `{"id_acao": int}`; `200 {"desfeito": true}`, `404`, `409 {"erro": "Passou o tempo de desfazer."}`, `400`, `429` (mesmo limitador do "Feito") |
| `GET /static/agro.<hash>.css` | **nova**. CSS não crítico do agricultor (`src/templates/agricultor.css` minificado), hash sha256 de 10 caracteres; `Cache-Control: public, max-age=31536000, immutable`; gzip; outro hash → 404 |
| `GET /favicon.svg` | **nova**. Ícone da aba, servido localmente (página 100 % offline) |
| `GET /api/culturas` | `ETag` + `Cache-Control: no-cache`; `If-None-Match` igual → `304` sem corpo. Cada cultura traz `icone` próprio (`#i-cafe-arabica`, `#i-cafe-conilon`; outras pela categoria) |
| Rotas com PIN (`X-PIN-Banca`) | `403 {"codigo": "pin_necessario"}` sem PIN; `403 {"erro": "Senha errada. Tente de novo.", "codigo": "pin_errado"}` com PIN errado; `429 {"segundos_restantes": 1–60}` no bloqueio |
| `POST /api/banca/seca_programada`, `POST /api/banca/florada` | mesmo contrato; os botões saíram da banca e estão no Calendário do café de `/tecnico` |

### 21.4 Arquivos novos

| Arquivo | Conteúdo |
|---|---|
| `src/templates/agricultor.css` | CSS não crítico da página do agricultor (o crítico continua inline); servido em `/static/agro.<hash>.css` |
| `tests/dados/pictogramas_iso9186.csv` | Gabarito ISO 9186-1: `codigo`, `simbolo`, `significado_pretendido`, `resposta_correta_exemplo`, `resposta_oposta_critica`, `n_amostra`, `pct_correta`, `pct_oposta`, `aprovado`. 19 linhas (10 ações, 3 selos de "quando", 6 causas); resultados vazios até o teste de campo ([protocolo](PROTOCOLO_TESTE_USUARIOS.md)) |
| `tests/e2e/validacao/metas_v4.json` | 70 metas da suíte UX (`id`, `item`, `descricao`, `indicador`, `operador`, `limiar`, `baseline_v3`) |
| `reports/ux4/<data>/resultado.json` | relatório da suíte: `commit`, `modo`, `ferramentas`, `total_metas`, `atendidas`, `falhas`, `pulados`, `metas[]` (com `valor` e `status`), `indicadores`, `detalhes`, `capturas_das_falhas` |
| `package.json` | dependências de desenvolvimento fixas (Playwright, axe-core, Lighthouse) e os scripts `test:ux` e `validar:ux` |
| `docs/ux/*.png` | capturas da avaliação de UX (≤ 150 KB cada) |

