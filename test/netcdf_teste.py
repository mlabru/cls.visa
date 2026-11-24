"""Arquivos netCDF pequenos com a estrutura dos do WRF (como o CDO os grava), para os testes.

Usado por conftest.py (fixture `rodada`) e pelos testes do modelo.
"""
from datetime import UTC, datetime

import netCDF4
import numpy as np

import geo

HORA_INICIAL = 17758656            # 23/11/2026 00Z em "hours since 1-1-1", calendário misto (o do arquivo real)
INICIO = datetime(2026, 11, 23, tzinfo=UTC)
NIVEIS = [1000, 850, 500, 250]


def lats_mercator(sul, norte, n):
    """Centros de n linhas igualmente espaçadas em y de Mercator."""
    ys = np.linspace(geo.mercator_y(sul), geo.mercator_y(norte), n)
    return np.array([geo.lat_de_mercator(y) for y in ys])


def escrever(caminho, lats, lons, horas=3, preencher=None):
    """Um netCDF com a estrutura dos arquivos do WRF (como o CDO os grava).

    `preencher(nome, passo, nivel, lats, lons)` dá o valor de cada campo; padrão: o passo.
    """
    preencher = preencher or (lambda nome, passo, nivel, la, lo: np.full((len(la), len(lo)), float(passo)))
    with netCDF4.Dataset(caminho, 'w', format='NETCDF3_64BIT_OFFSET') as arquivo:
        arquivo.createDimension('time', None)
        arquivo.createDimension('lon', len(lons))
        arquivo.createDimension('lat', len(lats))
        arquivo.createDimension('lev', len(NIVEIS))
        arquivo.createDimension('lev_2', 1)
        tempo = arquivo.createVariable('time', 'f8', ('time',))
        tempo.units = 'hours since 1-1-1 00:00:00'
        tempo.calendar = 'standard'
        tempo[:] = HORA_INICIAL + np.arange(horas)
        arquivo.createVariable('lon', 'f8', ('lon',))[:] = lons
        arquivo.createVariable('lat', 'f8', ('lat',))[:] = lats
        arquivo.createVariable('lev', 'f8', ('lev',))[:] = NIVEIS
        for nome, nivel_dim in [('max_dbz', 'lev_2'), ('rainc', 'lev_2'), ('rainnc', 'lev_2'), ('mcape', 'lev_2'),
                                ('wspd', 'lev'), ('tc', 'lev'), ('td', 'lev'), ('rh', 'lev'),
                                ('u', 'lev'), ('v', 'lev')]:
            var = arquivo.createVariable(nome, 'f4', ('time', nivel_dim, 'lat', 'lon'), fill_value=1e30)
            for passo in range(horas):
                for nivel in range(len(NIVEIS) if nivel_dim == 'lev' else 1):
                    var[passo, nivel] = preencher(nome, passo, nivel, lats, lons)
