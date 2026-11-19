"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Espaço aéreo: aerovias, cartas ENRC, FIR, TMA e áreas proibidas, restritas e perigosas.

Duas fontes possíveis para cada camada:

1. **Arquivo local (preferido)** — `data/espaco/<chave>.geojson`, baixado do WFS do DECEA
   uma vez por ciclo AIRAC (28 dias). O mapa desenha os vetores: cor, espessura e
   tracejado de verdade, informações ao clicar, e nenhuma dependência do Cloudflare.
   URL para baixar (abrir num navegador normal):
     https://geoaisweb.decea.mil.br/geoserver/ICA/ows?service=WFS&version=1.0.0
       &request=GetFeature&typeName=<camadas_wms>&outputFormat=application/json

2. **Imagens do GeoServer do DECEA (reserva)** — se não houver arquivo local. O navegador
   busca os tiles direto; cor e tracejado são um filtro de imagem aproximado.

Usado por main.py (rota /espaco/<camada>.geojson), pela camada de espaço aéreo, pelo painel,
pelos modos, pelas preferências, pela barra de saúde e pelo impacto (TMAs).
"""
import json
import time
from dataclasses import dataclass
from pathlib import Path

import config
import geo


@dataclass(frozen=True)
class CamadaEspaco:
    """Uma camada de espaço aéreo.

    Atributos:
        chave: identificador (nome do arquivo local e chave nas preferências).
        titulo: nome no painel.
        camadas_wms: camada(s) no GeoServer do DECEA, separadas por vírgula.
        cor_padrao: cor da camada quando o estilo é 'original'.
        estilizavel: se True, o painel deixa mudar cor, traço e opacidade.
        preenchimento: opacidade do preenchimento dos polígonos.
    """
    chave: str
    titulo: str
    camadas_wms: str
    cor_padrao: str
    estilizavel: bool = False
    preenchimento: float = 0.04


def _cartas(prefixo: str) -> str:
    """Lista as 9 cartas ENRC de um tipo ('H' altas, 'L' baixas) no formato do WMS."""
    return ','.join(f'ICA:ENRC_{prefixo}{numero}' for numero in range(1, 10))


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
POR_CHAVE = {camada.chave: camada for camada in CAMADAS}

CORES = {
    'original': 'Original', '#d8b4fe': 'Lilás', '#60a5fa': 'Azul', '#34d399': 'Verde',
    '#fbbf24': 'Âmbar', '#f87171': 'Vermelho', '#ffffff': 'Branco',
}
TRACOS = {'continuo': 'Contínua', 'tracejado': 'Tracejada', 'pontilhado': 'Pontilhada'}

ESTILO_PADRAO = {'cor': 'original', 'traco': 'continuo', 'opacidade': 0.8}

CICLO_AIRAC_DIAS = 28

# GeoJSON já preparado de cada arquivo: (mtime do arquivo, bytes prontos, (emenda, próxima emenda))
_cache: dict[Path, tuple[float, bytes, tuple]] = {}


# ---------------------------------------------------------------------------
# Arquivos locais
# ---------------------------------------------------------------------------

def arquivo_local(chave: str, pasta: Path = config.ESPACO_DIR) -> Path | None:
    """Acha o GeoJSON local da camada.

    Parâmetros:
        chave: chave da camada (de CAMADAS).
        pasta: onde procurar (testes); padrão config.ESPACO_DIR.

    Retorna:
        O caminho de `<chave>.geojson`; None se o arquivo não existir.
    """
    caminho = pasta / f'{chave}.geojson'
    if not caminho.is_file():
        return None
    return caminho


def idade_dias(caminho: Path) -> float:
    """Diz há quantos dias o arquivo foi modificado (pela data do sistema de arquivos)."""
    return (time.time() - caminho.stat().st_mtime) / 86400


# ---------------------------------------------------------------------------
# Fichas: propriedades AIXM -> campos legíveis para o popup
# ---------------------------------------------------------------------------

def _nivel(valor, unidade) -> str:
    """Formata um limite de aerovia: (245, 'FL') -> 'FL245'; 999 ou mais -> 'ilimitado'.

    Parâmetros:
        valor: número (ou texto numérico); None = desconhecido.
        unidade: 'FL' (ou vazio, tratado como FL) ou outra unidade (ex.: 'FT').

    Retorna:
        O texto do limite; '?' se `valor` for None.
    """
    if valor is None:
        return '?'
    valor = round(float(valor))
    if valor >= 999:
        return 'ilimitado'
    if (unidade or 'FL') == 'FL':
        return f'FL{valor:03d}'
    return f'{valor} {unidade}'


def _numero(*valores) -> float | None:
    """Devolve o primeiro dos valores que for um número (ou texto numérico); None se nenhum for."""
    for valor in valores:
        try:
            return float(valor)
        except (TypeError, ValueError):
            continue
    return None


def _data_sem_z(texto) -> str | None:
    """Tira o 'Z' do fim de uma data AIXM: '2026-10-29Z' -> '2026-10-29'; vazio ou None -> None."""
    return (texto or '').rstrip('Z') or None


def _sem_vazios(ficha: dict) -> dict:
    """Devolve a ficha sem os campos None ou '' (o popup não mostra linhas vazias)."""
    return {campo: valor for campo, valor in ficha.items() if valor not in (None, '')}


SENTIDOS = {'BOTH': 'ambos', 'FORWARD': 'só no sentido dos fixos', 'BACKWARD': 'só no sentido contrário'}


def _nivel_minimo(propriedades: dict) -> str | None:
    """Nível mínimo da aerovia (coluna `minimumn_level`, com o erro de grafia da base); None se não houver."""
    if not propriedades.get('minimumn_level'):
        return None
    return _nivel(propriedades['minimumn_level'], propriedades.get('uom_minimumn_level'))


def _rumos(propriedades: dict) -> str | None:
    """Rumo magnético de ida e de volta, ex.: '326° / volta 146°'; None se faltar algum."""
    rumo = propriedades.get('initial_magnetic_track')
    rumo_volta = propriedades.get('reverse_magnetic_track')
    if rumo is None or rumo_volta is None:
        return None
    return f'{rumo:03.0f}° / volta {rumo_volta:03.0f}°'


def ficha_aerovia(propriedades: dict) -> dict:
    """Converte as propriedades AIXM de um segmento de aerovia em campos legíveis para o popup.

    `_titulo` vira o cabeçalho do popup. Campos vazios são omitidos.

    Parâmetros:
        propriedades: propriedades da feição, como vieram do WFS do DECEA.

    Retorna:
        Dict campo -> texto, na ordem de exibição.
    """
    # Na base do DECEA o comprimento vem em `segment_length_uom` (colunas trocadas)
    comprimento = _numero(propriedades.get('segment_length'), propriedades.get('segment_length_uom'))
    designador = propriedades.get('text_designator', '?')
    fixo_de = propriedades.get('from_fix_ident', '?')
    fixo_para = propriedades.get('to_fix_ident', '?')
    limite_inferior = _nivel(propriedades.get('lower_limit'), propriedades.get('uom_lower_limit'))
    limite_superior = _nivel(propriedades.get('upper_limit'), propriedades.get('uom_upper_limit'))
    ficha = {
        '_titulo': f'{designador}  {fixo_de} → {fixo_para}',
        'Tipo': propriedades.get('code_type'),
        'Limites': f'{limite_inferior} a {limite_superior}',
        'Nível mínimo': _nivel_minimo(propriedades),
        'Sentido': SENTIDOS.get(propriedades.get('direction'), propriedades.get('direction')),
        'Rumo mag.': _rumos(propriedades),
        'Distância': f'{comprimento:.1f} NM' if comprimento else None,
        'Fixos': f"{propriedades.get('from_fix_type', '?')} {fixo_de} → "
                 f"{propriedades.get('to_fix_type', '?')} {fixo_para}",
        'Observação': propriedades.get('text_remark'),
        'Obs. da aerovia': propriedades.get('airway_text_remark'),
        'Vigente desde': _data_sem_z(propriedades.get('effectived')),
    }
    return _sem_vazios(ficha)


def _limite_vertical(valor, unidade, referencia) -> str:
    """Formata um limite vertical de espaço aéreo.

    Ex.: (0, 'FT', 'SFC') -> 'SFC'; (3500, 'FT', 'MSL') -> '3500 ft MSL'; (195, 'FL', 'STD') -> 'FL195'.

    Parâmetros:
        valor: número (ou texto numérico).
        unidade: 'FL', 'FT', 'M'...
        referencia: 'SFC' (vira AGL), 'MSL', 'STD' ou None.

    Retorna:
        O texto do limite; '?' se `valor` não for número.
    """
    numero = _numero(valor)
    if numero is None:
        return '?'
    if unidade == 'FL':
        if numero >= 999:
            return 'ilimitado'
        return f'FL{numero:03.0f}'
    if numero == 0 and referencia == 'SFC':
        return 'SFC'
    referencia_texto = 'AGL' if referencia == 'SFC' else (referencia or '')
    return f'{numero:.0f} {(unidade or "").lower()} {referencia_texto}'.strip()


TIPOS_ESPACO = {'FIR': 'FIR', 'TMA': 'TMA', 'SECTOR': 'Setor',
                'P': 'Área proibida', 'R': 'Área restrita', 'D': 'Área perigosa'}


def ficha_espaco(propriedades: dict) -> dict:
    """Converte as propriedades AIXM de FIR/TMA/setores em campos legíveis para o popup.

    A tabela do DECEA tem nomes de coluna truncados (vieram de shapefile):
    limite superior = upperlimit + uplimituni (unidade) + codedistve (referência);
    limite inferior = lowerlimi1 + lowerlimit (unidade!) + codedistv1 (referência).

    Parâmetros:
        propriedades: propriedades da feição, como vieram do WFS do DECEA.

    Retorna:
        Dict campo -> texto, na ordem de exibição, sem campos vazios.
    """
    tipo = TIPOS_ESPACO.get(propriedades.get('typ'), propriedades.get('typ') or '')
    limite_inferior = _limite_vertical(propriedades.get('lowerlimi1'), propriedades.get('lowerlimit'),
                                       propriedades.get('codedistv1'))
    limite_superior = _limite_vertical(propriedades.get('upperlimit'), propriedades.get('uplimituni'),
                                       propriedades.get('codedistve'))
    observacao = (propriedades.get('txtrmk_loc') or propriedades.get('classrmklo')
                  or propriedades.get('txtlocalty'))
    ficha = {
        '_titulo': f"{tipo} {propriedades.get('ident', '?')} – {propriedades.get('nam', '')}".strip(' –'),
        'Limites': f'{limite_inferior} a {limite_superior}',
        'FIR': propriedades.get('relatedfir'),
        'Horário': propriedades.get('codewrkhr'),
        'Observação': observacao,
        'Vigente desde': _data_sem_z(propriedades.get('effectived')),
    }
    return _sem_vazios(ficha)


def _limite_area(valor, unidade) -> str:
    """Formata um limite de área P/R/D (o arquivo não traz a referência MSL/AGL, só valor e unidade)."""
    numero = _numero(valor)
    if unidade == 'GND' or numero == 0:
        return 'GND'
    return _limite_vertical(numero, unidade, None)


def ficha_area(propriedades: dict) -> dict:
    """Converte as propriedades das áreas proibidas, restritas e perigosas em campos legíveis.

    As camadas ICA:eac_p/r/d têm colunas próprias, diferentes das da TMA.

    Parâmetros:
        propriedades: propriedades da feição, como vieram do WFS do DECEA.

    Retorna:
        Dict campo -> texto, na ordem de exibição, sem campos vazios.
    """
    tipo = TIPOS_ESPACO.get(propriedades.get('tipo'), propriedades.get('tipo') or 'Área')
    limite_inferior = _limite_area(propriedades.get('lowerlimit'), propriedades.get('uom_llimit'))
    limite_superior = _limite_area(propriedades.get('upperlimit'), propriedades.get('uom_ulimit'))
    ficha = {
        '_titulo': f"{tipo} {propriedades.get('id', '?')} – {propriedades.get('nome') or ''}".strip(' –'),
        'Limites': f'{limite_inferior} a {limite_superior}',
        'FIR': propriedades.get('fir'),
        'Perigo': propriedades.get('perigo'),
        'Designador': propriedades.get('designador'),
        'Observação': propriedades.get('observacao'),
        'Vigente desde': _data_sem_z(propriedades.get('efetivacao')),
    }
    return _sem_vazios(ficha)


_COLUNAS_DE_GEOMETRIA = ('bbox', 'the_geom', 'geom')


def _propriedades(chave: str, propriedades: dict) -> dict:
    """Escolhe a ficha certa para a feição, conforme a camada e as colunas que ela traz.

    Parâmetros:
        chave: chave da camada (nome do arquivo).
        propriedades: propriedades brutas da feição.

    Retorna:
        A ficha legível; para formatos desconhecidos, as propriedades brutas sem vazios
        e sem as colunas de geometria.
    """
    if chave.startswith('aerovias_') and 'text_designator' in propriedades:
        return ficha_aerovia(propriedades)
    if chave.startswith('areas_') and propriedades.get('tipo') in ('P', 'R', 'D'):
        return ficha_area(propriedades)
    if 'airspace_p' in propriedades and 'typ' in propriedades:
        return ficha_espaco(propriedades)
    sem_vazios = _sem_vazios(propriedades)
    return {campo: valor for campo, valor in sem_vazios.items() if campo.lower() not in _COLUNAS_DE_GEOMETRIA}


# ---------------------------------------------------------------------------
# Validade AIRAC e texto da fonte
# ---------------------------------------------------------------------------

def validade(caminho: Path) -> tuple[str | None, str | None]:
    """Lê do arquivo a emenda AIRAC vigente e a próxima.

    Parâmetros:
        caminho: o GeoJSON local.

    Retorna:
        (emenda vigente, próxima emenda), ex.: ('2026-10-29', '2026-11-26'); None onde o
        arquivo não trouxer a data.
    """
    geojson_preparado(caminho)  # garante a leitura (e o cache)
    return _cache[caminho][2]


def _dia_mes(data_iso: str) -> str:
    """'2026-11-26' -> '26/11'."""
    return f'{data_iso[8:10]}/{data_iso[5:7]}'


def texto_fonte(caminho: Path | None, hoje: str) -> tuple[str, bool]:
    """Monta o texto da fonte do espaço aéreo para o painel.

    Com data AIRAC no arquivo, vale a próxima emenda; sem ela, a idade do arquivo.

    Parâmetros:
        caminho: o GeoJSON local; None = sem arquivo (imagens do DECEA).
        hoje: data de hoje, 'AAAA-MM-DD' (UTC).

    Retorna:
        (texto para o painel, está velho?).
    """
    if caminho is None:
        return 'fonte: imagens do DECEA', False
    emenda, proxima = validade(caminho)
    if proxima:
        fim = _dia_mes(proxima)
        if hoje >= proxima:
            return f'fonte: arquivo local, AIRAC de {_dia_mes(emenda)} · VENCIDO em {fim}, baixe de novo', True
        return f'fonte: arquivo local, AIRAC de {_dia_mes(emenda)}, válido até {fim}', False
    dias = idade_dias(caminho)
    if dias > CICLO_AIRAC_DIAS:
        return f'fonte: arquivo local, há {dias:.0f} dias · ciclo AIRAC vencido?', True
    return f'fonte: arquivo local, há {dias:.0f} dias', False


# ---------------------------------------------------------------------------
# GeoJSON preparado para o navegador
# ---------------------------------------------------------------------------

def geojson_preparado(caminho: Path) -> bytes:
    """Lê o GeoJSON, arredonda as coordenadas e deixa as propriedades legíveis.

    O resultado fica em cache até o arquivo mudar (pela data de modificação).

    Fluxo:
        1. se o arquivo não mudou desde a última leitura, devolve o cache;
        2. para cada feição com geometria: guarda a primeira emenda AIRAC encontrada,
           troca as propriedades pela ficha e arredonda as coordenadas (5 casas ≈ 1 m);
        3. serializa em JSON compacto e guarda no cache.

    Parâmetros:
        caminho: o GeoJSON local.

    Retorna:
        Os bytes do FeatureCollection (UTF-8), prontos para a rota /espaco/<camada>.geojson.
    """
    mtime = caminho.stat().st_mtime
    if caminho in _cache and _cache[caminho][0] == mtime:
        return _cache[caminho][1]

    chave = caminho.stem
    dados = json.loads(caminho.read_text(encoding='utf-8'))
    feicoes = []
    emenda = None
    proxima = None
    for feature in dados.get('features', []):
        geometria = feature.get('geometry')
        if not geometria or 'coordinates' not in geometria:
            continue
        brutas = feature.get('properties') or {}
        emenda = emenda or _data_sem_z(brutas.get('emenda'))
        proxima = proxima or _data_sem_z(brutas.get('emenda_futura'))
        feicoes.append({'type': 'Feature', 'properties': _propriedades(chave, brutas),
                        'geometry': {'type': geometria['type'],
                                     'coordinates': geo.arredondar(geometria['coordinates'], 5)}})
    saida = json.dumps({'type': 'FeatureCollection', 'features': feicoes},
                       ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    _cache[caminho] = (mtime, saida, (emenda, proxima))
    return saida
