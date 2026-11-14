"""Permite rodar os testes sem um .env real (nenhum teste acessa a rede)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'visa'))
for nome, valor in {
    'VISA_SEGREDO_SESSAO': 'teste', 'VISA_GEOSERVER_WMS': 'http://geoserver.teste/wms',
    'VISA_PUSHER_CHAVE': 'x', 'VISA_REDEMET_API_KEY': 'x', 'VISA_TRACKER_AUTH_URL': 'http://auth.teste', 'VISA_TRACKER_TOKEN': 'x',
}.items():
    os.environ.setdefault(nome, valor)
