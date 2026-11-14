"""
A tela do CLS Visa, pensada para o meteorologista que acompanha o tráfego aéreo.

Este arquivo só monta as áreas da tela, que estão no pacote `tela/` (uma classe por área;
o desenho da tela e as regras comuns estão em tela/__init__.py), e guarda as preferências
iniciais de cada usuário. As camadas do mapa estão em camadas.py.
"""
from nicegui import app, ui

import animacao
import camadas
import config
import espaco
import radar
import wifs
from tela.cabecalho import Cabecalho
from tela.gaveta import Gaveta, PainelImpacto, PainelSonda
from tela.legenda import Legenda
from tela.linha_tempo import LinhaDoTempo
from tela.mapa import AreaDoMapa
from tela.painel_camadas import PainelCamadas

# Preferências iniciais de cada usuário (depois ficam salvas por navegador)
PADRAO = {
    'satelite_ligado': True,
    'satelite_tipo': 'realcada',
    'satelite_opacidade': 0.6,
    'radar_ligado': True,
    'radar_fonte': 'api',            # 'api' (REDEMET, sem VPN) ou 'geoserver' (precisa da VPN)
    'radar_produto_api': 'maxcappi',
    'radares': [r.codigo for r in radar.RADARES],
    'radar_produto': 'maxcappi',
    'radar_alcance': '400km',
    'radar_altura': '3',
    'dbz_min': 0,
    'radar_circulos': True,
    'radar_status_filtro': dict(radar.STATUS_FILTRO_PADRAO),
    'vento_ligado': False,
    'vento_fl': 340,
    'vento_horario': 'recente',
    'aeronaves_ligado': True,
    'aeronaves_prefixos': '',
    'aeronaves_fl': {'min': 0, 'max': 600},
    'aeronaves_rotulos': False,
    'aerodromos_ligado': True,
    'raios_ligado': True,
    'raios_fonte': 'api',            # 'api' (REDEMET, sem VPN) ou 'geoserver' (precisa da VPN)
    'raios_classificacao': 'tempo',
    'raios_raio_km': 10,
    'severo_ligado': True,
    'severo_estilo': {'cor': '#6366f1', 'opacidade': 0.6, 'raio_km': 5},
    'wifs_fl': wifs.NIVEL_PADRAO,
    'wifs': {c.chave: False for c in wifs.CAMADAS},
    'espaco': {c.chave: c.chave == 'tma' for c in espaco.CAMADAS},          # camada ligada?
    'estilos': {c.chave: dict(espaco.ESTILO_PADRAO) for c in espaco.CAMADAS if c.estilizavel},
    'animacao_janela_min': animacao.JANELA_PADRAO,
    'terminal': 'SBGR',
    'legenda_aberta': True,
}


def carregar_prefs() -> dict:
    prefs = app.storage.user.setdefault('prefs', {})
    for chave, valor in PADRAO.items():
        prefs.setdefault(chave, valor)
        if isinstance(valor, dict):  # chaves novas dentro de grupos já salvos
            for sub, padrao in valor.items():
                prefs[chave].setdefault(sub, padrao)
    # Valores salvos que deixaram de existir (ex.: arquivo de vento apagado) voltam ao padrão
    if prefs['vento_fl'] not in config.VENTO_FL_PARA_PA:
        prefs['vento_fl'] = PADRAO['vento_fl']
    return prefs


def pagina() -> None:
    """Uma página por aba do navegador. A ORDEM importa:

    1. o mapa, porque as camadas precisam dele;
    2. as camadas;
    3. as áreas da tela (algumas preenchem rótulos que as camadas usam: a idade do vento,
       a janela dos raios...);
    4. por fim, as camadas ligam conforme as preferências e os relógios começam.
    """
    prefs = carregar_prefs()
    ui.add_head_html('<link rel="stylesheet" href="/static/interface.css">')
    ui.query('.nicegui-content').classes('p-0 gap-0')

    area = AreaDoMapa()
    c = camadas.CamadasDaPagina(area.mapa, prefs)
    ui.on('rota', lambda e: c.rotas.alternar(e.args))   # botão "Ver / ocultar rota" no popup (aeronaves.js)

    painel = PainelCamadas(prefs, c)
    gaveta = Gaveta()
    sonda = PainelSonda(gaveta, area, prefs, c)
    impacto = PainelImpacto(gaveta, area, prefs, c, sonda)
    cabecalho = Cabecalho(prefs, area, c, painel.gaveta, impacto, sonda)
    legenda = Legenda(area, prefs, c)
    LinhaDoTempo(area, prefs, c)

    c.iniciar()
    c.ligar_relogios()
    # Primeiro desenho já, sem esperar o primeiro tique dos relógios
    cabecalho.saude.atualizar()
    cabecalho.pintar_modos()
    cabecalho.contar_impacto()
    legenda.desenhar()
