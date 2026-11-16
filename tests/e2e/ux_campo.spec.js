/*
 * Testes de ponta a ponta da interface do agricultor (Node + Playwright).
 *
 * Sobe `python src/app.py` num banco SQLite TEMPORÁRIO (variável AGRO_BANCO),
 * injeta os cenários pela API da banca e confere, num Chromium sem opções:
 *   1. primeira tela sem rolar (360x740 e 360x640): semáforo, título e
 *      pictogramas de ação acima da barra de voz; botões com 48x48 px ou mais;
 *   2. painel da banca recolhido (só com ?banca=1 ou toque longo de 2 s),
 *      selo técnico escondido, troca de cultura em dois passos e aviso a
 *      outro celular; página bem mais curta que a da versão anterior;
 *      PIN da banca pedido uma vez quando configurado;
 *   3. voz: aviso e bipes sem voz pt-BR, voz pt escolhida, preferência
 *      lembrada, repetição do Vermelho a cada 600 s e "perigo" dito uma vez;
 *   4. rede: 2G emulada (50 kbps, 800 ms), página oculta sem consultas e
 *      queda de rede mostrando a última notícia como antiga;
 *   5. nenhum erro de console (fora os de rede provocados de propósito);
 *   6. axe-core OPCIONAL (só se existir tests/e2e/vendor/axe.min.js);
 *   7. causa e retorno (Frente 3), a 360x740 e 360x640: pictograma da causa
 *      visível sem rolar, cada pictograma é um <button> que fala (ou bipa),
 *      botão "?" focável que fala a ajuda, "Feito" com a frase de confirmação
 *      (e guardado sem rede, reenviado depois), Vermelho silenciado não
 *      repete, "Chamar o técnico" abrindo o WhatsApp, rotas de florada/seca
 *      (v4: os botões saíram da banca para /tecnico) e a página /tecnico sem
 *      rolagem lateral.
 *
 * Capturas 360x740 (e 1280x800) vão para reports/ux/ (ou AGRO_UX_CAPTURAS).
 * Uso: node tests/e2e/ux_campo.spec.js   (código de saída 0 = tudo certo)
 */
"use strict";

const { chromium } = require("playwright");
const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");

const RAIZ = path.resolve(__dirname, "..", "..");
const PYTHON = process.env.PYTHON || "python";
const CAPTURAS = process.env.AGRO_UX_CAPTURAS || path.join(RAIZ, "reports", "ux");
const AXE = path.join(__dirname, "vendor", "axe.min.js");
const COMMIT_BASE = "160055b";
const CENARIOS = {
  SECA_EXTREMA: "vermelho", ONDA_DE_CALOR: "vermelho", GEADA: "vermelho",
  ENCHARCAMENTO: "vermelho", ATENCAO_MODERADA: "amarelo", CONDICAO_IDEAL: "verde",
};
const BIPES_POR_COR = { verde: 1, amarelo: 2, vermelho: 3 };

const falhas = [];
let verificacoes = 0;

/** Registra uma verificação; a falha não interrompe as demais. */
function verificar(condicao, descricao) {
  verificacoes += 1;
  if (condicao) { console.log("  ok   " + descricao); } else { console.log("  FALHA " + descricao); falhas.push(descricao); }
}

/** Porta TCP livre no computador. */
function portaLivre() {
  return new Promise((resolver, rejeitar) => {
    const servidor = net.createServer();
    servidor.unref();
    servidor.on("error", rejeitar);
    servidor.listen(0, "127.0.0.1", () => { const { port } = servidor.address(); servidor.close(() => resolver(port)); });
  });
}

const esperar = (ms) => new Promise((resolver) => setTimeout(resolver, ms));

/** Sobe o servidor Flask num banco temporário e espera ele responder. */
async function subirServidor(banco) {
  const porta = await portaLivre();
  const processo = spawn(PYTHON, [path.join("src", "app.py")], {
    cwd: RAIZ,
    env: Object.assign({}, process.env, { AGRO_BANCO: banco, AGRO_PORTA: String(porta), AGRO_HOST: "127.0.0.1" }),
    stdio: ["ignore", "ignore", "pipe"],
  });
  let log = "";
  processo.stderr.on("data", (pedaco) => { log += pedaco.toString(); });
  const base = "http://127.0.0.1:" + porta;
  for (let tentativa = 0; tentativa < 80; tentativa += 1) {
    try { await fetch(base + "/api/saude"); return { processo, base }; } catch (erro) { await esperar(250); }
  }
  processo.kill();
  throw new Error("O servidor não subiu:\n" + log);
}

/** Chamada à API (JSON) direto do Node, sem o navegador. */
async function api(base, metodo, rota, corpo, cabecalhos) {
  const resposta = await fetch(base + rota, {
    method: metodo,
    headers: Object.assign({ "Content-Type": "application/json" }, cabecalhos || {}),
    body: corpo === undefined ? undefined : JSON.stringify(corpo),
  });
  let dados = null;
  try { dados = await resposta.json(); } catch (erro) { dados = null; }
  return { status: resposta.status, dados };
}

/** Cria uma página que guarda os erros de console (menos os de rede esperados). */
async function novaPagina(contexto, errosPermitidos) {
  const pagina = await contexto.newPage();
  pagina.errosConsole = [];
  const permitido = (texto) => (errosPermitidos || []).some((padrao) => padrao.test(texto));
  pagina.on("console", (mensagem) => {
    if (mensagem.type() === "error" && !permitido(mensagem.text())) { pagina.errosConsole.push(mensagem.text()); }
  });
  pagina.on("pageerror", (erro) => { pagina.errosConsole.push("pageerror: " + erro.message); });
  return pagina;
}

async function esperarEstado(pagina, cor, tempo) {
  await pagina.waitForFunction((esperada) => document.body && !document.body.classList.contains("carregando")
    && document.body.getAttribute("data-estado") === esperada, cor, { timeout: tempo || 8000 });
}

/** Retângulos de semáforo, título, ações, barra de voz e aviso flutuante. */
async function medirPrimeiraTela(pagina) {
  return pagina.evaluate(() => {
    const ret = (seletor) => { const e = document.querySelector(seletor); if (!e) { return null; } const r = e.getBoundingClientRect(); return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, width: r.width, height: r.height }; };
    const toast = document.getElementById("toast");
    const botoes = Array.from(document.querySelectorAll("button, summary")).filter((e) => {
      const r = e.getBoundingClientRect(); const estilo = getComputedStyle(e);
      return r.width > 0 && r.height > 0 && estilo.visibility !== "hidden";
    }).map((e) => { const r = e.getBoundingClientRect(); return { id: e.id || e.className || e.tagName, w: r.width, h: r.height }; });
    return {
      altura: window.innerHeight, rolagem: window.scrollY, larguraDocumento: document.documentElement.scrollWidth,
      semaforo: ret("#semaforo"), titulo: ret("#titulo"), acoes: ret(".acoes"), barra: ret(".barra-voz"),
      toast: toast.classList.contains("visivel") ? ret("#toast") : null,
      quantidadeAcoes: document.querySelectorAll(".acoes .acao").length, botoes,
    };
  });
}

function cruza(a, b) {
  return !!(a && b && a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom);
}

/* ------------------------------------------------------------------------ */
async function testarPrimeiraTela(navegador, base) {
  console.log("\n[1] Primeira tela sem rolar, nos 6 cenários");
  fs.mkdirSync(CAPTURAS, { recursive: true });
  for (const [largura, altura] of [[360, 740], [360, 640]]) {
    const contexto = await navegador.newContext({ viewport: { width: largura, height: altura } });
    const pagina = await novaPagina(contexto);
    for (const [cenario, cor] of Object.entries(CENARIOS)) {
      const resposta = await api(base, "POST", "/api/demonstracao", { cenario });
      verificar(resposta.status === 200, `${cenario}: cenário injetado (HTTP ${resposta.status})`);
      await pagina.goto(base + "/");
      await esperarEstado(pagina, cor);
      await pagina.waitForSelector(".acoes .acao");
      const m = await medirPrimeiraTela(pagina);
      const limite = m.altura - m.barra.height;
      const rotulo = `${largura}x${altura} ${cenario}`;
      verificar(m.rolagem === 0 && m.larguraDocumento <= largura, `${rotulo}: sem rolar (nem para o lado)`);
      for (const nome of ["semaforo", "titulo", "acoes"]) {
        verificar(m[nome] && m[nome].bottom <= limite + 0.5, `${rotulo}: ${nome} acima da barra de voz (${Math.round(m[nome].bottom)} <= ${Math.round(limite)})`);
        verificar(!cruza(m[nome], m.barra) && !cruza(m[nome], m.toast), `${rotulo}: ${nome} não cruza a barra de voz nem o aviso`);
      }
      verificar(m.quantidadeAcoes >= 1 && m.quantidadeAcoes <= 3, `${rotulo}: 1 a 3 pictogramas de ação (${m.quantidadeAcoes})`);
      const pequenos = m.botoes.filter((b) => b.w < 48 || b.h < 48);
      verificar(pequenos.length === 0, `${rotulo}: todos os botões com 48x48 px ou mais ${pequenos.length ? JSON.stringify(pequenos) : ""}`);
      if (altura === 740) { await pagina.screenshot({ path: path.join(CAPTURAS, `${cenario.toLowerCase()}_360x740.png`) }); }
    }
    verificar(pagina.errosConsole.length === 0, `${largura}x${altura}: nenhum erro de console ${pagina.errosConsole.join(" | ")}`);
    await contexto.close();
  }
  const largo = await navegador.newContext({ viewport: { width: 1280, height: 800 } });
  const paginaLarga = await novaPagina(largo);
  await api(base, "POST", "/api/demonstracao", { cenario: "ONDA_DE_CALOR" });
  await paginaLarga.goto(base + "/");
  await esperarEstado(paginaLarga, "vermelho");
  await paginaLarga.screenshot({ path: path.join(CAPTURAS, "onda_de_calor_1280x800.png") });
  verificar(paginaLarga.errosConsole.length === 0, "1280x800: nenhum erro de console");
  await largo.close();
}

/* ------------------------------------------------------------------------ */
async function testarBancaECultura(navegador, base, banco) {
  console.log("\n[2] Banca protegida e troca de cultura em dois passos");
  await api(base, "POST", "/api/demonstracao", { cenario: "CONDICAO_IDEAL" });
  const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
  const pagina = await novaPagina(contexto);
  await pagina.goto(base + "/");
  await esperarEstado(pagina, "verde");
  verificar((await pagina.locator(".botao-cenario:visible").count()) === 0, "sem ?banca=1: nenhum botão de cenário visível");
  verificar(!(await pagina.locator("#selo-modo").isVisible()), "sem ?banca=1: selo técnico de modo escondido");
  await pagina.locator("#rotulo-banca").click();
  verificar(!(await pagina.locator("#area-banca").evaluate((e) => e.open)), "toque curto no rótulo não abre a banca");
  const caixa = await pagina.locator("#rotulo-banca").boundingBox();
  await pagina.mouse.move(caixa.x + 20, caixa.y + caixa.height / 2);
  await pagina.mouse.down();
  await pagina.waitForTimeout(2200);
  await pagina.mouse.up();
  await pagina.waitForSelector(".botao-cenario", { state: "visible", timeout: 5000 }).catch(() => null);
  verificar((await pagina.locator(".botao-cenario:visible").count()) === 6, "toque longo de 2 s abre a banca com os 6 cenários");

  const altura = await pagina.evaluate(() => { document.getElementById("area-banca").open = false; return document.documentElement.scrollHeight; });
  const alturaBase = await alturaVersaoAnterior(navegador, base);
  if (alturaBase !== null) {
    verificar(alturaBase - altura >= 400, `página a 360 px ficou ${alturaBase - altura} px mais curta que a versão anterior (${alturaBase} → ${altura})`);
  } else {
    console.log("  aviso: versão anterior indisponível no git; comparação de altura não feita");
  }

  const banca = await novaPagina(contexto);
  await banca.goto(base + "/?banca=1");
  await banca.waitForSelector(".botao-cenario", { state: "visible" });
  verificar((await banca.locator(".botao-cenario:visible").count()) === 6, "com ?banca=1: os 6 botões de cenário aparecem");
  verificar(await banca.locator("#selo-modo").isVisible(), "com ?banca=1: selo técnico de modo aparece");
  const larguras = await banca.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
  verificar(larguras[0] <= larguras[1], `com ?banca=1 a 360 px não há rolagem lateral (${larguras[0]} <= ${larguras[1]})`);
  await banca.close();

  // Outro celular já aberto antes da troca: precisa receber o aviso.
  const outroContexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
  const outro = await novaPagina(outroContexto);
  await outro.goto(base + "/");
  await esperarEstado(outro, "verde");

  await pagina.locator('.botao-cultura[data-codigo="cafe_conilon"]').click();
  await pagina.waitForSelector("#dialogo", { state: "visible" });
  const textoDialogo = await pagina.locator("#dialogo-texto").textContent();
  verificar(/Café Conilon/.test(textoDialogo) && /Confirmar\?/.test(textoDialogo), `diálogo de confirmação: "${textoDialogo}"`);
  await pagina.locator("#dialogo-nao").click();
  await pagina.waitForTimeout(300);
  verificar((await api(base, "GET", "/api/status")).dados.codigo_cultura === "cafe_arabica", "tocar em Conilon e depois em Não mantém cafe_arabica");
  await pagina.locator('.botao-cultura[data-codigo="cafe_conilon"]').click();
  await pagina.locator("#dialogo-sim").click();
  await pagina.waitForFunction(() => document.getElementById("cultura-atual-nome").textContent === "Café Conilon", null, { timeout: 5000 }).catch(() => null);
  verificar((await api(base, "GET", "/api/status")).dados.codigo_cultura === "cafe_conilon", "confirmar com Sim troca a cultura para cafe_conilon");
  verificar(await pagina.locator("#faixa-troca").isHidden(), "o celular que trocou não recebe o aviso da própria troca");
  const inicio = Date.now();
  const apareceu = await outro.waitForFunction(() => /trocada para/.test(document.getElementById("faixa-troca-texto").textContent)
    && !document.getElementById("faixa-troca").hidden, null, { timeout: 6000 }).then(() => true).catch(() => false);
  verificar(apareceu, `outro celular mostra "trocada para" em até 6 s (${Date.now() - inicio} ms)`);
  verificar(pagina.errosConsole.length === 0 && outro.errosConsole.length === 0, "banca e cultura: nenhum erro de console " + pagina.errosConsole.concat(outro.errosConsole).join(" | "));
  await outroContexto.close();
  await api(base, "POST", "/api/cultura", { codigo: "cafe_arabica" });

  // PIN da banca: pedido uma vez, enviado no cabeçalho e lembrado na sessão.
  execFileSync(PYTHON, [path.join("src", "seguranca_banca.py"), "--definir-pin", "1234", "--banco", banco], { cwd: RAIZ });
  const comPin = await novaPagina(contexto, [/status of 403/]);
  await comPin.goto(base + "/?banca=1");
  await comPin.waitForSelector(".botao-cenario", { state: "visible" });
  await comPin.locator('.botao-cenario[data-codigo="GEADA"]').click();
  await comPin.waitForSelector("#dialogo-pin", { state: "visible" });
  await comPin.locator("#dialogo-pin").fill("1234");
  await comPin.locator("#dialogo-sim").click();
  await esperarEstado(comPin, "vermelho");
  verificar((await api(base, "GET", "/api/status")).dados.cenario_demonstracao === "GEADA", "com PIN: senha pedida uma vez e o cenário entra");
  await comPin.locator('.botao-cenario[data-codigo="CONDICAO_IDEAL"]').click();
  await esperarEstado(comPin, "verde");
  verificar(await comPin.locator("#dialogo").isHidden(), "com PIN: a senha fica lembrada na sessão (não pergunta de novo)");
  verificar(comPin.errosConsole.length === 0, "com PIN: nenhum erro de console além do 403 esperado " + comPin.errosConsole.join(" | "));
  execFileSync(PYTHON, [path.join("src", "seguranca_banca.py"), "--remover-pin", "--banco", banco], { cwd: RAIZ });
  await contexto.close();
}

/** Altura do documento a 360 px na versão anterior (página do commit de base, mesma API). */
async function alturaVersaoAnterior(navegador, base) {
  let html;
  try {
    html = execFileSync("git", ["show", COMMIT_BASE + ":src/templates/index.html"], { cwd: RAIZ, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
  } catch (erro) { return null; }
  const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
  const pagina = await contexto.newPage();
  await pagina.route(base + "/", (rota) => rota.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: html }));
  await pagina.goto(base + "/");
  await pagina.waitForSelector(".botao-cenario", { timeout: 8000 }).catch(() => null);
  await pagina.waitForTimeout(500);
  const altura = await pagina.evaluate(() => document.documentElement.scrollHeight);
  await contexto.close();
  return altura;
}

/* ------------------------------------------------------------------------ */
/** Substitui a fala e o áudio do navegador por dublês que contam as chamadas. */
function dublesDeVoz(idiomas) {
  window.__falas = [];
  window.__osciladores = 0;
  window.__vibracoes = 0;
  function Fala(texto) { this.text = texto; this.voice = null; this.lang = ""; }
  window.SpeechSynthesisUtterance = Fala;
  const vozes = idiomas.map((idioma) => ({ lang: idioma, name: "Voz " + idioma, voiceURI: idioma, localService: true, default: false }));
  const sintese = {
    speaking: false, pending: false, paused: false, onvoiceschanged: null,
    getVoices() { return vozes; },
    cancel() {},
    speak(fala) {
      window.__falas.push({ texto: fala.text, lang: fala.voice ? fala.voice.lang : null });
      Promise.resolve().then(() => { if (fala.onstart) { fala.onstart({}); } if (fala.onend) { fala.onend({}); } });
    },
    addEventListener() {},
  };
  Object.defineProperty(window, "speechSynthesis", { value: sintese, configurable: true });
  function Contexto() { this.currentTime = 0; this.state = "running"; this.destination = {}; }
  Contexto.prototype.resume = function () { return Promise.resolve(); };
  Contexto.prototype.createOscillator = function () { window.__osciladores += 1; return { type: "", frequency: { value: 0 }, connect() {}, start() {}, stop() {} }; };
  Contexto.prototype.createGain = function () { return { gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} }, connect() {} }; };
  window.AudioContext = Contexto;
  window.webkitAudioContext = Contexto;
  Object.defineProperty(navigator, "vibrate", { value: () => { window.__vibracoes += 1; return true; }, configurable: true });
}

async function testarVoz(navegador, base) {
  console.log("\n[3] Voz que não falha calada");
  for (const [cenario, cor] of [["CONDICAO_IDEAL", "verde"], ["ATENCAO_MODERADA", "amarelo"], ["SECA_EXTREMA", "vermelho"]]) {
    await api(base, "POST", "/api/demonstracao", { cenario });
    const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
    await contexto.addInitScript(dublesDeVoz, ["en-US"]);
    const pagina = await novaPagina(contexto);
    await pagina.goto(base + "/");
    await esperarEstado(pagina, cor);
    await pagina.locator("#botao-voz").click();
    await pagina.waitForTimeout(200);
    const aviso = await pagina.locator("#aviso-voz").isVisible() ? await pagina.locator("#aviso-voz").textContent() : "";
    verificar(/não tem voz em português/.test(aviso), `${cor}: só voz en-US mostra o aviso "não tem voz em português"`);
    const bipes = await pagina.evaluate(() => window.__osciladores);
    verificar(bipes === BIPES_POR_COR[cor], `${cor}: ${bipes} bipe(s) (esperado ${BIPES_POR_COR[cor]})`);
    verificar((await pagina.evaluate(() => window.__falas.length)) === 0, `${cor}: sem voz pt nada é falado em inglês`);
    verificar(pagina.errosConsole.length === 0, `${cor}: nenhum erro de console`);
    await contexto.close();
  }

  await api(base, "POST", "/api/demonstracao", { cenario: "SECA_EXTREMA" });
  const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
  await contexto.addInitScript(dublesDeVoz, ["en-US", "pt-BR"]);
  const pagina = await novaPagina(contexto);
  await pagina.goto(base + "/");
  await esperarEstado(pagina, "vermelho");
  await pagina.locator("#botao-voz").click();
  await pagina.waitForFunction(() => window.__falas.length > 0);
  const fala = await pagina.evaluate(() => window.__falas[0]);
  verificar(String(fala.lang || "").indexOf("pt") === 0, `com pt-BR disponível a voz escolhida é "${fala.lang}"`);
  const perigos = (fala.texto.match(/perigo/gi) || []).length;
  verificar(perigos <= 1, `SECA_EXTREMA: "perigo" falado ${perigos} vez(es): "${fala.texto}"`);
  verificar(!(await pagina.locator("#aviso-voz").isVisible()), "com voz pt não aparece o aviso de falta de voz");
  await pagina.reload();
  await esperarEstado(pagina, "vermelho");
  verificar(await pagina.evaluate(() => document.body.classList.contains("voz-automatica")), "depois de tocar e recarregar, body tem a classe voz-automatica");
  verificar(pagina.errosConsole.length === 0, "voz pt: nenhum erro de console");
  await contexto.close();

  // Repetição do Vermelho com relógio controlado e status fixo.
  // Mesmo estado do HTML inicial (a situação não muda), com a demonstração "sem fim" durante o teste.
  await api(base, "POST", "/api/demonstracao", { cenario: "SECA_EXTREMA" });
  const status = (await api(base, "GET", "/api/status")).dados;
  status.segundos_restantes_demonstracao = 100000;
  status.intervalo_repeticao_alerta_segundos = 600;
  const relogio = await navegador.newContext({ viewport: { width: 360, height: 740 } });
  await relogio.addInitScript(dublesDeVoz, ["pt-BR"]);
  const paginaRelogio = await novaPagina(relogio);
  await paginaRelogio.clock.install();
  await paginaRelogio.route("**/api/status", (rota) => rota.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(status) }));
  await paginaRelogio.goto(base + "/");
  await esperarEstado(paginaRelogio, "vermelho");
  await paginaRelogio.locator("#botao-voz").click();
  await paginaRelogio.waitForFunction(() => window.__falas.length === 1);
  await paginaRelogio.clock.runFor(599000);
  const antes = await paginaRelogio.evaluate(() => window.__falas.length + window.__osciladores);
  verificar(antes === 1, `vermelho fixo: nenhuma repetição antes de 600 s (${antes - 1} repetições)`);
  await paginaRelogio.clock.runFor(2000);
  const depois = await paginaRelogio.evaluate(() => ({ falas: window.__falas.length, bipes: window.__osciladores, vibracoes: window.__vibracoes }));
  verificar(depois.falas + depois.bipes >= 2, `vermelho fixo: repete a fala depois de 600 s (${JSON.stringify(depois)})`);
  verificar(depois.vibracoes >= 1, "vermelho fixo: vibra junto com a repetição");
  verificar(paginaRelogio.errosConsole.length === 0, "repetição: nenhum erro de console");
  await relogio.close();
}

/* ------------------------------------------------------------------------ */
async function testarRede(navegador, base) {
  console.log("\n[4] Rede fraca e queda de rede");
  await api(base, "POST", "/api/demonstracao", { cenario: "SECA_EXTREMA" });
  const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
  const lenta = await novaPagina(contexto);
  const cdp = await contexto.newCDPSession(lenta);
  await cdp.send("Network.enable");
  await cdp.send("Network.emulateNetworkConditions", { offline: false, latency: 800, downloadThroughput: 50 * 1024 / 8, uploadThroughput: 50 * 1024 / 8 });
  const inicio = Date.now();
  await lenta.goto(base + "/", { waitUntil: "commit" });
  const pronto = await esperarEstado(lenta, "vermelho", 4000).then(() => true).catch(() => false);
  const tempo = Date.now() - inicio;
  verificar(pronto && tempo <= 4000, `2G (50 kbps, 800 ms): cor certa e sem "carregando" em ${tempo} ms (limite 4000 ms)`);
  await cdp.send("Network.emulateNetworkConditions", { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
  await lenta.close();

  const oculta = await novaPagina(contexto);
  await oculta.clock.install();
  await oculta.goto(base + "/");
  await esperarEstado(oculta, "vermelho");
  await oculta.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    Object.defineProperty(document, "hidden", { value: true, configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  let consultas = 0;
  oculta.on("request", (pedido) => { if (pedido.url().indexOf("/api/status") !== -1) { consultas += 1; } });
  await oculta.clock.runFor(60000);
  await oculta.waitForTimeout(300);
  verificar(consultas === 0, `página oculta por 60 s: ${consultas} consulta(s) a /api/status`);
  await oculta.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    Object.defineProperty(document, "hidden", { value: false, configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await oculta.waitForTimeout(500);
  verificar(consultas >= 1, "ao voltar a ficar visível a página consulta na hora");
  await oculta.close();

  const semRede = await novaPagina(contexto, [/Failed to load resource/, /ERR_INTERNET_DISCONNECTED/]);
  await semRede.goto(base + "/");
  await esperarEstado(semRede, "vermelho");
  await semRede.waitForTimeout(300);
  await contexto.setOffline(true);
  const desatualizada = await semRede.waitForFunction(() => document.body.classList.contains("desatualizada")
    && /sem ligação/i.test(document.body.innerText), null, { timeout: 15000 }).then(() => true).catch(() => false);
  verificar(desatualizada, "sem rede: body.desatualizada e o texto \"sem ligação\"");
  const textoAviso = await semRede.locator("#aviso-teste-texto").textContent();
  verificar(/Última notícia de (hoje|ontem|em) .* — sem ligação agora/.test(textoAviso), `sem rede: aviso "${textoAviso}"`);
  await semRede.screenshot({ path: path.join(CAPTURAS, "sem_ligacao_360x740.png") });
  await contexto.setOffline(false);
  verificar(semRede.errosConsole.length === 0, "sem rede: nenhum erro de console além dos de rede " + semRede.errosConsole.join(" | "));
  await contexto.close();
}

/* ------------------------------------------------------------------------ */
async function testarAcessibilidade(navegador, base) {
  if (!fs.existsSync(AXE)) { console.log("\n[5] axe-core ausente (tests/e2e/vendor/axe.min.js): verificação de acessibilidade pulada"); return; }
  console.log("\n[5] Acessibilidade (axe-core)");
  await api(base, "POST", "/api/demonstracao", { cenario: "SECA_EXTREMA" });
  const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
  const pagina = await novaPagina(contexto);
  await pagina.goto(base + "/");
  await esperarEstado(pagina, "vermelho");
  await pagina.addScriptTag({ path: AXE });
  const resultado = await pagina.evaluate(() => window.axe.run(document, { resultTypes: ["violations"] }));
  const graves = resultado.violations.filter((v) => v.impact === "critical" || v.impact === "serious");
  resultado.violations.forEach((v) => console.log(`  axe ${v.impact}: ${v.id} (${v.nodes.length})`));
  verificar(graves.length === 0, `axe-core: ${graves.length} violação(ões) graves`);
  await contexto.close();
}

/* ------------------------------------------------------------------------ */
const CENARIOS_CAUSA = { SECA_EXTREMA: "TERRA_SECA", ONDA_DE_CALOR: "CALOR", GEADA: "FRIO_GEADA", ENCHARCAMENTO: "ENCHARCADO" };

async function testarCausaERetorno(navegador, base, banco) {
  console.log("\n[7] Causa visível, pictogramas que falam, \"Feito\" e técnico");
  for (const [largura, altura] of [[360, 740], [360, 640]]) {
    const contexto = await navegador.newContext({ viewport: { width: largura, height: altura } });
    await contexto.addInitScript(dublesDeVoz, ["pt-BR"]);
    const pagina = await novaPagina(contexto);
    for (const [cenario, causa] of Object.entries(CENARIOS_CAUSA)) {
      await api(base, "POST", "/api/demonstracao", { cenario });
      await pagina.goto(base + "/");
      await esperarEstado(pagina, "vermelho");
      await pagina.waitForSelector(".acoes .acao");
      const rotulo = `${largura}x${altura} ${cenario}`;
      const m = await pagina.evaluate(() => {
        const r = (e) => e.getBoundingClientRect();
        const barra = r(document.querySelector(".barra-voz"));
        const causa = document.getElementById("causa");
        return {
          altura: window.innerHeight, barraTopo: barra.top, rolagem: window.scrollY, causa: r(causa).toJSON(),
          causaVisivel: getComputedStyle(causa).visibility !== "hidden" && r(causa).width >= 56 && r(causa).height >= 56,
          quadros: Array.from(document.querySelectorAll(".acao__quadro")).map((e) => ({ tag: e.tagName, w: r(e).width, h: r(e).height })),
          ajuda: !!document.getElementById("ajuda"),
        };
      });
      verificar(m.rolagem === 0 && m.causaVisivel && m.causa.bottom <= m.barraTopo + 0.5,
        `${rotulo}: pictograma da causa visível sem rolar (${Math.round(m.causa.bottom)} <= ${Math.round(m.barraTopo)})`);
      verificar(m.quadros.length >= 1 && m.quadros.every((q) => q.tag === "BUTTON" && q.w >= 56 && q.h >= 56),
        `${rotulo}: cada pictograma é um <button> de 56 px ou mais ${JSON.stringify(m.quadros)}`);
      const icone = await pagina.locator("#causa-icone").getAttribute("href");
      const status = (await api(base, "GET", "/api/status")).dados;
      verificar(status.causa === causa && icone !== "#i-broto", `${rotulo}: causa ${status.causa} com desenho ${icone}`);
      if (altura === 740) {
        // Cada toque fala (voz pt-BR do dublê) a descrição da ação ou da causa.
        const quantidade = m.quadros.length;
        for (let i = 0; i < quantidade; i += 1) {
          const antes = await pagina.evaluate(() => window.__falas.length + window.__osciladores);
          await pagina.locator(".acao__quadro").nth(i).click();
          await pagina.waitForFunction((n) => window.__falas.length + window.__osciladores > n, antes, { timeout: 3000 }).catch(() => null);
          const depois = await pagina.evaluate(() => ({ n: window.__falas.length + window.__osciladores, ultima: (window.__falas[window.__falas.length - 1] || {}).texto }));
          const descricao = status.acoes_detalhe[i].descricao;
          verificar(depois.n > antes && depois.ultima === descricao, `${rotulo}: tocar no pictograma ${i + 1} fala "${depois.ultima}"`);
        }
        await pagina.locator("#causa").click();
        await pagina.waitForTimeout(150);
        const falaCausa = await pagina.evaluate(() => (window.__falas[window.__falas.length - 1] || {}).texto);
        verificar(falaCausa === status.causa_descricao, `${rotulo}: tocar na causa fala "${falaCausa}"`);
        await pagina.screenshot({ path: path.join(CAPTURAS, `causa_${cenario.toLowerCase()}_${largura}x${altura}.png`) });
      }
    }
    const ajuda = await pagina.evaluate(() => { const b = document.getElementById("ajuda"); b.focus(); const r = b.getBoundingClientRect(); return { foco: document.activeElement === b, w: r.width, h: r.height, fixo: getComputedStyle(b.closest(".barra-voz")).position }; });
    verificar(ajuda.foco && ajuda.w >= 48 && ajuda.h >= 48 && ajuda.fixo === "fixed", `${largura}x${altura}: botão "?" fixo e focável ${JSON.stringify(ajuda)}`);
    await pagina.keyboard.press("Enter");
    await pagina.waitForTimeout(150);
    const falaAjuda = await pagina.evaluate(() => (window.__falas[window.__falas.length - 1] || {}).texto);
    verificar(/O desenho grande diz o motivo/.test(falaAjuda || ""), `${largura}x${altura}: "?" fala a ajuda ("${falaAjuda}")`);
    verificar(pagina.errosConsole.length === 0, `${largura}x${altura} causa: nenhum erro de console ${pagina.errosConsole.join(" | ")}`);
    await contexto.close();
  }

  // "Feito" de um toque no cenário SECA_EXTREMA: frase de confirmação falada e no aviso.
  await api(base, "POST", "/api/demonstracao", { cenario: "SECA_EXTREMA" });
  const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
  await contexto.addInitScript(dublesDeVoz, ["pt-BR"]);
  const pagina = await novaPagina(contexto, [/Failed to load resource/, /ERR_FAILED/]);
  await pagina.goto(base + "/");
  await esperarEstado(pagina, "vermelho");
  await pagina.waitForSelector("#feito", { state: "visible" });
  // A faixa "plantação trocada" do teste [2] ainda vale por 10 min: fechada para medir a tela de sempre.
  if (await pagina.locator("#faixa-troca").isVisible()) { await pagina.locator("#faixa-troca-fechar").click(); }
  const feito = await pagina.evaluate(() => { const b = document.getElementById("feito"); const r = b.getBoundingClientRect(); const barra = document.querySelector(".barra-voz").getBoundingClientRect(); return { ok: !b.hidden && r.height >= 48 && r.bottom <= barra.top + 0.5, fim: Math.round(r.bottom), barra: Math.round(barra.top), classes: document.body.className }; });
  verificar(feito.ok, `SECA_EXTREMA 360x740: botão "Feito" (48 px ou mais) acima da barra de voz ${JSON.stringify(feito)}`);
  await pagina.locator("#feito").click();
  const toast = await pagina.waitForFunction(() => /^Anotado: você regou/.test(document.getElementById("toast").textContent), null, { timeout: 5000 })
    .then(() => pagina.locator("#toast").textContent()).catch(() => "");
  const falaFeito = await pagina.evaluate(() => (window.__falas[window.__falas.length - 1] || {}).texto);
  // v4 (Frente A, melhoria A7 — adaptação justificada): a confirmação do "Feito" passou a terminar com
  // "Se não fez, toque de novo para desfazer." (o botão vira "Desfazer" por 15 s). A frase do servidor
  // continua a mesma; o teste aceita esse final fixo. O resto da verificação não mudou.
  verificar(/^Anotado: você regou (às \d+ horas|à 1 hora|à meia-noite|ao meio-dia)\.( Se não fez, toque de novo para desfazer\.)?$/.test(toast) && falaFeito === toast, `"Feito": confirmação "${toast}" falada`);
  verificar((await api(base, "GET", "/api/status")).dados.causa_silenciada === true, "depois do \"Feito\" a causa fica silenciada no servidor");
  await pagina.screenshot({ path: path.join(CAPTURAS, "feito_seca_extrema_360x740.png") });

  // v4 (A7 — adaptação justificada): logo depois do "Feito" o mesmo botão é "Desfazer" (15 s), então o
  // segundo toque desfaz em vez de anotar de novo. Para testar o "Feito" sem rede como antes, o primeiro
  // é desfeito (passada a trava de 0,8 s contra toque duplo) e o botão volta a "Feito".
  await pagina.waitForTimeout(900);
  await pagina.locator("#feito").click();
  await pagina.waitForFunction(() => document.getElementById("feito").getAttribute("data-estado-feito") === "pronto"
    && /desfeito/.test(document.getElementById("toast").textContent), null, { timeout: 5000 }).catch(() => null);

  // Sem rede: o toque fica guardado e vai quando a ligação voltar.
  // Toque sobre leitura da banca não entra nas "ações feitas" do técnico: conta-se o evento "feito".
  // v4 (integração, meta 11 — adaptação justificada): o evento de um toque da DEMONSTRAÇÃO não entra
  // mais no resumo do técnico (eventos_interface); ele é contado à parte em "demonstracao_excluida".
  // A verificação continua a mesma — o toque guardado sem rede chega ao servidor uma única vez.
  const eventosDaBanca = (resumo) => resumo.dados.eventos_interface.feito + resumo.dados.demonstracao_excluida.eventos_interface;
  const antes = eventosDaBanca(await api(base, "GET", "/api/tecnico/resumo"));
  await pagina.route("**/api/acao_feita", (rota) => rota.abort("failed"));
  await pagina.locator("#feito").click();
  await pagina.waitForFunction(() => /ligação voltar/.test(document.getElementById("toast").textContent), null, { timeout: 5000 }).catch(() => null);
  const guardados = await pagina.evaluate(() => JSON.parse(localStorage.getItem("agro_feitos") || "[]").length);
  verificar(guardados === 1, `sem rede: "Feito" guardado no aparelho (${guardados})`);
  await pagina.unroute("**/api/acao_feita");
  await pagina.evaluate(() => { document.dispatchEvent(new Event("visibilitychange")); });
  const reenviado = await pagina.waitForFunction(() => !localStorage.getItem("agro_feitos"), null, { timeout: 8000 }).then(() => true).catch(() => false);
  await pagina.waitForTimeout(300);
  const depois = eventosDaBanca(await api(base, "GET", "/api/tecnico/resumo"));
  verificar(reenviado && depois === antes + 1, `ligação de volta: "Feito" reenviado (${antes} → ${depois})`);
  verificar(pagina.errosConsole.length === 0, "\"Feito\": nenhum erro de console além da queda simulada " + pagina.errosConsole.join(" | "));
  await contexto.close();

  // Vermelho com "Feito" recente (causa_silenciada) não repete a fala depois de 600 s.
  const status = (await api(base, "GET", "/api/status")).dados;
  Object.assign(status, { segundos_restantes_demonstracao: 100000, intervalo_repeticao_alerta_segundos: 600, causa_silenciada: true });
  const relogio = await navegador.newContext({ viewport: { width: 360, height: 740 } });
  await relogio.addInitScript(dublesDeVoz, ["pt-BR"]);
  const paginaRelogio = await novaPagina(relogio);
  await paginaRelogio.clock.install();
  await paginaRelogio.route("**/api/status", (rota) => rota.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(status) }));
  await paginaRelogio.goto(base + "/");
  await esperarEstado(paginaRelogio, "vermelho");
  await paginaRelogio.locator("#botao-voz").click();
  await paginaRelogio.waitForFunction(() => window.__falas.length === 1);
  await paginaRelogio.clock.runFor(1250000);
  const repeticoes = await paginaRelogio.evaluate(() => window.__falas.length + window.__osciladores + window.__vibracoes - 1);
  verificar(repeticoes === 0, `Vermelho silenciado por "Feito": nenhuma repetição em 20 min (${repeticoes})`);
  await relogio.close();

  // "Chamar o técnico": confirma e abre o WhatsApp com o recado (o servidor continua offline).
  await api(base, "POST", "/api/tecnico/configuracao", { numero_whatsapp_tecnico: "35 99999 0000" });
  const tecnico = await navegador.newContext({ viewport: { width: 360, height: 740 } });
  const paginaTecnico = await novaPagina(tecnico);
  await paginaTecnico.route("https://wa.me/**", (rota) => rota.fulfill({ status: 200, contentType: "text/html", body: "<!doctype html><title>WhatsApp</title>" }));
  await paginaTecnico.goto(base + "/");
  await esperarEstado(paginaTecnico, "vermelho");
  await paginaTecnico.locator("#tecnico").click();
  await paginaTecnico.waitForSelector("#dialogo", { state: "visible" });
  await paginaTecnico.locator("#dialogo-sim").click();
  const abriu = await paginaTecnico.waitForURL(/^https:\/\/wa\.me\/5535999990000\?text=/, { timeout: 5000 }).then(() => true).catch(() => false);
  verificar(abriu, `"Técnico" abre ${paginaTecnico.url().slice(0, 60)}…`);
  verificar(paginaTecnico.errosConsole.length === 0, "técnico: nenhum erro de console " + paginaTecnico.errosConsole.join(" | "));

  // v4 (Frente A, melhoria A8 — adaptação justificada): "O café floriu hoje" e a seca programada são
  // configuração agronômica e saíram da área da banca (que ficou só de demonstração) para a página do
  // técnico (/tecnico, Frente C). As rotas POST /api/banca/florada e /api/banca/seca_programada mantêm o
  // mesmo contrato (PIN pelo cabeçalho X-PIN-Banca), que é o que este caso passa a conferir; a banca é
  // conferida sem os botões antigos.
  const banca = await novaPagina(tecnico, [/status of 403/]);
  execFileSync(PYTHON, [path.join("src", "seguranca_banca.py"), "--definir-pin", "1234", "--banco", banco], { cwd: RAIZ });
  await banca.goto(base + "/?banca=1");
  await banca.waitForSelector(".botao-cenario", { state: "visible" });
  verificar((await banca.locator("#b-florada, #b-seca").count()) === 0, "banca só de demonstração: sem florada nem seca programada (foram para /tecnico)");
  verificar((await api(base, "POST", "/api/banca/florada", {})).status === 403, "florada sem o PIN: 403 (contrato mantido)");
  const florada = await api(base, "POST", "/api/banca/florada", {}, { "X-PIN-Banca": "1234" });
  verificar(florada.status === 200 && /floriu em/.test(florada.dados.fala || ""), `florada com o PIN grava a data ("${(florada.dados || {}).fala}")`);
  const secaAntes = await api(base, "POST", "/api/banca/seca_programada", { ativa: false }, { "X-PIN-Banca": "1234" });
  const secaDepois = await api(base, "POST", "/api/banca/seca_programada", {}, { "X-PIN-Banca": "1234" });
  verificar(secaAntes.status === 200 && secaDepois.status === 200 && secaDepois.dados.seca_programada_ativa === true
    && /Seca programada ligada/.test(secaDepois.dados.fala || ""), `seca programada alterna pela rota ("${(secaDepois.dados || {}).fala}")`);
  verificar(banca.errosConsole.length === 0, "banca: nenhum erro de console " + banca.errosConsole.join(" | "));

  // Página do técnico a 360x740: PIN, resumo e nenhuma rolagem lateral.
  const paginaResumo = await novaPagina(tecnico, [/status of 403/]);
  await paginaResumo.goto(base + "/tecnico");
  await paginaResumo.waitForSelector("#pin", { state: "visible" });
  await paginaResumo.locator("#pin").fill("1234");
  await paginaResumo.locator("#form-pin button").click();
  await paginaResumo.waitForSelector("#conteudo:not([hidden])", { timeout: 8000 });
  await paginaResumo.waitForFunction(() => document.getElementById("texto-whatsapp").value.length > 0, null, { timeout: 5000 });
  const larguras = await paginaResumo.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
  verificar(larguras[0] <= larguras[1], `/tecnico a 360 px sem rolagem lateral (${larguras[0]} <= ${larguras[1]})`);
  const whatsapp = await paginaResumo.locator("#texto-whatsapp").inputValue();
  verificar(whatsapp.length <= 700 && /AgroAnálise/.test(whatsapp), `/tecnico: texto do WhatsApp com ${whatsapp.length} caracteres`);
  await paginaResumo.screenshot({ path: path.join(CAPTURAS, "tecnico_360x740.png"), fullPage: true });
  verificar(paginaResumo.errosConsole.length === 0, "/tecnico: nenhum erro de console além do 403 esperado " + paginaResumo.errosConsole.join(" | "));
  execFileSync(PYTHON, [path.join("src", "seguranca_banca.py"), "--remover-pin", "--banco", banco], { cwd: RAIZ });
  await tecnico.close();
}

async function principal() {
  const pasta = fs.mkdtempSync(path.join(os.tmpdir(), "agro_ux_"));
  const banco = path.join(pasta, "ux.db");
  const { processo, base } = await subirServidor(banco);
  const navegador = await chromium.launch();
  try {
    await testarPrimeiraTela(navegador, base);
    await testarBancaECultura(navegador, base, banco);
    await testarVoz(navegador, base);
    await testarRede(navegador, base);
    await testarAcessibilidade(navegador, base);
    await testarCausaERetorno(navegador, base, banco);
  } finally {
    await navegador.close();
    processo.kill();
    fs.rmSync(pasta, { recursive: true, force: true });
  }
  console.log(`\n${verificacoes - falhas.length}/${verificacoes} verificações ok. Capturas em ${CAPTURAS}`);
  if (falhas.length) {
    console.log("Falhas:\n - " + falhas.join("\n - "));
    process.exitCode = 1;
  }
}

principal().catch((erro) => { console.error(erro); process.exitCode = 1; });
