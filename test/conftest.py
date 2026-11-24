"""Permite rodar os testes sem um .env real (nenhum teste acessa a rede) e cria a rodada do WRF de teste."""
import os

import numpy as np
import pytest

import netcdf_teste

# `visa/` entra no sys.path pelo pyproject.toml ([tool.pytest.ini_options] pythonpath)
for nome, valor in {
    'VISA_SEGREDO_SESSAO': 'teste', 'VISA_GEOSERVER_WMS': 'http://geoserver.teste/wms',
    'VISA_PUSHER_CHAVE': 'x', 'VISA_REDEMET_API_KEY': 'x', 'VISA_TRACKER_AUTH_URL': 'http://auth.teste', 'VISA_TRACKER_TOKEN': 'x',
}.items():
    os.environ.setdefault(nome, valor)


@pytest.fixture
def rodada(tmp_path):
    """d1 de 30°S–20°S, 50°W–40°W (longitudes 0..360, como no arquivo real) e d2 no meio dele."""
    def preencher(nome, passo, nivel, la, lo):
        if nome == 'max_dbz':
            return np.full((len(la), len(lo)), 10.0 + passo)           # d1: 10, 11, 12 dBZ
        if nome in ('rainc', 'rainnc'):
            return np.full((len(la), len(lo)), 2.0 * passo)            # acumula 4 mm/h no total
        if nome == 'tc':
            valores = np.full((len(la), len(lo)), -20.0)
            if nivel == 0:
                valores[0, :] = 1e30                                    # abaixo do terreno
            return valores
        valores = {'td': -25.0, 'rh': 50.0, 'u': 10.0, 'v': 0.0, 'mcape': 1000.0}   # u, v: vento de oeste
        return np.full((len(la), len(lo)), valores.get(nome, 10.0))         # wspd 10 m/s
    netcdf_teste.escrever(tmp_path / 'wrfd1_2026112300.nc', netcdf_teste.lats_mercator(-30, -20, 40), np.arange(310, 320, 0.5) + 0.25,
              preencher=preencher)

    def preencher_d2(nome, passo, nivel, la, lo):
        valores = preencher(nome, passo, nivel, la, lo)
        return valores + 40 if nome == 'max_dbz' else valores           # d2: 50, 51, 52 dBZ
    netcdf_teste.escrever(tmp_path / 'wrfd2_2026112300.nc', netcdf_teste.lats_mercator(-26, -24, 30), np.arange(314, 316, 0.1) + 0.05,
              preencher=preencher_d2)
    (tmp_path / 'wrfd1_2026112212.nc').touch()
    (tmp_path / 'outro.nc').touch()
    return tmp_path
