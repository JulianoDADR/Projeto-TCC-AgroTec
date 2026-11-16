/*
 * axe-core e Lighthouse da suíte de validação UX (v4), só com Playwright e o Node.
 *
 * As duas ferramentas são OPCIONAIS (não são versionadas no projeto; vêm do
 * `npm install` do package.json da raiz):
 *   - axe-core: AGRO_AXE (caminho do axe.min.js), senão tests/e2e/vendor/axe.min.js,
 *     senão require.resolve("axe-core/axe.min.js");
 *   - Lighthouse: AGRO_LIGHTHOUSE (caminho do executável), senão node_modules/.bin/lighthouse
 *     da raiz do projeto, senão `lighthouse` no PATH.
 * Sem a ferramenta, o resultado sai com `pulado: true` e o aviso
 * "PULADO: <ferramenta> ausente" (com AGRO_UX_ESTRITO=1 a comparação das metas
 * transforma o pulo em falha).
 *
 * Exporta:
 *   localizarAxe() -> string|null, localizarLighthouse() -> string|null
 *   rodarAxe(page, opcoes?) -> Promise<{violacoes: Array<{id, impacto, alvos, n}>, incompletos_contraste: number, versao}>
 *   rodarLighthouse(url, opcoes?) -> Promise<{rodadas: Array, mediana: {acessibilidade, boas_praticas, desempenho, cls, lcp_ms}}>
 *   TAGS_AXE
 */
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFileSync, spawnSync } = require("child_process");

const RAIZ = path.resolve(__dirname, "..", "..", "..");
const TAGS_AXE = ["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa", "best-practice"];

/** Caminho do axe.min.js (ou null se não houver axe-core). */
function localizarAxe() {
  const candidatos = [];
  if (process.env.AGRO_AXE) { candidatos.push(process.env.AGRO_AXE); }
  candidatos.push(path.join(RAIZ, "tests", "e2e", "vendor", "axe.min.js"));
  for (const candidato of candidatos) {
    if (candidato && fs.existsSync(candidato)) { return candidato; }
  }
  try { return require.resolve("axe-core/axe.min.js", { paths: [RAIZ, __dirname] }); } catch (erro) { return null; }
}

/** Executável do Lighthouse (ou null). */
function localizarLighthouse() {
  if (process.env.AGRO_LIGHTHOUSE && fs.existsSync(process.env.AGRO_LIGHTHOUSE)) { return process.env.AGRO_LIGHTHOUSE; }
  const local = path.join(RAIZ, "node_modules", ".bin", "lighthouse");
  if (fs.existsSync(local)) { return local; }
  const busca = spawnSync("sh", ["-c", "command -v lighthouse"], { encoding: "utf8" });
  const achado = (busca.stdout || "").trim();
  return busca.status === 0 && achado ? achado : null;
}

/**
 * Roda o axe-core na página (injeta o script se preciso).
 * @param {import("playwright").Page} page
 * @param {{caminho?: string, contexto?: string}} [opcoes]
 * @returns {Promise<{violacoes: Array<{id:string, impacto:string, alvos:string[], n:number}>, incompletos_contraste:number, versao:string}>}
 */
async function rodarAxe(page, opcoes) {
  const caminho = (opcoes && opcoes.caminho) || localizarAxe();
  if (!caminho) { throw new Error("axe-core ausente"); }
  const presente = await page.evaluate(() => typeof window.axe !== "undefined");
  if (!presente) { await page.addScriptTag({ path: caminho }); }
  return page.evaluate(async ({ tags, contexto }) => {
    const resultado = await window.axe.run(contexto ? document.querySelector(contexto) : document, { runOnly: { type: "tag", values: tags }, resultTypes: ["violations", "incomplete"] });
    return {
      versao: window.axe.version,
      violacoes: resultado.violations.map((v) => ({ id: v.id, impacto: v.impact, n: v.nodes.length, alvos: v.nodes.slice(0, 5).map((n) => n.target.join(" ")) })),
      incompletos_contraste: (resultado.incomplete.find((i) => i.id === "color-contrast") || { nodes: [] }).nodes.length,
      incompletos_contraste_alvos: (resultado.incomplete.find((i) => i.id === "color-contrast") || { nodes: [] }).nodes.slice(0, 20).map((n) => n.target.join(" ")),
    };
  }, { tags: TAGS_AXE, contexto: (opcoes && opcoes.contexto) || null });
}

/** Mediana de uma lista de números (null se vazia). */
function mediana(valores) {
  const lista = valores.filter((v) => typeof v === "number" && !Number.isNaN(v)).sort((a, b) => a - b);
  if (!lista.length) { return null; }
  const meio = Math.floor(lista.length / 2);
  return lista.length % 2 ? lista[meio] : (lista[meio - 1] + lista[meio]) / 2;
}

/** Caminho do Chromium para o Lighthouse: CHROME_PATH, senão o do Playwright. */
function caminhoChrome() {
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) { return process.env.CHROME_PATH; }
  try { return require("playwright").chromium.executablePath(); } catch (erro) { return null; }
}

/**
 * Lighthouse mobile (CPU 4×, rede simulada padrão), `rodadas` vezes, com a mediana de cada métrica.
 * @param {string} url
 * @param {{rodadas?: number, binario?: string, antes?: Function}} [opcoes] `antes` (async) roda antes de cada rodada.
 */
async function rodarLighthouse(url, opcoes) {
  const binario = (opcoes && opcoes.binario) || localizarLighthouse();
  if (!binario) { throw new Error("lighthouse ausente"); }
  const rodadas = (opcoes && opcoes.rodadas) || 3;
  const pasta = fs.mkdtempSync(path.join(os.tmpdir(), "agro_lh_"));
  const saida = [];
  try {
    for (let i = 0; i < rodadas; i += 1) {
      if (opcoes && opcoes.antes) { await opcoes.antes(); }
      const arquivo = path.join(pasta, "lh_" + i + ".json");
      const ambiente = Object.assign({}, process.env);
      const chrome = caminhoChrome();
      if (chrome) { ambiente.CHROME_PATH = chrome; }
      execFileSync(binario, [url, "--quiet", "--output=json", "--output-path=" + arquivo, "--form-factor=mobile",
        "--throttling-method=simulate", "--throttling.cpuSlowdownMultiplier=4",
        "--only-categories=accessibility,best-practices,performance",
        "--chrome-flags=--headless=new --no-sandbox --disable-gpu"], { env: ambiente, stdio: ["ignore", "ignore", "pipe"], timeout: 180000 });
      const relatorio = JSON.parse(fs.readFileSync(arquivo, "utf8"));
      const nota = (c) => (relatorio.categories[c] && typeof relatorio.categories[c].score === "number" ? Math.round(relatorio.categories[c].score * 100) : null);
      const auditoria = (a) => (relatorio.audits[a] ? relatorio.audits[a].numericValue : null);
      saida.push({ acessibilidade: nota("accessibility"), boas_praticas: nota("best-practices"), desempenho: nota("performance"),
        cls: auditoria("cumulative-layout-shift"), lcp_ms: auditoria("largest-contentful-paint"),
        falhas: Object.values(relatorio.audits).filter((a) => a.score !== null && a.score < 1 && ["binary", "numeric"].includes(a.scoreDisplayMode) && a.weight !== 0).map((a) => a.id).slice(0, 15),
        versao: relatorio.lighthouseVersion });
    }
  } finally { fs.rmSync(pasta, { recursive: true, force: true }); }
  const chaves = ["acessibilidade", "boas_praticas", "desempenho", "cls", "lcp_ms"];
  const resumo = {};
  for (const chave of chaves) { resumo[chave] = mediana(saida.map((r) => r[chave])); }
  return { rodadas: saida, mediana: resumo };
}

module.exports = { TAGS_AXE, localizarAxe, localizarLighthouse, rodarAxe, rodarLighthouse, mediana, caminhoChrome };
