import io
from datetime import UTC, datetime

import netCDF4
import numpy as np
import pytest
from PIL import Image

import config
import geo
import modelo

HORA_INICIAL = 17758656            # 23/11/2026 00Z em "hours since 1-1-1", calendário misto (o do arquivo real)
INICIO = datetime(2026, 11, 23, tzinfo=UTC)
NIVEIS = [1000, 850, 500, 250]


def _lats_mercator(sul, norte, n):
    """Centros de n linhas igualmente espaçadas em y de Mercator."""
    ys = np.linspace(geo.mercator_y(sul), geo.mercator_y(norte), n)
    return np.array([geo.lat_de_mercator(y) for y in ys])


def _escrever(caminho, lats, lons, horas=3, preencher=None):
    """Um netCDF com a estrutura dos arquivos do WRF (como o CDO os grava).

    `preencher(nome, passo, nivel, lats, lons)` dá o valor de cada campo; padrão: o passo.
    """
    preencher = preencher or (lambda nome, passo, nivel, la, lo: np.full((len(la), len(lo)), float(passo)))
    with netCDF4.Dataset(caminho, 'w', format='NETCDF3_64BIT_OFFSET') as arquivo:
        arquivo.createDimension('time', None)
        arquivo.createDimension('lon', len(lons))
        arquivo.createDimension('lat', len(lats))
        arquivo.createDimension('lev', len(NIVEIS))
        arquivo.createDimension('lev_2', 1)
        tempo = arquivo.createVariable('time', 'f8', ('time',))
        tempo.units = 'hours since 1-1-1 00:00:00'
        tempo.calendar = 'standard'
        tempo[:] = HORA_INICIAL + np.arange(horas)
        arquivo.createVariable('lon', 'f8', ('lon',))[:] = lons
        arquivo.createVariable('lat', 'f8', ('lat',))[:] = lats
        arquivo.createVariable('lev', 'f8', ('lev',))[:] = NIVEIS
        for nome, nivel_dim in [('max_dbz', 'lev_2'), ('rainc', 'lev_2'), ('rainnc', 'lev_2'),
                                ('wspd', 'lev'), ('tc', 'lev')]:
            var = arquivo.createVariable(nome, 'f4', ('time', nivel_dim, 'lat', 'lon'), fill_value=1e30)
            for passo in range(horas):
                for nivel in range(len(NIVEIS) if nivel_dim == 'lev' else 1):
                    var[passo, nivel] = preencher(nome, passo, nivel, lats, lons)


@pytest.fixture
def rodada(tmp_path):
    """d1 de 30°S–20°S, 50°W–40°W (longitudes 0..360, como no arquivo real) e d2 no meio dele."""
    def preencher(nome, passo, nivel, la, lo):
        if nome == 'max_dbz':
            return np.full((len(la), len(lo)), 10.0 + passo)           # d1: 10, 11, 12 dBZ
        if nome in ('rainc', 'rainnc'):
            return np.full((len(la), len(lo)), 2.0 * passo)            # acumula 4 mm/h no total
        if nome == 'tc':
            valores = np.full((len(la), len(lo)), -20.0)
            if nivel == 0:
                valores[0, :] = 1e30                                    # abaixo do terreno
            return valores
        return np.full((len(la), len(lo)), 10.0)                        # wspd 10 m/s
    _escrever(tmp_path / 'wrfd1_2026112300.nc', _lats_mercator(-30, -20, 40), np.arange(310, 320, 0.5) + 0.25,
              preencher=preencher)

    def preencher_d2(nome, passo, nivel, la, lo):
        valores = preencher(nome, passo, nivel, la, lo)
        return valores + 40 if nome == 'max_dbz' else valores           # d2: 50, 51, 52 dBZ
    _escrever(tmp_path / 'wrfd2_2026112300.nc', _lats_mercator(-26, -24, 30), np.arange(314, 316, 0.1) + 0.05,
              preencher=preencher_d2)
    (tmp_path / 'wrfd1_2026112212.nc').touch()
    (tmp_path / 'outro.nc').touch()
    return tmp_path


def test_lista_rodadas_agrupa_dominios(rodada):
    rodadas = modelo.listar_rodadas(rodada)
    assert [r.nome for r in rodadas] == ['2026112300', '2026112212']
    assert [c.name for c in rodadas[0].arquivos] == ['wrfd1_2026112300.nc', 'wrfd2_2026112300.nc']
    assert rodadas[0].inicio == INICIO
    assert modelo.rodada_por_nome('2026112300', rodada) == rodadas[0]
    assert modelo.rodada_por_nome('2026010100', rodada) is None


def test_cabecalho_datas_no_calendario_do_arquivo(rodada):
    # Com o datetime do Python (gregoriano proléptico) sairia 25/11: dois dias errado.
    info = modelo.cabecalho(rodada / 'wrfd1_2026112300.nc')
    assert info.horarios[0] == INICIO and info.horarios[2] == datetime(2026, 11, 23, 2, tzinfo=UTC)
    assert info.niveis_hpa == tuple(NIVEIS)
    sul_oeste, norte_leste = info.grade.limites()
    assert sul_oeste[1] == pytest.approx(-50) and norte_leste[1] == pytest.approx(-40)   # 0..360 → -180..180
    assert sul_oeste[0] < -30 < -20 < norte_leste[0]                                    # meia célula além


def test_recusa_grade_fora_de_mercator():
    lats = np.arange(-35, -13, 0.13)          # igualmente espaçadas em graus: não é Mercator
    with pytest.raises(modelo.Indisponivel, match='Mercator'):
        modelo.grade_das_coordenadas(lats, np.arange(300, 320, 0.16))


def test_grade_indice():
    grade = modelo.grade_das_coordenadas(_lats_mercator(-30, -20, 11), np.arange(-50, -39.5, 1.0))
    assert grade.indice(-20, -50) == (0, 0)          # canto noroeste: linha 0 é o norte
    assert grade.indice(-30, -40) == (10, 10)
    assert grade.indice(-10, -45) is None


def test_compor_encaixa_o_dominio_interno(rodada):
    rodadas = modelo.listar_rodadas(rodada)
    valores, grade = modelo.campo_da_rodada(rodadas[0], modelo.POR_CHAVE['refletividade'], INICIO)
    assert grade.passo_lon == pytest.approx(0.1)                          # resolução do d2
    assert np.allclose(grade.limites(), modelo.limites_da_rodada(rodadas[0]))        # área do d1
    assert modelo.valor_no_ponto(valores, grade, -25, -45) == 50          # dentro do d2
    assert modelo.valor_no_ponto(valores, grade, -28, -48) == 10          # só no d1
    assert modelo.valor_no_ponto(valores, grade, 0, 0) is None


def test_chuva_da_ultima_hora(rodada):
    caminho = rodada / 'wrfd1_2026112300.nc'
    chuva = modelo.POR_CHAVE['chuva_1h']
    assert np.isnan(modelo.ler_campo(caminho, chuva, INICIO)).all()       # sem horário anterior
    uma_hora = modelo.ler_campo(caminho, chuva, datetime(2026, 11, 23, 1, tzinfo=UTC))
    assert np.allclose(uma_hora, 4.0)                                     # (2 + 2) − (0 + 0)


def test_campo_por_nivel_unidade_e_sem_valor(rodada):
    caminho = rodada / 'wrfd1_2026112300.nc'
    vento = modelo.ler_campo(caminho, modelo.POR_CHAVE['vento'], INICIO, 500)
    assert np.allclose(vento, 10 * modelo.KT_POR_MS)
    temperatura = modelo.ler_campo(caminho, modelo.POR_CHAVE['temperatura'], INICIO, 1000)
    assert np.isnan(temperatura[-1]).all() and not np.isnan(temperatura[0]).any()   # 1ª linha do arquivo = sul
    with pytest.raises(modelo.Indisponivel, match='nível'):
        modelo.ler_campo(caminho, modelo.POR_CHAVE['vento'], INICIO, 700)
    with pytest.raises(modelo.Indisponivel, match='horário'):
        modelo.ler_campo(caminho, modelo.POR_CHAVE['vento'], datetime(2026, 11, 24, tzinfo=UTC), 500)
    with pytest.raises(modelo.Indisponivel, match='variável'):
        modelo.ler_campo(caminho, modelo.POR_CHAVE['cape'], INICIO)


def test_indices_de_cor():
    paleta = ((20, '#000001'), (40, '#000002'))
    valores = np.array([[np.nan, 5, 20, 39.9, 40, 99]])
    assert modelo.indices_de_cor(valores, paleta).tolist() == [[0, 0, 1, 1, 2, 2]]


def test_rota_devolve_png(rodada):
    status, conteudo, tipo = modelo.buscar_imagem('2026112300', 'refletividade', '2026112301', pasta=rodada)
    assert (status, tipo) == (200, 'image/png')
    imagem = Image.open(io.BytesIO(conteudo))
    assert imagem.mode == 'P' and imagem.info['transparency'] == 0
    assert imagem.width == 100                                            # 10° de d1 no passo de 0,1° do d2
    assert set(np.unique(np.asarray(imagem))) == {0, 7}                   # d1 (11 dBZ) transparente; d2 51 dBZ

    assert modelo.buscar_imagem('2026112300', 'nada', '2026112301', pasta=rodada)[0] == 404
    assert modelo.buscar_imagem('2026112300', 'refletividade', '23/11', pasta=rodada)[0] == 400
    assert modelo.buscar_imagem('2020010100', 'refletividade', '2026112301', pasta=rodada)[0] == 404
    assert modelo.buscar_imagem('2026112300', 'refletividade', '2026113000', pasta=rodada)[0] == 404


def test_arquivos_reais_estao_em_mercator():
    rodadas = modelo.listar_rodadas(config.MODELO_DIR)
    if not rodadas:
        pytest.skip('pasta do modelo ausente nesta máquina')
    for caminho in rodadas[0].arquivos:
        info = modelo.cabecalho(caminho)
        assert info.horarios[0] == rodadas[0].inicio, caminho.name
