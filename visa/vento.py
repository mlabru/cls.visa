"""
Vento em altitude: localiza os arquivos, recorta para a América do Sul e calcula a idade.

Os arquivos seguem o padrão  wind_<AAAAMMDDTHHMMSSZ>_<pressão em Pa>.json  e estão
no formato do leaflet-velocity: uma lista com dois registros (componente U e V),
cada um com um `header` descrevendo a grade e uma lista `data` com os valores.

A lista de horários disponíveis é montada lendo a pasta — não há lista fixa no código.
"""
import json
import math
import re
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
    caminho: Path
    horario: datetime   # horário de referência (UTC)
    pressao_pa: int


def listar_arquivos(pasta: Path = config.VENTO_DIR) -> list[ArquivoVento]:
    """Todos os arquivos de vento da pasta, do mais recente para o mais antigo."""
    arquivos = []
    for caminho in pasta.glob('wind_*.json'):
        m = _PADRAO.match(caminho.name)
        if not m:
            continue
        horario = datetime.strptime(m.group(1), '%Y%m%dT%H%M%SZ').replace(tzinfo=UTC)
        arquivos.append(ArquivoVento(caminho, horario, int(m.group(2))))
    return sorted(arquivos, key=lambda a: (a.horario, a.pressao_pa), reverse=True)


def horarios_disponiveis(arquivos: list[ArquivoVento], fl: int) -> list[datetime]:
    """Horários que têm arquivo para o nível de voo pedido (mais recente primeiro)."""
    pa = config.VENTO_FL_PARA_PA.get(fl)
    return [a.horario for a in arquivos if a.pressao_pa == pa]


def escolher(arquivos: list[ArquivoVento], fl: int, horario: datetime | None) -> ArquivoVento | None:
    """Arquivo do nível de voo pedido, no horário pedido (ou o mais recente)."""
    pa = config.VENTO_FL_PARA_PA.get(fl)
    candidatos = [a for a in arquivos if a.pressao_pa == pa]
    if horario is not None:
        candidatos = [a for a in candidatos if a.horario == horario]
    return candidatos[0] if candidatos else None


def _recortar_registro(registro: dict, recorte: dict) -> dict:
    """Recorta um registro (U ou V) da grade global para a área de interesse."""
    h = registro['header']
    dx, dy, nx = h['dx'], h['dy'], h['nx']
    lo1, la1 = h['lo1'], h['la1']        # canto noroeste da grade original

    # Colunas: longitudes 0..360 a partir de lo1; a área pode cruzar o meridiano 0
    lon_oeste = recorte['lon_oeste'] % 360
    col0 = round((lon_oeste - lo1) / dx) % nx
    ncols = round((recorte['lon_leste'] - recorte['lon_oeste']) / dx) + 1
    # Linhas: latitudes decrescem a partir de la1
    lin0 = round((la1 - recorte['lat_norte']) / dy)
    nlins = round((recorte['lat_norte'] - recorte['lat_sul']) / dy) + 1

    dados = registro['data']
    novo = []
    for lin in range(lin0, lin0 + nlins):
        base = lin * nx
        novo.extend(dados[base + (col0 + c) % nx] for c in range(ncols))

    cabecalho = dict(h, lo1=lo1 + col0 * dx, la1=la1 - lin0 * dy, nx=ncols, ny=nlins)
    cabecalho['lo2'] = cabecalho['lo1'] + (ncols - 1) * dx
    cabecalho['la2'] = cabecalho['la1'] - (nlins - 1) * dy
    return {'header': cabecalho, 'data': novo}


@lru_cache(maxsize=32)
def carregar_recortado(caminho: Path) -> list[dict]:
    """Lê o arquivo e devolve só a área de VENTO_RECORTE (≈ 10× menor que o global)."""
    registros = json.loads(caminho.read_text())
    return [_recortar_registro(r, config.VENTO_RECORTE) for r in registros]


def horario_referencia(dados: list[dict], arquivo: ArquivoVento) -> datetime:
    """Horário do `header.refTime` do JSON; se faltar, o do nome do arquivo."""
    texto = dados[0]['header'].get('refTime') if dados else None
    return datas.ler_iso(texto) or arquivo.horario


def idade(referencia: datetime, agora: datetime | None = None) -> tuple[str, str]:
    """Texto da idade do dado e a cor do aviso ('positive', 'warning', 'negative')."""
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


def valor_no_ponto(registros: list[dict], lat: float, lon: float) -> tuple[int, int] | None:
    """(direção de onde vem, em graus; velocidade em kt) no ponto de grade mais próximo."""
    try:
        u_reg, v_reg = registros[0], registros[1]
        h = u_reg['header']
        col = round(((lon - h['lo1']) % 360) / h['dx'])
        lin = round((h['la1'] - lat) / h['dy'])
        if not (0 <= col < h['nx'] and 0 <= lin < h['ny']):
            return None
        u, v = u_reg['data'][lin * h['nx'] + col], v_reg['data'][lin * h['nx'] + col]
    except (IndexError, KeyError, TypeError):
        return None
    if u is None or v is None:
        return None
    direcao = round((270 - math.degrees(math.atan2(v, u))) % 360)
    return (direcao if direcao else 360), round(math.hypot(u, v) * KT_POR_MS)


_FL_DA_PRESSAO = {pa: fl for fl, pa in config.VENTO_FL_PARA_PA.items()}


def fl_da_pressao(pa: int) -> int:
    """Nível de voo de um nível de pressão: o mesmo do painel de vento (500 hPa = FL180);
    para os outros níveis, a atmosfera padrão arredondada de 5 em 5."""
    if pa in _FL_DA_PRESSAO:
        return _FL_DA_PRESSAO[pa]
    pes = 145366.45 * (1 - (pa / 101325) ** 0.190284)
    return round(pes / 100 / 5) * 5
