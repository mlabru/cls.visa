from datetime import UTC, datetime

import numpy as np

import config
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


def test_raios_perto():
    quando = datetime(2026, 11, 13, 17, 0, tzinfo=UTC)
    quadro = redemet.QuadroRaios(quando, ((-23.45, -46.45, 5), (-23.55, -46.55, 40), (-25.0, -46.5, 0)))
    r = sonda.raios_perto(quadro, -23.43, -46.47)
    assert r.contagem == {10: 1, 20: 2, 50: 2}
    assert r.novas == 1 and r.mais_perto[1] == 5 and r.mais_perto[0] < 5


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


def test_perfil_do_modelo_na_sonda(rodada, monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'MODELO_DIR', rodada)
    resultado = sonda.Resultado(-25, -45)
    sonda._modelo(resultado, None, None)                  # a rodada mais recente, perto de agora
    assert resultado.perfil is not None and resultado.perfil_erro == ''
    assert resultado.perfil.horario == datetime(2026, 11, 23, 2, tzinfo=UTC)   # o último (a rodada já passou)

    longe = sonda.Resultado(0, 0)
    sonda._modelo(longe, None, None)
    assert longe.perfil is None and longe.perfil_erro == 'fora da área do modelo'

    monkeypatch.setattr(config, 'MODELO_DIR', tmp_path / 'vazia')
    sem = sonda.Resultado(-25, -45)
    sonda._modelo(sem, None, None)
    assert sem.perfil_erro == 'nenhuma rodada do modelo na pasta'
