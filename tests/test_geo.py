import numpy as np

import geo

QUADRADO = [[-47, -24], [-46, -24], [-46, -23], [-47, -23], [-47, -24]]     # [lon, lat]


def test_distancia():
    assert round(geo.distancia_km(-23.43, -46.47, -22.81, -43.25)) == 336      # SBGR -> SBGL
    assert geo.distancia_km(0, 0, 0, 0) == 0


def test_graus_por_km():
    dlat, dlon = geo.graus_por_km(0, geo.KM_POR_GRAU)
    assert (dlat, dlon) == (1, 1)
    dlat, dlon = geo.graus_por_km(60, geo.KM_POR_GRAU)
    assert dlat == 1 and round(dlon, 6) == 2                                    # cos 60° = 0,5


def test_dentro_escalar_e_vetor_concordam():
    lats, lons = np.array([-23.5, -22.5, -23.5]), np.array([-46.5, -46.5, -45.5])
    assert geo.dentro_vetor(lats, lons, QUADRADO).tolist() == [True, False, False]
    assert [geo.dentro(la, lo, QUADRADO) for la, lo in zip(lats, lons, strict=True)] == [True, False, False]


def test_limites_e_arredondar():
    assert geo.limites([[-15, -47], [-20, -40]]) == [[-20, -47], [-15, -40]]
    assert geo.limites([]) is None
    assert geo.arredondar([[1.234567, [2.345678]]], 2) == [[1.23, [2.35]]]
