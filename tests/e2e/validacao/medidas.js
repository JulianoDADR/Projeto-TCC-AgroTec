/*
 * Medidas de página da suíte de validação UX (v4), só com Playwright.
 *
 * Portado de scripts/medidas_pagina.js da auditoria de acessibilidade do comitê
 * de UX: o contraste considera a OPACIDADE de todos os ancestrais e, em fundo
 * com gradiente, a PIOR parada do gradiente; o alvo de toque é o retângulo
 * renderizado; a oclusão é a sobreposição vertical com #barra-voz.
 *
 * Exporta (assinaturas estáveis, usadas também pela frente A):
 *   medirContraste(page, raiz)  -> Promise<Array<{seletor, texto, tamanho_px, peso, grande, minimo, razao,
 *                                   frente, fundo, opacidade, acima_dobra}>>
 *   medirIcones(page, raiz)     -> Promise<Array<{seletor, razao, controle}>>
 *   medirOclusao(page, seletores: string[]) -> Promise<Array<{seletor, sobreposicao_px}>>
 *   medirAlvos(page, raiz)      -> Promise<Array<{seletor, rotulo, largura, altura}>>
 *   medirFocoObscurecido(page, tabs) -> Promise<{tabs, obscurecidos, focos}>
 *   coletarEstado(page)         -> Promise<objeto com o retrato do semáforo, cartão, ações e barra>
 *   medirTextoAmpliado(page, fator) -> Promise<{scrollWidth, clientWidth, rolagem_lateral, colisoes_topo}>
 *   analisarCss(texto)          -> {font_size_px: number, ocorrencias: string[]}
 *   scriptVozFalsa(lingua)      -> código para addInitScript (voz, vibração e bipes registrados)
 */
"use strict";

/**
 * Biblioteca executada DENTRO da página: instala window.__uxMedidas (idempotente).
 * Não usa nada de fora da função (é serializada com toString()).
 */
function instalarBiblioteca() {
  if (window.__uxMedidas) { return; }
  function parse(str) {
    if (!str) { return null; }
    const texto = String(str).trim();
    let m = texto.match(/^rgba?\(([^)]+)\)$/);
    if (m) {
      const p = m[1].split(/[ ,/]+/).filter(Boolean).map((v) => (v.endsWith("%") ? parseFloat(v) / 100 : Number(v)));
      return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
    }
    m = texto.match(/^color\(srgb ([^)]+)\)$/);
    if (m) {
      const p = m[1].split(/[ /]+/).filter(Boolean).map(Number);
      return [p[0] * 255, p[1] * 255, p[2] * 255, p.length > 3 ? p[3] : 1];
    }
    return null;
  }
  function coresGradiente(str) {
    const saida = [];
    const re = /(rgba?\([^)]+\)|color\(srgb [^)]+\))/g;
    let m;
    while ((m = re.exec(str))) { const c = parse(m[1]); if (c) { saida.push(c); } }
    return saida;
  }
  function sobre(c, fundo) {
    const a = c[3];
    return [c[0] * a + fundo[0] * (1 - a), c[1] * a + fundo[1] * (1 - a), c[2] * a + fundo[2] * (1 - a), 1];
  }
  function misturar(c, fundo, o) { return sobre([c[0], c[1], c[2], o], fundo); }
  function lin(v) { const x = v / 255; return x <= 0.04045 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); }
  function lum(c) { return 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2]); }
  function razao(a, b) { const la = lum(a), lb = lum(b); return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05); }
  function hex(c) { return "#" + c.slice(0, 3).map((v) => Math.round(v).toString(16).padStart(2, "0")).join(""); }
  /** Renderizado, com área, sem display:none/visibility:hidden/opacidade ~0 e fora de recortes de leitor de tela. */
  function visivel(el) {
    const r = el.getBoundingClientRect();
    if (r.width <= 1 || r.height <= 1) { return false; }
    let e = el;
    let opacidade = 1;
    while (e && e.nodeType === 1) {
      const s = getComputedStyle(e);
      if (s.display === "none" || s.visibility === "hidden" || s.visibility === "collapse") { return false; }
      opacidade *= parseFloat(s.opacity);
      if ((s.clip && s.clip !== "auto" && /rect\(0(px)?,? ?0(px)?/.test(s.clip)) || /inset\(50%\)/.test(s.clipPath || "")) { return false; }
      e = e.parentElement;
    }
    return opacidade > 0.05;
  }
  function seletor(el) {
    if (el.id) { return "#" + el.id; }
    const partes = [];
    let e = el;
    while (e && e.nodeType === 1 && partes.length < 4) {
      let p = e.tagName.toLowerCase();
      if (e.id) { partes.unshift("#" + e.id); break; }
      const cls = String(e.getAttribute("class") || "").trim().split(/\s+/).filter(Boolean)[0];
      if (cls) { p += "." + cls; }
      const acao = e.getAttribute("data-acao");
      if (acao) { p += "[data-acao=" + acao + "]"; }
      partes.unshift(p);
      e = e.parentElement;
    }
    return partes.join(" > ");
  }
  /** Pior razão de contraste do primeiro plano com os fundos possíveis, com a opacidade dos ancestrais. */
  function contraste(el, corPrimeiroPlano) {
    const cadeia = [];
    let e = el;
    while (e && e.nodeType === 1) { cadeia.unshift(e); e = e.parentElement; }
    let candidatos = [[255, 255, 255, 1]];
    const opacidades = [];
    let filtro = false;
    for (const n of cadeia) {
      const s = getComputedStyle(n);
      const o = parseFloat(s.opacity);
      if (o < 1) { opacidades.push({ o, atras: candidatos.slice() }); }
      if (s.filter && s.filter !== "none") { filtro = true; }
      const bc = parse(s.backgroundColor);
      if (bc && bc[3] > 0) { candidatos = candidatos.map((c) => sobre(bc, c)); }
      if (s.backgroundImage && s.backgroundImage !== "none") {
        const gs = coresGradiente(s.backgroundImage);
        if (gs.length) {
          const novos = [];
          for (const c of candidatos) { for (const g of gs) { novos.push(sobre(g, c)); } }
          candidatos = novos.slice(0, 24);
        }
      }
    }
    const fg = parse(corPrimeiroPlano) || [0, 0, 0, 1];
    let pares = candidatos.map((b) => [sobre(fg, b), b]);
    for (let i = opacidades.length - 1; i >= 0; i -= 1) {
      const { o, atras } = opacidades[i];
      pares = pares.map(([f, b]) => [misturar(f, atras[0], o), misturar(b, atras[0], o)]);
    }
    let pior = null;
    for (const [f, b] of pares) {
      const r = razao(f, b);
      if (!pior || r < pior.razao) { pior = { razao: Math.round(r * 100) / 100, frente: hex(f), fundo: hex(b) }; }
    }
    pior.opacidade = Math.round(opacidades.reduce((a, x) => a * x.o, 1) * 1000) / 1000;
    pior.filtro = filtro;
    return pior;
  }
  function raizDe(raiz) {
    if (!raiz) { return document.body; }
    if (typeof raiz === "string") { return document.querySelector(raiz); }
    return raiz;
  }
  function textos(raiz) {
    const base = raizDe(raiz);
    if (!base) { return []; }
    const saida = [];
    const todos = [base].concat(Array.from(base.querySelectorAll("*")));
    for (const el of todos) {
      if (["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE"].includes(el.tagName) || el.closest("svg")) { continue; }
      const temTexto = Array.from(el.childNodes).some((n) => n.nodeType === 3 && n.textContent.trim().length > 0);
      const campo = (el.tagName === "INPUT" && !["hidden", "checkbox", "radio"].includes(el.type) && (el.value || el.placeholder)) || el.tagName === "SELECT" || el.tagName === "TEXTAREA";
      if ((!temTexto && !campo) || !visivel(el)) { continue; }
      const s = getComputedStyle(el);
      const tam = parseFloat(s.fontSize);
      const peso = parseInt(s.fontWeight, 10) || 400;
      const grande = tam >= 24 || (tam >= 18.66 && peso >= 700);
      const c = contraste(el, s.color);
      const r = el.getBoundingClientRect();
      const texto = campo && !temTexto ? String(el.value || el.placeholder || "") : Array.from(el.childNodes).filter((n) => n.nodeType === 3).map((n) => n.textContent.trim()).join(" ");
      saida.push(Object.assign({
        seletor: seletor(el), texto: texto.replace(/\s+/g, " ").slice(0, 60), tamanho_px: tam, peso, grande, minimo: grande ? 3 : 4.5,
        acima_dobra: r.top < window.innerHeight && r.bottom > 0,
      }, c));
    }
    return saida;
  }
  function icones(raiz) {
    const base = raizDe(raiz);
    if (!base) { return []; }
    const saida = [];
    for (const svg of Array.from(base.querySelectorAll("svg"))) {
      if (!svg.querySelector("use, path, circle, rect") || svg.getAttribute("width") === "0" || !visivel(svg)) { continue; }
      const controle = !!svg.closest("button, a[href], summary, [role=button], label");
      const s = getComputedStyle(svg);
      saida.push(Object.assign({ seletor: seletor(svg.parentElement) + " > svg", controle }, contraste(svg, s.color)));
    }
    return saida;
  }
  function alvos(raiz) {
    const base = raizDe(raiz) || document.body;
    const lista = Array.from(base.querySelectorAll("button, a[href], summary, input:not([type=hidden]), select, textarea, [role=button], [tabindex]:not([tabindex='-1'])"));
    return lista.filter(visivel).map((el) => {
      const r = el.getBoundingClientRect();
      return { seletor: seletor(el), rotulo: (el.getAttribute("aria-label") || el.textContent || el.value || "").trim().replace(/\s+/g, " ").slice(0, 40),
        largura: Math.round(r.width * 10) / 10, altura: Math.round(r.height * 10) / 10 };
    });
  }
  function retangulo(alvo) {
    const el = typeof alvo === "string" ? document.querySelector(alvo) : alvo;
    if (!el || !visivel(el)) { return null; }
    const r = el.getBoundingClientRect();
    return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, width: r.width, height: r.height };
  }
  /** Altura (px) da faixa em que o retângulo do elemento fica sob a barra de voz. */
  function sobreposicao(alvo) {
    const barra = document.getElementById("barra-voz");
    const a = retangulo(alvo);
    const b = barra ? retangulo(barra) : null;
    if (!a || !b) { return 0; }
    const el = typeof alvo === "string" ? document.querySelector(alvo) : alvo;
    if (barra.contains(el) || el.contains(barra)) { return 0; }
    const horizontal = a.left < b.right && b.left < a.right;
    if (!horizontal) { return 0; }
    return Math.max(0, Math.round(Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top)));
  }
  window.__uxMedidas = { parse, lum, razao, visivel, seletor, contraste, textos, icones, alvos, retangulo, sobreposicao };
}

/** Garante a biblioteca na página (idempotente). */
async function instalar(page) {
  await page.evaluate(`(${instalarBiblioteca.toString()})()`);
}

/**
 * Contraste de todo texto visível sob `raiz` (seletor CSS, ou o body).
 * @returns {Promise<Array<{seletor:string, texto:string, tamanho_px:number, razao:number, acima_dobra:boolean}>>}
 */
async function medirContraste(page, raiz) {
  await instalar(page);
  return page.evaluate((r) => window.__uxMedidas.textos(r), raiz || null);
}

/** Contraste dos ícones (SVG) visíveis; `controle` = dentro de botão, link, summary ou rótulo. */
async function medirIcones(page, raiz) {
  await instalar(page);
  return page.evaluate((r) => window.__uxMedidas.icones(r), raiz || null);
}

/**
 * Sobreposição vertical (px) de cada seletor com #barra-voz, na rolagem atual.
 * @returns {Promise<Array<{seletor:string, sobreposicao_px:number}>>}
 */
async function medirOclusao(page, seletores) {
  await instalar(page);
  return page.evaluate((lista) => lista.map((s) => ({ seletor: s, sobreposicao_px: window.__uxMedidas.sobreposicao(s) })), seletores);
}

/** Alvos interativos visíveis com largura e altura renderizadas. */
async function medirAlvos(page, raiz) {
  await instalar(page);
  return page.evaluate((r) => window.__uxMedidas.alvos(r), raiz || null);
}

/**
 * Aperta Tab `tabs` vezes a partir do topo e conta os focos (fora da própria barra) cobertos pela #barra-voz.
 * @returns {Promise<{tabs:number, obscurecidos:number, focos:Array}>}
 */
async function medirFocoObscurecido(page, tabs) {
  await instalar(page);
  await page.evaluate(() => { window.scrollTo(0, 0); if (document.activeElement && document.activeElement.blur) { document.activeElement.blur(); } });
  const focos = [];
  for (let i = 0; i < (tabs || 25); i += 1) {
    await page.keyboard.press("Tab");
    await page.waitForTimeout(30);
    focos.push(await page.evaluate(() => {
      const el = document.activeElement;
      if (!el || el === document.body || el === document.documentElement) { return { seletor: "body", sobreposicao_px: 0 }; }
      return { seletor: window.__uxMedidas.seletor(el), sobreposicao_px: window.__uxMedidas.sobreposicao(el) };
    }));
  }
  return { tabs: focos.length, obscurecidos: focos.filter((f) => f.sobreposicao_px > 0).length, focos };
}

/** Retrato do estado: semáforo, lâmpadas, cartão, ações, Feito, barra, fontes e rolagem lateral. */
async function coletarEstado(page) {
  await instalar(page);
  return page.evaluate(() => {
    const U = window.__uxMedidas;
    const $ = (id) => document.getElementById(id);
    const texto = (id) => ($(id) ? $(id).textContent.replace(/\s+/g, " ").trim() : null);
    const semaforo = $("semaforo");
    const estiloSemaforo = semaforo ? getComputedStyle(semaforo) : null;
    const caixaSemaforo = semaforo ? semaforo.getBoundingClientRect() : { x: 0, y: 0 };
    const lampadas = Array.from(document.querySelectorAll(".lampada")).map((l) => {
      const r = l.getBoundingClientRect();
      const s = getComputedStyle(l);
      const cor = (String(l.className).match(/lampada--(\w+)/) || [])[1] || "";
      return { cor, x: r.x - caixaSemaforo.x, y: r.y - caixaSemaforo.y, w: r.width, h: r.height, cx: r.x + r.width / 2, cy: r.y + r.height / 2,
        diametro: Math.round(Math.min(r.width, r.height) * 10) / 10, sombra: s.boxShadow, filtro: s.filter, opacidade: s.opacity,
        acesa_classe: !!l.closest(".acesa") || l.classList.contains("acesa") };
    });
    const xs = lampadas.map((l) => l.cx), ys = lampadas.map((l) => l.cy);
    const orientacao = lampadas.length ? ((Math.max(...ys) - Math.min(...ys)) >= (Math.max(...xs) - Math.min(...xs)) ? "vertical" : "horizontal") : null;
    const acoes = Array.from(document.querySelectorAll("#acoes > li, #acoes .acao")).filter((li, i, todos) => todos.indexOf(li) === i && li.getAttribute("data-acao"))
      .map((li) => ({ codigo: li.getAttribute("data-acao"), antiga: li.getAttribute("data-antiga") === "true", icones: Array.from(li.querySelectorAll("use")).map((u) => u.getAttribute("href") || u.getAttribute("xlink:href")),
        visivel: U.visivel(li), legenda: li.textContent.replace(/\s+/g, " ").trim().slice(0, 60) }));
    const causa = $("causa");
    const causaUso = causa ? causa.querySelector("use") : null;
    const chipUso = $("chip-icone") || (document.querySelector(".chip-estado use"));
    const feito = $("feito");
    const fontes = U.textos(null).map((t) => t.tamanho_px);
    return {
      estado: document.body.getAttribute("data-estado"), confianca: document.body.getAttribute("data-confianca"), classes: document.body.className,
      aria_semaforo: semaforo ? semaforo.getAttribute("aria-label") : null,
      borda_semaforo: estiloSemaforo ? [estiloSemaforo.borderTopStyle, estiloSemaforo.outlineStyle].join(" ") : "",
      lampadas, orientacao,
      titulo: texto("titulo"), chip: texto("chip-texto") || (document.querySelector(".chip-estado") ? document.querySelector(".chip-estado").textContent.replace(/\s+/g, " ").trim() : null),
      chip_icone: chipUso ? chipUso.getAttribute("href") : null,
      aviso: $("aviso-teste") && U.visivel($("aviso-teste")) ? $("aviso-teste").textContent.replace(/\s+/g, " ").trim() : "",
      mensagem: texto("mensagem"), causa_icone: causaUso ? causaUso.getAttribute("href") : null, causa_visivel: causa ? U.visivel(causa) : false,
      acoes, feito_visivel: feito ? U.visivel(feito) : false, feito_estado: feito ? feito.getAttribute("data-estado-feito") : null,
      tecnico_visivel: $("tecnico") ? U.visivel($("tecnico")) : false,
      barra_fixa: $("barra-voz") ? ["fixed", "sticky"].includes(getComputedStyle($("barra-voz")).position) : false,
      texto_cartao: (document.querySelector(".cartao-estado") || document.querySelector("main") || document.body).innerText.replace(/\s+/g, " ").slice(0, 600),
      menor_fonte_px: fontes.length ? Math.min(...fontes) : null,
      scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth,
      altura_documento: document.documentElement.scrollHeight, viewport: [innerWidth, innerHeight],
    };
  });
}

/**
 * Amplia TODO texto pelo `fator` (como o tamanho de fonte do Android) e mede rolagem lateral e colisões no topo.
 * Mesmo método da auditoria: tamanho computado × fator, fixado em cada elemento.
 */
async function medirTextoAmpliado(page, fator) {
  await instalar(page);
  return page.evaluate((f) => {
    const els = Array.from(document.querySelectorAll("body *")).filter((e) => !e.closest("svg"));
    const tamanhos = els.map((e) => parseFloat(getComputedStyle(e).fontSize));
    els.forEach((e, i) => { e.style.fontSize = (tamanhos[i] * f) + "px"; });
    const topo = document.querySelector("header, .topo, .cabecalho");
    const colisoes = [];
    if (topo) {
      const folhas = Array.from(topo.querySelectorAll("*")).filter((e) => window.__uxMedidas.visivel(e) && !e.closest("svg")
        && (Array.from(e.childNodes).some((n) => n.nodeType === 3 && n.textContent.trim()) || e.matches("button, a, svg.icone")));
      for (let i = 0; i < folhas.length; i += 1) {
        for (let j = i + 1; j < folhas.length; j += 1) {
          const a = folhas[i], b = folhas[j];
          if (a.contains(b) || b.contains(a)) { continue; }
          const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
          const w = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
          const h = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
          if (w > 1 && h > 1) { colisoes.push(window.__uxMedidas.seletor(a) + " x " + window.__uxMedidas.seletor(b)); }
        }
      }
    }
    return { fator: f, scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth,
      rolagem_lateral: document.documentElement.scrollWidth > document.documentElement.clientWidth, colisoes_topo: colisoes };
  }, fator);
}

/** Conta `font-size: <n>px` num texto de CSS (folha, <style> ou atributo style). */
function analisarCss(texto) {
  const ocorrencias = (String(texto || "").match(/font-size\s*:\s*[\d.]+px/g) || []);
  return { font_size_px: ocorrencias.length, ocorrencias: ocorrencias.slice(0, 20) };
}

/**
 * Script para addInitScript: voz falsa (registra texto, rate e instante), vibração e bipes contados.
 * `lingua` = idioma da única voz instalada ("pt-BR" ou, para "sem voz em português", "en-US").
 */
function scriptVozFalsa(lingua) {
  return `(function () {
    window.__falas = []; window.__vibracoes = 0; window.__bipes = 0;
    var voz = { lang: ${JSON.stringify(lingua || "pt-BR")}, name: "Voz falsa", default: true, localService: true, voiceURI: "falsa" };
    var sintese = { speaking: false, pending: false, paused: false, onvoiceschanged: null,
      getVoices: function () { return [voz]; },
      addEventListener: function () {}, removeEventListener: function () {},
      speak: function (u) {
        window.__falas.push({ texto: String(u.text || ""), rate: typeof u.rate === "number" ? u.rate : 1, t: Date.now(), p: performance.now() });
        var eu = this; eu.speaking = true;
        setTimeout(function () { if (u.onstart) { u.onstart({}); } setTimeout(function () { eu.speaking = false; if (u.onend) { u.onend({}); } }, 40); }, 5);
      },
      cancel: function () { this.speaking = false; }, pause: function () {}, resume: function () {} };
    try { Object.defineProperty(window, "speechSynthesis", { value: sintese, configurable: true }); } catch (e) {}
    window.SpeechSynthesisUtterance = function (t) { this.text = t; this.lang = ""; this.voice = null; this.rate = 1; this.pitch = 1; this.volume = 1; };
    try { navigator.vibrate = function () { window.__vibracoes += 1; return true; }; } catch (e) {}
    var Original = window.AudioContext || window.webkitAudioContext;
    if (Original) {
      var criar = Original.prototype.createOscillator;
      Original.prototype.createOscillator = function () {
        var o = criar.call(this); var iniciar = o.start;
        o.start = function () { window.__bipes += 1; return iniciar.apply(o, arguments); };
        return o;
      };
    }
  })();`;
}

module.exports = {
  instalarBiblioteca, instalar, medirContraste, medirIcones, medirOclusao, medirAlvos, medirFocoObscurecido,
  coletarEstado, medirTextoAmpliado, analisarCss, scriptVozFalsa,
};
