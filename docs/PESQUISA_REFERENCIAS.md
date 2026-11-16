# Pesquisa e Referências — AgroAnálise v3 e v4

> Revisão de literatura que sustentou a versão 3.0.0 (15/11/2026), mais as referências de UX de campo da versão
> 4.0.0 (16/11/2026, [seção 11](#11-ux-de-campo-da-v4-referências-novas)). Para cada referência: quem publicou, quando, onde
> está, que tipo de fonte é, se foi **lida na íntegra**, se foi **conferida pelo comitê de checagem**, o que ela diz
> (com os números) e **o que mudou no AgroAnálise por causa dela**. Toda referência tem URL; fonte sem endereço público
> não entrou na lista.
>
> Documentos irmãos: [CHANGELOG](../CHANGELOG.md#300--2026-11-15) · [Workflow](WORKFLOW.md#11-regra-térmica-da-v3-ia-em-dado-real-e-retorno-do-agricultor) ·
> [Dicionário de Dados](DICIONARIO_DADOS.md#20-mudanças-da-v3) · [Mapeamento de Funções](MAPEAMENTO_FUNCOES.md) · [README](../README.md).

## Sumário

1. [Como a pesquisa foi feita e como ler esta página](#1-como-a-pesquisa-foi-feita-e-como-ler-esta-página)
2. [Decisão sobre as regras agronômicas](#2-decisão-sobre-as-regras-agronômicas)
3. [Agronomia e agrometeorologia](#3-agronomia-e-agrometeorologia)
4. [Protótipos IoT e sensores](#4-protótipos-iot-e-sensores)
5. [Inteligência artificial e dados](#5-inteligência-artificial-e-dados)
6. [UX para baixo letramento e ICT4D](#6-ux-para-baixo-letramento-e-ict4d)
7. [Análises próprias em dado real](#7-análises-próprias-em-dado-real)
8. [Rastreabilidade: cada mudança da v3 e a fonte que a sustenta](#8-rastreabilidade-cada-mudança-da-v3-e-a-fonte-que-a-sustenta)
9. [Ideias avaliadas e descartadas](#9-ideias-avaliadas-e-descartadas)
10. [Próximos passos de pesquisa](#10-próximos-passos-de-pesquisa)
11. [UX de campo da v4: referências novas](#11-ux-de-campo-da-v4-referências-novas)

---

## 1. Como a pesquisa foi feita e como ler esta página

A v3 partiu de seis limitações conhecidas da v2 (CHANGELOG, "Limitações conhecidas da 2.0.0"). A principal: com o
simulador e as faixas do cliente, **35–53 % das horas de junho a agosto ficavam Vermelhas no Café Arábica**. Quatro
frentes de pesquisa trabalharam em paralelo (agronomia, protótipos IoT, IA aplicada à agricultura, UX/ICT4D). Cada
achado trouxe a fonte, a implicação para o AgroAnálise, a lacuna no código (arquivo e linha) e uma proposta. Depois
um **comitê de checagem** tentou refutar os achados que iriam mudar o comportamento do sistema: buscou a fonte de novo,
de forma independente, e reexecutou as análises com o código e os dados reais do repositório.

**Limite de acesso.** Nesta sessão vários domínios estavam fora da rede (fao.org, embrapa.br, gov.br, scielo.br,
PMC/MDPI, arXiv, alice.cnptia, livrariaepamig, meetingorganizer, archive-api.open-meteo.com, portal do INMET). Por
isso muitas fontes foram lidas **pelo resumo e pelos trechos da busca**, e isso está dito em cada uma. Código aberto,
datasets e READMEs do GitHub/CRAN foram lidos na íntegra. Os números que mudaram regras do sistema foram conferidos
pelo comitê ou medidos pelo próprio projeto em dado real (seção 7).

Legenda dos campos:

| Campo | Valores |
|---|---|
| **Leitura** | *íntegra* (texto, código ou dado completo) · *resumo/trechos* (resumo, registro bibliográfico ou trechos da busca) |
| **Comitê** | *confirmada* · *parcialmente confirmada* (o que não foi confirmado está dito) · *não conferida* (só o pesquisador leu; não sustenta sozinha nenhum número do sistema) |
| **Efeito na v3** | o que entrou no código ou na documentação; "nenhum (descartada)" remete à [seção 9](#9-ideias-avaliadas-e-descartadas) |

Credibilidade usada na triagem: documento técnico oficial, norma, artigo revisado por pares e dataset/código aberto
com documentação = alta; extensão rural, imprensa técnica e preprint = média; blog e matéria sem revisão = baixa.

---

## 2. Decisão sobre as regras agronômicas

### 2.1 O problema

A v2 comparava **cada leitura horária** de temperatura com a faixa do cliente (Arábica: ideal 18–22 °C, atenção
15–25 °C, crítico 8–32 °C; Conilon: 22–26 / 19–29 / 12–34 °C). Fora da faixa de atenção a leitura era Vermelha.

### 2.2 O que a literatura diz sobre esses números

- **São critérios de aptidão do local, por temperatura MÉDIA ANUAL** do zoneamento agrícola. Para o Arábica, "apto sem
  restrição" é temperatura média anual de 18 a 22 °C, deficiência hídrica anual de 0 a 100 mm e probabilidade de
  geada até 25 % ([A1](#a1)). Para o Conilon, temperatura média anual de 22 a 26 °C, altitude abaixo de 500 m e
  deficiência hídrica abaixo de 200 mm ([A2](#a2)). Comparar uma leitura de 3 h da madrugada com uma média anual
  confunde **clima** com **tempo**.
- **O dano real ao cafeeiro é fisiológico e tem limiares próprios:** geada com abrigo abaixo de 2 °C (relva −2 a
  −3 °C; [A4](#a4)) e máxima a partir de 34 °C na florada, que aborta flores ([A6](#a6)). Frio sem geada reduz o
  crescimento, mas é reversível e esperado no repouso de inverno ([A7](#a7)).
- **Alarme falso demais ensina a desobedecer** o alarme ([U2](#u2)).

### 2.3 O que medimos

Em dado real (ERA5-Land, Varginha/MG, 2018–2021, média anual de 20,4 °C, ou seja, **local apto**), a regra da v2
pintava **27,6 % do ano e 37,7 % do inverno de Vermelho**, com 1.842 trocas de cor por ano (cerca de 5 por dia).
Aplicar só a média de 24 h com Vermelho fora da atenção ainda deixaria 12,5 % das horas de inverno Vermelhas (a média
de 24 h fica abaixo de 15 °C nessas horas). Detalhes na [seção 7](#7-análises-próprias-em-dado-real).

### 2.4 A decisão

Os números do cliente **continuam cadastrados como estão** em `parametros_cultura` e continuam sendo a **faixa ideal**.
Nenhuma linha mudou. O que mudou é a **forma de aplicar a temperatura**, no modo `fisiologica`
(`configuracao_sistema.regra_temperatura`); o solo continua instantâneo. Status = máximo dos componentes, como antes.

| # | Componente | Regra `fisiologica` | Cor máxima | Fonte |
|---|---|---|---|---|
| 1 | Conforto (faixa do cliente) | faixa sobre a **média móvel de 24 h** (≥ 18 h com dado; senão a leitura); motivos `DIA_FRIO`/`DIA_QUENTE`, voz "Não precisa fazer nada agora" | **Amarelo** | [A1](#a1), [A2](#a2), [A7](#a7) |
| 2 | Geada | abrigo ≤ `limiar_geada_abrigo_c` (2,0 °C) + `incerteza_sensor_ar_c` (DHT11 2,0 °C; DHT22 0,5; SHT3x 0,2) → **≤ 4,0 °C no DHT11**; `GEADA_AGORA`, sem banda morta | Vermelho | [A4](#a4), [P8](#p8) |
| 3 | Calor | máxima do dia ≥ `critico_max` → Amarelo `CALOR_DIA`; máxima ≥ max(34 °C, `critico_max`) **e** leitura ≥ `critico_max` → Vermelho na florada (`CALOR_FLORADA`: data registrada + 45 dias, ou set.–nov.) ou no 2º dia seguido (`CALOR_SEGUIDO`) | Vermelho | [A6](#a6), [A9](#a9) |
| 4 | Frio sem geada | o antigo `critico_min` (8 / 12 °C) deixa de gerar Vermelho e de ser chamado de geada | Amarelo | [A7](#a7) |
| 5 | Seca programada (opcional, desligada) | na janela 24/06–04/09, solo entre o crítico e o ideal → `SECA_PROGRAMADA`, "Não regue ainda"; abaixo do crítico do solo continua Vermelho (nenhum número novo) | Amarelo | [A8](#a8) |
| 6 | Status gravado | banda morta de 3 % da faixa crítica + off-delay de 60 s só para **melhorar**; piorar é imediato | — | [I11](#i11), [P8](#p8) |

Premissa de instalação: o sensor de ar fica **em abrigo ventilado a 1,5 m do chão**, como no abrigo meteorológico,
porque o limiar de geada é de temperatura do abrigo ([A4](#a4), [A5](#a5), [P10](#p10)).

**Compatibilidade.** Chave ausente = `instantanea` (regra da v2); os 179 testes antigos criam bancos sem a chave e
continuam verdes sem alteração de regra. O coletor semeia `fisiologica` ao iniciar; o técnico pode voltar com
`definir_configuracao('regra_temperatura', 'instantanea')`. A banca (`DEMONSTRACAO`) e o `DATASET` usam sempre a regra
instantânea. Na regra fisiológica a IA do instante recebe a temperatura no ponto médio do ideal, para não trazer o
Vermelho térmico de volta pelo `max(regra, IA)`; a IA de prognóstico foi retreinada com o rótulo fisiológico.

**Efeito medido** (`python scripts/medir_impacto_regra.py`): Arábica em dado real, Vermelho 27,6 % → **0,1 %** do ano
(inverno 37,7 % → 0,0 %), Verde 37,4 % → 48,4 %, trocas de cor 1.842 → 119 por ano; Conilon, Vermelho 16,7 % → 0,3 %,
trocas 1.580 → 120. **Custo assumido:** mais Amarelo *informativo* no inverno do Arábica (julho: 35,1 % → 69,9 % das
horas), que é a verdade agronômica — dia abaixo do ideal — sem pedir ação de emergência.

**O que ainda precisa de validação humana:** o limiar de geada no Conilon (mantido igual ao Arábica; a espécie é mais
sensível ao frio, [A3](#a3)), a janela de florada padrão e a seca programada precisam de um agrônomo da região antes
do piloto (ver [seção 10](#10-próximos-passos-de-pesquisa)).

---

## 3. Agronomia e agrometeorologia

<a id="a1"></a>
### A1. Portaria SPA/MAPA nº 58/2005 — Zoneamento agrícola do café arábica

- **Instituição · ano · tipo:** Ministério da Agricultura (MAPA), cópia da CETESB · 2005 · documento técnico oficial
- **URL:** <https://licenciamento.cetesb.sp.gov.br/legislacao/federal/portarias/2005_Port_Fed_58.pdf>
- **Leitura:** resumo/trechos (o PDF não abriu nesta sessão) · **Comitê:** confirmada
- **O que diz:** municípios "aptos sem restrição" têm **temperatura média anual entre 18 °C e 22 °C**, deficiência
  hídrica média anual de 0 a 100 mm e probabilidade de geada ≤ 25 %; 22–23 °C = apto com restrição térmica; 22–23 °C
  com deficiência hídrica de 100–150 mm = restrição térmica e hídrica. Ressalva do comitê: a portaria vale para São
  Paulo na safra 2005/06; a portaria de Goiás (75/2011) usa 18–23 °C, mas também como **média anual**. A Embrapa
  Cerrados (Comunicado Técnico 36, citado pelo pesquisador) chama a deficiência hídrica e a temperatura média anual de
  "parâmetros fundamentais para estudos de aptidão climática".
- **Efeito na v3:** é o fundamento da regra `fisiologica`: a faixa do cliente passou a ser comparada com a média
  móvel de 24 h e só pode dar Amarelo (`src/regras_agronomicas.py`, `avaliar_status_campo`). A faixa cadastrada não
  mudou.

<a id="a2"></a>
### A2. Incaper — Tecnologias para o café conilon (critérios de zoneamento)

- **Instituição · ano · tipo:** Instituto Capixaba de Pesquisa, Assistência Técnica e Extensão Rural (Incaper) · 2016 ·
  documento técnico oficial
- **URL:** <https://biblioteca.incaper.es.gov.br/digital/bitstream/item/5052/1/tecnologiasconilon.pdf>
- **Leitura:** resumo/trechos · **Comitê:** confirmada (vários materiais do Incaper e a Circular Técnica 6 da Embrapa)
- **O que diz:** "Altitude inferior a 500 m; Temperatura média anual 22 a 26 °C; Deficiência hídrica inferior a
  200 mm". Nuances: déficits de 150–200 mm são aptos com irrigação ocasional; o limite de 500 m é flexível. Partelli e
  Bonomo (2016), citados num artigo de 2025, ampliam para 22–27,5 °C.
- **Efeito na v3:** mesma decisão do Arábica (faixa do cliente na média de 24 h, no máximo Amarelo). A proposta de um
  Amarelo de "frio prolongado" com média diária < 15 °C por 3 dias foi **descartada** por falta de fonte primária
  para o número.

<a id="a3"></a>
### A3. Rodrigues et al. — Aclimatação do *Coffea canephora* a baixas temperaturas

- **Autores · ano · tipo:** Rodrigues et al., *Australian Journal of Crop Science* 10(5):666-674 · 2016 · artigo científico
- **URL:** <https://cropj.com/rodrigues_10_5_2016_666_674.pdf>
- **Leitura:** resumo/trechos · **Comitê:** não conferida
- **O que diz:** queda de fotossíntese e de crescimento abaixo de cerca de 18–20 °C; *C. canephora* tem "baixa
  capacidade de aclimatação a baixas temperaturas". Um estudo da Embrapa (citado) mostrou clones de Conilon mais
  sensíveis ao frio que o Catucaí IPR 102.
- **Efeito na v3:** nenhum número novo. Motivou manter no Conilon o mesmo limiar de geada do Arábica (o mais
  conservador disponível) e listar a validação com o Incaper como próximo passo.

<a id="a4"></a>
### A4. Epamig e Embrapa Café — orientação técnica sobre geadas

- **Instituição · ano · tipo:** pesquisadores da Epamig e da Embrapa Café, via Revista Cultivar · 2021 · documento
  técnico de extensão
- **URL:** <https://revistacultivar.com.br/noticias/pesquisadores-da-epamig-e-embrapa-cafe-orientam-cafeicultores-sobre-o-que-fazer-apos-as-geadas>
- **Leitura:** resumo/trechos · **Comitê:** parcialmente confirmada
- **O que diz:** a geada "se dá quando a temperatura é menor que **2 °C no abrigo** e **−2 a −3 °C na relva**"; "para o
  cafeeiro a temperatura letal é menor que −2 °C"; material da Embrapa Café: folhas entre −3 e −4 °C morrem. A
  diferença média abrigo–relva é de cerca de 4 °C (Sentelhas et al., 1995, citado numa circular da Epamig).
  **Não confirmado:** o critério "≤ 3 °C a 2 m" atribuído ao IDR-Paraná; por isso o sistema usa 2 °C da
  Epamig/Embrapa mais a incerteza do sensor, e não 3 °C.
- **Efeito na v3:** chave `limiar_geada_abrigo_c = 2.0`; Vermelho `GEADA_AGORA` com leitura ≤ 2,0 + incerteza do
  sensor (≤ 4,0 °C no DHT11); o aviso de geada da tarde passou a usar o mesmo limiar
  (`prognostico.aplicar_antecipacao`). Na v2 a "geada" disparava abaixo de 8 °C no Arábica e 12 °C no Conilon.

<a id="a5"></a>
### A5. ESALQ/USP — LCE 306 Meteorologia Agrícola, Aula 12 (Geada)

- **Instituição · ano · tipo:** Escola Superior de Agricultura Luiz de Queiroz (USP) · 2012 · material didático técnico
- **URL:** <https://www2.esalq.usp.br/departamentos/leb/aulas/lce306/Aula12_2012.pdf>
- **Leitura:** resumo/trechos · **Comitê:** não conferida isoladamente (coerente com [A4](#a4))
- **O que diz:** diferença média abrigo–relva de cerca de 4 °C; em noite de geada a relva chega a −2 °C com o abrigo
  em 2 °C; a geada de radiação (noite limpa, sem vento) é a mais comum. Um estudo de 2024 em Minas Gerais (citado)
  achou diferenças médias de até 2,6 °C.
- **Efeito na v3:** sustenta a premissa de instalação **em abrigo a 1,5 m** (README, Dicionário 20.1). Um sensor de
  relva (DS18B20) ficou como próximo passo.

<a id="a6"></a>
### A6. Embrapa — "Temperaturas elevadas" (CBAGRO 2003)

- **Autores · ano · tipo:** Iaffe, Pinto et al. (Embrapa), anais do Congresso Brasileiro de Agrometeorologia · 2003 ·
  artigo científico
- **URL:** <https://www.alice.cnptia.embrapa.br/alice/bitstream/doc/8420/1/PC-Temperaturas-elevadas-CBAGRO-2003.pdf>
- **Leitura:** resumo/trechos · **Comitê:** confirmada (também em Camargo 1985 e Sediyama et al. 2001, citados)
- **O que diz:** **34 °C** de máxima é o limiar de dano; conta-se a frequência de dias com máxima ≥ 34 °C, sobretudo
  de setembro a novembro (período principal de florescimento); com deficiência hídrica, "causa abortamento de flores"
  ("estrelinhas"); na antese o calor desidrata o tubo polínico; há probabilidade de 3 a 4 dias seguidos ≥ 34 °C.
  Ressalva: fontes de 2001–2008 e do Arábica.
- **Efeito na v3:** chave `limiar_calor_dano_c = 34.0`; motivos `CALOR_FLORADA` (Vermelho no 1º dia na florada),
  `CALOR_SEGUIDO` (fora da florada, 2º dia seguido) e `CALOR_DIA` (Amarelo); no Conilon vale max(34, `critico_max`) =
  34 °C. Voz: "Calor forte na florada. Regue no fim da tarde e faça sombra nas mudas."

<a id="a7"></a>
### A7. DaMatta et al. — Ecophysiology of coffee growth and production

- **Autores · ano · tipo:** DaMatta, Ronchi, Maestri e Barros, *Brazilian Journal of Plant Physiology* · 2007 · artigo
  de revisão
- **URL:** <https://scielo.br/j/bjpp/a/xSBXnvrDDczyDbBtzspGs3v>
- **Leitura:** resumo/trechos (scielo.br fora da rede) · **Comitê:** não conferida
- **O que diz:** com média anual abaixo de 17–18 °C o crescimento é "largamente deprimido"; temperaturas positivas
  baixas (em geral < 10 °C) afetam a fotossíntese, que quase cessa entre 5 e 10 °C; uma noite de 12 h a ~6 °C reduz a
  assimilação no dia seguinte. É estresse reversível, sem ação imediata possível, ao contrário da geada.
- **Efeito na v3:** o frio sem geada virou **Amarelo informativo** (`DIA_FRIO`, "O dia está frio para o café. Não
  precisa fazer nada agora."), e o antigo `critico_min` deixou de gerar Vermelho. O Vermelho térmico ficou reservado à
  geada e ao calor na florada.

<a id="a8"></a>
### A8. Guerra, Rocha, Rodrigues et al. (Embrapa Cerrados) — manejo estratégico da irrigação do café

- **Instituição · ano · tipo:** Embrapa Cerrados (Planaltina-DF) · 2005 (e estudo de 2011) · artigo científico
- **URL:** <https://sbicafe.ufv.br/items/46703cd6-1804-4062-841a-3af5714ec5ac> e
  <https://www.alice.cnptia.embrapa.br/handle/doc/1180974?locale=en>
- **Leitura:** resumo/trechos · **Comitê:** parcialmente confirmada
- **O que diz:** suspender a irrigação de **24 de junho a 4 de setembro** sincroniza a florada e "pode ser usada como
  referência para as principais regiões cafeeiras do Cerrado". O estudo de 2011 mostra que estresse fraco ou curto não
  sincroniza e que estresse forte ou longo prejudica (−3,4 MPa foi severo). **Não confirmados:** o limiar de retomada
  de −2,0 MPa e os "> 80 % de cereja".
- **Efeito na v3:** **seca programada** opcional e desligada por padrão (`seca_programada_ativa`, janela
  `06-24`→`09-04`, botão na banca). Dentro da janela o solo abaixo do ideal é Amarelo "Não regue ainda"; o piso de
  segurança é o `critico_min` do solo já cadastrado — nenhum número novo foi inventado.

<a id="a9"></a>
### A9. Camargo e Camargo — Definição e esquematização das fases fenológicas do cafeeiro arábica

- **Autores · ano · tipo:** A. P. Camargo e M. B. P. Camargo, *Bragantia* 60(1):65-68 · 2001 · artigo científico
- **URL:** <https://sbicafe.ufv.br/items/91864e14-fe7e-44f4-ad81-fa64b696969d>
- **Leitura:** resumo/trechos · **Comitê:** não conferida isoladamente (a janela de florada foi confirmada em [A6](#a6))
- **O que diz:** o Arábica completa o ciclo em dois anos fenológicos, com seis fases que começam em setembro; florada,
  chumbinho e expansão são as fases em que o déficit severo prejudica o grão.
- **Efeito na v3:** janela de florada padrão de **setembro a novembro** quando a data não foi registrada; botão "O café
  floriu hoje" grava `data_florada_principal` (janela de 45 dias).

<a id="a10"></a>
### A10. Pezzopane et al. — Exigência térmica do café arábica cv. Mundo Novo

- **Autores · ano · tipo:** Pezzopane et al., *Ciência e Agrotecnologia* (UFLA) · 2008 · artigo científico
- **URL:** <https://sbicafe.ufv.br/items/e6b3e293-5d7c-4194-a07a-6eec74c6497b>
- **Leitura:** resumo/trechos · **Comitê:** confirmada
- **O que diz:** temperatura-base e soma térmica da florada à colheita de **10,2 °C e 2.887 graus-dia** (sem correção
  hídrica) e **10,5 °C e 2.761 graus-dia** (com fator hídrico). O comitê resolveu a inversão de ordem que aparecia nas
  versões pt/en do resumo.
- **Efeito na v3:** nenhum no código (fenologia por graus-dia descartada nesta rodada, [seção 9](#9-ideias-avaliadas-e-descartadas));
  referência para a previsão de colheita falada.

<a id="a11"></a>
### A11. FAO-56, Tabela 12 — coeficiente de cultura (Kc) do café

- **Instituição · ano · tipo:** FAO (Allen, Pereira, Raes e Smith), *Irrigation and Drainage Paper 56*, via dataset
  `Kc_Tropical_Fruits_and_Trees` do pacote R FAO56 no CRAN · 1998 · dataset
- **URL:** <https://raw.githubusercontent.com/cran/FAO56/master/data/Kc_Tropical_Fruits_and_Trees.rda>
- **Leitura:** íntegra (o `.rda` foi decodificado) · **Comitê:** confirmada (decodificado de novo)
- **O que diz:** `Coffee_bare_ground_cover` = Kc 0,90 / 0,95 / 0,95; `Coffee_with_weeds` = 1,05 / 1,10 / 1,10.
  Tabela 22 (confirmada pela busca): raiz do café de 0,9–1,5 m e fração de depleção sem estresse **p = 0,40**.
- **Efeito na v3:** nenhum no código (balanço hídrico descartado nesta rodada). Ajudou a interpretar a escala: se a
  sonda for calibrada entre ponto seco crítico e capacidade de campo, o ideal de 60 % do Arábica coincide com p = 0,40.

<a id="a12"></a>
### A12. PyETo — Hargreaves-Samani (FAO-56, equação 52)

- **Autor · ano · tipo:** Mark Richards (woodcrafty), implementação aberta da FAO-56 · 2015 · protótipo open source
- **URL:** <https://raw.githubusercontent.com/woodcrafty/PyETo/master/pyeto/fao.py>
- **Leitura:** íntegra · **Comitê:** confirmada (fórmula); a superestimação no Sudeste **não** foi conferida
- **O que diz:** ETo = 0,0023 × (Tmed + 17,8) × (Tmax − Tmin)^0,5 × 0,408 × Ra, com Ra pela latitude e pelo dia do ano
  (FAO-56 eq. 21). Estudos brasileiros (resumos) indicam superestimação sem calibração e recomendam coeficiente
  calibrável. No simulador deu 2,0 mm/d (jun.) a 3,7 mm/d (fev.).
- **Efeito na v3:** nenhum (descartada até haver calibração contra estação do INMET); próximo passo.

<a id="a13"></a>
### A13. pyfao56 — balanço hídrico FAO-56 (USDA-ARS)

- **Autores · ano · tipo:** K. Thorp (USDA-ARS), *SoftwareX* · 2022 · protótipo open source
- **URL:** <https://raw.githubusercontent.com/kthorp/pyfao56/main/src/pyfao56/model.py>
- **Leitura:** íntegra · **Comitê:** confirmada
- **O que diz:** `p = sorted([0.1, pbase + 0.04*(5.0 − ETc), 0.8])[1]`; RAW = p·TAW (eq. 83); Ks =
  (TAW − Dr)/(TAW − RAW) limitado a [0, 1] (eq. 84); TAW = 1000·(θCC − θPMP)·Zr (eq. 82).
- **Efeito na v3:** nenhum no código; base da calibração por capacidade de campo e da frase "água para uns N dias"
  (próximos passos).

<a id="a14"></a>
### A14. Zoneamento agroclimático do café — balanço hídrico sequencial (Thornthwaite-Mather)

- **Instituição · ano · tipo:** Embrapa (anais) · 2009 · artigo científico
- **URL:** <https://www.alice.cnptia.embrapa.br/alice/bitstream/doc/1151790/1/PC-Zoneamento-agroclimatico-cafe-2009.pdf>
- **Leitura:** resumo/trechos · **Comitê:** não conferida
- **O que diz:** o zoneamento do café usa Thornthwaite-Mather com a planilha de Rolim et al. (1998) e CAD de 100 mm;
  um trabalho de Piracicaba conclui que o balanço climatológico "não substitui o balanço hídrico realizado com medidas
  diretas no campo".
- **Efeito na v3:** nenhum (descartada nesta rodada).

<a id="a15"></a>
### A15. UFLA/CONBEA — resposta do cafeeiro irrigado a tensões de água no solo

- **Instituição · ano · tipo:** UFLA, anais do XXXV CONBEA (repositório da UFCG) · 2006 · artigo científico
- **URL:** <https://bdtd.ufcg.edu.br/jspui/bitstream/riufcg/41653/1/RESPOSTA%20DO%20CAFEEIRO%20IRRIGADO%20-%20XXXV%20CONBEA%20UFCG%202006.pdf>
- **Leitura:** resumo/trechos · **Comitê:** não conferida
- **O que diz:** irrigar quando a tensão a 25 cm chega a 20, 60, 100 e 140 kPa; as maiores produtividades em Lavras
  (2004) vieram de 20 kPa e do Sisda; a *Coffee Science* (2014) diz que se adotam "tensões na faixa de 40 kPa".
- **Efeito na v3:** nenhum (variável de tensão em kPa descartada por falta de hardware).

---

## 4. Protótipos IoT e sensores

<a id="p1"></a>
### P1. Adla et al. — Calibração de sensores de solo capacitivos e resistivos de baixo custo

- **Autores · ano · tipo:** Adla, Rai, Karumanchi et al., *Sensors* 20(2):363 · 2020 · artigo científico
- **URL:** <https://pmc.ncbi.nlm.nih.gov/articles/PMC7014303>
- **Leitura:** resumo/trechos · **Comitê:** coerente com a checagem da calibração ([P2](#p2), [P3](#p3)); não conferida isoladamente
- **O que diz:** com calibração específica do solo, capacitivos de baixo custo empataram com um sensor padrão
  secundário (SM100, MAE de 1,67 % de umidade volumétrica); os resistivos muito baratos (YL69, YL100 — o mesmo garfo
  do AgroAnálise) foram menos exatos e menos precisos, "razoáveis pelo preço"; os autores recomendam usá-los junto com
  sensores mais exatos, e não sozinhos.
- **Efeito na v3:** calibração da sonda **no host** (`src/calibracao_sonda.py`), sem regravar o Arduino; README
  recomenda o sensor capacitivo como evolução.

<a id="p2"></a>
### P2. Strypsteen et al. — Desempenho em campo e estratégias de calibração de sensores capacitivos baratos

- **Autores · ano · tipo:** Strypsteen et al. (Wageningen University/KU Leuven), *Sensors* 26(11):3291 · 2026 · artigo
  científico
- **URL:** <https://research.wur.nl/en/publications/field-performance-and-calibration-strategies-for-low-cost-capacit/>
- **Leitura:** resumo/trechos · **Comitê:** confirmada (WUR e registro da Lund University); o procedimento exato do
  ajuste de 1 ponto não foi confirmado
- **O que diz:** calibração por solo no laboratório: RMSE 0,002–0,022 m³/m³; a mesma calibração levada ao campo:
  **0,055–0,191**; calibração feita no campo: **0,005–0,036**; ajuste simplificado de **1 ponto no campo: 0,006–0,078**
  (faixa larga, depende do solo). Piora com muita água e com salinidade.
- **Efeito na v3:** assistente de calibração na página do técnico com **dois pontos** (`concluir_dois_pontos`) ou
  **aferição de 1 ponto no campo** (`aferir_um_ponto`, método `UM_PONTO_CAMPO`); a captura recusa janela instável
  (menos de 6 leituras ou desvio-padrão acima de 1 pp); a tela técnica mostra "calibrado há N dias".

<a id="p3"></a>
### P3. Viana — Protocolo para a calibração e a aferição de sensores de umidade de solo

- **Instituição · ano · tipo:** J. H. M. Viana, Embrapa Milho e Sorgo, Boletim de Pesquisa e Desenvolvimento 262 ·
  2024 · documento técnico oficial
- **URL:** <https://www.infoteca.cnptia.embrapa.br/infoteca/bitstream/doc/1170710/1/Protocolo-para-a-calibracao-e-a-afericao-de-sensores-de-umidade-de-solo.pdf>
- **Leitura:** resumo/trechos (registro na Infoteca) · **Comitê:** coerente com a checagem
- **O que diz:** a calibração contra um método-padrão (gravimétrico) é essencial; nas condições brasileiras os dados
  do sensor precisam de ajuste e "dificilmente a equação de um tipo de solo serve com precisão para outro".
- **Efeito na v3:** calibração por sonda (`id_no`), com data e método gravados em `calibracao_sonda`; o valor bruto
  do firmware fica em `umidade_solo_firmware` para recalibrar depois. O protocolo gravimétrico ficou como próximo passo.

<a id="p4"></a>
### P4. rbaron/b-parasite — sensor aberto de solo com sonda alimentada só na medida

- **Autor · ano · tipo:** rbaron (projeto aberto de hardware) · 2023 · protótipo open source
- **URL:** <https://github.com/rbaron/b-parasite>
- **Leitura:** íntegra (código `code/prstlib/src/adc.c` e README) · **Comitê:** não conferida (prática corroborada
  pelo guia da SparkFun, lido pelos trechos)
- **O que diz:** `prst_adc_soil_read` liga a excitação da sonda, espera 30 ms, lê o ADC e desliga; cerca de 3,0 µA em
  sono profundo e mais de 2 anos com uma CR2032. O guia da SparkFun recomenda não alimentar o sensor resistivo o tempo
  todo, por causa da eletrólise.
- **Efeito na v3:** firmware com `AGRO_PINO_ALIMENTACAO_SOLO` e `lerSoloComPulso()`: a sonda recebe tensão só 20 ms por
  medida (antes: 24 h por dia, amostrada a cada 100 ms); padrão `-1` mantém a ligação da v2.

<a id="p5"></a>
### P5. Vinduino — monitoramento de umidade do solo com excitação alternada

- **Autor · ano · tipo:** Reinier van der Lee (Hackaday Prize 2015, OSHWA) · 2015–2019 · protótipo open source
- **URL:** <https://github.com/ReiniervdL/Vinduino>
- **Leitura:** íntegra (README) · **Comitê:** não conferida
- **O que diz:** sensores de gesso em várias profundidades; excitação por pulsos curtos de corrente alternada contra a
  eletrólise; entradas separadas por FETs; enlaces LoRa de mais de 5 milhas.
- **Efeito na v3:** nenhum (polaridade alternada e várias sondas descartadas por falta de hardware para testar).

<a id="p6"></a>
### P6. Sensores capacitivos genéricos com defeito de fábrica

- **Fonte · ano · tipo:** Hackster.io, matéria sobre o trabalho de Martin Uhlmann · cerca de 2023 (não confirmado) ·
  imprensa técnica (credibilidade baixa)
- **URL:** <https://www.hackster.io/news/martin-uhlmann-finds-an-epidemic-of-faulty-soil-moisture-sensors-and-comes-up-with-a-quick-fix-c05153bef67e>
- **Leitura:** resumo/trechos · **Comitê:** não conferida
- **O que diz:** placas v1.2/v2.0 com regulador ausente, NE555 no lugar do TLC555 ou resistor desconectado; o número
  "82 % não funcionam" não tem confirmação independente. No código do b-parasite ([P4](#p4)) os pontos seco e molhado
  são polinômios da tensão de alimentação.
- **Efeito na v3:** nenhum no código; "teste do sensor novo" na banca ficou como próximo passo antes de recomendar o
  capacitivo em escala.

<a id="p7"></a>
### P7. Placidi et al. — Caracterização de sensores capacitivos baratos para redes IoT

- **Autores · ano · tipo:** Placidi et al. (Universidade de Perugia), *Sensors* 20(12):3585 · 2020 · artigo científico
- **URL:** <https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7348898/>
- **Leitura:** resumo/trechos · **Comitê:** não conferida
- **O que diz:** unidades nominalmente idênticas variam entre si; a relação tensão × água só é confiável com
  compactação constante ao redor da sonda (reinstalar desloca a leitura).
- **Efeito na v3:** a calibração é guardada **por sonda** (`id_no`), não por cultura.

<a id="p8"></a>
### P8. Datasheet do DHT11 (Aosong)

- **Instituição · ano · tipo:** Aosong Electronics, cópia da USC · datasheet · norma ou padrão de fabricante
- **URL:** <https://ece-classes.usc.edu/ee459/library/datasheets/DHT11.pdf>
- **Leitura:** resumo/trechos · **Comitê:** confirmada (várias cópias; algumas páginas de distribuidor dizem ±1 °C, o
  valor conservador é ±2 °C)
- **O que diz:** temperatura 0–50 °C com **±2 °C**; umidade 20–90 %UR com ±5 %UR. Fontes secundárias: DHT22 −40 a
  80 °C com ±0,5 °C; SHT3x ±0,2 °C. Na biblioteca da Adafruit (`DHT.cpp`, lida pelo pesquisador) a decodificação do
  DHT22 usa bit de sinal, por isso o tipo é fixado na compilação.
- **Efeito na v3:** `incerteza_sensor_ar_c` somada ao limiar de geada; `PERFIS_SENSOR_AR` (DHT11 0–50 °C ±2,0; DHT22
  −40–80 °C ±0,5; SHT3X −40–125 °C ±0,2) define os limites físicos do saneador; firmware com `AGRO_SENSOR_AR`
  (DHT11/DHT22) e linha de perfil `#AGROANALISE_PERFIL`. Com DHT22, −1,5 °C deixa de ser anomalia e a geada fica em
  2,5 °C.

<a id="p9"></a>
### P9. 3D-PAWS — estação meteorológica impressa em 3D (NCAR/UCAR)

- **Instituição · ano · tipo:** NCAR/UCAR COMET · 2025 · protótipo open source
- **URL:** <https://github.com/3d-paws/3D-PAWS-Particle-FullStation>
- **Leitura:** íntegra (`docs/Sensors.md`, `docs/SystemHealthBits.md`, `docs/LoRaRelay.md`) · **Comitê:** não conferida
- **O que diz:** detecta sensores pelo endereço I2C na inicialização; mantém um registro de saúde em bits; guarda
  observações não enviadas; reinicia a cada ~22 h para recuperar sensores ausentes; repassa nós remotos por LoRa.
- **Efeito na v3:** inspirou a linha de perfil do firmware (o host passa a saber qual sensor está instalado). Detecção
  automática do SHT por I2C e o registro de saúde em bits foram descartados (sem placa para testar).

<a id="p10"></a>
### P10. Holden et al. — Abrigo barato contra radiação para medir a temperatura do ar

- **Autores · ano · tipo:** Holden, Klene, Keefe e Moisen (USDA Forest Service), *Agricultural and Forest Meteorology*
  180:281-286 · 2013 · artigo científico
- **URL:** <https://research.fs.usda.gov/treesearch/44116>
- **Leitura:** resumo/trechos · **Comitê:** confirmada (MAE de 0,99 °C)
- **O que diz:** sensores baratos dentro de um abrigo caseiro tiveram erro médio absoluto de 0,99 °C, igual ao de um
  abrigo comercial; abrigos improvisados erram de 0,8 a 3,0 °C (outro estudo, citado). A WMO/CIMO fixa a altura do
  sensor entre 1,25 e 2 m.
- **Efeito na v3:** a instalação **em abrigo ventilado a 1,5 m** passou a ser premissa explícita do limiar de geada
  (README, Workflow 11.1, Dicionário 20.1). O checklist de instalação na banca ficou como próximo passo.

<a id="p11"></a>
### P11. IFSULDEMINAS — LoRa em estação de sensoriamento de baixo custo

- **Instituição · ano · tipo:** IFSULDEMINAS, Campus Muzambinho (Jornada Científica) · ano não confirmado · artigo
  científico (anais)
- **URL:** <https://josif.ifsuldeminas.edu.br/ojs/index.php/anais/article/download/3355/2403/22885>
- **Leitura:** resumo/trechos · **Comitê:** não conferida
- **O que diz:** enlace LoRa de até 1.290 m num dos cenários, em região cafeeira.
- **Efeito na v3:** nenhum (nó remoto LoRa descartado nesta rodada; esforço grande e sem hardware).

<a id="p12"></a>
### P12. González-Teruel et al. — Sensor de umidade do solo SDI-12 de baixo custo

- **Autores · ano · tipo:** González-Teruel et al., *Sensors* 19(3) · 2019 · artigo científico
- **URL:** <https://www.ncbi.nlm.nih.gov/pmc/articles/PMC6387356/>
- **Leitura:** resumo/trechos · **Comitê:** não conferida
- **O que diz:** a temperatura influenciou o sensor capacitivo em campo, que ainda assim detectou rega e chuva.
- **Efeito na v3:** nenhum (compensação térmica descartada; exige sensor de temperatura do solo).

---

## 5. Inteligência artificial e dados

<a id="i1"></a>
### I1. Open-Meteo open-data — ERA5-Land horário no AWS Open Data

- **Instituição · ano · tipo:** Open-Meteo, redistribuindo Copernicus ERA5-Land (C3S/ECMWF) · 2025–2026 · dataset
- **URL:** <https://github.com/open-meteo/open-data>
- **Leitura:** íntegra (README) e dado baixado · **Comitê:** confirmada (análises reexecutadas sobre os CSVs)
- **O que diz:** ERA5-Land global de 0,1° (~11 km), horário, desde 1950, licença CC BY 4.0, um arquivo por ano; um
  ponto de grade custa poucos KB por leitura parcial. Foram baixados 2018–2021 de Varginha/MG e de São Gabriel da
  Palha/ES (35.064 h por ponto). Ressalva: a reanálise suaviza a geada radiativa de baixada.
- **Efeito na v3:** `referencia_climatica/` (dado offline com atribuição em `LICENCA_E_FONTE.txt`),
  `src/referencia_climatica.py`, **SEÇÃO 7** do relatório de treino, `scripts/medir_impacto_regra.py`, coeficientes da
  mínima e margens conformais por região, climatologia mês × hora em `models/complemento_prognostico.json`.

<a id="i2"></a>
### I2. Open-Meteo — README do projeto (termos da API)

- **Instituição · ano · tipo:** Open-Meteo · 2025–2026 · protótipo open source (AGPLv3; dados CC BY 4.0)
- **URL:** <https://github.com/open-meteo/open-meteo>
- **Leitura:** íntegra · **Comitê:** não conferida separadamente
- **O que diz:** API "free for non-commercial use", sem chave; atribuição obrigatória; acima de 10.000 requisições por
  dia ou uso comercial exige contato. Um estudo MDPI (2025, citado) acha o ERA5-Land "excelente" para a temperatura
  mínima no Brasil, com vieses na máxima. Não há verificação publicada da previsão de geada contra o INMET.
- **Efeito na v3:** previsão do tempo online **descartada** nesta rodada (licença não comercial a decidir com o
  cliente, API fora da rede para validar); a atribuição do dado offline segue o README.

<a id="i3"></a>
### I3. Talsma et al. — Previsão de geada com aprendizado de máquina

- **Autores · ano · tipo:** Talsma et al., *Frontiers in Artificial Intelligence* · 2023 · artigo científico
- **URL:** <https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2022.963781/full>
- **Leitura:** resumo/trechos · **Comitê:** os números do artigo **não** foram conferidos; a validação própria abaixo foi
  confirmada
- **O que diz:** RF, DNN e CNN para a mínima com até 48 h, a partir de sensores IoT pontuais; abaixo de 24 h as
  próprias temperaturas do ar dominam. **Validação própria (Varginha 2021, 364 noites):** os coeficientes ajustados no
  simulador erram MAE 3,30 °C com viés −2,93 °C (121 noites de alarme para 10 eventos < 8 °C); `COEF_TMIN_PADRAO` erra
  1,37 °C; a persistência ("mínima de ontem"), 1,20 °C.
- **Efeito na v3:** coeficientes da mínima ajustados **por região** no ERA5-Land (`ajustar_coef_tmin_referencia`) e,
  no sítio, por noites locais (`ajustar_coef_tmin_local`); persistência e `COEF_TMIN_PADRAO` viraram baselines na
  avaliação (`avaliar_geada_referencia`). Resultado: MAE 3,30 → **1,02 °C**, viés −2,93 → −0,01 °C, alarmes falsos
  48 → 4.

<a id="i4"></a>
### I4. Calibração sob mudança de domínio

- **Autores · ano · tipo:** "Where are we with calibration under dataset shift in image classification?", *TMLR* ·
  2025 · artigo científico
- **URL:** <https://arxiv.org/pdf/2507.07780>
- **Leitura:** resumo/trechos · **Comitê:** não conferida (a degradação do modelo em dado real foi confirmada pela
  análise própria, [seção 7](#7-análises-próprias-em-dado-real))
- **O que diz:** sob mudança de domínio, recalibração por *temperature/Platt scaling* é o baseline recomendado.
- **Efeito na v3:** **Platt local** de 2 parâmetros (`platt_local = 'a,b'`, ajustado por `desempenho_local.py` com
  ≥ 200 previsões resolvidas e ≥ 20 positivas), aplicado antes do limiar.

<a id="i5"></a>
### I5. Previsão de umidade do solo para irrigação — avaliação operacional (preprint)

- **Autores · ano · tipo:** "A Modular Orchestrated Pipeline for Soil Moisture Forecasting and Irrigation Decision
  Support", engrXiv · 2025 · preprint (credibilidade média)
- **URL:** <https://engrxiv.org/preprint/view/7870/12769>
- **Leitura:** resumo/trechos · **Comitê:** não conferida
- **O que diz:** a persistência é difícil de vencer; gradient boosting perde para ela numa série irrigada e ruidosa;
  meteorologia reduz o erro em ~14,6 %. Validação própria (umidade de solo **modelada** do ERA5-Land): a extrapolação
  linear de 24 h piora em relação à persistência.
- **Efeito na v3:** nenhum (secagem exponencial descartada até haver dado de sensor real e registro de regas).

<a id="i6"></a>
### I6. SWIM² — umidade do solo com sensor e modelagem inversa

- **Autores · ano · tipo:** Hendrickx et al., resumo EGU24-20013 (também EGU25 e EGU26) · 2024–2026 · artigo
  científico (resumo de congresso)
- **URL:** <https://meetingorganizer.copernicus.org/EGU24/EGU24-20013.html>
- **Leitura:** resumo/trechos · **Comitê:** não conferida
- **O que diz:** balanço hídrico FAO acoplado a sensores com inversão bayesiana; em aipo (2022) economizou ~45 mm
  (21 %) sem perda de produção; a calibração exige 10–20 dias de dado.
- **Efeito na v3:** nenhum (balanço hídrico descartado nesta rodada).

<a id="i7"></a>
### I7. Modelo híbrido com incerteza para irrigação (conformal prediction)

- **Autores · ano · tipo:** "An Uncertainty-Aware Hybrid Mathematical-Machine-Learning Model for Smart Irrigation
  Decision Support", arXiv · 2026 · preprint (credibilidade baixa)
- **URL:** <https://arxiv.org/pdf/2609.13864>
- **Leitura:** resumo/trechos · **Comitê:** só a ideia metodológica foi usada; nenhum número adotado
- **O que diz:** a cobertura média de bandas conformais esconde erro por regime (seca × águas); calibrar por grupo.
- **Efeito na v3:** margem da mínima noturna = **quantil q90 conformal por estação** (seca mai.–set., águas out.–abr.),
  com correção de amostra finita (`quantis_conformal_tmin`). Cobertura medida em 2021: 90,8 % (seca) e 90,0 % (águas).

<a id="i8"></a>
### I8. ISMN — controle de qualidade automático de umidade do solo

- **Instituição · ano · tipo:** International Soil Moisture Network (Dorigo et al., *Vadose Zone Journal* 2013) ·
  2013–2024 · norma ou padrão de qualidade
- **URL:** <https://ismn.bafg.de/data/flag-overview>
- **Leitura:** resumo/trechos · **Comitê:** não conferida
- **O que diz:** flags de spike, quebra positiva/negativa, platô de 12 h e saturação; consistência com dado auxiliar
  (subida do solo sem chuva é suspeita).
- **Efeito na v3:** nenhum (regra "subida sem causa" descartada nesta rodada).

<a id="i9"></a>
### I9. Nandi e Shrestha — sensores de solo baratos e caros por faixa de umidade e textura

- **Autores · ano · tipo:** Nandi e Shrestha, *Sensors* 24(18):5886 · 2024 · artigo científico
- **URL:** <https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11435841/>
- **Leitura:** resumo/trechos · **Comitê:** não conferida
- **O que diz:** RMSE máximo do resistivo de 4,79 % (2,64 % com ADC de 16 bits) e do capacitivo de 3,78 % (1,87 %);
  a precisão depende da textura. Um erro de 2,6–4,8 pp tem o tamanho da margem entre as faixas.
- **Efeito na v3:** reforçou a calibração no host ([P2](#p2)); cenários de viés e deriva na avaliação de robustez ficaram
  como próximo passo.

<a id="i10"></a>
### I10. LE3D — detectores de deriva leves para dispositivos limitados

- **Autores · ano · tipo:** "LE3D: A Lightweight Ensemble Framework of Data Drift Detectors for Resource-Constrained
  Devices", arXiv · 2022 · artigo científico; também "A Sequential Concept Drift Detection Method for On-Device
  Learning on Low-End Edge Devices", arXiv 2212.09637
- **URL:** <https://arxiv.org/pdf/2211.01840> e <https://arxiv.org/abs/2212.09637>
- **Leitura:** resumo/trechos · **Comitê:** não conferida
- **O que diz:** ADWIN, Page-Hinkley e KSWIN rodam em IoT limitado; métodos por taxa de erro precisam de rótulo — e no
  AgroAnálise o rótulo do prognóstico chega sozinho 12 h depois.
- **Efeito na v3:** **portão de segurança local** (`src/desempenho_local.py`, de hora em hora): Page-Hinkley
  (δ = 0,005, limiar 50) sobre o log-loss das previsões resolvidas + Brier de 28 dias contra a climatologia →
  `prognostico_suspenso`. O critério do plano "dispara em até 100 passos" mostrou-se inalcançável com esses padrões
  (112 passos sem ruído); o teste exige ≤ 120.

<a id="i11"></a>
### I11. ISA-18.2 — gestão de alarmes industriais (banda morta e atrasos)

- **Instituição · ano · tipo:** ISA Interchange, "Solve common problems in industrial automation alarm systems" ·
  2013–2024 · norma ou padrão (texto normativo pago; usado material de prática)
- **URL:** <https://blog.isa.org/solve-common-problems-industrial-automation-alarm-systems>
- **Leitura:** resumo/trechos · **Comitê:** usado só como conceito (os números de 1–5 % e 5–60 s vêm de material de
  prática, não do texto normativo)
- **O que diz:** banda morta (deadband) e atrasos (on/off-delay) são dimensionados alarme a alarme; *chattering* = mais
  de 4 ativações em 60 s; um off-delay transforma repetições em um só alarme.
- **Efeito na v3:** `classificar_com_banda_morta` (3 % da faixa crítica ≈ 0,72 °C / 1,8 pp) e off-delay de 60 s na
  melhora (`banda_morta_atraso_s`); geada e calor nunca esperam. Critério de ≤ 1 troca em 720 leituras atendido em 100
  de 100 sementes novas.

<a id="i12"></a>
### I12. INMET A304 (Natal/RN) — série horária reorganizada

- **Instituição · ano · tipo:** INMET, reorganizado por alvarofpp/dataset-inmet (licença CDLA-Sharing 1.0) ·
  2003–2024 · dataset
- **URL:** <https://raw.githubusercontent.com/alvarofpp/dataset-inmet/main/data/filter_wmo/A304.csv>
- **Leitura:** íntegra (dado baixado) · **Comitê:** não conferida separadamente
- **O que diz:** com 6.013 noites, os coeficientes da mínima ajustados no simulador erraram MAE 2,73 °C com viés
  −2,70 °C, o mesmo viés visto no ERA5-Land.
- **Efeito na v3:** só checagem auxiliar de que o viés dos coeficientes do simulador não é artefato da reanálise; não
  entra no repositório.

---

## 6. UX para baixo letramento e ICT4D

<a id="u1"></a>
### U1. Medhi et al. — Designing Mobile Interfaces for Novice and Low-Literacy Users

- **Autores · ano · tipo:** Medhi, Patnaik, Brunskill, Gautama, Thies e Toyama, *ACM TOCHI* 18(1) · 2011 · artigo
  científico
- **URL:** <https://www.microsoft.com/en-us/research/wp-content/uploads/2016/02/ToCHI2711_Medhi.pdf>
- **Leitura:** íntegra (pesquisador) · **Comitê:** confirmada pelo resumo e pelo DBLP; os percentuais não foram
  reconferidos
- **O que diz:** conclusão da tarefa de 0 % na interface de texto, 72 % na de voz e **100 % na gráfica com áudio**;
  56 de 90 pessoas não navegaram menus hierárquicos. Recomendações: pistas gráficas, anotação por voz em cada
  elemento, poucos níveis, nada de rolar a tela, mediadores humanos.
- **Efeito na v3:** **desenho da causa** ao lado do título (terra seca, encharcado, frio, calor, aparelho), que fala uma
  dica sensorial ao toque; pictogramas viraram **botões que falam**; botão **"?"** de ajuda falada; tudo na primeira tela
  a 360×740.

<a id="u2"></a>
### U2. LeClerc e Joslyn — The Cry Wolf Effect and Weather-Related Decision Making

- **Autores · ano · tipo:** LeClerc e Joslyn, *Risk Analysis* 35(3):385-395 · 2015 · artigo científico
- **URL:** <https://econpapers.repec.org/RePEc:wly:riskan:v:35:y:2015:i:3:p:385-395>
- **Leitura:** resumo/trechos · **Comitê:** confirmada (com nuance)
- **O que diz:** experimento com 354 estudantes: taxa alta de alarme falso piorou as decisões ("efeito lobo"); uma
  redução **pequena** dos alarmes falsos não mudou a obediência; acrescentar a probabilidade à previsão aumentou a
  obediência e a qualidade da decisão mais do que isso. Ressalva: estudantes, não agricultores.
- **Efeito na v3:** sustenta a regra `fisiologica` (redução **grande** do Vermelho térmico, de 27,6 % para 0,1 % do ano),
  a **certeza falada em dois níveis** sem números ("É quase certo que…" para p ≥ 0,8; "Pode ser que…") e o "Feito" que
  pausa a repetição do Vermelho.

<a id="u3"></a>
### U3. Fabregas, Kremer e Schilbach — Realizing the potential of digital development: agricultural advice

- **Autores · ano · tipo:** Fabregas, Kremer e Schilbach, *Science* 366 (resumo da Precision Development) · 2019 ·
  artigo científico
- **URL:** <https://precisiondev.org/customized-digital-advice-can-help-farmers-reduce-crop-loss-and-manage-weather-shocks-a-summary-or-as-much-as-we-can-summarize/>
- **Leitura:** resumo/trechos · **Comitê:** confirmada (+4 % de produtividade e +22 % nas **chances** de adoção, não 22 %
  de adoção)
- **O que diz:** informação agrícola por celular aumenta produtividade e adoção com benefício ~10 vezes o custo; uma
  linha de mão dupla no Quênia reduziu atrasos de adubação em 22 % (resumo da PxD).
- **Efeito na v3:** **"Feito" de um toque** (`POST /api/acao_feita`, tabela `registro_acoes`): o sistema passa a ouvir
  o agricultor, o que dá rótulo de campo e pausa a repetição do Vermelho por 60 min.

<a id="u4"></a>
### U4. IBGE — Censo Agropecuário 2017 (orientação técnica)

- **Instituição · ano · tipo:** IBGE, Atlas do Espaço Rural · 2019 · documento técnico oficial
- **URL:** <https://www.ibge.gov.br/apps/atlasrural/pdfs/05_00_Texto.pdf>
- **Leitura:** resumo/trechos · **Comitê:** confirmada (20,1 % recebem assistência técnica; 18,2 % na agricultura
  familiar segundo o WRI Brasil)
- **O que diz:** cerca de um quinto dos estabelecimentos recebe orientação técnica, número estável desde 1995-96;
  23 % dos produtores não sabem ler e escrever.
- **Efeito na v3:** **página do técnico** (`/tecnico`, com PIN; resumo de 7 dias pronto para colar no WhatsApp) e
  **"Chamar o técnico"** (`POST /api/pedido_ajuda` → link `wa.me` aberto pelo celular; servidor continua offline).

<a id="u5"></a>
### U5. Ushauri — conselho agrícola a uma ligação de distância

- **Instituição · ano · tipo:** Alliance of Bioversity International e CIAT · 2019 · documento técnico (relato de
  projeto)
- **URL:** <https://alliancebioversityciat.org/stories/ushauri-agricultural-advice-phone-call-away>
- **Leitura:** resumo/trechos · **Comitê:** não conferida
- **O que diz:** o agricultor deixa a pergunta em áudio; o extensionista responde num painel; piloto com 260
  agricultores, usabilidade 80/100, cada resposta reaproveitada para 3 agricultores em média.
- **Efeito na v3:** desenho do pedido de ajuda com o estado do aparelho anexado e do resumo para o técnico.

<a id="u6"></a>
### U6. Ahmed et al. — celular compartilhado em Bangladesh

- **Autores · ano · tipo:** Ahmed, Haque, Haider, Chen e Dell, *CHI 2019* · 2019 · artigo científico
- **URL:** <https://dl.acm.org/doi/10.1145/3290605.3300410>
- **Leitura:** resumo/trechos · **Comitê:** só o resumo; a **lacuna no código** foi confirmada (a repetição do
  Vermelho só ocorria com a página visível)
- **O que diz:** em comunidades marginalizadas é comum um aparelho para várias pessoas, com relações de poder e de
  gênero decidindo quem fica com ele.
- **Efeito na v3:** **alarme sonoro na estação base** (`src/alarme_local.py`, serviço `agroanalise-alarme`): toca na
  transição para Vermelho e no aviso de geada das 17–20 h, respeita o silêncio noturno, o "Feito" e 3 toques/10 min.

<a id="u7"></a>
### U7. Patel et al. — Avaaj Otalo, fórum de voz para pequenos agricultores

- **Autores · ano · tipo:** Patel, Chittamuru, Jain, Dave e Parikh, *CHI 2010* · 2010 · artigo científico
- **URL:** <https://dl.acm.org/doi/10.1145/1753326.1753434>
- **Leitura:** resumo/trechos · **Comitê:** não conferida
- **O que diz:** 51 agricultores, 7 meses, ~6.795 chamadas; o fórum de perguntas e respostas foi ~60 % do tráfego;
  preferência por teclas sobre fala.
- **Efeito na v3:** nenhum (banco de áudio com voz regional descartado nesta rodada).

<a id="u8"></a>
### U8. Stirzaker, Mbakwe e Mziray — sistema de aprendizado de água do solo para pequenos irrigantes

- **Autores · ano · tipo:** Stirzaker et al., *International Journal of Water Resources Development* 33(5):788-803 ·
  2017 · artigo científico
- **URL:** <https://repository.up.ac.za/handle/2263/61453>
- **Leitura:** resumo/trechos · **Comitê:** não conferida
- **O que diz:** o Chameleon mostra a água do solo em cores ligadas a limiares de ação e mede tensão (sem calibração por
  solo); o ganho vem de o agricultor comparar a cor ao longo dos dias com o que fez.
- **Efeito na v3:** confirmou o semáforo como linguagem; o resumo por dia foi para a página do técnico (a faixa de 7
  bolinhas na tela do agricultor foi descartada pelo orçamento de 16 KB).

<a id="u9"></a>
### U9. ISO 24495-1:2023 — Linguagem simples

- **Instituição · ano · tipo:** ISO · 2023 · norma
- **URL:** <https://www.iso.org/standard/78907.html>
- **Leitura:** resumo/trechos (texto pago) · **Comitê:** não conferida
- **O que diz:** o primeiro princípio é "o leitor recebe o que precisa"; a PxD/Ama Krushi (citada) elevou o atendimento
  com mensagens curtas, padronizadas e com o conselho repetido.
- **Efeito na v3:** nenhum (encurtar as frases mudaria textos exatos validados por testes; vai para o co-design).

<a id="u10"></a>
### U10. Dowse e Ehlers — avaliação de pictogramas com população de baixo letramento

- **Autores · ano · tipo:** Dowse e Ehlers, *Patient Education and Counseling* · 2001 · artigo científico
- **URL:** <https://core.ac.uk/download/145041976.pdf>
- **Leitura:** resumo/trechos · **Comitê:** não conferida
- **O que diz:** pictogramas desenhados localmente atingiram o critério ANSI de compreensão ≥ 85 % (20 de 23) contra 11
  dos pictogramas padrão; a ISO 9186-1:2014 padroniza o teste de compreensão de símbolos.
- **Efeito na v3:** nenhum no código; protocolo de teste dos pictogramas é o primeiro passo de campo (seção 10).

<a id="u11"></a>
### U11. Rose et al. — Decision support tools for agriculture

- **Autores · ano · tipo:** Rose et al., *Agricultural Systems* 149:165-174 · 2016 · artigo científico
- **URL:** <https://econpapers.repec.org/RePEc:eee:agisys:v:149:y:2016:i:c:p:165-174>
- **Leitura:** resumo/trechos · **Comitê:** não conferida
- **O que diz:** 395 ferramentas de apoio à decisão no Reino Unido e adoção baixa; a ferramenta deve "parecer e se
  comportar diferente" para agricultor, consultor e gestor; co-design com usuários diversos.
- **Efeito na v3:** a página do técnico é um produto separado (`tecnico.html`, linguagem técnica e números), fora do
  orçamento da página do agricultor; `eventos_interface` registra o uso, anônimo e com retenção de 90 dias, para o
  co-design.

<a id="u12"></a>
### U12. W3C — WCAG 2.2, critério 2.5.8 (tamanho do alvo)

- **Instituição · ano · tipo:** W3C · 2023 · norma
- **URL:** <https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum>
- **Leitura:** resumo/trechos · **Comitê:** não conferida
- **O que diz:** alvo mínimo de 24×24 px (AA) e 44×44 px (AAA); contraste de texto 4,5:1 (AA) e 7:1 (AAA). Medição
  própria com Playwright: todos os alvos da v2 já tinham ≥ 48 px.
- **Efeito na v3:** os botões novos ("Feito", "?", pictogramas) têm ≥ 48–56 px; modo noturno descartado pelo orçamento.

<a id="u13"></a>
### U13. Singh et al. (Digital Green) — Farmer.Chat

- **Autores · ano · tipo:** Singh et al., arXiv:2409.08916 · 2024 · preprint
- **URL:** <https://arxiv.org/abs/2409.08916>
- **Leitura:** resumo/trechos · **Comitê:** não conferida
- **O que diz:** mais de 15.000 agricultores e 300.000 perguntas; ganho de confiança e de agência, sobretudo para
  mulheres e baixo letramento; ensaio por cluster no Quênia ainda sem resultado.
- **Efeito na v3:** a contagem "avisos confirmados × não confirmados" foi para a página do técnico; o histórico de acertos
  falado ao agricultor ficou para depois de 4 semanas de campo.

---

## 7. Análises próprias em dado real

Todas offline, sobre `referencia_climatica/` ([I1](#i1)), reproduzidas pelo comitê e pela integração. Script no
repositório: `python scripts/medir_impacto_regra.py` (tabela mês a mês em
[WORKFLOW 11.2](WORKFLOW.md#112-impacto-medido-python-scriptsmedir_impacto_regrapy)).

| Medida | Antes (v2, regra instantânea) | Depois (v3, regra fisiológica) |
|---|---|---|
| Arábica (Varginha, média anual 20,4 °C), Vermelho no ano | 27,6 % | **0,1 %** |
| Arábica, Vermelho jun.–ago. | 37,7 % | **0,0 %** |
| Arábica, Verde / Amarelo no ano | 37,4 % / 35,0 % | 48,4 % / 51,5 % |
| Arábica, trocas de cor por ano | 1.842 | **119** |
| Conilon (S. Gabriel da Palha), Vermelho no ano | 16,7 % | **0,3 %** |
| Conilon, trocas de cor por ano | 1.580 | **120** |
| Prognóstico 12 h em 2021 (fora da amostra), Arábica / Conilon | AUC 0,793 / 0,716; Brier 0,211 / 0,219, **pior que a climatologia** (0,173 / 0,189); falso Amarelo 22,6 / 35,7 h/semana | falso Amarelo **0,0 / 0,0 h/semana**; AUC 0,48 / 0,64; Brier 0,0031 / 0,0082, ainda sem vencer a climatologia (0,0030 / 0,0078) |
| Mínima noturna em Varginha 2021 | MAE 3,30 °C, viés −2,93 °C, 48 alarmes falsos | MAE **1,02 °C**, viés −0,01 °C, 4 alarmes falsos; cobertura q90 90,8 % / 90,0 % |
| Simulador, Arábica, Vermelho no ano (inclui solo) | 28,5 % | 16,5 % (térmico 0,1 %) |

Leituras honestas:

- Com o rótulo da v2, metade das "antecipações" era só o ciclo diário cruzando 15/25 °C; o simulador superestimava o
  resfriamento noturno (amplitude diária simulada de julho 7,2 °C contra 13,1 °C real).
- Com o rótulo fisiológico, Vermelho térmico real é raríssimo (0,05 % e 0,16 % das horas de 2021): a IA não tem o que
  aprender e não vence a climatologia. Por isso o **portão local** decide no próprio sítio e a antecipação útil vem da
  secagem do solo e da geada.
- A reanálise (~11 km) suaviza a geada de baixada: serve para medir métodos, não para contar geadas no talhão.

---

## 8. Rastreabilidade: cada mudança da v3 e a fonte que a sustenta

| Mudança no AgroAnálise | Arquivo / chave | Referências |
|---|---|---|
| Faixa do cliente sobre a média de 24 h, no máximo Amarelo | `regras_agronomicas.avaliar_status_campo`, `regra_temperatura` | [A1](#a1), [A2](#a2), [A7](#a7), [U2](#u2), seção 7 |
| Geada no abrigo ≤ 2 °C + incerteza do sensor | `limiar_geada_abrigo_c`, `incerteza_sensor_ar_c` | [A4](#a4), [A5](#a5), [P8](#p8), [P10](#p10) |
| Calor ≥ 34 °C na florada / 2 dias seguidos | `limiar_calor_dano_c`, `dias_janela_florada`, `data_florada_principal` | [A6](#a6), [A9](#a9) |
| Seca programada opcional | `seca_programada_*` | [A8](#a8) |
| Banda morta + off-delay no status gravado | `classificar_com_banda_morta`, `banda_morta_*` | [I11](#i11), [P8](#p8) |
| Perfil do sensor de ar e limites físicos | firmware `AGRO_SENSOR_AR`, `PERFIS_SENSOR_AR` | [P8](#p8), [P9](#p9) |
| Sonda alimentada só na medida | firmware `AGRO_PINO_ALIMENTACAO_SOLO` | [P4](#p4) |
| Calibração da sonda no host (dois pontos ou 1 ponto) | `src/calibracao_sonda.py`, tabela `calibracao_sonda` | [P1](#p1), [P2](#p2), [P3](#p3), [P7](#p7), [I9](#i9) |
| Referência climática offline e seção 7 | `referencia_climatica/`, `avaliar_em_referencia` | [I1](#i1), [I12](#i12) |
| Prognóstico com rótulo fisiológico | `carregar_rotulador`, versão `-fisiologica` | [A1](#a1), seção 7 |
| Mínima por região + margem conformal | `complemento_prognostico.json` | [I3](#i3), [I7](#i7), [A4](#a4) |
| Portão local (Brier × climatologia, Page-Hinkley, Platt) | `src/desempenho_local.py` | [I4](#i4), [I10](#i10) |
| Certeza falada em dois níveis | `gerar_frase_antecipacao(probabilidade=...)` | [U2](#u2) |
| Causa visível, pictogramas que falam, "?" | `derivar_causa`, `index.html` | [U1](#u1) |
| "Feito" de um toque | `POST /api/acao_feita`, `registro_acoes` | [U3](#u3), [U2](#u2) |
| Alarme na estação base | `src/alarme_local.py` | [U6](#u6) |
| Página do técnico e "Chamar o técnico" | `/tecnico`, `POST /api/pedido_ajuda` | [U4](#u4), [U5](#u5), [U11](#u11) |

---

## 9. Ideias avaliadas e descartadas

Cada ideia foi avaliada pelo comitê; "descartada" significa **não nesta versão**, com o motivo.

| Ideia | Por que ficou de fora | O que precisaria para voltar | Fontes |
|---|---|---|---|
| Previsão do tempo online opcional (Open-Meteo) | API fora da rede para validar contra o sensor; licença só não comercial (decisão do cliente); nenhuma verificação publicada da previsão de geada contra o INMET | Decisão de licença; verificação local previsão × sensor com correção de viés | [I2](#i2) |
| ETo de Hargreaves, Kc, TAW/RAW, balanço hídrico, "água para N dias", Thornthwaite-Mather, SWIM² | Hargreaves precisa de coeficiente calibrado no Sudeste (não verificado); o balanço só tem sentido físico com a sonda calibrada entre capacidade de campo e ponto de murcha | Calibração CC/PMP em campo; registro de regas e chuvas (o "Feito" já fornece); estação INMET de referência | [A11](#a11)–[A14](#a14), [I6](#i6) |
| Secagem exponencial no lugar da extrapolação linear do ETA | Evidência de preprint e de umidade **modelada**; testes antigos fixam faixas de ETA | Dado de sensor real com regas registradas | [I5](#i5) |
| Fenologia completa por graus-dia e previsão falada de colheita | Tb e soma térmica confirmados, mas os cortes de fase são heurísticos sem fonte; só a janela de florada entrou | Validação dos cortes com agrônomo | [A9](#a9), [A10](#a10) |
| Protocolo serial v3 (ADC bruto, `id_no`), LoRa, polaridade alternada, várias sondas, DS18B20 de relva/solo, Vcc por bandgap, autodetecção SHT | A calibração no host já funciona invertendo o mapeamento do firmware; o resto exige hardware que não pôde ser testado (sem toolchain AVR nem placa); LoRa é esforço grande | Bancada com placa real | [P5](#p5), [P9](#p9), [P11](#p11), [A5](#a5) |
| Variável de tensão do solo (kPa) | Sem hardware; fontes lidas só pelo resumo | Tensiômetro ou Watermark no piloto | [A15](#a15), [U8](#u8) |
| Frio prolongado do Conilon (média diária < 15 °C por 3 dias) | O número de 15 °C não tem fonte primária | Agrônomo do Incaper | [A2](#a2), [A3](#a3) |
| Recalibrar o simulador contra o ERA5-Land | Arriscava os testes antigos do simulador; a avaliação em dado real direto ataca o mesmo problema | Nova rodada com metas de realismo por região | [I1](#i1) |
| Banco de áudio com voz regional ou TTS neural offline | Esforço grande; licença das vozes não conferida; evidência mista sobre voz humana × sintética | Gravação com técnico local e co-design | [U7](#u7) |
| Linguagem simples com no máximo 20 palavras e ação repetida | Mudaria textos exatos validados por testes antigos | Co-design com agricultores | [U9](#u9) |
| Faixa de 7 bolinhas da semana e modo noturno | Orçamento de 16 KB gzip da página já usado pela causa, pictogramas tocáveis e "Feito"; o resumo semanal foi para `/tecnico` | Folga de bytes ou página separada | [U8](#u8), [U12](#u12) |
| Controle de qualidade "subida sem causa" e desvio do DHT11 contra referência externa | Valor médio; a referência externa dependia da previsão online | Previsão online ou segundo sensor | [I8](#i8) |
| Histórico de acertos falado ao agricultor | Só faz sentido depois de 4 semanas de campo (não usar números do simulador) | Piloto de campo | [U13](#u13) |

---

## 10. Próximos passos de pesquisa

Em ordem de prioridade para o piloto de campo.

1. **Validação agronômica com especialista regional.** Rever com agrônomo da Epamig/Embrapa Café (Arábica) e do Incaper
   (Conilon): limiar de geada no Conilon, janela de florada padrão, seca programada (piso de segurança em % de solo) e
   se a geada instantânea deve **furar o silêncio noturno** do alarme local. Registrar cada decisão com fonte em
   `parametros_cultura.fonte_referencia` ou no CHANGELOG.
2. **Piloto de campo instrumentado (4 a 8 semanas por propriedade).** DHT em abrigo ventilado a 1,5 m (pratos plásticos
   brancos empilhados, sobre grama, a mais de 10 m de parede, [P10](#p10)), de preferência DHT22; sonda pulsada e
   calibrada com o assistente; comparar a temperatura com a estação automática do INMET mais próxima e contar quantas
   vezes o Vermelho e o Amarelo antecipado se confirmaram (`analise_antecipacao` + `registro_acoes`). Só então avaliar
   o portão local e o Platt com dado do sítio.
3. **Teste de compreensão dos pictogramas** ([U10](#u10), [V23](#v23)): mostrar cada símbolo sem legenda a pelo menos
   50 agricultores da região, perguntar "o que isto pede para fazer?", aprovar com ≥ 85 % (critério ANSI) e ≤ 5 % de
   leitura oposta, e redesenhar com eles os reprovados. Inclui os desenhos de causa da v3 e os redesenhados na v4.
   **v4:** roteiro completo em [PROTOCOLO_TESTE_USUARIOS.md](PROTOCOLO_TESTE_USUARIOS.md#5-compreensão-dos-pictogramas-iso-9186-1)
   e gabarito em `tests/dados/pictogramas_iso9186.csv`.
4. **Co-design com famílias** ([U11](#u11)): 3 rodadas com 5 a 8 famílias, incluindo mulheres e jovens que seguram o
   celular; tarefas observadas ("o que a tela pede?", "mostre que já regou", "chame o técnico"); usar
   `eventos_interface` para medir uso real. Encurtar as frases ([U9](#u9)) e decidir sobre a voz regional ([U7](#u7))
   a partir daqui. **v4:** a fala curta já existe; tarefas, métricas, TCLE e roteiro do moderador em
   [PROTOCOLO_TESTE_USUARIOS.md](PROTOCOLO_TESTE_USUARIOS.md).
5. **Calibração gravimétrica da sonda** ([P3](#p3)): 3 amostras de solo secas a 105 °C por talhão; comparar com o ajuste
   de 1 ponto; testar o sensor capacitivo v1.2 com teste de aceitação ([P6](#p6)). Com a sonda calibrada entre
   capacidade de campo e ponto de murcha, retomar o balanço hídrico FAO-56 ([A11](#a11)–[A13](#a13)).
6. **Hargreaves calibrado** ([A12](#a12)) contra a ETo da estação INMET mais próxima, antes de qualquer frase de "água
   para N dias".
7. **Prognóstico com alvo de solo.** Em dado real o Vermelho térmico fisiológico é raro; avaliar trocar o alvo da IA
   para eventos de secagem e encharcamento, sempre contra persistência e climatologia.
8. **Previsão do tempo opcional** ([I2](#i2)) só com decisão de licença do cliente e verificação local previsão ×
   sensor; nunca obrigatória (o sistema continua 100 % offline).
9. **Parâmetros do Page-Hinkley** ([I10](#i10)): calibrar δ e o limiar no simulador e em dado do sítio (o critério de
   100 passos era inalcançável com os padrões atuais).
10. **Sensor de relva** (DS18B20) para medir o que a folha baixa sente na geada de radiação ([A5](#a5)), comparando com a
    margem abrigo → relva usada hoje.

---

## 11. UX de campo da v4: referências novas

A v4 (16/11/2026) não mudou regra agronômica nenhuma: mudou **como** a tela, a voz e os desenhos dizem o que o sistema
já sabia. As fontes abaixo sustentam a auditoria de UX ([AVALIACAO_UX.md](AVALIACAO_UX.md)) e o
[protocolo de teste com agricultores](PROTOCOLO_TESTE_USUARIOS.md). As referências da v3 que a v4 reaproveitou são só
apontadas (U1, U2, U6, U9, U10, U11, U12), sem repetir a ficha.

**Limite de acesso, de novo.** Nesta sessão w3.org, nngroup.com, web.dev, developer.chrome.com, embrapa, scielo e mdpi
estavam bloqueados. Os textos do W3C foram lidos **na íntegra** pela cópia oficial no GitHub
(`raw.githubusercontent.com/w3c/wcag`), e o gov.br Design System pelo pacote npm. O restante foi lido **só pelo resumo
da busca** e está marcado assim. Nenhuma referência desta seção passou pelo comitê de checagem da v3; os números que
entraram nas metas foram **medidos pelo próprio projeto** (seção 3 da avaliação), não tirados das fontes.

### 11.1 Normas e guias de acessibilidade

<a id="v1"></a>
#### V1. W3C — Understanding WCAG 2.2: 1.4.3 Contraste (mínimo) e 1.4.6 (aprimorado)

- **URL:** <https://raw.githubusercontent.com/w3c/wcag/main/understanding/20/contrast-minimum.html>
- **Leitura:** íntegra
- **O que diz:** 4,5:1 para texto normal e 3:1 para texto grande (AA); 7:1 e 4,5:1 no aprimorado (AAA). Componente
  **inativo** está isento, o que não vale para um botão esmaecido que continua funcionando.
- **Efeito na v4:** pictogramas da notícia antiga deixaram de usar opacidade .55 (marca tracejada); meta própria de
  ≥ 7:1 na 1ª tela por causa do sol (7,34:1 medido).

<a id="v2"></a>
#### V2. W3C — Understanding 1.4.11 Contraste não textual

- **URL:** <https://raw.githubusercontent.com/w3c/wcag/main/understanding/21/non-text-contrast.html>
- **Leitura:** íntegra
- **O que diz:** partes gráficas necessárias para entender o conteúdo e indicadores de estado de componentes com ≥ 3:1
  contra as cores vizinhas.
- **Efeito na v4:** a razão lâmpada acesa ÷ apagada virou meta (≥ 4,5 viva, ≥ 3 com ofuscamento e em cores forçadas);
  anel branco na lâmpada acesa e apagadas mais escuras (3,92 → 9,98).

<a id="v3"></a>
#### V3. W3C — Understanding 1.4.1 Uso de cor

- **URL:** <https://raw.githubusercontent.com/w3c/wcag/main/understanding/20/use-of-color.html>
- **Leitura:** íntegra
- **O que diz:** a cor não pode ser o único meio de transmitir informação, indicar ação ou distinguir um elemento.
- **Efeito na v4:** "não siga esta cor" ganhou forma (borda tracejada + relógio no semáforo); dado de teste apaga o
  semáforo em vez de esmaecer a cor.

<a id="v4"></a>
#### V4. W3C — Understanding 1.4.10 Reflow e 1.4.4 Redimensionar texto

- **URL:** <https://raw.githubusercontent.com/w3c/wcag/main/understanding/21/reflow.html>
- **Leitura:** íntegra (1.4.10); 1.4.4 só citado
- **O que diz:** conteúdo utilizável a 320 px de CSS sem rolagem em duas direções; texto ampliável até 200 %.
- **Efeito na v4:** texto em `rem`, chip e topo que quebram linha; sem rolagem lateral com texto a 130/150/200 % e com
  zoom de 150–200 % (240 e 180 px de CSS).

<a id="v5"></a>
#### V5. W3C — Understanding 2.5.8 Tamanho do alvo (mínimo) e 2.5.5 (aprimorado)

- **URL:** <https://raw.githubusercontent.com/w3c/wcag/main/understanding/22/target-size-minimum.html>
- **Leitura:** íntegra (na v3 este critério tinha sido lido só pelo resumo, [U12](#u12))
- **O que diz:** 24×24 px (AA), com exceção por espaçamento; 44×44 px no AAA.
- **Efeito na v4:** o link "Voltar" de `/tecnico` (196×20 px, que passava no axe só pela exceção de espaçamento) foi
  para ≥ 48 px; meta de 48 px em todo interativo.

<a id="v6"></a>
#### V6. W3C — Understanding 4.1.2 Nome, função, valor

- **URL:** <https://raw.githubusercontent.com/w3c/wcag/main/understanding/20/name-role-value.html>
- **Leitura:** íntegra
- **O que diz:** nome e estado de componentes expostos à tecnologia assistiva e atualizados quando mudam.
- **Efeito na v4:** `aria-label` do semáforo com concordância ("luz vermelha acesa") e com a marca de notícia antiga,
  teste, demonstração ou sem ligação.

<a id="v7"></a>
#### V7. W3C — Understanding 2.4.11 Foco não obscurecido (mínimo) e 2.3.3 Animação por interação

- **URLs:** <https://raw.githubusercontent.com/w3c/wcag/main/understanding/22/focus-not-obscured-minimum.html> ·
  2.3.3 no mesmo repositório (`understanding/21/animation-from-interactions.html`)
- **Leitura:** 2.3.3 íntegra; 2.4.11 só citado (página canônica, não aberta)
- **O que diz:** o elemento com foco não pode ficar inteiramente escondido por conteúdo do autor (barras fixas);
  animação disparada por interação deve poder ser desligada.
- **Efeito na v4:** `scroll-padding-bottom` com a altura da barra de voz e aviso de voz fora da barra (foco escondido
  7 → 0); o pulsar da lâmpada vermelha continua respeitando `prefers-reduced-motion`.

<a id="v8"></a>
#### V8. W3C — WCAG 4.1.3 Mensagens de status e 2.2.1 Tempo ajustável

- **URLs:** <https://www.w3.org/WAI/WCAG22/Understanding/status-messages.html> ·
  <https://www.w3.org/WAI/WCAG22/Understanding/timing-adjustable.html>
- **Leitura:** resumo/trechos
- **O que diz:** mensagens de status anunciadas sem receber foco; conteúdo com tempo limite deve dar controle ao
  usuário (o ponto de toasts que somem sozinhos é discutido).
- **Efeito na v4:** um só `#anunciador` (role=status) que só muda quando a chave do anúncio muda; confirmação do "Feito"
  persistente ("Feito às HH:MM"), não só no toast de 3,5 s.

<a id="v9"></a>
#### V9. W3C/WAI — ARIA Authoring Practices: Dialog (Modal)

- **URLs:** <https://www.w3.org/WAI/ARIA/apg/patterns/dialogmodal/> ·
  <https://w3c.github.io/wai-website/ARIA/apg/patterns/dialog-modal/>
- **Leitura:** resumo/trechos
- **O que diz:** fundo inerte, Tab circula dentro do diálogo, Esc fecha, o foco volta ao elemento que abriu.
- **Efeito na v4:** `<dialog>` nativo com `showModal()`, foco inicial no "Não" e devolução do foco.

<a id="v10"></a>
#### V10. CSSWG — explainer de `meta name="text-scale"` e MDN

- **URLs:** <https://raw.githubusercontent.com/w3c/csswg-drafts/main/css-env-1/explainers/meta-text-scale.md> ·
  <https://developer.mozilla.org/en-us/docs/Web/html/reference/elements/meta/name/text-scale>
- **Leitura:** explainer íntegra; MDN resumo
- **O que diz:** com `<meta name="text-scale" content="scale">` o navegador aplica a escala de fonte do sistema ao `rem`
  da página em vez de reduzir a página inteira; o explainer cita cerca de 37 % dos usuários de Android com a escala de
  fonte alterada.
- **Efeito na v4:** meta nas duas páginas e 0 `font-size` em px (eram 52).

<a id="v11"></a>
#### V11. Cores forçadas: web-features e Adrian Roselli

- **URLs:** <https://web-platform-dx.github.io/web-features-explorer/features/forced-colors> ·
  <https://adrianroselli.com/2021/02/whcm-and-system-colors.html>
- **Leitura:** resumo/trechos
- **O que diz:** `@media (forced-colors: active)` e cores de sistema (`Canvas`, `CanvasText`, `ButtonText`);
  `forced-color-adjust: none` só para gráficos que precisam das cores.
- **Efeito na v4:** o semáforo mantém as cores em cores forçadas (1,11 → 9,98), barra com fundo `Canvas` e botão de voz
  com borda `ButtonText`.

<a id="v12"></a>
#### V12. Chromium — escurecimento automático de páginas (blink-dev)

- **URL:** <https://groups.google.com/a/chromium.org/g/blink-dev/c/hkjRPJ1-Ruk>
- **Leitura:** resumo/trechos
- **O que diz:** discussão do Blink sobre o escurecimento automático de páginas; citada pelo auditor, conteúdo lido só
  pelo resumo. O que sustenta a mudança é a **medição própria**: com o Chromium em `forceDarkModeEnabled`, a página com
  `color-scheme: only light` volta a ter fundo claro.
- **Efeito na v4:** `<meta name="color-scheme" content="only light">` nas duas páginas (amarelo aceso ÷ apagado sob o
  escurecimento 3,43 → 14,05).

<a id="v13"></a>
#### V13. Plataforma web: contexto seguro, cache imutável, `overscroll-behavior`, regiões vivas

- **URLs:** <https://w3c.github.io/webappsec-secure-contexts/> ·
  <https://developer.mozilla.org/en-US/docs/Web/Security/Secure_contexts> · <https://web.dev/articles/love-your-cache> ·
  <https://web.dev/articles/offline-ux-design-guidelines> · <https://developer.chrome.com/blog/overscroll-behavior> ·
  <https://developer.mozilla.org/docs/Web/Accessibility/ARIA/ARIA_Live_Regions> ·
  <https://lists.w3.org/Archives/Public/public-aria/2017Nov/0022.html> ·
  <https://bugzilla.mozilla.org/show_bug.cgi?id=1810596> · <https://developer.chrome.com/blog/timer-throttling-in-chrome-88>
- **Leitura:** resumo/trechos (todas)
- **O que dizem:** Service Worker só em contexto seguro (só `127.0.0.0/8` e `::1` são confiáveis em `http://`); arquivo
  com hash no nome + `Cache-Control: immutable`; `overscroll-behavior-y: contain` desliga o "puxar para atualizar";
  regiões vivas devem receber só mudanças reais (navegadores e leitores divergem ao reanunciar texto idêntico).
- **Efeito na v4:** `/static/agro.<hash>.css` imutável (2ª visita 16.678 → 15.240 B); Service Worker continua
  descartado; `overscroll-behavior-y: contain`; `definirTexto` e 0 mutações em 10 min. O ETag/polling adaptativo ficou
  para a v5.

<a id="v14"></a>
#### V14. Toasts acessíveis: Scott O'Hara e USPTO Design System

- **URLs:** <https://www.scottohara.me/blog/2019/07/08/a-toast-to-a11y-toasts.html> ·
  <https://uspto.github.io/USPTO-Design-System/components/alertsNotifications.html>
- **Leitura:** resumo/trechos
- **O que dizem:** confirmação importante não deve depender de um aviso que some sozinho.
- **Efeito na v4:** todo toast é também falado, e o estado do "Feito" fica no próprio botão.

<a id="v15"></a>
#### V15. gov.br Design System 3.7.0 (`@govbr-ds/core`) e Design Tokens (DTCG 2025.10)

- **URLs:** <https://registry.npmjs.org/@govbr-ds/core> · <https://gitlab.com/govbr-ds/govbr-ds-dev/govbr-ds-dev-core> ·
  <https://www.w3.org/community/reports/design-tokens/CG-FINAL-format-20251028/>
- **Leitura:** gov.br DS íntegra (código e tokens do pacote npm); DTCG resumo/trechos
- **O que diz:** tokens semânticos de retorno (`--danger`, `--warning`, `--success`, `--info`) com variantes; base de
  texto de 14 px; botão grande de 48 px; mensagem de erro em linha com `role=alert`.
- **Efeito na v4:** cores de estado iguais em `/` e `/tecnico` com variantes `-texto`; `--neutro-acao` no "Não" (o
  vermelho fica só para perigo da lavoura); erro do PIN em linha com `role=alert`. O texto principal do AgroAnálise já
  é maior que a base do gov.br.

<a id="v16"></a>
#### V16. Material/Android — alvos de 48 dp

- **URL:** <https://support.google.com/accessibility/android/answer/7101858?hl=en-GB>
- **Leitura:** resumo/trechos
- **O que diz:** alvos de toque de pelo menos 48×48 dp.
- **Efeito na v4:** meta de 48 px nas duas páginas.

### 11.2 Visão em campo: sol, daltonismo, idade

<a id="v17"></a>
#### V17. DisplayMate — brilho e contraste de celulares sob luz ambiente

- **URL:** <https://displaymate.com/Smartphone_Brightness_ShootOut_1.htm>
- **Leitura:** resumo/trechos
- **O que diz:** sob luz forte (dezenas de milhares de lux) a tela perde a maior parte do contraste; a luz refletida se
  soma às luminâncias.
- **Efeito na v4:** modelo de ofuscamento da suíte (somar 0,10 às duas luminâncias) e meta ≥ 3,0 com ele (1,91 → 4,78).
  É modelo, não medição ao sol.

<a id="v18"></a>
#### V18. Universidade de Waterloo — artigo da GI 2026 sobre legibilidade sob luz ambiente

- **URL:** <https://cs.uwaterloo.ca/~dvogel/gi2026/papers/1075b.pdf>
- **Leitura:** resumo/trechos
- **O que diz:** citado pelo auditor como evidência de perda de legibilidade da tela sob luz ambiente; conteúdo não
  conferido além do resumo.
- **Efeito na v4:** anel branco na lâmpada acesa (pista sem cor).

<a id="v19"></a>
#### V19. Daltonismo e semáforos: NYU Abu Dhabi e prevalência no Brasil

- **URLs:** <https://nyuad.design/essay/color-blindness-and-traffic-light-designs> · <https://www.rbojournal.org/en/?p=10406>
- **Leitura:** resumo/trechos
- **O que dizem:** citados pelo auditor sobre desenho de semáforos para daltônicos e prevalência da deficiência de visão
  de cores no Brasil; conteúdo lido só pelo resumo. O veredito da v4 vem da medição própria com 4 filtros.
- **Efeito na v4:** a ordem fixa vermelho → amarelo → verde e os ícones ✗/!/✓ ficaram; a captura de daltonismo
  ([AVALIACAO_UX.md, seção 8](AVALIACAO_UX.md#8-capturas)) usa as matrizes de Machado, Oliveira e Fernandes (2009).

<a id="v20"></a>
#### V20. Hou et al. (2022) — tamanho de fonte para idosos

- **URL:** <https://pmc.ncbi.nlm.nih.gov/articles/PMC9376262/>
- **Leitura:** resumo/trechos
- **O que diz:** pessoas idosas preferem e leem melhor fontes maiores em telas pequenas.
- **Efeito na v4:** mínimo de 16 px (1 rem) em todo texto da página do agricultor.

<a id="v21"></a>
#### V21. TetraLogical — funções Android e TalkBack

- **URL:** <https://tetralogical.com/blog/2022/07/07/android-accessibility-roles-and-talkback/>
- **Leitura:** resumo/trechos
- **O que diz:** como o TalkBack anuncia nomes e funções.
- **Efeito na v4:** nenhum direto; TalkBack em aparelho real é pendência ([AVALIACAO_UX.md, seção 9](AVALIACAO_UX.md#9-pendências-e-limites-da-avaliação)).

### 11.3 Baixo letramento, voz e pictogramas

<a id="v22"></a>
#### V22. Medhi, Sagar e Toyama (2007) — interfaces sem texto para usuários analfabetos

- **URL:** <https://ideas.repec.org/a/tpr/itintd/v4y2007i1p37-50.html>
- **Leitura:** resumo/trechos
- **O que diz:** anotação de voz em todo elemento e um recurso de ajuda consistente fazem diferença para quem não lê;
  complementa [U1](#u1) (Medhi et al. 2011).
- **Efeito na v4:** todo aviso e todo diálogo falados (33 % → 100 %); ajuda "?" montada a partir do que está na tela;
  selo de som no desenho da causa.

<a id="v23"></a>
#### V23. ISO 9186-1:2014, ANSI Z535.3 e ISO 3864 — compreensão de símbolos

- **URLs:** <https://www.iso.org/standard/59226.html?browse=tc> ·
  <https://webstore.ansi.org/preview-pages/ISO/preview_ISO+9186-1-2014.pdf> · <https://pmc.ncbi.nlm.nih.gov/articles/PMC10258412> ·
  <https://www.iiid.net/wp-content/uploads/Symbol-Comprehension-Testing-Protocol.pdf>
- **Leitura:** resumo/trechos (norma paga; prévia da ANSI e protocolo do IIID pelos trechos). Os critérios de 67 % e
  85 % vêm de fonte secundária (PMC10258412).
- **O que dizem:** teste de compreensão com o símbolo isolado, pergunta aberta e codificação das respostas, incluindo a
  leitura **oposta**; aceitação de 67 % (ISO 3864) e 85 % (ANSI Z535.3). Com [U10](#u10) (Dowse e Ehlers), desenhos
  feitos com a população local passam onde os padrão falham.
- **Efeito na v4:** gabarito `tests/dados/pictogramas_iso9186.csv` (1 linha por código) e
  [protocolo, seção 5](PROTOCOLO_TESTE_USUARIOS.md#5-compreensão-dos-pictogramas-iso-9186-1); a meta de compreensão
  continua de campo.

<a id="v24"></a>
#### V24. GSMA — internet móvel para quem tem dificuldade de leitura e escrita

- **URL:** <https://www.gsma.com/solutions-and-impact/connectivity-for-good/mobile-for-development/blog/making-mobile-internet-more-accessible-for-people-with-reading-or-writing-difficulties/>
- **Leitura:** resumo/trechos
- **O que diz:** ícones sem sentido no contexto local falham; numeracia é distinta do letramento.
- **Efeito na v4:** ícones redesenhados ("agora" vira cronômetro, "Nada" deixa de ser a mão de PARE, Arábica e Conilon
  com letra própria) e fala curta sem números.

<a id="v25"></a>
#### V25. Wolters, Campbell, DePlacido, Liddell e Owens (2007) — idosos e voz sintética

- **URL:** <https://www.isca-archive.org/ssw_2007/wolters07_ssw.html>
- **Leitura:** resumo/trechos
- **O que diz:** comparou 210 × 150 palavras por minuto; idosos com perda auditiva entendem pior; ajudam fala mais
  lenta, pausas e vocabulário familiar.
- **Efeito na v4:** repetição a 0,8 e fala lenta lembrada (0,85); "tenha geada" no lugar de "geie"; fala curta de até
  15 palavras.

<a id="v26"></a>
#### V26. Guias de interface de voz (Avaya e Microsoft)

- **URLs:** <https://support.avaya.com/elmodocs2/AIR/CDV2/html/15601.htm> ·
  <https://learn.microsoft.com/pt-br/previous-versions/office/developer/speech-technologies/ms994651(v=msdn.10)>
- **Leitura:** resumo/trechos
- **O que dizem:** o mais importante primeiro; mensagens curtas; poucas instruções por vez; oferecer "repetir" e
  reformular na 2ª vez.
- **Efeito na v4:** `fala_curta` com a ação na 2ª frase (logo depois de "Sinal &lt;cor&gt;."); mensagem completa no 2º
  toque; uma ordem por ação.

<a id="v27"></a>
#### V27. IPLF — a norma ISO de linguagem simples, parte 1

- **URL:** <https://www.iplfederation.org/the-iso-plain-language-standard-part1>
- **Leitura:** resumo/trechos
- **O que diz:** os quatro princípios da ISO 24495-1 ([U9](#u9)): o leitor recebe o que precisa, encontra, entende e
  usa; fórmulas de legibilidade não bastam.
- **Efeito na v4:** a meta de voz mede **ordem e tamanho** (ação até a 2ª frase, ≤ 15 palavras), não só Flesch-PT; na
  v3 o veto era mudar textos testados, e a v4 resolveu com um campo novo (`fala_curta`) sem tocar na `mensagem_voz`.

<a id="v28"></a>
#### V28. Martins et al. (1996) — Flesch para o português (via NILC-Metrix)

- **URL:** <https://arxiv.org/pdf/2201.03445> (Leal et al., NILC-Metrix, 200 métricas para o português brasileiro)
- **Leitura:** resumo/trechos; a publicação original de Martins et al. (1996) não foi encontrada na busca
- **O que diz:** o NILC-Metrix inclui o índice de Flesch adaptado ao português por Martins et al. (1996)
  (248,835 − 1,015·palavras/frase − 84,6·sílabas/palavra); revisões apontam que fórmulas desse tipo são superficiais
  (medem estrutura, não ordem nem relevância).
- **Efeito na v4:** meta de Flesch-PT mediano ≥ 80 na fala curta (91,3 medido); sílabas contadas por heurística (±1).

<a id="v29"></a>
#### V29. Nielsen — heurísticas e severidade

- **URLs:** <https://www.nngroup.com/articles/ten-usability-heuristics/> ·
  <https://www.cs.princeton.edu/courses/archive/spring13/cos436/handouts/Heuristics_explained.pdf> ·
  <https://course.ccs.neu.edu/is4800/ssl/nielsen93.pdf> (Nielsen e Landauer 1993)
- **Leitura:** resumo/trechos (nngroup bloqueado; material de curso e resultado da busca)
- **O que diz:** 10 heurísticas e escala de severidade 0–4; o modelo de 1993 estima ~85 % dos problemas com 5 usuários
  quando a chance de cada um achar um problema fica perto de 0,31, por isso rodadas pequenas e repetidas.
- **Efeito na v4:** avaliação heurística por tela; [protocolo](PROTOCOLO_TESTE_USUARIOS.md) com 5–8 pessoas por rodada
  e cotas para o público heterogêneo.

<a id="v30"></a>
#### V30. Baumgartner et al. (CHI 2019) — Pictorial System Usability Scale (P-SUS)

- **URL:** <https://infoscience.epfl.ch/record/268432?ln=en> (DOI 10.1145/3290605.3300299)
- **Leitura:** resumo/trechos
- **O que diz:** versão pictórica de 10 itens do SUS para celular; validada com 60 estudantes; somas correlacionadas
  com o SUS verbal e vista como mais motivadora. Não foi validada com baixo letramento.
- **Efeito na v4:** inspira a escala de rostos do [protocolo, seção 9](PROTOCOLO_TESTE_USUARIOS.md#9-satisfação-escala-pictórica),
  declarada como adaptação não validada.

<a id="v31"></a>
#### V31. Confiança em conselho agrícola digital (CGIAR, Quênia)

- **URL:** <https://cgspace.cgiar.org/bitstreams/f26a3038-5a46-4256-bc58-cc16093aef16/download>
- **Leitura:** resumo/trechos
- **O que diz:** a credibilidade da fonte é barreira medida para seguir conselho por celular.
- **Efeito na v4:** o resumo do técnico exclui a demonstração; dado de teste e demonstração nunca se parecem com alerta
  real (com [U2](#u2), efeito "lobo").

<a id="v32"></a>
#### V32. Resolução CNS nº 510/2016 — ética em pesquisa em ciências humanas e sociais

- **URL:** <https://www.ifsudestemg.edu.br/documentos-institucionais/unidades/reitoria/comissoes-e-comites/ceph/orientacoes-tcle-res-510_ciencias-humanas-e-sociais_2020.pdf>
  (orientações de um CEP sobre a resolução; texto da resolução em cópias institucionais)
- **Leitura:** resumo/trechos
- **O que diz:** consentimento pode ser oral, escrito ou em outra forma adequada ao participante, registrado em áudio,
  imagem ou escrita; no oral, testemunha que não seja da equipe; o participante pode consultar o registro.
- **Efeito na v4:** TCLE lido em voz alta e gravado no [protocolo, seção 3](PROTOCOLO_TESTE_USUARIOS.md#3-ética-consentimento-lido-em-voz-alta).

### 11.4 Rastreabilidade: cada mudança da v4 e a fonte que a sustenta

| Mudança | Arquivo / chave | Referências |
|---|---|---|
| Semáforo apagado (`neutro`) para dado de teste e demonstração fora da banca; sem alarme nem vibração | `index.html` (`body[data-estado]`), `alarme_local.ORIGENS_QUE_TOCAM` | [V3](#v3), [U2](#u2), [V31](#v31), [U1](#u1) |
| Notícia antiga marcada pela forma (tracejado + relógio), sem opacidade | `agricultor.css`, `li[data-antiga]` | [V1](#v1), [V2](#v2), [V3](#v3) |
| Semáforo deitado, lâmpada ≥ 72 px, anel branco, apagadas mais escuras | `agricultor.css` | [V2](#v2), [V17](#v17), [V18](#v18), [V19](#v19) |
| Cores forçadas e `color-scheme: only light` | `index.html`, `tecnico.html` | [V11](#v11), [V12](#v12) |
| `rem`, `meta text-scale`, mínimo de 16 px, AAA na 1ª tela | as duas páginas | [V1](#v1), [V4](#v4), [V10](#v10), [V20](#v20) |
| Nada atrás da barra de voz; foco visível | `scroll-padding-bottom`, `#aviso-voz` no fluxo | [V7](#v7), [U1](#u1) |
| `aria-label` com concordância e marca de estado; um só `#anunciador` | `index.html` | [V6](#v6), [V8](#v8), [V13](#v13) |
| `fala_curta` (ação na 2ª frase, ≤ 15 palavras, sem números), repetição mais lenta | `acoes_orientacao.montar_fala_curta`, `index.html` | [V25](#v25), [V26](#v26), [V27](#v27), [V28](#v28), [U9](#u9) |
| "1 grau", "menos 2 graus", "tenha geada", uma ordem de rega por fala | `gerador_dataset.falar_graus`, `prognostico.FRASES_CERTEZA`, `_juntar_acoes` | [V24](#v24), [V25](#v25), [V27](#v27) |
| Toda ordem falada tem pictograma; subjuntivo "que faça" não é ordem | `acoes_orientacao.acoes_faladas`, `acoes_cobrem_mensagem` | [U1](#u1), [V22](#v22) |
| Avisos e diálogos falados; ajuda contextual | `index.html` | [V22](#v22), [V14](#v14), [V8](#v8) |
| "Feito" com desfazer e sem duplicata | `POST /api/acao_feita/desfazer`, `retorno_agricultor` | [U6](#u6), [V29](#v29) |
| `<dialog>` modal nativo; PIN com erro em linha | `index.html`, `seguranca_banca` | [V9](#v9), [V15](#v15) |
| Pictogramas redesenhados, ícone por cultivar, gabarito ISO 9186-1 | `index.html`, `tests/dados/pictogramas_iso9186.csv` | [V23](#v23), [V24](#v24), [U10](#u10) |
| Seca programada e florada no `/tecnico`, com confirmação | `tecnico.html` | [U11](#u11), [V29](#v29) |
| Alvos ≥ 48 px e tokens de estado iguais nas duas páginas | `tecnico.html` | [V5](#v5), [V15](#v15), [V16](#v16) |
| CSS estático imutável; sem Service Worker; `overscroll-behavior` | `app.py` (`/static/agro.<hash>.css`) | [V13](#v13) |
| Protocolo de teste com agricultores | `docs/PROTOCOLO_TESTE_USUARIOS.md` | [V23](#v23), [V29](#v29), [V30](#v30), [V32](#v32), [U10](#u10), [U11](#u11) |
