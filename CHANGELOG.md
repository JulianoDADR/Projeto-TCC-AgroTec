# Changelog — AgroAnálise

Todas as mudanças relevantes deste projeto ficam registradas aqui.

O formato segue o [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/) e o projeto usa
[Versionamento Semântico](https://semver.org/lang/pt-BR/). Cada item diz **o que mudou** e **por que isso importa para
o agricultor** que não lê e depende só da cor, da voz e dos pictogramas.

## [Não publicado]

Nada por enquanto.

## [4.0.0] — 2026-11-15

Versão de **UX de campo**: a tela do agricultor passa a dizer a verdade em todos os estados (dado de teste,
demonstração, notícia antiga, sem ligação, erro do programa) e em todos os aparelhos (celular baixo, fonte ampliada,
cores forçadas, escurecimento automático, sol); a voz diz **primeiro o que fazer**, em até 15 palavras e sem números;
"Feito" ganhou desfazer e deixou de duplicar; e uma suíte permanente (`scripts/validar_ux.sh`) trava regressões
contra as 70 metas de `tests/e2e/validacao/metas_v4.json`. Invariantes mantidas: saída só por semáforo (0/1/2) +
voz + pictogramas; regras agronômicas, limiares e números do cliente intactos (`src/regras_agronomicas.py` sem
mudança; status de toda a grade idêntico ao de `5c66abe`); página do agricultor 100 % offline (sem CDN, sem
Service Worker).

Integração: merges `b768d5c` (conteudo-voz), `56bc5a0` (interface-agricultor) e `7177458` (tecnico-e-validacao)
sobre `5c66abe`, mais o commit de integração (costuras e correções abaixo).

### Adicionado

- **Frente A — página do agricultor** (`src/templates/index.html`, `src/templates/agricultor.css`, `src/app.py`):
  CSS crítico inline + `GET /static/agro.<hash>.css` com `Cache-Control: public, max-age=31536000, immutable`;
  semáforo sempre deitado com lâmpada ≥ 72 px; estado **neutro** (lâmpadas apagadas, sem alarme nem vibração) para
  dado de teste e demonstração vista fora da banca; **última notícia** marcada pela forma (borda tracejada + relógio);
  503 ≠ sem ligação ("Programa com problema"); `aria-label` do semáforo com concordância e marca de antiga/teste;
  cores forçadas, `color-scheme: only light`, `text-scale` e `rem`; `fala_curta` no 1º toque, mensagem longa no 2º e
  fala mais lenta (0,8/0,85) lembrada; avisos e diálogos falados; uma só região viva (`#anunciador`); "Feito" com
  **Desfazer** por 15 s (`POST /api/acao_feita/desfazer`, aceito até 120 s) e sem duplicata; `<dialog>` modal
  nativo; PIN com `codigo` e contagem; pictogramas redesenhados e ícone próprio para Arábica e Conilon; gabarito
  ISO 9186-1 em `tests/dados/pictogramas_iso9186.csv` (resultados vazios até o teste de campo).
- **Frente B — conteúdo e retorno** (`acoes_orientacao`, `gerador_dataset`, `prognostico`, `retorno_agricultor`,
  `database`, `seguranca_banca`): `montar_fala_curta`, `acoes_cobrem_mensagem`, `falar_graus` ("1 grau",
  "menos 2 graus"), "tenha geada" no lugar de "geie", uma ordem de rega por fala, sombra no calor antecipado,
  `desfazer_acao`/`registrar_acao_sem_repetir`, marca `demonstracao` em pedidos e eventos e o resumo do técnico sem
  a demonstração.
- **Frente C — técnico e suíte** (`src/templates/tecnico.html`, `scripts/validar_ux.sh`, `tests/e2e/validacao/`,
  `package.json`): alvos ≥ 48 px, retorno junto do botão no assistente de calibração, tokens de cor iguais aos do
  agricultor, "Calendário do café" (seca programada e florada com a consequência dita e confirmação, saídos da banca);
  suíte permanente com matriz de 24 estados × 6 telas, axe-core, Lighthouse, contraste próprio, pixels das lâmpadas,
  regiões vivas, bytes e coerência cor × chip × título × voz × pictograma × causa.
- **Documentação da v4:**
  - [`docs/AVALIACAO_UX.md`](docs/AVALIACAO_UX.md) (novo): método (3 lentes de auditoria, ferramentas e versões,
    4 personas, 7 tarefas, heurísticas de Nielsen, WCAG 2.2, APG, ISO 24495-1 e ISO 9186-1), resultados antes × depois,
    os 40 achados por severidade com status, matriz componente × estado, inventário de componentes, as 70 metas e 8
    capturas em [`docs/ux/`](docs/ux/) (antes × depois do semáforo, simulação de daltonismo, antecipação, banca,
    `/tecnico`, cores forçadas);
  - [`docs/PROTOCOLO_TESTE_USUARIOS.md`](docs/PROTOCOLO_TESTE_USUARIOS.md) (novo): teste com agricultores reais —
    recrutamento pela ATER, TCLE lido em voz alta (Resolução CNS 510/2016), 5–8 participantes por rodada, tarefas
    T1–T8, métricas, compreensão dos pictogramas pela ISO 9186-1, escala com rostos, roteiro do moderador e cuidados
    de contexto rural;
  - [`docs/PESQUISA_REFERENCIAS.md`](docs/PESQUISA_REFERENCIAS.md#11-ux-de-campo-da-v4-referências-novas): seção 11
    com 32 referências novas (V1–V32), cada uma marcada como lida na íntegra ou só pelo resumo, e a rastreabilidade
    mudança → fonte da v4;
  - `docs/WORKFLOW.md` (seção 12 e gate de UX na publicação), `docs/DICIONARIO_DADOS.md` (seção 21),
    `docs/MAPEAMENTO_FUNCOES.md` (regenerado por `ast`: 344 itens públicos; interfaces da v4 13/13 idênticas ou
    compatíveis) e os 4 diagramas (`.mmd` + `.svg` re-renderizados com mermaid 11.4.1);
  - README: "Novidades da v4" com capturas antes × depois e "Como rodar a suíte de UX".

### Corrigido na integração (cada item tem teste que falha sem a correção, ou meta da suíte)

- **"Feito" sem `id_leitura` durante a demonstração contava como ação real:** entrava no resumo do técnico e calava
  por 60 min o alerta real da mesma causa depois da apresentação. Agora fica ligado à leitura da banca
  (`test_pagina_v4.test_feito_sem_leitura_na_demonstracao_nao_vale_como_real`).
- **Evento "feito" da demonstração no resumo do técnico** (meta 11): o evento passa a levar a marca `demonstracao`
  e é contado em `demonstracao_excluida`.
- **"Feito" aparecia com notícia antiga, sem ligação e relógio errado** (coerência): agora só com notícia viva.
- **Simulação: voz sem o pictograma mostrado** — a fala de teste termina com "Confira o aparelho.".
- **Amarelo sem pictograma** (IA acima da regra com a terra perto da borda): a fala curta ficava sem ação; agora
  repete o "olhe" da própria mensagem ("Olhe a terra hoje"), sem recomendação nova.
- **Oclusão a 360×640** em 13 casos (antiga, simulação, 503, sem voz): o selo decorativo da marca sai em telas
  < 380 px e marca + plantação cabem numa linha.
- **Semáforo neutro com a lâmpada do meio "acesa" por pixel** (ícone do aparelho claro demais): ícone cinza-escuro.
- **1ª carga sem estado e com 503** ficava "Semáforo carregando" por mais de 10 s: a 1ª falha já mostra o problema.
- **Troca de cultura por outro aparelho não era falada** (meta 9): falada quando a página já recebeu um toque.
- **2ª visita com 15.783 B** (meta 15.500): `/api/culturas` com ETag + `no-cache` (304 sem corpo).
- **Pictogramas "agora" × "tardinha" com 20 % de diferença a 24 px** (meta 40 %): "agora" vira cronômetro e
  "tardinha" uma faixa de céu escuro com o sol na linha do horizonte (mínimo de 43,4 % entre todos os pares).
- **Texto a 200 % a 320 px com rolagem lateral** (374 px): chip, rótulo do botão de voz, "Feito/Técnico" e números
  quebram ou empilham.
- **Alarme da estação base tocava com dados de teste:** com o cabo solto o coletor cai em Simulação e o
  `alarme_local` bipava o Vermelho sintético enquanto a tela dizia "Dados de teste: não são da sua lavoura"
  (efeito lobo, meta 11). Agora só leitura de campo (`CAMPO_USB`) toca; `--testar-som` continua para a instalação.
- **`id_acao`/`id_leitura` acima de 2⁶³ davam 500** (`OverflowError` no SQLite) em `POST /api/acao_feita` e
  `/desfazer`: agora 400 (`test_pagina_v4.test_id_enorme_e_400_e_nao_500`).
- **Zoom do navegador de 150–200 % no celular (240–180 px de CSS) com rolagem lateral** (263/233 px): abaixo de
  320 px a lâmpada acompanha a largura, a faixa da troca de cultura rola com a página (fixa, cobria a tela
  inteira) e o botão de voz mostra só o alto-falante (rótulo mantido para o leitor de tela). Só no CSS estático.
- **Cores forçadas: o botão "Ouvir orientação" perdia o contorno** e parecia texto solto: borda `ButtonText`.
- **Suíte do agricultor medindo nada:** com o `medidas.js` da frente C, os filtros por `tipo` ficavam vazios e duas
  verificações de contraste passavam sem medir; um adaptador devolve o formato esperado e a verificação exige itens.

### Corrigido na entrega (verificação final)

- **Subjuntivo lido como ordem no Amarelo antecipado de calor:** em "É quase certo que **faça** muito calor amanhã de
  manhã. Faça sombra nas mudas novas e regue no fim da tarde…", o padrão de ordem aceitava "que faça" e o prazo
  "de manhã" virava o pictograma **"Manhã"** e a fala curta "Regue de manhã", contra "regue no fim da tarde" da voz
  completa (24 dos 1.248 itens do espaço da auditoria; a checagem de coerência não pegava porque "de manhã" está na
  frase). `acoes_orientacao._PADRAO_ORDEM` passa a ignorar o verbo depois de "que "; agora as figuras são "Sombra" +
  "Tardinha" e a fala curta "Sinal amarelo. Faça sombra e regue no fim da tarde. Faz calor."
  (`test_conteudo_voz_v4.TesteVozViraFigura.test_subjuntivo_depois_de_que_nao_e_ordem`, que falha sem a correção e
  confere em todo o espaço que a rega da figura é a mesma da ordem falada). Nenhuma regra, limiar ou frase mudou.

### Verificação da integração (do zero: data/, models/, reports/, logs/ e __pycache__ apagados)

- `python -m unittest discover -s tests`: **426 testes OK** (332 da v3 + 94 novos), nenhum pulado (regressão final da
  entrega, 16/11/2026; 425 na integração, antes do teste do subjuntivo). Na mesma regressão, do zero e com
  `AGRO_UX_ESTRITO=1`: `ux_campo` 225/225, `agricultor_v4` 403/403, `tecnico_v4` 34/34, unitários da interface 76 e
  `validar_ux.sh` 70/70 (`GET /` 14.983 B, CSS 3.551 B, Lighthouse `/` LCP 795 ms e CLS 0,001).
- `AGRO_UX_ESTRITO=1 scripts/rodar_testes_ux.sh` (axe-core 4.14 e Lighthouse 13.6 presentes): `ux_campo` 225/225,
  `agricultor_v4` 403/403, `tecnico_v4` 34/34 e `validar_ux.sh` com **70/70 metas** (a v3, medida pela mesma suíte num
  worktree de `5c66abe`, atende 19/70).
- Antes × depois (mesma suíte): oclusão pela barra 118 → 0 casos; foco escondido 7 → 0; lâmpada mínima 55,6 → 72,8 px;
  acesa ÷ apagada 3,92 → 9,98 (sol 1,91 → 4,78; cores forçadas 1,11 → 9,98; escurecimento automático 3,43 → 14,05);
  textos abaixo de AA 5 → 0; menor fonte da 1ª tela 14 → 16 px e menor contraste 5,57 → 7,34:1; incoerências
  cor × chip × voz × pictograma × causa 9 → 0 estados; fala curta com mediana de 31 → 11 palavras, ação até a 2ª
  frase 0 → 100 %; mutações nas regiões vivas em 10 min 720 → 0; `GET /` gzip 15.907 → 14.982 B; 2ª visita
  16.678 → 15.240 B. Lighthouse mobile (CPU 4×) continua 100/100/100 em `/` e `/tecnico`.
- Sistema vivo (Arduino falso em pty + coletor + servidor waitress + alarme local, ~4 min com DHT morto, lixo na
  serial, silêncio e desconexão física): nenhum traceback; Simulação mostrada como "Dados de teste" e sem alarme.

### Testes antigos alterados, com justificativa no próprio teste

- `tests/e2e/ux_campo.spec.js` caso [7]: florada e seca saíram da banca para `/tecnico` (rotas com o mesmo
  contrato); o 2º toque logo depois do "Feito" agora é "Desfazer" e a confirmação termina com "Se não fez, toque de
  novo para desfazer."; o evento "feito" da demonstração é contado em `demonstracao_excluida`.
- `tests/test_interface_campo.py::test_pin_exigido_e_limite_de_tentativas`: respostas do PIN com `codigo` e
  `segundos_restantes` (a mensagem não mudou).
- `tests/test_interface_retorno.py::test_demonstracao_desligado_e_feito_nao_tocam`: a leitura de SIMULACAO deixou
  de tocar o alarme da estação (antes o teste exigia "VERMELHO"); a leitura de campo continua tocando.
- `tests/test_integracao_v3.py::test_geada_com_terra_seca_mostra_a_rega_que_a_voz_pede`: "Regue de dia, quando o sol
  sair." no lugar de "Regue agora, de dia" ao lado de "Está gelando agora".
- Os limites antigos de tamanho (16.384 B e meta de 16.000 B) ficaram sem alteração.

### Limitações conhecidas da 4.0.0

- **Momento da rega na geada pede validação de extensionista:** com geada e terra seca a voz agora diz "Cubra as
  mudas e chegue terra no pé agora. Regue de dia, quando o sol sair." (pictograma REGAR_MANHA). Status, limiares e
  regras não mudaram; a ordem e o momento da rega precisam ser confirmados por um técnico da Emater/Incaper.
- **Compreensão dos pictogramas não testada em campo:** o gabarito ISO 9186-1 está pronto, sem resultados
  (meta de campo: ≥ 85 % de acerto, ≤ 5 % de leitura oposta, N ≥ 50); roteiro em
  [docs/PROTOCOLO_TESTE_USUARIOS.md](docs/PROTOCOLO_TESTE_USUARIOS.md). O desenho da causa "terra seca" ainda parece
  pouco legível nas capturas.
- **Nenhum agricultor participou da avaliação da v4:** tudo foi medido por ferramentas e emulação
  ([docs/AVALIACAO_UX.md](docs/AVALIACAO_UX.md), seção 9).
- **Densidade de texto acima da dobra:** as palavras visíveis subiram de 17 para 42–49, porque a mensagem longa agora
  cabe acima da barra de voz. Nenhuma meta trata disso; decidir no co-design se ela deve ficar recolhida.
- **Sem leitura nenhuma** a lâmpada amarela fica acesa ("Esperando o sensor") e o `aria-label` não tem marca própria;
  coerente com a ação "Aparelho", mas não especificado.
- **Depois de um erro 5xx** a tela pode levar até 60 s (espera crescente da consulta) para voltar a "viva" quando o
  servidor se recupera (comportamento herdado da v3).
- **ETag/polling adaptativo em `/api/status`** ficou para a v5 (só `/api/culturas` tem ETag).
- **Orçamento da página no limite:** `GET /` com gzip chega a ~14.980 B no pior caso (meta 15.000 B); qualquer
  acréscimo em `index.html` precisa sair de outro lugar (de preferência para `agricultor.css`).
- **Recarregar sem rede continua deixando a tela em branco:** sem Service Worker (http por IP da rede local não é
  contexto seguro); `overscroll-behavior-y: contain` só evita o "puxar para atualizar" acidental.
- **Fala automática sem toque:** navegadores bloqueiam voz antes do 1º toque; a troca de cultura por outro aparelho
  só é falada depois de algum toque na página.
- TalkBack, sol real, escurecimento automático de fabricante e celular Android de entrada não foram testados em
  hardware (medidos por emulação do Chromium e por modelo de pixels).

## [3.0.0] — 2026-11-15

Versão que corrige **como** a faixa do cliente é aplicada à temperatura, valida a IA em **dado climático real** e
fecha o ciclo com o agricultor (causa visível, "Feito", alarme na estação base) e com o técnico de ATER. As invariantes
continuam: saída só por semáforo (0 Verde, 1 Amarelo, 2 Vermelho) + voz + pictogramas, sem gráficos; os números do
cliente seguem **cadastrados como a faixa ideal** (Arábica 18–22 °C / solo 60–80 %; Conilon 22–26 °C / solo 55–75 %;
nenhuma linha de `parametros_cultura` mudou); os 6 módulos nos mesmos caminhos; tudo funciona offline.

Integração: merges `bd0a44d` (regras-fisiologicas), `301164c` (ia-dado-real) e `3ab2b9e` (interface-retorno) sobre
a baseline `1e20971`, mais o commit de integração (correções de costura, testes e documentação).

### Decisão agronômica: a faixa do cliente vale sobre a média de 24 h (regra `fisiologica`)

Justificativa completa, checagem das fontes e efeito medido em
[docs/PESQUISA_REFERENCIAS.md, seção 2](docs/PESQUISA_REFERENCIAS.md#2-decisão-sobre-as-regras-agronômicas).

- **Por quê:** 18–22 °C (Arábica) e 22–26 °C (Conilon) são critérios de aptidão por **temperatura média anual** do
  zoneamento ([Portaria 58/2005](https://licenciamento.cetesb.sp.gov.br/legislacao/federal/portarias/2005_Port_Fed_58.pdf);
  [Incaper, Conilon](https://biblioteca.incaper.es.gov.br/digital/bitstream/item/5052/1/tecnologiasconilon.pdf)).
  Compará-los com cada leitura horária confunde clima com tempo: em Varginha (MG), local apto (média anual de
  20,4 °C no ERA5-Land 2018–2021), a regra v2 pintava **27,6 % do ano e 37,7 % do inverno de Vermelho**, com
  **1.842 trocas de cor por ano** — o efeito "lobo" que ensina o agricultor a ignorar o Vermelho
  ([Risk Analysis 35(3), 2015](https://econpapers.repec.org/RePEc:wly:riskan:v:35:y:2015:i:3:p:385-395)).
- **Como fica** (`src/regras_agronomicas.py`, chave `regra_temperatura='fisiologica'`; status = max dos componentes):
  1. **Conforto:** faixa do cliente sobre a **média móvel de 24 h** (≥ 18 h com dado; senão a leitura), no máximo
     **Amarelo** (`DIA_FRIO`/`DIA_QUENTE`, "Não precisa fazer nada agora").
  2. **Geada (Vermelho na hora):** abrigo ≤ 2,0 °C + incerteza do sensor (DHT11 ±2,0 → **≤ 4,0 °C**; DHT22 2,5 °C)
     ([Epamig/Embrapa Café](https://revistacultivar.com.br/noticias/pesquisadores-da-epamig-e-embrapa-cafe-orientam-cafeicultores-sobre-o-que-fazer-apos-as-geadas)).
     O sensor deve ficar **em abrigo a 1,5 m do chão**, como o abrigo meteorológico.
  3. **Calor:** máxima do dia ≥ `critico_max` → Amarelo (`CALOR_DIA`); máxima ≥ max(34 °C, `critico_max`) com a
     leitura ≥ `critico_max` → Vermelho na florada (`CALOR_FLORADA`; `data_florada_principal` + 45 dias, ou set.–nov.)
     ou no 2º dia seguido (`CALOR_SEGUIDO`)
     ([Embrapa, CBAGRO 2003](https://www.alice.cnptia.embrapa.br/alice/bitstream/doc/8420/1/PC-Temperaturas-elevadas-CBAGRO-2003.pdf)).
  4. O antigo `critico_min` de temperatura (8 / 12 °C) deixa de gerar Vermelho e de ser chamado de geada.
  5. **Seca programada** opcional (desligada; 24/06–04/09,
     [Embrapa Cerrados](https://sbicafe.ufv.br/items/46703cd6-1804-4062-841a-3af5714ec5ac)): solo entre o
     `critico_min` e o ideal vira Amarelo `SECA_PROGRAMADA` ("Não regue ainda"); abaixo do crítico continua Vermelho.
- **Compatibilidade:** chave ausente = `instantanea` (v2); o coletor semeia `fisiologica` ao iniciar (o técnico pode
  voltar com `definir_configuracao('regra_temperatura', 'instantanea')`). A banca (DEMONSTRACAO) e o DATASET seguem a
  regra instantânea. Na regra fisiológica a IA do instante recebe a temperatura no ponto médio do ideal (não
  reintroduz o Vermelho térmico pelo `max`).

### Impacto medido (antes = `1e20971`, depois = esta versão)

| Base | Cultura | Vermelho no ano | Vermelho jun.–ago. | Verde no ano | Trocas de cor/ano |
|---|---|---|---|---|---|
| ERA5-Land 2018–21 (Varginha/MG), solo ideal | Arábica | 27,6 % → **0,1 %** | 37,7 % → **0,0 %** | 37,4 % → 48,4 % | 1.842 → **119** |
| ERA5-Land 2018–21 (S. Gabriel da Palha/ES), solo ideal | Conilon | 16,7 % → **0,3 %** | 23,8 % → **0,0 %** | 43,0 % → 60,3 % | 1.580 → **120** |
| Simulador (mesmo `dataset_limpo.csv`) | Arábica | 28,5 % → 16,5 % | 43,2 % → 17,9 % | 28,3 % → 30,7 % | 790 → 267 |
| Simulador (mesmo `dataset_limpo.csv`) | Conilon | 34,2 % → 26,7 % | 58,0 % → 45,8 % | 24,6 % → 26,8 % | 653 → 266 |

O Vermelho que sobra no simulador é quase todo de **solo** (o simulador não modela rega): térmico 0,1 % (Arábica) e
0,3 % (Conilon) do ano. O custo assumido é mais Amarelo **informativo** no inverno do Arábica (jul.: 42,9 % → 69,9 %
no ERA5), que é a verdade agronômica (dia abaixo do ideal) sem pedir ação de emergência. A banda morta com off-delay
reduz ainda mais as trocas no status gravado (critério de ≤ 1 troca em 720 leituras atendido em 100 de 100 sementes
novas, a 2 s e a 5 s por leitura). Tabela mês a mês: `python scripts/medir_impacto_regra.py` (offline; a saída completa está em
`docs/WORKFLOW.md`, seção "Regra térmica da v3").

**Prognóstico de 12 h** (seção 2 do relatório, simulador; o alvo mudou com o rótulo fisiológico, de 31,4 % para 7,2 %
das horas): AUC 0,919 → 0,872, Brier 0,094 → 0,034, recall 0,809 → 0,687, falso Amarelo antecipado 7,8 → 2,2 h/semana.
**Em dado real** (seção 7 nova, 2021 fora da amostra): com o rótulo v2 a IA tinha AUC 0,793/0,716 e Brier 0,211/0,219,
**pior que a climatologia** (0,173/0,189), com 22,6/35,7 h/semana de falso Amarelo; com o rótulo fisiológico o falso
Amarelo é **0,0 h/semana**, mas a IA também não vence a climatologia (Vermelho térmico real é raríssimo: 0,05 % e
0,16 % das horas de 2021) — por isso o **portão local** (abaixo) a suspende em campo até ela se provar.
**Aviso de geada em dado real (Varginha 2021):** MAE da mínima 3,30 °C → **1,02 °C**, viés −2,93 → −0,01 °C, alarmes
falsos 48 → 4 (coeficientes por região + margem conformal q90 com cobertura de 90,8 % na seca e 90,0 % nas águas).

Cada item diz o que mudou, **por que importa para o agricultor** e a referência que o sustenta (códigos de
[docs/PESQUISA_REFERENCIAS.md](docs/PESQUISA_REFERENCIAS.md), onde estão o autor, o ano, a URL e o que a fonte diz).

### Adicionado

#### Regras fisiológicas e medição de campo (frente regras-fisiologicas)

- **Regra térmica `fisiologica`** (`src/regras_agronomicas.py`: `avaliar_status_campo` em tempo real e
  `rotular_status_horario` vetorizada para o treino, com 100 % de concordância também em 3 × 2.000 h de ERA5-Land;
  `buscar_resumo_termico` com média de 24 h e máximas de hoje/ontem). *Para o agricultor:* o Vermelho volta a
  significar perigo de verdade (geada ou calor que aborta flores), em vez de pintar toda madrugada de inverno.
  Ref.: [A1](docs/PESQUISA_REFERENCIAS.md#a1) · [A2](docs/PESQUISA_REFERENCIAS.md#a2) · [A4](docs/PESQUISA_REFERENCIAS.md#a4) · [A6](docs/PESQUISA_REFERENCIAS.md#a6) · [A7](docs/PESQUISA_REFERENCIAS.md#a7) · [U2](docs/PESQUISA_REFERENCIAS.md#u2).
- **Banda morta + off-delay no status gravado** (`classificar_com_banda_morta`; 3 % da faixa crítica = 0,72 °C /
  1,8 pp; a melhora só é gravada depois de `banda_morta_atraso_s` = 60 s de folga contínua; piorar é imediato; geada e
  calor nunca esperam). *Para o agricultor:* fim do semáforo e da voz trocando de cor na borda da faixa.
  Ref.: [I11](docs/PESQUISA_REFERENCIAS.md#i11) · [P8](docs/PESQUISA_REFERENCIAS.md#p8).
- **Janela de florada** ("O café floriu hoje" na banca grava `data_florada_principal`; sem data, set.–nov.) e **seca
  programada** opcional e desligada (24/06–04/09). *Para o agricultor:* o calor de 34 °C só vira emergência quando
  ameaça as flores; quem faz estresse hídrico de propósito não ouve "regue agora" no meio da técnica.
  Ref.: [A6](docs/PESQUISA_REFERENCIAS.md#a6) · [A8](docs/PESQUISA_REFERENCIAS.md#a8) · [A9](docs/PESQUISA_REFERENCIAS.md#a9).
- **Perfil do sensor de ar no firmware** (`AGRO_SENSOR_AR` DHT11/DHT22 no mesmo pino; linha
  `#AGROANALISE_PERFIL;PROTOCOLO=2;AR=...;SOLO=...;PULSO=...` depois do banner v2, que ficou intacto); o coletor grava
  `sensor_ar_modelo` e `incerteza_sensor_ar_c` e usa os limites físicos do perfil (com DHT22, −1,5 °C deixa de ser
  anomalia e a geada fica em 2,5 °C). *Para o agricultor:* com um sensor melhor, o aviso de geada fica mais preciso
  sem mexer em nada. Ref.: [P8](docs/PESQUISA_REFERENCIAS.md#p8) · [P9](docs/PESQUISA_REFERENCIAS.md#p9).
- **Sonda de solo alimentada só na medida** (`AGRO_PINO_ALIMENTACAO_SOLO`, `lerSoloComPulso()`: 20 ms por leitura;
  padrão `-1` mantém a ligação da v2). *Para o agricultor:* o espeto corrói menos e a leitura dura mais sem
  recalibrar. Ref.: [P4](docs/PESQUISA_REFERENCIAS.md#p4).
- Colunas novas em `historico_leituras` (`motivo_regra`, `base_temperatura`, `temperatura_media_24h`,
  `umidade_solo_firmware`) e 24 chaves novas semeadas por `semear_configuracoes_v3` (Dicionário, seção 20).

#### IA validada em dado real (frente ia-dado-real)

- **Referência climática real e offline** (`referencia_climatica/`: ERA5-Land 2018–2021 de Varginha/MG e São Gabriel
  da Palha/ES, CC BY 4.0, ~0,5 MB; `src/referencia_climatica.py`; `scripts/baixar_referencia_era5land.py` +
  `requirements-referencia.txt`, opcionais e fora do campo) e **SEÇÃO 7** do relatório com climatologia mês × hora e
  persistência fora da amostra. *Para o agricultor:* os avisos passam a ser medidos contra o clima de verdade de uma
  região cafeeira, não só contra o simulador. Ref.: [I1](docs/PESQUISA_REFERENCIAS.md#i1) · [I12](docs/PESQUISA_REFERENCIAS.md#i12).
- **Prognóstico retreinado com o rótulo fisiológico** (`--regra-temperatura`, versão `-fisiologica`;
  `models/complemento_prognostico.json` com climatologia, coeficientes da mínima e margens por região; o `.pkl`
  continua com 9 chaves). *Para o agricultor:* o Amarelo antecipado falso em dado real caiu de 22,6 para
  0,0 h/semana no Arábica. Ref.: [A1](docs/PESQUISA_REFERENCIAS.md#a1) · [I1](docs/PESQUISA_REFERENCIAS.md#i1).
- **Aviso de geada recalibrado** (`tmin_prevista − margem ≤ 2,0 + incerteza`; coeficientes local > região > pacote >
  padrão; margem conformal q90 por estação; frio sem geada vira frase de frio sem "Cubra as mudas"). *Para o
  agricultor:* em Varginha 2021 os alarmes falsos de geada caíram de 48 para 4 noites; ninguém cobre muda à toa.
  Ref.: [A4](docs/PESQUISA_REFERENCIAS.md#a4) · [I3](docs/PESQUISA_REFERENCIAS.md#i3) · [I7](docs/PESQUISA_REFERENCIAS.md#i7).
- **Portão de segurança local** (`src/desempenho_local.py`, timer `agroanalise-desempenho` de hora em hora): Brier de
  28 dias do sítio contra a climatologia + Page-Hinkley no log-loss → `prognostico_suspenso`; Platt local e
  coeficientes locais da mínima. *Para o agricultor:* se a IA não acertar na propriedade dele, ela para de antecipar
  sozinha (geada e secagem continuam). Ref.: [I4](docs/PESQUISA_REFERENCIAS.md#i4) · [I10](docs/PESQUISA_REFERENCIAS.md#i10).
- **Certeza falada em dois níveis**, sem números: "É quase certo que…" (p ≥ 0,8) e "Pode ser que…". *Para o
  agricultor:* saber o grau de certeza aumenta a confiança no aviso. Ref.: [U2](docs/PESQUISA_REFERENCIAS.md#u2).

#### Interface com causa, retorno do agricultor e ponte com o técnico (frente interface-retorno)

- **Causa visível** ao lado do título (terra seca, encharcado, frio, calor, aparelho), que fala uma dica sensorial ao
  toque; pictogramas viraram botões que falam; botão "?" de ajuda falada. *Para o agricultor:* o "por quê" aparece em
  desenho e voz, sem precisar ler. Ref.: [U1](docs/PESQUISA_REFERENCIAS.md#u1).
- **"Feito" de um toque** (`POST /api/acao_feita`, sem PIN, 10/min por IP, guardado sem rede e reenviado): registra a
  ação e pausa por 60 min a repetição do Vermelho daquela causa. *Para o agricultor:* o aparelho escuta e para de
  insistir no que ele já fez. Ref.: [U3](docs/PESQUISA_REFERENCIAS.md#u3) · [U2](docs/PESQUISA_REFERENCIAS.md#u2).
- **Alarme sonoro local** na estação base (`src/alarme_local.py`, serviço `agroanalise-alarme`): toca na transição para
  Vermelho e no aviso de geada das 17–20 h, respeita o silêncio (21:30–05:00) — exceto o Vermelho de geada (`GEADA_AGORA`), que toca de madrugada, quando a geada acontece —, o "Feito" e 3 toques/10 min. *Para o
  agricultor:* o aviso chega mesmo com o celular no bolso de outra pessoa da família. Ref.: [U6](docs/PESQUISA_REFERENCIAS.md#u6).
- **Página do técnico** (`/tecnico`, PIN; resumo de 7 dias por cor e causa, avisos confirmados, ações, sensor,
  prognóstico) e **"Chamar o técnico"** (`POST /api/pedido_ajuda` → link `wa.me` aberto pelo WhatsApp do celular; o
  servidor continua offline). *Para o agricultor:* só ~20 % recebem assistência técnica; o caso chega ao técnico já
  resumido. Ref.: [U4](docs/PESQUISA_REFERENCIAS.md#u4) · [U5](docs/PESQUISA_REFERENCIAS.md#u5) · [U11](docs/PESQUISA_REFERENCIAS.md#u11).
- **Calibração da sonda no host** (`src/calibracao_sonda.py`): dois pontos ou aferição de 1 ponto no campo, por sonda e
  com data, pelo assistente da página do técnico; o coletor aplica só em `CAMPO_USB`, antes do saneador, e guarda o
  valor do firmware. *Para o agricultor:* "terra seca" passa a valer para o solo dele, sem regravar o Arduino.
  Ref.: [P1](docs/PESQUISA_REFERENCIAS.md#p1) · [P2](docs/PESQUISA_REFERENCIAS.md#p2) · [P3](docs/PESQUISA_REFERENCIAS.md#p3) · [P7](docs/PESQUISA_REFERENCIAS.md#p7) · [I9](docs/PESQUISA_REFERENCIAS.md#i9).
- Tabelas novas `registro_acoes`, `eventos_interface`, `pedido_ajuda` e `calibracao_sonda` (criadas pelos módulos).

#### Documentação

- **`docs/PESQUISA_REFERENCIAS.md`** (novo): 52 referências por tema, cada uma com URL, tipo, leitura, checagem do
  comitê, números e efeito no sistema; decisão agronômica; análises em dado real; rastreabilidade mudança → fonte;
  ideias descartadas; próximos passos de pesquisa.
- `docs/WORKFLOW.md` (seção 11, gates 14–19, riscos R19–R23), `docs/DICIONARIO_DADOS.md` (seção 20, com o esquema
  conferido por `PRAGMA` em 20.8), `docs/MAPEAMENTO_FUNCOES.md` (regenerado por `ast`: 323 itens públicos em 17
  módulos, conformidade com as interfaces da v3) e os 4 diagramas `.mmd`/`.svg` atualizados e renderizados com
  mermaid 11.4.1.

### Alterado

- **A faixa de temperatura do cliente passou a ser aplicada à média de 24 h** (no máximo Amarelo) no modo
  `fisiologica`, que o coletor liga ao iniciar; a faixa cadastrada não mudou. *Para o agricultor:* inverno do Arábica
  em dado real com 0,0 % de horas Vermelhas (era 37,7 %). Ref.: [A1](docs/PESQUISA_REFERENCIAS.md#a1) · [A2](docs/PESQUISA_REFERENCIAS.md#a2).
- **"Geada" deixou de ser qualquer leitura abaixo de `critico_min`** (8 °C no Arábica, 12 °C no Conilon): agora é o
  abrigo ≤ 2 °C + incerteza do sensor; abaixo da atenção sem geada a voz fala em **frio** e não manda cobrir mudas.
  *Para o agricultor:* "pode gear" volta a significar geada. Ref.: [A4](docs/PESQUISA_REFERENCIAS.md#a4) · [A5](docs/PESQUISA_REFERENCIAS.md#a5) · [A7](docs/PESQUISA_REFERENCIAS.md#a7).
- **Calor:** a leitura acima de 25 °C (Arábica) deixou de ser Vermelho o ano todo; Vermelho só com máxima ≥ 34 °C na
  florada ou em 2 dias seguidos. Ref.: [A6](docs/PESQUISA_REFERENCIAS.md#a6).
- **IA do instante na regra fisiológica** recebe a temperatura no ponto médio do ideal (não reintroduz o Vermelho
  térmico pelo `max`); a banca (`DEMONSTRACAO`) e o `DATASET` continuam na regra instantânea.
- **Prognóstico de 12 h:** novo alvo (rótulo fisiológico, 7,2 % das horas em vez de 31,4 %), coeficientes da mínima
  por região, margem conformal, Platt local e certeza falada; com a chave ausente ou `instantanea`, saída idêntica à
  v2. Ref.: [I3](docs/PESQUISA_REFERENCIAS.md#i3) · [I4](docs/PESQUISA_REFERENCIAS.md#i4) · [I7](docs/PESQUISA_REFERENCIAS.md#i7) · [U2](docs/PESQUISA_REFERENCIAS.md#u2).
- **Pictogramas e causa seguem a voz:** com `motivo_regra` gravado, as figuras vêm do motivo quando a frase o confirma.
- **Instalação:** `deploy/instalar.sh` habilita por glob todas as unidades `deploy/agroanalise-*.service`/`.timer`
  (inclui `agroanalise-alarme` e o par `agroanalise-desempenho`) e copia `referencia_climatica/` e `scripts/` também
  sem `rsync`; `deploy/iniciar_windows.bat` inicia o alarme.
- **Testes antigos alterados, com justificativa** (os demais 176 da v2 ficaram intactos):
  - `test_confiabilidade_campo.test_c_units_systemd_validas`: deixou de exigir a lista exata [coletor, web] (as
    especificações exigem `agroanalise-alarme.service` e o par oneshot+timer `agroanalise-desempenho`).
  - `test_interface_campo.test_minificacao_preserva_o_essencial` e `test_agroanalise.test_pagina_anuncia_so_mudanca_de_situacao`:
    o minificador de JS por tokens encurta nomes locais; os testes passaram a procurar um texto fixo que só aquela
    função produz (`"sem-leitura|"`) e `var a=1;`. Sem isso a página teria ~17,6 KB gzip.

### Corrigido (na integração das três frentes)

*Para o agricultor:* nenhum desses defeitos chegou a uma instalação; todos foram achados no sistema vivo ou na grade
de 63.180 casos e têm teste que falha sem a correção (`tests/test_integracao_v3.py`).

- **Pictogramas contra a voz na regra instantânea:** o `motivo_regra` também é gravado na banca (DEMONSTRACAO) e no
  modo `instantanea`; o mapa da regra fisiológica mostrava "Nada a fazer" num Vermelho de calor (35 °C, voz "Faça
  sombra… regue no fim da tarde"). Agora o mapa só vale quando a frase o confirma; na voz da v2 as figuras são as da
  v2 (idênticas em toda a grade testada).
- **Causa "Frio" com voz "Tudo bem":** na regra fisiológica a noite fria com média de 24 h ideal é Verde; a causa e as
  ações vinham da classe instantânea ("Frio", "Cubra as mudas"). Agora `derivar_causa` respeita o `NENHUM` gravado e
  segue a frase (`mensagem_voz`), e as ações dos motivos de solo ficam só com o que a voz pede.
- **Geada com terra seca** passou a mostrar também a rega que a voz pede (antes só cobrir/chegar terra);
  **seca programada empatada com o calor do dia** mostra "Não regue" (antes "Nada a fazer").
- **Calibração aplicada duas vezes na troca de cultura:** `app.reclassificar_medicao_de_campo` reaproveitava a
  umidade já calibrada como se viesse do firmware (+5 pp viravam +10 pp, visto no sistema vivo). Agora entra o
  `umidade_solo_firmware`.
- Chaves escritas pelo portão local (`prognostico_brier_local_28d`, `prognostico_avaliado_em`, `quantis_tmin_local`)
  agora são semeadas com tipo e descrição; descrições de `coef_tmin_local`/`platt_local` corrigidas (`'a,b,c'`/`'a,b'`).
- `deploy/instalar.sh` sem `rsync` passou a copiar `referencia_climatica/`, `scripts/` e `requirements-referencia.txt`.
- Conflito em `tests/test_confiabilidade_campo.py::test_c_units_systemd_validas` resolvido unindo as duas frentes
  (coletor e web obrigatórios; alarme 24/7 com as diretivas; desempenho oneshot com `.timer` `Persistent=true`).

### Descartado de propósito (avaliado e deixado para depois)

Detalhes e o que precisaria para voltar em [PESQUISA_REFERENCIAS.md, seção 9](docs/PESQUISA_REFERENCIAS.md#9-ideias-avaliadas-e-descartadas).

- **Previsão do tempo online (Open-Meteo):** API fora da rede para validar contra o sensor, licença só não comercial
  (decisão do cliente) e nenhuma verificação publicada da previsão de geada. *Para o agricultor:* o sistema continua
  100 % offline. Ref.: [I2](docs/PESQUISA_REFERENCIAS.md#i2).
- **ETo de Hargreaves, Kc, balanço hídrico e "água para N dias":** fórmulas confirmadas, mas Hargreaves precisa de
  calibração no Sudeste e o balanço só tem sentido físico com a sonda calibrada em capacidade de campo. Ref.:
  [A11](docs/PESQUISA_REFERENCIAS.md#a11) · [A12](docs/PESQUISA_REFERENCIAS.md#a12) · [A13](docs/PESQUISA_REFERENCIAS.md#a13) · [A14](docs/PESQUISA_REFERENCIAS.md#a14) · [I6](docs/PESQUISA_REFERENCIAS.md#i6).
- **Secagem exponencial do ETA:** evidência de preprint e de umidade modelada, não de sensor. Ref.: [I5](docs/PESQUISA_REFERENCIAS.md#i5).
- **Fenologia por graus-dia e previsão de colheita falada:** cortes de fase sem fonte; só a janela de florada entrou.
  Ref.: [A9](docs/PESQUISA_REFERENCIAS.md#a9) · [A10](docs/PESQUISA_REFERENCIAS.md#a10).
- **Protocolo serial v3, LoRa, polaridade alternada, várias sondas, DS18B20, Vcc, autodetecção I2C:** exigem hardware
  que não pôde ser testado. Ref.: [P5](docs/PESQUISA_REFERENCIAS.md#p5) · [P9](docs/PESQUISA_REFERENCIAS.md#p9) · [P11](docs/PESQUISA_REFERENCIAS.md#p11) · [A5](docs/PESQUISA_REFERENCIAS.md#a5).
- **Tensão do solo em kPa:** sem hardware. Ref.: [A15](docs/PESQUISA_REFERENCIAS.md#a15) · [U8](docs/PESQUISA_REFERENCIAS.md#u8).
- **Frio prolongado do Conilon (< 15 °C por 3 dias):** número sem fonte primária. Ref.: [A2](docs/PESQUISA_REFERENCIAS.md#a2) · [A3](docs/PESQUISA_REFERENCIAS.md#a3).
- **Recalibrar o simulador contra o ERA5-Land:** arriscava os testes antigos; a avaliação direta em dado real resolve
  o mesmo problema. Ref.: [I1](docs/PESQUISA_REFERENCIAS.md#i1).
- **Voz regional gravada, linguagem simples de ≤ 20 palavras, 7 bolinhas da semana, modo noturno:** esforço,
  licença ou orçamento de 16 KB; ficam para o co-design. Ref.: [U7](docs/PESQUISA_REFERENCIAS.md#u7) · [U9](docs/PESQUISA_REFERENCIAS.md#u9) · [U8](docs/PESQUISA_REFERENCIAS.md#u8) · [U12](docs/PESQUISA_REFERENCIAS.md#u12).
- **QC "subida sem causa" e desvio do DHT11 contra referência externa; histórico de acertos falado:** dependem de
  previsão online ou de semanas de campo. Ref.: [I8](docs/PESQUISA_REFERENCIAS.md#i8) · [U13](docs/PESQUISA_REFERENCIAS.md#u13).

### Limitações conhecidas da 3.0.0

Próximos passos de pesquisa para cada uma em [PESQUISA_REFERENCIAS.md, seção 10](docs/PESQUISA_REFERENCIAS.md#10-próximos-passos-de-pesquisa).

- **Regras fisiológicas sem validação de campo:** os limiares vêm de fontes conferidas, mas muitas foram lidas pelo
  resumo (domínios fora da rede); o limiar de geada do Conilon é o mesmo do Arábica (a espécie é mais sensível ao
  frio, [A3](docs/PESQUISA_REFERENCIAS.md#a3)), e a seca programada usa o crítico do solo como piso. Falta revisão com
  agrônomo da Epamig/Embrapa Café e do Incaper e um piloto com o sensor em abrigo.
- **Prognóstico sem ganho em dado real:** com o rótulo fisiológico a IA não vence a climatologia na reanálise (AUC
  0,48/0,64; recall 0 no ano de 2021, que teve 24/72 horas de transição para Vermelho). O falso Amarelo é zero, e o
  portão local suspende o modelo se o Brier do sítio não vencer a climatologia; a antecipação útil em campo vem da
  secagem do solo (ETA) e da geada.
- **Page-Hinkley:** o critério "dispara em até 100 passos após um degrau de +0,5" é inalcançável com os padrões
  `delta=0,005` e `limiar=50` (sem ruído dispara no passo 112; com ruído, 110–112). Mantidos os padrões; o teste exige
  ≤ 120 passos.
- **Página no limite:** com a voz fisiológica mais longa (geada + terra seca) e um nome de propriedade de 55 letras,
  `GET /` chega a 16.006 B gzip (meta da v3: 16.000 B; limite do teste antigo: 16.384 B). Com o nome padrão, 15.978 B.
- **Off-delay no primeiro minuto:** depois de reiniciar, o estado da banda é semeado pela última leitura gravada; a
  volta ao Verde espera 60 s de folga contínua (visto no sistema vivo).
- **Reanálise ≠ talhão:** o ERA5-Land (~11 km) suaviza a geada radiativa de baixada; serve para medir métodos, não
  para contar geadas no sítio. Hardware (DHT22, sonda pulsada, alarme com caixa de som) só testado em emulação.
- Herdadas da v2 e ainda válidas: simulador sem rega (Vermelho de solo alto no simulador), critério literal de geada
  de 12 °C, veranico dependente do sorteio, firmware só com `g++ -fsyntax-only`.

## [2.0.0] — 2026-11-14

Versão que faz o sistema **antecipar** o estresse hídrico e térmico (requisito do cliente que a v1 não cumpria: a IA
só copiava a regra do instante), que aguenta o campo 24 horas por dia sem técnico por perto e que se faz entender por
quem não lê, mesmo sem voz em português ou com rede fraca. As invariantes da v1 foram mantidas: saída só por semáforo
(0 Verde, 1 Amarelo, 2 Vermelho) e voz, sem gráficos; faixas agronômicas idênticas (Arábica 18–22 °C / solo 60–80 %;
Conilon 22–26 °C / solo 55–75 %); os 6 módulos nos mesmos caminhos; os 57 testes da v1 sem alteração.

Integração: merges `a1e1bbe` (antecipacao-ia), `f8a7ef5` (confiabilidade-campo) e `e80b49c` (interface-campo) sobre a
baseline `160055b`.

### Adicionado

#### Antecipação de estresse (frente antecipacao-ia)

- **IA de prognóstico de 12 h** (`src/prognostico.py`, `models/modelo_prognostico.pkl`): RandomForest calibrado
  (isotônico, dobras temporais com intervalo de 12 h) sobre 36 features de tendência das últimas 24 h de campo
  (variações de 1/3/6/12 h, médias e extremos, VPD, horas em estresse, margens até as bordas da faixa, hora do dia).
  Limiar escolhido por orçamento: no máximo 14 h/semana de Amarelo antecipado falso.
  *Para o agricultor:* o semáforo fica Amarelo **antes** do problema, com tempo de regar, sombrear ou proteger —
  no simulador, 86,7 % dos inícios de Vermelho tiveram pelo menos 3 h de aviso, com mediana de 10,5 h.
- **Regra de segurança da antecipação** (`aplicar_antecipacao`): só leituras de campo com 18 das últimas 24 h de
  dados; só leva de Verde para Amarelo; nunca cria um Vermelho, nunca rebaixa uma cor; nunca lança exceção.
  *Para o agricultor:* o Vermelho continua significando perigo agora, medido — a previsão nunca assusta à toa.
- **Quando a terra seca** (`estimar_horas_ate_secar`): ETA até a borda de atenção do solo pela perda das últimas
  24 h, ignorado depois de chuva ou rega. *Para o agricultor:* "a terra deve ficar seca amanhã de manhã. Regue hoje
  no fim da tarde."
- **Aviso de geada ainda à tarde** (`prever_temperatura_minima_noite`): entre 17 h e 19 h, a mínima da madrugada é
  prevista pelo ponto de orvalho de Magnus (`gerador_dataset.ponto_orvalho_magnus`); coeficientes ajustados no treino.
  *Para o agricultor:* "Esta noite pode gear. Antes de escurecer, cubra as mudas novas e chegue terra no pé das
  plantas." — dito com luz do dia, quando ainda dá para agir (100 % das geadas do teste avisadas até as 19 h).
- **Motivo, prazo e memória de estresse falados** (`explicar_alerta`, `gerar_frase_antecipacao`, `falar_prazo`):
  motivos `SECA_CHEGANDO`, `SECA_AGORA`, `ENCHARCAMENTO`, `CALOR_TARDE`, `GEADA_NOITE`, `FRIO`; prazos sem números
  ("daqui a pouco", "esta noite", "amanhã de manhã"); "A terra está seca há 9 horas. Regue agora."
- **Tabela `analise_antecipacao`** (1:1 com a leitura, `ON DELETE CASCADE`) com probabilidade, ETA, mínima prevista,
  motivo, prazo, horas em estresse e versão do prognóstico. *Para a assistência técnica:* toda antecipação é auditável.
- **Simulador climático realista** (`simular_serie_temporal`, 8.760 h): estações do hemisfério sul com mínimo em
  julho, chuva por mês (águas de outubro a março), veranico de 10 a 20 dias, geada radiativa de maio a agosto,
  escaldadura rara; `gerar_relatorio_realismo` → `reports/realismo_simulador.txt`. Funciona para qualquer cultura
  (eventos relativos às faixas). *Para o agricultor:* a IA aprende situações que acontecem de verdade na roça.
- **Avaliação honesta** (`reports/relatorio_treinamento.txt` em 6 seções com rótulo de escopo): fidelidade à regra
  (não é desempenho de campo), generalização temporal (`dividir_temporal`, `validacao_temporal`), robustez a ruído de
  sensor (`avaliar_robustez_ruido`), teste de antecipação (`avaliar_antecipacao`), calibração (Brier e
  `reports/curva_confiabilidade.png`) e geada. *Para quem decide:* nenhum "100 %" aparece sem dizer o que mede.

#### Confiabilidade 24/7 do campo (frente confiabilidade-campo)

- **Mediana móvel antes da regra** (`qualidade_sensores.FiltroMedianaMovel`, 12 leituras; valor além do crítico
  passa direto). *Para o agricultor:* o semáforo não pisca entre verde e amarelo por causa do tremido do sensor, e um
  perigo real não espera.
- **Diagnóstico de sensor solto, travado ou impossível** (`DiagnosticoSensores`, `EstadoSensores`, colunas
  `estado_sensor` e `diagnostico_sensor`). Sensor suspeito dá no máximo Amarelo e a voz diz "O sensor da terra
  parece solto ou quebrado. Confira o espeto na terra. Não regue só por causa deste aviso: olhe a terra com a mão."
  *Para o agricultor:* um espeto solto não manda mais regar uma lavoura encharcada.
- **Protocolo serial v2** (`$2,t,ur,solo*XX` com checksum XOR, `LinhaSerial`, `interpretar_linha_serial_detalhada`,
  `calcular_checksum_nmea`, `ContadoresProtocolo`), banner `#AGROANALISE_PRONTO;PROTOCOLO=2`, linha com `nan`
  quando o DHT11 falha, **watchdog de 8 s** no firmware, descarte da 1ª linha depois de abrir a porta e saneador
  semeado pela última leitura de campo (`SaneadorTempoReal.semear`, `buscar_ultima_leitura_origem`).
  *Para o agricultor:* ruído no cabo ou linha cortada não vira "Perigo, geada"; placa travada volta sozinha; DHT11
  quebrado não apaga a medição da terra.
- **Plausibilidade física da queda do solo** no saneador do campo (`taxa_maxima_queda_solo_pp_h = 8`).
- **Gravação sem perda**: fila em memória de 720 leituras quando o banco trava, `inserir_leituras_em_lote` numa
  transação, configurações na mesma transação da leitura (`configuracoes_extras`).
- **Batimento do coletor e vigias**: `coletor_batimento_em`, `coletor_pid`, `coletor_ultimo_erro`,
  `coletor_fila_pendente`, `disco_livre_mb`, contadores `serial_*`; vigia de salto de relógio
  (`relogio_ultimo_salto_em`) e de disco (`espaco_minimo_mb`).
- **Relógio à prova de saltos**: coluna `instante_utc` (ISO UTC) e histórico ordenado por `id_leitura`.
  *Para o agricultor:* uma leitura com data errada não "congela" o semáforo numa cor velha.
- **Retenção, agregação, backup e compactação** (`aplicar_retencao`, tabela `historico_agregado_hora`,
  `fazer_backup`, CLI `--backup`, `--compactar`, `--reter`; `auto_vacuum = INCREMENTAL`).
  *Para o agricultor:* o computador do sítio não enche o disco e não perde o histórico de anos.
- **Migração automática** de bancos v1 (`migrar_esquema`, idempotente).
- **Operação como serviço**: `src/servidor.py` (waitress), `src/registro.py` (logs rotativos 5 × 5 MB + journald),
  `deploy/agroanalise-coletor.service` e `deploy/agroanalise-web.service` (`Restart=always`), `deploy/instalar.sh`
  (idempotente), `deploy/iniciar_windows.bat`. *Para o agricultor:* falta de luz ou erro não deixa o sistema parado
  esperando alguém.
- Novas configurações semeadas: `janela_mediana_leituras`, `taxa_maxima_queda_solo_pp_h`, `minutos_travamento_solo`,
  `horas_travamento_ar`, `exigir_protocolo_v2`, `espaco_minimo_mb`, `retencao_dias_brutos` e as de batimento e
  contadores (17 no total).
- Stubs `tests/stubs_arduino/` para conferir o firmware com `g++ -fsyntax-only`.

#### Interface do agricultor (frente interface-campo)

- **Pictogramas de ação** (`src/acoes_orientacao.py`, `derivar_acoes`, campo `acoes` em `/api/status`): regar agora,
  regar no fim da tarde, regar de manhã, não regar, abrir valetas, fazer sombra, cobrir mudas, chegar terra, nada a
  fazer, verificar o aparelho — até 3, coerentes com a frase falada (teste de propriedade em 5.000 leituras).
  *Para o agricultor:* quem não lê vê a cor, o motivo e **o que fazer** na primeira tela, sem rolar.
- **Primeira tela sem rolar** a 360×740 e 360×640 (semáforo horizontal em telas baixas, barra de voz medida com
  `ResizeObserver`), botões ≥ 48 px.
- **Proteção contra toques acidentais** (`src/seguranca_banca.py`): painel da banca recolhido (`?banca=1` ou toque
  longo de 2 s), PIN opcional guardado como hash PBKDF2-SHA256, 5 tentativas por IP por minuto, corpo limitado a
  4 KB, troca de cultura com confirmação Sim/Não e aviso "A plantação foi trocada para … às HH:MM" nos outros
  aparelhos. *Para o agricultor:* uma criança mexendo no celular não muda o diagnóstico de todos.
- **Voz que não falha calada**: exige voz pt; sem ela, aviso grande e bipes por cor (1, 2, 3); preferência lembrada;
  Vermelho repetido a cada 600 s com vibração; o estado não é dito duas vezes.
- **Rede fraca**: gzip, estado embutido na página, consulta adaptativa (5 s visível, pausa oculta, recuo até 60 s),
  último estado guardado e mostrado como "Última notícia de hoje às HH:MM — sem ligação agora".
- **Status e saúde honestos**: `/api/status` com `motivo_principal`, `prazo_horas`, `horas_em_estresse`,
  `antecipacao`, `estado_sensor`, `relogio_inconsistente`, `coletor_vivo`, troca de cultura e
  `intervalo_repeticao_alerta_segundos`; títulos "Confira o aparelho", "Relógio do aparelho errado" e "O programa
  parou"; `/api/saude` com `componentes` e 503.
- Testes: `tests/test_antecipacao_ia.py` (48), `tests/test_confiabilidade_campo.py` (43),
  `tests/test_interface_campo.py` (28) e `tests/e2e/ux_campo.spec.js` (Playwright, 167 verificações) com
  `scripts/rodar_testes_ux.sh`.

#### Documentação

- `docs/WORKFLOW.md`, `docs/DICIONARIO_DADOS.md` e `docs/MAPEAMENTO_FUNCOES.md` reescritos para o código final; os 4
  diagramas Mermaid atualizados (arquitetura, DER com as 2 tabelas novas, sequência com a antecipação, máquina de
  estados com as vigias) e re-renderizados; este `CHANGELOG.md`; seção "Novidades da v2" no README.

### Alterado

- **Frase de geada no instante** (`gerar_mensagem_orientacao`, mudança deliberada): abaixo de `critico_min`, "Hoje à
  noite pode gear. Cubra as mudas…" virou "Está gelando agora. Cubra as mudas e chegue terra no pé das plantas assim
  que puder." *Motivo:* a frase antiga avisava no passado — a geada já estava acontecendo; o "pode gear" agora sai à
  tarde, pelo prognóstico.
- `HORAS_SIMULACAO` de 5.000 para **8.760** e início do dataset em `2025-11-01` (ano agrícola completo): o dataset
  passou de 10.000 para 17.520 linhas.
- `processar_leitura` ganhou kwargs só por palavra-chave (`filtro`, `estado_sensores`, `modulo_antecipacao`,
  `configuracoes_extras`); a chamada posicional da v1 continua válida. A lógica foi separada em
  `montar_registro_leitura` + `gravar_registro_leitura`.
- `inserir_leitura` ganhou `configuracoes_extras` (opcional); `RegistroLeitura` ganhou `estado_sensor`,
  `diagnostico_sensor` e `instante_utc` com valor padrão (construtores da v1 continuam válidos).
- `buscar_historico` e `buscar_ultima_leitura` ordenam por `id_leitura DESC` (ordem de gravação), com os índices
  novos `idx_historico_cultura_id` e `idx_historico_cultura_origem_id`.
- `interpretar_linha_serial` aceita v1 e v2 e devolve `None` para checksum inválido (mesma assinatura e retorno).
- `demonstracao_expira_em` passou a ser gravada em ISO UTC com `+00:00`; coletor e Flask aceitam os dois formatos.
- O coletor registra uma linha por leitura só em DEBUG (`--verboso`) e um resumo INFO a cada 5 min.
- `index.html` reorganizado: ordem semáforo → título → ações → detalhes → culturas; seletores de cultura saem do topo;
  selo técnico do modo só com `?banca=1`.
- Relatório de treino: as métricas da v1 agora ficam sob "SEÇÃO 1 — Fidelidade à regra (não é desempenho de campo)".
- `requirements.txt`: `waitress>=3.0`. `.gitignore`: `logs/` e `*.log.*`.

### Corrigido

- **Voz × pictograma na secagem iminente:** com o motivo `SECA_CHEGANDO` e a voz dizendo "Regue agora", o pictograma
  mostrava "Regue no fim da tarde". Agora `derivar_acoes(..., mensagem_voz=...)` escolhe o pictograma de rega pelo
  momento que a voz realmente pediu — a figura nunca contradiz o que o agricultor ouve (3 testes de regressão).
- SVGs dos diagramas (já desde a v1) tinham `<br>` sem fechar dentro de `foreignObject` e não abriam como arquivo
  isolado no navegador (erro de XML); agora saem com `<br/>` e são validados como XML.

### Descartado de propósito

- **PWA (Service Worker + manifest):** exigiria HTTPS na rede local do sítio. A página usa estado embutido e
  `localStorage` com a idade da última notícia no lugar.

### Limitações conhecidas da 2.0.0

- **Simulador × campo:** todas as métricas de prognóstico (AUC 0,919, Brier 0,094, 86,7 % de inícios de Vermelho com
  ≥ 3 h de aviso) e de geada (100 % avisadas, erro de 0,33 °C) vêm do simulador climático, cuja geada é gerada pela
  mesma física de ponto de orvalho usada na previsão. O desempenho de campo só será conhecido com leituras reais.
- **Calibração possivelmente otimista:** `FRACAO_DOBRA_CALIBRACAO` foi escolhida olhando o conjunto de teste temporal;
  o Brier do teste (0,0942, igual ao não calibrado) pode estar um pouco otimista.
- **Critério literal de geada inalcançável:** o caso "12 °C e UR 40 % às 18 h → Amarelo no Arábica" não pode ser
  cumprido, porque 12 °C já está abaixo de `atencao_min = 15` (Vermelho pela regra, e a antecipação nunca rebaixa).
  O aviso de geada com Amarelo foi provado com 18 °C e UR 25 %.
- **Inverno muito Vermelho no Arábica:** com o simulador realista e as faixas fornecidas, 35 a 53 % das horas de
  jun.–ago. ficam Vermelhas (abaixo de 15 °C). É consequência das regras, que não foram alteradas; vale revisar as
  faixas com o agrônomo.
- **Veranico depende do sorteio:** `PROB_VERANICO_ANO = 0,85`; com a semente 2024 do Conilon não há 10 dias seguidos
  sem chuva em jan.–mar. (o critério exige só a semente 42).
- **Mediana sensível à semente:** com σ = 1 p.p. a 1 p.p. da borda do solo, 6 de 20 sementes testadas na integração
  tiveram de 2 a 7 trocas de cor gravadas em 720 leituras (o critério exige semente fixa; a histerese da tela
  suaviza). Uma histerese no status gravado ou janela maior seria mais robusta.
- **Página no limite de tamanho:** `GET /` comprimido pesa 16.006 B no pior caso, contra o limite de 16.384 B do teste
  `test_pagina_comprimida_e_pequena` (378 B de folga).
- **Prognóstico em cultura nova:** a IA de prognóstico não é bloqueada por cultura (features relativas às faixas, por
  desenho), mas só foi validada para os dois cafés.
- **Hardware real:** o firmware v2 e o watchdog não foram testados em placa física nem com toolchain AVR (só
  `g++ -fsyntax-only` com stubs e emulador em pty); o instalador systemd não foi executado numa máquina real (só
  `bash -n` e `systemd-analyze verify`).
- O DHT11 não mede abaixo de 0 °C: numa geada forte o valor registrado satura em 0 °C (marcado como anomalia
  `negativo` se vier abaixo); o aviso de geada sai antes, à tarde.

## [1.0.0] — 2026-11-14

Baseline do MVP (commit `160055b`).

### Adicionado

- **Firmware** `arduino/coleta_sensores.ino`: DHT11 no D2 e higrômetro no A0 com média móvel de 10 amostras sem
  `delay()`, `isnan` no DHT11, avisos de diagnóstico com `#`, CSV `t,ur,solo` a 9600 bps a cada 2 s, LED de
  heartbeat. *Para o agricultor:* um aparelho simples e barato no meio da lavoura.
- **Banco** `src/database.py`: SQLite em WAL com `culturas`, `parametros_cultura` (formato longo, escalável),
  `historico_leituras` e `configuracao_sistema`; faixas do Café Arábica e do Café Conilon (Embrapa/Incaper);
  `cadastrar_cultura` com validação. *Para o agricultor:* outras culturas entram sem programação nova.
- **Dados** `src/gerador_dataset.py`: motor de regras (`classificar_variavel`, `calcular_status_regra`), features
  `deficit_hidrico_solo` e `desvio_termico`, mensagem falada ESTADO + CAUSA + AÇÃO, simulação de 5.000 h por cultura,
  ruído de 3 %, detector e saneamento de anomalias, 6 cenários de demonstração.
- **IA** `src/treinamento_ia.py`: RandomForest × Regressão Logística, validação cruzada de 5 dobras, prova
  operacional de Falsos Positivos (água desperdiçada) e Falsos Negativos (safra perdida), `models/modelo_agroanalise.pkl`.
- **Coletor 24/7** `src/coletor_serial.py`: serial com autodetecção, fallback automático para Simulação, Modo
  Demonstração, saneador em tempo real, `status = max(regra, IA)` (princípio da precaução), recarga a quente do modelo.
  *Para o agricultor:* o semáforo nunca apaga porque um cabo saiu.
- **Companheiro Virtual** `src/app.py` + `src/templates/index.html`: semáforo gigante, voz pt-BR (Web Speech API),
  histerese para não piscar, aviso de leitura velha e de dados de teste, botões da banca, sem CDN.
- 57 testes (`tests/test_agroanalise.py`) e documentação de governança em `docs/` com 4 diagramas Mermaid.

[Não publicado]: #nao-publicado
[4.0.0]: #400--2026-11-15
[2.0.0]: #200--2026-11-14
[1.0.0]: #100--2026-11-14
