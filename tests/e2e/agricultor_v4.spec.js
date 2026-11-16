/*
 * Testes de ponta a ponta da página do agricultor v4 (Node + Playwright).
 *
 * Sobe `python src/app.py` num banco SQLite TEMPORÁRIO (AGRO_BANCO) e confere,
 * num Chromium sem opções, cada critério de aceite da Frente A:
 *   rede        A1 — Slow 3G + CPU 6×: 1ª lâmpada acesa ≤ 2.500 ms; 2ª visita com o CSS do cache e ≤ 15.500 B;
 *   oclusao     A2 — #acoes/#feito/#tecnico acima da barra de voz em 5 celulares × 13 estados; lâmpada ≥ 72 px,
 *                    vermelho ≥ verde, mesma orientação; 25 Tabs sem foco escondido;
 *   confianca   A3 — neutro (teste/demonstração de outro aparelho), última notícia pela forma, 503 ≠ sem ligação,
 *                    rótulo do semáforo e contraste no estado antigo;
 *   robustez    A4 — cores forçadas, escurecimento automático, ofuscamento ao sol, acromatopsia e overscroll;
 *   texto       A5 — fonte a 130 % e 150 % sem rolagem lateral nem colisão; 1ª tela com texto ≥ 16 px e ≥ 7:1;
 *   voz         A6 — fala curta → completa → devagar, preferência lembrada, retorno falado, ajuda, regiões vivas;
 *   feito       A7 — desfazer, sem duplicata, "Feito às HH:MM", diálogo modal nativo e PIN errado claro;
 *   pictogramas A8 — pares de símbolos diferentes a 24 px, mapas injetivos e banca sem florada/seca;
 *   acessibilidade — axe-core (wcag2a/2aa/21aa/22aa + best-practice): 0 violações em 17 estados × 2 telas
 *                    (360×740 e 1280×800). axe vem de AGRO_AXE, tests/e2e/vendor/axe.min.js ou do pacote
 *                    "axe-core" do Node; sem ele, o caso é pulado com aviso.
 *
 * As medições de contraste, oclusão e pixels usam tests/e2e/validacao/{medidas,pixels}.js (suíte da
 * Frente C) quando existirem; antes disso, as cópias locais abaixo, com a mesma assinatura.
 * A "demonstração vista fora da banca" é ligada a partir do endereço 127.0.0.2 (outro aparelho da rede),
 * e o navegador abre a página de 127.0.0.1.
 *
 * Uso: node tests/e2e/agricultor_v4.spec.js [caso ...]   (sem caso = todos; código de saída 0 = tudo certo)
 * AGRO_UX_CELULARES="360x640" limita os celulares do caso oclusao (depuração; sem ela, os 5).
 * Capturas em reports/ux_v4/ (ou AGRO_UX_CAPTURAS).
 */
"use strict";

const { chromium } = require("playwright");
const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const http = require("http");
const net = require("net");
const os = require("os");
const path = require("path");

const RAIZ = path.resolve(__dirname, "..", "..");
const PYTHON = process.env.PYTHON || "python";
const CAPTURAS = process.env.AGRO_UX_CAPTURAS || path.join(RAIZ, "reports", "ux_v4");
const COMMIT_V3 = "5c66abe";
const CENARIOS = {
  SECA_EXTREMA: "vermelho", ONDA_DE_CALOR: "vermelho", GEADA: "vermelho",
  ENCHARCAMENTO: "vermelho", ATENCAO_MODERADA: "amarelo", CONDICAO_IDEAL: "verde",
};
const TODOS_CELULARES = [[360, 640], [360, 740], [360, 800], [393, 873], [412, 915]];
// AGRO_UX_CELULARES="360x640,412x915" restringe os celulares (depuração); sem ela, os 5.
const CELULARES = process.env.AGRO_UX_CELULARES
  ? TODOS_CELULARES.filter(([l, a]) => process.env.AGRO_UX_CELULARES.split(",").includes(l + "x" + a)) : TODOS_CELULARES;
const ACAO_NA_FRASE = /Regue|Não regue|Pare|Abra|Faça|Cubra|Chegue|Proteja|Confira|Verifique|Olhe/;
const OUTRO_APARELHO = "127.0.0.2";
const TAGS_AXE = ["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa", "best-practice"];

/** Caminho do axe-core: AGRO_AXE, a cópia em tests/e2e/vendor ou o pacote do Node (null se não houver). */
function caminhoAxe() {
  const candidatos = [process.env.AGRO_AXE, path.join(__dirname, "vendor", "axe.min.js")].filter(Boolean);
  const local = candidatos.find((caminho) => fs.existsSync(caminho));
  if (local) { return local; }
  try { return require.resolve("axe-core/axe.min.js"); } catch (erro) { return null; }
}

const falhas = [];
let verificacoes = 0;

/** Registra uma verificação; a falha não interrompe as demais. */
function verificar(condicao, descricao) {
  verificacoes += 1;
  if (condicao) { console.log("  ok   " + descricao); } else { console.log("  FALHA " + descricao); falhas.push(descricao); }
}

const esperar = (ms) => new Promise((resolver) => setTimeout(resolver, ms));

/** Porta TCP livre no computador. */
function portaLivre() {
  return new Promise((resolver, rejeitar) => {
    const servidor = net.createServer();
    servidor.unref();
    servidor.on("error", rejeitar);
    servidor.listen(0, "127.0.0.1", () => { const { port } = servidor.address(); servidor.close(() => resolver(port)); });
  });
}

/** Sobe o servidor Flask num banco temporário e espera ele responder. */
async function subirServidor(banco) {
  const porta = Number(process.env.AGRO_PORTA_TESTE) || await portaLivre();
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

/**
 * Chamada à API direto do Node. `opcoes.origem` escolhe o endereço local (outro "aparelho" da rede);
 * `opcoes.cabecalhos` acrescenta cabeçalhos (ex.: X-PIN-Banca).
 */
function api(base, metodo, rota, corpo, opcoes) {
  const config = opcoes || {};
  const url = new URL(base + rota);
  const texto = corpo === undefined ? null : JSON.stringify(corpo);
  return new Promise((resolver, rejeitar) => {
    const pedido = http.request({
      hostname: url.hostname, port: url.port, path: url.pathname + url.search, method: metodo,
      localAddress: config.origem, headers: Object.assign({ "Content-Type": "application/json" }, config.cabecalhos || {}),
    }, (resposta) => {
      let dados = "";
      resposta.setEncoding("utf8");
      resposta.on("data", (pedaco) => { dados += pedaco; });
      resposta.on("end", () => {
        let json = null;
        try { json = JSON.parse(dados); } catch (erro) { json = null; }
        resolver({ status: resposta.statusCode, dados: json, cabecalhos: resposta.headers });
      });
    });
    pedido.on("error", rejeitar);
    if (texto !== null) { pedido.write(texto); }
    pedido.end();
  });
}

/** Grava uma leitura no banco temporário (origem, status, idade em segundos) com o Python do projeto. */
function inserirLeitura(banco, origem, status, idadeSegundos) {
  const codigo = [
    "import sys",
    "from datetime import datetime, timedelta, timezone",
    "from pathlib import Path",
    "sys.path.insert(0, 'src')",
    "from database import RegistroLeitura, inserir_leitura, buscar_parametros_cultura",
    "from gerador_dataset import calcular_features",
    "banco, origem, status, idade = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), float(sys.argv[4])",
    "instante = datetime.now(timezone.utc) - timedelta(seconds=idade)",
    "temperatura, solo = {0: (22.0, 60.0), 1: (31.0, 35.0), 2: (36.0, 15.0)}[status]",
    "deficit, desvio = calcular_features(temperatura, solo, buscar_parametros_cultura('cafe_arabica', caminho_banco=banco))",
    "print(inserir_leitura(RegistroLeitura(data_hora=instante.astimezone().replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%S'),",
    "    codigo_cultura='cafe_arabica', origem=origem, temperatura_ar_bruta=temperatura, umidade_ar_bruta=60.0,",
    "    umidade_solo_bruta=solo, temperatura_ar=temperatura, umidade_ar=60.0, umidade_solo=solo, deficit_hidrico_solo=deficit,",
    "    desvio_termico=desvio, status_regra=status, status_predito=status, status_alerta=status, probabilidade_predicao=1.0,",
    "    flag_anomalia=0, descricao_anomalia='', mensagem_voz='Perigo. A terra está seca. Regue agora.', versao_modelo='REGRAS',",
    "    estado_sensor='OK', instante_utc=instante.isoformat(timespec='seconds')), caminho_banco=banco))",
  ].join("\n");
  return Number(execFileSync(PYTHON, ["-c", codigo, banco, origem, String(status), String(idadeSegundos)], { cwd: RAIZ, encoding: "utf8" }).trim());
}

/** Cria uma página que guarda os erros de console (menos os padrões permitidos). */
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

/** Espera a página sair do "carregando" (estado aplicado). */
async function esperarPronta(pagina) {
  await pagina.waitForFunction(() => document.body && !document.body.classList.contains("carregando"), null, { timeout: 8000 });
}

/** Substitui a fala, o áudio e a vibração do navegador por dublês que gravam {texto, rate, lang}. */
function dublesDeVoz(idiomas) {
  window.__falas = [];
  window.__osciladores = 0;
  window.__vibracoes = 0;
  function Fala(texto) { this.text = texto; this.voice = null; this.lang = ""; this.rate = 1; }
  window.SpeechSynthesisUtterance = Fala;
  const vozes = idiomas.map((idioma) => ({ lang: idioma, name: "Voz " + idioma, voiceURI: idioma, localService: true, default: false }));
  const sintese = {
    speaking: false, pending: false, paused: false, onvoiceschanged: null,
    getVoices() { return vozes; },
    cancel() {},
    speak(fala) {
      window.__falas.push({ texto: fala.text, rate: fala.rate, lang: fala.voice ? fala.voice.lang : null });
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

/** Última fala gravada pelo dublê. */
async function ultimaFala(pagina) {
  return pagina.evaluate(() => (window.__falas[window.__falas.length - 1] || { texto: "", rate: 0 }));
}

/**
 * Abre a página com um estado FORÇADO: o HTML real com o estado embutido trocado e /api/status
 * respondendo o mesmo estado (como pedem os critérios "interceptado com ...").
 */
async function abrirComEstado(pagina, base, estado, caminho) {
  const html = await (await fetch(base + "/")).text();
  const inicio = html.indexOf('id="estado-inicial">') + 'id="estado-inicial">'.length;
  const fim = html.indexOf("</script>", inicio);
  const corpo = html.slice(0, inicio) + JSON.stringify(estado).replace(/</g, "\\u003c") + html.slice(fim);
  await pagina.route(base + "/" + (caminho || ""), (rota) => rota.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: corpo }));
  await pagina.route("**/api/status", (rota) => rota.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(estado) }));
  await pagina.goto(base + "/" + (caminho || ""));
  await esperarPronta(pagina);
}

/** Status atual do servidor visto de um aparelho (padrão: o próprio computador, que é a banca). */
async function statusAtual(base, origem) {
  return (await api(base, "GET", "/api/status", undefined, { origem })).dados;
}

/* ------------------------------------------------------------------------ */
/* Medições locais (mesma assinatura da suíte C; usadas enquanto ela não existir) */
/* ------------------------------------------------------------------------ */
const MEDIDAS_LOCAIS = {
  /**
   * Contraste de cada texto e ícone visível sob `raiz`: cor do texto com a opacidade dos ancestrais
   * sobre o fundo efetivo, no PIOR ponto dos gradientes. Devolve
   * [{seletor, texto, tamanho_px, razao, acima_dobra, tipo: "texto"|"icone", grande}].
   */
  async medirContraste(pagina, raiz) {
    return pagina.evaluate((seletorRaiz) => {
      function canal(c) { c /= 255; return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); }
      function luminancia(cor) { return 0.2126 * canal(cor[0]) + 0.7152 * canal(cor[1]) + 0.0722 * canal(cor[2]); }
      function razao(a, b) { const x = luminancia(a); const y = luminancia(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); }
      function compor(cima, baixo) { const a = cima[3]; return [0, 1, 2].map((i) => cima[i] * a + baixo[i] * (1 - a)).concat([1]); }
      // Cores no texto computado: rgb(), rgba(), color(srgb ...) e color-mix(in srgb, A p%, B).
      function lerCores(texto) {
        const cores = [];
        const padrao = /color-mix\(in srgb,\s*(rgba?\([^)]*\)|color\(srgb[^)]*\))\s*([\d.]+)%?,\s*(rgba?\([^)]*\)|color\(srgb[^)]*\)|transparent)\s*(?:([\d.]+)%)?\)|rgba?\([^)]*\)|color\(srgb[^)]*\)|transparent/g;
        let achado;
        while ((achado = padrao.exec(texto))) {
          if (achado[0].indexOf("color-mix") === 0) {
            const a = lerCores(achado[1])[0];
            const b = lerCores(achado[3])[0];
            const p = Number(achado[2]) / 100;
            cores.push([0, 1, 2, 3].map((i) => a[i] * p + b[i] * (1 - p)));
          } else { cores.push(lerCor(achado[0])); }
        }
        return cores;
      }
      function lerCor(texto) {
        if (texto === "transparent") { return [0, 0, 0, 0]; }
        if (texto.indexOf("color(srgb") === 0) {
          const n = texto.replace(/color\(srgb|\)|\//g, " ").trim().split(/\s+/).map(Number);
          return [n[0] * 255, n[1] * 255, n[2] * 255, n.length > 3 ? n[3] : 1];
        }
        const n = texto.replace(/rgba?\(|\)/g, "").split(/[\s,/]+/).filter(Boolean).map(Number);
        return [n[0], n[1], n[2], n.length > 3 ? n[3] : 1];
      }
      // Camadas de fundo do elemento até a raiz; cada gradiente vira várias opções (uma por parada).
      function fundos(elemento) {
        const camadas = [];
        for (let no = elemento; no && no.nodeType === 1; no = no.parentElement) {
          const estilo = getComputedStyle(no);
          const opcoes = [];
          if (estilo.backgroundImage && estilo.backgroundImage !== "none") { lerCores(estilo.backgroundImage).forEach((c) => opcoes.push(c)); }
          const cor = lerCor(estilo.backgroundColor);
          if (opcoes.length) { camadas.push(opcoes.map((c) => (cor[3] > 0 ? compor(c, cor) : c))); }
          else if (cor[3] > 0) { camadas.push([cor]); }
          if ((cor[3] >= 1 && !opcoes.length) || (opcoes.length && opcoes.every((c) => c[3] >= 1))) { break; }
        }
        let candidatos = [[255, 255, 255, 1]];
        for (let i = camadas.length - 1; i >= 0; i -= 1) {
          const novos = [];
          camadas[i].forEach((c) => candidatos.forEach((b) => novos.push(compor(c, b))));
          candidatos = novos.slice(0, 64);
        }
        return candidatos;
      }
      function opacidade(elemento) { let o = 1; for (let no = elemento; no && no.nodeType === 1; no = no.parentElement) { o *= Number(getComputedStyle(no).opacity); } return o; }
      function visivel(elemento) {
        const r = elemento.getBoundingClientRect();
        if (r.width < 1 || r.height < 1) { return false; }
        const estilo = getComputedStyle(elemento);
        if (estilo.visibility === "hidden" || estilo.clip === "rect(0px, 0px, 0px, 0px)") { return false; }
        for (let no = elemento; no; no = no.parentElement) { if (getComputedStyle(no).display === "none") { return false; } }
        const dialogo = elemento.closest("dialog");
        return !dialogo || dialogo.open;
      }
      function nome(elemento) { return elemento.id ? "#" + elemento.id : elemento.tagName.toLowerCase() + (elemento.className && typeof elemento.className === "string" ? "." + elemento.className.trim().split(/\s+/)[0] : ""); }
      const raiz = document.querySelector(seletorRaiz) || document.body;
      const barra = document.getElementById("barra-voz");
      const dobra = barra && getComputedStyle(barra).position === "fixed" ? barra.getBoundingClientRect().top : window.innerHeight;
      const saida = [];
      const medir = (elemento, tipo, texto) => {
        const estilo = getComputedStyle(elemento);
        const corTexto = lerCor(estilo.color);
        corTexto[3] *= opacidade(elemento);
        const tamanho = parseFloat(estilo.fontSize);
        const peso = Number(estilo.fontWeight) || 400;
        let pior = Infinity;
        fundos(elemento).forEach((fundo) => { pior = Math.min(pior, razao(compor(corTexto, fundo), fundo)); });
        const r = elemento.getBoundingClientRect();
        saida.push({ seletor: nome(elemento), texto, tamanho_px: tamanho, razao: Math.round(pior * 100) / 100, acima_dobra: r.bottom <= dobra + 1 && r.top < dobra,
          tipo, grande: tamanho >= 24 || (tamanho >= 18.66 && peso >= 700) });
      };
      raiz.querySelectorAll("*").forEach((elemento) => {
        if (elemento.closest("svg") || elemento.closest(".so-leitor")) { return; }
        const texto = Array.from(elemento.childNodes).filter((n) => n.nodeType === 3).map((n) => n.textContent).join("").trim();
        if (texto && visivel(elemento)) { medir(elemento, "texto", texto.slice(0, 40)); }
      });
      raiz.querySelectorAll("svg.icone").forEach((icone) => {
        const dono = icone.parentElement;
        if (visivel(icone) && !icone.closest(".semaforo")) { medir(icone, "icone", "ícone em " + nome(dono)); }
      });
      return saida;
    }, raiz || "body");
  },

  /** Sobreposição vertical (px) de cada seletor com a barra de voz fixa. */
  async medirOclusao(pagina, seletores) {
    return pagina.evaluate((lista) => {
      const barra = document.getElementById("barra-voz").getBoundingClientRect();
      return lista.map((seletor) => {
        const elemento = document.querySelector(seletor);
        if (!elemento) { return { seletor, sobreposicao_px: 0, ausente: true }; }
        const r = elemento.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) { return { seletor, sobreposicao_px: 0, ausente: true }; }
        const horizontal = Math.min(r.right, barra.right) - Math.max(r.left, barra.left);
        const vertical = Math.min(r.bottom, barra.bottom) - Math.max(r.top, barra.top);
        return { seletor, sobreposicao_px: horizontal > 0 && vertical > 0 ? Math.round(vertical * 10) / 10 : 0 };
      });
    }, seletores);
  },
};

/** Matrizes de daltonismo (Machado, Oliveira e Fernandes 2009, severidade 1) em RGB linear. */
const FILTROS_COR = {
  protanopia: [[0.152286, 1.052583, -0.204868], [0.114503, 0.786281, 0.099216], [-0.003882, -0.048116, 1.051998]],
  deuteranopia: [[0.367322, 0.860646, -0.227968], [0.280085, 0.672501, 0.047413], [-0.011820, 0.042940, 0.968881]],
  tritanopia: [[1.255528, -0.076749, -0.178779], [-0.078411, 0.930809, 0.147602], [0.004733, 0.691367, 0.303900]],
};

const PIXELS_LOCAIS = {
  /**
   * Luminância média (relativa, WCAG) no disco de raio < 0,42 × largura de cada lâmpada; devolve a acesa
   * (classe .acesa ou cor de body[data-estado]), a maior apagada e a razão (L1 + 0,05 + ofuscamento) /
   * (L2 + 0,05 + ofuscamento). `filtro` simula daltonismo antes da luminância (acromatopsia = luminância).
   */
  async razaoLampadas(pagina, opcoes) {
    const config = opcoes || {};
    const caixa = await pagina.locator("#semaforo").boundingBox();
    const discos = await pagina.evaluate(() => Array.from(document.querySelectorAll(".lampada")).map((l) => {
      const r = l.getBoundingClientRect();
      const s = l.closest(".soquete");
      const estado = document.body.getAttribute("data-estado");
      return { x: r.left + r.width / 2, y: r.top + r.height / 2, largura: r.width,
        acesa: s.classList.contains("acesa") || l.classList.contains("lampada--" + estado) };
    }));
    const png = await pagina.screenshot({ clip: caixa });
    const analisador = await pagina.context().newPage();
    const medias = await analisador.evaluate(async ({ b64, discos, caixa, matriz }) => {
      const imagem = new Image();
      imagem.src = "data:image/png;base64," + b64;
      await imagem.decode();
      const tela = document.createElement("canvas");
      tela.width = imagem.width; tela.height = imagem.height;
      const ctx = tela.getContext("2d");
      ctx.drawImage(imagem, 0, 0);
      const escala = imagem.width / caixa.width;
      const dados = ctx.getImageData(0, 0, tela.width, tela.height).data;
      const linear = (c) => { c /= 255; return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
      return discos.map((d) => {
        const cx = (d.x - caixa.x) * escala; const cy = (d.y - caixa.y) * escala; const raio = 0.42 * d.largura * escala;
        let soma = 0; let n = 0;
        for (let y = Math.floor(cy - raio); y <= cy + raio; y += 1) {
          for (let x = Math.floor(cx - raio); x <= cx + raio; x += 1) {
            if ((x - cx) * (x - cx) + (y - cy) * (y - cy) > raio * raio || x < 0 || y < 0 || x >= tela.width || y >= tela.height) { continue; }
            const i = (y * tela.width + x) * 4;
            let rgb = [linear(dados[i]), linear(dados[i + 1]), linear(dados[i + 2])];
            if (matriz) { rgb = matriz.map((linha) => Math.min(1, Math.max(0, linha[0] * rgb[0] + linha[1] * rgb[1] + linha[2] * rgb[2]))); }
            soma += 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2];
            n += 1;
          }
        }
        return { acesa: d.acesa, luminancia: soma / Math.max(1, n) };
      });
    }, { b64: png.toString("base64"), discos, caixa, matriz: FILTROS_COR[config.filtro] || null });
    await analisador.close();
    const acesa = medias.filter((m) => m.acesa).map((m) => m.luminancia)[0];
    const maior = Math.max.apply(null, medias.filter((m) => !m.acesa).map((m) => m.luminancia));
    const extra = 0.05 + (config.ofuscamento || 0);
    return { acesa, maior_apagada: maior, razao: acesa === undefined ? 0 : (acesa + extra) / (maior + extra) };
  },
};

let medidas = MEDIDAS_LOCAIS;
let pixels = PIXELS_LOCAIS;
try {
  // Integração v4: o medidas.js da suíte C separa textos (medirContraste) e ícones (medirIcones) e não
  // marca o "tipo". Este adaptador devolve o formato desta suíte ({tipo: "texto"|"icone", ...}); sem ele
  // os filtros por "tipo" ficavam vazios e as verificações de contraste passavam sem medir nada.
  const suiteC = require("./validacao/medidas.js");
  medidas = Object.assign({}, suiteC, {
    async medirContraste(pagina, raiz) {
      const textos = (await suiteC.medirContraste(pagina, raiz)).map((t) => Object.assign({ tipo: "texto" }, t));
      const icones = typeof suiteC.medirIcones === "function"
        ? (await suiteC.medirIcones(pagina, raiz)).map((i) => Object.assign({ tipo: "icone", texto: i.seletor }, i))
        : [];
      return textos.concat(icones);
    },
  });
} catch (erro) { medidas = MEDIDAS_LOCAIS; }
try { pixels = require("./validacao/pixels.js"); } catch (erro) { pixels = PIXELS_LOCAIS; }

/* ------------------------------------------------------------------------ */
/** Estados de teste: os 6 cenários reais e os forçados (antigo, teste) a partir do status real. */
function estadoAntigo(status) {
  return Object.assign({}, status, { leitura_desatualizada: true, titulo: "Sem medições novas",
    mensagem_voz: "Faz 3 horas que o aparelho não manda medição. A última cor pode não valer mais. Confira o aparelho.",
    fala_curta: "Atenção. Medição antiga, não siga a cor: confira o aparelho.", status_alerta: 1, cor: "amarelo",
    acoes: ["VERIFICAR_APARELHO"], acoes_detalhe: [{ codigo: "VERIFICAR_APARELHO", legenda: "Aparelho", descricao: "Confira o aparelho" }],
    causa: "APARELHO", legenda_causa: "Aparelho", demonstracao_ativa: false, segundos_restantes_demonstracao: 0 });
}
function estadoTeste(status) {
  return Object.assign({}, status, { dados_simulados: true, titulo: "Sensor desligado: dados de teste",
    mensagem_voz: "O aparelho do campo não está mandando medições. As cores agora são só um teste e não mostram a sua lavoura.",
    fala_curta: "Atenção. Teste, não é a sua lavoura: confira o aparelho.",
    acoes: ["VERIFICAR_APARELHO"], acoes_detalhe: [{ codigo: "VERIFICAR_APARELHO", legenda: "Aparelho", descricao: "Confira o aparelho" }],
    causa: "APARELHO", legenda_causa: "Aparelho", demonstracao_ativa: false, segundos_restantes_demonstracao: 0 });
}

/* ------------------------------------------------------------------------ */
/** Roda o axe-core na página e devolve as violações resumidas ({id, impacto, nos, alvo}). */
async function rodarAxe(pagina, axe) {
  await pagina.addScriptTag({ path: axe });
  return pagina.evaluate(async (tags) => {
    const resultado = await window.axe.run(document, { runOnly: { type: "tag", values: tags } });
    return resultado.violations.map((v) => ({ id: v.id, impacto: v.impact, nos: v.nodes.length, alvo: v.nodes[0].target.join(" ") }));
  }, TAGS_AXE);
}

async function testarAcessibilidade(navegador, base, banco) {
  console.log("\n[acessibilidade] axe-core sem violações em todos os estados da página do agricultor");
  const axe = caminhoAxe();
  if (!axe) { console.log("  aviso: axe-core não encontrado (AGRO_AXE, tests/e2e/vendor/axe.min.js ou npm axe-core); caso pulado"); return; }
  const cenario = (codigo) => async (p) => { await api(base, "POST", "/api/demonstracao", { cenario: codigo }); await p.goto(base + "/"); await esperarPronta(p); };
  const estados = Object.keys(CENARIOS).map((codigo) => ({ nome: codigo, abrir: cenario(codigo) }));
  estados.push({ nome: "simulacao", abrir: async (p) => {
    await api(base, "POST", "/api/demonstracao/encerrar", {}); inserirLeitura(banco, "SIMULACAO", 2, 1); await p.goto(base + "/"); await esperarPronta(p);
  } });
  estados.push({ nome: "antiga", abrir: async (p) => { await api(base, "POST", "/api/demonstracao", { cenario: "SECA_EXTREMA" }); await abrirComEstado(p, base, estadoAntigo(await statusAtual(base))); } });
  estados.push({ nome: "teste", abrir: async (p) => { await api(base, "POST", "/api/demonstracao", { cenario: "GEADA" }); await abrirComEstado(p, base, estadoTeste(await statusAtual(base))); } });
  estados.push({ nome: "demonstracao-de-fora", abrir: async (p) => {
    await api(base, "POST", "/api/demonstracao", { cenario: "GEADA" }, { origem: OUTRO_APARELHO }); await p.goto(base + "/"); await esperarPronta(p);
  } });
  estados.push({ nome: "offline", abrir: async (p) => {
    await cenario("SECA_EXTREMA")(p); await p.context().setOffline(true);
    await p.waitForFunction(() => document.body.dataset.confianca === "sem-ligacao", null, { timeout: 15000 }).catch(() => null);
  } });
  estados.push({ nome: "503", abrir: async (p) => {
    await cenario("CONDICAO_IDEAL")(p);
    await p.route("**/api/status", (rota) => rota.fulfill({ status: 503, contentType: "application/json", body: '{"erro":"indisponível"}' }));
    await p.waitForFunction(() => document.body.dataset.confianca === "erro-servidor", null, { timeout: 25000 }).catch(() => null);
  } });
  estados.push({ nome: "sem-voz-pt", voz: ["en-US"], abrir: async (p) => {
    await cenario("GEADA")(p); await p.locator("#botao-voz").click();
    await p.waitForFunction(() => document.body.classList.contains("sem-voz"), null, { timeout: 3000 }).catch(() => null);
  } });
  estados.push({ nome: "desfazer", abrir: async (p) => {
    await cenario("SECA_EXTREMA")(p); await p.locator("#feito").click();
    await p.waitForSelector("[aria-label='Desfazer']", { state: "visible", timeout: 3000 }).catch(() => null);
  } });
  estados.push({ nome: "dialogo-tecnico", abrir: async (p) => {
    await cenario("ATENCAO_MODERADA")(p); await p.locator("#tecnico").click(); await p.waitForSelector("#dialogo[open]", { timeout: 3000 }).catch(() => null);
  } });
  estados.push({ nome: "banca-aberta", abrir: async (p) => {
    await api(base, "POST", "/api/demonstracao", { cenario: "ONDA_DE_CALOR" }); await p.goto(base + "/?banca=1"); await esperarPronta(p);
    await p.waitForSelector(".botao-cenario", { state: "visible", timeout: 5000 }).catch(() => null);
  } });
  for (const [largura, altura] of [[360, 740], [1280, 800]]) {
    for (const estado of estados) {
      const contexto = await navegador.newContext({ viewport: { width: largura, height: altura } });
      await contexto.addInitScript(dublesDeVoz, estado.voz || ["pt-BR"]);
      const pagina = await novaPagina(contexto, [/Failed to load resource/, /ERR_INTERNET_DISCONNECTED/, /status of 503/]);
      await estado.abrir(pagina);
      await pagina.waitForTimeout(300);
      const violacoes = await rodarAxe(pagina, axe);
      verificar(violacoes.length === 0, `${largura}x${altura} ${estado.nome}: axe sem violações ${violacoes.length ? JSON.stringify(violacoes) : ""}`);
      verificar(pagina.errosConsole.length === 0, `${largura}x${altura} ${estado.nome}: nenhum erro de console ${pagina.errosConsole.join(" | ")}`);
      await contexto.close();
    }
  }
  await api(base, "POST", "/api/demonstracao/encerrar", {});
}

/* ------------------------------------------------------------------------ */
async function testarRede(navegador, base) {
  console.log("\n[rede] A1 — Slow 3G e CPU 6×, CSS imutável na 2ª visita");
  await api(base, "POST", "/api/demonstracao", { cenario: "SECA_EXTREMA" });
  const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
  await contexto.addInitScript(() => {
    // Instante (desde o início da navegação) em que a 1ª lâmpada aparece acesa na tela.
    const marcar = () => {
      if (window.__acesaEm) { return; }
      const lampada = document.querySelector(".acesa .lampada");
      if (lampada && getComputedStyle(lampada).boxShadow !== "none") { requestAnimationFrame(() => { window.__acesaEm = window.__acesaEm || performance.now(); }); }
    };
    new MutationObserver(marcar).observe(document, { subtree: true, childList: true, attributes: true });
  });
  const pagina = await novaPagina(contexto);
  const cdp = await contexto.newCDPSession(pagina);
  await cdp.send("Network.enable");
  await cdp.send("Network.emulateNetworkConditions", { offline: false, latency: 2000, downloadThroughput: 50 * 1000, uploadThroughput: 50 * 1000 });
  await cdp.send("Emulation.setCPUThrottlingRate", { rate: 6 });
  const respostas = new Map();
  // "requestServedFromCache" chega ANTES de "responseReceived": a marca de cache é somada, nunca sobrescrita.
  cdp.on("Network.responseReceived", (e) => {
    const antes = respostas.get(e.requestId) || {};
    respostas.set(e.requestId, { url: e.response.url, cache: !!(antes.cache || e.response.fromDiskCache || e.response.fromMemoryCache),
      sw: !!e.response.fromServiceWorker, status: e.response.status, tipo: e.type });
  });
  cdp.on("Network.requestServedFromCache", (e) => { const r = respostas.get(e.requestId) || {}; r.cache = true; respostas.set(e.requestId, r); });
  const transferidos = new Map();
  cdp.on("Network.loadingFinished", (e) => { transferidos.set(e.requestId, e.encodedDataLength); });
  await pagina.goto(base + "/", { waitUntil: "commit" });
  await pagina.waitForFunction(() => window.__acesaEm, null, { timeout: 20000 }).catch(() => null);
  const acesaEm = await pagina.evaluate(() => window.__acesaEm || Infinity);
  verificar(acesaEm <= 2500, `1ª visita (Slow 3G 2.000 ms/50 kB/s, CPU 6×): lâmpada acesa em ${Math.round(acesaEm)} ms (limite 2.500)`);
  await pagina.waitForLoadState("load");
  await pagina.waitForTimeout(1500);
  const cssPrimeira = Array.from(respostas.values()).find((r) => /\/static\/agro\.[0-9a-f]{10}\.css$/.test(r.url || ""));
  verificar(!!cssPrimeira && cssPrimeira.status === 200, "1ª visita: /static/agro.<hash>.css carregado (200)");
  const outroHost = Array.from(respostas.values()).filter((r) => r.url && !r.url.startsWith(base) && !r.url.startsWith("data:"));
  verificar(outroHost.length === 0, `0 recursos de outro host ${JSON.stringify(outroHost.map((r) => r.url))}`);

  respostas.clear();
  transferidos.clear();
  await pagina.goto(base + "/", { waitUntil: "load" });
  await pagina.waitForTimeout(1500);
  const css = Array.from(respostas.entries()).filter(([, r]) => /\/static\/agro\.[0-9a-f]{10}\.css$/.test(r.url || ""));
  const doCache = css.every(([, r]) => r.cache && !r.sw);
  verificar(css.length === 0 || doCache, `2ª visita: CSS do cache do navegador (sem Service Worker) ${JSON.stringify(css.map(([, r]) => r))}`);
  // Transferência da 2ª visita até o "load" + 1,5 s: documento, CSS, ícone e a consulta de culturas.
  const total = Array.from(transferidos.values()).reduce((a, b) => a + b, 0);
  verificar(total <= 15500, `2ª visita transfere ${total} B (limite 15.500)`);
  verificar(pagina.errosConsole.length === 0, "rede: nenhum erro de console " + pagina.errosConsole.join(" | "));
  await contexto.close();
}

/* ------------------------------------------------------------------------ */
async function testarOclusao(navegador, base, banco) {
  console.log("\n[oclusao] A2 — nada essencial atrás da barra de voz; semáforo grande e estável");
  const orientacoes = new Set();
  const seletores = ["#acoes", "#feito", "#tecnico"];
  for (const [largura, altura] of CELULARES) {
    const rotuloTela = `${largura}x${altura}`;
    const larguras = {};
    const estados = [];
    for (const cenario of Object.keys(CENARIOS)) { estados.push({ nome: cenario, abrir: async (p) => { await api(base, "POST", "/api/demonstracao", { cenario }); await p.goto(base + "/"); await esperarPronta(p); } }); }
    estados.push({ nome: "simulacao", abrir: async (p) => {
      await api(base, "POST", "/api/demonstracao/encerrar", {});
      inserirLeitura(banco, "SIMULACAO", 2, 1);
      await p.goto(base + "/"); await esperarPronta(p);
    } });
    estados.push({ nome: "antiga", abrir: async (p) => { await api(base, "POST", "/api/demonstracao", { cenario: "SECA_EXTREMA" }); await abrirComEstado(p, base, estadoAntigo(await statusAtual(base))); } });
    estados.push({ nome: "dados_simulados", abrir: async (p) => { await api(base, "POST", "/api/demonstracao", { cenario: "GEADA" }); await abrirComEstado(p, base, estadoTeste(await statusAtual(base))); } });
    estados.push({ nome: "offline", abrir: async (p) => {
      await api(base, "POST", "/api/demonstracao", { cenario: "SECA_EXTREMA" });
      await p.goto(base + "/"); await esperarPronta(p);
      await p.context().setOffline(true);
      await p.waitForFunction(() => document.body.dataset.confianca === "sem-ligacao", null, { timeout: 15000 }).catch(() => null);
    } });
    estados.push({ nome: "503", abrir: async (p) => {
      await api(base, "POST", "/api/demonstracao", { cenario: "CONDICAO_IDEAL" });
      await p.goto(base + "/"); await esperarPronta(p);
      await p.route("**/api/status", (rota) => rota.fulfill({ status: 503, contentType: "application/json", body: '{"erro":"indisponível"}' }));
      await p.waitForFunction(() => document.body.dataset.confianca === "erro-servidor", null, { timeout: 25000 }).catch(() => null);
    } });
    estados.push({ nome: "troca-de-cultura", abrir: async (p) => {
      await api(base, "POST", "/api/demonstracao", { cenario: "SECA_EXTREMA" });
      await p.goto(base + "/"); await esperarPronta(p);
      await api(base, "POST", "/api/cultura", { codigo: "cafe_conilon" }, { origem: OUTRO_APARELHO });
      await p.waitForFunction(() => !document.getElementById("faixa-troca").hidden, null, { timeout: 8000 }).catch(() => null);
    } });
    estados.push({ nome: "sem-voz-pt", voz: ["en-US"], abrir: async (p) => {
      await api(base, "POST", "/api/demonstracao", { cenario: "GEADA" });
      await p.goto(base + "/"); await esperarPronta(p);
      await p.locator("#botao-voz").click();
      await p.waitForFunction(() => document.body.classList.contains("sem-voz"), null, { timeout: 3000 }).catch(() => null);
    } });
    for (const estado of estados) {
      const contexto = await navegador.newContext({ viewport: { width: largura, height: altura } });
      await contexto.addInitScript(dublesDeVoz, estado.voz || ["pt-BR"]);
      const pagina = await novaPagina(contexto, [/Failed to load resource/, /ERR_INTERNET_DISCONNECTED/, /status of 503/]);
      await estado.abrir(pagina);
      await pagina.waitForTimeout(250);
      const rotulo = `${rotuloTela} ${estado.nome}`;
      const oclusao = await medidas.medirOclusao(pagina, seletores);
      const cobertos = oclusao.filter((o) => o.sobreposicao_px > 1);
      verificar(cobertos.length === 0, `${rotulo}: #acoes, #feito e #tecnico acima da barra de voz ${cobertos.length ? JSON.stringify(cobertos) : ""}`);
      const m = await pagina.evaluate(() => ({ lampada: document.querySelector(".lampada").getBoundingClientRect().width,
        direcao: getComputedStyle(document.getElementById("semaforo")).flexDirection, estado: document.body.dataset.estado,
        lateral: document.documentElement.scrollWidth - document.documentElement.clientWidth }));
      verificar(m.lampada >= 72 && m.lateral <= 0, `${rotulo}: lâmpada com ${m.lampada.toFixed(1)} px (≥ 72) e sem rolagem lateral`);
      larguras[m.estado] = Math.min(larguras[m.estado] || Infinity, m.lampada);
      orientacoes.add(m.direcao);
      if ((largura === 360 && altura === 740) || cobertos.length) { await pagina.screenshot({ path: path.join(CAPTURAS, `oclusao_${estado.nome.toLowerCase()}_${rotuloTela}.png`) }); }
      verificar(pagina.errosConsole.length === 0, `${rotulo}: nenhum erro de console ${pagina.errosConsole.join(" | ")}`);
      await contexto.setOffline(false);
      await contexto.close();
      if (estado.nome === "troca-de-cultura") { await api(base, "POST", "/api/cultura", { codigo: "cafe_arabica" }); }
    }
    verificar(larguras.vermelho >= larguras.verde, `${rotuloTela}: lâmpada no vermelho (${larguras.vermelho}) ≥ no verde (${larguras.verde})`);
  }
  verificar(orientacoes.size === 1, `semáforo com a mesma orientação nos 5 celulares em pé (${Array.from(orientacoes).join(", ")})`);

  // Foco pelo teclado: 25 Tabs, nenhum elemento focado fora da barra fica sob ela.
  for (const cenario of ["SECA_EXTREMA", "CONDICAO_IDEAL"]) {
    await api(base, "POST", "/api/demonstracao", { cenario });
    const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
    const pagina = await novaPagina(contexto);
    await pagina.goto(base + "/?banca=1");
    await esperarPronta(pagina);
    await pagina.waitForSelector(".botao-cenario");
    const escondidos = [];
    for (let i = 0; i < 25; i += 1) {
      await pagina.keyboard.press("Tab");
      const r = await pagina.evaluate(() => {
        const ativo = document.activeElement;
        const barra = document.getElementById("barra-voz");
        if (!ativo || barra.contains(ativo) || ativo === document.body) { return null; }
        const a = ativo.getBoundingClientRect(); const b = barra.getBoundingClientRect();
        const cruza = a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
        return cruza ? (ativo.id || ativo.className || ativo.tagName) : null;
      });
      if (r) { escondidos.push(r); }
    }
    verificar(escondidos.length === 0, `${cenario}: 25 Tabs sem foco escondido sob a barra ${JSON.stringify(escondidos)}`);
    await contexto.close();
  }
}

/* ------------------------------------------------------------------------ */
async function testarConfianca(navegador, base) {
  console.log("\n[confianca] A3 — neutro, última notícia pela forma, 503 ≠ sem ligação, rótulo do semáforo");
  // (a) Simulação e demonstração ligada em OUTRO aparelho: semáforo apagado, sem alarme nem fala sozinha.
  await api(base, "POST", "/api/demonstracao", { cenario: "GEADA" }, { origem: OUTRO_APARELHO });
  const demoFora = await statusAtual(base);
  verificar(demoFora.aparelho_da_banca === false && demoFora.status_alerta === 2, "demonstração de outro aparelho: aparelho_da_banca=false e status_alerta 2 intacto");
  for (const [nome, estado] of [["simulação", estadoTeste(Object.assign({}, demoFora, { status_alerta: 2, cor: "vermelho" }))],
    ["demonstração de fora", Object.assign({}, demoFora, { segundos_restantes_demonstracao: 100000 })]]) {
    const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
    await contexto.addInitScript(dublesDeVoz, ["pt-BR"]);
    const pagina = await novaPagina(contexto);
    await pagina.clock.install();
    await abrirComEstado(pagina, base, estado);
    const m = await pagina.evaluate(() => {
      const raiz = getComputedStyle(document.documentElement);
      const cores = ["--verde", "--amarelo", "--vermelho"].map((v) => { const d = document.createElement("div"); d.style.color = raiz.getPropertyValue(v); document.body.appendChild(d); const c = getComputedStyle(d).color; d.remove(); return c; });
      return { estado: document.body.dataset.estado, brilhos: Array.from(document.querySelectorAll(".lampada")).map((l) => getComputedStyle(l).boxShadow),
        borda: getComputedStyle(document.querySelector(".cartao-estado")).borderTopColor, cores,
        chip: document.getElementById("chip-icone").getAttribute("href"), aviso: document.getElementById("aviso-teste").hidden ? "" : document.getElementById("aviso-teste").textContent,
        aria: document.getElementById("semaforo").getAttribute("aria-label") };
    });
    verificar(m.estado === "neutro", `${nome}: body[data-estado=neutro] (${m.estado})`);
    verificar(m.brilhos.every((b) => b === "none"), `${nome}: nenhuma lâmpada com brilho ${JSON.stringify(m.brilhos)}`);
    verificar(m.cores.indexOf(m.borda) === -1, `${nome}: borda do cartão ${m.borda} fora das cores do semáforo`);
    verificar(["#i-check", "#i-x", "#i-exclamacao"].indexOf(m.chip) === -1, `${nome}: ícone do chip ${m.chip}`);
    verificar(/teste|Demonstração/.test(m.aviso), `${nome}: aviso visível "${m.aviso}"`);
    verificar(/^Semáforo apagado/.test(m.aria), `${nome}: aria-label "${m.aria}"`);
    await pagina.clock.runFor(30 * 60 * 1000);
    const repouso = await pagina.evaluate(() => ({ vibracoes: window.__vibracoes, falas: window.__falas.length, bipes: window.__osciladores }));
    verificar(repouso.vibracoes === 0 && repouso.falas === 0 && repouso.bipes === 0, `${nome}: 30 min sem vibrar nem falar sozinho ${JSON.stringify(repouso)}`);
    await pagina.locator("#botao-voz").click();
    await pagina.clock.runFor(100);
    const fala = await ultimaFala(pagina);
    verificar(/^Atenção\./.test(fala.texto), `${nome}: 1ª fala começa com "Atenção." ("${fala.texto}")`);
    await pagina.screenshot({ path: path.join(CAPTURAS, `neutro_${nome.split(" ")[0].normalize("NFD").replace(/[^\w]/g, "")}_360x740.png`) });
    verificar(pagina.errosConsole.length === 0, `${nome}: nenhum erro de console ${pagina.errosConsole.join(" | ")}`);
    await contexto.close();
  }
  // A mesma demonstração aberta na banca (?banca=1) continua com a cor.
  {
    const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
    const pagina = await novaPagina(contexto);
    await pagina.goto(base + "/?banca=1");
    await esperarPronta(pagina);
    verificar(await pagina.evaluate(() => document.body.dataset.estado === "vermelho"), "demonstração vista com ?banca=1: vermelho (a banca vê a cor)");
    await contexto.close();
  }
  await api(base, "POST", "/api/demonstracao/encerrar", {});

  // (b) Última notícia: leitura antiga e sem rede, em SECA_EXTREMA e CONDICAO_IDEAL.
  for (const cenario of ["SECA_EXTREMA", "CONDICAO_IDEAL"]) {
    await api(base, "POST", "/api/demonstracao", { cenario });
    const vivo = await statusAtual(base);
    const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
    const pagina = await novaPagina(contexto, [/Failed to load resource/, /ERR_INTERNET_DISCONNECTED/]);
    await pagina.goto(base + "/");
    await esperarPronta(pagina);
    const borda = (p) => p.evaluate(() => ({ borda: getComputedStyle(document.getElementById("semaforo")).borderTopStyle, opacidade: getComputedStyle(document.getElementById("acoes")).opacity }));
    const noVivo = await borda(pagina);
    verificar(noVivo.borda !== "dashed", `${cenario} vivo: semáforo sem borda tracejada (${noVivo.borda})`);
    await contexto.setOffline(true);
    await pagina.evaluate(() => window.dispatchEvent(new Event("offline")));
    await pagina.waitForFunction(() => document.body.dataset.confianca === "sem-ligacao", null, { timeout: 15000 }).catch(() => null);
    const semRede = await borda(pagina);
    const antigas = await pagina.locator("#acoes > li[data-antiga='true']").count();
    verificar(semRede.borda === "dashed" && semRede.opacidade === "1" && antigas >= 1, `${cenario} sem rede: borda tracejada, ações opacas e marcadas como antigas (${JSON.stringify(semRede)}, ${antigas})`);
    verificar(/Sem ligação/.test(await pagina.locator("#titulo").textContent()), `${cenario} sem rede: título "Sem ligação"`);
    await pagina.screenshot({ path: path.join(CAPTURAS, `sem_ligacao_${cenario.toLowerCase()}_360x740.png`) });
    await contexto.setOffline(false);
    await pagina.unroute("**/api/status").catch(() => null);
    const antiga = await navegador.newPage({ viewport: { width: 360, height: 740 } });
    await abrirComEstado(antiga, base, Object.assign({}, vivo, { leitura_desatualizada: true }));
    const naAntiga = await borda(antiga);
    verificar(naAntiga.borda === "dashed" && naAntiga.opacidade === "1", `${cenario} leitura antiga: borda tracejada e ações opacas (${JSON.stringify(naAntiga)})`);
    // (e) Contraste no estado antigo: texto ≥ 4,5:1 (3:1 grande) e ícones das ações ≥ 3:1.
    const contraste = await medidas.medirContraste(antiga, "body");
    const ruins = contraste.filter((c) => (c.tipo === "texto" && c.razao < (c.grande ? 3 : 4.5)));
    const icones = contraste.filter((c) => c.tipo === "icone" && /acao|#acoes|button\.acao__quadro|span\.acao__quando/.test(c.texto) && c.razao < 3);
    const medidosTexto = contraste.filter((c) => c.tipo === "texto").length;
    const medidosIcone = contraste.filter((c) => c.tipo === "icone" && /acao|#acoes|button\.acao__quadro|span\.acao__quando/.test(c.texto)).length;
    verificar(medidosTexto > 5 && medidosIcone > 0 && ruins.length === 0 && icones.length === 0,
      `${cenario} antigo: contraste AA de ${medidosTexto} textos e ${medidosIcone} ícones das ações ${JSON.stringify(ruins.concat(icones).slice(0, 5))}`);
    await antiga.screenshot({ path: path.join(CAPTURAS, `antiga_${cenario.toLowerCase()}_360x740.png`) });
    await antiga.close();
    await contexto.close();
  }

  // (c) 503 por 20 s depois de um verde: "programa com problema", nunca "sem ligação" nem ✓.
  {
    await api(base, "POST", "/api/demonstracao", { cenario: "CONDICAO_IDEAL" });
    const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
    const pagina = await novaPagina(contexto, [/status of 503/]);
    await pagina.clock.install();
    await pagina.goto(base + "/");
    await esperarPronta(pagina);
    await pagina.route("**/api/status", (rota) => rota.fulfill({ status: 503, contentType: "application/json", body: '{"erro":"O sistema teve um problema."}' }));
    for (let i = 0; i < 4; i += 1) { await pagina.clock.runFor(5000); await pagina.waitForTimeout(150); }
    const m = await pagina.evaluate(() => ({ titulo: document.getElementById("titulo").textContent, mensagem: document.getElementById("mensagem").textContent,
      chip: document.getElementById("chip-icone").getAttribute("href"), confianca: document.body.dataset.confianca, acoes: Array.from(document.querySelectorAll("#acoes > li")).map((l) => l.dataset.acao) }));
    verificar(!/Sem ligação/.test(m.titulo) && !/Chegue mais perto/.test(m.mensagem) && m.chip !== "#i-check", `503: "${m.titulo}" / chip ${m.chip} / ${m.confianca}`);
    verificar(m.confianca === "erro-servidor" && m.acoes.join() === "VERIFICAR_APARELHO", `503: data-confianca=erro-servidor e ação VERIFICAR_APARELHO (${m.acoes})`);
    await pagina.screenshot({ path: path.join(CAPTURAS, "erro_servidor_360x740.png") });
    await contexto.close();
    const offline = await navegador.newContext({ viewport: { width: 360, height: 740 } });
    const p2 = await novaPagina(offline, [/Failed to load resource/, /ERR_INTERNET_DISCONNECTED/]);
    await p2.goto(base + "/");
    await esperarPronta(p2);
    await offline.setOffline(true);
    await p2.evaluate(() => window.dispatchEvent(new Event("offline")));
    verificar(/Sem ligação/.test(await p2.locator("#titulo").textContent()), "setOffline: o título continua \"Sem ligação\"");
    await offline.close();
  }

  // (d) Rótulo do semáforo: concordância e marca de antiga/teste em 4 cenários × 4 estados.
  for (const cenario of ["SECA_EXTREMA", "ATENCAO_MODERADA", "CONDICAO_IDEAL", "GEADA"]) {
    await api(base, "POST", "/api/demonstracao", { cenario });
    const vivo = await statusAtual(base);
    for (const [nome, estado] of [["vivo", vivo], ["simulado", estadoTeste(vivo)], ["antigo", Object.assign({}, vivo, { leitura_desatualizada: true })], ["offline", null]]) {
      const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
      const pagina = await novaPagina(contexto, [/Failed to load resource/, /ERR_INTERNET_DISCONNECTED/]);
      if (estado) { await abrirComEstado(pagina, base, estado); } else {
        await pagina.goto(base + "/"); await esperarPronta(pagina);
        await contexto.setOffline(true);
        await pagina.evaluate(() => window.dispatchEvent(new Event("offline")));
      }
      const aria = await pagina.locator("#semaforo").getAttribute("aria-label");
      const ok = /luz (verde|amarela|vermelha) acesa|apagado/.test(aria) && !/luz (vermelho|amarelo) /.test(aria)
        && (!(estado && estado.leitura_desatualizada) || /antiga/.test(aria)) && (!(estado && estado.dados_simulados) || /teste/.test(aria));
      verificar(ok, `${cenario} ${nome}: aria-label "${aria}"`);
      await contexto.close();
    }
  }
  // (f) A API não mudou: o status da leitura de teste continua com a cor da leitura.
  await api(base, "POST", "/api/demonstracao", { cenario: "SECA_EXTREMA" });
  verificar((await statusAtual(base)).status_alerta === 2, "GET /api/status mantém status_alerta 2 (só a apresentação mudou)");
}

/* ------------------------------------------------------------------------ */
async function testarRobustez(navegador, base) {
  console.log("\n[robustez] A4 — cores forçadas, escurecimento automático, sol e acromatopsia");
  for (const cenario of ["CONDICAO_IDEAL", "ATENCAO_MODERADA", "SECA_EXTREMA"]) {
    await api(base, "POST", "/api/demonstracao", { cenario });
    const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 }, forcedColors: "active" });
    const pagina = await novaPagina(contexto);
    await pagina.goto(base + "/");
    await esperarPronta(pagina);
    await pagina.waitForTimeout(300);
    const r = await pixels.razaoLampadas(pagina, {});
    const barra = await pagina.evaluate(() => {
      const b = document.getElementById("barra-voz"); const q = b.getBoundingClientRect();
      const meio = document.elementFromPoint(q.left + q.width / 2, q.top + q.height / 2);
      return { fundo: getComputedStyle(b).backgroundColor, dentro: b.contains(meio) };
    });
    verificar(r.razao >= 3, `cores forçadas ${cenario}: acesa ÷ apagada ${r.razao.toFixed(2)} (≥ 3,0)`);
    verificar(barra.fundo !== "rgba(0, 0, 0, 0)" && barra.dentro, `cores forçadas ${cenario}: barra de voz opaca ${JSON.stringify(barra)}`);
    if (cenario === "SECA_EXTREMA") { await pagina.screenshot({ path: path.join(CAPTURAS, "cores_forcadas_360x740.png") }); }
    await contexto.close();
  }
  {
    const escuro = await chromium.launch({ args: ["--enable-features=WebContentsForceDark", "--blink-settings=forceDarkModeEnabled=true"] });
    await api(base, "POST", "/api/demonstracao", { cenario: "ATENCAO_MODERADA" });
    const contexto = await escuro.newContext({ viewport: { width: 360, height: 740 }, colorScheme: "dark" });
    const pagina = await contexto.newPage();
    await pagina.goto(base + "/");
    await esperarPronta(pagina);
    await pagina.waitForTimeout(300);
    const caixa = await pagina.locator(".cartao-estado").boundingBox();
    const png = await pagina.screenshot({ clip: { x: caixa.x + caixa.width - 30, y: caixa.y + caixa.height - 14, width: 8, height: 6 } });
    const analisador = await contexto.newPage();
    const fundo = await analisador.evaluate(async (b64) => {
      const i = new Image(); i.src = "data:image/png;base64," + b64; await i.decode();
      const c = document.createElement("canvas"); c.width = i.width; c.height = i.height; const x = c.getContext("2d"); x.drawImage(i, 0, 0);
      const d = x.getImageData(0, 0, 1, 1).data; const l = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
      return 0.2126 * l(d[0]) + 0.7152 * l(d[1]) + 0.0722 * l(d[2]);
    }, png.toString("base64"));
    await analisador.close();
    const r = await pixels.razaoLampadas(pagina, {});
    verificar(fundo >= 0.8, `escurecimento automático: fundo do cartão com luminância ${fundo.toFixed(2)} (≥ 0,8)`);
    verificar(r.razao >= 9, `escurecimento automático: amarelo aceso ÷ apagado ${r.razao.toFixed(2)} (≥ 9,0)`);
    await pagina.screenshot({ path: path.join(CAPTURAS, "escurecimento_automatico_360x740.png") });
    await escuro.close();
  }
  for (const cenario of ["CONDICAO_IDEAL", "ATENCAO_MODERADA", "SECA_EXTREMA"]) {
    await api(base, "POST", "/api/demonstracao", { cenario });
    const vivo = await statusAtual(base);
    for (const [nome, estado] of [["vivo", vivo], ["esmaecido", Object.assign({}, vivo, { leitura_desatualizada: true })]]) {
      const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
      const pagina = await novaPagina(contexto);
      await abrirComEstado(pagina, base, estado);
      await pagina.waitForTimeout(200);
      for (const filtro of [undefined, "acromatopsia"]) {
        const normal = await pixels.razaoLampadas(pagina, { filtro });
        const sol = await pixels.razaoLampadas(pagina, { filtro, ofuscamento: 0.1 });
        verificar(normal.razao >= 4.5 && sol.razao >= 3, `${cenario} ${nome}${filtro ? " " + filtro : ""}: acesa ÷ apagada ${normal.razao.toFixed(2)} (≥ 4,5) e ao sol ${sol.razao.toFixed(2)} (≥ 3,0)`);
      }
      await contexto.close();
    }
  }
  // Diferença vivo × esmaecido em tons de cinza no semáforo: v4 ≥ 2 × v3.
  const diferencas = {};
  for (const versao of ["v3", "v4"]) {
    let html = null;
    if (versao === "v3") {
      try { html = execFileSync("git", ["show", COMMIT_V3 + ":src/templates/index.html"], { cwd: RAIZ, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }); } catch (erro) { html = null; }
      if (!html) { console.log("  aviso: v3 indisponível no git; comparação com a v3 pulada"); break; }
    }
    await api(base, "POST", "/api/demonstracao", { cenario: "SECA_EXTREMA" });
    const vivo = await statusAtual(base);
    const imagens = [];
    for (const estado of [vivo, Object.assign({}, vivo, { leitura_desatualizada: true })]) {
      const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
      const pagina = await contexto.newPage();
      if (html) {
        await pagina.route(base + "/", (rota) => rota.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: html }));
        await pagina.route("**/api/status", (rota) => rota.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(estado) }));
        await pagina.goto(base + "/");
        await pagina.waitForFunction(() => !document.body.classList.contains("carregando"), null, { timeout: 8000 }).catch(() => null);
      } else { await abrirComEstado(pagina, base, estado); }
      await pagina.waitForTimeout(300);
      await pagina.evaluate(() => { document.querySelectorAll(".lampada").forEach((l) => { l.style.animation = "none"; }); });
      const caixa = await pagina.locator("#semaforo").boundingBox();
      imagens.push((await pagina.screenshot({ clip: { x: caixa.x - 6, y: caixa.y - 6, width: Math.min(caixa.width + 12, 300), height: Math.min(caixa.height + 12, 130) } })).toString("base64"));
      await contexto.close();
    }
    const analisador = await navegador.newPage();
    diferencas[versao] = await analisador.evaluate(async (pngs) => {
      const dados = [];
      for (const b64 of pngs) {
        const i = new Image(); i.src = "data:image/png;base64," + b64; await i.decode();
        const c = document.createElement("canvas"); c.width = i.width; c.height = i.height; const x = c.getContext("2d"); x.drawImage(i, 0, 0);
        dados.push(x.getImageData(0, 0, c.width, c.height).data);
      }
      let diferentes = 0; const n = Math.min(dados[0].length, dados[1].length) / 4;
      for (let p = 0; p < n; p += 1) {
        const cinza = (d) => 0.2126 * d[p * 4] + 0.7152 * d[p * 4 + 1] + 0.0722 * d[p * 4 + 2];
        if (Math.abs(cinza(dados[0]) - cinza(dados[1])) > 16) { diferentes += 1; }
      }
      return diferentes / n;
    }, imagens);
    await analisador.close();
  }
  if (diferencas.v3 !== undefined) {
    verificar(diferencas.v4 >= 2 * diferencas.v3, `acromatopsia: vivo × esmaecido difere em ${(diferencas.v4 * 100).toFixed(1)} % dos pixels (v3: ${(diferencas.v3 * 100).toFixed(1)} %; meta ≥ 2×)`);
  }
  const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
  const pagina = await contexto.newPage();
  await pagina.goto(base + "/");
  await esperarPronta(pagina);
  await pagina.waitForTimeout(500);
  verificar(await pagina.evaluate(() => getComputedStyle(document.documentElement).overscrollBehaviorY === "contain"), "html { overscroll-behavior-y: contain } (puxar para atualizar sem querer)");
  await contexto.close();
}

/* ------------------------------------------------------------------------ */
async function testarTexto(navegador, base) {
  console.log("\n[texto] A5 — fonte do celular a 130 % e 150 %, AAA na primeira tela");
  for (const [largura, altura] of [[360, 740], [320, 640]]) {
    for (const cenario of ["SECA_EXTREMA", "GEADA"]) {
      await api(base, "POST", "/api/demonstracao", { cenario });
      for (const escala of [130, 150]) {
        const contexto = await navegador.newContext({ viewport: { width: largura, height: altura }, isMobile: true, hasTouch: true });
        const pagina = await novaPagina(contexto);
        await pagina.goto(base + "/");
        await esperarPronta(pagina);
        await pagina.addStyleTag({ content: `html { font-size: ${escala}% !important; }` });
        await pagina.waitForTimeout(300);
        const m = await pagina.evaluate(() => {
          const r = (s) => document.querySelector(s).getBoundingClientRect();
          const a = r(".marca"); const b = r(".cultura-atual"); const barra = r("#barra-voz");
          return { largura: document.documentElement.scrollWidth, cliente: document.documentElement.clientWidth,
            cruza: a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom,
            acoes: r("#acoes").bottom, feito: document.getElementById("feito").hidden ? 0 : r("#feito").bottom, barra: barra.top };
        });
        const rotulo = `${largura}x${altura} ${cenario} ${escala} %`;
        verificar(m.largura <= m.cliente, `${rotulo}: sem rolagem lateral (${m.largura} <= ${m.cliente})`);
        verificar(!m.cruza, `${rotulo}: marca e plantação não se cruzam no topo`);
        if (escala === 130 && largura === 360) {
          verificar(m.acoes <= m.barra + 1 && m.feito <= m.barra + 1, `${rotulo}: ações (${Math.round(m.acoes)}) e Feito (${Math.round(m.feito)}) acima da barra (${Math.round(m.barra)})`);
        }
        if (escala === 130 && largura === 360 && cenario === "GEADA") { await pagina.screenshot({ path: path.join(CAPTURAS, "texto_130_geada_360x740.png") }); }
        await contexto.close();
      }
    }
  }
  for (const cenario of ["SECA_EXTREMA", "ONDA_DE_CALOR", "ATENCAO_MODERADA", "CONDICAO_IDEAL"]) {
    await api(base, "POST", "/api/demonstracao", { cenario });
    const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
    const pagina = await novaPagina(contexto);
    await pagina.goto(base + "/");
    await esperarPronta(pagina);
    await pagina.waitForTimeout(400);
    const textos = (await medidas.medirContraste(pagina, "body")).filter((c) => c.tipo === "texto" && c.acima_dobra);
    const fracos = textos.filter((c) => c.tamanho_px < 16 || c.razao < 7);
    verificar(textos.length > 5 && fracos.length === 0, `${cenario}: ${textos.length} textos na 1ª tela com ≥ 16 px e ≥ 7:1 ${JSON.stringify(fracos.slice(0, 6))}`);
    await contexto.close();
  }
}

/* ------------------------------------------------------------------------ */
async function testarVoz(navegador, base) {
  console.log("\n[voz] A6 — fala curta primeiro, retorno falado e regiões vivas caladas");
  await api(base, "POST", "/api/demonstracao", { cenario: "SECA_EXTREMA" });
  const status = await statusAtual(base);
  {
    const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
    await contexto.addInitScript(dublesDeVoz, ["pt-BR"]);
    const pagina = await novaPagina(contexto);
    await pagina.goto(base + "/");
    await esperarPronta(pagina);
    await pagina.locator("#botao-voz").click();
    await pagina.waitForFunction(() => window.__falas.length >= 1);
    const primeira = await ultimaFala(pagina);
    const frases = primeira.texto.split(/(?<=\.)\s+/);
    verificar(/^Sinal/.test(primeira.texto) && ACAO_NA_FRASE.test(frases[1] || ""), `1º toque: fala curta "${primeira.texto}"`);
    await pagina.locator("#botao-voz").click();
    await pagina.waitForFunction(() => window.__falas.length >= 2);
    const segunda = await ultimaFala(pagina);
    verificar(segunda.texto.indexOf(status.mensagem_voz.slice(0, 40)) !== -1, `2º toque: mensagem completa ("${segunda.texto.slice(0, 60)}…")`);
    await pagina.locator("#botao-voz").click();
    await pagina.waitForFunction(() => window.__falas.length >= 3);
    const terceira = await ultimaFala(pagina);
    verificar(terceira.rate <= 0.85 && terceira.texto.split(/\s+/).length <= 15, `3º toque: devagar (rate ${terceira.rate}) e curta (${terceira.texto.split(/\s+/).length} palavras)`);
    await pagina.reload();
    await esperarPronta(pagina);
    await pagina.locator("#botao-voz").click();
    await pagina.waitForFunction(() => window.__falas.length >= 1);
    verificar((await ultimaFala(pagina)).rate <= 0.85, "depois de recarregar: a preferência de fala lenta foi lembrada");
    verificar(pagina.errosConsole.length === 0, "voz: nenhum erro de console " + pagina.errosConsole.join(" | "));
    await contexto.close();
  }
  {
    const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
    await contexto.addInitScript(dublesDeVoz, ["pt-BR"]);
    await contexto.addInitScript(() => {
      const quebra = { get() { throw new Error("armazenamento bloqueado"); } };
      Object.defineProperty(window, "localStorage", quebra);
      Object.defineProperty(window, "sessionStorage", quebra);
    });
    const pagina = await novaPagina(contexto);
    await pagina.goto(base + "/");
    await esperarPronta(pagina);
    for (let i = 0; i < 3; i += 1) { await pagina.locator("#botao-voz").click(); await pagina.waitForTimeout(50); }
    verificar(pagina.errosConsole.length === 0, "localStorage lançando exceção: nenhum erro de console " + pagina.errosConsole.join(" | "));
    await contexto.close();
  }
  // (b) Retorno falado na 1ª visita: cultura, diálogo, técnico e "Feito" sem rede.
  {
    const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
    await contexto.addInitScript(dublesDeVoz, ["pt-BR"]);
    const pagina = await novaPagina(contexto, [/Failed to load resource/, /ERR_FAILED/]);
    await pagina.goto(base + "/");
    await esperarPronta(pagina);
    await pagina.waitForSelector('.botao-cultura[data-codigo="cafe_conilon"]', { state: "attached" });
    await pagina.locator('.botao-cultura[data-codigo="cafe_conilon"]').click();
    await pagina.waitForSelector("#dialogo", { state: "visible" });
    const textos = () => pagina.evaluate(() => window.__falas.map((f) => f.texto).join(" | "));
    verificar(/Conilon/.test(await textos()), "tocar em Conilon: o nome é falado");
    await pagina.locator("#dialogo-sim").click();
    await pagina.waitForFunction(() => window.__falas.some((f) => /Agora o aparelho cuida do Café Conilon/.test(f.texto)), null, { timeout: 5000 }).catch(() => null);
    verificar(/Agora o aparelho cuida do Café Conilon/.test(await textos()), "confirmar a troca: \"Agora o aparelho cuida do Café Conilon\" falado");
    await api(base, "POST", "/api/cultura", { codigo: "cafe_arabica" });
    await pagina.locator("#tecnico").click();
    await pagina.waitForSelector("#dialogo", { state: "visible" });
    verificar(/Chamar o técnico/.test(await textos()), "tocar em Técnico: o diálogo é falado");
    await pagina.locator("#dialogo-nao").click();
    await pagina.reload();
    await esperarPronta(pagina);
    await pagina.route("**/api/acao_feita", (rota) => rota.abort("failed"));
    await pagina.locator("#feito").click();
    await pagina.waitForFunction(() => window.__falas.some((f) => /Anotado/.test(f.texto)), null, { timeout: 5000 }).catch(() => null);
    verificar(/Anotado/.test(await textos()), "\"Feito\" sem rede: \"Anotado\" falado");
    await pagina.unroute("**/api/acao_feita");
    await pagina.evaluate(() => localStorage.removeItem("agro_feitos"));
    await contexto.close();
  }
  // (c) Ajuda montada com o que está na tela.
  for (const [cenario, comFeito] of [["SECA_EXTREMA", true], ["CONDICAO_IDEAL", false]]) {
    await api(base, "POST", "/api/demonstracao", { cenario });
    const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
    await contexto.addInitScript(dublesDeVoz, ["pt-BR"]);
    const pagina = await novaPagina(contexto);
    await pagina.goto(base + "/");
    await esperarPronta(pagina);
    await pagina.locator("#ajuda").click();
    await pagina.waitForFunction(() => window.__falas.length >= 1);
    const fala = (await ultimaFala(pagina)).texto;
    const ok = /Técnico/.test(fala) && /ouvir de novo/.test(fala) && /O desenho grande diz o motivo/.test(fala) && (/Feito/.test(fala) === comFeito);
    verificar(ok, `${cenario}: ajuda "${fala}"`);
    // (d) A causa tem o selo de som (parece tocável).
    verificar((await pagina.locator("#causa use[href='#i-som']").count()) === 1, `${cenario}: #causa com o selo de som`);
    await contexto.close();
  }
  // (e) Regiões vivas: 10 min com o estado parado = 0 mutações; banca ≤ 1 lote em 30 s; mudança anuncia.
  {
    await api(base, "POST", "/api/demonstracao", { cenario: "ATENCAO_MODERADA" });
    const parado = Object.assign(await statusAtual(base), { demonstracao_ativa: false, segundos_restantes_demonstracao: 0 });
    const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
    const pagina = await novaPagina(contexto);
    await pagina.clock.install();
    await abrirComEstado(pagina, base, parado);
    await pagina.evaluate(() => {
      window.__mutacoes = 0;
      const observador = new MutationObserver((lista) => { window.__mutacoes += lista.length; });
      document.querySelectorAll("[aria-live],[role=status],[role=alert]").forEach((e) => observador.observe(e, { childList: true, characterData: true, subtree: true }));
    });
    await pagina.clock.runFor(10 * 60 * 1000);
    verificar((await pagina.evaluate(() => window.__mutacoes)) === 0, `10 min com o estado parado: ${await pagina.evaluate(() => window.__mutacoes)} mutações nas regiões vivas`);
    await contexto.close();
  }
  {
    await api(base, "POST", "/api/demonstracao", { cenario: "GEADA" });
    const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
    const pagina = await novaPagina(contexto);
    await pagina.goto(base + "/?banca=1");
    await esperarPronta(pagina);
    await pagina.evaluate(() => {
      window.__lotes = 0;
      new MutationObserver(() => { window.__lotes += 1; }).observe(document.querySelector(".faixa-demo"), { childList: true, characterData: true, subtree: true });
    });
    await pagina.waitForTimeout(30000);
    const lotes = await pagina.evaluate(() => window.__lotes);
    verificar(lotes <= 1, `faixa da demonstração: ${lotes} lote(s) de mutação em 30 s`);
    await contexto.close();
  }
  {
    await api(base, "POST", "/api/demonstracao", { cenario: "CONDICAO_IDEAL" });
    const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
    const pagina = await novaPagina(contexto);
    await pagina.goto(base + "/");
    await esperarPronta(pagina);
    await pagina.evaluate(() => { window.__anuncios = 0; new MutationObserver((l) => { window.__anuncios += l.length; }).observe(document.getElementById("anunciador"), { childList: true, characterData: true, subtree: true }); });
    await api(base, "POST", "/api/demonstracao", { cenario: "SECA_EXTREMA" });
    await pagina.waitForFunction(() => window.__anuncios >= 1, null, { timeout: 9000 }).catch(() => null);
    const anuncio = await pagina.locator("#anunciador").textContent();
    verificar((await pagina.evaluate(() => window.__anuncios)) >= 1 && /Perigo/.test(anuncio), `verde → vermelho: #anunciador recebe "${anuncio}"`);
    await contexto.close();
  }
}

/* ------------------------------------------------------------------------ */
async function testarFeitoEDialogo(navegador, base, banco) {
  console.log("\n[feito] A7 — desfazer, sem duplicata, diálogo modal nativo e PIN errado claro");
  await api(base, "POST", "/api/demonstracao", { cenario: "GEADA" });
  {
    const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
    await contexto.addInitScript(dublesDeVoz, ["pt-BR"]);
    const pagina = await novaPagina(contexto);
    await pagina.goto(base + "/");
    await esperarPronta(pagina);
    await pagina.locator("#feito").click();
    const desfazer = await pagina.waitForSelector("[aria-label='Desfazer']", { state: "visible", timeout: 1000 }).then((e) => e.boundingBox()).catch(() => null);
    verificar(!!desfazer && desfazer.width >= 48 && desfazer.height >= 48, `"Feito" vira "Desfazer" em até 1 s com ${desfazer ? Math.round(desfazer.width) + "x" + Math.round(desfazer.height) : "—"} px`);
    await pagina.waitForFunction(() => window.__falas.some((f) => /desfazer\.$/.test(f.texto)), null, { timeout: 4000 }).catch(() => null);
    verificar(await pagina.evaluate(() => window.__falas.some((f) => /^Anotado: .*Se não fez, toque de novo para desfazer\.$/.test(f.texto))), "confirmação falada termina com \"Se não fez, toque de novo para desfazer.\"");
    await pagina.screenshot({ path: path.join(CAPTURAS, "feito_desfazer_360x740.png") });
    await pagina.waitForTimeout(900);
    await pagina.locator("[aria-label='Desfazer']").click();
    await pagina.waitForFunction(() => window.__falas.some((f) => /desfeito/.test(f.texto)), null, { timeout: 4000 }).catch(() => null);
    verificar((await statusAtual(base)).causa_silenciada === false && /desfeito/.test((await ultimaFala(pagina)).texto), "tocar em Desfazer: causa_silenciada false e \"Pronto, desfeito.\" falado");
    // Três toques seguidos (sem querer): um registro só e, depois de 16 s, "Feito às HH:MM".
    await pagina.locator("#feito").click({ clickCount: 1 });
    await pagina.locator("#feito").click();
    await pagina.locator("#feito").click();
    await pagina.waitForTimeout(16000);
    const m = await pagina.evaluate(() => ({ texto: document.getElementById("feito").textContent, desativado: document.getElementById("feito").getAttribute("aria-disabled"), estado: document.getElementById("feito").dataset.estadoFeito }));
    verificar(/Feito.*\d{2}:\d{2}/.test(m.texto) && m.desativado === "true" && m.estado === "feito", `3 toques e 16 s: ${JSON.stringify(m)}`);
    await pagina.screenshot({ path: path.join(CAPTURAS, "feito_marcado_360x740.png") });
    verificar(pagina.errosConsole.length === 0, "feito: nenhum erro de console " + pagina.errosConsole.join(" | "));
    await contexto.close();
  }
  {
    const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
    await contexto.addInitScript(dublesDeVoz, ["pt-BR"]);
    const pagina = await novaPagina(contexto);
    await pagina.goto(base + "/");
    await esperarPronta(pagina);
    await pagina.locator("#tecnico").focus();
    await pagina.keyboard.press("Enter");
    await pagina.waitForSelector("#dialogo", { state: "visible" });
    const dentro = [];
    dentro.push(await pagina.evaluate(() => document.getElementById("dialogo").contains(document.activeElement)));
    for (let i = 0; i < 6; i += 1) {
      await pagina.keyboard.press("Tab");
      dentro.push(await pagina.evaluate(() => document.getElementById("dialogo").contains(document.activeElement)));
    }
    const modal = await pagina.evaluate(() => document.querySelector("#dialogo").matches(":modal"));
    verificar(dentro.every(Boolean) && modal, `diálogo modal nativo com o foco preso em 6 Tabs (${dentro.join(",")}, :modal ${modal})`);
    await pagina.screenshot({ path: path.join(CAPTURAS, "dialogo_tecnico_360x740.png") });
    await pagina.keyboard.press("Escape");
    await pagina.waitForTimeout(150);
    const depois = await pagina.evaluate(() => ({ aberto: document.getElementById("dialogo").open, foco: document.activeElement.id }));
    verificar(!depois.aberto && depois.foco === "tecnico", `Esc fecha e devolve o foco a #tecnico ${JSON.stringify(depois)}`);
    const cores = await pagina.evaluate(() => {
      const raiz = getComputedStyle(document.documentElement);
      const resolver = (v) => { const d = document.createElement("div"); d.style.backgroundColor = raiz.getPropertyValue(v); document.body.appendChild(d); const c = getComputedStyle(d).backgroundColor; d.remove(); return c; };
      return { nao: getComputedStyle(document.getElementById("dialogo-nao")).backgroundColor, vermelho: resolver("--vermelho"), texto: resolver("--vermelho-texto") };
    });
    verificar(cores.nao !== cores.vermelho && cores.nao !== cores.texto, `"Não" sem a cor de perigo ${JSON.stringify(cores)}`);
    await contexto.close();
  }
  // PIN errado: o diálogo fica aberto com a mensagem na linha, o foco volta à senha e os botões dizem Entrar/Cancelar.
  execFileSync(PYTHON, [path.join("src", "seguranca_banca.py"), "--definir-pin", "1234", "--banco", banco], { cwd: RAIZ });
  try {
    const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
    await contexto.addInitScript(dublesDeVoz, ["pt-BR"]);
    const pagina = await novaPagina(contexto, [/status of 403/, /status of 429/]);
    await pagina.goto(base + "/?banca=1");
    await pagina.waitForSelector(".botao-cenario", { state: "visible" });
    await pagina.locator('.botao-cenario[data-codigo="ENCHARCAMENTO"]').click();
    await pagina.waitForSelector("#dialogo-pin", { state: "visible" });
    await pagina.locator("#dialogo-pin").fill("0000");
    await pagina.locator("#dialogo-sim").click();
    await pagina.waitForSelector("#dialogo-erro", { state: "visible", timeout: 5000 }).catch(() => null);
    const m = await pagina.evaluate(() => ({ aberto: document.getElementById("dialogo").open, erro: document.getElementById("dialogo-erro").textContent,
      foco: document.activeElement.id, sim: document.getElementById("dialogo-sim").textContent, nao: document.getElementById("dialogo-nao").textContent }));
    verificar(m.aberto && /Senha errada/.test(m.erro) && m.foco === "dialogo-pin" && /Entrar/.test(m.sim) && /Cancelar/.test(m.nao), `PIN errado: ${JSON.stringify(m)}`);
    verificar(/Senha errada/.test((await ultimaFala(pagina)).texto), "PIN errado: a mensagem é falada");
    await pagina.screenshot({ path: path.join(CAPTURAS, "pin_errado_360x740.png") });
    await pagina.locator("#dialogo-pin").fill("1234");
    await pagina.locator("#dialogo-sim").click();
    await pagina.waitForFunction(() => !document.getElementById("dialogo").open, null, { timeout: 5000 }).catch(() => null);
    verificar((await statusAtual(base)).cenario_demonstracao === "ENCHARCAMENTO", "PIN certo depois do errado: o cenário entra");
    // Bloqueio: a contagem regressiva aparece no botão.
    for (let i = 0; i < 5; i += 1) { await api(base, "POST", "/api/demonstracao/encerrar", {}, { cabecalhos: { "X-PIN-Banca": "0000" } }); }
    await pagina.evaluate(() => sessionStorage.clear());
    await pagina.locator('.botao-cenario[data-codigo="GEADA"]').click();
    await pagina.waitForSelector("#dialogo", { state: "visible" });
    await pagina.waitForTimeout(300);
    const bloqueio = await pagina.locator("#dialogo-sim").textContent();
    verificar(/Espere \d+ segundos/.test(bloqueio), `429: contagem regressiva "${bloqueio}"`);
    await pagina.keyboard.press("Escape");
    verificar(pagina.errosConsole.length === 0, "PIN: nenhum erro de console além dos 403/429 esperados " + pagina.errosConsole.join(" | "));
    await contexto.close();
  } finally {
    execFileSync(PYTHON, [path.join("src", "seguranca_banca.py"), "--remover-pin", "--banco", banco], { cwd: RAIZ });
  }
}

/* ------------------------------------------------------------------------ */
async function testarPictogramas(navegador, base) {
  console.log("\n[pictogramas] A8 — símbolos distintos a 24 px, mapas injetivos e banca só de demonstração");
  const fonte = fs.readFileSync(path.join(RAIZ, "src", "templates", "index.html"), "utf8");
  const ler = (nome) => Function("return " + fonte.match(new RegExp("var " + nome + " = (\\{[\\s\\S]*?\\});"))[1])();
  const ACOES = ler("ACOES");
  const CAUSAS = ler("CAUSAS");
  const ICONES_CULTURA = ler("ICONES_CULTURA");
  const unico = (lista) => new Set(lista).size === lista.length;
  verificar(unico(Object.values(ACOES).map((a) => a.join("+"))), "ACOES (desenho + selo) é injetivo");
  verificar(unico(Object.values(CAUSAS)), "CAUSAS é injetivo");
  verificar(unico(Object.values(ICONES_CULTURA)), "ICONES_CULTURA é injetivo (Arábica ≠ Conilon)");
  const simbolos = Array.from(new Set(Object.values(ACOES).map((a) => a[0]).concat(Object.values(ACOES).map((a) => a[1]).filter(Boolean))));
  const contexto = await navegador.newContext({ viewport: { width: 360, height: 740 } });
  const pagina = await novaPagina(contexto);
  await pagina.goto(base + "/");
  await esperarPronta(pagina);
  const tintas = await pagina.evaluate(async (nomes) => {
    const saida = {};
    for (const nome of nomes) {
      const simbolo = document.getElementById("i-" + nome);
      const svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="#000" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="color:#000">'
        + simbolo.innerHTML.replace(/currentColor/g, "#000") + "</svg>";
      const imagem = new Image();
      imagem.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
      await imagem.decode();
      const tela = document.createElement("canvas"); tela.width = 24; tela.height = 24;
      const ctx = tela.getContext("2d"); ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, 24, 24); ctx.drawImage(imagem, 0, 0, 24, 24);
      const dados = ctx.getImageData(0, 0, 24, 24).data;
      const tinta = [];
      for (let i = 0; i < 576; i += 1) { tinta.push((dados[i * 4] + dados[i * 4 + 1] + dados[i * 4 + 2]) / 3 < 128 ? 1 : 0); }
      saida[nome] = tinta;
    }
    return saida;
  }, simbolos);
  const parecidos = [];
  for (let i = 0; i < simbolos.length; i += 1) {
    for (let j = i + 1; j < simbolos.length; j += 1) {
      const a = tintas[simbolos[i]]; const b = tintas[simbolos[j]];
      let uniao = 0; let diferente = 0;
      for (let p = 0; p < 576; p += 1) { if (a[p] || b[p]) { uniao += 1; if (a[p] !== b[p]) { diferente += 1; } } }
      const fracao = diferente / Math.max(1, uniao);
      if (fracao < 0.4) { parecidos.push(`${simbolos[i]}×${simbolos[j]} ${(fracao * 100).toFixed(1)} %`); }
    }
  }
  const nascendo = tintas["sol-nascendo"]; const poente = tintas["sol-poente"];
  let u = 0; let d = 0;
  for (let p = 0; p < 576; p += 1) { if (nascendo[p] || poente[p]) { u += 1; if (nascendo[p] !== poente[p]) { d += 1; } } }
  verificar(d / u >= 0.4, `manhã × tardinha diferem em ${(d / u * 100).toFixed(1)} % dos pixels com tinta`);
  verificar(parecidos.length === 0, `${simbolos.length} símbolos de ação e de "quando": todos os pares diferem em ≥ 40 % ${parecidos.join("; ")}`);
  await pagina.goto(base + "/?banca=1");
  await pagina.waitForSelector(".botao-cenario", { state: "visible" });
  verificar((await pagina.locator("#b-seca, #b-florada").count()) === 0, "/?banca=1 sem #b-seca nem #b-florada (foram para /tecnico)");
  // Folha de contato dos pictogramas (24 px e 72 px) para a revisão visual.
  await pagina.setContent("<!doctype html><body style='margin:12px;font:14px sans-serif'></body>");
  const sprite = fonte.match(/<svg hidden>[\s\S]*?<\/svg>/)[0].replace("<svg hidden>", "<svg width=\"0\" height=\"0\" style=\"position:absolute\">");
  await pagina.evaluate(({ sprite, nomes }) => {
    document.body.insertAdjacentHTML("beforeend", sprite);
    nomes.forEach((n) => document.body.insertAdjacentHTML("beforeend", `<div style="display:inline-flex;flex-direction:column;align-items:center;width:96px;margin:6px"><svg width="72" height="72" fill="none" stroke="#5c3f00" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="color:#5c3f00"><use href="#i-${n}"/></svg><svg width="24" height="24" fill="none" stroke="#000" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><use href="#i-${n}"/></svg>${n}</div>`));
  }, { sprite, nomes: simbolos.concat(["seca", "encharcado", "floco", "sol", "cafe-arabica", "cafe-conilon", "desfazer"]) });
  await pagina.screenshot({ path: path.join(CAPTURAS, "pictogramas.png"), fullPage: true });
  await contexto.close();
}

/* ------------------------------------------------------------------------ */
async function principal() {
  const pedidos = process.argv.slice(2);
  const casos = { rede: testarRede, oclusao: testarOclusao, confianca: testarConfianca, robustez: testarRobustez, texto: testarTexto,
    voz: testarVoz, feito: testarFeitoEDialogo, "feito-dialogo": testarFeitoEDialogo, pictogramas: testarPictogramas,
    acessibilidade: testarAcessibilidade };
  // "feito" é apelido de "feito-dialogo": a lista completa roda cada caso uma vez só.
  const escolhidos = pedidos.length ? pedidos : Object.keys(casos).filter((nome) => nome !== "feito");
  fs.mkdirSync(CAPTURAS, { recursive: true });
  const pasta = fs.mkdtempSync(path.join(os.tmpdir(), "agro_ux_v4_"));
  const banco = path.join(pasta, "ux.db");
  const { processo, base } = await subirServidor(banco);
  const navegador = await chromium.launch();
  try {
    for (const nome of escolhidos) {
      if (!casos[nome]) { throw new Error("Caso desconhecido: " + nome + " (use " + Object.keys(casos).join(", ") + ")"); }
      await casos[nome](navegador, base, banco);
    }
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
