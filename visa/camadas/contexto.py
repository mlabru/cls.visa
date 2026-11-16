"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Camadas de contexto aeronáutico: tráfego, conflitos do FAB.ia, rotas, espaço aéreo e aeródromos.
"""
from datetime import UTC, datetime
from typing import ClassVar

import httpx
from nicegui import ui

import aerodromos
import aeronaves
import config
import desenho
import espaco
import geo
import impacto
import previsao
import rotas

from .ajudantes import escrever


class CamadaAeronaves:
    # Tráfego é contexto para o meteorologista: desenho discreto, para o tempo dominar a tela.
    # A exceção é a aeronave em área de raios ou eco forte: âmbar e sempre com o indicativo.
    ESTILO: ClassVar[dict] = {'cor': '#94a3b8', 'corIndicativo': '#cbd5e1', 'corNivel': '#94a3b8', 'tamanho': 12,
              'corAlerta': '#f59e0b'}

    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.prefs = prefs
        self.versao_enviada = -1
        with mapa:
            self.camada = mapa.generic_layer(name='aeronavesLayer',
                                             args=[{**self.ESTILO, 'rotulos': prefs['aeronaves_rotulos']}])
        self.rotulo_contador: ui.label | None = None

    def prefixos(self) -> list[str]:
        texto = self.prefs['aeronaves_prefixos'].upper()
        return [p.strip() for p in texto.split(',') if p.strip()]

    def enviar(self, forcar: bool = False) -> None:
        """Manda ao navegador as aeronaves filtradas (só se algo mudou)."""
        if not forcar and aeronaves.trafego.versao == self.versao_enviada:
            return
        self.versao_enviada = aeronaves.trafego.versao
        if self.prefs['aeronaves_ligado']:
            faixa = self.prefs['aeronaves_fl']
            lista = aeronaves.filtrar(self.prefixos(), faixa['min'], faixa['max'])
            for a in lista:          # em área de raios ou eco forte (calculado em impacto.py)
                a['alerta'] = impacto.alerta_da_aeronave(a['lat'], a['lon'])
        else:
            lista = []
        self.camada.run_method('setDados', lista)
        escrever(self.rotulo_contador, f'{len(lista)} exibidas de {len(aeronaves.trafego.aeronaves)}')

    def mudar_rotulos(self) -> None:
        self.camada.run_method('setOpcoes', {'rotulos': self.prefs['aeronaves_rotulos']})


class CamadaConflitos:
    """FAB.ia: linha tracejada de cada aeronave até onde ela encontra o núcleo, e um círculo ali.

    Segue o filtro da camada de tráfego (ligada, prefixos, faixa de níveis): linha de uma
    aeronave escondida confundiria. A linha começa na posição ATUAL da aeronave (redesenho a
    cada previsao.REDESENHO_S), não na do cálculo, que tem até 1 min.
    """

    def __init__(self, mapa: ui.leaflet, camada_aeronaves: CamadaAeronaves) -> None:
        self.mapa, self.aeronaves = mapa, camada_aeronaves
        self.camadas: list = []

    def desenhar(self) -> None:
        for camada in self.camadas:
            self.mapa.remove_layer(camada)
        self.camadas = []
        p = self.aeronaves.prefs
        if not p['aeronaves_ligado'] or not previsao.situacao.conflitos:
            return
        faixa = p['aeronaves_fl']
        visiveis = {a['indicativo'] for a in aeronaves.filtrar(self.aeronaves.prefixos(), faixa['min'], faixa['max'])}
        with self.mapa:
            conflitos = previsao.para_a_tela(previsao.situacao.conflitos, p['fabia_baixa'])
            for c in [c for c in conflitos if c.indicativo in visiveis][:previsao.MAX_CONFLITOS]:
                agora = aeronaves.trafego.aeronaves.get(c.chave)
                inicio = [agora.lat, agora.lon] if agora else [c.lat0, c.lon0]
                cor = previsao.COR_CONFIANCA[c.confianca]
                linha = self.mapa.generic_layer(name='polyline', args=[[inicio, [c.lat, c.lon]], {
                    'color': cor, 'weight': 2, 'opacity': 0.9, 'dashArray': '4 6'}])
                ponto = self.mapa.generic_layer(name='circleMarker', args=[[c.lat, c.lon], {
                    'radius': 7, 'color': cor, 'weight': 2, 'fill': True, 'fillOpacity': 0.15}])
                ponto.run_method('bindPopup', desenho.balao(
                    f'{c.indicativo} em ~{c.minutos} min',
                    [(c.nucleo.descricao(), None), (previsao.TEXTO_CONFIANCA[c.confianca], cor)],
                    ('FAB.ia: projeção em linha reta; consultivo', '#94a3b8')))
                self.camadas += [linha, ponto]


class CamadaRotas:
    """Trajetórias pedidas no popup das aeronaves (botão "Ver / ocultar rota")."""

    def __init__(self, mapa: ui.leaflet) -> None:
        self.mapa = mapa
        self.camadas: dict[str, list] = {}         # indicativo -> linhas (L.polyline) no mapa
        self.rotulo_lista: ui.label | None = None        # preenchidos por tela/painel_camadas.py
        self.botao_limpar: ui.button | None = None

    async def alternar(self, indicativo: str) -> None:
        indicativo = str(indicativo or '').upper()
        if indicativo in self.camadas:
            self.remover(indicativo)
            self.mostrar_lista()
            return
        try:
            rota = await rotas.buscar(indicativo)
        except ValueError:
            ui.notify(f'{indicativo}: indicativo sem rota consultável', type='warning')
            return
        except httpx.HTTPError as erro:
            ui.notify(f'O tracker do DECEA não respondeu ({type(erro).__name__})', type='negative')
            return
        if rota is None:
            ui.notify(f'{indicativo}: nenhuma posição registrada hoje', type='warning')
            return
        self.camadas[indicativo] = self.desenhar(rota)
        self.mostrar_lista()

    def desenhar(self, rota: rotas.Rota) -> list:
        """Uma linha por trecho: o voo atual em laranja, os anteriores do dia em cinza."""
        linhas, n = [], len(rota.trechos)
        with self.mapa:
            for i, t in enumerate(rota.trechos):
                atual = i == n - 1
                linha = self.mapa.generic_layer(name='polyline', args=[t.pontos, {
                    'color': '#f97316' if atual else '#94a3b8', 'weight': 2.5 if atual else 1.5,
                    'opacity': 0.85, 'dashArray': '6 8'}])
                trecho = f"trecho {i + 1} de {n}{' (atual)' if atual else ''}" if n > 1 else ''
                linha.run_method('bindPopup', desenho.balao(
                    rota.indicativo, [(f'{t.origem} → {t.destino}', None), (trecho, None)]))
                linhas.append(linha)
        caixa = geo.limites([p for t in rota.trechos for p in t.pontos])
        if caixa:
            self.mapa.run_map_method('fitBounds', caixa, {'padding': [30, 30], 'maxZoom': 9})
        return linhas

    def remover(self, indicativo: str) -> None:
        for linha in self.camadas.pop(indicativo):
            self.mapa.remove_layer(linha)

    def limpar(self) -> None:
        for indicativo in list(self.camadas):
            self.remover(indicativo)
        self.mostrar_lista()

    def mostrar_lista(self) -> None:
        escrever(self.rotulo_lista, 'Rotas no mapa: ' + ', '.join(self.camadas) if self.camadas else '')
        if self.botao_limpar:
            self.botao_limpar.set_visibility(bool(self.camadas))


class CamadasEspaco:
    """Aerovias, cartas, FIR, TMA: vetores do arquivo local ou, na falta dele, imagens do DECEA."""

    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.mapa, self.prefs = mapa, prefs
        self.camadas: dict[str, object] = {}
        self.vetorial: set[str] = set()          # chaves desenhadas a partir de arquivo local
        self.rotulos_fonte: dict[str, ui.label] = {}    # preenchido por tela/painel_camadas.py

    def estilo_vetor(self, chave: str) -> dict:
        definicao = espaco.POR_CHAVE[chave]
        estilo = self.prefs['estilos'].get(chave, espaco.ESTILO_PADRAO)
        cor = definicao.cor_padrao if estilo['cor'] == 'original' else estilo['cor']
        return {'cor': cor, 'traco': estilo['traco'], 'opacidade': estilo['opacidade'],
                'preenchimento': definicao.preenchimento}

    def aplicar(self, chave: str) -> None:
        if chave in self.camadas:
            self.mapa.remove_layer(self.camadas.pop(chave))
        self.vetorial.discard(chave)
        self.mostrar_fonte(chave)
        if not self.prefs['espaco'][chave]:
            return
        if espaco.arquivo_local(chave):
            with self.mapa:
                self.camadas[chave] = self.mapa.generic_layer(
                    name='espacoVetorLayer',
                    args=[f'/espaco/{chave}.geojson', {'estilo': self.estilo_vetor(chave)}])
            self.vetorial.add(chave)
            return
        definicao = espaco.POR_CHAVE[chave]
        estilo = self.prefs['estilos'].get(chave)
        opcoes = {
            'layers': definicao.camadas_wms, 'format': 'image/png', 'transparent': True,
            'version': '1.1.1', 'zIndex': 5, 'attribution': 'DECEA',
            'opacity': estilo['opacidade'] if estilo else 0.8,
        }
        if estilo:
            opcoes['estilo'] = {'cor': estilo['cor'], 'traco': estilo['traco']}
        with self.mapa:
            self.camadas[chave] = self.mapa.wms_layer(url_template=config.DECEA_WMS, options=opcoes)

    def mostrar_fonte(self, chave: str) -> None:
        texto, velho = espaco.texto_fonte(espaco.arquivo_local(chave), datetime.now(UTC).date().isoformat())
        escrever(self.rotulos_fonte.get(chave), texto, 'text-caption q-pl-lg ' + ('text-warning' if velho else 'text-grey'))

    def mudar_estilo(self, chave: str) -> None:
        camada = self.camadas.get(chave)
        if camada and chave in self.vetorial:
            camada.run_method('setEstilo', self.estilo_vetor(chave))
        elif camada:  # imagens: muda só a aparência, sem baixar de novo
            estilo = self.prefs['estilos'][chave]
            camada.run_method('setEstilo', {'cor': estilo['cor'], 'traco': estilo['traco']})
            camada.run_method('setOpacity', estilo['opacidade'])


class CamadaAerodromos:
    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.mapa, self.prefs = mapa, prefs
        with mapa:
            self.camada = mapa.generic_layer(name='aerodromosLayer', args=[{}])
        self.rotulo_situacao: ui.label | None = None

    async def enviar(self) -> None:
        await self.mapa.initialized()   # antes disso o mapa ainda não existe no navegador
        lista = await aerodromos.obter() if self.prefs['aerodromos_ligado'] else []
        self.camada.run_method('setDados', [a.para_mapa() for a in lista])
        erro = aerodromos.situacao.erro
        escrever(self.rotulo_situacao, erro or f'{len(lista)} aeródromos' if self.prefs['aerodromos_ligado'] else '',
                 'text-caption ' + ('text-negative' if erro else ''))
