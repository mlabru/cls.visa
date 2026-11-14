import copy
from datetime import UTC, datetime, timedelta

import modos
import saude
from interface import PADRAO

AGORA = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)


def test_idade_curta():
    assert [saude.idade_curta(s) for s in (10, 240, 3 * 3600, 3 * 86400)] == ['agora', '4 min', '3 h', '3 d']


def test_radar_usa_a_imagem_mais_nova():
    ind = saude.radar([AGORA - timedelta(minutes=50), AGORA - timedelta(minutes=4)], True, AGORA)
    assert (ind.texto, ind.nivel) == ('4 min', saude.OK)
    assert saude.radar([AGORA - timedelta(minutes=45)], True, AGORA).nivel == saude.ATENCAO
    assert saude.radar([AGORA - timedelta(hours=2)], True, AGORA).nivel == saude.CRITICO
    assert saude.radar([], True, AGORA).nivel == saude.CRITICO
    assert saude.radar([], False, AGORA).nivel == saude.SEM_DADOS


def test_raios_e_geoserver():
    assert saude.raios(True, None).texto == 'ao vivo'
    assert saude.raios(False, None).nivel == saude.CRITICO
    assert saude.raios(None, None).nivel == saude.SEM_DADOS
    passado = saude.raios(True, datetime(2026, 11, 13, 13, 30, tzinfo=UTC))
    assert (passado.texto, passado.nivel) == ('até 13:30Z', saude.ATENCAO)


def test_severo_pela_idade_da_rodada():
    ind = saude.severo('2026-11-13T12:00:00Z', '2026-11-13T18:00:00Z', agora=AGORA)
    assert (ind.texto, ind.nivel) == ('12Z', saude.OK) and '13/11 18:00Z' in ind.detalhe
    assert saude.severo('2026-11-12T12:00:00Z', None, agora=AGORA).nivel == saude.CRITICO
    assert saude.severo(None, None).nivel == saude.SEM_DADOS
    assert saude.severo('2026-11-13T12:00:00Z', None, erro='GeoServer inacessível').texto == 'fora'


def test_trafego_aerodromos_vento():
    assert saude.trafego(True, AGORA - timedelta(seconds=5), 'conectado', AGORA).texto == 'ao vivo'
    assert saude.trafego(True, AGORA - timedelta(minutes=5), 'conectado', AGORA).nivel == saude.CRITICO
    assert saude.trafego(False, None, 'reconectando').nivel == saude.CRITICO
    assert saude.aerodromos(300).nivel == saude.OK
    assert saude.aerodromos(None).nivel == saude.SEM_DADOS
    assert saude.aerodromos(10, erro='REDEMET indisponível').nivel == saude.CRITICO
    assert saude.vento(None, 340).nivel == saude.CRITICO
    assert saude.vento(AGORA - timedelta(hours=2), 340, AGORA).texto == '13Z'


def test_airac():
    assert saude.airac({'tma': '2026-11-26'}, '2026-11-13').texto == 'até 26/11'
    assert saude.airac({'tma': '2026-11-26', 'fir': '2026-11-10'}, '2026-11-13').nivel == saude.CRITICO
    assert saude.airac({'tma': None}, '2026-11-13').nivel == saude.SEM_DADOS


def test_modos_aplicar_e_reconhecer():
    prefs = copy.deepcopy(PADRAO)
    assert modos.ativo(prefs) == 'conveccao'          # quem abre pela primeira vez cai em Convecção
    modos.aplicar(modos.POR_CHAVE['altitude'], prefs)
    assert modos.ativo(prefs) == 'altitude'
    assert prefs['vento_ligado'] and not prefs['radar_ligado']
    assert {c for c, v in prefs['wifs'].items() if v} == {'turbulencia', 'gelo_severo'}
    prefs['espaco']['fir'] = True                     # ajuste à mão: vira "personalizado"
    assert modos.ativo(prefs) is None
    modos.aplicar(modos.POR_CHAVE['terminal'], prefs)
    assert modos.ativo(prefs) == 'terminal' and not prefs['espaco']['fir']


def test_satelite():
    assert saude.satelite(AGORA - timedelta(minutes=30), agora=AGORA).nivel == saude.OK   # atraso normal
    assert saude.satelite(AGORA - timedelta(minutes=50), agora=AGORA).nivel == saude.ATENCAO
    assert saude.satelite(AGORA - timedelta(hours=2), agora=AGORA).nivel == saude.CRITICO
    assert saude.satelite(None, 'API da REDEMET indisponível').texto == 'fora'
    assert saude.radar([], True, AGORA, erro='API da REDEMET indisponível').texto == 'fora'
