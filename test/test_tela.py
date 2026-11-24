"""Testes da tela, sem navegador (nicegui.testing): a página monta e os botões principais funcionam.

A página é montada direto de interface.pagina (sem main.py: nada de Pusher nem tarefas de fundo).
Todos os clientes HTTP dos módulos respondem 503, então nenhum teste acessa a rede, e a tela
tem de aguentar as fontes fora do ar. Qualquer log de ERROR durante o teste o reprova.
"""
import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from nicegui import ui
from nicegui.testing import User, user_simulation

import aerodromos
import config
import impacto
import interface
import modos
import preferencias
import previsao
import radar
import redemet
import rotas
import severo

AGORA = datetime.now(UTC)
pytestmark = pytest.mark.anyio          # testes assíncronos pelo plugin do anyio (vem com o NiceGUI)


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def sem_rede(monkeypatch, tmp_path):
    """Toda consulta HTTP dos módulos recebe 503 (como se as fontes estivessem fora do ar)."""
    transporte = httpx.MockTransport(lambda pedido: httpx.Response(503, text='fora (teste)'))
    for modulo in (aerodromos, radar, redemet, rotas, severo):
        monkeypatch.setattr(modulo, '_cliente', httpx.AsyncClient(transport=transporte))
    monkeypatch.setattr(redemet, '_cache_api', {})
    monkeypatch.setattr(config, 'MODELO_DIR', tmp_path / 'sem_modelo')   # nem os arquivos reais do WRF


@pytest.fixture
def prefs(monkeypatch):
    """As preferências da página: um dicionário do teste, para conferir o que os botões mudam."""
    valores = preferencias.completar({})
    monkeypatch.setattr(interface, 'carregar_prefs', lambda: valores)
    return valores


@pytest.fixture
def estado(monkeypatch):
    """Estado do servidor zerado (impacto e FAB.ia), sem afetar os outros testes."""
    monkeypatch.setattr(impacto, 'situacao', impacto.Situacao())
    monkeypatch.setattr(previsao, 'situacao', previsao.Situacao())


@pytest.fixture
async def user(sem_rede, prefs, estado, caplog):
    async with user_simulation(root=interface.pagina) as usuario:
        yield usuario
    erros = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert not erros, erros


async def abrir(user: User) -> None:
    """Abre a página e espera a primeira volta dos relógios (ui.timer roda logo ao conectar).

    Sem esperar, um redesenho do relógio logo depois de um clique poderia esconder um botão que
    não redesenha nada (o teste passaria por acaso).
    """
    await user.open('/')
    await asyncio.sleep(0.5)


def _conflito(chave: str, confianca: str) -> previsao.Conflito:
    nucleo = previsao.Nucleo(previsao.RADAR, ((0, 0), (0, 1)), 0.05, 0.1, 50, AGORA)
    return previsao.Conflito(chave, chave, 350, 5, 0, 0, 0, -1, nucleo, confianca, AGORA)


def _com_impacto(**campos) -> None:
    s = impacto.situacao
    s.versao, s.radar_quando, s.raios_quando = 1, AGORA, AGORA
    s.fontes = frozenset({previsao.RADAR, previsao.RAIOS})
    for nome, valor in campos.items():
        setattr(s, nome, valor)


# --- página ------------------------------------------------------------------------

async def test_pagina_abre_com_as_fontes_fora(user: User):
    await abrir(user)
    for texto in ('CLS Visa', 'FAB.ia', 'Sonda', 'Convecção', 'Altitude', 'Terminal', 'Observação', 'Legenda'):
        await user.should_see(texto)


async def test_modo_altitude_liga_as_camadas_dele(user: User, prefs):
    await abrir(user)
    user.find(marker='modo-altitude').click()
    assert modos.ativo(prefs) == 'altitude'
    assert prefs['vento_ligado']


# --- FAB.ia ------------------------------------------------------------------------

async def test_fabia_esconde_confianca_baixa_e_a_chave_mostra(user: User, prefs):
    _com_impacto()
    s = previsao.situacao
    s.calculado = AGORA
    s.conflitos = [_conflito('ALT1', previsao.ALTA), _conflito('BAI2', previsao.BAIXA)]
    await abrir(user)
    user.find(marker='botao-fabia').click()
    await user.should_see('1 conflito(s) (1 de confiança baixa escondido(s))')
    await user.should_see('ALT1')
    await user.should_not_see('BAI2')
    user.find(kind=ui.switch, content='mostrar confiança baixa').click()
    await user.should_see('2 conflito(s)')
    await user.should_see('BAI2')
    assert prefs['fabia_baixa']


async def test_fabia_com_a_chave_ja_ligada_mostra_tudo(user: User, prefs):
    # Pessoa que deixou a chave ligada: abrir o painel não pode falhar (a chave é criada já ligada)
    prefs['fabia_baixa'] = True
    _com_impacto()
    previsao.situacao.calculado = AGORA
    previsao.situacao.conflitos = [_conflito('ALT1', previsao.ALTA), _conflito('BAI2', previsao.BAIXA)]
    await abrir(user)
    user.find(marker='botao-fabia').click()
    await user.should_see('2 conflito(s)')


async def test_fabia_parado_e_fonte_atrasada_aparecem(user: User):
    _com_impacto(radar_quando=AGORA - timedelta(hours=2), fontes=frozenset({previsao.RAIOS}))
    previsao.situacao.pausa = 'sem quadro recente (teste)'
    await abrir(user)
    user.find(marker='botao-fabia').click()
    await user.should_see('FAB.ia parado: sem quadro recente (teste)')
    await user.should_see('radar atrasado(s): fora do tráfego em área de risco e do FAB.ia')


async def test_clicar_num_aerodromo_abre_a_sonda(user: User):
    item = impacto.Impacto('aerodromo', 'SBGR', 'Guarulhos', -23.43, -46.47, impacto.ALTO,
                           ('raios a 4 km (célula ativa há 6 min)',), 50)
    _com_impacto(aerodromos=[item])
    await abrir(user)
    user.find(marker='botao-fabia').click()
    await user.should_see('raios a 4 km (célula ativa há 6 min)')
    user.find(marker='impacto-SBGR').click()
    await user.should_see('23,43°S 46,47°O', retries=20)     # a sonda mostra o ponto escolhido


# --- animação ----------------------------------------------------------------------

async def test_animacao_sem_rede_avisa_e_nao_quebra(user: User):
    await abrir(user)
    user.find(marker='animacao-tocar').click()
    await user.should_see('API da REDEMET indisponível', retries=20)


# --- modelo WRF --------------------------------------------------------------------

async def test_modelo_ligado_mostra_rodada_e_anda_no_horario(user: User, prefs, rodada, monkeypatch):
    monkeypatch.setattr(config, 'MODELO_DIR', rodada)
    prefs['modelo_ligado'] = True
    await abrir(user)
    await user.should_see('rodada 23/11 00Z · previsão do modelo, não observação', retries=20)
    await user.should_see('WRF · Refletividade máxima simulada')
    await user.should_see('23/11 02Z (+2 h)')            # hoje é depois da rodada de teste: o último horário
    user.find(marker='modelo-antes').click()
    await user.should_see('23/11 01Z (+1 h)', retries=20)


async def test_modelo_sem_rodada_avisa(user: User, prefs):
    prefs['modelo_ligado'] = True
    await abrir(user)
    await user.should_see('Nenhuma rodada em', retries=20)


async def test_sonda_mostra_o_perfil_do_modelo(user: User, rodada, monkeypatch):
    monkeypatch.setattr(config, 'MODELO_DIR', rodada)
    item = impacto.Impacto('aerodromo', 'SBXX', 'Teste', -25.0, -45.0, impacto.ALTO, ('teste',), 50)
    _com_impacto(aerodromos=[item])
    await abrir(user)
    user.find(marker='botao-fabia').click()
    user.find(marker='impacto-SBXX').click()
    await user.should_see('Perfil previsto (WRF)', retries=20)
    await user.should_see('Isoterma de 0 °C: abaixo do primeiro nível (1000 hPa já com -20 °C)')
    await user.should_see('270°/19 kt')


async def test_modelo_vento_em_particulas(user: User, prefs, rodada, monkeypatch):
    monkeypatch.setattr(config, 'MODELO_DIR', rodada)
    prefs['modelo_ligado'] = True
    await abrir(user)
    await user.should_see('rodada 23/11 00Z', retries=20)
    user.find(marker='modelo-vento').click()
    assert prefs['modelo_vento']
    await user.should_see('vento em partículas · FL340 · valor sob o cursor', retries=20)


async def test_sonda_mostra_turbulencia_e_gelo(user: User, rodada, monkeypatch):
    monkeypatch.setattr(config, 'MODELO_DIR', rodada)
    item = impacto.Impacto('aerodromo', 'SBXX', 'Teste', -25.0, -45.0, impacto.ALTO, ('teste',), 50)
    _com_impacto(aerodromos=[item])
    await abrir(user)
    user.find(marker='botao-fabia').click()
    user.find(marker='impacto-SBXX').click()
    await user.should_see('turb / gelo', retries=20)
    await user.should_see('— / mod')                     # sem turbulência; gelo moderado (0,3 g/kg a −20 °C)
