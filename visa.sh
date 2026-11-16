#!/usr/bin/env bash
# Centro Logístico de Simulação Meteorológica
# Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo
#
# Orquestração do CLS Visa: instalar, rodar, parar, testar.
#
#   ./visa.sh <comando>        (pode ser chamado de qualquer pasta)
#
# Comandos:
#   instalar    instala as dependências no .venv (requirements e requirements-dev)
#   rodar       roda em primeiro plano (Ctrl+C para parar)
#   iniciar     roda em segundo plano; saída em logs/visa.log (outra porta: logs/visa.<porta>.log)
#   parar       para o que foi iniciado com "iniciar"
#   reiniciar   parar + iniciar
#   estado      está rodando? responde? .env, vento e espaço aéreo presentes?
#   log         acompanha o log (Ctrl+C para sair)
#   testar      roda os testes (argumentos extras vão para o pytest)
#   verificar   ruff + testes: rodar antes de cada commit
#   avaliar     FAB.ia: acertos e alarmes falsos dos conflitos previstos [AAAA-MM-DD ...]
#
# A porta vem de VISA_PORTA (ambiente ou .env); padrão 8090.

set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$RAIZ"                      # o NiceGUI grava .nicegui/ na pasta atual: sempre a raiz

PY="$RAIZ/.venv/bin/python"
LOGS="$RAIZ/logs"

# --- auxiliares --------------------------------------------------------------

erro() { echo "erro: $*" >&2; exit 1; }

porta() {
    # ambiente > .env > 8090
    if [[ -n "${VISA_PORTA:-}" ]]; then echo "$VISA_PORTA"; return; fi
    local p
    p="$(grep -E '^VISA_PORTA=' .env 2>/dev/null | tail -1 | cut -d= -f2 | tr -d ' "'"'" || true)"
    echo "${p:-8090}"
}

# Cada porta tem o seu PID, o seu log e os seus registros do FAB.ia: uma instância de teste
# (VISA_PORTA=8091) não se confunde com a principal nem mistura as estatísticas dela.
# A porta 8090 mantém os nomes sem sufixo.
SUFIXO=""; [[ "$(porta)" == 8090 ]] || SUFIXO=".$(porta)"
PIDS="$LOGS/visa$SUFIXO.pid"
LOG="$LOGS/visa$SUFIXO.log"
export VISA_PREVISAO_DIR="${VISA_PREVISAO_DIR:-$LOGS/previsao$SUFIXO}"

exige_venv() { [[ -x "$PY" ]] || erro ".venv não encontrado em $RAIZ (crie com: python3 -m venv .venv)"; }
exige_env()  { [[ -f .env ]]  || erro ".env não encontrado (copie .env.exemplo para .env e preencha)"; }

pid_ativo() {
    # imprime o PID se o servidor iniciado por este script ainda estiver rodando
    [[ -f "$PIDS" ]] || return 1
    local pid; pid="$(cat "$PIDS")"
    # confere se o PID ainda é o nosso (o sistema reaproveita números de processo)
    if kill -0 "$pid" 2>/dev/null && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q 'visa/main.py'; then
        echo "$pid"; return 0
    fi
    rm -f "$PIDS"; return 1
}

responde() { curl -s -o /dev/null --max-time 3 "http://localhost:$(porta)/"; }

# --- comandos ----------------------------------------------------------------

instalar() {
    [[ -x "$PY" ]] || python3 -m venv .venv
    "$PY" -m pip install -r requirements.txt -r requirements-dev.txt
}

rodar() {
    exige_venv; exige_env
    echo "CLS Visa em http://localhost:$(porta)  (Ctrl+C para parar)"
    exec "$PY" visa/main.py
}

iniciar() {
    exige_venv; exige_env
    if pid="$(pid_ativo)"; then echo "já está rodando (PID $pid)"; return 0; fi
    mkdir -p "$LOGS"
    nohup "$PY" visa/main.py >> "$LOG" 2>&1 &
    echo $! > "$PIDS"
    # espera até 20 s o servidor responder
    for _ in $(seq 20); do
        if responde; then echo "rodando em http://localhost:$(porta) (PID $(cat "$PIDS"))"; return 0; fi
        pid_ativo > /dev/null || { tail -20 "$LOG"; erro "o servidor terminou ao iniciar (veja acima ou em $LOG)"; }
        sleep 1
    done
    echo "aviso: iniciado (PID $(cat "$PIDS")), mas ainda não responde; veja: ./visa.sh log"
}

parar() {
    local pid
    if ! pid="$(pid_ativo)"; then echo "não está rodando"; return 0; fi
    kill "$pid"
    for _ in $(seq 10); do                 # até 10 s para encerrar com calma
        kill -0 "$pid" 2>/dev/null || { rm -f "$PIDS"; echo "parado"; return 0; }
        sleep 1
    done
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$PIDS"; echo "parado à força (não respondeu em 10 s)"
}

estado() {
    local pid
    if pid="$(pid_ativo)"; then echo "processo:      rodando (PID $pid)"; else echo "processo:      parado (ou iniciado fora deste script)"; fi
    if responde; then echo "porta $(porta):    responde"; else echo "porta $(porta):    não responde"; fi
    [[ -f .env ]] && echo ".env:          ok" || echo ".env:          AUSENTE"
    local ventos; ventos="$(find wifs/ventos -name '*.json' 2>/dev/null | wc -l)"
    echo "vento:         $ventos arquivos em wifs/ventos/"
    local espaco; espaco="$(find data/espaco -maxdepth 1 -name '*.geojson' 2>/dev/null | wc -l)"
    echo "espaço aéreo:  $espaco de 9 GeoJSONs em data/espaco/ (atualizar a cada AIRAC: data/espaco/LEIAME.md)"
}

log() { [[ -f "$LOG" ]] || erro "sem log ainda ($LOG)"; tail -n 50 -f "$LOG"; }

testar() { exige_venv; "$PY" -m pytest "$@"; }

verificar() {
    exige_venv
    echo "== ruff";   "$RAIZ/.venv/bin/ruff" check .
    echo "== testes"; "$PY" -m pytest
}

avaliar() { exige_venv; exige_env; "$PY" visa/previsao.py "$@"; }

ajuda() { sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

# --- despacho ----------------------------------------------------------------

comando="${1:-ajuda}"; shift || true
case "$comando" in
    instalar|rodar|iniciar|parar|estado|log|testar|verificar|avaliar|ajuda) "$comando" "$@" ;;
    reiniciar) parar; iniciar ;;
    -h|--help) ajuda ;;
    *) ajuda; erro "comando desconhecido: $comando" ;;
esac
