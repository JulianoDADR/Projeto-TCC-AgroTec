#!/usr/bin/env bash
# Suíte permanente de validação UX da v4 (um comando, offline).
#
# Sobe o servidor de estados (tests/e2e/validacao/servidor_estados.py) num banco
# TEMPORÁRIO e numa porta própria (nunca a 5000), percorre a matriz de estados
# no Chromium do Playwright (contraste, alvos, oclusão, foco, pixels das
# lâmpadas, regiões vivas, retorno, bytes, pictogramas, axe-core e Lighthouse),
# confere coerência e legibilidade da voz em Python e compara tudo com os
# limiares de tests/e2e/validacao/metas_v4.json.
#
# Uso:
#   scripts/validar_ux.sh                     # código 0 = todas as metas atendidas; 1 = alguma falhou
#   scripts/validar_ux.sh --linha-de-base     # só grava o relatório (registrar a v3; sai com 0)
#   scripts/validar_ux.sh --projeto <pasta>   # mede outra cópia do projeto (ex.: a v3 em 5c66abe)
#   scripts/validar_ux.sh --grupos grade,foco # só alguns grupos (depuração; metas sem medida ficam puladas)
#   scripts/validar_ux.sh --saida <pasta>     # pasta do relatório (padrão reports/ux4/<data>)
# Variáveis:
#   AGRO_UX_PORTA (padrão 5210; 5000 é recusada), AGRO_AXE (axe.min.js), AGRO_LIGHTHOUSE
#   (executável), CHROME_PATH (Chromium do Lighthouse; padrão o do Playwright), PYTHON
#   e AGRO_UX_ESTRITO=1 (ferramenta ausente vira falha em vez de "PULADO").
# Relatório: reports/ux4/<data>/resultado.json, com as capturas das metas que falharam.
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python}"
PORTA="${AGRO_UX_PORTA:-5210}"
LINHA_DE_BASE=0
PROJETO=""
GRUPOS=""
SAIDA=""

while [ $# -gt 0 ]; do
  case "$1" in
    --linha-de-base) LINHA_DE_BASE=1 ;;
    --projeto) PROJETO="$(cd "$2" && pwd)"; shift ;;
    --grupos) GRUPOS="$2"; shift ;;
    --saida) SAIDA="$2"; shift ;;
    -h|--help) sed -n '2,24p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "Opção desconhecida: $1 (veja --help)" >&2; exit 2 ;;
  esac
  shift
done

if [ "$PORTA" = "5000" ]; then
  echo "A porta 5000 é a do servidor do sítio: use outra em AGRO_UX_PORTA." >&2
  exit 2
fi
if ! command -v node >/dev/null 2>&1; then
  echo "Node não encontrado: instale o Node.js para rodar a suíte de validação UX." >&2
  exit 2
fi
if ! node -e "require('playwright')" >/dev/null 2>&1; then
  echo "Pacote 'playwright' não encontrado para o Node (npm install, com o package.json da raiz)." >&2
  exit 2
fi

if [ -n "$PROJETO" ]; then
  export AGRO_UX_PROJETO="$PROJETO"
  echo "Projeto medido: $PROJETO"
fi
if [ -z "$SAIDA" ]; then
  SAIDA="$RAIZ/reports/ux4/$(date +%Y-%m-%d_%H%M%S)"
fi
mkdir -p "$SAIDA"
SAIDA="$(cd "$SAIDA" && pwd)"

# Porta ocupada (por outro processo): pega uma livre, nunca a 5000.
if ! "$PYTHON" - "$PORTA" <<'FIM' >/dev/null 2>&1
import socket, sys
s = socket.socket()
s.bind(("127.0.0.1", int(sys.argv[1])))
s.close()
FIM
then
  PORTA="$("$PYTHON" - <<'FIM'
import socket
while True:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    porta = s.getsockname()[1]
    s.close()
    if porta != 5000:
        print(porta)
        break
FIM
)"
fi
BASE="http://127.0.0.1:$PORTA"

LOG_SERVIDOR="$SAIDA/servidor_estados.log"
"$PYTHON" "$RAIZ/tests/e2e/validacao/servidor_estados.py" "$PORTA" >"$LOG_SERVIDOR" 2>&1 &
PID_SERVIDOR=$!
encerrar() { kill "$PID_SERVIDOR" 2>/dev/null || true; wait "$PID_SERVIDOR" 2>/dev/null || true; }
trap encerrar EXIT INT TERM

for _ in $(seq 1 120); do
  if node -e "fetch('$BASE/__ux/estados').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" 2>/dev/null; then
    break
  fi
  if ! kill -0 "$PID_SERVIDOR" 2>/dev/null; then
    echo "O servidor de estados não subiu:" >&2
    cat "$LOG_SERVIDOR" >&2
    exit 2
  fi
  sleep 0.5
done
echo "Servidor de estados em $BASE (log: $LOG_SERVIDOR)"
echo "Relatório em $SAIDA"

ARGS_MEDIR=(medir --base "$BASE" --saida "$SAIDA")
if [ -n "$GRUPOS" ]; then
  ARGS_MEDIR+=(--grupos "$GRUPOS")
fi
node "$RAIZ/tests/e2e/validacao/matriz_estados.js" "${ARGS_MEDIR[@]}"

echo
echo "== Coerência cor × chip × título × voz × pictograma × causa"
"$PYTHON" "$RAIZ/tests/e2e/validacao/coerencia.py" "$SAIDA"
echo
echo "== Legibilidade e conteúdo da voz"
"$PYTHON" "$RAIZ/tests/e2e/validacao/legibilidade.py" "$SAIDA"

encerrar
trap - EXIT INT TERM

echo
echo "== Metas da v4 (tests/e2e/validacao/metas_v4.json)"
ARGS_COMPARAR=(comparar --saida "$SAIDA")
if [ "$LINHA_DE_BASE" = "1" ]; then
  ARGS_COMPARAR+=(--linha-de-base)
fi
set +e
node "$RAIZ/tests/e2e/validacao/matriz_estados.js" "${ARGS_COMPARAR[@]}"
CODIGO=$?
set -e
exit "$CODIGO"
