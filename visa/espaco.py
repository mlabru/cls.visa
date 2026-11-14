"""
Espaço aéreo: aerovias, cartas ENRC, FIR, TMA e áreas proibidas, restritas e perigosas.

Duas fontes possíveis para cada camada:

1. **Arquivo local (preferido)** — `dados/espaco/<chave>.geojson`, baixado do WFS do DECEA
   uma vez por ciclo AIRAC (28 dias). O mapa desenha os vetores: cor, espessura e
   tracejado de verdade, informações ao clicar, e nenhuma dependência do Cloudflare.
   URL para baixar (abrir num navegador normal):
     https://geoaisweb.decea.mil.br/geoserver/ICA/ows?service=WFS&version=1.0.0
       &request=GetFeature&typeName=<camadas_wms>&outputFormat=application/json

2. **Imagens do GeoServer do DECEA (reserva)** — se não houver arquivo local. O navegador
   busca os tiles direto; cor e tracejado são um filtro de imagem aproximado.
"""
import json
import time
from dataclasses import dataclass
from pathlib import Path

import config
import geo


@dataclass(frozen=True)
class CamadaEspaco:
    chave: str
    titulo: str
    camadas_wms: str
    cor_padrao: str
    estilizavel: bool = False
    preenchimento: float = 0.04     # opacidade do preenchimento dos polígonos


def _cartas(prefixo: str) -> str:
    return ','.join(f'ICA:ENRC_{prefixo}{n}' for n in range(1, 10))


CAMADAS = [
    CamadaEspaco('aerovias_altas', 'Aerovias altas', 'ICA:vw_aerovia_alta_v2', '#d8b4fe', estilizavel=True),
    CamadaEspaco('aerovias_baixas', 'Aerovias baixas', 'ICA:vw_aerovia_baixa_v2', '#60a5fa', estilizavel=True),
    # As cartas ENRC só existem como imagem (o WFS não as conhece): sempre vêm do DECEA
    CamadaEspaco('cartas_altas', 'Cartas altas (ENRC H)', _cartas('H'), '#a855f7'),
    CamadaEspaco('cartas_baixas', 'Cartas baixas (ENRC L)', _cartas('L'), '#9333ea'),
    CamadaEspaco('fir', 'FIR', 'ICA:fir', '#e2e8f0'),
    CamadaEspaco('setor_fir', 'Setores de FIR', 'ICA:SETOR_FIR', '#94a3b8'),
    CamadaEspaco('tma', 'TMA', 'ICA:TMA', '#fbbf24'),
    CamadaEspaco('setores_tma', 'Setores de TMA', 'ICA:setores_tma', '#f59e0b'),
    CamadaEspaco('areas_proibidas', 'Áreas proibidas (P)', 'ICA:eac_p', '#ef4444', preenchimento=0.2),
    CamadaEspaco('areas_restritas', 'Áreas restritas (R)', 'ICA:eac_r', '#f97316', preenchimento=0.15),
    CamadaEspaco('areas_perigosas', 'Áreas perigosas (D)', 'ICA:eac_d', '#facc15', preenchimento=0.1),
]
POR_CHAVE = {c.chave: c for c in CAMADAS}

CORES = {
    'original': 'Original', '#d8b4fe': 'Lilás', '#60a5fa': 'Azul', '#34d399': 'Verde',
    '#fbbf24': 'Âmbar', '#f87171': 'Vermelho', '#ffffff': 'Branco',
}
TRACOS = {'continuo': 'Contínua', 'tracejado': 'Tracejada', 'pontilhado': 'Pontilhada'}

ESTILO_PADRAO = {'cor': 'original', 'traco': 'continuo', 'opacidade': 0.8}

CICLO_AIRAC_DIAS = 28


# ---------------------------------------------------------------------------
# Arquivos locais
# ---------------------------------------------------------------------------

def arquivo_local(chave: str, pasta: Path = config.ESPACO_DIR) -> Path | None:
    caminho = pasta / f'{chave}.geojson'
    return caminho if caminho.is_file() else None


def idade_dias(caminho: Path) -> float:
    return (time.time() - caminho.stat().st_mtime) / 86400


def _nivel(valor, unidade) -> str:
    if valor is None:
        return '?'
    valor = round(float(valor))
    if valor >= 999:
        return 'ilimitado'
    return f'FL{valor:03d}' if (unidade or 'FL') == 'FL' else f'{valor} {unidade}'


def _numero(*valores) -> float | None:
    for v in valores:
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


SENTIDOS = {'BOTH': 'ambos', 'FORWARD': 'só no sentido dos fixos', 'BACKWARD': 'só no sentido contrário'}


def ficha_aerovia(p: dict) -> dict:
    """Propriedades AIXM do segmento -> campos legíveis para o popup (na ordem de exibição).

    `_titulo` vira o cabeçalho do popup. Campos vazios são omitidos.
    """
    # Na base do DECEA o comprimento vem em `segment_length_uom` (colunas trocadas)
    comprimento = _numero(p.get('segment_length'), p.get('segment_length_uom'))
    rumo = p.get('initial_magnetic_track')
    rumo_volta = p.get('reverse_magnetic_track')
    ficha = {
        '_titulo': f"{p.get('text_designator', '?')}  {p.get('from_fix_ident', '?')} → {p.get('to_fix_ident', '?')}",
        'Tipo': p.get('code_type'),
        'Limites': f"{_nivel(p.get('lower_limit'), p.get('uom_lower_limit'))} a "
                   f"{_nivel(p.get('upper_limit'), p.get('uom_upper_limit'))}",
        'Nível mínimo': _nivel(p['minimumn_level'], p.get('uom_minimumn_level')) if p.get('minimumn_level') else None,
        'Sentido': SENTIDOS.get(p.get('direction'), p.get('direction')),
        'Rumo mag.': f'{rumo:03.0f}° / volta {rumo_volta:03.0f}°' if rumo is not None and rumo_volta is not None else None,
        'Distância': f'{comprimento:.1f} NM' if comprimento else None,
        'Fixos': f"{p.get('from_fix_type', '?')} {p.get('from_fix_ident', '?')} → "
                 f"{p.get('to_fix_type', '?')} {p.get('to_fix_ident', '?')}",
        'Observação': p.get('text_remark'),
        'Obs. da aerovia': p.get('airway_text_remark'),
        'Vigente desde': (p.get('effectived') or '').rstrip('Z') or None,
    }
    return {k: v for k, v in ficha.items() if v not in (None, '')}


def _limite_vertical(valor, unidade, referencia) -> str:
    """Ex.: (0, 'FT', 'SFC') -> 'SFC'; (3500, 'FT', 'MSL') -> '3500 ft MSL'; (195, 'FL', 'STD') -> 'FL195'."""
    v = _numero(valor)
    if v is None:
        return '?'
    if unidade == 'FL':
        return 'ilimitado' if v >= 999 else f'FL{v:03.0f}'
    if v == 0 and referencia == 'SFC':
        return 'SFC'
    ref = 'AGL' if referencia == 'SFC' else (referencia or '')
    return f'{v:.0f} {(unidade or "").lower()} {ref}'.strip()


TIPOS_ESPACO = {'FIR': 'FIR', 'TMA': 'TMA', 'SECTOR': 'Setor',
                'P': 'Área proibida', 'R': 'Área restrita', 'D': 'Área perigosa'}


def ficha_espaco(p: dict) -> dict:
    """Propriedades AIXM de FIR/TMA/setores -> campos legíveis.

    A tabela do DECEA tem nomes de coluna truncados (vieram de shapefile):
    limite superior = upperlimit + uplimituni (unidade) + codedistve (referência);
    limite inferior = lowerlimi1 + lowerlimit (unidade!) + codedistv1 (referência).
    """
    tipo = TIPOS_ESPACO.get(p.get('typ'), p.get('typ') or '')
    ficha = {
        '_titulo': f"{tipo} {p.get('ident', '?')} – {p.get('nam', '')}".strip(' –'),
        'Limites': f"{_limite_vertical(p.get('lowerlimi1'), p.get('lowerlimit'), p.get('codedistv1'))} a "
                   f"{_limite_vertical(p.get('upperlimit'), p.get('uplimituni'), p.get('codedistve'))}",
        'FIR': p.get('relatedfir'),
        'Horário': p.get('codewrkhr'),
        'Observação': p.get('txtrmk_loc') or p.get('classrmklo') or p.get('txtlocalty'),
        'Vigente desde': (p.get('effectived') or '').rstrip('Z') or None,
    }
    return {k: v for k, v in ficha.items() if v not in (None, '')}


def _limite_area(valor, unidade) -> str:
    """Limites das áreas P/R/D: o arquivo não traz a referência (MSL/AGL), só valor e unidade."""
    v = _numero(valor)
    if unidade == 'GND' or v == 0:
        return 'GND'
    return _limite_vertical(v, unidade, None)


def ficha_area(p: dict) -> dict:
    """Áreas proibidas, restritas e perigosas (ICA:eac_p/r/d): colunas próprias, diferentes das da TMA."""
    tipo = TIPOS_ESPACO.get(p.get('tipo'), p.get('tipo') or 'Área')
    ficha = {
        '_titulo': f"{tipo} {p.get('id', '?')} – {p.get('nome') or ''}".strip(' –'),
        'Limites': f"{_limite_area(p.get('lowerlimit'), p.get('uom_llimit'))} a "
                   f"{_limite_area(p.get('upperlimit'), p.get('uom_ulimit'))}",
        'FIR': p.get('fir'),
        'Perigo': p.get('perigo'),
        'Designador': p.get('designador'),
        'Observação': p.get('observacao'),
        'Vigente desde': (p.get('efetivacao') or '').rstrip('Z') or None,
    }
    return {k: v for k, v in ficha.items() if v not in (None, '')}


def _propriedades(chave: str, p: dict) -> dict:
    if chave.startswith('aerovias_') and 'text_designator' in p:
        return ficha_aerovia(p)
    if chave.startswith('areas_') and p.get('tipo') in ('P', 'R', 'D'):
        return ficha_area(p)
    if 'airspace_p' in p and 'typ' in p:
        return ficha_espaco(p)
    return {k: v for k, v in p.items() if v not in (None, '') and k.lower() not in ('bbox', 'the_geom', 'geom')}


def validade(caminho: Path) -> tuple[str | None, str | None]:
    """(emenda vigente, próxima emenda) do ciclo AIRAC, se o arquivo trouxer — ex.: ('2026-10-29', '2026-11-26')."""
    geojson_preparado(caminho)  # garante a leitura (e o cache)
    return _cache[caminho][2]


_cache: dict[Path, tuple[float, bytes, tuple]] = {}


def geojson_preparado(caminho: Path) -> bytes:
    """Lê o GeoJSON, arredonda coordenadas e deixa as propriedades legíveis. Cache até o arquivo mudar."""
    mtime = caminho.stat().st_mtime
    if caminho in _cache and _cache[caminho][0] == mtime:
        return _cache[caminho][1]

    chave = caminho.stem
    dados = json.loads(caminho.read_text(encoding='utf-8'))
    feicoes = []
    emenda = proxima = None
    for f in dados.get('features', []):
        geometria = f.get('geometry')
        if not geometria or 'coordinates' not in geometria:
            continue
        brutas = f.get('properties') or {}
        emenda = emenda or (brutas.get('emenda') or '').rstrip('Z') or None
        proxima = proxima or (brutas.get('emenda_futura') or '').rstrip('Z') or None
        feicoes.append({'type': 'Feature', 'properties': _propriedades(chave, brutas),
                        'geometry': {'type': geometria['type'],
                                     'coordinates': geo.arredondar(geometria['coordinates'], 5)}})
    saida = json.dumps({'type': 'FeatureCollection', 'features': feicoes},
                       ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    _cache[caminho] = (mtime, saida, (emenda, proxima))
    return saida
