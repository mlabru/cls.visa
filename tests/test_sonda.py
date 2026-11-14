from datetime import datetime, timezone

import numpy as np

import radar
import redemet
import sonda


def test_pixel_e_dbz():
    imagem = redemet.Imagem('radar/x.png', -10, 0, -50, -40, None, 'be')
    img = np.zeros((11, 11, 4), np.uint8)
    r, g, b = radar.PALETA[radar.PALETA[:, 0] == 40][0, 1:].astype(int)        # cor de 40 dBZ
    img[5, 5] = (r, g, b, 255)
    assert sonda.cobre(imagem, -5, -45) and not sonda.cobre(imagem, 1, -45)
    assert sonda.dbz_da_cor(sonda.pixel(img, imagem, -5, -45)) == 40
    assert sonda.dbz_da_cor(sonda.pixel(img, imagem, -1, -49)) is None          # transparente
    assert sonda.dbz_da_cor(np.array([128, 128, 128, 255])) is None             # cinza: abaixo de 0 dBZ


def test_vento_no_ponto():
    cabecalho = {'lo1': 300.0, 'la1': 0.0, 'dx': 1.0, 'dy': 1.0, 'nx': 3, 'ny': 3}
    u = {'header': cabecalho, 'data': [0.0] * 9}
    v = {'header': cabecalho, 'data': [0.0] * 9}
    u['data'][4], v['data'][4] = 10.0, 0.0           # (lat -1, lon -59): vento de oeste, 10 m/s
    assert sonda.vento_no_ponto([u, v], -1, -59) == (270, 19)
    v['data'][4], u['data'][4] = -10.0, 0.0          # vem do norte
    assert sonda.vento_no_ponto([u, v], -1, -59) == (360, 19)
    assert sonda.vento_no_ponto([u, v], -20, -59) is None                     # fora da grade
    assert [sonda.fl_da_pressao(p) for p in (25000, 50000, 85000)] == [340, 180, 50]   # como no painel
    assert sonda.fl_da_pressao(12500) == 475                                       # fora da tabela: padrão


def test_raios_perto():
    quando = datetime(2026, 11, 13, 17, 0, tzinfo=timezone.utc)
    quadro = redemet.QuadroRaios(quando, ((-23.45, -46.45, 5), (-23.55, -46.55, 40), (-25.0, -46.5, 0)))
    r = sonda.raios_perto(quadro, -23.43, -46.47)
    assert r.contagem == {10: 1, 20: 2, 50: 2}
    assert r.novas_20km == 1 and r.mais_perto[1] == 5 and r.mais_perto[0] < 5


def test_area_severa():
    quadrado = {'geometry': {'type': 'Polygon', 'coordinates': [[[-47, -24], [-46, -24], [-46, -23], [-47, -23],
                                                                 [-47, -24]]]}}
    assert sonda.em_area_severa([quadrado], 'area', 5, -23.5, -46.5)
    assert not sonda.em_area_severa([quadrado], 'area', 5, -22.5, -46.5)
    multi = {'geometry': {'type': 'MultiPolygon', 'coordinates': [quadrado['geometry']['coordinates']]}}
    assert sonda.em_area_severa([multi], 'area', 5, -23.5, -46.5)
    ponto = {'geometry': {'type': 'Point', 'coordinates': [-46.5, -23.5]}}
    assert sonda.em_area_severa([ponto], 'ponto', 5, -23.52, -46.5)
    assert not sonda.em_area_severa([ponto], 'ponto', 5, -23.7, -46.5)


def test_titulo():
    assert sonda.Resultado(-23.4321, -46.4699).titulo == '23,43°S 46,47°O'
