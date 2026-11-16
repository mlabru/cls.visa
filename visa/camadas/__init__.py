"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Camadas do mapa: cada classe liga, desliga e atualiza uma camada do Leaflet.

Não monta painel nenhum: o pacote `tela/` (painel_camadas.py) cria os controles e chama estes métodos.

Arquivos:
  observacao.py     radar, satélite, raios
  animacao_mapa.py  a animação dessas três no mapa
  prevista.py       tempo severo, WIFS, vento
  contexto.py       tráfego, conflitos (FAB.ia), rotas, espaço aéreo, aeródromos
  ajudantes.py      opacidades, UltimoPedido, escrever (comuns a várias camadas)
  __init__.py       CamadasDaPagina: todas as camadas de uma página e os seus relógios

Padrões que se repetem nas classes (explicados em DECISOES.md):
  aplicar()      recria a camada a partir das preferências (liga, desliga, troca de fonte);
  atualizar()    troca só o que mudou (setUrl/setParams), para o mapa não piscar;
  _pedido        UltimoPedido: resposta de um pedido velho é ignorada;
  congelada      True durante a animação: a camada ao vivo não troca de imagem;
  self.prefs     é o MESMO dicionário de preferências da interface (não uma cópia).
Os atributos de tela (ui.label etc.) começam como None e são preenchidos por tela/painel_camadas.py.
"""
from nicegui import ui

import animacao
import config
import espaco
import previsao
import severo
import vento
import wifs

from .animacao_mapa import CamadaAnimacao
from .contexto import CamadaAerodromos, CamadaAeronaves, CamadaConflitos, CamadaRotas, CamadasEspaco
from .observacao import CamadaRadar, CamadaRaios, CamadaSatelite
from .prevista import CamadaSevero, CamadasWifs, CamadaVento

# ---------------------------------------------------------------------------
# Todas as camadas de uma página
# ---------------------------------------------------------------------------

class CamadasDaPagina:
    """As camadas de UMA página (uma aba do navegador), criadas juntas.

    Existe para que cada área da tela (pacote `tela/`) receba um objeto só, e não um
    parâmetro por camada. Cada aba aberta tem o seu conjunto: as preferências e o que está no mapa
    são de cada pessoa. O que é comum a todos (aeronaves, impacto, caches) fica nos módulos.
    """

    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.satelite = CamadaSatelite(mapa, prefs)
        self.radar = CamadaRadar(mapa, prefs)
        self.vento = CamadaVento(mapa, prefs)
        self.espaco = CamadasEspaco(mapa, prefs)
        self.aerodromos = CamadaAerodromos(mapa, prefs)
        self.aeronaves = CamadaAeronaves(mapa, prefs)
        self.conflitos = CamadaConflitos(mapa, self.aeronaves)
        self.raios = CamadaRaios(mapa, prefs)
        self.wifs = CamadasWifs(mapa, prefs)
        self.severo = CamadaSevero(mapa, prefs)
        self.rotas = CamadaRotas(mapa)
        self.animacao = CamadaAnimacao(mapa, prefs, self.radar, self.satelite, self.raios)

    def iniciar(self) -> None:
        """Liga as camadas conforme as preferências salvas.

        Chamar DEPOIS de montar o painel: algumas camadas escrevem em rótulos que o painel
        cria (ex.: a idade do vento, a janela dos raios). As que consultam a rede vão num
        ui.timer(0, ...) para não atrasar a abertura da página.
        """
        ui.timer(0, self.satelite.aplicar, once=True)
        self.radar.aplicar()
        self.vento.aplicar()
        self.raios.aplicar()
        for camada in wifs.CAMADAS:
            self.wifs.aplicar(camada.chave)
        ui.timer(0, lambda: self.radar.consultar_status(aplicar_filtro=True), once=True)
        ui.timer(0, self.severo.ligar, once=True)
        for definicao in espaco.CAMADAS:
            self.espaco.aplicar(definicao.chave)
        ui.timer(0, self.aerodromos.enviar, once=True)

    def ligar_relogios(self) -> None:
        """Atualizações periódicas de cada camada (os intervalos estão em config.py e nos módulos)."""
        ui.timer(config.AERONAVE_ENVIO_S, self.aeronaves.enviar)
        ui.timer(previsao.REDESENHO_S, self.conflitos.desenhar)
        ui.timer(config.RADAR_ATUALIZA_S, self.radar.atualizar_horario)
        ui.timer(config.RADAR_ATUALIZA_S, self.raios.no_relogio)
        ui.timer(config.RADAR_ATUALIZA_S, self.radar.consultar_status)
        ui.timer(wifs.RECARREGA_S, self.wifs.recarregar)
        ui.timer(config.SATELITE_ATUALIZA_S, self.satelite.atualizar)
        ui.timer(severo.CATALOGO_CACHE_S, lambda: self.severo.carregar_catalogo(forcar=True))
        ui.timer(vento.IDADE_ATUALIZA_S, self.vento.mostrar_idade)
        ui.timer(config.AERODROMOS_ATUALIZA_S, self.aerodromos.enviar)
        ui.timer(animacao.INTERVALO_S, self.animacao.tique)
