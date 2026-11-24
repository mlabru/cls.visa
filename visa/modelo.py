"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Modelo numérico (WRF): lê os arquivos netCDF da pasta e devolve os campos como imagem.

Os arquivos seguem o padrão  wrfd<domínio>_<AAAAMMDDHH>.nc : um por domínio e por rodada
(d1 = o domínio maior, ~18 km; d2 = o interno, ~5 km). Cada um tem a previsão de hora em hora
(variável `time`) em 22 níveis de pressão (`lev`, hPa); os campos de superfície usam `lev_2`.

O que este módulo garante:
  - **a grade é Mercator** (a do mapa): as latitudes do arquivo não são igualmente espaçadas,
    mas o y de Mercator é. Assim cada campo vira imagem sem reprojeção. O arquivo não diz a
    projeção; se um dia vier em outra, `grade_das_coordenadas` recusa em vez de desenhar torto;
  - **um campo só por imagem:** o d2 é encaixado dentro do d1 (`compor`), na resolução do d2;
  - **as datas certas:** a referência do arquivo é "hours since 1-1-1", no calendário misto
    (juliano/gregoriano). Com o `datetime` do Python (gregoriano proléptico) as datas sairiam
    2 dias adiantadas; por isso são lidas pelo `cftime` (`netCDF4.num2date`);
  - **leitura sob trava:** a biblioteca do netCDF não pode ser usada por duas threads ao mesmo
    tempo. Cada leitura abre o arquivo, lê um campo 2D (10–150 ms) e fecha, dentro de `_LEITURA`.

É previsão, não observação: a refletividade daqui é simulada pelo modelo e tem legenda própria.
Não alimenta o painel de impacto nem o FAB.ia.

Quem usa: a rota /modelo/imagem (main.py), a camada `CamadaModelo` (camadas/prevista.py), o painel,
a legenda e a barra de saúde (tela/), e a sonda (`perfil`, sonda.py). Não importa o NiceGUI.
"""
import io
import itertools
import logging
import math
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import netCDF4
import numpy as np
from PIL import Image

import cache
import config
import geo
import vento

log = logging.getLogger(__name__)

_PADRAO = re.compile(r'^wrfd(\d+)_(\d{10})\.nc$')
_FORMATO_HORARIO = '%Y%m%d%H'   # nomes de rodada e horários na URL: AAAAMMDDHH
TOLERANCIA_GRADE = 0.01         # passo da grade (lon e y de Mercator) pode variar até 1%
KT_POR_MS = 1.943844
_LEITURA = threading.Lock()     # uma leitura de netCDF por vez (a biblioteca não é thread-safe)


class Indisponivel(Exception):
    """O campo pedido não pode ser lido; a mensagem já vem pronta para a tela."""


# ---------------------------------------------------------------------------
# Rodadas (arquivos da pasta)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rodada:
    """Uma rodada do modelo: os arquivos de todos os domínios com o mesmo início.

    Atributos:
        nome: o início, como no nome do arquivo (AAAAMMDDHH).
        inicio: o início da rodada (UTC).
        arquivos: um por domínio, do maior (d1) para o menor.
    """
    nome: str
    inicio: datetime
    arquivos: tuple[Path, ...]


def listar_rodadas(pasta: Path | None = None) -> list[Rodada]:
    """Todas as rodadas da pasta, da mais recente para a mais antiga.

    Parâmetros:
        pasta: onde procurar (arquivos fora do padrão wrfd<N>_<AAAAMMDDHH>.nc são ignorados);
            None = config.MODELO_DIR, lido a cada chamada (os testes trocam a pasta por ele).
    """
    pasta = pasta or config.MODELO_DIR
    por_nome: dict[str, list[tuple[int, Path]]] = {}
    for caminho in pasta.glob('wrfd*.nc'):
        partes = _PADRAO.match(caminho.name)
        if partes:
            por_nome.setdefault(partes.group(2), []).append((int(partes.group(1)), caminho))
    rodadas = []
    for nome, dominios in por_nome.items():
        try:
            inicio = datetime.strptime(nome, _FORMATO_HORARIO).replace(tzinfo=UTC)
        except ValueError:
            continue
        rodadas.append(Rodada(nome, inicio, tuple(caminho for _, caminho in sorted(dominios))))
    return sorted(rodadas, key=lambda rodada: rodada.inicio, reverse=True)


def rodada_por_nome(nome: str, pasta: Path | None = None) -> Rodada | None:
    """A rodada com esse nome (AAAAMMDDHH), ou None se não houver."""
    return next((rodada for rodada in listar_rodadas(pasta) if rodada.nome == nome), None)


# ---------------------------------------------------------------------------
# Grade (Mercator)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Grade:
    """Grade regular em Mercator, com a linha 0 no norte (como numa imagem).

    Atributos:
        oeste: borda oeste da grade (não o centro da primeira célula), em graus.
        norte_y: borda norte, em y de Mercator (radianos).
        passo_lon: largura de uma célula, em graus.
        passo_y: altura de uma célula, em y de Mercator.
        nx, ny: colunas e linhas.
    """
    oeste: float
    norte_y: float
    passo_lon: float
    passo_y: float
    nx: int
    ny: int

    @property
    def leste(self) -> float:
        return self.oeste + self.nx * self.passo_lon

    @property
    def sul_y(self) -> float:
        return self.norte_y - self.ny * self.passo_y

    def limites(self) -> list[list[float]]:
        """[[sul, oeste], [norte, leste]] em graus, no formato do Leaflet (bordas das células)."""
        return [[geo.lat_de_mercator(self.sul_y), self.oeste], [geo.lat_de_mercator(self.norte_y), self.leste]]

    def centros_lon(self) -> np.ndarray:
        """Longitude do centro de cada coluna."""
        return self.oeste + (np.arange(self.nx) + 0.5) * self.passo_lon

    def centros_y(self) -> np.ndarray:
        """y de Mercator do centro de cada linha (do norte para o sul)."""
        return self.norte_y - (np.arange(self.ny) + 0.5) * self.passo_y

    def centros_lat(self) -> np.ndarray:
        """Latitude do centro de cada linha (do norte para o sul), em graus."""
        return np.array([geo.lat_de_mercator(float(y)) for y in self.centros_y()])

    def indice(self, lat: float, lon: float) -> tuple[int, int] | None:
        """(linha, coluna) da célula que contém o ponto; None fora da grade."""
        coluna = math.floor((lon - self.oeste) / self.passo_lon)
        linha = math.floor((self.norte_y - geo.mercator_y(lat)) / self.passo_y)
        if 0 <= linha < self.ny and 0 <= coluna < self.nx:
            return linha, coluna
        return None


def _passo_regular(valores: np.ndarray, o_que: str) -> float:
    """O passo médio de uma sequência crescente; Indisponivel se não for regular."""
    passos = np.diff(valores)
    medio = float(passos.mean())
    if medio <= 0 or (passos.max() - passos.min()) / medio > TOLERANCIA_GRADE:
        raise Indisponivel(f'Modelo: grade irregular em {o_que} (o arquivo não está em Mercator?)')
    return medio


def grade_das_coordenadas(lats: np.ndarray, lons: np.ndarray) -> Grade:
    """A grade de Mercator descrita pelas coordenadas dos centros das células.

    Parâmetros:
        lats: latitudes dos centros das linhas, do sul para o norte (como no arquivo).
        lons: longitudes dos centros das colunas, de oeste para leste (0..360 ou -180..180).

    Retorna:
        A grade, com bordas meia célula além dos centros.

    Exceções:
        Indisponivel: se o espaçamento não for regular em longitude e em y de Mercator.
    """
    lons = (np.asarray(lons, dtype=float) + 180) % 360 - 180
    ys = np.array([geo.mercator_y(float(lat)) for lat in lats])
    passo_lon = _passo_regular(lons, 'longitude')
    passo_y = _passo_regular(ys, 'latitude')
    return Grade(oeste=float(lons[0]) - passo_lon / 2, norte_y=float(ys[-1]) + passo_y / 2,
                 passo_lon=passo_lon, passo_y=passo_y, nx=len(lons), ny=len(ys))


# ---------------------------------------------------------------------------
# Leitura dos arquivos
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Cabecalho:
    """O que se sabe de um arquivo sem ler os campos.

    Atributos:
        grade: a grade (linha 0 no norte).
        horarios: os horários previstos (UTC), na ordem do arquivo.
        niveis_hpa: os níveis de pressão (hPa), na ordem do arquivo.
    """
    grade: Grade
    horarios: tuple[datetime, ...]
    niveis_hpa: tuple[int, ...]


def _para_utc(data) -> datetime:
    """Um cftime (ou datetime) como datetime do Python em UTC."""
    return datetime(data.year, data.month, data.day, data.hour, data.minute, tzinfo=UTC)


@lru_cache(maxsize=16)
def _cabecalho(caminho: Path, _mtime_ns: int) -> Cabecalho:
    """Lê o cabeçalho; `_mtime_ns` faz o cache esquecer um arquivo regravado."""
    try:
        with _LEITURA, netCDF4.Dataset(caminho) as arquivo:
            lats = np.asarray(arquivo['lat'][:], dtype=float)
            lons = np.asarray(arquivo['lon'][:], dtype=float)
            tempo = arquivo['time']
            horarios = tuple(_para_utc(data) for data in netCDF4.num2date(
                tempo[:], tempo.units, getattr(tempo, 'calendar', 'standard')))
            niveis = tuple(round(float(nivel)) for nivel in arquivo['lev'][:])
    except (OSError, KeyError, ValueError, AttributeError) as erro:
        raise Indisponivel(f'Modelo: não foi possível ler {caminho.name} ({erro})') from erro
    if len(lats) > 1 and lats[0] > lats[-1]:
        raise Indisponivel(f'Modelo: {caminho.name} tem as latitudes do norte para o sul (não previsto)')
    return Cabecalho(grade_das_coordenadas(lats, lons), horarios, niveis)


def cabecalho(caminho: Path) -> Cabecalho:
    """O cabeçalho de um arquivo (grade, horários, níveis), guardado em cache.

    Exceções:
        Indisponivel: arquivo ilegível ou fora de Mercator.
    """
    try:
        mtime_ns = caminho.stat().st_mtime_ns
    except OSError as erro:
        raise Indisponivel(f'Modelo: arquivo {caminho.name} não encontrado') from erro
    return _cabecalho(caminho, mtime_ns)


def _ler(caminho: Path, variavel: str, passo: int, nivel: int) -> np.ndarray:
    """Um campo 2D do arquivo, com a linha 0 no norte e NaN onde não há valor.

    Parâmetros:
        caminho: o arquivo.
        variavel: nome da variável no netCDF.
        passo: índice do horário.
        nivel: índice do nível (0 para os campos de superfície, que só têm um).
    """
    try:
        with _LEITURA, netCDF4.Dataset(caminho) as arquivo:
            if variavel not in arquivo.variables:
                raise Indisponivel(f'Modelo: {caminho.name} não tem a variável {variavel}')
            dados = arquivo[variavel]
            fatia = dados[passo, nivel] if dados.ndim == 4 else dados[passo]
    except (OSError, IndexError) as erro:
        raise Indisponivel(f'Modelo: não foi possível ler {variavel} de {caminho.name} ({erro})') from erro
    valores = np.ma.filled(np.ma.asarray(fatia, dtype=np.float32), np.nan)
    return valores[::-1]   # o arquivo vem do sul para o norte


# ---------------------------------------------------------------------------
# Campos oferecidos
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Campo:
    """Um campo do modelo que vira camada.

    Atributos:
        chave: nome curto (URL, preferências).
        titulo: nome no painel.
        unidade: unidade dos valores, depois de `fator`.
        variavel: variável do netCDF (a chuva usa duas: ver `ler_campo`).
        por_nivel: True se tem um valor por nível de pressão (escolhido pelo FL).
        paleta: (limite inferior, cor) em ordem crescente; abaixo do primeiro limite, transparente.
        fator: multiplica o valor do arquivo (ex.: m/s → kt).
        rotulos: nome de cada faixa da paleta (ex.: leve, moderada, forte); vazio = a legenda
            mostra os números.
        experimental: índice com limiares ainda não validados (a tela avisa).
        so_externo: calculado só no domínio externo (d1), sem encaixar o d2.
    """
    chave: str
    titulo: str
    unidade: str
    variavel: str
    por_nivel: bool
    paleta: tuple[tuple[float, str], ...]
    fator: float = 1.0
    rotulos: tuple[str, ...] = ()
    experimental: bool = False
    so_externo: bool = False


CAMPOS = [
    # Cores diferentes das do radar de propósito: é refletividade simulada, não observada.
    Campo('refletividade', 'Refletividade máxima simulada', 'dBZ', 'max_dbz', False, (
        (20, '#7fcdbb'), (25, '#41b6c4'), (30, '#1d91c0'), (35, '#225ea8'), (40, '#fed976'),
        (45, '#feb24c'), (50, '#fd8d3c'), (55, '#f03b20'), (60, '#bd0026'), (65, '#7a0177'))),
    Campo('chuva_1h', 'Chuva na última hora', 'mm', 'rainc+rainnc', False, (
        (0.5, '#c6dbef'), (1, '#9ecae1'), (2, '#6baed6'), (5, '#3182bd'), (10, '#08519c'),
        (20, '#756bb1'), (30, '#54278f'), (50, '#dd3497'))),
    Campo('cape', 'CAPE máximo', 'J/kg', 'mcape', False, (
        (250, '#ffffb2'), (500, '#fed976'), (1000, '#feb24c'), (1500, '#fd8d3c'), (2000, '#fc4e2a'),
        (3000, '#e31a1c'), (4000, '#b10026'))),
    Campo('vento', 'Vento', 'kt', 'wspd', True, (
        (20, '#c7e9c0'), (40, '#74c476'), (60, '#31a354'), (80, '#fed976'), (100, '#fd8d3c'),
        (120, '#e31a1c'), (150, '#800026')), fator=KT_POR_MS),
    # De 5 em 5 °C entre −50 e +30: num mesmo nível a temperatura varia poucos graus (com faixas
    # de 10 °C, 500 hPa saía numa cor só). Fronteira azul/amarelo em 0 °C; 0 a −20 °C é a faixa do gelo.
    Campo('temperatura', 'Temperatura', '°C', 'tc', True, (
        (-90, '#2d004b'), (-60, '#3f007d'), (-50, '#54278f'), (-45, '#6a51a3'), (-40, '#807dba'),
        (-35, '#9e9ac8'), (-30, '#08306b'),
        (-25, '#08519c'), (-20, '#2171b5'), (-15, '#4292c6'), (-10, '#6baed6'), (-5, '#9ecae1'),
        (0, '#fee391'), (5, '#fec44f'), (10, '#fe9929'), (15, '#ec7014'), (20, '#cc4c02'),
        (25, '#993404'), (30, '#662506'))),
    Campo('umidade', 'Umidade relativa', '%', 'rh', True, (
        (50, '#d9f0a3'), (60, '#addd8e'), (70, '#78c679'), (80, '#41ab5d'), (90, '#238443'),
        (95, '#005a32'))),
    # Índices (ver DECISOES 2.7): limiares de partida, a calibrar com observações (PIREP).
    # Ellrod & Knapp (1992): TI1 em 10⁻⁷ s⁻². Só no d1: o índice foi feito para grades grossas,
    # e no d2 (5 km) a convecção resolvida pelo modelo o satura (24/11 18Z, 250 hPa: 15% da
    # área do d2 acima de 12, contra 2% do d1; 20% desses pixels sob ≥ 30 dBZ simulados).
    Campo('turbulencia', 'Turbulência (Ellrod TI1)', '×10⁻⁷ s⁻²', 'u+v+geopt', True, (
        (4, '#fde047'), (8, '#f97316'), (12, '#dc2626')),
        rotulos=('leve', 'moderada', 'forte'), experimental=True, so_externo=True),
    # Água de nuvem líquida entre 0 e −20 °C (super-resfriada), em g/kg. Nos dados de
    # 24/11 18Z em 500 hPa: mediana 0,05, percentil 90 0,33, percentil 99 0,61 g/kg.
    Campo('gelo', 'Gelo (água super-resfriada)', 'g/kg', 'qcloud+tc', True, (
        (0.05, '#a5f3fc'), (0.2, '#22d3ee'), (0.5, '#0e7490')),
        rotulos=('leve', 'moderado', 'forte'), experimental=True),
]
POR_CHAVE = {campo.chave: campo for campo in CAMPOS}


GRAVIDADE = 9.80665             # m/s²: geopotencial (m²/s²) ÷ g = altura (m)
_RAIO_TERRA_M = geo.RAIO_TERRA_KM * 1000
GELO_T_MAX, GELO_T_MIN = 0.0, -20.0     # °C: a faixa da água super-resfriada que forma gelo
# O TI1 é para turbulência em céu claro na alta troposfera; perto do chão, o cisalhamento vem
# do atrito e do aquecimento, e o índice marca demais (24/11 18Z: 'moderada' em 900 hPa).
TURBULENCIA_HPA_MAX = 500              # só de 500 hPa (FL180) para cima


def ellrod_ti1(u: np.ndarray, v: np.ndarray, z: np.ndarray, passo_lon: float, passo_y: float,
               lats: np.ndarray) -> np.ndarray:
    """Índice de turbulência TI1 de Ellrod: cisalhamento vertical × deformação horizontal.

    TI1 = VWS × DEF, com VWS = |∂V/∂z| e DEF = √[(∂u/∂x − ∂v/∂y)² + (∂v/∂x + ∂u/∂y)²].

    O cisalhamento é a diferença centrada entre o nível de baixo e o de cima (nas pontas, com
    o vizinho). Na grade Mercator, uma célula mede, em metros, passo × R × cos(lat) nas duas
    direções (passo_lon em graus, passo_y em y de Mercator).

    Parâmetros:
        u, v: vento (m/s), forma (níveis, linhas, colunas), linha 0 no norte; ≥ 2 de cada.
        z: altura de cada nível (m), mesma forma.
        passo_lon: largura de uma célula, graus.
        passo_y: altura de uma célula, em y de Mercator.
        lats: latitude do centro de cada linha (do norte para o sul).

    Retorna:
        TI1 em 10⁻⁷ s⁻², na mesma forma (NaN onde falta valor, ex.: abaixo do terreno).
    """
    coseno = np.cos(np.radians(lats))[None, :, None]
    largura_m = math.radians(passo_lon) * _RAIO_TERRA_M * coseno
    altura_m = passo_y * _RAIO_TERRA_M * coseno
    with np.errstate(invalid='ignore', divide='ignore'):
        espessura = np.gradient(z, axis=0)
        cisalhamento = np.hypot(np.gradient(u, axis=0) / espessura, np.gradient(v, axis=0) / espessura)
        du_dx = np.gradient(u, axis=2) / largura_m
        dv_dx = np.gradient(v, axis=2) / largura_m
        du_dy = -np.gradient(u, axis=1) / altura_m      # linha 0 no norte: descer uma linha é ir para o sul
        dv_dy = -np.gradient(v, axis=1) / altura_m
        deformacao = np.hypot(du_dx - dv_dy, dv_dx + du_dy)
    return cisalhamento * deformacao * 1e7


def agua_super_resfriada(temperatura: np.ndarray, agua_nuvem: np.ndarray) -> np.ndarray:
    """Água de nuvem (g/kg) onde a temperatura está entre GELO_T_MIN e GELO_T_MAX; NaN fora.

    Parâmetros:
        temperatura: °C.
        agua_nuvem: razão de mistura da água de nuvem (qcloud), kg/kg.
    """
    with np.errstate(invalid='ignore'):
        na_faixa = (temperatura <= GELO_T_MAX) & (temperatura >= GELO_T_MIN)
    return np.where(na_faixa, agua_nuvem * 1000, np.nan)


def categoria(campo: Campo, valor: float | None) -> str | None:
    """O rótulo da faixa do valor ('leve', 'moderada'...); None abaixo da primeira ou sem valor."""
    if valor is None or not math.isfinite(valor) or not campo.rotulos:
        return None
    rotulo = None
    for (limite, _), nome in zip(campo.paleta, campo.rotulos, strict=True):
        if valor >= limite:
            rotulo = nome
    return rotulo


def ler_campo(caminho: Path, campo: Campo, horario: datetime, nivel_hpa: int | None = None) -> np.ndarray:
    """O campo num horário (e nível), já na unidade do `Campo`, linha 0 no norte.

    A chuva do arquivo é acumulada desde o início da rodada (convectiva `rainc` + da
    microfísica `rainnc`); a da última hora é a diferença para o horário anterior. No
    primeiro horário não há anterior: tudo NaN.

    Parâmetros:
        caminho: o arquivo de um domínio.
        campo: o que ler.
        horario: um dos horários do arquivo (UTC).
        nivel_hpa: o nível de pressão (só para campos `por_nivel`).

    Exceções:
        Indisponivel: horário ou nível fora do arquivo, variável ausente, arquivo ilegível.
    """
    info = cabecalho(caminho)
    if horario not in info.horarios:
        raise Indisponivel(f'Modelo: {caminho.name} não tem o horário {horario:%d/%m %H}Z')
    passo = info.horarios.index(horario)
    nivel = 0
    if campo.por_nivel:
        if nivel_hpa not in info.niveis_hpa:
            raise Indisponivel(f'Modelo: {caminho.name} não tem o nível {nivel_hpa} hPa')
        nivel = info.niveis_hpa.index(nivel_hpa)

    if campo.chave == 'turbulencia':
        if nivel_hpa > TURBULENCIA_HPA_MAX:
            raise Indisponivel(f'Turbulência (Ellrod): só de FL{vento.fl_da_pressao(TURBULENCIA_HPA_MAX * 100):03d} '
                               f'({TURBULENCIA_HPA_MAX} hPa) para cima')
        # Três níveis (o de baixo, o pedido e o de cima) para o cisalhamento vertical
        indices = sorted({max(nivel - 1, 0), nivel, min(nivel + 1, len(info.niveis_hpa) - 1)})
        u, v, z = (np.stack([_ler(caminho, variavel, passo, indice) for indice in indices])
                   for variavel in ('u', 'v', 'geopt'))
        grade = info.grade
        ti1 = ellrod_ti1(u, v, z / GRAVIDADE, grade.passo_lon, grade.passo_y, grade.centros_lat())
        return ti1[indices.index(nivel)]
    if campo.chave == 'gelo':
        return agua_super_resfriada(_ler(caminho, 'tc', passo, nivel), _ler(caminho, 'qcloud', passo, nivel))
    if campo.chave == 'chuva_1h':
        if passo == 0:
            return np.full((info.grade.ny, info.grade.nx), np.nan, dtype=np.float32)
        agora = _ler(caminho, 'rainc', passo, 0) + _ler(caminho, 'rainnc', passo, 0)
        antes = _ler(caminho, 'rainc', passo - 1, 0) + _ler(caminho, 'rainnc', passo - 1, 0)
        return np.maximum(agora - antes, 0)   # arredondamentos podem dar -0,0001
    return _ler(caminho, campo.variavel, passo, nivel) * np.float32(campo.fator)


def compor(grosso: np.ndarray, grade_grossa: Grade, fino: np.ndarray, grade_fina: Grade
           ) -> tuple[np.ndarray, Grade]:
    """Encaixa o domínio interno (fino) no externo (grosso), na resolução do fino.

    O resultado cobre a área do grosso; onde o fino existe, vale o fino (inclusive os NaN dele:
    um eco que só o d1 tem não aparece dentro do d2). Vizinho mais próximo, sem interpolar.

    Parâmetros:
        grosso, grade_grossa: o campo do domínio externo e a sua grade.
        fino, grade_fina: o do domínio interno.

    Retorna:
        (campo composto, a grade dele).
    """
    # Número inteiro de células do fino, com o passo ajustado (diferença desprezível) para a
    # grade cobrir exatamente a área do grosso: as bordas da imagem são sempre as do d1.
    largura = grade_grossa.leste - grade_grossa.oeste
    altura = grade_grossa.norte_y - grade_grossa.sul_y
    nx = round(largura / grade_fina.passo_lon)
    ny = round(altura / grade_fina.passo_y)
    grade = Grade(grade_grossa.oeste, grade_grossa.norte_y, largura / nx, altura / ny, nx, ny)
    centros_lon = grade.centros_lon()
    centros_y = grade.centros_y()

    colunas = np.clip(np.floor((centros_lon - grade_grossa.oeste) / grade_grossa.passo_lon).astype(int),
                      0, grade_grossa.nx - 1)
    linhas = np.clip(np.floor((grade_grossa.norte_y - centros_y) / grade_grossa.passo_y).astype(int),
                     0, grade_grossa.ny - 1)
    resultado = grosso[np.ix_(linhas, colunas)]

    colunas_fino = np.floor((centros_lon - grade_fina.oeste) / grade_fina.passo_lon).astype(int)
    linhas_fino = np.floor((grade_fina.norte_y - centros_y) / grade_fina.passo_y).astype(int)
    dentro_col = (colunas_fino >= 0) & (colunas_fino < grade_fina.nx)
    dentro_lin = (linhas_fino >= 0) & (linhas_fino < grade_fina.ny)
    if dentro_col.any() and dentro_lin.any():
        resultado[np.ix_(dentro_lin, dentro_col)] = fino[np.ix_(linhas_fino[dentro_lin], colunas_fino[dentro_col])]
    return resultado, grade


def campo_da_rodada(rodada: Rodada, campo: Campo, horario: datetime, nivel_hpa: int | None = None
                    ) -> tuple[np.ndarray, Grade]:
    """O campo de todos os domínios da rodada num só, na resolução do domínio mais fino.

    Um domínio interno sem o horário pedido (previsão mais curta) é deixado de fora, e um
    campo `so_externo` usa só o d1 (com as mesmas bordas: a imagem só tem menos pixels).

    Exceções:
        Indisponivel: o domínio externo não tem o campo.
    """
    externo = rodada.arquivos[0]
    valores = ler_campo(externo, campo, horario, nivel_hpa)
    grade = cabecalho(externo).grade
    for interno in ([] if campo.so_externo else rodada.arquivos[1:]):
        try:
            valores_internos = ler_campo(interno, campo, horario, nivel_hpa)
        except Indisponivel as erro:
            log.info('%s', erro)
            continue
        valores, grade = compor(valores, grade, valores_internos, cabecalho(interno).grade)
    return valores, grade


def valor_no_ponto(valores: np.ndarray, grade: Grade, lat: float, lon: float) -> float | None:
    """O valor da célula que contém o ponto; None fora da grade ou sem valor."""
    indice = grade.indice(lat, lon)
    if indice is None:
        return None
    valor = float(valores[indice])
    return valor if math.isfinite(valor) else None


# ---------------------------------------------------------------------------
# Imagem
# ---------------------------------------------------------------------------

def indices_de_cor(valores: np.ndarray, paleta: tuple[tuple[float, str], ...]) -> np.ndarray:
    """O índice da cor de cada célula: 0 = transparente (abaixo da paleta ou NaN), 1.. = a faixa.

    Parâmetros:
        valores: o campo.
        paleta: (limite inferior, cor), em ordem crescente.
    """
    limites = np.array([limite for limite, _ in paleta], dtype=float)
    indices = np.digitize(np.nan_to_num(valores, nan=-np.inf), limites)
    return indices.astype(np.uint8)


def png(indices: np.ndarray, paleta: tuple[tuple[float, str], ...]) -> bytes:
    """PNG com paleta (um byte por pixel), com o índice 0 transparente.

    Parâmetros:
        indices: de indices_de_cor.
        paleta: a mesma usada para os índices.
    """
    altura, largura = indices.shape
    imagem = Image.frombytes('P', (largura, altura), np.ascontiguousarray(indices).tobytes())
    cores = [0, 0, 0]
    for _, cor in paleta:
        cores += [int(cor[1:3], 16), int(cor[3:5], 16), int(cor[5:7], 16)]
    imagem.putpalette(cores)
    saida = io.BytesIO()
    imagem.save(saida, 'PNG', transparency=0)
    return saida.getvalue()


_IMAGENS: dict[tuple, bytes] = {}


def imagem(rodada: Rodada, campo: Campo, horario: datetime, nivel_hpa: int | None = None) -> bytes:
    """O PNG do campo (todos os domínios compostos), guardado em cache.

    A chave do cache inclui a data de modificação dos arquivos: uma rodada regravada é relida.

    Exceções:
        Indisponivel: ver campo_da_rodada.
    """
    try:
        versao = tuple(caminho.stat().st_mtime_ns for caminho in rodada.arquivos)
    except OSError as erro:
        raise Indisponivel(f'Modelo: arquivos da rodada {rodada.nome} sumiram') from erro
    chave = (rodada.nome, versao, campo.chave, horario, nivel_hpa if campo.por_nivel else None)
    if chave not in _IMAGENS:
        valores, _ = campo_da_rodada(rodada, campo, horario, nivel_hpa)
        cache.guardar(_IMAGENS, chave, png(indices_de_cor(valores, campo.paleta), campo.paleta),
                      config.MODELO_CACHE_IMAGENS)
    return _IMAGENS[chave]


def limites_da_rodada(rodada: Rodada) -> list[list[float]]:
    """[[sul, oeste], [norte, leste]] da imagem composta (a área do domínio externo)."""
    return cabecalho(rodada.arquivos[0]).grade.limites()


def horarios_da_rodada(rodada: Rodada) -> tuple[datetime, ...]:
    """Os horários previstos da rodada (os do domínio externo)."""
    return cabecalho(rodada.arquivos[0]).horarios


# ---------------------------------------------------------------------------
# Vento em partículas (formato do leaflet-velocity)
# ---------------------------------------------------------------------------

def para_grade_regular(valores: np.ndarray, grade: Grade, passo: float) -> tuple[np.ndarray, float, float]:
    """Reamostra um campo da grade Mercator para uma grade regular em lat/lon (vizinho mais próximo).

    O leaflet-velocity só aceita grade com o mesmo passo em latitude em toda a área; a do
    modelo é Mercator (o passo em latitude diminui para o sul).

    Parâmetros:
        valores: o campo, linha 0 no norte.
        grade: a grade dele.
        passo: o passo da grade nova, em graus (o mesmo em lat e lon).

    Retorna:
        (campo novo, linha 0 no norte; latitude do centro da primeira linha; longitude do
        centro da primeira coluna).
    """
    norte = geo.lat_de_mercator(grade.norte_y)
    sul = geo.lat_de_mercator(grade.sul_y)
    linhas = max(1, math.floor((norte - sul) / passo))
    colunas = max(1, math.floor((grade.leste - grade.oeste) / passo))
    lat_inicial = norte - passo / 2
    lon_inicial = grade.oeste + passo / 2
    lats = lat_inicial - np.arange(linhas) * passo
    lons = lon_inicial + np.arange(colunas) * passo
    ys = np.array([geo.mercator_y(float(lat)) for lat in lats])
    origem_linhas = np.clip(np.floor((grade.norte_y - ys) / grade.passo_y).astype(int), 0, grade.ny - 1)
    origem_colunas = np.clip(np.floor((lons - grade.oeste) / grade.passo_lon).astype(int), 0, grade.nx - 1)
    return valores[np.ix_(origem_linhas, origem_colunas)], lat_inicial, lon_inicial


def _registro_velocity(valores: np.ndarray, numero: int, lat_inicial: float, lon_inicial: float,
                       passo: float, inicio: datetime, horas: int) -> dict:
    """Um registro (U ou V) no formato do leaflet-velocity; NaN vira null (sem dado)."""
    linhas, colunas = valores.shape
    dados = [None if not math.isfinite(valor) else round(valor, 2) for valor in valores.ravel().tolist()]
    return {'header': {'parameterCategory': 2, 'parameterNumber': numero,   # 2 = U, 3 = V
                       'lo1': lon_inicial, 'la1': lat_inicial, 'dx': passo, 'dy': passo,
                       'nx': colunas, 'ny': linhas,
                       'refTime': inicio.strftime('%Y-%m-%dT%H:%M:%SZ'), 'forecastTime': horas},
            'data': dados}


_VENTOS: dict[tuple, list[dict]] = {}


def vento_velocity(rodada: Rodada, horario: datetime, nivel_hpa: int) -> list[dict]:
    """O vento do nível, [U, V] em m/s, no formato do leaflet-velocity, guardado em cache.

    Só o domínio externo (d1), reamostrado em config.MODELO_VENTO_PASSO: as partículas são uma
    visão geral, e a grade de 0,05° do d2 daria vários MB por horário. O valor exato num
    ponto está na sonda (`perfil`, pelo d2).

    Exceções:
        Indisponivel: nível ou horário fora do arquivo, arquivo ilegível.
    """
    externo = rodada.arquivos[0]
    try:
        versao = externo.stat().st_mtime_ns
    except OSError as erro:
        raise Indisponivel(f'Modelo: arquivo {externo.name} sumiu') from erro
    chave = (rodada.nome, versao, horario, nivel_hpa)
    if chave not in _VENTOS:
        info = cabecalho(externo)
        if horario not in info.horarios:
            raise Indisponivel(f'Modelo: {externo.name} não tem o horário {horario:%d/%m %H}Z')
        if nivel_hpa not in info.niveis_hpa:
            raise Indisponivel(f'Modelo: {externo.name} não tem o nível {nivel_hpa} hPa')
        passo_tempo = info.horarios.index(horario)
        nivel = info.niveis_hpa.index(nivel_hpa)
        passo = max(config.MODELO_VENTO_PASSO, info.grade.passo_lon)
        horas = round((horario - rodada.inicio).total_seconds() / 3600)
        registros = []
        for variavel, numero in (('u', 2), ('v', 3)):
            regular, lat_inicial, lon_inicial = para_grade_regular(
                _ler(externo, variavel, passo_tempo, nivel), info.grade, passo)
            registros.append(_registro_velocity(regular, numero, lat_inicial, lon_inicial, passo,
                                                rodada.inicio, horas))
        cache.guardar(_VENTOS, chave, registros, config.MODELO_CACHE_VENTOS)
    return _VENTOS[chave]


# ---------------------------------------------------------------------------
# Perfil vertical num ponto (para a sonda)
# ---------------------------------------------------------------------------

PERFIL_TOPO_HPA = 100           # níveis acima deste ficam de fora (50 hPa vem vazio do modelo)
_PERFIL_3D = ('tc', 'td', 'rh', 'u', 'v', 'qcloud')
_PERFIL_SUPERFICIE = ('mcape', 'max_dbz')


@dataclass(frozen=True)
class NivelPerfil:
    """Um nível do perfil vertical previsto.

    Atributos:
        hpa: nível de pressão.
        fl: nível de voo (atmosfera padrão; os do painel de vento nos níveis dele).
        temperatura, orvalho: °C.
        umidade: umidade relativa, %.
        direcao: de onde vem o vento, graus (1 a 360).
        velocidade_kt: velocidade do vento, kt.
        turbulencia: TI1 de Ellrod no d1, 10⁻⁷ s⁻² (None sem valor).
        gelo: água super-resfriada, g/kg (None fora de 0 a −20 °C ou sem valor).
    """
    hpa: int
    fl: int
    temperatura: float
    orvalho: float | None
    umidade: float | None
    direcao: int | None
    velocidade_kt: int | None
    turbulencia: float | None = None
    gelo: float | None = None


@dataclass(frozen=True)
class Perfil:
    """O que o modelo prevê num ponto, num horário.

    Atributos:
        rodada: a rodada usada.
        horario: o horário previsto (UTC).
        arquivo: o domínio usado (o mais fino que cobre o ponto).
        resolucao_km: largura de uma célula da grade nesse ponto, km.
        niveis: os níveis acima do terreno, de baixo para cima, até PERFIL_TOPO_HPA.
        cape: CAPE máximo, J/kg (None sem valor).
        refletividade: refletividade máxima simulada, dBZ.
        chuva_1h: chuva na última hora, mm (None no primeiro horário da rodada).
    """
    rodada: Rodada
    horario: datetime
    arquivo: Path
    resolucao_km: float
    niveis: tuple[NivelPerfil, ...]
    cape: float | None
    refletividade: float | None
    chuva_1h: float | None


def _numero(valor) -> float | None:
    """Um valor lido do netCDF como float; None se vier sem valor (máscara ou NaN)."""
    if np.ma.is_masked(valor):
        return None
    numero = float(valor)
    return numero if math.isfinite(numero) else None


def _ler_ponto(caminho: Path, passo: int, linha_arquivo: int, coluna: int) -> dict[str, np.ndarray]:
    """Lê só a coluna do ponto (todas as alturas) das variáveis do perfil, abrindo o arquivo uma vez.

    Parâmetros:
        caminho: o arquivo.
        passo: índice do horário.
        linha_arquivo: índice da latitude no arquivo (do sul para o norte).
        coluna: índice da longitude.

    Retorna:
        variável -> valores (3D: um por nível; superfície: um só); 'chuva_antes' e 'chuva_agora'
        com rainc + rainnc no horário anterior (se houver) e no pedido.
    """
    valores = {}
    try:
        with _LEITURA, netCDF4.Dataset(caminho) as arquivo:
            for nome in _PERFIL_3D:
                valores[nome] = np.ma.filled(np.ma.asarray(arquivo[nome][passo, :, linha_arquivo, coluna],
                                                           dtype=float), np.nan)
            for nome in _PERFIL_SUPERFICIE:
                valores[nome] = arquivo[nome][passo, 0, linha_arquivo, coluna]
            for chave, indice in (('chuva_agora', passo), ('chuva_antes', passo - 1)):
                if indice >= 0:
                    valores[chave] = (_numero(arquivo['rainc'][indice, 0, linha_arquivo, coluna]),
                                      _numero(arquivo['rainnc'][indice, 0, linha_arquivo, coluna]))
    except (OSError, IndexError) as erro:
        raise Indisponivel(f'Modelo: não foi possível ler o perfil de {caminho.name} ({erro})') from erro
    return valores


def _chuva_da_hora(valores: dict) -> float | None:
    """Chuva na última hora a partir dos acumulados lidos por _ler_ponto (None sem horário anterior)."""
    antes, agora = valores.get('chuva_antes'), valores.get('chuva_agora')
    if antes is None or agora is None or None in antes or None in agora:
        return None
    return max(sum(agora) - sum(antes), 0.0)


def _ti1_no_ponto(rodada: Rodada, horario: datetime, lat: float, lon: float) -> dict[int, float]:
    """TI1 de Ellrod no ponto, nos níveis até TURBULENCIA_HPA_MAX, pelo d1 (como no mapa).

    Lê só uma janela de 3×3 células em volta do ponto (a deformação precisa dos vizinhos), em
    todos os níveis, de u, v e geopotencial.

    Retorna:
        nível (hPa) -> TI1 em 10⁻⁷ s⁻²; vazio se o ponto está fora do d1 ou falta o horário.
    """
    externo = rodada.arquivos[0]
    info = cabecalho(externo)
    indice = info.grade.indice(lat, lon)
    if indice is None or horario not in info.horarios:
        return {}
    linha, coluna = indice                       # linha 0 no norte
    grade = info.grade
    linha_arquivo = grade.ny - 1 - linha         # no arquivo, linha 0 no sul
    inicio_l, fim_l = max(linha_arquivo - 1, 0), min(linha_arquivo + 2, grade.ny)
    inicio_c, fim_c = max(coluna - 1, 0), min(coluna + 2, grade.nx)
    passo = info.horarios.index(horario)
    try:
        with _LEITURA, netCDF4.Dataset(externo) as arquivo:
            u, v, geopotencial = (
                np.ma.filled(np.ma.asarray(arquivo[nome][passo, :, inicio_l:fim_l, inicio_c:fim_c], dtype=float),
                             np.nan)[:, ::-1, :]                               # linha 0 no norte
                for nome in ('u', 'v', 'geopt'))
    except (OSError, IndexError) as erro:
        raise Indisponivel(f'Modelo: não foi possível ler a turbulência de {externo.name} ({erro})') from erro
    # Linhas da janela (norte para o sul) na grade, e o ponto dentro da janela
    linhas_grade = [grade.ny - 1 - linha_arq for linha_arq in range(fim_l - 1, inicio_l - 1, -1)]
    lats = grade.centros_lat()[linhas_grade]
    ti1 = ellrod_ti1(u, v, geopotencial / GRAVIDADE, grade.passo_lon, grade.passo_y, lats)
    no_ponto = ti1[:, linhas_grade.index(linha), coluna - inicio_c]
    return {hpa: float(valor) for hpa, valor in zip(info.niveis_hpa, no_ponto, strict=True)
            if math.isfinite(valor) and hpa <= TURBULENCIA_HPA_MAX}


def _niveis_do_perfil(niveis_hpa: tuple[int, ...], valores: dict[str, np.ndarray],
                      ti1: dict[int, float] | None = None) -> tuple[NivelPerfil, ...]:
    """Os níveis com temperatura (acima do terreno), de baixo para cima, até PERFIL_TOPO_HPA.

    Parâmetros:
        niveis_hpa: os níveis do arquivo, na ordem dos valores.
        valores: de _ler_ponto.
        ti1: de _ti1_no_ponto (nível -> TI1); None ou vazio = sem turbulência.
    """
    ti1 = ti1 or {}
    resultado = []
    for indice, hpa in sorted(enumerate(niveis_hpa), key=lambda par: -par[1]):
        temperatura = _numero(valores['tc'][indice])
        if hpa < PERFIL_TOPO_HPA or temperatura is None:
            continue
        vento_u, vento_v = _numero(valores['u'][indice]), _numero(valores['v'][indice])
        direcao, velocidade = (None, None)
        if vento_u is not None and vento_v is not None:
            direcao, velocidade = vento.direcao_velocidade(vento_u, vento_v)
        agua_nuvem = _numero(valores['qcloud'][indice])
        gelo = None
        if agua_nuvem is not None and GELO_T_MIN <= temperatura <= GELO_T_MAX:
            gelo = agua_nuvem * 1000
        resultado.append(NivelPerfil(hpa, vento.fl_da_pressao(hpa * 100), temperatura,
                                     _numero(valores['td'][indice]), _numero(valores['rh'][indice]),
                                     direcao, velocidade, ti1.get(hpa), gelo))
    return tuple(resultado)


def perfil(rodada: Rodada, horario: datetime, lat: float, lon: float) -> Perfil | None:
    """O perfil vertical previsto no ponto, pelo domínio mais fino que o cobre.

    Lê só a coluna do ponto (não os campos inteiros): ~10 leituras pequenas, numa abertura do arquivo.

    Parâmetros:
        rodada: a rodada.
        horario: o horário previsto (um dos da rodada).
        lat, lon: o ponto, em graus.

    Retorna:
        O perfil; None se o ponto está fora da área do modelo.

    Exceções:
        Indisponivel: arquivo ilegível, horário fora da rodada.
    """
    for caminho in reversed(rodada.arquivos):        # do domínio mais fino para o maior
        info = cabecalho(caminho)
        indice = info.grade.indice(lat, lon)
        if indice is None or horario not in info.horarios:
            continue
        linha, coluna = indice
        passo = info.horarios.index(horario)
        valores = _ler_ponto(caminho, passo, info.grade.ny - 1 - linha, coluna)
        resolucao_km = info.grade.passo_lon * geo.KM_POR_GRAU * math.cos(math.radians(lat))
        niveis = _niveis_do_perfil(info.niveis_hpa, valores, _ti1_no_ponto(rodada, horario, lat, lon))
        return Perfil(rodada, horario, caminho, resolucao_km, niveis,
                      _numero(valores['mcape']), _numero(valores['max_dbz']), _chuva_da_hora(valores))
    if horario not in horarios_da_rodada(rodada):
        raise Indisponivel(f'Modelo: a rodada {rodada.nome} não tem o horário {horario:%d/%m %H}Z')
    return None


def isoterma_zero(niveis: tuple[NivelPerfil, ...]) -> float | None:
    """A pressão (hPa) onde a temperatura cruza 0 °C pela primeira vez, subindo a partir do chão.

    Interpolada em ln(p) entre os dois níveis do cruzamento (a temperatura varia quase
    linearmente com a altura, e a altura, com ln(p)).

    Parâmetros:
        niveis: de baixo para cima (Perfil.niveis).

    Retorna:
        A pressão; None se não há cruzamento (tudo acima ou tudo abaixo de 0 °C).
    """
    for embaixo, em_cima in itertools.pairwise(niveis):
        if embaixo.temperatura > 0 >= em_cima.temperatura:
            fracao = embaixo.temperatura / (embaixo.temperatura - em_cima.temperatura)
            ln_p = math.log(embaixo.hpa) + fracao * (math.log(em_cima.hpa) - math.log(embaixo.hpa))
            return math.exp(ln_p)
    return None


def texto_isoterma(niveis: tuple[NivelPerfil, ...]) -> str:
    """A isoterma de 0 °C para a sonda: '~FL145 (590 hPa)', ou por que não há uma."""
    if not niveis:
        return 'sem níveis'
    hpa = isoterma_zero(niveis)
    if hpa is not None:
        return f'~FL{vento.fl_da_pressao(round(hpa * 100)):03d} ({hpa:.0f} hPa)'
    if niveis[0].temperatura <= 0:
        return f'abaixo do primeiro nível ({niveis[0].hpa} hPa já com {niveis[0].temperatura:.0f} °C)'
    return 'acima de todos os níveis'


# ---------------------------------------------------------------------------
# Para a tela (textos e escolhas; sem NiceGUI)
# ---------------------------------------------------------------------------

def nivel_do_fl(fl: int) -> int:
    """O nível de pressão (hPa) de um nível de voo do painel (os mesmos do vento: FL340 = 250 hPa)."""
    return config.VENTO_FL_PARA_PA[fl] // 100


def horario_mais_proximo(horarios: tuple[datetime, ...] | list[datetime], agora: datetime) -> datetime | None:
    """O horário previsto mais perto de `agora` (o primeiro ao abrir a camada); None sem horários."""
    if not horarios:
        return None
    return min(horarios, key=lambda horario: abs((horario - agora).total_seconds()))


def rotulo_horario(horario: datetime, inicio: datetime) -> str:
    """Um horário previsto como texto: '24/11 18Z (+42 h)', a antecedência contada do início da rodada."""
    horas = round((horario - inicio).total_seconds() / 3600)
    return f'{horario:%d/%m %H}Z (+{horas} h)'


def url_imagem(rodada: Rodada, campo: Campo, horario: datetime, fl: int) -> str:
    """O endereço da imagem na rota /modelo/imagem (o nível só nos campos por nível)."""
    url = f'/modelo/imagem?r={rodada.nome}&c={campo.chave}&h={horario:%Y%m%d%H}'
    if campo.por_nivel:
        url += f'&n={nivel_do_fl(fl)}'
    return url


def buscar_imagem(nome_rodada: str, chave: str, horario: str, nivel_hpa: int = 0,
                  pasta: Path | None = None) -> tuple[int, bytes, str]:
    """Responde ao pedido de imagem do navegador (rota /modelo/imagem).

    Parâmetros:
        nome_rodada: AAAAMMDDHH da rodada.
        chave: chave do campo (POR_CHAVE).
        horario: horário previsto, AAAAMMDDHH (UTC).
        nivel_hpa: nível de pressão, para os campos por nível.
        pasta: a pasta dos arquivos (None = config.MODELO_DIR).

    Retorna:
        (status HTTP, conteúdo, tipo): 200 com o PNG; 400 pedido malformado; 404 rodada, campo,
        horário ou nível inexistente.
    """
    campo = POR_CHAVE.get(chave)
    if campo is None:
        return 404, b'campo desconhecido', 'text/plain'
    try:
        instante = datetime.strptime(horario, _FORMATO_HORARIO).replace(tzinfo=UTC)
    except ValueError:
        return 400, b'horario deve ser AAAAMMDDHH', 'text/plain'
    rodada = rodada_por_nome(nome_rodada, pasta)
    if rodada is None:
        return 404, b'rodada desconhecida', 'text/plain'
    try:
        return 200, imagem(rodada, campo, instante, nivel_hpa), 'image/png'
    except Indisponivel as erro:
        return 404, str(erro).encode(), 'text/plain'
