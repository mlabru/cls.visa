"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Vento em altitude: localiza os arquivos, recorta para a América do Sul e
calcula a idade.

Os arquivos seguem o padrão  wind_<AAAAMMDDTHHMMSSZ>_<pressão em Pa>.json  e estão
no formato do leaflet-velocity: uma lista com dois registros (componente U e V),
cada um com um `header` descrevendo a grade e uma lista `data` com os valores.

A lista de horários disponíveis é montada lendo a pasta — não há lista fixa no código.

Quem usa: a camada de vento e o painel (camadas/, tela/gaveta.py), a sonda (valor num ponto)
e o FAB.ia (impacto.py e previsao.py: o vento que leva as tempestades, `deslocamento`).
Não importa o NiceGUI.
"""
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import config
import datas

IDADE_ATUALIZA_S = 60   # o aviso de idade do arquivo no painel é refeito a cada minuto
_PADRAO = re.compile(r'^wind_(\d{8}T\d{6}Z)_(\d+)\.json$')


@dataclass(frozen=True)
class ArquivoVento:
    """Um arquivo de vento da pasta.

    Atributos:
        caminho: o arquivo.
        horario: horário de referência (UTC), tirado do nome.
        pressao_pa: nível de pressão, em Pa (50000 = 500 hPa).
    """
    caminho: Path
    horario: datetime
    pressao_pa: int


def listar_arquivos(pasta: Path = config.VENTO_DIR) -> list[ArquivoVento]:
    """Todos os arquivos de vento da pasta, do mais recente para o mais antigo.

    Parâmetros:
        pasta: onde procurar (arquivos fora do padrão wind_<horário>_<Pa>.json são ignorados).

    Retorna:
        Os arquivos, por horário e depois por pressão, em ordem decrescente.
    """
    arquivos = []
    for caminho in pasta.glob('wind_*.json'):
        partes = _PADRAO.match(caminho.name)
        if not partes:
            continue
        horario = datetime.strptime(partes.group(1), '%Y%m%dT%H%M%SZ').replace(tzinfo=UTC)
        arquivos.append(ArquivoVento(caminho, horario, int(partes.group(2))))
    return sorted(arquivos, key=lambda arquivo: (arquivo.horario, arquivo.pressao_pa), reverse=True)


def horarios_disponiveis(arquivos: list[ArquivoVento], fl: int) -> list[datetime]:
    """Horários que têm arquivo para o nível de voo pedido (mais recente primeiro).

    Parâmetros:
        arquivos: de listar_arquivos.
        fl: nível de voo (config.VENTO_FL_PARA_PA).
    """
    pressao_pa = config.VENTO_FL_PARA_PA.get(fl)
    return [arquivo.horario for arquivo in arquivos if arquivo.pressao_pa == pressao_pa]


def escolher(arquivos: list[ArquivoVento], fl: int, horario: datetime | None) -> ArquivoVento | None:
    """Arquivo do nível de voo pedido, no horário pedido (ou o mais recente).

    Parâmetros:
        arquivos: de listar_arquivos (mais recente primeiro).
        fl: nível de voo (config.VENTO_FL_PARA_PA).
        horario: horário pedido; None = o mais recente.

    Retorna:
        O arquivo, ou None se não houver.
    """
    pressao_pa = config.VENTO_FL_PARA_PA.get(fl)
    candidatos = [arquivo for arquivo in arquivos if arquivo.pressao_pa == pressao_pa]
    if horario is not None:
        candidatos = [arquivo for arquivo in candidatos if arquivo.horario == horario]
    return candidatos[0] if candidatos else None


def _recortar_registro(registro: dict, recorte: dict) -> dict:
    """Recorta um registro (U ou V) da grade global para a área de interesse.

    Parâmetros:
        registro: {'header': ..., 'data': [...]} no formato do leaflet-velocity; a grade começa
            no canto noroeste (lo1, la1), com longitudes 0..360 e latitudes decrescentes.
        recorte: dict com lat_norte, lat_sul, lon_oeste e lon_leste, em graus.

    Retorna:
        Um registro no mesmo formato, só com a área do recorte (header ajustado).
    """
    cabecalho = registro['header']
    passo_lon = cabecalho['dx']
    passo_lat = cabecalho['dy']
    colunas_global = cabecalho['nx']
    lon_inicial = cabecalho['lo1']       # canto noroeste da grade original
    lat_inicial = cabecalho['la1']

    # Colunas: longitudes 0..360 a partir de lon_inicial; a área pode cruzar o meridiano 0
    lon_oeste = recorte['lon_oeste'] % 360
    primeira_coluna = round((lon_oeste - lon_inicial) / passo_lon) % colunas_global
    colunas = round((recorte['lon_leste'] - recorte['lon_oeste']) / passo_lon) + 1
    # Linhas: latitudes decrescem a partir de lat_inicial
    primeira_linha = round((lat_inicial - recorte['lat_norte']) / passo_lat)
    linhas = round((recorte['lat_norte'] - recorte['lat_sul']) / passo_lat) + 1

    dados = registro['data']
    novo = []
    for linha in range(primeira_linha, primeira_linha + linhas):
        inicio_da_linha = linha * colunas_global
        novo.extend(dados[inicio_da_linha + (primeira_coluna + coluna) % colunas_global] for coluna in range(colunas))

    novo_cabecalho = dict(cabecalho, lo1=lon_inicial + primeira_coluna * passo_lon,
                          la1=lat_inicial - primeira_linha * passo_lat, nx=colunas, ny=linhas)
    novo_cabecalho['lo2'] = novo_cabecalho['lo1'] + (colunas - 1) * passo_lon
    novo_cabecalho['la2'] = novo_cabecalho['la1'] - (linhas - 1) * passo_lat
    return {'header': novo_cabecalho, 'data': novo}


@lru_cache(maxsize=32)
def carregar_recortado(caminho: Path) -> list[dict]:
    """Lê o arquivo e devolve só a área de VENTO_RECORTE (≈ 10× menor que o global).

    Guardado em cache (32 arquivos): cada clique da sonda lê os 17 níveis.

    Parâmetros:
        caminho: o arquivo de vento.

    Retorna:
        Os dois registros (U e V) recortados.
    """
    registros = json.loads(caminho.read_text())
    return [_recortar_registro(registro, config.VENTO_RECORTE) for registro in registros]


def horario_referencia(dados: list[dict], arquivo: ArquivoVento) -> datetime:
    """Horário do `header.refTime` do JSON; se faltar, o do nome do arquivo.

    Parâmetros:
        dados: os registros do arquivo (de carregar_recortado).
        arquivo: o arquivo, para o horário do nome.
    """
    texto = dados[0]['header'].get('refTime') if dados else None
    return datas.ler_iso(texto) or arquivo.horario


def idade(referencia: datetime, agora: datetime | None = None) -> tuple[str, str]:
    """Texto da idade do dado e a cor do aviso.

    Parâmetros:
        referencia: horário de referência do arquivo (UTC).
        agora: hora atual (None = agora de verdade).

    Retorna:
        (texto como 'há 3 h', cor 'positive', 'warning' ou 'negative'); passado
        VENTO_IDADE_VELHO_H, o texto ganha ' · DESATUALIZADO'.
    """
    agora = agora or datetime.now(UTC)
    horas = (agora - referencia).total_seconds() / 3600
    if horas < 1:
        texto = f'há {max(0, round(horas * 60))} min'
    elif horas < 48:
        texto = f'há {horas:.0f} h'
    else:
        texto = f'há {horas / 24:.0f} dias'
    if horas <= config.VENTO_IDADE_ALERTA_H:
        return texto, 'positive'
    if horas <= config.VENTO_IDADE_VELHO_H:
        return texto, 'warning'
    return texto + ' · DESATUALIZADO', 'negative'


# ---------------------------------------------------------------------------
# Valor num ponto (para a sonda)
# ---------------------------------------------------------------------------

KT_POR_MS = 1.943844


def uv_no_ponto(registros: list[dict], lat: float, lon: float) -> tuple[float, float] | None:
    """Componentes do vento no ponto de grade mais próximo.

    Parâmetros:
        registros: [U, V] de carregar_recortado.
        lat, lon: o ponto, em graus.

    Retorna:
        (u para leste, v para norte), em m/s; None fora da grade, sem valor ou com arquivo malformado.
    """
    try:
        registro_u = registros[0]
        registro_v = registros[1]
        cabecalho = registro_u['header']
        coluna = round(((lon - cabecalho['lo1']) % 360) / cabecalho['dx'])
        linha = round((cabecalho['la1'] - lat) / cabecalho['dy'])
        if not (0 <= coluna < cabecalho['nx'] and 0 <= linha < cabecalho['ny']):
            return None
        indice = linha * cabecalho['nx'] + coluna
        vento_u = registro_u['data'][indice]
        vento_v = registro_v['data'][indice]
    except (IndexError, KeyError, TypeError):
        return None
    if vento_u is None or vento_v is None:
        return None
    return float(vento_u), float(vento_v)


def valor_no_ponto(registros: list[dict], lat: float, lon: float) -> tuple[int, int] | None:
    """Vento no ponto de grade mais próximo, como se diz na aviação.

    Parâmetros:
        registros: [U, V] de carregar_recortado.
        lat, lon: o ponto, em graus.

    Retorna:
        (direção de onde vem, em graus, de 1 a 360; velocidade em kt), ou None sem valor.
    """
    componentes = uv_no_ponto(registros, lat, lon)
    if componentes is None:
        return None
    return direcao_velocidade(*componentes)


def direcao_velocidade(vento_u: float, vento_v: float) -> tuple[int, int]:
    """Componentes do vento como se diz na aviação (também usado pelo perfil do modelo WRF).

    Parâmetros:
        vento_u: componente para leste, m/s.
        vento_v: componente para norte, m/s.

    Retorna:
        (direção de onde vem, em graus, de 1 a 360; velocidade em kt).
    """
    direcao = round((270 - math.degrees(math.atan2(vento_v, vento_u))) % 360)
    if direcao == 0:
        direcao = 360
    return direcao, round(math.hypot(vento_u, vento_v) * KT_POR_MS)


# ---------------------------------------------------------------------------
# Vento que leva as tempestades (para o movimento dos núcleos do FAB.ia, previsao.py)
# ---------------------------------------------------------------------------

# As tempestades andam, grosso modo, com o vento médio da camada 850–300 hPa ("vento de
# deslocamento"). É uma aproximação: células fortes podem desviar uns 30° e andar mais devagar.
DESLOCAMENTO_PA = (85000, 70000, 50000, 30000)
DESLOCAMENTO_IDADE_MAX_H = config.VENTO_IDADE_VELHO_H   # arquivo mais longe de agora que isso: não usa
KM_MIN_POR_MS = 0.06                                     # 1 m/s = 0,06 km/min


def arquivos_de_deslocamento(agora: datetime, arquivos: list[ArquivoVento]) -> list[ArquivoVento]:
    """Os arquivos de DESLOCAMENTO_PA do horário mais perto de `agora` que tenha todos os níveis.

    Vazio se não houver horário completo a menos de DESLOCAMENTO_IDADE_MAX_H de agora: vento
    velho daria um movimento pior que nenhum.

    Parâmetros:
        agora: hora de referência (UTC).
        arquivos: de listar_arquivos.

    Retorna:
        Um arquivo por nível, na ordem de DESLOCAMENTO_PA; vazio se não houver horário bom.
    """
    por_horario: dict[datetime, dict[int, ArquivoVento]] = {}
    for arquivo in arquivos:
        if arquivo.pressao_pa in DESLOCAMENTO_PA:
            por_horario.setdefault(arquivo.horario, {})[arquivo.pressao_pa] = arquivo

    def distancia_s(horario: datetime) -> float:
        """Quantos segundos o horário está de `agora`, para mais ou para menos."""
        return abs((agora - horario).total_seconds())

    limite_s = DESLOCAMENTO_IDADE_MAX_H * 3600
    completos = []
    for horario, niveis in por_horario.items():
        if len(niveis) == len(DESLOCAMENTO_PA) and distancia_s(horario) <= limite_s:
            completos.append(horario)
    if not completos:
        return []
    melhor = min(completos, key=distancia_s)
    return [por_horario[melhor][pressao_pa] for pressao_pa in DESLOCAMENTO_PA]


def deslocamento_medio(niveis: list[list[dict]], lat: float, lon: float) -> tuple[float, float] | None:
    """A média do vento nos níveis dados, como velocidade de deslocamento.

    Parâmetros:
        niveis: os registros [U, V] de cada nível (de carregar_recortado).
        lat, lon: o ponto, em graus.

    Retorna:
        (para o norte, para o leste), em km/min; None se faltar o valor em algum nível.
    """
    valores = []
    for registros in niveis:
        componentes = uv_no_ponto(registros, lat, lon)
        if componentes is None:
            return None
        valores.append(componentes)
    if not valores:
        return None
    media_u = sum(componentes[0] for componentes in valores) / len(valores)
    media_v = sum(componentes[1] for componentes in valores) / len(valores)
    return media_v * KM_MIN_POR_MS, media_u * KM_MIN_POR_MS


def deslocamento(agora: datetime, pasta: Path = config.VENTO_DIR
                 ) -> Callable[[float, float], tuple[float, float] | None] | None:
    """O vento de deslocamento das tempestades, como função de um ponto.

    Lê os arquivos uma vez (carregar_recortado guarda) e devolve a função que o previsao.py usa.

    Parâmetros:
        agora: hora de referência (UTC), para escolher o horário dos arquivos.
        pasta: a pasta dos arquivos de vento.

    Retorna:
        Função (lat, lon) -> (vn, ve) em km/min (ou None no ponto sem valor); None se não
        houver vento recente na pasta ou se os arquivos estiverem ilegíveis.
    """
    escolhidos = arquivos_de_deslocamento(agora, listar_arquivos(pasta))
    if not escolhidos:
        return None
    try:
        niveis = [carregar_recortado(arquivo.caminho) for arquivo in escolhidos]
    except (OSError, ValueError, KeyError, TypeError):
        return None

    def no_ponto(lat: float, lon: float) -> tuple[float, float] | None:
        """Vento de deslocamento no ponto, em km/min (para o norte, para o leste)."""
        return deslocamento_medio(niveis, lat, lon)

    return no_ponto


_FL_DA_PRESSAO = {pressao_pa: fl for fl, pressao_pa in config.VENTO_FL_PARA_PA.items()}


def fl_da_pressao(pa: int) -> int:
    """Nível de voo de um nível de pressão.

    Para os níveis do painel de vento, o mesmo do painel (500 hPa = FL180); para os outros, a
    atmosfera padrão arredondada de 5 em 5.

    Parâmetros:
        pa: pressão, em Pa.
    """
    if pa in _FL_DA_PRESSAO:
        return _FL_DA_PRESSAO[pa]
    pes = 145366.45 * (1 - (pa / 101325) ** 0.190284)
    return round(pes / 100 / 5) * 5
