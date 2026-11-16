"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

A animação no mapa: prepara as imagens das últimas horas e troca a opacidade a cada passo.
A linha do tempo (passos de 10 min) é calculada em animacao.py.
"""
import logging

from nicegui import background_tasks, ui

import animacao
import redemet

from .ajudantes import OPACIDADE_RADAR, OPACIDADE_RAIOS
from .observacao import CamadaRadar, CamadaRaios, CamadaSatelite

log = logging.getLogger(__name__)


class CamadaAnimacao:
    """Anima radar (fonte API) e satélite nas últimas horas.

    Ao começar, o Python baixa e prepara todas as imagens (ficam no cache do servidor) e
    cria no mapa uma camada por imagem, invisível. Cada passo só muda a opacidade: liga as
    imagens daquele horário e desliga as do anterior. Nada pisca e nada é baixado de novo.
    Enquanto anima, as camadas ao vivo ficam escondidas e paradas.
    """

    def __init__(self, mapa: ui.leaflet, prefs: dict, camada_radar: CamadaRadar,
                 camada_satelite: CamadaSatelite, camada_raios: CamadaRaios) -> None:
        self.mapa, self.prefs = mapa, prefs
        self.radar, self.satelite, self.raios = camada_radar, camada_satelite, camada_raios
        self.passos: list[animacao.Passo] = []
        self.indice = 0
        self.ativa = self.tocando = self.preparando = False
        self.aviso = ''
        self.camadas: dict[str, object] = {}      # url -> camada (invisível até ser a vez dela)
        self.visiveis: dict[str, float] = {}      # url -> opacidade, as que estão na tela
        self.ao_mudar = lambda: None              # a linha do tempo (tela/) atualiza os controles
        self._assinatura: tuple = ()
        self._pausa = 0

    def assinatura(self) -> tuple:
        """O que, se mudar, invalida a animação (ela para e volta ao vivo)."""
        p = self.prefs
        return (p['satelite_ligado'], p['satelite_tipo'], p['satelite_opacidade'], p['radar_ligado'],
                p['radar_fonte'], p['radar_produto_api'], tuple(p['radares']), p['dbz_min'],
                p['raios_ligado'], p['raios_fonte'], p['animacao_janela_min'])

    def _informar(self, aviso: str = '') -> None:
        self.aviso = aviso
        self.ao_mudar()

    async def iniciar(self) -> None:
        if self.ativa or self.preparando:
            return
        p = self.prefs
        com_satelite = p['satelite_ligado']
        com_radar = p['radar_ligado'] and self.radar.pela_api()
        com_raios = p['raios_ligado'] and self.raios.pela_api()
        if not (com_satelite or com_radar or com_raios):
            self._informar('ligue o satélite, o radar ou os raios (fonte API) para animar')
            return
        self.preparando, inicio = True, self.assinatura()
        self._informar('buscando os horários…')
        try:
            janela, dbz = p['animacao_janela_min'], p['dbz_min']
            satelites = await redemet.satelite_quadros(
                p['satelite_tipo'], animacao.quadros_de_satelite(janela)) if com_satelite else []
            quadros = await redemet.radar_quadros(p['radar_produto_api']) if com_radar else []
            escolhidas = set(p['radares'])
            quadros = [redemet.QuadroRadar(q.quando, tuple(i for i in q.imagens if i.codigo in escolhidas))
                       for q in quadros]
            relampagos = await redemet.raios_quadros() if com_raios else []
            passos = animacao.linha_do_tempo(satelites, quadros, janela, relampagos)
            if not passos:
                self._informar('a REDEMET não devolveu imagens para animar')
                return
            imagens = {passo.satelite.url(): (passo.satelite, 0) for passo in passos if passo.satelite}
            imagens.update({i.url(dbz): (i, dbz) for passo in passos if passo.radar for i in passo.radar.imagens})
            quadros_raios = {passo.raios.url(): passo.raios for passo in passos if passo.raios}
            self._informar(f'preparando {len(imagens) + len(quadros_raios)} imagens…')
            await redemet.preparar(list(imagens.values()))
            for quadro in quadros_raios.values():          # desenhadas aqui mesmo (rápido)
                redemet.imagem_raios(quadro.chave)
        except redemet.Indisponivel as erro:
            self._informar(str(erro))
            return
        except Exception:
            # Qualquer outra falha (resposta inesperada da API, por exemplo): avisa e registra.
            # Sem isto o erro sumiria no log e o botão ficaria "preparando" para sempre.
            log.exception('animação: falha ao preparar')
            self._informar('falha ao preparar a animação (detalhes no log)')
            return
        finally:
            self.preparando = False     # em qualquer saída: o botão volta a funcionar
        if self.assinatura() != inicio:     # a pessoa mudou as camadas enquanto preparava
            self._informar('as camadas mudaram: tente de novo')
            return
        for camada in (self.radar, self.satelite, self.raios):
            camada.pausar_ao_vivo()
        with self.mapa:
            for url, (imagem, _) in imagens.items():
                if imagem.caminho.startswith('satelite/'):   # por baixo, como a camada ao vivo
                    opcoes = {'opacity': 0, 'pane': 'tilePane', 'zIndex': 2, 'interactive': False}
                else:
                    opcoes = {'opacity': 0, 'interactive': False}
                self.camadas[url] = self.mapa.image_overlay(url=url, bounds=imagem.limites, options=opcoes)
            for url, quadro in quadros_raios.items():        # por último: ficam por cima do radar
                self.camadas[url] = self.mapa.image_overlay(url=url, bounds=quadro.limites, options={
                    'opacity': 0, 'interactive': False, 'className': 'pixelado'})
        self.passos, self._assinatura = passos, inicio
        self.ativa = self.tocando = True
        self._pausa = 0
        avisos = [f'{nome} do GeoServer não anima' for nome, ligado, api in
                  (('o radar', p['radar_ligado'], com_radar), ('os raios', p['raios_ligado'], com_raios))
                  if ligado and not api]
        self.aviso = '; '.join(avisos)
        self.mostrar(0)

    def _alvo(self, passo: animacao.Passo) -> dict[str, float]:
        alvo = {}
        if passo.satelite:
            alvo[passo.satelite.url()] = self.prefs['satelite_opacidade']
        if passo.radar:
            for imagem in passo.radar.imagens:
                alvo[imagem.url(self.prefs['dbz_min'])] = OPACIDADE_RADAR
        if passo.raios:
            alvo[passo.raios.url()] = OPACIDADE_RAIOS
        return alvo

    def mostrar(self, indice: int) -> None:
        """Liga as imagens do passo `indice` e desliga as que não fazem parte dele."""
        alvo = self._alvo(self.passos[indice])
        for url in set(self.visiveis) - set(alvo):
            self.camadas[url].run_method('setOpacity', 0)
        for url, opacidade in alvo.items():
            if url not in self.visiveis:
                self.camadas[url].run_method('setOpacity', opacidade)
        self.visiveis, self.indice = alvo, indice
        self.ao_mudar()

    def tique(self) -> None:
        """Chamado pelo relógio da página: avança um passo (e para de vez se as camadas mudaram)."""
        if not self.ativa:
            return
        if self.assinatura() != self._assinatura:
            self.parar('as camadas mudaram: de volta ao vivo')
            return
        if not self.tocando:
            return
        if self.indice < len(self.passos) - 1:
            self.mostrar(self.indice + 1)
        elif self._pausa < animacao.PAUSA_NO_FIM:   # fica um pouco no mais recente
            self._pausa += 1
        else:
            self._pausa = 0
            self.mostrar(0)

    def alternar(self) -> None:
        """Botão tocar/pausar (a primeira vez, prepara e começa)."""
        if not self.ativa:
            background_tasks.create(self.iniciar(), name='animacao')
            return
        self.tocando = not self.tocando
        self.ao_mudar()

    def andar(self, passos: int) -> None:
        if self.ativa:
            self.tocando = False
            self.mostrar((self.indice + passos) % len(self.passos))

    def ir_para(self, indice: int) -> None:
        if self.ativa and indice != self.indice and 0 <= indice < len(self.passos):
            self.tocando = False
            self.mostrar(indice)

    def parar(self, aviso: str = '') -> None:
        """Volta ao vivo: tira as imagens da animação e reaparece com as camadas atualizadas."""
        for camada in self.camadas.values():
            self.mapa.remove_layer(camada)
        self.camadas.clear()
        self.visiveis = {}
        self.passos, self.indice = [], 0
        self.ativa = self.tocando = False
        for camada in (self.radar, self.satelite, self.raios):
            camada.retomar_ao_vivo()
        self._informar(aviso)
