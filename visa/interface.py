"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

A tela do CLS Visa, pensada para o meteorologista que acompanha o tráfego aéreo.

Este arquivo só monta as áreas da tela, que estão no pacote `tela/` (uma classe por área;
o desenho da tela e as regras comuns estão em tela/__init__.py), e lê as preferências do
navegador (o conteúdo delas está em preferencias.py). As camadas do mapa estão no pacote camadas/.
"""
from nicegui import app, ui

import camadas
import preferencias
from tela.cabecalho import Cabecalho
from tela.gaveta import Gaveta, PainelImpacto, PainelSonda
from tela.legenda import Legenda
from tela.linha_tempo import LinhaDoTempo
from tela.mapa import AreaDoMapa
from tela.painel_camadas import PainelCamadas


def carregar_prefs() -> dict:
    """As preferências deste navegador, completadas com o que faltar (preferencias.completar)."""
    return preferencias.completar(app.storage.user.setdefault('prefs', {}))


def pagina() -> None:
    """Monta a página (uma por aba do navegador). A ORDEM importa (DECISOES.md, 5.5).

    Fluxo:
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
    camadas_ = camadas.CamadasDaPagina(area.mapa, prefs)
    # botão "Ver / ocultar rota" no popup das aeronaves (aeronaves.js)
    ui.on('rota', lambda evento: camadas_.rotas.alternar(evento.args))

    painel = PainelCamadas(prefs, camadas_)
    gaveta = Gaveta()
    sonda = PainelSonda(gaveta, area, prefs, camadas_)
    impacto = PainelImpacto(gaveta, area, prefs, camadas_, sonda)
    cabecalho = Cabecalho(prefs, area, camadas_, painel.gaveta, impacto, sonda)
    legenda = Legenda(area, prefs, camadas_)
    LinhaDoTempo(area, prefs, camadas_)

    camadas_.iniciar()
    camadas_.ligar_relogios()
    # Primeiro desenho já, sem esperar o primeiro tique dos relógios
    cabecalho.saude.atualizar()
    cabecalho.pintar_modos()
    cabecalho.contar_impacto()
    legenda.desenhar()
