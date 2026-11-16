/*
 * Regiões vivas, retorno falado e retorno do "Feito" (suíte de validação UX v4), só com Playwright.
 *
 * O relógio da página é controlado com page.clock (10 ou 30 minutos simulados
 * em segundos de verdade); as mudanças no DOM das regiões vivas
 * ([aria-live] ≠ off, role=status/alert/log/timer, <output>) são contadas com
 * MutationObserver: "registros" = MutationRecord (texto, nós e atributos) e
 * "lotes" = chamadas do observador (o que um leitor de tela pode anunciar).
 *
 * Exporta:
 *   medirMutacoesParado(nav, base, opcoes)   -> {estado, minutos, registros, lotes, por_regiao}
 *   medirLotes30s(nav, base, opcoes)         -> {estado, url, alvo, segundos, lotes, registros, regioes}
 *   medirAlarmes(nav, base, estado, minutos) -> {estado, minutos, falas_automaticas, vibracoes, bipes}
 *   medirRetornoFalado(nav, base)            -> {avisos, dialogos, eventos}
 *   medirRepeticaoVoz(nav, base)             -> {rates, rate_repeticao, rate_preferencia_lembrada}
 *   medirFeito(nav, base)                    -> {registros_3_toques, estado_14s, estado_16s, desfazer_15s}
 *   medirDemonstracaoNoResumo(base)          -> {antes, depois, delta}
 */
"use strict";

const { scriptVozFalsa } = require("./medidas.js");

const SELETOR_VIVO = "[aria-live]:not([aria-live='off']), [role=status], [role=alert], [role=log], [role=timer], output";
const esperar = (ms) => new Promise((resolver) => setTimeout(resolver, ms));

/** POST JSON direto do Node. */
async function post(base, rota, corpo, cabecalhos) {
  const resposta = await fetch(base + rota, { method: "POST", headers: Object.assign({ "Content-Type": "application/json" }, cabecalhos || {}), body: JSON.stringify(corpo || {}) });
  let dados = null;
  try { dados = await resposta.json(); } catch (erro) { dados = null; }
  return { status: resposta.status, dados };
}

/** GET JSON direto do Node. */
async function get(base, rota) {
  const resposta = await fetch(base + rota);
  let dados = null;
  try { dados = await resposta.json(); } catch (erro) { dados = null; }
  return { status: resposta.status, dados };
}

/** Novo contexto de celular (360×740) com a voz falsa. */
async function contextoCelular(nav, lingua) {
  const contexto = await nav.newContext({ viewport: { width: 360, height: 740 }, deviceScaleFactor: 1, isMobile: true, hasTouch: true, locale: "pt-BR" });
  await contexto.addInitScript(scriptVozFalsa(lingua || "pt-BR"));
  return contexto;
}

/** Avança o relógio da página em passos, dando tempo real para as consultas à API terminarem. */
async function avancar(page, milissegundos, passo) {
  const tamanho = passo || 1000;
  for (let feito = 0; feito < milissegundos; feito += tamanho) {
    await page.clock.runFor(Math.min(tamanho, milissegundos - feito));
    await page.waitForTimeout(20);
  }
}

/** Espera a primeira pintura útil (corpo sem "carregando" e com data-estado). */
async function esperarPronto(page, limite) {
  await page.waitForFunction(() => document.body && document.body.getAttribute("data-estado") && !document.body.classList.contains("carregando"), null, { timeout: limite || 8000 }).catch(() => null);
}

/** Liga um MutationObserver em cada região viva (filtrada por `filtro`, se houver) e acumula em window.__vivas. */
async function observarRegioes(page, filtro) {
  return page.evaluate(({ seletor, filtro }) => {
    window.__vivas = { registros: 0, lotes: 0, por_regiao: {} };
    const alvoFiltro = filtro ? document.querySelector(filtro) : null;
    if (filtro && !alvoFiltro) { return []; }
    const regioes = Array.from(document.querySelectorAll(seletor)).filter((r) => !alvoFiltro || r.contains(alvoFiltro) || alvoFiltro.contains(r));
    // Só as regiões mais externas (uma região dentro de outra é anunciada pela de fora).
    const externas = regioes.filter((r) => !regioes.some((o) => o !== r && o.contains(r)));
    externas.forEach((regiao) => {
      const nome = regiao.id ? "#" + regiao.id : "." + String(regiao.className || regiao.tagName).split(" ")[0];
      window.__vivas.por_regiao[nome] = 0;
      new MutationObserver((lista) => {
        window.__vivas.lotes += 1; window.__vivas.registros += lista.length; window.__vivas.por_regiao[nome] += lista.length;
      }).observe(regiao, { subtree: true, childList: true, characterData: true, attributes: true });
    });
    return Object.keys(window.__vivas.por_regiao);
  }, { seletor: SELETOR_VIVO, filtro: filtro || null });
}

/** Mutações nas regiões vivas com o estado PARADO durante `minutos` (padrão 10, estado Verde). */
async function medirMutacoesParado(nav, base, opcoes) {
  const { minutos = 10, estado = "verde" } = opcoes || {};
  await post(base, "/__ux/estado/" + estado);
  const contexto = await contextoCelular(nav);
  try {
    const page = await contexto.newPage();
    await page.clock.install();
    await page.goto(base + "/");
    await avancar(page, 1000, 250);
    await esperarPronto(page, 4000);
    await avancar(page, 6000);
    const regioes = await observarRegioes(page, null);
    await avancar(page, minutos * 60 * 1000, 5000);
    const vivas = await page.evaluate(() => window.__vivas);
    return Object.assign({ estado, minutos, regioes }, vivas);
  } finally { await contexto.close(); }
}

/** Lotes de mutação em 30 s numa região (cartão do estado ou faixa da demonstração). */
async function medirLotes30s(nav, base, opcoes) {
  const { estado = "verde", url = "/", alvo = null, segundos = 30, excluir = [] } = opcoes || {};
  await post(base, "/__ux/estado/" + estado);
  const contexto = await contextoCelular(nav);
  try {
    const page = await contexto.newPage();
    await page.clock.install();
    await page.goto(base + url);
    await avancar(page, 1000, 250);
    await esperarPronto(page, 4000);
    await avancar(page, 2000);
    let regioes;
    if (alvo) {
      regioes = await observarRegioes(page, alvo);
    } else {
      regioes = await page.evaluate(({ seletor, excluir }) => {
        window.__vivas = { registros: 0, lotes: 0, por_regiao: {} };
        const todas = Array.from(document.querySelectorAll(seletor)).filter((r) => !excluir.some((s) => r.matches(s) || r.closest(s)));
        const externas = todas.filter((r) => !todas.some((o) => o !== r && o.contains(r)));
        externas.forEach((regiao) => {
          const nome = regiao.id ? "#" + regiao.id : "." + String(regiao.className || regiao.tagName).split(" ")[0];
          window.__vivas.por_regiao[nome] = 0;
          new MutationObserver((lista) => { window.__vivas.lotes += 1; window.__vivas.registros += lista.length; window.__vivas.por_regiao[nome] += lista.length; })
            .observe(regiao, { subtree: true, childList: true, characterData: true, attributes: true });
        });
        return Object.keys(window.__vivas.por_regiao);
      }, { seletor: SELETOR_VIVO, excluir });
    }
    await avancar(page, segundos * 1000, 1000);
    const vivas = await page.evaluate(() => window.__vivas);
    return { estado, url, alvo, segundos, regioes, lotes: vivas.lotes, registros: vivas.registros, por_regiao: vivas.por_regiao };
  } finally { await contexto.close(); }
}

/** Alarmes (falas automáticas), vibrações e bipes em `minutos` com a voz ligada por um toque. */
async function medirAlarmes(nav, base, estado, minutos) {
  await post(base, "/__ux/estado/" + estado);
  const contexto = await contextoCelular(nav);
  try {
    const page = await contexto.newPage();
    await page.clock.install();
    await page.goto(base + "/");
    await avancar(page, 1000, 250);
    await esperarPronto(page, 4000);
    const botao = page.locator("#botao-voz");
    if (await botao.isVisible().catch(() => false)) { await botao.click(); }
    await avancar(page, 3000, 500);
    const inicio = await page.evaluate(() => ({ falas: window.__falas.length, vibracoes: window.__vibracoes, bipes: window.__bipes }));
    await avancar(page, (minutos || 30) * 60 * 1000, 5000);
    const fim = await page.evaluate(() => ({ falas: window.__falas.length, vibracoes: window.__vibracoes, bipes: window.__bipes }));
    return { estado, minutos: minutos || 30, falas_automaticas: fim.falas - inicio.falas, vibracoes: fim.vibracoes, bipes: fim.bipes - inicio.bipes };
  } finally { await contexto.close(); }
}

/** Script de página que registra avisos (toast, alertas, faixas) e diálogos que aparecem, com o instante. */
const OBSERVADOR_AVISOS = `(function () {
  window.__avisos = [];
  var vistos = new WeakMap();
  function visivel(e) { if (!e || e.hidden) return false; var s = getComputedStyle(e); if (s.display === "none" || s.visibility === "hidden" || parseFloat(s.opacity) < 0.05) return false; var r = e.getBoundingClientRect(); return r.width > 1 && r.height > 1; }
  function texto(e) { return (e.textContent || "").replace(/\\s+/g, " ").trim(); }
  function examinar() {
    var candidatos = Array.prototype.slice.call(document.querySelectorAll("#toast, [role=alert], #faixa-troca, .aviso-voz, dialog, #dialogo, [role=dialog], [role=alertdialog]"));
    candidatos.forEach(function (e) {
      var dialogo = e.matches("dialog, #dialogo, [role=dialog], [role=alertdialog]");
      var ativo = dialogo ? (visivel(e) && (e.tagName !== "DIALOG" || e.open)) : (visivel(e) && texto(e).length > 0);
      var chave = ativo ? (dialogo ? "d:" : "a:") + texto(e).slice(0, 80) : "";
      if (ativo && vistos.get(e) !== chave) {
        window.__avisos.push({ tipo: dialogo ? "dialogo" : "aviso", alvo: e.id ? "#" + e.id : "." + String(e.className).split(" ")[0], texto: texto(e).slice(0, 120),
          silencioso: !!e.closest("[data-silencioso]") , p: performance.now() });
      }
      vistos.set(e, chave);
    });
  }
  document.addEventListener("DOMContentLoaded", function () {
    new MutationObserver(examinar).observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ["hidden", "class", "open", "style"] });
    examinar();
  });
})();`;

/** Toca no seletor se estiver visível (devolve true se tocou). */
async function tocarSeVisivel(page, seletor) {
  const alvo = page.locator(seletor).first();
  if (await alvo.isVisible().catch(() => false)) { await alvo.click({ timeout: 3000 }).catch(() => null); return true; }
  return false;
}

/** Junta avisos e falas de uma página: falado = alguma fala entre 1 s antes e 2,5 s depois do aviso. */
async function avisosFalados(page) {
  return page.evaluate(() => (window.__avisos || []).map((a) => Object.assign({}, a, {
    falado: (window.__falas || []).some((f) => f.p >= a.p - 1000 && f.p <= a.p + 2500),
  })));
}

/**
 * Retorno falado na 1ª visita: percorre Feito, técnico, ajuda, queda de rede, banca (cenário e cultura)
 * e troca de cultura por outro aparelho, e verifica se cada aviso e diálogo foi falado.
 */
async function medirRetornoFalado(nav, base) {
  const eventos = [];
  await post(base, "/__ux/estado/vermelho");
  let contexto = await contextoCelular(nav);
  try {
    await contexto.addInitScript(OBSERVADOR_AVISOS);
    const page = await contexto.newPage();
    await page.goto(base + "/");
    await esperarPronto(page);
    await page.waitForTimeout(600);
    if (await tocarSeVisivel(page, "#feito")) { await page.waitForTimeout(1500); }
    if (await tocarSeVisivel(page, "#tecnico")) {
      await page.waitForTimeout(1200);
      await tocarSeVisivel(page, "#dialogo-nao");
      await page.waitForTimeout(800);
    }
    await contexto.setOffline(true);
    await page.waitForFunction(() => /sem-ligacao|desatualizada|offline/.test(document.body.className + " " + (document.body.getAttribute("data-confianca") || "")), null, { timeout: 15000 }).catch(() => null);
    await page.waitForTimeout(1500);
    await contexto.setOffline(false);
    await page.waitForTimeout(6500);
    await post(base, "/__ux/cultura/cafe_conilon");
    await page.waitForTimeout(7000);
    eventos.push(...(await avisosFalados(page)).map((e) => Object.assign(e, { pagina: "/" })));
  } finally { await contexto.close(); }
  await post(base, "/__ux/estado/verde");
  contexto = await contextoCelular(nav);
  try {
    await contexto.addInitScript(OBSERVADOR_AVISOS);
    const page = await contexto.newPage();
    await page.goto(base + "/?banca=1");
    await esperarPronto(page);
    await page.waitForTimeout(600);
    if (await tocarSeVisivel(page, ".botao-cenario")) { await page.waitForTimeout(1200); await tocarSeVisivel(page, "#dialogo-nao"); await page.waitForTimeout(800); }
    if (await tocarSeVisivel(page, ".botao-cultura[aria-pressed='false']")) { await page.waitForTimeout(1200); await tocarSeVisivel(page, "#dialogo-nao"); await page.waitForTimeout(800); }
    eventos.push(...(await avisosFalados(page)).map((e) => Object.assign(e, { pagina: "/?banca=1" })));
  } finally { await contexto.close(); }
  const resumir = (tipo) => {
    const lista = eventos.filter((e) => e.tipo === tipo && !e.silencioso);
    const falados = lista.filter((e) => e.falado).length;
    return { total: lista.length, falados, pct: lista.length ? Math.round(falados / lista.length * 1000) / 10 : 100,
      silenciosos: eventos.filter((e) => e.tipo === tipo && e.silencioso).length };
  };
  return { avisos: resumir("aviso"), dialogos: resumir("dialogo"), eventos };
}

/** Repetição da fala (#repetir, ou o 3º toque seguido no botão de voz) mais lenta, e a preferência lembrada ao voltar. */
async function medirRepeticaoVoz(nav, base) {
  await post(base, "/__ux/estado/vermelho");
  const contexto = await contextoCelular(nav);
  try {
    const page = await contexto.newPage();
    await page.goto(base + "/");
    await esperarPronto(page);
    await page.waitForTimeout(500);
    await page.click("#botao-voz");
    await page.waitForTimeout(700);
    // Repetir: #repetir, se existir; senão o mesmo botão mais 2 vezes (curta → completa → curta devagar).
    const temRepetir = await page.locator("#repetir").isVisible().catch(() => false);
    for (let i = 0; i < (temRepetir ? 1 : 2); i += 1) {
      await page.click(temRepetir ? "#repetir" : "#botao-voz");
      await page.waitForTimeout(700);
    }
    const primeiras = await page.evaluate(() => window.__falas.map((f) => f.rate));
    await page.reload();
    await esperarPronto(page);
    await page.waitForTimeout(500);
    await page.click("#botao-voz");
    await page.waitForTimeout(700);
    const depois = await page.evaluate(() => window.__falas.map((f) => f.rate));
    const rateRepeticao = primeiras.length >= 2 ? primeiras[primeiras.length - 1] : null;
    return { rates: primeiras, rates_depois: depois, rate_primeira: primeiras.length ? primeiras[0] : null, rate_repeticao: rateRepeticao,
      rate_preferencia_lembrada: depois.length ? depois[0] : null };
  } finally { await contexto.close(); }
}

/** Total de toques em "Feito" no resumo do técnico (sem PIN configurado). */
async function totalFeitos(base) {
  const r = await get(base, "/api/tecnico/resumo?dias=1");
  return r.dados && r.dados.acoes_feitas ? Number(r.dados.acoes_feitas.total || 0) : null;
}

/** 3 toques seguidos em Feito → registros no servidor; e a janela de desfazer na tela (14 s e 16 s). */
async function medirFeito(nav, base) {
  await post(base, "/__ux/estado/vermelho");
  const resultado = {};
  let contexto = await contextoCelular(nav);
  try {
    const page = await contexto.newPage();
    await page.goto(base + "/");
    await esperarPronto(page);
    await page.waitForTimeout(500);
    const antes = await totalFeitos(base);
    const visivel = await page.locator("#feito").isVisible().catch(() => false);
    if (visivel) {
      for (let i = 0; i < 3; i += 1) { await page.locator("#feito").click({ force: true, noWaitAfter: true, timeout: 2000 }).catch(() => null); await page.waitForTimeout(150); }
    }
    await page.waitForTimeout(2500);
    const depois = await totalFeitos(base);
    resultado.feito_visivel = visivel;
    resultado.registros_3_toques = antes === null || depois === null ? null : depois - antes;
  } finally { await contexto.close(); }
  await post(base, "/__ux/estado/vermelho");
  contexto = await contextoCelular(nav);
  try {
    const page = await contexto.newPage();
    await page.clock.install();
    await page.goto(base + "/");
    await avancar(page, 1000, 250);
    await esperarPronto(page, 4000);
    await avancar(page, 500, 250);
    if (await page.locator("#feito").isVisible().catch(() => false)) {
      await page.locator("#feito").click();
      await avancar(page, 14000, 1000);
      resultado.estado_14s = await page.evaluate(() => { const b = document.getElementById("feito"); return b && !b.hidden ? b.getAttribute("data-estado-feito") : null; });
      await avancar(page, 2000, 1000);
      resultado.estado_16s = await page.evaluate(() => { const b = document.getElementById("feito"); return b && !b.hidden ? b.getAttribute("data-estado-feito") : null; });
    }
    resultado.desfazer_15s = resultado.estado_14s === "desfazer" && resultado.estado_16s !== "desfazer";
  } finally { await contexto.close(); }
  return resultado;
}

/** Pedido de ajuda, evento e Feito durante a demonstração NÃO podem entrar no resumo do técnico. */
async function medirDemonstracaoNoResumo(base) {
  await post(base, "/__ux/estado/demonstracao_GEADA");
  const contar = async () => {
    const r = await get(base, "/api/tecnico/resumo?dias=1");
    const d = r.dados || {};
    const eventos = d.eventos_interface ? Object.values(d.eventos_interface).reduce((a, v) => a + Number(v || 0), 0) : 0;
    return { pedidos: Number((d.pedidos_ajuda || {}).total || 0), eventos, feitos: Number((d.acoes_feitas || {}).total || 0) };
  };
  const antes = await contar();
  const status = (await get(base, "/api/status")).dados || {};
  const respostas = [
    (await post(base, "/api/pedido_ajuda", {})).status,
    (await post(base, "/api/evento", { tipo: "feito" })).status,
    (await post(base, "/api/acao_feita", { codigo_acao: (status.acoes || ["COBRIR_MUDAS"])[0], causa: status.causa || "FRIO_GEADA", id_leitura: status.id_leitura || null })).status,
  ];
  const depois = await contar();
  const delta = (depois.pedidos - antes.pedidos) + (depois.eventos - antes.eventos) + (depois.feitos - antes.feitos);
  return { antes, depois, respostas, delta };
}

module.exports = {
  SELETOR_VIVO, medirMutacoesParado, medirLotes30s, medirAlarmes, medirRetornoFalado, medirRepeticaoVoz, medirFeito,
  medirDemonstracaoNoResumo, avancar, esperarPronto, post, get,
};
