/*
 * Testes de ponta a ponta da página do técnico na v4 (Node + Playwright).
 *
 * Sobe `python src/app.py` num banco SQLite TEMPORÁRIO (AGRO_BANCO) e numa porta
 * livre (nunca a 5000) e confere, num Chromium sem opções:
 *   C2 alvos      com e sem o assistente aberto (calendário e calibração), todo button,
 *                 a[href], summary, input e select visível tem ≥ 48 px de altura (360×740);
 *   C2 retorno    sem leituras da sonda, "Medir terra seca" e "Concluir" mostram o erro
 *                 no próprio cartão: dentro da tela e a ≤ 120 px do botão;
 *   C2 texto      com o texto a 130 % (e 150 %), sem rolagem lateral a 320 e 360 px;
 *   C3 calendário #t-seca e #t-florada existem; Cancelar não muda nada; Confirmar
 *                 inverte banca.seca_programada_ativa e mostra role=status com
 *                 "Seca programada"; a florada vira a data de hoje;
 *   C3 PIN        com PIN definido depois de a página abrir (src/seguranca_banca.py
 *                 --definir-pin 4321 no banco temporário), Confirmar sem PIN pede a
 *                 senha e não muda o estado; com a senha, muda;
 *   qualidade     nenhum erro de console (fora o 403 esperado do PIN), axe-core sem
 *                 violações (se houver axe-core: AGRO_AXE, tests/e2e/vendor ou npm) e
 *                 capturas 360×740 e 1280×800 em reports/ux4/tecnico/ (ou AGRO_UX_CAPTURAS).
 *
 * Uso: node tests/e2e/tecnico_v4.spec.js   (código de saída 0 = tudo certo)
 */
"use strict";

const { chromium } = require("playwright");
const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");
const medidas = require("./validacao/medidas.js");
const ferramentas = require("./validacao/axe_lighthouse.js");

const RAIZ = path.resolve(__dirname, "..", "..");
const PYTHON = process.env.PYTHON || "python";
const CAPTURAS = process.env.AGRO_UX_CAPTURAS || path.join(RAIZ, "reports", "ux4", "tecnico");
const SELETOR_INTERATIVOS = "button, a[href], summary, input:not([type=hidden]), select";

const falhas = [];
let verificacoes = 0;

/** Registra uma verificação; a falha não interrompe as demais. */
function verificar(condicao, descricao) {
  verificacoes += 1;
  if (condicao) { console.log("  ok   " + descricao); } else { console.log("  FALHA " + descricao); falhas.push(descricao); }
}

/** Porta TCP livre (nunca a 5000, a do sítio). */
function portaLivre() {
  return new Promise((resolver, rejeitar) => {
    const servidor = net.createServer();
    servidor.unref();
    servidor.on("error", rejeitar);
    servidor.listen(0, "127.0.0.1", () => {
      const { port } = servidor.address();
      servidor.close(() => (port === 5000 ? portaLivre().then(resolver, rejeitar) : resolver(port)));
    });
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
  for (let tentativa = 0; tentativa < 120; tentativa += 1) {
    try { await fetch(base + "/api/saude"); return { processo, base }; } catch (erro) { await esperar(250); }
  }
  processo.kill();
  throw new Error("O servidor não subiu:\n" + log);
}

/** JSON de uma rota (GET) direto do Node. */
async function lerJson(base, rota) {
  const resposta = await fetch(base + rota);
  return resposta.json();
}

/** Estado da banca (seca programada e florada) em GET /api/cenarios. */
async function estadoBanca(base) { return (await lerJson(base, "/api/cenarios")).banca; }

/** Data local de hoje em AAAA-MM-DD. */
function hojeIso() {
  const d = new Date();
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
}

/** Página nova que guarda os erros de console (fora os permitidos). */
async function novaPagina(contexto, permitidos) {
  const pagina = await contexto.newPage();
  pagina.errosConsole = [];
  pagina.on("console", (m) => { if (m.type() === "error" && !(permitidos || []).some((p) => p.test(m.text()))) { pagina.errosConsole.push(m.text()); } });
  pagina.on("pageerror", (e) => pagina.errosConsole.push("pageerror: " + e.message));
  return pagina;
}

/** Abre /tecnico e espera o conteúdo (ou o cartão do PIN). */
async function abrirTecnico(pagina, base) {
  await pagina.goto(base + "/tecnico");
  await pagina.waitForFunction(() => !document.getElementById("conteudo").hidden || !document.getElementById("cartao-pin").hidden, null, { timeout: 8000 });
  await pagina.waitForFunction(() => !/carregando/.test(document.getElementById("t-seca-estado").textContent), null, { timeout: 8000 }).catch(() => null);
}

/** Interativos visíveis com altura menor que 48 px. */
async function alvosPequenos(pagina) {
  return pagina.evaluate((seletor) => Array.from(document.querySelectorAll(seletor)).filter((e) => {
    const r = e.getBoundingClientRect();
    const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
  }).map((e) => ({ alvo: e.id || e.getAttribute("data-ponto") || e.textContent.trim().slice(0, 30) || e.tagName, altura: Math.round(e.getBoundingClientRect().height * 10) / 10 }))
    .filter((a) => a.altura < 48), SELETOR_INTERATIVOS);
}

/** Abre o "assistente": as duas confirmações do calendário e o erro do passo de 2 pontos. */
async function abrirAssistente(pagina) {
  await pagina.locator("#t-seca").click();
  await pagina.locator("#t-florada").click();
  await pagina.locator("[data-ponto='SECO']").click();
  await pagina.waitForFunction(() => { const p = document.querySelector("#passo-dois-pontos"); return /\S/.test(p.querySelector(".erro-passo").textContent + p.querySelector(".retorno-passo").textContent); }, null, { timeout: 5000 }).catch(() => null);
}

/** Posição do retorno visível do cartão do botão em relação ao botão e à tela. */
async function posicaoRetorno(pagina, seletorBotao) {
  return pagina.evaluate((seletor) => {
    const botao = document.querySelector(seletor);
    const cartao = botao.closest(".passo, .cartao");
    const visivel = Array.from(cartao.querySelectorAll(".erro-passo, .retorno-passo")).find((p) => p.textContent.trim());
    if (!visivel) { return null; }
    const r = visivel.getBoundingClientRect();
    const b = botao.getBoundingClientRect();
    return { texto: visivel.textContent.trim().slice(0, 80), classe: visivel.className, papel: visivel.getAttribute("role"),
      top: Math.round(r.top), bottom: Math.round(r.bottom), altura_tela: innerHeight, distancia: Math.round(r.top - b.bottom) };
  }, seletorBotao);
}

/** C2: alvos de 48 px, retorno junto do botão, texto ampliado sem rolagem lateral, capturas e axe. */
async function testarPadrao(navegador, base) {
  console.log("\n[C2] Página do técnico no padrão (360×740)");
  const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 }, locale: "pt-BR" });
  // 422 = resposta de validação da API de calibração sem leituras da sonda (o caminho testado aqui).
  const pagina = await novaPagina(contexto, [/status of 422/]);
  await abrirTecnico(pagina, base);
  let pequenos = await alvosPequenos(pagina);
  verificar(pequenos.length === 0, "sem o assistente: todo interativo visível com ≥ 48 px " + JSON.stringify(pequenos));
  const meta = await pagina.evaluate(() => ({ escala: !!document.querySelector('meta[name="text-scale"][content="scale"]'),
    cores: (document.querySelector('meta[name="color-scheme"]') || {}).content }));
  verificar(meta.escala && meta.cores === "only light", "meta text-scale e color-scheme only light " + JSON.stringify(meta));
  await pagina.screenshot({ path: path.join(CAPTURAS, "tecnico_360x740.png"), fullPage: true });

  // Retorno junto do botão: "Medir terra seca" e "Concluir" sem leituras da sonda.
  for (const seletor of ["[data-ponto='SECO']", "#concluir"]) {
    await pagina.locator(seletor).scrollIntoViewIfNeeded();
    await pagina.locator(seletor).click();
    await pagina.waitForFunction((s) => { const c = document.querySelector(s).closest(".passo, .cartao"); return Array.from(c.querySelectorAll(".erro-passo, .retorno-passo")).some((p) => p.textContent.trim()); }, seletor, { timeout: 5000 }).catch(() => null);
    await pagina.waitForTimeout(400);
    const pos = await posicaoRetorno(pagina, seletor);
    verificar(!!pos && pos.top >= 0 && pos.bottom <= pos.altura_tela && pos.distancia <= 120,
      `${seletor}: retorno no cartão, visível e perto do botão ${JSON.stringify(pos)}`);
    verificar(!!pos && ((/erro-passo/.test(pos.classe) && pos.papel === "alert") || (/retorno-passo/.test(pos.classe) && pos.papel === "status")), `${seletor}: retorno com role=alert/status`);
  }
  const globais = await pagina.evaluate(() => ({ mensagem: document.getElementById("mensagem").textContent, erro: document.getElementById("erro").textContent }));
  verificar(!globais.mensagem.trim() && !globais.erro.trim(), "#mensagem e #erro globais ficam só para o resumo " + JSON.stringify(globais));

  await abrirAssistente(pagina);
  pequenos = await alvosPequenos(pagina);
  verificar(pequenos.length === 0, "com o assistente aberto: todo interativo visível com ≥ 48 px " + JSON.stringify(pequenos));
  await pagina.screenshot({ path: path.join(CAPTURAS, "tecnico_360x740_assistente.png"), fullPage: true });
  const contraste = await medidas.medirContraste(pagina, null);
  const baixos = contraste.filter((t) => t.razao < t.minimo);
  verificar(baixos.length === 0, "contraste próprio (opacidade e gradiente): todo texto ≥ 4,5:1 (3:1 se grande) " + JSON.stringify(baixos.slice(0, 5)));
  const axe = ferramentas.localizarAxe();
  if (axe) {
    const r = await ferramentas.rodarAxe(pagina, { caminho: axe });
    verificar(r.violacoes.length === 0, "axe-core " + r.versao + " com o assistente: 0 violações " + JSON.stringify(r.violacoes));
  } else {
    console.log("  PULADO: axe-core ausente");
  }
  verificar(pagina.errosConsole.length === 0, "360×740: nenhum erro de console (fora o 422 esperado da calibração) " + pagina.errosConsole.join(" | "));
  await contexto.close();

  // Texto ampliado (como o tamanho de fonte do Android) a 320 e 360 px.
  for (const largura of [320, 360]) {
    for (const fator of [1.3, 1.5]) {
      const ctx = await navegador.newContext({ viewport: { width: largura, height: 740 }, locale: "pt-BR" });
      const p = await ctx.newPage();
      await abrirTecnico(p, base);
      await abrirAssistente(p);
      const r = await medidas.medirTextoAmpliado(p, fator);
      verificar(!r.rolagem_lateral && r.colisoes_topo.length === 0, `texto a ${Math.round(fator * 100)} % a ${largura} px: sem rolagem lateral nem colisão no topo (${r.scrollWidth}/${r.clientWidth}) ${r.colisoes_topo.join(", ")}`);
      await ctx.close();
    }
  }

  // Computador do técnico (1280×800).
  const largo = await navegador.newContext({ viewport: { width: 1280, height: 800 }, locale: "pt-BR" });
  const paginaLarga = await novaPagina(largo);
  await abrirTecnico(paginaLarga, base);
  await paginaLarga.screenshot({ path: path.join(CAPTURAS, "tecnico_1280x800.png"), fullPage: true });
  const pequenosLargo = await alvosPequenos(paginaLarga);
  verificar(pequenosLargo.length === 0, "1280×800: todo interativo visível com ≥ 48 px " + JSON.stringify(pequenosLargo));
  if (axe) {
    const r = await ferramentas.rodarAxe(paginaLarga, { caminho: axe });
    verificar(r.violacoes.length === 0, "axe-core a 1280×800 sem o assistente: 0 violações " + JSON.stringify(r.violacoes));
  }
  verificar(paginaLarga.errosConsole.length === 0, "1280×800: nenhum erro de console " + paginaLarga.errosConsole.join(" | "));
  await largo.close();
}

/** C3: calendário do café com consequência, Confirmar/Cancelar e o PIN da banca. */
async function testarCalendario(navegador, base, banco) {
  console.log("\n[C3] Calendário do café no /tecnico");
  const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 }, locale: "pt-BR" });
  const pagina = await novaPagina(contexto, [/status of 403/]);
  await abrirTecnico(pagina, base);
  verificar(await pagina.locator("#t-seca").isVisible() && await pagina.locator("#t-florada").isVisible(), "#t-seca e #t-florada existem e aparecem");

  // Cancelar não muda nada.
  const antes = await estadoBanca(base);
  await pagina.locator("#t-seca").click();
  const consequencia = await pagina.locator("#conf-seca-texto").textContent();
  verificar(/o aparelho não vai mandar regar\. Confirmar\?$/.test(consequencia), `consequência dita antes de confirmar: "${consequencia}"`);
  const alturas = await pagina.evaluate(() => Array.from(document.querySelectorAll("#conf-seca [data-confirmar], #conf-seca [data-cancelar]")).map((b) => b.getBoundingClientRect().height));
  verificar(alturas.length === 2 && alturas.every((h) => h >= 48), "Confirmar e Cancelar com ≥ 48 px " + JSON.stringify(alturas));
  await pagina.locator("#conf-seca [data-cancelar]").click();
  await pagina.waitForTimeout(300);
  const depoisCancelar = await estadoBanca(base);
  verificar(depoisCancelar.seca_programada_ativa === antes.seca_programada_ativa, "Cancelar deixa banca.seca_programada_ativa inalterado");
  verificar(await pagina.locator("#conf-seca").isHidden(), "Cancelar fecha a confirmação");

  // Confirmar inverte e mostra o retorno junto do botão.
  await pagina.locator("#t-seca").click();
  await pagina.locator("#conf-seca [data-confirmar]").click();
  await pagina.waitForFunction(() => /Seca programada/.test(document.querySelector("#passo-seca [role=status]").textContent), null, { timeout: 5000 }).catch(() => null);
  const depoisConfirmar = await estadoBanca(base);
  verificar(depoisConfirmar.seca_programada_ativa === !antes.seca_programada_ativa, `Confirmar inverte banca.seca_programada_ativa (${antes.seca_programada_ativa} → ${depoisConfirmar.seca_programada_ativa})`);
  const status = await pagina.locator("#passo-seca [role=status]").textContent();
  verificar(/Seca programada/.test(status), `role=status junto do botão: "${status}"`);
  verificar(/Desligar seca programada/.test(await pagina.locator("#t-seca").textContent()), "o botão passa a oferecer o contrário (Desligar)");

  // Florada: hoje.
  await pagina.locator("#t-florada").click();
  verificar((await pagina.locator("#conf-florada-texto").textContent()) === "A data da florada será hoje. Confirmar?", "consequência da florada dita antes de confirmar");
  await pagina.locator("#conf-florada [data-confirmar]").click();
  await pagina.waitForFunction(() => /\S/.test(document.querySelector("#passo-florada [role=status]").textContent), null, { timeout: 5000 }).catch(() => null);
  verificar((await estadoBanca(base)).data_florada_principal === hojeIso(), "Confirmar grava a florada com a data de hoje");
  await pagina.screenshot({ path: path.join(CAPTURAS, "tecnico_calendario_360x740.png") });

  // PIN definido depois de a página abrir: Confirmar sem PIN pede a senha e não muda nada.
  execFileSync(PYTHON, [path.join("src", "seguranca_banca.py"), "--definir-pin", "4321", "--banco", banco], { cwd: RAIZ, stdio: "ignore" });
  const antesPin = await estadoBanca(base);
  await pagina.locator("#t-seca").click();
  await pagina.locator("#conf-seca [data-confirmar]").click();
  await pagina.waitForFunction(() => !document.querySelector("#conf-seca .confirmacao__pin").hidden, null, { timeout: 5000 }).catch(() => null);
  const pedido = await pagina.evaluate(() => ({ campo: !document.querySelector("#conf-seca .confirmacao__pin").hidden,
    erro: document.querySelector("#passo-seca [role=alert]").textContent }));
  verificar(pedido.campo && /senha/i.test(pedido.erro), "sem PIN: a confirmação pede a senha " + JSON.stringify(pedido));
  verificar((await estadoBanca(base)).seca_programada_ativa === antesPin.seca_programada_ativa, "sem PIN: o estado não muda");
  await pagina.screenshot({ path: path.join(CAPTURAS, "tecnico_pin_360x740.png") });
  await pagina.locator("#conf-seca [data-pin-confirmacao]").fill("4321");
  await pagina.locator("#conf-seca [data-confirmar]").click();
  await pagina.waitForFunction(() => /Seca programada/.test(document.querySelector("#passo-seca [role=status]").textContent), null, { timeout: 5000 }).catch(() => null);
  verificar((await estadoBanca(base)).seca_programada_ativa === !antesPin.seca_programada_ativa, "com o PIN digitado: o estado muda");
  verificar(pagina.errosConsole.length === 0, "calendário: nenhum erro de console além do 403 esperado " + pagina.errosConsole.join(" | "));
  await contexto.close();

  // Página aberta já com PIN: pede a senha antes de mostrar o histórico.
  const novo = await navegador.newContext({ viewport: { width: 360, height: 740 }, locale: "pt-BR" });
  const paginaPin = await novaPagina(novo, [/status of 403/]);
  await abrirTecnico(paginaPin, base);
  verificar(await paginaPin.locator("#cartao-pin").isVisible(), "com PIN: /tecnico pede a senha antes do histórico");
  await paginaPin.locator("#pin").fill("4321");
  await paginaPin.locator("#form-pin button[type=submit]").click();
  await paginaPin.waitForFunction(() => !document.getElementById("conteudo").hidden, null, { timeout: 5000 }).catch(() => null);
  verificar(await paginaPin.locator("#t-seca").isVisible(), "com a senha certa: o calendário aparece");
  await novo.close();
  execFileSync(PYTHON, [path.join("src", "seguranca_banca.py"), "--remover-pin", "--banco", banco], { cwd: RAIZ, stdio: "ignore" });

  // Depois da mescla com a frente A, a banca do agricultor não tem mais os dois botões.
  const banca = await navegador.newContext({ viewport: { width: 360, height: 740 } });
  const paginaBanca = await banca.newPage();
  await paginaBanca.goto(base + "/?banca=1");
  await paginaBanca.waitForTimeout(800);
  const naBanca = await paginaBanca.evaluate(() => !!(document.getElementById("b-seca") || document.getElementById("b-florada")));
  console.log(naBanca ? "  aviso: /?banca=1 ainda tem #b-seca/#b-florada (a frente A os remove na mescla)" : "  ok   /?banca=1 sem #b-seca nem #b-florada");
  await banca.close();
}

async function principal() {
  fs.mkdirSync(CAPTURAS, { recursive: true });
  const pasta = fs.mkdtempSync(path.join(os.tmpdir(), "agro_tecnico_v4_"));
  const banco = path.join(pasta, "tecnico.db");
  execFileSync(PYTHON, ["-c", "import sys; sys.path.insert(0, 'src'); from pathlib import Path; from database import inicializar_banco; inicializar_banco(Path(sys.argv[1]))", banco], { cwd: RAIZ });
  const { processo, base } = await subirServidor(banco);
  const navegador = await chromium.launch();
  try {
    await testarPadrao(navegador, base);
    await testarCalendario(navegador, base, banco);
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
