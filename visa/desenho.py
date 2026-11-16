"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Desenho de formas simples no mapa, feito em Python (sem JavaScript próprio).

Cada forma vira uma camada do próprio Leaflet (L.circle, L.polyline, L.geoJSON...),
criada com `mapa.generic_layer(...)`. Aqui ficam só funções puras que montam os dados:
os balões (HTML com o texto escapado), os polígonos e os retângulos.
"""
import html
import math

import geo


def balao(titulo: str, linhas: list[tuple[str, str | None]] = (), nota: tuple[str, str] | None = None) -> str:
    """HTML do balão (popup): título em negrito e linhas de texto, com cor opcional.

    Todo texto passa por html.escape: o Leaflet insere o balão como HTML.
    """
    partes = [f'<b>{html.escape(titulo)}</b>']
    for texto, cor in linhas:
        if not texto:
            continue
        estilo = f' style="color:{cor}"' if cor else ''
        partes.append(f'<div{estilo}>{html.escape(texto)}</div>')
    if nota:
        texto, cor = nota
        partes.append(f'<div style="font-size:10px;color:{cor}">{html.escape(texto)}</div>')
    return ''.join(partes)


def circulo(lat: float, lon: float, raio_km: float, lados: int = 16) -> list[list[float]]:
    """Anel [lon, lat] (GeoJSON) que aproxima um círculo de `raio_km` em volta do ponto."""
    dlat, dlon = geo.graus_por_km(lat, raio_km)
    anel = [[round(lon + dlon * math.cos(2 * math.pi * i / lados), 4),
             round(lat + dlat * math.sin(2 * math.pi * i / lados), 4)] for i in range(lados)]
    return anel + [anel[0]]


# ---------------------------------------------------------------------------
# Tempo severo
# ---------------------------------------------------------------------------

def severo_geojson(feicoes: list[dict], tipo: str, raio_km: float) -> dict:
    """Áreas: as feições como vieram. Pontos: um MultiPolygon de círculos de `raio_km`.

    Os pontos viram polígonos aqui porque o L.geoJSON só desenha pontos como círculo
    com uma função JavaScript (pointToLayer); polígonos aceitam um estilo fixo.
    """
    if tipo != 'ponto':
        return {'type': 'FeatureCollection',
                'features': [{'type': 'Feature', 'properties': {}, 'geometry': f['geometry']}
                             for f in feicoes if f.get('geometry')]}
    circulos = [[circulo(lat, lon, raio_km, lados=12)]
                for f in feicoes if (f.get('geometry') or {}).get('type') == 'Point'
                for lon, lat in [f['geometry']['coordinates'][:2]]]
    features = [{'type': 'Feature', 'properties': {},
                 'geometry': {'type': 'MultiPolygon', 'coordinates': circulos}}] if circulos else []
    return {'type': 'FeatureCollection', 'features': features}


def severo_estilo(estilo: dict, tipo: str) -> dict:
    """Estilo Leaflet: pontos sempre bem visíveis (0,7); áreas com a opacidade escolhida."""
    opacidade = 0.7 if tipo == 'ponto' else estilo['opacidade']
    return {'color': estilo['cor'], 'weight': 1, 'fillColor': estilo['cor'], 'fillOpacity': opacidade}


COBERTURA = {'color': '#e2e8f0', 'weight': 1, 'opacity': 0.9, 'dashArray': '6 6',
             'fill': False, 'interactive': False}
