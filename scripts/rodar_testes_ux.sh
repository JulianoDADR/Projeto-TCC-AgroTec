#!/usr/bin/env bash
# Roda todos os testes da interface do agricultor:
#   1. testes unitários da interface (python -m unittest tests.test_interface_campo
#      tests.test_interface_retorno: causa, "Feito", alarme local, técnico e calibração);
#   2. testes de ponta a ponta no navegador (node tests/e2e/ux_campo.spec.js),
#      que sobem o servidor num banco temporário e gravam capturas em reports/ux/;
#   3. testes da v4: página do agricultor (tests/e2e/agricultor_v4.spec.js, se
#      existir), página do técnico (tests/e2e/tecnico_v4.spec.js e
#      python -m unittest tests.test_tecnico_v4);
#   4. a suíte permanente de validação UX (scripts/validar_ux.sh), que compara
#      com as metas da v4 e grava reports/ux4/<data>/resultado.json.
# Pré-requisitos: Python com as dependências do projeto e Node com o pacote
# "playwright" (Chromium instalado); `npm install` com o package.json da raiz
# instala as versões fixas e copia o axe-core para tests/e2e/vendor/. axe-core e
# Lighthouse são opcionais ("PULADO: ... ausente"; AGRO_UX_ESTRITO=1 exige os dois).
# Uso: scripts/rodar_testes_ux.sh   (código de saída 0 = tudo certo)
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RAIZ"
PYTHON="${PYTHON:-python}"
export PYTHON

if ! command -v node >/dev/null 2>&1; then
  echo "Node não encontrado: instale o Node.js para rodar os testes no navegador." >&2
  exit 2
fi
if ! node -e "require('playwright')" >/dev/null 2>&1; then
  echo "Pacote 'playwright' não encontrado para o Node (npm install playwright)." >&2
  exit 2
fi
if [ ! -f models/modelo_agroanalise.pkl ]; then
  echo "Aviso: modelo de IA ainda não treinado; os testes usam só as regras agronômicas." >&2
fi

echo "== Testes unitários da interface =="
"$PYTHON" -m unittest tests.test_interface_campo tests.test_interface_retorno -v

echo
echo "== Testes de ponta a ponta (Playwright) =="
node tests/e2e/ux_campo.spec.js

echo
echo "== v4: página do agricultor =="
if [ -f tests/e2e/agricultor_v4.spec.js ]; then
  node tests/e2e/agricultor_v4.spec.js
else
  echo "tests/e2e/agricultor_v4.spec.js ainda não existe (frente A): pulado."
fi

echo
echo "== v4: página do técnico =="
"$PYTHON" -m unittest tests.test_tecnico_v4 -v
node tests/e2e/tecnico_v4.spec.js

echo
echo "== v4: suíte de validação UX (scripts/validar_ux.sh) =="
scripts/validar_ux.sh
