/*
 * Matriz de estados da suíte de validação UX (v4): mede e compara com as metas.
 *
 * Percorre os estados do servidor de estados (tests/e2e/validacao/servidor_estados.py)
 * em 6 tamanhos de tela (360×640, 360×740, 360×800, 393×873, 412×915 e 1280×800),
 * mais "sem rede depois da 1ª carga", 503 e "sem voz pt-BR", e junta num só JSON:
 *   grade      retrato do estado, contraste de texto e ícone, alvos ≥ 48 px, oclusão pela
 *              #barra-voz, erros de console, recursos de outro host e captura de cada caso;
 *   foco       foco obscurecido pela barra depois de 25 Tabs (360×740);
 *   axe        axe-core em cada estado × 360×740 e 1280×800 (+ cobertura dos "incompletos"
 *              de contraste pela medição própria);
 *   tecnico    /tecnico com e sem o assistente aberto (alvos, contraste, axe, captura);
 *   texto      texto a 130 % e 150 % a 320 e 360 px (rolagem lateral e colisões no topo),
 *              font-size em px no CSS e meta text-scale das duas páginas;
 *   pixels     lâmpadas: razão acesa ÷ apagada, ofuscamento de +0,10, 4 filtros de
 *              daltonismo, cores forçadas, escurecimento automático e vivo × esmaecido;
 *   vivas      regiões vivas, alarmes, retorno falado, repetição da voz e "Feito";
 *   bytes      GET / gzip (pior caso), CSS estático, 2ª visita, Slow 3G + CPU 6× e hosts;
 *   picto      diferença de pixels dos pictogramas a 24 px, mapas injetivos e gabarito ISO;
 *   lighthouse Lighthouse mobile (mediana de 3 rodadas) em / e /tecnico.
 *
 * Uso:
 *   node tests/e2e/validacao/matriz_estados.js medir --base http://127.0.0.1:5210 --saida reports/ux4/<data> [--grupos grade,foco]
 *   node tests/e2e/validacao/matriz_estados.js comparar --saida reports/ux4/<data> [--linha-de-base]
 * "comparar" junta medidas_node.json, coerencia.json e legibilidade.json, calcula os
 * indicadores, compara com tests/e2e/validacao/metas_v4.json e grava resultado.json
 * (código de saída 1 se alguma meta falhar; 0 com --linha-de-base). Ferramenta ausente
 * (axe-core, Lighthouse) vira "PULADO" — e falha com AGRO_UX_ESTRITO=1.
 *
 * Exporta as funções de medida e de comparação (para os testes e para a frente A).
 */
"use strict";

const fs = require("fs");
const http = require("http");
const path = require("path");
const zlib = require("zlib");
const { chromium } = require("playwright");
const medidas = require("./medidas.js");
const pixels = require("./pixels.js");
const vivas = require("./regioes_vivas.js");
const ferramentas = require("./axe_lighthouse.js");

// AGRO_UX_PROJETO aponta para outra cópia do projeto (por exemplo a v3, para a linha de base).
const RAIZ = path.resolve(process.env.AGRO_UX_PROJETO || path.join(__dirname, "..", "..", ".."));
const VIEWPORTS = [[360, 640], [360, 740], [360, 800], [393, 873], [412, 915], [1280, 800]];
const CELULARES = VIEWPORTS.filter(([w]) => w < 600);
const VIEWPORTS_AXE = ["360x740", "1280x800"];
const CORES = ["verde", "amarelo", "vermelho"];
// Classes de estado da matriz (pelo nome do estado no servidor de estados).
const VIVOS = ["verde", "amarelo", "vermelho", "vermelho_geada", "florada_calor", "dia_frio", "seca_programada", "encharcado",
  "antecipa_seca", "antecipa_geada", "antecipa_calor", "anomalia", "troca_cultura", "sem_voz", "banca"];
const ESMAECIDOS = ["leitura_antiga", "coletor_parado", "relogio_errado", "offline", "erro_503"];
const NEUTROS = ["simulacao", "simulacao_verde", "demonstracao_GEADA"];
const ESTADOS_PIXELS = ["verde", "amarelo", "vermelho", "vermelho_geada", "antecipa_seca", "leitura_antiga", "coletor_parado",
  "relogio_errado", "offline", "erro_503", "simulacao", "simulacao_verde", "demonstracao_GEADA"];
const FILTROS = ["protanopia", "deuteranopia", "tritanopia", "acromatopsia"];
const ESTADOS_TEXTO = ["verde", "vermelho", "leitura_antiga", "simulacao", "sem_leitura", "sensor_defeito", "troca_cultura"];
const ERROS_DE_REDE = [/Failed to load resource/i, /ERR_INTERNET_DISCONNECTED/, /status of 50\d/, /net::ERR_/];
const SLOW_3G = { offline: false, latency: 2000, downloadThroughput: 500 * 1000 / 8 * 0.8, uploadThroughput: 500 * 1000 / 8 * 0.8 };
const ESPERA_REDE_MS = 15000;

/** Lê "--chave valor" e "--bandeira" da linha de comando. */
function lerArgumentos(argv) {
  const saida = { _: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a.startsWith("--")) {
      const chave = a.slice(2);
      if (i + 1 < argv.length && !argv[i + 1].startsWith("--")) { saida[chave] = argv[i + 1]; i += 1; } else { saida[chave] = true; }
    } else { saida._.push(a); }
  }
  return saida;
}

/** Arredonda para `casas` decimais (null passa direto). */
function arred(valor, casas) {
  if (valor === null || valor === undefined || Number.isNaN(valor)) { return null; }
  const f = Math.pow(10, casas === undefined ? 2 : casas);
  return Math.round(valor * f) / f;
}

/** Nome de arquivo seguro. */
function nomeArquivo(texto) { return String(texto).replace(/[^A-Za-z0-9_.-]+/g, "_"); }

/** GET bruto (sem descompactar) com Accept-Encoding: gzip → {status, cabecalhos, bytes, corpo}. */
function getBruto(url, cabecalhos) {
  return new Promise((resolver, rejeitar) => {
    const pedido = http.get(url, { headers: Object.assign({ "Accept-Encoding": "gzip" }, cabecalhos || {}) }, (resposta) => {
      const pedacos = [];
      resposta.on("data", (p) => pedacos.push(p));
      resposta.on("end", () => {
        const bruto = Buffer.concat(pedacos);
        let corpo = bruto;
        if (/gzip/.test(resposta.headers["content-encoding"] || "")) { try { corpo = zlib.gunzipSync(bruto); } catch (erro) { corpo = bruto; } }
        resolver({ status: resposta.statusCode, cabecalhos: resposta.headers, bytes: bruto.length,
          gzip_bytes: /gzip/.test(resposta.headers["content-encoding"] || "") ? bruto.length : zlib.gzipSync(bruto, { level: 9 }).length,
          comprimido_pelo_servidor: /gzip/.test(resposta.headers["content-encoding"] || ""), corpo: corpo.toString("utf8") });
      });
    });
    pedido.on("error", rejeitar);
    pedido.setTimeout(20000, () => pedido.destroy(new Error("tempo esgotado: " + url)));
  });
}

/** Configura um estado no servidor de estados (devolve {estado, banco, cliente}). */
async function configurar(base, nome) {
  const r = await vivas.post(base, "/__ux/estado/" + encodeURIComponent(nome));
  if (r.status !== 200) { throw new Error("Estado " + nome + " não configurado: HTTP " + r.status); }
  return r.dados;
}

/**
 * Abre `/` num estado, com a ação do navegador que o estado pede depois da 1ª carga.
 * @returns {Promise<{contexto, page, erros: string[], hosts: string[], info}>}
 */
async function abrirEstado(nav, base, nome, viewport, opcoes) {
  const o = opcoes || {};
  const info = await configurar(base, nome);
  const lingua = info.cliente === "sem_voz" ? "en-US" : "pt-BR";
  const contexto = await nav.newContext(Object.assign({ viewport: { width: viewport[0], height: viewport[1] }, deviceScaleFactor: o.dpr || 1, locale: "pt-BR" }, o.contexto || {}));
  await contexto.addInitScript(medidas.scriptVozFalsa(lingua));
  const page = await contexto.newPage();
  const erros = [];
  const hosts = [];
  const hostBase = new URL(base).host;
  page.on("console", (m) => { if (m.type() === "error" && !ERROS_DE_REDE.some((p) => p.test(m.text()))) { erros.push(m.text()); } });
  page.on("pageerror", (e) => erros.push("pageerror: " + e.message));
  page.on("request", (r) => { try { const u = new URL(r.url()); if (/^https?:$/.test(u.protocol) && u.host !== hostBase) { hosts.push(u.host); } } catch (erro) { /* data: e blob: */ } });
  await page.goto(base + (info.cliente === "banca" || o.banca ? "/?banca=1" : "/"), { waitUntil: "load" });
  await vivas.esperarPronto(page, 8000);
  await page.waitForTimeout(250);
  if (info.cliente === "sem_rede") {
    await contexto.setOffline(true);
    await page.waitForFunction(() => /sem-ligacao|desatualizada|offline|sem-rede/.test((document.body.getAttribute("data-confianca") || "") + " " + document.body.className), null, { timeout: ESPERA_REDE_MS }).catch(() => null);
    await page.waitForTimeout(300);
  } else if (info.cliente === "erro_503") {
    await vivas.post(base, "/__ux/falha", { status: 503 });
    await page.waitForFunction(() => /erro-servidor|erro|desatualizada|sem-ligacao/.test((document.body.getAttribute("data-confianca") || "") + " " + document.body.className), null, { timeout: ESPERA_REDE_MS }).catch(() => null);
    await page.waitForTimeout(300);
  } else if (info.cliente === "trocar_cultura") {
    await vivas.post(base, "/__ux/cultura/cafe_conilon");
    await page.waitForFunction(() => { const f = document.getElementById("faixa-troca"); return f && !f.hidden; }, null, { timeout: 9000 }).catch(() => null);
    await page.waitForTimeout(300);
  } else if (info.cliente === "sem_voz") {
    await page.locator("#botao-voz").click({ timeout: 3000 }).catch(() => null);
    await page.waitForTimeout(500);
  }
  return { contexto, page, erros, hosts, info };
}

/** Lista de estados do servidor (GET /__ux/estados). */
async function listarEstados(base) {
  const r = await vivas.get(base, "/__ux/estados");
  if (r.status !== 200 || !Array.isArray(r.dados)) { throw new Error("Servidor de estados indisponível em " + base); }
  return r.dados;
}

/** Ouve a fala do botão de voz (texto falado ao 1º toque). */
async function ouvirFala(page) {
  const antes = await page.evaluate(() => (window.__falas || []).length);
  await page.locator("#botao-voz").click({ timeout: 2500 }).catch(() => null);
  await page.waitForTimeout(500);
  return page.evaluate((n) => (window.__falas || []).slice(n).map((f) => f.texto), antes);
}

/** Cobertura dos "incompletos" de contraste do axe pela medição própria (texto medido no elemento). */
async function coberturaIncompletos(page, alvos) {
  await medidas.instalar(page);
  return page.evaluate((lista) => {
    let cobertos = 0;
    for (const alvo of lista) {
      let el = null;
      try { el = document.querySelector(alvo); } catch (erro) { el = null; }
      if (!el) { cobertos += 1; continue; }
      const medidos = window.__uxMedidas.textos(el);
      const temTexto = el.textContent.trim().length > 0 && window.__uxMedidas.visivel(el);
      if (medidos.length || !temTexto) { cobertos += 1; }
    }
    return { total: lista.length, cobertos };
  }, alvos);
}

// ---------------------------------------------------------------------------
// Grupos de medida
// ---------------------------------------------------------------------------

/** Grade estados × viewports: retrato, contraste, ícones, alvos, oclusão, console, hosts, axe e captura. */
async function grupoGrade(nav, base, estados, saida, opcoes) {
  const capturas = path.join(saida, "capturas");
  fs.mkdirSync(capturas, { recursive: true });
  const axe = ferramentas.localizarAxe();
  const resultado = { casos: [], axe: { disponivel: !!axe, casos: [] } };
  for (const estado of estados) {
    for (const vp of VIEWPORTS) {
      const rotulo = vp[0] + "x" + vp[1];
      const { contexto, page, erros, hosts, info } = await abrirEstado(nav, base, estado.nome, vp);
      try {
        const retrato = await medidas.coletarEstado(page);
        const contraste = await medidas.medirContraste(page, null);
        const icones = await medidas.medirIcones(page, null);
        const alvos = await medidas.medirAlvos(page, null);
        const oclusao = await medidas.medirOclusao(page, ["#acoes", "#feito", "#tecnico"]);
        const captura = path.join(capturas, nomeArquivo(estado.nome + "_" + rotulo) + ".png");
        await page.screenshot({ path: captura, animations: "disabled" });
        const caso = { estado: estado.nome, viewport: rotulo, cliente: info.cliente, retrato,
          contraste_falhas: contraste.filter((t) => t.razao < t.minimo),
          contraste_primeira_tela: contraste.filter((t) => t.acima_dobra).map((t) => ({ seletor: t.seletor, texto: t.texto, tamanho_px: t.tamanho_px, razao: t.razao })),
          icones_falhas: icones.filter((i) => i.controle && i.razao < 3),
          alvos_pequenos: alvos.filter((a) => a.altura < 48),
          oclusao, erros_console: erros.slice(0, 10), hosts_externos: Array.from(new Set(hosts)), captura: path.relative(saida, captura) };
        if (rotulo === "360x740") {
          caso.api = (await vivas.get(base, "/api/status")).dados;
          caso.falas = await ouvirFala(page);
        }
        if (axe && VIEWPORTS_AXE.includes(rotulo) && !(opcoes && opcoes.semAxe)) {
          const a = await ferramentas.rodarAxe(page, { caminho: axe }).catch((erro) => ({ erro: String(erro.message || erro), violacoes: [], incompletos_contraste: 0, incompletos_contraste_alvos: [] }));
          const cobertura = await coberturaIncompletos(page, a.incompletos_contraste_alvos || []);
          resultado.axe.casos.push({ pagina: "/", estado: estado.nome, viewport: rotulo, versao: a.versao, violacoes: a.violacoes, erro: a.erro || null,
            incompletos_contraste: a.incompletos_contraste, incompletos_cobertos: cobertura.cobertos, captura: caso.captura });
        }
        resultado.casos.push(caso);
        process.stdout.write(".");
      } finally { await contexto.close(); }
    }
  }
  process.stdout.write("\n");
  return resultado;
}

/** Foco obscurecido pela barra de voz depois de 25 Tabs, em cada estado a 360×740. */
async function grupoFoco(nav, base, estados) {
  const casos = [];
  for (const estado of estados) {
    const { contexto, page } = await abrirEstado(nav, base, estado.nome, [360, 740]);
    try {
      const r = await medidas.medirFocoObscurecido(page, 25);
      casos.push({ estado: estado.nome, tabs: r.tabs, obscurecidos: r.obscurecidos, focos: r.focos.filter((f) => f.sobreposicao_px > 0) });
    } finally { await contexto.close(); }
  }
  return { casos };
}

/** Página /tecnico a 360×740 e 1280×800, com e sem o assistente aberto. */
async function grupoTecnico(nav, base, saida) {
  await configurar(base, "verde");
  const axe = ferramentas.localizarAxe();
  const casos = [];
  for (const vp of [[360, 740], [1280, 800]]) {
    for (const assistente of [false, true]) {
      const contexto = await nav.newContext({ viewport: { width: vp[0], height: vp[1] }, locale: "pt-BR" });
      const page = await contexto.newPage();
      const erros = [];
      page.on("console", (m) => { if (m.type() === "error" && !ERROS_DE_REDE.some((p) => p.test(m.text()))) { erros.push(m.text()); } });
      page.on("pageerror", (e) => erros.push("pageerror: " + e.message));
      try {
        await page.goto(base + "/tecnico");
        await page.waitForFunction(() => { const c = document.getElementById("conteudo"); return c && !c.hidden; }, null, { timeout: 8000 }).catch(() => null);
        await page.waitForTimeout(600);
        if (assistente) {
          for (const seletor of ["#t-seca", "#t-florada", "details:not([open]) > summary"]) {
            const alvos = page.locator(seletor);
            const n = await alvos.count();
            for (let i = 0; i < n; i += 1) { await alvos.nth(i).click({ timeout: 2000 }).catch(() => null); }
          }
          await page.locator("[data-ponto='SECO']").click({ timeout: 2000 }).catch(() => null);
          await page.waitForTimeout(800);
        }
        const rotulo = vp[0] + "x" + vp[1] + (assistente ? "_assistente" : "");
        const captura = path.join(saida, "capturas", "tecnico_" + rotulo + ".png");
        await page.screenshot({ path: captura, fullPage: true });
        const alvos = await medidas.medirAlvos(page, null);
        const contraste = await medidas.medirContraste(page, null);
        const icones = await medidas.medirIcones(page, null);
        const caso = { viewport: vp[0] + "x" + vp[1], assistente, alvos_pequenos: alvos.filter((a) => a.altura < 48), total_alvos: alvos.length,
          contraste_falhas: contraste.filter((t) => t.razao < t.minimo), icones_falhas: icones.filter((i) => i.controle && i.razao < 3),
          erros_console: erros, captura: path.relative(saida, captura) };
        if (axe) {
          const a = await ferramentas.rodarAxe(page, { caminho: axe }).catch((erro) => ({ erro: String(erro.message || erro), violacoes: [], incompletos_contraste: 0, incompletos_contraste_alvos: [] }));
          const cobertura = await coberturaIncompletos(page, a.incompletos_contraste_alvos || []);
          caso.axe = { violacoes: a.violacoes, erro: a.erro || null, incompletos_contraste: a.incompletos_contraste, incompletos_cobertos: cobertura.cobertos };
        }
        casos.push(caso);
      } finally { await contexto.close(); }
    }
  }
  return { casos, axe_disponivel: !!axe };
}

/** Texto ampliado (130 % e 150 %) a 320 e 360 px em / e /tecnico, CSS em px e meta text-scale. */
async function grupoTexto(nav, base) {
  const casos = [];
  const paginas = ESTADOS_TEXTO.map((e) => ({ url: "/", estado: e })).concat([{ url: "/tecnico", estado: "verde" }]);
  for (const alvo of paginas) {
    for (const largura of [320, 360]) {
      for (const fator of [1.3, 1.5]) {
        let aberto;
        if (alvo.url === "/") {
          aberto = await abrirEstado(nav, base, alvo.estado, [largura, 740]);
        } else {
          await configurar(base, alvo.estado);
          const contexto = await nav.newContext({ viewport: { width: largura, height: 740 }, locale: "pt-BR" });
          const page = await contexto.newPage();
          await page.goto(base + alvo.url);
          await page.waitForFunction(() => { const c = document.getElementById("conteudo"); return c && !c.hidden; }, null, { timeout: 8000 }).catch(() => null);
          await page.waitForTimeout(500);
          aberto = { contexto, page };
        }
        try {
          const r = await medidas.medirTextoAmpliado(aberto.page, fator);
          casos.push({ pagina: alvo.url, estado: alvo.estado, largura, fator, scrollWidth: r.scrollWidth, clientWidth: r.clientWidth,
            excesso_px: Math.max(0, r.scrollWidth - r.clientWidth), colisoes_topo: r.colisoes_topo });
        } finally { await aberto.contexto.close(); }
      }
    }
  }
  // CSS das duas páginas: <style> embutido, folhas externas do mesmo host e atributos style.
  await configurar(base, "verde");
  const css = {};
  const metas = {};
  for (const url of ["/", "/tecnico"]) {
    const r = await getBruto(base + url);
    const html = r.corpo;
    let texto = (html.match(/<style[^>]*>[\s\S]*?<\/style>/gi) || []).join("\n") + "\n" + (html.match(/style="[^"]*"/gi) || []).join("\n");
    for (const href of extrairFolhas(html)) {
      const folha = await getBruto(new URL(href, base).toString());
      texto += "\n" + folha.corpo;
    }
    css[url] = medidas.analisarCss(texto);
    metas[url] = { text_scale: /<meta[^>]+name=["']text-scale["']/i.test(html), color_scheme_only_light: /<meta[^>]+name=["']color-scheme["'][^>]+only light/i.test(html) };
  }
  return { casos, css, metas };
}

/** href das folhas de estilo (rel=stylesheet) do mesmo host. */
function extrairFolhas(html) {
  const saida = [];
  const re = /<link\b[^>]*>/gi;
  let m;
  while ((m = re.exec(html))) {
    const tag = m[0];
    if (!/rel=["']?stylesheet/i.test(tag)) { continue; }
    const href = (tag.match(/href=["']([^"']+)["']/i) || [])[1];
    if (href && !/^https?:\/\//i.test(href) && !href.includes("{{")) { saida.push(href); }
  }
  return Array.from(new Set(saida));
}

/** Pixels das lâmpadas: normal, ofuscamento, daltonismo, cores forçadas, escurecimento e vivo × esmaecido. */
async function grupoPixels(nav, base, saida) {
  const casos = [];
  const recortesAcromatopsia = {};
  for (const nome of ESTADOS_PIXELS) {
    const { contexto, page } = await abrirEstado(nav, base, nome, [360, 740], { dpr: 2 });
    try {
      const normal = await pixels.razaoLampadas(page, {});
      const ofuscado = await pixels.razaoLampadas(page, { ofuscamento: 0.1 });
      const filtros = {};
      for (const filtro of FILTROS) { filtros[filtro] = (await pixels.razaoLampadas(page, { filtro })).razao; }
      await pixels.aplicarFiltro(page, "acromatopsia");
      recortesAcromatopsia[nome] = await pixels.recorteSemaforo(page);
      await pixels.aplicarFiltro(page, null);
      const retrato = await medidas.coletarEstado(page);
      casos.push({ estado: nome, data_estado: retrato.estado, confianca: retrato.confianca, cor_acesa: normal.cor_acesa, razao: normal.razao,
        razao_ofuscamento: ofuscado.razao, filtros, lampadas_acesas: normal.lampadas_acesas, luminancias: normal.luminancias,
        borda_semaforo: retrato.borda_semaforo, sombras: retrato.lampadas.map((l) => l.sombra) });
    } finally { await contexto.close(); }
  }
  const diferencas = {};
  for (const [vivo, esmaecido] of [["vermelho", "offline"], ["vermelho", "erro_503"], ["amarelo", "leitura_antiga"]]) {
    if (recortesAcromatopsia[vivo] && recortesAcromatopsia[esmaecido]) {
      const contexto = await nav.newContext();
      const page = await contexto.newPage();
      try { diferencas[vivo + "_x_" + esmaecido] = await pixels.diferencaPixels(page, recortesAcromatopsia[vivo], recortesAcromatopsia[esmaecido]); } finally { await contexto.close(); }
    }
  }
  // Cores forçadas (Windows/Android de alto contraste).
  const forcado = [];
  for (const nome of ["verde", "amarelo", "vermelho"]) {
    const { contexto, page } = await abrirEstado(nav, base, nome, [360, 740], { dpr: 2, contexto: { forcedColors: "active" } });
    try {
      const r = await pixels.razaoLampadas(page, {});
      await page.screenshot({ path: path.join(saida, "capturas", "forcado_" + nome + ".png") });
      // Em cores forçadas a lâmpada acesa pode ser a MAIS ESCURA (CanvasText sobre Canvas): vale a razão simétrica.
      forcado.push({ estado: nome, razao: r.razao_simetrica === undefined ? r.razao : r.razao_simetrica, razao_acesa_sobre_apagada: r.razao, luminancias: r.luminancias });
    } finally { await contexto.close(); }
  }
  // Escurecimento automático do Chrome (WebContentsForceDark), num navegador à parte.
  const escuro = [];
  const navEscuro = await chromium.launch({ args: ["--enable-features=WebContentsForceDark", "--blink-settings=forceDarkModeEnabled=true"] });
  try {
    for (const nome of ["amarelo", "verde", "vermelho"]) {
      const { contexto, page } = await abrirEstado(navEscuro, base, nome, [360, 740], { dpr: 2, contexto: { colorScheme: "dark" } });
      try {
        const r = await pixels.razaoLampadas(page, {});
        await page.screenshot({ path: path.join(saida, "capturas", "escuro_auto_" + nome + ".png") });
        escuro.push({ estado: nome, razao: r.razao, luminancias: r.luminancias });
      } finally { await contexto.close(); }
    }
  } finally { await navEscuro.close(); }
  return { casos, diferencas_acromatopsia: diferencas, forcado, escuro };
}

/** Regiões vivas, alarmes, retorno falado, repetição da voz, "Feito", desfazer e resumo do técnico. */
async function grupoVivas(nav, base) {
  const r = {};
  r.mutacoes_parado = await vivas.medirMutacoesParado(nav, base, { minutos: 10, estado: "verde" });
  r.cartao_30s = await vivas.medirLotes30s(nav, base, { estado: "verde", alvo: ".cartao-estado, #anunciador" });
  r.faixa_demo_30s = await vivas.medirLotes30s(nav, base, { estado: "demonstracao_GEADA", url: "/?banca=1", alvo: ".faixa-demo, #faixa-demo" });
  r.alarmes_simulacao = await vivas.medirAlarmes(nav, base, "simulacao", 30);
  r.alarmes_demonstracao = await vivas.medirAlarmes(nav, base, "demonstracao_GEADA", 30);
  r.retorno_falado = await vivas.medirRetornoFalado(nav, base);
  r.repeticao = await vivas.medirRepeticaoVoz(nav, base);
  r.feito = await vivas.medirFeito(nav, base);
  r.demonstracao_no_resumo = await vivas.medirDemonstracaoNoResumo(base);
  // Desfazer aceito pelo servidor logo depois do toque (a janela de 120 s é conferida por coerencia.py).
  await configurar(base, "vermelho");
  const status = (await vivas.get(base, "/api/status")).dados || {};
  const feito = await vivas.post(base, "/api/acao_feita", { codigo_acao: (status.acoes || ["REGAR_AGORA"])[0], causa: status.causa || "TERRA_SECA", id_leitura: status.id_leitura || null });
  const desfazer = feito.dados && feito.dados.id_acao ? await vivas.post(base, "/api/acao_feita/desfazer", { id_acao: feito.dados.id_acao }) : { status: null };
  r.desfazer_servidor = { feito: feito.status, desfazer: desfazer.status };
  return r;
}

/** Peso e rede: GET / gzip em todos os estados, CSS estático, 2ª visita, Slow 3G + CPU 6× e hosts externos. */
async function grupoBytes(nav, base, estados) {
  const paginas = [];
  for (const estado of estados) {
    await configurar(base, estado.nome);
    const r = await getBruto(base + "/");
    paginas.push({ estado: estado.nome, gzip_bytes: r.gzip_bytes, comprimido_pelo_servidor: r.comprimido_pelo_servidor, html: r.corpo });
  }
  const pior = paginas.reduce((a, p) => (p.gzip_bytes > a.gzip_bytes ? p : a), paginas[0]);
  const folhas = extrairFolhas(pior.html);
  const css = [];
  for (const href of folhas) {
    const r = await getBruto(new URL(href, base).toString());
    css.push({ href, status: r.status, gzip_bytes: r.gzip_bytes, cache_control: r.cabecalhos["cache-control"] || "" });
  }
  // 2ª visita (cache HTTP do navegador) e 1ª lâmpada acesa com Slow 3G + CPU 6×.
  await configurar(base, "verde");
  const contexto = await nav.newContext({ viewport: { width: 360, height: 740 }, locale: "pt-BR" });
  const segunda = { bytes: null, pedidos: [] };
  try {
    const page = await contexto.newPage();
    await page.goto(base + "/");
    await vivas.esperarPronto(page, 8000);
    await page.waitForTimeout(1500);
    const cdp = await contexto.newCDPSession(page);
    await cdp.send("Network.enable");
    const tamanhos = new Map();
    const urls = new Map();
    cdp.on("Network.requestWillBeSent", (e) => urls.set(e.requestId, e.request.url));
    cdp.on("Network.loadingFinished", (e) => tamanhos.set(e.requestId, e.encodedDataLength));
    await page.goto(base + "/");
    await vivas.esperarPronto(page, 8000);
    await page.waitForTimeout(1500);
    segunda.bytes = Array.from(tamanhos.values()).reduce((a, v) => a + v, 0);
    segunda.pedidos = Array.from(tamanhos.entries()).map(([id, b]) => ({ url: (urls.get(id) || "").replace(base, ""), bytes: b }));
  } finally { await contexto.close(); }
  const lenta = [];
  for (const nome of ["verde", "vermelho"]) {
    await configurar(base, nome);
    const ctx = await nav.newContext({ viewport: { width: 360, height: 740 }, locale: "pt-BR" });
    try {
      await ctx.addInitScript(() => {
        function conferir() {
          const b = document.body;
          if (b && /^(verde|amarelo|vermelho)$/.test(b.getAttribute("data-estado") || "") && !b.classList.contains("carregando")) { window.__lampadaEm = performance.now(); return; }
          requestAnimationFrame(conferir);
        }
        requestAnimationFrame(conferir);
      });
      const page = await ctx.newPage();
      const cdp = await ctx.newCDPSession(page);
      await cdp.send("Network.enable");
      await cdp.send("Network.emulateNetworkConditions", SLOW_3G);
      await cdp.send("Emulation.setCPUThrottlingRate", { rate: 6 });
      await page.goto(base + "/", { timeout: 60000 });
      await page.waitForFunction(() => typeof window.__lampadaEm === "number", null, { timeout: 30000 }).catch(() => null);
      lenta.push({ estado: nome, ms: await page.evaluate(() => (typeof window.__lampadaEm === "number" ? Math.round(window.__lampadaEm) : null)) });
    } finally { await ctx.close(); }
  }
  return { paginas: paginas.map((p) => ({ estado: p.estado, gzip_bytes: p.gzip_bytes, comprimido_pelo_servidor: p.comprimido_pelo_servidor })),
    pior: { estado: pior.estado, gzip_bytes: pior.gzip_bytes }, css, segunda_visita: segunda, lenta };
}

/** Mapas de ícones declarados no script da página: `var NOME = {...};`. */
function extrairMapas(html) {
  const mapas = {};
  const re = /var\s+(ACOES|CAUSAS|ICONES_CULTURA|ICONES_ACAO|ICONES_CAUSA)\s*=\s*(\{[\s\S]*?\});/g;
  let m;
  while ((m = re.exec(html))) {
    try { mapas[m[1]] = Function('"use strict"; return (' + m[2] + ");")(); } catch (erro) { mapas[m[1]] = null; }
  }
  return mapas;
}

/** Normaliza um nome de pictograma para o id do <symbol> ("regador" → "i-regador"). */
function idSimbolo(valor) {
  const v = String(valor).replace(/^#/, "");
  return v.startsWith("i-") ? v : "i-" + v;
}

/** Valores de um mapa como lista de símbolos (string ou lista de strings). */
function simbolosDoValor(valor) { return (Array.isArray(valor) ? valor : [valor]).map(idSimbolo); }

/** Pictogramas: diferença de pixels com tinta a 24 px em todos os pares, mapas injetivos e gabarito ISO 9186-1. */
async function grupoPictogramas(nav, base) {
  await configurar(base, "verde");
  const html = (await getBruto(base + "/")).corpo;
  // Os mapas vêm do template (a página servida tem o script minificado, com outros nomes).
  const mapas = extrairMapas(fs.readFileSync(path.join(RAIZ, "src", "templates", "index.html"), "utf8"));
  const culturas = ((await vivas.get(base, "/api/culturas")).dados || {}).culturas || [];
  const nao_injetivos = [];
  const verificarInjetivo = (nome, pares) => {
    const vistos = new Map();
    for (const [chave, valor] of pares) {
      const k = JSON.stringify(valor);
      if (vistos.has(k)) { nao_injetivos.push({ mapa: nome, chaves: [vistos.get(k), chave], valor }); } else { vistos.set(k, chave); }
    }
  };
  if (mapas.ACOES) { verificarInjetivo("ACOES", Object.entries(mapas.ACOES)); }
  if (mapas.CAUSAS) { verificarInjetivo("CAUSAS", Object.entries(mapas.CAUSAS)); }
  if (mapas.ICONES_CULTURA) {
    verificarInjetivo("ICONES_CULTURA", culturas.map((c) => [c.codigo, mapas.ICONES_CULTURA[c.codigo] || mapas.ICONES_CULTURA[c.categoria] || "padrão"]));
  }
  const simbolos = new Set();
  for (const nome of ["ACOES", "CAUSAS"]) { for (const v of Object.values(mapas[nome] || {})) { simbolosDoValor(v).forEach((s) => simbolos.add(s)); } }
  const contexto = await nav.newContext({ viewport: { width: 400, height: 300 } });
  let pares = [];
  try {
    const page = await contexto.newPage();
    await page.setContent(html.replace(/<script[\s\S]*?<\/script>/gi, ""), { waitUntil: "domcontentloaded" });
    pares = await page.evaluate(async (ids) => {
      async function tinta(id) {
        const s = document.getElementById(id);
        if (!s) { return null; }
        const svg = s.outerHTML.replace(/^<symbol/, '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" fill="none" stroke="black" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"').replace(/<\/symbol>$/, "</svg>");
        const imagem = new Image();
        imagem.src = "data:image/svg+xml," + encodeURIComponent(svg);
        await imagem.decode();
        const c = document.createElement("canvas"); c.width = 24; c.height = 24;
        const x = c.getContext("2d"); x.drawImage(imagem, 0, 0, 24, 24);
        const d = x.getImageData(0, 0, 24, 24).data;
        const saida = new Uint8Array(576);
        for (let i = 0; i < 576; i += 1) { saida[i] = d[i * 4 + 3] > 64 ? 1 : 0; }
        return saida;
      }
      const mapa = {};
      for (const id of ids) { mapa[id] = await tinta(id); }
      const lista = ids.filter((id) => mapa[id]);
      const resultado = [];
      for (let i = 0; i < lista.length; i += 1) {
        for (let j = i + 1; j < lista.length; j += 1) {
          const a = mapa[lista[i]], b = mapa[lista[j]];
          let dif = 0, uniao = 0;
          for (let k = 0; k < 576; k += 1) { if (a[k] || b[k]) { uniao += 1; } if (a[k] !== b[k]) { dif += 1; } }
          resultado.push({ par: lista[i] + " x " + lista[j], pct: uniao ? Math.round(dif / uniao * 1000) / 10 : 100 });
        }
      }
      return { resultado, ausentes: ids.filter((id) => !mapa[id]) };
    }, Array.from(simbolos).sort());
  } finally { await contexto.close(); }
  // Gabarito ISO 9186-1 (frente A): 1 linha por código de ação e de causa.
  const arquivo = path.join(RAIZ, "tests", "dados", "pictogramas_iso9186.csv");
  const iso = { existe: fs.existsSync(arquivo), faltando: [], repetidos: [] };
  if (iso.existe) {
    const linhas = fs.readFileSync(arquivo, "utf8").split(/\r?\n/).filter((l) => l.trim()).slice(1);
    const contagem = {};
    linhas.forEach((l) => { const c = l.split(",")[0].replace(/"/g, "").trim(); contagem[c] = (contagem[c] || 0) + 1; });
    const codigos = Object.keys(mapas.ACOES || {}).concat(Object.keys(mapas.CAUSAS || {}));
    iso.faltando = codigos.filter((c) => !contagem[c]);
    iso.repetidos = Object.keys(contagem).filter((c) => contagem[c] > 1);
    iso.linhas = linhas.length;
  }
  const ordenados = (pares.resultado || []).slice().sort((a, b) => a.pct - b.pct);
  return { mapas_encontrados: Object.keys(mapas), simbolos: Array.from(simbolos).sort(), ausentes: pares.ausentes || [], piores_pares: ordenados.slice(0, 8),
    diferenca_min_pct: ordenados.length ? ordenados[0].pct : null, nao_injetivos, iso };
}

/** Lighthouse em / (estado verde) e /tecnico. */
async function grupoLighthouse(base) {
  const binario = ferramentas.localizarLighthouse();
  if (!binario) { return { disponivel: false }; }
  const antes = () => configurar(base, "verde");
  const raiz = await ferramentas.rodarLighthouse(base + "/", { rodadas: 3, binario, antes });
  const tecnico = await ferramentas.rodarLighthouse(base + "/tecnico", { rodadas: 3, binario, antes });
  return { disponivel: true, binario, raiz, tecnico };
}

const GRUPOS = ["grade", "foco", "tecnico", "texto", "pixels", "vivas", "bytes", "picto", "lighthouse"];

/** Roda os grupos pedidos e grava <saida>/medidas_node.json. */
async function medir(base, saida, grupos) {
  fs.mkdirSync(path.join(saida, "capturas"), { recursive: true });
  const estados = await listarEstados(base);
  const avisos = [];
  const axe = ferramentas.localizarAxe();
  const lighthouse = ferramentas.localizarLighthouse();
  if (!axe) { avisos.push("PULADO: axe-core ausente"); }
  if (!lighthouse && grupos.includes("lighthouse")) { avisos.push("PULADO: lighthouse ausente"); }
  avisos.forEach((a) => console.log(a));
  const nav = await chromium.launch();
  const resultado = { base, estados: estados.map((e) => e.nome), ferramentas: { axe, lighthouse, playwright: require("playwright/package.json").version }, avisos, grupos: {} };
  try {
    for (const grupo of grupos) {
      const inicio = Date.now();
      console.log("== " + grupo);
      if (grupo === "grade") { resultado.grupos.grade = await grupoGrade(nav, base, estados, saida); }
      if (grupo === "foco") { resultado.grupos.foco = await grupoFoco(nav, base, estados); }
      if (grupo === "tecnico") { resultado.grupos.tecnico = await grupoTecnico(nav, base, saida); }
      if (grupo === "texto") { resultado.grupos.texto = await grupoTexto(nav, base); }
      if (grupo === "pixels") { resultado.grupos.pixels = await grupoPixels(nav, base, saida); }
      if (grupo === "vivas") { resultado.grupos.vivas = await grupoVivas(nav, base); }
      if (grupo === "bytes") { resultado.grupos.bytes = await grupoBytes(nav, base, estados); }
      if (grupo === "picto") { resultado.grupos.picto = await grupoPictogramas(nav, base); }
      if (grupo === "lighthouse") { resultado.grupos.lighthouse = await grupoLighthouse(base); }
      console.log("   " + grupo + " em " + Math.round((Date.now() - inicio) / 1000) + " s");
      fs.writeFileSync(path.join(saida, "medidas_node.json"), JSON.stringify(resultado, null, 1));
    }
  } finally {
    await nav.close();
    await configurar(base, "verde").catch(() => null);
  }
  return resultado;
}

// ---------------------------------------------------------------------------
// Indicadores e comparação com as metas
// ---------------------------------------------------------------------------

/** Concordância e marca do aria-label do semáforo para um estado (lista de problemas). */
function problemasAria(nome, retrato) {
  const aria = String(retrato.aria_semaforo || "");
  const p = [];
  if (!aria) { return ["sem aria-label"]; }
  if (/luz (vermelho|amarelo) acesa|luzes? \w+ acesos/i.test(aria)) { p.push("concordância: " + aria); }
  if (CORES.includes(retrato.estado) && !/luz (vermelha|amarela|verde) acesa/i.test(aria) && !/(vermelha|amarela|verde)/i.test(aria)) { p.push("cor não dita: " + aria); }
  if (ESMAECIDOS.includes(nome) && !/antig|desatualiz|sem liga|velh|pode não valer|não vale|problema|erro/i.test(aria)) { p.push("sem marca de antiga: " + aria); }
  if (["simulacao", "simulacao_verde"].includes(nome) && !/teste|simula/i.test(aria)) { p.push("sem marca de teste: " + aria); }
  if (nome === "demonstracao_GEADA" && !/demonstra|teste/i.test(aria)) { p.push("sem marca de demonstração: " + aria); }
  return p;
}

/** Lê um JSON se existir. */
function lerJson(arquivo) { return fs.existsSync(arquivo) ? JSON.parse(fs.readFileSync(arquivo, "utf8")) : null; }

/** Indicadores numéricos (os que as metas comparam) a partir das medidas. */
function calcularIndicadores(medidasNode, coerencia, legibilidade) {
  const g = (medidasNode && medidasNode.grupos) || {};
  const ind = {};
  const det = {};
  if (g.grade) {
    const casos = g.grade.casos;
    const celulares = casos.filter((c) => CELULARES.some(([w, h]) => c.viewport === w + "x" + h));
    // Contraste.
    const falhasTexto = new Map();
    casos.forEach((c) => c.contraste_falhas.forEach((t) => falhasTexto.set(c.estado + " " + t.seletor, Object.assign({ estado: c.estado, viewport: c.viewport }, t))));
    ind["contraste.textos_abaixo"] = falhasTexto.size;
    det["contraste.textos_abaixo"] = Array.from(falhasTexto.values()).slice(0, 25);
    const falhasIcone = new Map();
    casos.forEach((c) => c.icones_falhas.forEach((t) => falhasIcone.set(c.estado + " " + t.seletor, Object.assign({ estado: c.estado, viewport: c.viewport }, t))));
    ind["contraste.icones_controle_abaixo"] = falhasIcone.size;
    det["contraste.icones_controle_abaixo"] = Array.from(falhasIcone.values()).slice(0, 25);
    const primeira = casos.filter((c) => c.viewport === "360x740" && VIVOS.includes(c.estado)).flatMap((c) => c.contraste_primeira_tela.map((t) => Object.assign({ estado: c.estado }, t)));
    ind["contraste.primeira_tela_min_razao"] = primeira.length ? Math.min(...primeira.map((t) => t.razao)) : null;
    ind["contraste.primeira_tela_min_fonte_px"] = primeira.length ? Math.min(...primeira.map((t) => t.tamanho_px)) : null;
    det["contraste.primeira_tela_min_razao"] = primeira.slice().sort((a, b) => a.razao - b.razao).slice(0, 8);
    det["contraste.primeira_tela_min_fonte_px"] = primeira.slice().sort((a, b) => a.tamanho_px - b.tamanho_px).slice(0, 8);
    // Oclusão.
    const ocl = celulares.flatMap((c) => c.oclusao.filter((o) => o.sobreposicao_px > 1).map((o) => ({ estado: c.estado, viewport: c.viewport, seletor: o.seletor, px: o.sobreposicao_px, captura: c.captura })));
    ind["oclusao.casos_com_sobreposicao"] = ocl.length;
    ind["oclusao.max_px"] = ocl.length ? Math.max(...ocl.map((o) => o.px)) : 0;
    det["oclusao.casos_com_sobreposicao"] = ocl.slice(0, 30);
    // Semáforo.
    const comLampada = celulares.filter((c) => c.retrato.lampadas.length);
    ind["semaforo.lampada_min_px"] = comLampada.length ? Math.min(...comLampada.map((c) => Math.min(...c.retrato.lampadas.map((l) => l.diametro)))) : null;
    det["semaforo.lampada_min_px"] = comLampada.map((c) => ({ estado: c.estado, viewport: c.viewport, px: Math.min(...c.retrato.lampadas.map((l) => l.diametro)) })).sort((a, b) => a.px - b.px).slice(0, 8);
    const difs = CELULARES.map(([w, h]) => {
      const vp = w + "x" + h;
      const tam = (nome) => { const c = casos.find((x) => x.estado === nome && x.viewport === vp); return c && c.retrato.lampadas.length ? Math.min(...c.retrato.lampadas.map((l) => l.diametro)) : null; };
      return { viewport: vp, vermelho: tam("vermelho"), verde: tam("verde") };
    }).filter((d) => d.vermelho !== null && d.verde !== null);
    ind["semaforo.vermelho_menos_verde_px"] = difs.length ? Math.min(...difs.map((d) => arred(d.vermelho - d.verde, 1))) : null;
    det["semaforo.vermelho_menos_verde_px"] = difs;
    const orientacoes = new Set(comLampada.map((c) => c.retrato.orientacao).filter(Boolean));
    ind["semaforo.orientacoes_celular"] = orientacoes.size;
    det["semaforo.orientacoes_celular"] = Array.from(orientacoes);
    // Estados sem cor: aria-label, bordas e 503.
    const ariaProblemas = casos.filter((c) => c.viewport === "360x740").map((c) => ({ estado: c.estado, problemas: problemasAria(c.estado, c.retrato), aria: c.retrato.aria_semaforo })).filter((x) => x.problemas.length);
    ind["estados.aria_label_incorretos"] = ariaProblemas.length;
    det["estados.aria_label_incorretos"] = ariaProblemas;
    const erro503 = casos.filter((c) => ["erro_503", "primeira_503"].includes(c.estado)).filter((c) => /sem liga[çc][ãa]o/i.test(c.retrato.texto_cartao || "") || /i-check$/.test(c.retrato.chip_icone || "") || (c.retrato.feito_visivel && /feito/i.test(c.retrato.texto_cartao || "") && c.retrato.feito_estado === "feito"));
    ind["estados.erro_503_sem_ligacao_ou_check"] = erro503.length;
    det["estados.erro_503_sem_ligacao_ou_check"] = erro503.map((c) => ({ estado: c.estado, viewport: c.viewport, chip: c.retrato.chip, chip_icone: c.retrato.chip_icone, texto: (c.retrato.texto_cartao || "").slice(0, 120) }));
    // Alvos, console e hosts.
    const pequenos = new Map();
    casos.forEach((c) => c.alvos_pequenos.forEach((a) => pequenos.set(a.seletor, Object.assign({ estado: c.estado, viewport: c.viewport }, a))));
    ind["alvos.raiz_abaixo_48"] = pequenos.size;
    det["alvos.raiz_abaixo_48"] = Array.from(pequenos.values()).slice(0, 25);
    const erros = casos.filter((c) => c.erros_console.length).map((c) => ({ estado: c.estado, viewport: c.viewport, erros: c.erros_console }));
    ind["console.casos_com_erro"] = erros.length;
    det["console.casos_com_erro"] = erros.slice(0, 10);
    const hosts = Array.from(new Set(casos.flatMap((c) => c.hosts_externos)));
    ind["rede.recursos_outro_host"] = hosts.length;
    det["rede.recursos_outro_host"] = hosts;
    // axe em /.
    if (g.grade.axe && g.grade.axe.disponivel) {
      const ax = g.grade.axe.casos;
      ind["axe.raiz_casos"] = ax.length;
      ind["axe.raiz_estados"] = new Set(ax.map((a) => a.estado)).size;
      ind["axe.raiz_violacoes"] = ax.reduce((s, a) => s + a.violacoes.reduce((t, v) => t + v.n, 0) + (a.erro ? 1 : 0), 0);
      det["axe.raiz_violacoes"] = ax.filter((a) => a.violacoes.length || a.erro).map((a) => ({ estado: a.estado, viewport: a.viewport, violacoes: a.violacoes, erro: a.erro, captura: a.captura })).slice(0, 20);
      const incompletos = ax.reduce((s, a) => s + a.incompletos_contraste, 0);
      const cobertos = ax.reduce((s, a) => s + a.incompletos_cobertos, 0);
      ind["axe.incompletos_contraste_cobertos_pct"] = incompletos ? arred(cobertos / incompletos * 100, 1) : 100;
      det["axe.incompletos_contraste_cobertos_pct"] = { incompletos, cobertos };
    }
  }
  if (g.foco) {
    const obs = g.foco.casos.filter((c) => c.obscurecidos > 0);
    ind["foco.obscurecidos_max"] = g.foco.casos.length ? Math.max(...g.foco.casos.map((c) => c.obscurecidos)) : null;
    det["foco.obscurecidos_max"] = obs.map((c) => ({ estado: c.estado, obscurecidos: c.obscurecidos, focos: c.focos.slice(0, 5) }));
  }
  if (g.tecnico) {
    const t = g.tecnico.casos;
    const pequenos = new Map();
    t.forEach((c) => c.alvos_pequenos.forEach((a) => pequenos.set(a.seletor + (c.assistente ? " (assistente)" : ""), Object.assign({ viewport: c.viewport, assistente: c.assistente }, a))));
    ind["alvos.tecnico_abaixo_48"] = pequenos.size;
    det["alvos.tecnico_abaixo_48"] = Array.from(pequenos.values());
    ind["contraste.tecnico_abaixo"] = t.reduce((s, c) => s + c.contraste_falhas.length + c.icones_falhas.length, 0);
    det["contraste.tecnico_abaixo"] = t.flatMap((c) => c.contraste_falhas.concat(c.icones_falhas)).slice(0, 20);
    ind["console.tecnico_erros"] = t.reduce((s, c) => s + c.erros_console.length, 0);
    det["console.tecnico_erros"] = t.flatMap((c) => c.erros_console).slice(0, 10);
    if (g.tecnico.axe_disponivel) {
      ind["axe.tecnico_violacoes"] = t.reduce((s, c) => s + (c.axe ? c.axe.violacoes.reduce((x, v) => x + v.n, 0) + (c.axe.erro ? 1 : 0) : 0), 0);
      det["axe.tecnico_violacoes"] = t.filter((c) => c.axe && c.axe.violacoes.length).map((c) => ({ viewport: c.viewport, assistente: c.assistente, violacoes: c.axe.violacoes }));
    }
  }
  if (g.texto) {
    const casos = g.texto.casos;
    const rolagem = casos.filter((c) => c.excesso_px > 0);
    ind["texto.casos_com_rolagem_lateral"] = rolagem.length;
    ind["texto.rolagem_lateral_max_px"] = casos.length ? Math.max(...casos.map((c) => c.excesso_px)) : null;
    det["texto.casos_com_rolagem_lateral"] = rolagem.map((c) => ({ pagina: c.pagina, estado: c.estado, largura: c.largura, fator: c.fator, scrollWidth: c.scrollWidth, clientWidth: c.clientWidth }));
    const colisoes = casos.filter((c) => c.colisoes_topo.length);
    ind["texto.colisoes_topo"] = colisoes.reduce((s, c) => s + c.colisoes_topo.length, 0);
    det["texto.colisoes_topo"] = colisoes.map((c) => ({ pagina: c.pagina, estado: c.estado, largura: c.largura, fator: c.fator, colisoes: c.colisoes_topo.slice(0, 5) }));
    ind["texto.font_size_px_css"] = Object.values(g.texto.css).reduce((s, c) => s + c.font_size_px, 0);
    det["texto.font_size_px_css"] = g.texto.css;
    ind["texto.paginas_sem_text_scale"] = Object.values(g.texto.metas).filter((m) => !m.text_scale).length;
    det["texto.paginas_sem_text_scale"] = g.texto.metas;
  }
  if (g.pixels) {
    const p = g.pixels;
    const vivosPx = p.casos.filter((c) => VIVOS.includes(c.estado) && c.razao !== null);
    const esmaecidosPx = p.casos.filter((c) => ESMAECIDOS.includes(c.estado) && c.razao !== null);
    const minimo = (lista, chave) => (lista.length ? Math.min(...lista.map((c) => c[chave])) : null);
    ind["semaforo.razao_min_vivos"] = minimo(vivosPx, "razao");
    ind["semaforo.razao_min_esmaecidos"] = minimo(esmaecidosPx, "razao");
    ind["semaforo.razao_min_ofuscamento"] = minimo(vivosPx.concat(esmaecidosPx), "razao_ofuscamento");
    ind["semaforo.razao_vermelho_vivo"] = (p.casos.find((c) => c.estado === "vermelho") || {}).razao || null;
    det["semaforo.razao_min_vivos"] = vivosPx.map((c) => ({ estado: c.estado, cor: c.cor_acesa, razao: c.razao, ofuscamento: c.razao_ofuscamento, filtros: c.filtros }));
    det["semaforo.razao_min_esmaecidos"] = esmaecidosPx.map((c) => ({ estado: c.estado, cor: c.cor_acesa, razao: c.razao, ofuscamento: c.razao_ofuscamento }));
    const filtrados = vivosPx.concat(esmaecidosPx).flatMap((c) => Object.values(c.filtros).filter((v) => typeof v === "number"));
    ind["semaforo.razao_min_daltonismo"] = filtrados.length ? Math.min(...filtrados) : null;
    ind["semaforo.razao_min_forcado"] = p.forcado.length && p.forcado.every((f) => f.razao !== null) ? Math.min(...p.forcado.map((f) => f.razao)) : (p.forcado.length ? 0 : null);
    det["semaforo.razao_min_forcado"] = p.forcado;
    const amareloEscuro = p.escuro.find((e) => e.estado === "amarelo");
    ind["semaforo.razao_amarelo_escuro_auto"] = amareloEscuro ? amareloEscuro.razao : null;
    det["semaforo.razao_amarelo_escuro_auto"] = p.escuro;
    const neutros = p.casos.filter((c) => NEUTROS.includes(c.estado));
    const neutrosErrados = neutros.filter((c) => c.lampadas_acesas > 0 || c.data_estado !== "neutro");
    ind["estados.neutros_com_lampada_acesa"] = neutrosErrados.length;
    det["estados.neutros_com_lampada_acesa"] = neutros.map((c) => ({ estado: c.estado, data_estado: c.data_estado, lampadas_acesas: c.lampadas_acesas, sombras: c.sombras }));
    const semTracejado = esmaecidosPx.concat(p.casos.filter((c) => ESMAECIDOS.includes(c.estado) && c.razao === null)).filter((c, i, todos) => todos.indexOf(c) === i && !/dashed/.test(c.borda_semaforo || ""));
    ind["estados.esmaecidos_sem_tracejado"] = semTracejado.length;
    det["estados.esmaecidos_sem_tracejado"] = semTracejado.map((c) => ({ estado: c.estado, borda: c.borda_semaforo }));
    ind["estados.diferenca_acromatopsia"] = typeof p.diferencas_acromatopsia.vermelho_x_offline === "number" ? p.diferencas_acromatopsia.vermelho_x_offline : null;
    det["estados.diferenca_acromatopsia"] = p.diferencas_acromatopsia;
  }
  if (g.vivas) {
    const v = g.vivas;
    ind["vivas.mutacoes_10min_parado"] = v.mutacoes_parado.registros;
    det["vivas.mutacoes_10min_parado"] = v.mutacoes_parado;
    ind["vivas.lotes_30s_faixa_demo"] = v.faixa_demo_30s.lotes;
    ind["vivas.lotes_30s_cartao"] = v.cartao_30s.lotes;
    det["vivas.lotes_30s_cartao"] = v.cartao_30s;
    ind["retorno.avisos_falados_pct"] = v.retorno_falado.avisos.pct;
    ind["retorno.dialogos_falados_pct"] = v.retorno_falado.dialogos.total ? v.retorno_falado.dialogos.pct : 0;
    det["retorno.avisos_falados_pct"] = v.retorno_falado;
    ind["retorno.rate_repeticao"] = v.repeticao.rate_repeticao;
    ind["retorno.rate_preferencia_lembrada"] = v.repeticao.rate_preferencia_lembrada;
    det["retorno.rate_repeticao"] = v.repeticao;
    ind["feito.registros_3_toques"] = v.feito.registros_3_toques;
    ind["feito.desfazer_15s_na_tela"] = v.feito.desfazer_15s ? 1 : 0;
    det["feito.desfazer_15s_na_tela"] = v.feito;
    ind["feito.desfazer_aceito_servidor"] = v.desfazer_servidor.desfazer === 200 ? 1 : 0;
    det["feito.desfazer_aceito_servidor"] = v.desfazer_servidor;
    ind["alarmes.simulacao_e_demonstracao"] = [v.alarmes_simulacao, v.alarmes_demonstracao].reduce((s, a) => s + a.falas_automaticas + a.vibracoes + a.bipes, 0);
    det["alarmes.simulacao_e_demonstracao"] = [v.alarmes_simulacao, v.alarmes_demonstracao];
    ind["resumo.eventos_da_demonstracao"] = v.demonstracao_no_resumo.delta;
    det["resumo.eventos_da_demonstracao"] = v.demonstracao_no_resumo;
  }
  if (g.bytes) {
    const b = g.bytes;
    ind["bytes.html_gzip_max"] = b.pior.gzip_bytes;
    det["bytes.html_gzip_max"] = b.paginas.slice().sort((x, y) => y.gzip_bytes - x.gzip_bytes).slice(0, 5);
    const cssTotal = b.css.reduce((s, c) => s + c.gzip_bytes, 0);
    ind["bytes.css_gzip"] = b.css.length ? cssTotal : null;
    ind["bytes.css_cache_imutavel"] = b.css.length && b.css.every((c) => /immutable/.test(c.cache_control) && /max-age=31536000\b/.test(c.cache_control)) ? 1 : 0;
    det["bytes.css_gzip"] = b.css;
    ind["bytes.html_mais_css"] = b.pior.gzip_bytes + cssTotal;
    ind["bytes.segunda_visita"] = b.segunda_visita.bytes;
    det["bytes.segunda_visita"] = b.segunda_visita;
    const lentas = b.lenta.map((l) => l.ms).filter((x) => typeof x === "number");
    ind["rede.primeira_lampada_ms"] = lentas.length === b.lenta.length && lentas.length ? Math.max(...lentas) : null;
    det["rede.primeira_lampada_ms"] = b.lenta;
  }
  if (g.picto) {
    ind["picto.diferenca_min_pct"] = g.picto.diferenca_min_pct;
    det["picto.diferenca_min_pct"] = g.picto.piores_pares;
    ind["picto.mapas_nao_injetivos"] = g.picto.nao_injetivos.length;
    det["picto.mapas_nao_injetivos"] = g.picto.nao_injetivos;
    ind["picto.gabarito_iso_ok"] = g.picto.iso.existe && !g.picto.iso.faltando.length && !g.picto.iso.repetidos.length ? 1 : 0;
    det["picto.gabarito_iso_ok"] = g.picto.iso;
  }
  if (g.lighthouse && g.lighthouse.disponivel) {
    const r = g.lighthouse.raiz.mediana, t = g.lighthouse.tecnico.mediana;
    ind["lighthouse.raiz_acessibilidade"] = r.acessibilidade;
    ind["lighthouse.raiz_boas_praticas"] = r.boas_praticas;
    ind["lighthouse.raiz_desempenho"] = r.desempenho;
    ind["lighthouse.raiz_cls"] = arred(r.cls, 3);
    ind["lighthouse.raiz_lcp_ms"] = r.lcp_ms === null ? null : Math.round(r.lcp_ms);
    ind["lighthouse.tecnico_acessibilidade"] = t.acessibilidade;
    ind["lighthouse.tecnico_desempenho"] = t.desempenho;
    det["lighthouse.raiz_desempenho"] = g.lighthouse.raiz.rodadas;
    det["lighthouse.tecnico_desempenho"] = g.lighthouse.tecnico.rodadas;
  }
  if (coerencia) {
    Object.assign(ind, coerencia.indicadores || {});
    Object.assign(det, coerencia.detalhes || {});
  }
  if (legibilidade) {
    Object.assign(ind, legibilidade.indicadores || {});
    Object.assign(det, legibilidade.detalhes || {});
  }
  return { indicadores: ind, detalhes: det };
}

/** Compara um valor com a meta: "ok", "falhou" ou "pulado" (sem valor). */
function avaliarMeta(meta, valor) {
  if (valor === null || valor === undefined || Number.isNaN(valor)) { return "pulado"; }
  const l = meta.limiar;
  switch (meta.operador) {
    case "<=": return valor <= l ? "ok" : "falhou";
    case ">=": return valor >= l ? "ok" : "falhou";
    case "==": return valor === l ? "ok" : "falhou";
    case "<": return valor < l ? "ok" : "falhou";
    case ">": return valor > l ? "ok" : "falhou";
    default: throw new Error("Operador desconhecido na meta " + meta.id + ": " + meta.operador);
  }
}

/**
 * Compara os indicadores com as metas.
 * @returns {{metas: Array, falhas: string[], pulados: string[]}}
 */
function compararMetas(indicadores, metas, opcoes) {
  const o = opcoes || {};
  const linhas = [];
  for (const meta of metas.metas) {
    let valor = indicadores[meta.indicador];
    if (meta.relativo_a_v3 && typeof valor === "number" && typeof meta.baseline_v3 === "number" && meta.baseline_v3 > 0) { valor = arred(valor / meta.baseline_v3, 2); }
    let status = avaliarMeta(meta, valor);
    const ferramenta = meta.ferramenta || null;
    const ausente = ferramenta && (o.ausentes || []).includes(ferramenta);
    if (status === "pulado" && !ausente && !o.gruposParciais) { status = "falhou"; }
    if (status === "pulado" && o.estrito) { status = "falhou"; }
    linhas.push({ id: meta.id, item: meta.item, descricao: meta.descricao, indicador: meta.indicador, valor, operador: meta.operador, limiar: meta.limiar,
      baseline_v3: meta.baseline_v3 === undefined ? null : meta.baseline_v3, status, motivo_pulo: status === "pulado" ? (ausente ? ferramenta + " ausente" : "grupo não medido") : null });
  }
  return { metas: linhas, falhas: linhas.filter((l) => l.status === "falhou").map((l) => l.id), pulados: linhas.filter((l) => l.status === "pulado").map((l) => l.id) };
}

/** Capturas citadas pelas metas que falharam (o resto é apagado da pasta de capturas). */
function capturasDasFalhas(detalhes, falhas, metas) {
  const usadas = new Set();
  const visitar = (valor) => {
    if (!valor) { return; }
    if (Array.isArray(valor)) { valor.forEach(visitar); return; }
    if (typeof valor === "object") { Object.entries(valor).forEach(([k, v]) => { if (k === "captura" && typeof v === "string") { usadas.add(v); } else { visitar(v); } }); }
  };
  for (const id of falhas) {
    const meta = metas.metas.find((m) => m.id === id);
    if (meta) { visitar(detalhes[meta.indicador]); }
  }
  return usadas;
}

/** Junta tudo, compara, grava resultado.json e devolve o código de saída. */
function comparar(saida, opcoes) {
  const o = opcoes || {};
  const medidasNode = lerJson(path.join(saida, "medidas_node.json"));
  const coerencia = lerJson(path.join(saida, "coerencia.json"));
  const legibilidade = lerJson(path.join(saida, "legibilidade.json"));
  const metas = JSON.parse(fs.readFileSync(o.metas || path.join(__dirname, "metas_v4.json"), "utf8"));
  const { indicadores, detalhes } = calcularIndicadores(medidasNode, coerencia, legibilidade);
  const ausentes = [];
  if (!medidasNode || !medidasNode.ferramentas || !medidasNode.ferramentas.axe) { ausentes.push("axe-core"); }
  if (!medidasNode || !medidasNode.ferramentas || !medidasNode.ferramentas.lighthouse) { ausentes.push("lighthouse"); }
  const estrito = process.env.AGRO_UX_ESTRITO === "1";
  const gruposMedidos = medidasNode ? Object.keys(medidasNode.grupos) : [];
  const gruposParciais = GRUPOS.some((gr) => !gruposMedidos.includes(gr)) || !coerencia || !legibilidade;
  const comparacao = compararMetas(indicadores, metas, { ausentes, estrito, gruposParciais });
  const avisos = ((medidasNode && medidasNode.avisos) || []).concat(gruposParciais ? ["Medição parcial: grupos medidos = " + gruposMedidos.join(", ")] : []);
  const capturas = capturasDasFalhas(detalhes, comparacao.falhas, metas);
  const pastaCapturas = path.join(saida, "capturas");
  if (fs.existsSync(pastaCapturas) && !o.manterCapturas) {
    for (const arquivo of fs.readdirSync(pastaCapturas)) {
      const relativo = path.join("capturas", arquivo);
      const manter = capturas.has(relativo) || /^(verde|vermelho|amarelo|leitura_antiga|simulacao)_(360x740|1280x800)\.png$|^tecnico_/.test(arquivo);
      if (!manter) { fs.unlinkSync(path.join(pastaCapturas, arquivo)); }
    }
  }
  let commit = null;
  try { commit = require("child_process").execFileSync("git", ["-C", RAIZ, "rev-parse", "--short", "HEAD"], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim(); } catch (erro) { commit = null; }
  const resultado = { suite: "AgroAnálise validação UX v4", data: new Date().toISOString(), commit, modo: o.linhaDeBase ? "linha_de_base" : "metas", estrito,
    ferramentas: medidasNode ? medidasNode.ferramentas : null, avisos, total_metas: comparacao.metas.length,
    atendidas: comparacao.metas.filter((m) => m.status === "ok").length, falhas: comparacao.falhas, pulados: comparacao.pulados,
    metas: comparacao.metas, indicadores, detalhes, capturas_das_falhas: Array.from(capturas) };
  fs.writeFileSync(path.join(saida, "resultado.json"), JSON.stringify(resultado, null, 1));
  for (const m of comparacao.metas) {
    const marca = m.status === "ok" ? "ok    " : m.status === "pulado" ? "PULADO" : "FALHA ";
    console.log(`${marca} ${m.id.padEnd(46)} ${String(m.valor).padStart(10)} ${m.operador} ${m.limiar}  (v3: ${m.baseline_v3 === null ? "—" : m.baseline_v3})`);
  }
  avisos.forEach((a) => console.log(a));
  console.log(`\n${resultado.atendidas}/${resultado.total_metas} metas atendidas; ${comparacao.falhas.length} falhas; ${comparacao.pulados.length} puladas.`);
  console.log("Relatório: " + path.join(saida, "resultado.json"));
  if (o.linhaDeBase) { return 0; }
  return comparacao.falhas.length ? 1 : 0;
}

async function principal() {
  const args = lerArgumentos(process.argv.slice(2));
  const comando = args._[0];
  const saida = path.resolve(args.saida || path.join(RAIZ, "reports", "ux4", new Date().toISOString().slice(0, 10)));
  fs.mkdirSync(saida, { recursive: true });
  if (comando === "medir") {
    if (!args.base) { throw new Error("Informe --base http://127.0.0.1:<porta> (nunca a porta 5000)."); }
    if (/:5000\b/.test(args.base)) { throw new Error("A porta 5000 é a do sítio: use outra."); }
    const grupos = args.grupos ? String(args.grupos).split(",").filter((g) => GRUPOS.includes(g)) : GRUPOS;
    await medir(args.base.replace(/\/$/, ""), saida, grupos);
    return 0;
  }
  if (comando === "comparar") {
    return comparar(saida, { linhaDeBase: !!args["linha-de-base"], metas: args.metas, manterCapturas: !!args["manter-capturas"] });
  }
  console.error("Uso: node matriz_estados.js medir --base URL --saida DIR [--grupos g1,g2] | comparar --saida DIR [--linha-de-base]");
  return 2;
}

if (require.main === module) {
  principal().then((codigo) => { process.exitCode = codigo; }).catch((erro) => { console.error(erro && erro.stack ? erro.stack : erro); process.exitCode = 2; });
}

module.exports = {
  VIEWPORTS, CELULARES, VIVOS, ESMAECIDOS, NEUTROS, GRUPOS, abrirEstado, listarEstados, grupoGrade, grupoFoco, grupoTecnico, grupoTexto,
  grupoPixels, grupoVivas, grupoBytes, grupoPictogramas, grupoLighthouse, medir, calcularIndicadores, compararMetas, comparar, extrairMapas,
  extrairFolhas, problemasAria, getBruto,
};
