import json
from datetime import datetime, timedelta, timezone

import config
import vento


def _grade_global(tmp_path):
    """Grade global 1,25° (como os arquivos reais) com valor = índice da célula."""
    nx, ny = 288, 145
    cab = {'lo1': 0.0, 'la1': 90.0, 'dx': 1.25, 'dy': 1.25, 'nx': nx, 'ny': ny,
           'refTime': '2026-02-28T12:00:00Z'}
    registros = [{'header': dict(cab, parameterNumber=n), 'data': list(range(nx * ny))} for n in (2, 3)]
    caminho = tmp_path / 'wind_20260228T120000Z_25000.json'
    caminho.write_text(json.dumps(registros))
    return caminho, nx


def test_lista_e_escolhe_pelo_nome(tmp_path):
    _grade_global(tmp_path)
    (tmp_path / 'wind_20260228T090000Z_25000.json').write_text('[]')
    (tmp_path / 'outro.json').write_text('[]')
    arquivos = vento.listar_arquivos(tmp_path)
    assert [a.horario.hour for a in arquivos] == [12, 9]
    assert vento.escolher(arquivos, 340, None).horario.hour == 12          # FL340 = 25000 Pa
    assert vento.escolher(arquivos, 50, None) is None                      # sem arquivo de 850 hPa


def test_recorte_preserva_os_valores(tmp_path):
    caminho, nx = _grade_global(tmp_path)
    recortado = vento.carregar_recortado(caminho)
    cab = recortado[0]['header']
    r = config.VENTO_RECORTE
    assert cab['la1'] == r['lat_norte'] and cab['lo1'] == r['lon_oeste'] % 360
    assert len(recortado[0]['data']) == cab['nx'] * cab['ny']

    # Ponto de teste: 15°S 47,5°W (perto de Brasília) — mesmo valor nas duas grades
    lat, lon = -15.0, -47.5
    i_glob = round((90 - lat) / 1.25) * nx + round((lon % 360) / 1.25)
    i_rec = round((cab['la1'] - lat) / 1.25) * cab['nx'] + round(((lon % 360) - cab['lo1']) / 1.25)
    assert recortado[0]['data'][i_rec] == i_glob


def test_idade_e_cores():
    ref = datetime(2026, 2, 28, 12, tzinfo=timezone.utc)
    assert vento.idade(ref, ref + timedelta(hours=3)) == ('há 3 h', 'positive')
    assert vento.idade(ref, ref + timedelta(hours=12))[1] == 'warning'
    texto, cor = vento.idade(ref, ref + timedelta(days=257))
    assert cor == 'negative' and 'DESATUALIZADO' in texto and '257 dias' in texto


def test_arquivos_reais_tem_todos_os_niveis():
    arquivos = vento.listar_arquivos()
    if not arquivos:
        return  # pasta de dados ausente nesta máquina
    for fl in config.VENTO_FL_PARA_PA:
        assert vento.horarios_disponiveis(arquivos, fl), f'FL{fl} sem arquivo'
