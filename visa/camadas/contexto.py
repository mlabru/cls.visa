"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Camadas de contexto aeronáutico: tráfego, conflitos do FAB.ia, rotas, espaço aéreo e aeródromos.

Criadas por `CamadasDaPagina` (camadas/__init__.py). As aeronaves e os aeródromos são desenhados
em JS (static/aeronaves.js e static/camadas.js): daqui vão só os dados prontos (`setDados`).
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
    """Tráfego aéreo ao vivo, desenhado num canvas por static/aeronaves.js.

    O Python filtra (ligada, prefixos, faixa de níveis) e marca o alerta; o JS só pinta.
    """

    # Tráfego é contexto para o meteorologista: desenho discreto, para o tempo dominar a tela.
    # A exceção é a aeronave em área de raios ou eco forte: âmbar e sempre com o indicativo.
    ESTILO: ClassVar[dict] = {'cor': '#94a3b8', 'corIndicativo': '#cbd5e1', 'corNivel': '#94a3b8',
                              'tamanho': 12, 'corAlerta': '#f59e0b'}

    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.prefs = prefs
        self.versao_enviada = -1                  # versão de aeronaves.trafego já mandada ao navegador
        with mapa:
            self.camada = mapa.generic_layer(name='aeronavesLayer',
                                             args=[{**self.ESTILO, 'rotulos': prefs['aeronaves_rotulos']}])
        self.rotulo_contador: ui.label | None = None

    def prefixos(self) -> list[str]:
        """Lê o filtro de prefixos do painel ('GLO, TAM' -> ['GLO', 'TAM'])."""
        texto = self.prefs['aeronaves_prefixos'].upper()
        return [prefixo.strip() for prefixo in texto.split(',') if prefixo.strip()]

    def enviar(self, forcar: bool = False) -> None:
        """Manda ao navegador as aeronaves filtradas (só se algo mudou).

        Parâmetros:
            forcar: se True, manda mesmo sem mudança no tráfego (ex.: o filtro mudou).
        """
        if not forcar and aeronaves.trafego.versao == self.versao_enviada:
            return
        self.versao_enviada = aeronaves.trafego.versao
        lista = []
        if self.prefs['aeronaves_ligado']:
            faixa = self.prefs['aeronaves_fl']
            lista = aeronaves.filtrar(self.prefixos(), faixa['min'], faixa['max'])
            for aeronave in lista:          # em área de raios ou eco forte (calculado em impacto.py)
                aeronave['alerta'] = impacto.alerta_da_aeronave(aeronave['lat'], aeronave['lon'])
        self.camada.run_method('setDados', lista)
        escrever(self.rotulo_contador, f'{len(lista)} exibidas de {len(aeronaves.trafego.aeronaves)}')

    def mudar_rotulos(self) -> None:
        """Liga/desliga os rótulos (indicativo e nível) no desenho do JS."""
        self.camada.run_method('setOpcoes', {'rotulos': self.prefs['aeronaves_rotulos']})


class CamadaConflitos:
    """FAB.ia: linha tracejada de cada aeronave até onde ela encontra o núcleo, e um círculo ali.

    Segue o filtro da camada de tráfego (ligada, prefixos, faixa de níveis): linha de uma
    aeronave escondida confundiria. A linha começa na posição ATUAL da aeronave (redesenho a
    cada previsao.REDESENHO_S), não na do cálculo, que tem até 1 min.
    """

    def __init__(self, mapa: ui.leaflet, camada_aeronaves: CamadaAeronaves) -> None:
        self.mapa = mapa
        self.aeronaves = camada_aeronaves
        self.camadas: list = []

    def desenhar(self) -> None:
        """Apaga os conflitos desenhados e desenha os atuais (chamado pelo relógio da página)."""
        for camada in self.camadas:
            self.mapa.remove_layer(camada)
        self.camadas = []
        prefs = self.aeronaves.prefs
        if not prefs['aeronaves_ligado'] or not previsao.situacao.conflitos:
            return
        with self.mapa:
            for conflito in self._conflitos_visiveis():
                self.camadas += self._desenhar_conflito(conflito)

    def _conflitos_visiveis(self) -> list[previsao.Conflito]:
        """Filtra os conflitos para a tela: só de aeronaves visíveis, no máximo `previsao.MAX_CONFLITOS`.

        Retorna:
            Os conflitos a desenhar, na ordem de `previsao.para_a_tela`.
        """
        prefs = self.aeronaves.prefs
        faixa = prefs['aeronaves_fl']
        filtradas = aeronaves.filtrar(self.aeronaves.prefixos(), faixa['min'], faixa['max'])
        visiveis = {aeronave['indicativo'] for aeronave in filtradas}
        conflitos = previsao.para_a_tela(previsao.situacao.conflitos, prefs['fabia_baixa'])
        return [conflito for conflito in conflitos if conflito.indicativo in visiveis][:previsao.MAX_CONFLITOS]

    def _desenhar_conflito(self, conflito: previsao.Conflito) -> list:
        """Desenha a linha da aeronave até o encontro e o círculo com o balão.

        Parâmetros:
            conflito: o conflito previsto pelo FAB.ia.

        Retorna:
            [linha, círculo]: as camadas criadas (para apagar no próximo redesenho).
        """
        posicao_agora = aeronaves.trafego.aeronaves.get(conflito.chave)
        if posicao_agora:
            inicio = [posicao_agora.lat, posicao_agora.lon]
        else:
            inicio = [conflito.lat0, conflito.lon0]
        encontro = [conflito.lat, conflito.lon]
        cor = previsao.COR_CONFIANCA[conflito.confianca]
        linha = self.mapa.generic_layer(name='polyline', args=[[inicio, encontro], {
            'color': cor, 'weight': 2, 'opacity': 0.9, 'dashArray': '4 6'}])
        ponto = self.mapa.generic_layer(name='circleMarker', args=[encontro, {
            'radius': 7, 'color': cor, 'weight': 2, 'fill': True, 'fillOpacity': 0.15}])
        ponto.run_method('bindPopup', desenho.balao(
            f'{conflito.indicativo} em ~{conflito.minutos} min',
            [(conflito.nucleo.descricao(), None), (previsao.TEXTO_CONFIANCA[conflito.confianca], cor)],
            ('FAB.ia: projeção em linha reta; consultivo', '#94a3b8')))
        return [linha, ponto]


class CamadaRotas:
    """Trajetórias pedidas no popup das aeronaves (botão "Ver / ocultar rota")."""

    def __init__(self, mapa: ui.leaflet) -> None:
        self.mapa = mapa
        self.camadas: dict[str, list] = {}         # indicativo -> linhas (L.polyline) no mapa
        self.rotulo_lista: ui.label | None = None        # preenchidos por tela/painel_camadas.py
        self.botao_limpar: ui.button | None = None

    async def alternar(self, indicativo: str) -> None:
        """Mostra a rota da aeronave, ou a esconde se já está no mapa (evento 'rota' do aeronaves.js).

        Parâmetros:
            indicativo: o indicativo de chamada (ex.: 'GLO1234').
        """
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
        """Desenha uma linha por trecho: o voo atual em laranja, os anteriores do dia em cinza.

        Também enquadra o mapa na rota inteira.

        Parâmetros:
            rota: a rota do tracker do DECEA.

        Retorna:
            As linhas criadas (para remover depois).
        """
        linhas = []
        total = len(rota.trechos)
        with self.mapa:
            for indice, trecho in enumerate(rota.trechos):
                atual = indice == total - 1
                if atual:
                    estilo = {'color': '#f97316', 'weight': 2.5}
                else:
                    estilo = {'color': '#94a3b8', 'weight': 1.5}
                linha = self.mapa.generic_layer(name='polyline', args=[trecho.pontos, {
                    **estilo, 'opacity': 0.85, 'dashArray': '6 8'}])
                linha.run_method('bindPopup', desenho.balao(
                    rota.indicativo, [(f'{trecho.origem} → {trecho.destino}', None),
                                      (_texto_do_trecho(indice, total), None)]))
                linhas.append(linha)
        pontos = [ponto for trecho in rota.trechos for ponto in trecho.pontos]
        caixa = geo.limites(pontos)
        if caixa:
            self.mapa.run_map_method('fitBounds', caixa, {'padding': [30, 30], 'maxZoom': 9})
        return linhas

    def remover(self, indicativo: str) -> None:
        """Tira do mapa as linhas de uma rota.

        Parâmetros:
            indicativo: o indicativo da rota (tem que estar no mapa).
        """
        for linha in self.camadas.pop(indicativo):
            self.mapa.remove_layer(linha)

    def limpar(self) -> None:
        """Tira todas as rotas do mapa (botão "limpar" do painel)."""
        for indicativo in list(self.camadas):
            self.remover(indicativo)
        self.mostrar_lista()

    def mostrar_lista(self) -> None:
        """Escreve no painel quais rotas estão no mapa e mostra o botão "limpar" se houver alguma."""
        texto = ''
        if self.camadas:
            texto = 'Rotas no mapa: ' + ', '.join(self.camadas)
        escrever(self.rotulo_lista, texto)
        if self.botao_limpar:
            self.botao_limpar.set_visibility(bool(self.camadas))


def _texto_do_trecho(indice: int, total: int) -> str:
    """Linha do balão que diz qual trecho é este ('trecho 2 de 3 (atual)').

    Parâmetros:
        indice: posição do trecho (0 = primeiro voo do dia).
        total: quantos trechos a rota tem.

    Retorna:
        O texto; '' se a rota tem um trecho só.
    """
    if total <= 1:
        return ''
    texto = f'trecho {indice + 1} de {total}'
    if indice == total - 1:
        texto += ' (atual)'
    return texto


class CamadasEspaco:
    """Aerovias, cartas, FIR, TMA: vetores do arquivo local ou, na falta dele, imagens do DECEA."""

    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.mapa = mapa
        self.prefs = prefs
        self.camadas: dict[str, object] = {}
        self.vetorial: set[str] = set()          # chaves desenhadas a partir de arquivo local
        self.rotulos_fonte: dict[str, ui.label] = {}    # preenchido por tela/painel_camadas.py

    def estilo_vetor(self, chave: str) -> dict:
        """Monta o estilo de uma camada vetorial para o L.espacoVetorLayer (static/camadas.js).

        Parâmetros:
            chave: a camada (`espaco.CAMADAS[...].chave`).

        Retorna:
            {cor, traco, opacidade, preenchimento}; a cor 'original' vira a cor padrão da camada.
        """
        definicao = espaco.POR_CHAVE[chave]
        estilo = self.prefs['estilos'].get(chave, espaco.ESTILO_PADRAO)
        cor = definicao.cor_padrao if estilo['cor'] == 'original' else estilo['cor']
        return {'cor': cor, 'traco': estilo['traco'], 'opacidade': estilo['opacidade'],
                'preenchimento': definicao.preenchimento}

    def aplicar(self, chave: str) -> None:
        """Liga ou desliga uma camada: vetorial se há arquivo local, senão imagens WMS do DECEA.

        Parâmetros:
            chave: a camada (`espaco.CAMADAS[...].chave`).
        """
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
        """Escreve no painel de onde vem a camada (arquivo local e a data dele, ou o DECEA).

        Parâmetros:
            chave: a camada (`espaco.CAMADAS[...].chave`).
        """
        hoje = datetime.now(UTC).date().isoformat()
        texto, velho = espaco.texto_fonte(espaco.arquivo_local(chave), hoje)
        cor = 'text-warning' if velho else 'text-grey'
        escrever(self.rotulos_fonte.get(chave), texto, 'text-caption q-pl-lg ' + cor)

    def mudar_estilo(self, chave: str) -> None:
        """Aplica cor, traço e opacidade escolhidos sem recriar a camada.

        Parâmetros:
            chave: a camada (`espaco.CAMADAS[...].chave`).
        """
        camada = self.camadas.get(chave)
        if not camada:
            return
        if chave in self.vetorial:
            camada.run_method('setEstilo', self.estilo_vetor(chave))
            return
        # imagens: muda só a aparência, sem baixar de novo
        estilo = self.prefs['estilos'][chave]
        camada.run_method('setEstilo', {'cor': estilo['cor'], 'traco': estilo['traco']})
        camada.run_method('setOpacity', estilo['opacidade'])


class CamadaAerodromos:
    """Aeródromos coloridos pela condição do boletim, desenhados por static/camadas.js."""

    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.mapa = mapa
        self.prefs = prefs
        with mapa:
            self.camada = mapa.generic_layer(name='aerodromosLayer', args=[{}])
        self.rotulo_situacao: ui.label | None = None

    async def enviar(self) -> None:
        """Manda ao navegador os aeródromos (ou nenhum, se desligada) e escreve a situação no painel."""
        await self.mapa.initialized()   # antes disso o mapa ainda não existe no navegador
        ligada = self.prefs['aerodromos_ligado']
        lista = await aerodromos.obter() if ligada else []
        self.camada.run_method('setDados', [aerodromo.para_mapa() for aerodromo in lista])
        erro = aerodromos.situacao.erro
        texto = ''
        if self.prefs['aerodromos_ligado']:
            texto = erro or f'{len(lista)} aeródromos'
        escrever(self.rotulo_situacao, texto, 'text-caption ' + ('text-negative' if erro else ''))
