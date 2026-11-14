from datetime import datetime, timedelta, timezone

import animacao
import config
import redemet

E = config.REDEMET_ESTATICO_URL
T0 = datetime(2026, 11, 13, 14, 0, tzinfo=timezone.utc)


def _sat(minuto):
    return redemet.Imagem(f'satelite/s{minuto}.png', -56, 12.5, -100, -25, T0 + timedelta(minutes=minuto))


def _radar(minuto):
    imagem = redemet.Imagem(f'radar/r{minuto}.png', -5, 2, -52, -45, T0 + timedelta(minutes=minuto), 'be')
    return redemet.QuadroRadar(imagem.quando, (imagem,))


def test_linha_do_tempo_de_10_em_10_min():
    satelites = [_sat(m) for m in range(0, 130, 10)]          # 14:00 ... 16:00
    quadros = [_radar(m) for m in range(-2, 140, 20)]         # 13:58, 14:18 ... 16:18
    passos = animacao.linha_do_tempo(satelites, quadros, 60)
    horas = [f'{p.quando:%H:%M}' for p in passos]
    assert horas == ['15:10', '15:20', '15:30', '15:40', '15:50', '16:00', '16:10', '16:18']
    p1600 = passos[5]
    assert p1600.satelite.quando.minute == 0 and p1600.radar.quando.minute == 58   # radar das 15:58
    assert passos[1].radar.quando.minute == 18     # 15:20 já usa o radar das 15:18 (tolerância)
    assert passos[-1].satelite.quando.hour == 16 and passos[-1].radar.quando.minute == 18


def test_linha_do_tempo_vazia_ou_so_satelite():
    assert animacao.linha_do_tempo([], [], 60) == []
    passos = animacao.linha_do_tempo([_sat(0), _sat(10), _sat(20)], [], 120)
    assert passos[0].quando == T0 and all(p.radar is None for p in passos)   # sem passos vazios no início


def test_rotulo():
    passo = animacao.Passo(T0, None, None)
    assert animacao.rotulo(passo, T0 + timedelta(minutes=25)) == '14:00Z · há 25 min'
    assert animacao.rotulo(passo, T0 + timedelta(hours=3)) == '14:00Z · há 3,0 h'
    assert animacao.quadros_de_satelite(120) == 14


def _estacao(codigo, minuto):
    quando = T0 + timedelta(minutes=minuto)
    return {'localidade': codigo, 'lat_min': '-5', 'lat_max': '2', 'lon_min': '-52', 'lon_max': '-45',
            'path': f'{E}/radar/{codigo}/{minuto}.png', 'data': f'{quando:%Y-%m-%d %H:%M:%S}'}


def test_quadros_do_radar():
    dados = {'radar': [
        [_estacao('be', 0), _estacao('al', 1), _estacao('pc', -120)],    # pc parada há 2 h: fica fora
        [_estacao('be', 20), _estacao('al', 21)],
        [_estacao('be', 20), _estacao('al', 21)],                        # a API repete o último
        [{'localidade': 'mn', 'lat_min': '0', 'lat_max': '1', 'lon_min': '0', 'lon_max': '1',
          'path': None, 'data': None}],                                  # quadro sem imagem
    ]}
    quadros = redemet.converter_radar_quadros(dados)
    assert [q.quando.minute for q in quadros] == [1, 21]
    assert [i.codigo for i in quadros[0].imagens] == ['be', 'al']


def test_quadros_do_satelite():
    dados = {'lat_lon': {'lon_min': '-100', 'lon_max': '-25.24', 'lat_min': '-56', 'lat_max': '12.52'},
             'satelite': [{'data': '2026-11-13 14:10:00', 'path': f'{E}/satelite/b.png'},
                          {'data': '2026-11-13 14:00:00', 'path': f'{E}/satelite/a.png'},
                          {'data': None, 'path': f'{E}/satelite/c.png'}]}
    lista = redemet.converter_satelite_quadros(dados)
    assert [i.caminho for i in lista] == ['satelite/a.png', 'satelite/b.png']
