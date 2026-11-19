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
#
# O comando "ajuda" imprime o cabeçalho acima, da linha 2 até a linha "A porta vem de".

set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$RAIZ"                      # o NiceGUI grava .nicegui/ na pasta atual: sempre a raiz

PYTHON="$RAIZ/.venv/bin/python"
PASTA_LOGS="$RAIZ/logs"

# --- auxiliares --------------------------------------------------------------

# Mostra a mensagem de erro e encerra o script.
erro() { echo "erro: $*" >&2; exit 1; }

# Imprime a porta do servidor: VISA_PORTA do ambiente > VISA_PORTA do .env > 8090.
porta() {
    if [[ -n "${VISA_PORTA:-}" ]]; then echo "$VISA_PORTA"; return; fi
    local porta_do_env
    porta_do_env="$(grep -E '^VISA_PORTA=' .env 2>/dev/null | tail -1 | cut -d= -f2 | tr -d ' "'"'" || true)"
    echo "${porta_do_env:-8090}"
}

# Cada porta tem o seu PID, o seu log e os seus registros do FAB.ia: uma instância de teste
# (VISA_PORTA=8091) não se confunde com a principal nem mistura as estatísticas dela.
# A porta 8090 mantém os nomes sem sufixo.
SUFIXO=""; [[ "$(porta)" == 8090 ]] || SUFIXO=".$(porta)"
ARQUIVO_PID="$PASTA_LOGS/visa$SUFIXO.pid"
ARQUIVO_LOG="$PASTA_LOGS/visa$SUFIXO.log"
export VISA_PREVISAO_DIR="${VISA_PREVISAO_DIR:-$PASTA_LOGS/previsao$SUFIXO}"

# Encerram o script com uma explicação se faltar o .venv ou o .env.
exige_venv() { [[ -x "$PYTHON" ]] || erro ".venv não encontrado em $RAIZ (crie com: python3 -m venv .venv)"; }
exige_env()  { [[ -f .env ]]  || erro ".env não encontrado (copie .env.exemplo para .env e preencha)"; }

# Imprime o PID se o servidor iniciado por este script ainda estiver rodando; senão, apaga o
# arquivo de PID e falha (código 1).
pid_ativo() {
    [[ -f "$ARQUIVO_PID" ]] || return 1
    local pid; pid="$(cat "$ARQUIVO_PID")"
    # confere se o PID ainda é o nosso (o sistema reaproveita números de processo)
    if kill -0 "$pid" 2>/dev/null && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q 'visa/main.py'; then
        echo "$pid"; return 0
    fi
    rm -f "$ARQUIVO_PID"; return 1
}

# Sucesso se o servidor responde por HTTP na porta (espera até 3 s).
responde() { curl -s -o /dev/null --max-time 3 "http://localhost:$(porta)/"; }

# --- comandos ----------------------------------------------------------------

# instalar: cria o .venv se faltar e instala requirements.txt e requirements-dev.txt.
instalar() {
    [[ -x "$PYTHON" ]] || python3 -m venv .venv
    "$PYTHON" -m pip install -r requirements.txt -r requirements-dev.txt
}

# rodar: roda em primeiro plano; o script vira o servidor (exec), Ctrl+C para.
rodar() {
    exige_venv; exige_env
    echo "CLS Visa em http://localhost:$(porta)  (Ctrl+C para parar)"
    exec "$PYTHON" visa/main.py
}

# iniciar: roda em segundo plano, grava o PID e espera até 20 s o servidor responder.
iniciar() {
    exige_venv; exige_env
    local pid
    if pid="$(pid_ativo)"; then echo "já está rodando (PID $pid)"; return 0; fi
    mkdir -p "$PASTA_LOGS"
    nohup "$PYTHON" visa/main.py >> "$ARQUIVO_LOG" 2>&1 &
    echo $! > "$ARQUIVO_PID"
    for _ in $(seq 20); do
        if responde; then echo "rodando em http://localhost:$(porta) (PID $(cat "$ARQUIVO_PID"))"; return 0; fi
        pid_ativo > /dev/null || { tail -20 "$ARQUIVO_LOG"; erro "o servidor terminou ao iniciar (veja acima ou em $ARQUIVO_LOG)"; }
        sleep 1
    done
    echo "aviso: iniciado (PID $(cat "$ARQUIVO_PID")), mas ainda não responde; veja: ./visa.sh log"
}

# parar: pede para encerrar (SIGTERM), espera até 10 s e, se preciso, força (SIGKILL).
parar() {
    local pid
    if ! pid="$(pid_ativo)"; then echo "não está rodando"; return 0; fi
    kill "$pid"
    for _ in $(seq 10); do
        kill -0 "$pid" 2>/dev/null || { rm -f "$ARQUIVO_PID"; echo "parado"; return 0; }
        sleep 1
    done
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$ARQUIVO_PID"; echo "parado à força (não respondeu em 10 s)"
}

# estado: processo, porta, .env e quantos arquivos de vento e de espaço aéreo existem.
estado() {
    local pid
    if pid="$(pid_ativo)"; then
        echo "processo:      rodando (PID $pid)"
    else
        echo "processo:      parado (ou iniciado fora deste script)"
    fi
    if responde; then echo "porta $(porta):    responde"; else echo "porta $(porta):    não responde"; fi
    if [[ -f .env ]]; then echo ".env:          ok"; else echo ".env:          AUSENTE"; fi
    # `|| true`: sem a pasta, o find falha e, com pipefail, encerraria o script aqui
    local arquivos_vento arquivos_espaco
    arquivos_vento="$(find wifs/ventos -name '*.json' 2>/dev/null | wc -l || true)"
    echo "vento:         $arquivos_vento arquivos em wifs/ventos/"
    arquivos_espaco="$(find data/espaco -maxdepth 1 -name '*.geojson' 2>/dev/null | wc -l || true)"
    echo "espaço aéreo:  $arquivos_espaco de 9 GeoJSONs em data/espaco/ (atualizar a cada AIRAC: data/espaco/LEIAME.md)"
}

# log: mostra as últimas 50 linhas do log desta porta e continua acompanhando.
log() { [[ -f "$ARQUIVO_LOG" ]] || erro "sem log ainda ($ARQUIVO_LOG)"; tail -n 50 -f "$ARQUIVO_LOG"; }

# testar: pytest; os argumentos extras (ex.: -k nome) vão direto para ele.
testar() { exige_venv; "$PYTHON" -m pytest "$@"; }

# verificar: ruff + testes, o que tem de passar antes de cada commit.
verificar() {
    exige_venv
    echo "== ruff";   "$RAIZ/.venv/bin/ruff" check .
    echo "== testes"; "$PYTHON" -m pytest
}

# avaliar: relatório do FAB.ia (visa/previsao.py como script); argumentos = datas AAAA-MM-DD.
avaliar() { exige_venv; exige_env; "$PYTHON" visa/previsao.py "$@"; }

# ajuda: imprime o cabeçalho deste arquivo (da linha 2 até "A porta vem de") sem o "# " do começo.
ajuda() { sed -n '2,/^# A porta vem de/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

# --- despacho ----------------------------------------------------------------

comando="${1:-ajuda}"; shift || true
case "$comando" in
    instalar|rodar|iniciar|parar|estado|log|testar|verificar|avaliar|ajuda) "$comando" "$@" ;;
    reiniciar) parar; iniciar ;;
    -h|--help) ajuda ;;
    *) ajuda; erro "comando desconhecido: $comando" ;;
esac
