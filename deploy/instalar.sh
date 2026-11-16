#!/usr/bin/env bash
# =============================================================================
#  AgroAnálise — instalador para Linux com systemd (Raspberry Pi OS, Debian,
#  Ubuntu). Deixa o coletor, o servidor web e o alarme sonoro local rodando
#  24/7, reiniciando sozinhos depois de falha ou queda de energia. Instala TODAS
#  as unidades deploy/agroanalise-*.service e deploy/agroanalise-*.timer (por
#  glob): um serviço novo de qualquer frente entra sem mexer neste arquivo.
#
#  Uso (como root, a partir da pasta do projeto):
#      sudo bash deploy/instalar.sh
#  Variáveis opcionais:
#      AGRO_DESTINO=/opt/agroanalise   pasta da instalação
#      AGRO_USUARIO=agroanalise        usuário de serviço (sem login)
#      AGRO_SEM_SYSTEMD=1              só prepara arquivos (não ativa serviços)
#
#  IDEMPOTENTE: pode ser rodado de novo para atualizar o código; não apaga o
#  banco (data/), o modelo (models/) nem os logs (logs/).
# =============================================================================
set -euo pipefail

ORIGEM="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESTINO="${AGRO_DESTINO:-/opt/agroanalise}"
USUARIO="${AGRO_USUARIO:-agroanalise}"
PASTA_UNITS="/etc/systemd/system"
# Unidades systemd do projeto, achadas por glob (serviços e temporizadores).
shopt -s nullglob
UNIDADES=("$ORIGEM"/deploy/agroanalise-*.service "$ORIGEM"/deploy/agroanalise-*.timer)
shopt -u nullglob
[[ ${#UNIDADES[@]} -gt 0 ]] || { printf '[instalar] ERRO: nenhuma unidade deploy/agroanalise-*.service\n' >&2; exit 1; }

informar() { printf '[instalar] %s\n' "$*"; }
falhar() { printf '[instalar] ERRO: %s\n' "$*" >&2; exit 1; }

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    falhar "rode como root: sudo bash deploy/instalar.sh"
fi
command -v python3 >/dev/null 2>&1 || falhar "python3 não encontrado (apt install python3 python3-venv)."

# 1. Usuário de serviço, com acesso à porta serial (grupo dialout).
if ! id -u "$USUARIO" >/dev/null 2>&1; then
    informar "criando o usuário de serviço '$USUARIO'"
    useradd --system --home-dir "$DESTINO" --no-create-home --shell /usr/sbin/nologin "$USUARIO"
fi
if getent group dialout >/dev/null 2>&1; then
    usermod -aG dialout "$USUARIO"
fi
# Alto-falante do alarme local (ALSA).
if getent group audio >/dev/null 2>&1; then
    usermod -aG audio "$USUARIO"
fi

# 2. Código na pasta de destino (preserva data/, models/, reports/, logs/ e .venv/).
mkdir -p "$DESTINO"
if [[ "$(cd "$DESTINO" && pwd)" != "$ORIGEM" ]]; then
    informar "copiando o projeto de $ORIGEM para $DESTINO"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete \
            --exclude '.git/' --exclude '.venv/' --exclude 'data/' --exclude 'models/' \
            --exclude 'reports/' --exclude 'logs/' --exclude '__pycache__/' \
            "$ORIGEM/" "$DESTINO/"
    else
        for item in src arduino deploy docs tests scripts referencia_climatica requirements.txt requirements-referencia.txt README.md CHANGELOG.md; do
            if [[ -e "$ORIGEM/$item" ]]; then
                rm -rf "${DESTINO:?}/$item"
                cp -a "$ORIGEM/$item" "$DESTINO/"
            fi
        done
    fi
fi
mkdir -p "$DESTINO/data" "$DESTINO/models" "$DESTINO/reports" "$DESTINO/logs"

# 3. Ambiente virtual e dependências.
if [[ ! -x "$DESTINO/.venv/bin/python" ]]; then
    informar "criando o ambiente virtual em $DESTINO/.venv"
    python3 -m venv "$DESTINO/.venv"
fi
informar "instalando as dependências (requirements.txt)"
"$DESTINO/.venv/bin/python" -m pip install --upgrade pip >/dev/null
"$DESTINO/.venv/bin/python" -m pip install -r "$DESTINO/requirements.txt"

chown -R "$USUARIO":"$USUARIO" "$DESTINO"

# 4. Pipeline de dados e IA, só se o modelo ainda não existir.
PY="$DESTINO/.venv/bin/python"
como_servico() { runuser -u "$USUARIO" -- "$@"; }
if [[ ! -f "$DESTINO/models/modelo_agroanalise.pkl" ]]; then
    informar "primeira instalação: gerando banco, dataset e modelo de IA (alguns minutos)"
    (cd "$DESTINO" && como_servico "$PY" src/database.py && como_servico "$PY" src/gerador_dataset.py \
        && como_servico "$PY" src/treinamento_ia.py)
else
    informar "modelo de IA já existe; só garantindo o banco e as migrações"
    (cd "$DESTINO" && como_servico "$PY" src/database.py >/dev/null)
fi

if [[ "${AGRO_SEM_SYSTEMD:-0}" == "1" ]]; then
    informar "AGRO_SEM_SYSTEMD=1: serviços não ativados. Instalação de arquivos concluída."
    exit 0
fi
command -v systemctl >/dev/null 2>&1 || falhar "systemctl não encontrado: este sistema não usa systemd."

# 5. Unidades systemd (reinício automático, sem limite de tentativas).
#    Serviço com temporizador de mesmo nome (ex.: agroanalise-desempenho) é
#    disparado só pelo .timer; os demais serviços ficam ligados 24/7.
ATIVAR=()
for unidade in "${UNIDADES[@]}"; do
    nome="$(basename "$unidade")"
    sed -e "s#/opt/agroanalise#${DESTINO}#g" \
        -e "s#^User=agroanalise#User=${USUARIO}#" \
        -e "s#^Group=agroanalise#Group=${USUARIO}#" \
        "$DESTINO/deploy/${nome}" > "$PASTA_UNITS/${nome}"
    chmod 0644 "$PASTA_UNITS/${nome}"
    if [[ "$nome" == *.service && -e "$ORIGEM/deploy/${nome%.service}.timer" ]]; then
        continue
    fi
    ATIVAR+=("$nome")
done
systemctl daemon-reload
for nome in "${ATIVAR[@]}"; do
    informar "ativando $nome"
    systemctl enable --now "$nome"
    if [[ "$nome" == *.service ]]; then
        systemctl restart "$nome"
    fi
done

# 6. Relógio certo (NTP): as leituras têm data/hora e a demonstração tem prazo.
if command -v timedatectl >/dev/null 2>&1; then
    timedatectl set-ntp true || informar "aviso: não foi possível ligar o NTP (timedatectl set-ntp true)."
fi

ENDERECO="$(hostname -I 2>/dev/null | awk '{print $1}')"
informar "pronto! Abra no celular, na mesma rede: http://${ENDERECO:-<ip-deste-computador>}:5000"
informar "situação: systemctl status 'agroanalise-*'"
informar "teste do alto-falante: sudo -u ${USUARIO} ${PY} ${DESTINO}/src/alarme_local.py --testar-som"
