# -*- coding: utf-8 -*-
"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Contas de geografia usadas por vários módulos (funções puras, sem rede nem tela).

Por que um módulo só para isso: a mesma conta aparecia copiada em sonda.py, impacto.py,
desenho.py, severo.py e espaco.py, às vezes com pequenas diferenças. Uma correção feita
numa cópia não chegava às outras. Aqui cada conta existe uma vez e tem teste.

Convenções (as mesmas do resto do sistema):
  - pontos do Leaflet e das nossas listas: [lat, lon];
  - anéis e coordenadas GeoJSON: [lon, lat] (é a ordem do padrão GeoJSON);
  - distâncias em km; ângulos em graus.

Precisão: as contas tratam a Terra como esfera (haversine) e, para áreas pequenas
(até algumas dezenas de km), aproximam o círculo por uma elipse em graus. O erro é bem
menor que a resolução dos dados (pixel de radar ~1 km, célula de raios 0,1° ≈ 11 km).
"""
import math

import numpy as np

RAIO_TERRA_KM = 6371.0
KM_POR_GRAU = 111.32     # comprimento de 1° de latitude (e de longitude no equador)


def distancia_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distância sobre a esfera (fórmula de haversine)."""
    f1, f2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((f2 - f1) / 2) ** 2 + math.cos(f1) * math.cos(f2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * RAIO_TERRA_KM * math.asin(math.sqrt(a))


def graus_por_km(lat: float, km: float) -> tuple[float, float]:
    """(Δlat, Δlon) em graus que correspondem a `km` na latitude `lat`.

    A longitude "encolhe" com o cosseno da latitude. O mínimo de 0,01 no cosseno evita
    divisão por zero perto dos polos (que o sistema nunca usa, mas não deve quebrar).
    """
    dlat = km / KM_POR_GRAU
    dlon = km / (KM_POR_GRAU * max(math.cos(math.radians(lat)), 0.01))
    return dlat, dlon


def dentro(lat: float, lon: float, anel: list) -> bool:
    """O ponto está dentro do polígono? Anel em [lon, lat] (GeoJSON).

    Algoritmo do raio: conta quantas arestas uma semirreta saindo do ponto cruza;
    ímpar = dentro. Não precisa de biblioteca (shapely etc.) e é rápido o bastante
    para as poucas centenas de polígonos de TMA e de tempo severo.
    """
    resultado, j = False, len(anel) - 1
    for i in range(len(anel)):
        xi, yi = anel[i][0], anel[i][1]
        xj, yj = anel[j][0], anel[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            resultado = not resultado
        j = i
    return resultado


def dentro_vetor(lats: np.ndarray, lons: np.ndarray, anel: list) -> np.ndarray:
    """O mesmo que `dentro`, para muitos pontos de uma vez (numpy). Devolve um vetor de bool.

    Usado no painel de impacto, que testa milhares de pixels de radar contra cada TMA:
    o laço passa pelas arestas (poucas) e não pelos pontos (muitos).
    """
    xs = np.array([p[0] for p in anel], float)
    ys = np.array([p[1] for p in anel], float)
    resultado = np.zeros(lats.shape, bool)
    for i in range(len(anel)):
        j = i - 1
        xi, yi, xj, yj = xs[i], ys[i], xs[j], ys[j]
        if yi == yj:            # aresta horizontal: nunca é cruzada (e dividiria por zero)
            continue
        cruza = ((yi > lats) != (yj > lats)) & (lons < (xj - xi) * (lats - yi) / (yj - yi) + xi)
        resultado ^= cruza
    return resultado


def deslocar(lat: float, lon: float, rumo: float, km):
    """Ponto a `km` de distância no `rumo` (graus, 0 = norte, sentido horário).

    Conta no plano local (como `graus_por_km`): erro desprezível até algumas centenas de km,
    que é o alcance da previsão de conflitos (30 min de voo). `km` pode ser um array numpy:
    devolve então os arrays de latitudes e longitudes.
    """
    r = math.radians(rumo)
    dlat = np.multiply(km, math.cos(r)) / KM_POR_GRAU
    dlon = np.multiply(km, math.sin(r)) / (KM_POR_GRAU * max(math.cos(math.radians(lat)), 0.01))
    return lat + dlat, lon + dlon


# ---------------------------------------------------------------------------
# Grade de 0,1° (a das células de raios da REDEMET): impacto e previsão usam a mesma
# ---------------------------------------------------------------------------

GRADE = 0.1                     # graus
Chave = tuple[int, int]         # (floor(lat*10), floor(lon*10))


def chave_celula(lat: float, lon: float) -> Chave:
    """Célula de 0,1° que contém o ponto."""
    return math.floor(lat * 10), math.floor(lon * 10)


def centro(chave: Chave) -> tuple[float, float]:
    """Centro (lat, lon) da célula."""
    return (chave[0] + 0.5) * GRADE, (chave[1] + 0.5) * GRADE


def vizinhas(chave: Chave) -> list[Chave]:
    """A célula e as 8 em volta (inclusive na diagonal)."""
    i, j = chave
    return [(i + di, j + dj) for di in (-1, 0, 1) for dj in (-1, 0, 1)]


def mercator_y(lat: float) -> float:
    """Coordenada vertical da projeção de Mercator (a do mapa), em radianos."""
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def limites(pontos: list[list[float]]) -> list[list[float]] | None:
    """[[sul, oeste], [norte, leste]] de uma lista de pontos [lat, lon] (formato do Leaflet)."""
    if not pontos:
        return None
    lats, lons = [p[0] for p in pontos], [p[1] for p in pontos]
    return [[min(lats), min(lons)], [max(lats), max(lons)]]


def arredondar(coordenadas, casas: int):
    """Arredonda coordenadas GeoJSON aninhadas (ponto, linha, polígono, multipolígono...).

    Menos casas = arquivo menor para o navegador: 4 casas ≈ 10 m, 5 casas ≈ 1 m.
    """
    if isinstance(coordenadas, (int, float)):
        return round(coordenadas, casas)
    return [arredondar(c, casas) for c in coordenadas]
