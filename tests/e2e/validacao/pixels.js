/*
 * Pixels das lâmpadas do semáforo (suíte de validação UX v4), só com Playwright.
 *
 * Mede, num recorte de #semaforo, a luminância relativa média (WCAG) do disco
 * central de cada lâmpada (raio < 0,42 do raio da lâmpada, como na auditoria
 * scripts/analisar_lampadas.py) e a razão da lâmpada ACESA (a cor de
 * body[data-estado]) pela MAIOR apagada. Opções:
 *   - ofuscamento: luminância somada às duas (luz do sol refletida na tela,
 *     "veiling glare"); +0,10 é o caso da meta da v4;
 *   - filtro: simulação de daltonismo por matriz de cor sobre <html>
 *     (protanopia, deuteranopia e tritanopia de Machado et al., 2009, com
 *     severidade 1; acromatopsia = luminância Rec. 709).
 * Cores forçadas e escurecimento automático vêm do contexto/navegador de
 * quem chama (forcedColors: "active"; --enable-features=WebContentsForceDark).
 *
 * Exporta:
 *   razaoLampadas(page, {ofuscamento?, filtro?}) -> Promise<{acesa, maior_apagada, razao, cor_acesa,
 *                                                  luminancias, lampadas_acesas}>
 *   aplicarFiltro(page, filtro|null), recorteSemaforo(page) -> Promise<Buffer>,
 *   diferencaPixels(page, bufferA, bufferB) -> Promise<number>  (diferença média de luminância, 0..1)
 *   MATRIZES_DALTONISMO
 */
"use strict";

const MATRIZES_DALTONISMO = {
  protanopia: "0.152286 1.052583 -0.204868 0 0 0.114503 0.786281 0.099216 0 0 -0.003882 -0.048116 1.051998 0 0 0 0 0 1 0",
  deuteranopia: "0.367322 0.860646 -0.227968 0 0 0.280085 0.672501 0.047413 0 0 -0.011820 0.042940 0.968881 0 0 0 0 0 1 0",
  tritanopia: "1.255528 -0.076749 -0.178779 0 0 -0.078411 0.930809 0.147602 0 0 0.004733 0.691367 0.303900 0 0 0 0 0 1 0",
  acromatopsia: "0.2126 0.7152 0.0722 0 0 0.2126 0.7152 0.0722 0 0 0.2126 0.7152 0.0722 0 0 0 0 0 1 0",
};
const CORES = ["vermelho", "amarelo", "verde"];
// Uma lâmpada conta como acesa por pixel quando brilha ≥ 2× (razão WCAG) a mais escura.
const LIMIAR_ACESA_POR_PIXEL = 2.0;

/** Liga (ou desliga, com null) a simulação de daltonismo na página inteira. */
async function aplicarFiltro(page, filtro) {
  if (filtro && !MATRIZES_DALTONISMO[filtro]) { throw new Error("Filtro desconhecido: " + filtro); }
  await page.evaluate(({ filtro, matriz }) => {
    const antigoSvg = document.getElementById("ux-daltonismo-svg");
    if (antigoSvg) { antigoSvg.remove(); }
    const antigoEstilo = document.getElementById("ux-daltonismo-estilo");
    if (antigoEstilo) { antigoEstilo.remove(); }
    if (!filtro) { return; }
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.id = "ux-daltonismo-svg";
    svg.setAttribute("width", "0"); svg.setAttribute("height", "0"); svg.setAttribute("aria-hidden", "true");
    svg.style.position = "absolute";
    svg.innerHTML = '<filter id="ux-daltonismo" color-interpolation-filters="linearRGB"><feColorMatrix type="matrix" values="' + matriz + '"/></filter>';
    document.body.appendChild(svg);
    const estilo = document.createElement("style");
    estilo.id = "ux-daltonismo-estilo";
    estilo.textContent = "html { filter: url(#ux-daltonismo); }";
    document.head.appendChild(estilo);
  }, { filtro: filtro || null, matriz: filtro ? MATRIZES_DALTONISMO[filtro] : "" });
  await page.waitForTimeout(120);
}

/** Captura PNG só do #semaforo (animações paradas no quadro inicial). */
async function recorteSemaforo(page) {
  return page.locator("#semaforo").screenshot({ animations: "disabled" });
}

/** Página em branco do mesmo contexto, usada para decodificar PNGs num canvas (reaproveitada). */
async function paginaAnalise(page) {
  const contexto = page.context();
  if (!contexto.__uxPaginaAnalise || contexto.__uxPaginaAnalise.isClosed()) {
    contexto.__uxPaginaAnalise = await contexto.newPage();
    await contexto.__uxPaginaAnalise.setContent("<!doctype html><title>análise</title>");
  }
  return contexto.__uxPaginaAnalise;
}

/** Luminância média do disco central de cada lâmpada no PNG (caixas em px CSS, relativas ao #semaforo). */
async function luminanciasNoRecorte(page, png, caixas, larguraCss) {
  const analise = await paginaAnalise(page);
  return analise.evaluate(async ({ base64, caixas, larguraCss }) => {
    const imagem = new Image();
    imagem.src = "data:image/png;base64," + base64;
    await imagem.decode();
    const tela = document.createElement("canvas");
    tela.width = imagem.width; tela.height = imagem.height;
    const ctx = tela.getContext("2d");
    ctx.drawImage(imagem, 0, 0);
    const dados = ctx.getImageData(0, 0, tela.width, tela.height).data;
    const escala = imagem.width / larguraCss;
    const lin = (v) => { const x = v / 255; return x <= 0.04045 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); };
    const saida = {};
    for (const c of caixas) {
      const cx = (c.x + c.w / 2) * escala, cy = (c.y + c.h / 2) * escala;
      const raio = Math.min(c.w, c.h) / 2 * escala * 0.42;
      let soma = 0, n = 0;
      for (let y = Math.max(0, Math.floor(cy - raio)); y <= Math.min(tela.height - 1, Math.ceil(cy + raio)); y += 1) {
        for (let x = Math.max(0, Math.floor(cx - raio)); x <= Math.min(tela.width - 1, Math.ceil(cx + raio)); x += 1) {
          if (Math.hypot(x - cx, y - cy) >= raio) { continue; }
          const i = (y * tela.width + x) * 4;
          soma += 0.2126 * lin(dados[i]) + 0.7152 * lin(dados[i + 1]) + 0.0722 * lin(dados[i + 2]);
          n += 1;
        }
      }
      saida[c.cor] = n ? soma / n : null;
    }
    return saida;
  }, { base64: png.toString("base64"), caixas, larguraCss });
}

/**
 * Razão de luminância lâmpada acesa ÷ maior apagada (WCAG: (L1 + 0,05) / (L2 + 0,05)).
 * Sem cor acesa (data-estado "neutro" ou fora das três cores), `razao` = null e `lampadas_acesas`
 * conta as lâmpadas que brilham por pixel.
 * @returns {Promise<{acesa:number|null, maior_apagada:number|null, razao:number|null, cor_acesa:string|null,
 *                    luminancias:Object, lampadas_acesas:number}>}
 */
async function razaoLampadas(page, opcoes) {
  const { ofuscamento = 0, filtro = null } = opcoes || {};
  if (filtro) { await aplicarFiltro(page, filtro); }
  try {
    const info = await page.evaluate(() => {
      const s = document.getElementById("semaforo").getBoundingClientRect();
      const caixas = Array.from(document.querySelectorAll("#semaforo .lampada")).map((l) => {
        const r = l.getBoundingClientRect();
        return { cor: (String(l.className).match(/lampada--(\w+)/) || [])[1] || "", x: r.x - s.x, y: r.y - s.y, w: r.width, h: r.height };
      });
      return { caixas, largura: s.width, estado: document.body.getAttribute("data-estado") };
    });
    const png = await recorteSemaforo(page);
    const lum = await luminanciasNoRecorte(page, png, info.caixas, info.largura);
    const g = Number(ofuscamento) || 0;
    const razaoWcag = (a, b) => (Math.max(a, b) + g + 0.05) / (Math.min(a, b) + g + 0.05);
    const valores = CORES.filter((c) => typeof lum[c] === "number").map((c) => lum[c]);
    const minima = valores.length ? Math.min(...valores) : 0;
    const acesasPorPixel = valores.filter((v) => (v + 0.05) / (minima + 0.05) >= LIMIAR_ACESA_POR_PIXEL).length;
    const cor = CORES.includes(info.estado) ? info.estado : null;
    const arred = (v) => (v === null ? null : Math.round(v * 10000) / 10000);
    if (!cor || typeof lum[cor] !== "number") {
      return { acesa: null, maior_apagada: null, razao: null, cor_acesa: null, luminancias: lum, lampadas_acesas: acesasPorPixel };
    }
    const apagadas = CORES.filter((c) => c !== cor && typeof lum[c] === "number").map((c) => lum[c]);
    const maior = apagadas.length ? Math.max(...apagadas) : 0;
    const r = (lum[cor] + g + 0.05) / (maior + g + 0.05);
    return { acesa: arred(lum[cor]), maior_apagada: arred(maior), razao: Math.round(r * 100) / 100, cor_acesa: cor,
      luminancias: Object.fromEntries(Object.entries(lum).map(([k, v]) => [k, arred(v)])), lampadas_acesas: acesasPorPixel,
      razao_simetrica: Math.round(razaoWcag(lum[cor], maior) * 100) / 100 };
  } finally {
    if (filtro) { await aplicarFiltro(page, null); }
  }
}

/** Diferença média de luminância (0..1) entre dois PNGs, na área comum (canto superior esquerdo). */
async function diferencaPixels(page, pngA, pngB) {
  const analise = await paginaAnalise(page);
  return analise.evaluate(async ({ a, b }) => {
    const carregar = async (base64) => { const im = new Image(); im.src = "data:image/png;base64," + base64; await im.decode(); return im; };
    const [ia, ib] = await Promise.all([carregar(a), carregar(b)]);
    const w = Math.min(ia.width, ib.width), h = Math.min(ia.height, ib.height);
    const ler = (im) => { const c = document.createElement("canvas"); c.width = w; c.height = h; const x = c.getContext("2d"); x.drawImage(im, 0, 0); return x.getImageData(0, 0, w, h).data; };
    const da = ler(ia), db = ler(ib);
    const lin = (v) => { const x = v / 255; return x <= 0.04045 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); };
    const lum = (d, i) => 0.2126 * lin(d[i]) + 0.7152 * lin(d[i + 1]) + 0.0722 * lin(d[i + 2]);
    let soma = 0;
    for (let i = 0; i < da.length; i += 4) { soma += Math.abs(lum(da, i) - lum(db, i)); }
    return Math.round(soma / (w * h) * 10000) / 10000;
  }, { a: pngA.toString("base64"), b: pngB.toString("base64") });
}

module.exports = { MATRIZES_DALTONISMO, aplicarFiltro, recorteSemaforo, razaoLampadas, diferencaPixels, luminanciasNoRecorte };
