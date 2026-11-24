import io
from datetime import UTC, datetime

import numpy as np
import pytest
from PIL import Image

import config
import modelo
from netcdf_teste import INICIO, NIVEIS, lats_mercator


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
    grade = modelo.grade_das_coordenadas(lats_mercator(-30, -20, 11), np.arange(-50, -39.5, 1.0))
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


def test_textos_e_escolhas_para_a_tela(rodada):
    rodada_ = modelo.listar_rodadas(rodada)[0]
    horarios = modelo.horarios_da_rodada(rodada_)
    assert modelo.horario_mais_proximo(horarios, datetime(2026, 11, 23, 1, 20, tzinfo=UTC)) == horarios[1]
    assert modelo.horario_mais_proximo(horarios, datetime(2027, 1, 1, tzinfo=UTC)) == horarios[-1]
    assert modelo.horario_mais_proximo((), INICIO) is None
    assert modelo.rotulo_horario(datetime(2026, 11, 24, 18, tzinfo=UTC), INICIO) == '24/11 18Z (+42 h)'
    assert modelo.nivel_do_fl(340) == 250
    assert modelo.url_imagem(rodada_, modelo.POR_CHAVE['vento'], horarios[1], 340) == \
        '/modelo/imagem?r=2026112300&c=vento&h=2026112301&n=250'
    assert modelo.url_imagem(rodada_, modelo.POR_CHAVE['cape'], horarios[1], 340).endswith('h=2026112301')


def test_paletas_crescentes_e_cores_validas():
    for campo in modelo.CAMPOS:
        limites = [limite for limite, _ in campo.paleta]
        assert limites == sorted(set(limites)), campo.chave
        assert all(len(cor) == 7 and cor[0] == '#' for _, cor in campo.paleta), campo.chave
        assert len(campo.paleta) < 255, campo.chave          # índice 0 é o transparente, um byte por pixel
