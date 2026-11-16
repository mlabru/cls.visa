import asyncio

import numpy as np

import geo
import impacto
import redemet
import sonda


def _raios(c10=0, c20=0, c50=0, novas=0, perto=None):
    return sonda.RaiosPerto({10: c10, 20: c20, 50: c50}, novas, perto)


def test_niveis_do_aerodromo():
    nivel, motivos, _ = impacto.classificar_aerodromo(30, _raios(1, 2, 3, 1, (4.2, 6)), 'g')
    assert nivel == impacto.ALTO
    assert motivos == ['raios a 4 km (célula ativa há 6 min)', '1 célula(s) nova(s) até 20 km', '30 dBZ a até 10 km']
    assert impacto.classificar_aerodromo(46, _raios(), 'g')[0] == impacto.ALTO
    assert impacto.classificar_aerodromo(None, _raios(), 'r')[0] == impacto.ALTO            # METAR restrição
    assert impacto.classificar_aerodromo(None, _raios(0, 1, 1, 0, (15, 3)), 'g')[0] == impacto.MEDIO
    assert impacto.classificar_aerodromo(36, _raios(), 'g')[0] == impacto.MEDIO
    nivel, motivos, _ = impacto.classificar_aerodromo(None, _raios(), 'gw')
    assert nivel == impacto.MEDIO and motivos == ['aviso de aeródromo em vigor']
    assert impacto.classificar_aerodromo(None, _raios(0, 0, 2, 0, (40, 30)), 'g')[0] == impacto.BAIXO
    assert impacto.classificar_aerodromo(22, _raios(), 'g')[0] == impacto.BAIXO
    assert impacto.classificar_aerodromo(None, _raios(), 'g')[0] is None
    assert impacto.classificar_aerodromo(None, _raios(), 'cinza')[0] is None                # sem METAR


def test_niveis_da_tma_e_ordem():
    assert impacto.classificar_tma(30, 5, 1)[0] == impacto.ALTO
    assert impacto.classificar_tma(None, 1, 0)[1] == ['1 célula(s) com raios']
    assert impacto.classificar_tma(38, 0, 0)[0] == impacto.MEDIO
    assert impacto.classificar_tma(25, 0, 0)[0] == impacto.BAIXO
    assert impacto.classificar_tma(None, 0, 0)[0] is None
    a = impacto.Impacto('aerodromo', 'SBAA', '', 0, 0, impacto.MEDIO, (), 50)
    b = impacto.Impacto('aerodromo', 'SBBB', '', 0, 0, impacto.ALTO, (), 10)
    c = impacto.Impacto('aerodromo', 'SBCC', '', 0, 0, impacto.ALTO, (), 30)
    assert [i.ident for i in impacto.ordenar([a, b, c])] == ['SBCC', 'SBBB', 'SBAA']


def test_dbz_robusto_ignora_pixel_isolado():
    assert impacto.dbz_robusto(np.array([66.0, 20.0])) is None             # poucos pixels
    assert impacto.dbz_robusto(np.array([66.0, 48.0, 45.0, 20.0])) == 45   # o 3º maior
    assert impacto.eco_fixo([np.ones((2, 2), bool)] * 5) is None           # poucos quadros
    mascaras = [np.array([[True, True], [False, False]])] * 8 + [np.array([[True, False], [False, False]])] * 2
    assert impacto.eco_fixo(mascaras).tolist() == [[True, False], [False, False]]


def test_agrupar_tmas():
    anel = [[-47, -24], [-46, -24], [-46, -23], [-47, -23], [-47, -24]]
    poligono = {'type': 'Polygon', 'coordinates': [anel]}
    tmas = impacto.agrupar_tmas([
        {'geometry': poligono, 'properties': {'ident': 'SBXP_01', 'nam': 'São Paulo 1'}},
        {'geometry': poligono, 'properties': {'ident': 'SBXP', 'nam': 'São Paulo'}},
        {'geometry': None, 'properties': {'ident': 'X'}},
    ])
    assert [(s, n, len(a)) for s, n, a in tmas] == [('SBXP', 'São Paulo', 2)]


def _campo():
    """Imagem de radar sintética: 41x41 px cobrindo 1°, com um núcleo de 50 dBZ no centro."""
    imagem = redemet.Imagem('radar/x.png', -24, -23, -47, -46, None, 'xx')
    campo = np.full((41, 41), np.nan)
    campo[19:22, 19:22] = 50.0          # 9 pixels no centro (-23,5; -46,5)
    campo[0, 0] = 70.0                  # pixel isolado no canto
    return imagem, campo


def test_dbz_perto_e_tma():
    campos = [_campo()]
    assert impacto.dbz_max_perto(campos, -23.5, -46.5, 10) == 50
    assert impacto.dbz_max_perto(campos, -23.02, -46.98, 5) is None        # só o pixel isolado
    anel = [[-46.6, -23.6], [-46.4, -23.6], [-46.4, -23.4], [-46.6, -23.4], [-46.6, -23.6]]
    celulas = ((-23.55, -46.55, 3), (-23.45, -46.45, 40), (-20.0, -40.0, 0))
    item = impacto.avaliar_tma('SBXP', 'São Paulo', [anel], campos, celulas)
    assert item.nivel == impacto.ALTO and item.motivos == ('2 célula(s) com raios (1 nova(s))', 'máximo de 50 dBZ')


def test_mapa_de_perigo():
    perigo = impacto.mapa_de_perigo([_campo()], ((-20.05, -40.05, 3),))
    assert perigo[geo.chave_celula(-20.05, -40.05)] == 'raios na célula'
    assert perigo[geo.chave_celula(-20.15, -40.05)] == 'raios a menos de 10 km'
    assert perigo[geo.chave_celula(-23.5, -46.5)] == f'eco ≥ {impacto.ECO_FORTE_DBZ} dBZ'
    assert geo.chave_celula(-20.3, -40.3) not in perigo


def test_celulas_fortes_ignoram_pixel_isolado():
    fortes = impacto.celulas_fortes([_campo()])
    assert fortes == {geo.chave_celula(-23.5, -46.5): 50}       # o pixel de 70 dBZ no canto não entra
    quadro = redemet.QuadroRaios(None, ((-20.05, -40.05, 3),))
    assert impacto.celulas_de_raios(quadro) == {geo.chave_celula(-20.05, -40.05): 3}


def test_erro_some_quando_a_api_volta_sem_quadro_novo(monkeypatch):
    # Antes, o aviso só sumia com um quadro de radar novo (até ~20 min depois de a API voltar)
    async def vazio(*_):
        return []

    monkeypatch.setattr(redemet, 'radar_quadros', vazio)
    monkeypatch.setattr(redemet, 'raios_quadros', vazio)
    monkeypatch.setattr(impacto.aerodromos.situacao, 'aerodromos', ['algum'])
    monkeypatch.setattr(impacto, 'situacao', impacto.Situacao(erro='API da REDEMET indisponível', versao=1))
    asyncio.run(impacto.atualizar())
    assert impacto.situacao.erro == '' and impacto.situacao.versao == 1     # nada recalculado


def test_fontes_validas_e_perigo_so_das_recentes():
    from datetime import UTC, datetime, timedelta

    import previsao
    agora = datetime(2026, 11, 16, 12, tzinfo=UTC)
    antes = lambda m: agora - timedelta(minutes=m)
    assert impacto.fontes_validas(antes(40), antes(10), agora) == {previsao.RADAR, previsao.RAIOS}
    assert impacto.fontes_validas(antes(50), antes(10), agora) == {previsao.RAIOS}
    assert impacto.fontes_validas(None, antes(25), agora) == frozenset()
    radar_, raios = {(1, 1): 'eco', (2, 2): 'eco'}, {(2, 2): 'raios na célula'}
    assert impacto.combinar_perigo(radar_, raios, frozenset({previsao.RADAR, previsao.RAIOS})) == \
        {(1, 1): 'eco', (2, 2): 'raios na célula'}
    assert impacto.combinar_perigo(radar_, raios, frozenset({previsao.RAIOS})) == raios
    assert impacto.combinar_perigo(radar_, raios, frozenset()) == {}


def test_raios_de_interferencia_saem_e_chuva_fica():
    """Antena no centro de uma grade de ±3,6° (~2 km por pixel): um raio de 1° de largura indo
    para leste, uma célula redonda e uma célula alongada na direção da antena."""
    lat0, lon0, n = -10.0, -50.0, 400
    lats = np.linspace(lat0 + 3.6, lat0 - 3.6, n)
    lons = np.linspace(lon0 - 3.6, lon0 + 3.6, n)
    la, lo = np.meshgrid(lats, lons, indexing='ij')
    dy = (la - lat0) * geo.KM_POR_GRAU
    dx = (lo - lon0) * geo.KM_POR_GRAU * np.cos(np.radians(lat0))
    dist, azimute = np.hypot(dx, dy), np.degrees(np.arctan2(dx, dy)) % 360
    campo = np.full((n, n), np.nan)
    raio = (np.abs(azimute - 90) < 0.6) & (dist > 40) & (dist < 380)
    redonda = np.hypot(dx - 0, dy - 150) < 20                               # 150 km ao norte
    alongada = ((dx + 250) / 40) ** 2 + (dy / 15) ** 2 < 1                  # 80 × 30 km, a 250 km a oeste
    campo[raio] = 50
    campo[redonda] = 45
    campo[alongada] = 45
    apagar = impacto.raios_de_interferencia(campo, lats, lons, lat0, lon0)
    assert apagar[raio].mean() > 0.8
    assert not apagar[redonda].any() and not apagar[alongada].any()
