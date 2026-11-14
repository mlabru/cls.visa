"""
A gaveta da direita, com duas abas:
  FAB.ia    (impacto) aeródromos, TMAs e tráfego afetados agora (calculado no servidor, impacto.py)
            e os conflitos previstos de aeronaves com núcleos de tempestade (previsao.py)
  Sonda     tudo sobre um ponto do mapa (sonda.py)

Gaveta só abre, fecha e troca de aba; cada painel desenha o seu conteúdo.
"""
from nicegui import background_tasks, ui

import aeronaves
import camadas
import impacto
import previsao
import radar
import redemet
import severo
import sonda
import vento
from tela.mapa import AreaDoMapa
from tela.pecas import secao

IMPACTO, SONDA = 'impacto', 'sonda'


class Gaveta:
    """O painel da direita (escondido até ser pedido) e as suas abas."""

    def __init__(self) -> None:
        self.gaveta = ui.right_drawer(value=False).props('width=350 bordered').classes('q-pa-sm painel')
        with self.gaveta:
            with ui.tabs().props('dense align=justify narrow-indicator').classes('w-full text-grey-5') as abas:
                ui.tab(IMPACTO, label='FAB.ia', icon='warning')
                ui.tab(SONDA, label='Sonda', icon='ads_click')
            with ui.tab_panels(abas, value=IMPACTO).classes('w-full bg-transparent') as self.paineis:
                self.aba_impacto = ui.tab_panel(IMPACTO).classes('q-pa-none q-pt-sm')
                self.aba_sonda = ui.tab_panel(SONDA).classes('q-pa-none q-pt-sm')

    def mostrando(self, aba: str) -> bool:
        """A gaveta está aberta nesta aba?"""
        return bool(self.gaveta.value) and self.paineis.value == aba

    def mostrar(self, aba: str) -> None:
        self.paineis.set_value(aba)
        self.gaveta.show()

    def esconder(self) -> None:
        self.gaveta.hide()


# ---------------------------------------------------------------------------
# Sonda
# ---------------------------------------------------------------------------

class PainelSonda:
    """Clique direito no mapa (sempre) ou clique com a sonda ligada: mostra o ponto na aba Sonda."""

    def __init__(self, gaveta: Gaveta, area: AreaDoMapa, prefs: dict, camadas_: camadas.CamadasDaPagina) -> None:
        self.gaveta, self.area, self.mapa, self.prefs = gaveta, area, area.mapa, prefs
        self.severo = camadas_.severo
        self.ligada = False          # modo "clique esquerdo sonda"
        self.marca = None            # círculo amarelo no ponto sondado
        self._pedido = 0             # o último clique vence (ver DECISOES.md, 5.4)
        with gaveta.aba_sonda:
            self.conteudo = ui.column().classes('w-full gap-1')
            self.dica = ui.label('Clique direito no mapa (ou ligue "Sonda" no cabeçalho e clique) para ver '
                                 'tudo sobre um ponto.').classes('legenda-texto')
        self.mapa.on('map-click', self._no_clique)
        self.mapa.on('map-contextmenu', self._no_clique_direito)

    def alternar_modo(self) -> bool:
        """Liga/desliga o modo em que o clique esquerdo sonda. Devolve se ficou ligado."""
        self.ligada = not self.ligada
        if self.ligada:
            self.area.area.classes(add='sonda-ligada')       # cursor em cruz (interface.css)
        else:
            self.area.area.classes(remove='sonda-ligada')
        return self.ligada

    def _no_clique(self, e) -> None:
        if self.ligada:
            self._no_clique_direito(e)

    def _no_clique_direito(self, e) -> None:
        ponto = e.args['latlng']
        self.ir(ponto['lat'], ponto['lng'])

    def ir(self, lat: float, lon: float) -> None:
        """Sonda o ponto em segundo plano (a consulta pode levar alguns segundos)."""
        background_tasks.create(self.sondar(lat, lon), name='sonda')

    def fechar(self) -> None:
        self._pedido += 1                # uma resposta ainda a caminho não reabre o painel
        if self.marca:
            self.mapa.remove_layer(self.marca)
            self.marca = None
        self.gaveta.esconder()

    async def sondar(self, lat: float, lon: float) -> None:
        self._pedido += 1
        pedido = self._pedido
        if self.marca:
            self.mapa.remove_layer(self.marca)
        with self.mapa:
            self.marca = self.mapa.generic_layer(name='circleMarker', args=[[lat, lon], {
                'radius': 8, 'color': '#fbbf24', 'weight': 2, 'fillOpacity': 0.15, 'interactive': False}])
        self.conteudo.clear()
        with self.conteudo:
            self._titulo(sonda.Resultado(lat, lon).titulo)
            ui.spinner(size='lg').classes('self-center q-mt-lg')
        self.dica.set_visibility(False)
        self.gaveta.mostrar(SONDA)
        s = self.severo
        com_severo = self.prefs['severo_ligado'] and s.feicoes
        resultado = await sonda.sondar(
            lat, lon, self.prefs['radar_produto_api'],
            s.feicoes if com_severo else None, s.tipo,
            self.prefs['severo_estilo']['raio_km'], s.escolha['validade'])
        if pedido == self._pedido:       # a pessoa não clicou em outro ponto nesse meio-tempo
            self._desenhar(resultado)

    def _titulo(self, titulo: str) -> None:
        with ui.row().classes('w-full items-center no-wrap'):
            ui.icon('ads_click', size='sm').classes('text-amber')
            with ui.column().classes('col gap-0'):
                ui.label('Sonda').classes('text-subtitle2 text-weight-bold')
                ui.label(titulo).classes('text-caption text-grey')
            ui.button(icon='close', on_click=self.fechar).props('flat dense round size=sm color=grey-5')

    def _desenhar(self, r: sonda.Resultado) -> None:
        self.conteudo.clear()
        with self.conteudo:
            self._titulo(r.titulo)
            self._radar(r)
            self._raios(r)
            self._severo(r)
            self._vento(r)
            self._aerodromo(r)
            ui.label('WIFS no ponto (turbulência, gelo) depende da VPN: ainda não disponível.') \
                .classes('legenda-texto q-mt-md')

    @staticmethod
    def _radar(r: sonda.Resultado) -> None:
        secao('Radar', 'radar')
        if r.radar_erro:
            ui.label(r.radar_erro).classes('legenda-texto')
            return
        if not r.radar_agora:
            return
        dbz, sigla, quando = r.radar_agora
        with ui.row().classes('items-center gap-2'):
            ui.element('span').classes('sonda-cor').style(f'background: {radar.cor_dbz(dbz)}')
            ui.label('sem eco' if dbz is None else f'{dbz} dBZ').classes('text-body2 text-weight-bold')
            ui.label(f'{sigla} · {quando:%H:%MZ}').classes('legenda-texto')
        # Histórico: uma barrinha por quadro, altura e cor pelo dBZ
        with ui.row().classes('sonda-barras items-end no-wrap gap-0'):
            for quando_q, dbz_q in r.radar_historico:
                altura = 3 if dbz_q is None else 6 + dbz_q * 0.6
                with ui.element('div').classes('col').style(
                        f'height: {altura:.0f}px; background: {radar.cor_dbz(dbz_q)}; margin: 0 1px'):
                    ui.tooltip(f"{quando_q:%H:%MZ}: {'sem eco' if dbz_q is None else f'{dbz_q} dBZ'}")
        if r.radar_historico:
            with ui.row().classes('w-full justify-between'):
                ui.label(f'{r.radar_historico[0][0]:%H:%MZ}').classes('legenda-texto')
                ui.label('dBZ no ponto, quadro a quadro').classes('legenda-texto')
                ui.label(f'{r.radar_historico[-1][0]:%H:%MZ}').classes('legenda-texto')

    @staticmethod
    def _raios(r: sonda.Resultado) -> None:
        secao('Raios', 'bolt')
        if r.raios_erro:
            ui.label(r.raios_erro).classes('legenda-texto')
            return
        if not r.raios:
            return
        c = r.raios.contagem
        ui.label(' · '.join(f'{c[d]} até {d} km' for d in sonda.RAIOS_DISTANCIAS_KM)).classes('text-body2')
        ui.label(f'células de 0,1° com descargas · quadro de {r.raios_quando:%H:%MZ}').classes('legenda-texto')
        if r.raios.mais_perto:
            d, minutos = r.raios.mais_perto
            ui.label(f'mais próxima a {d:.0f} km, ativa há {minutos} min').classes('legenda-texto')
        if r.raios.novas_20km:
            ui.label(f'{r.raios.novas_20km} célula(s) nova(s) (< {redemet.CELULA_NOVA_MIN} min) até '
                     f'{sonda.RAIO_CELULAS_NOVAS_KM} km: convecção começando ou crescendo') \
                .classes('text-caption text-red-4')

    @staticmethod
    def _severo(r: sonda.Resultado) -> None:
        secao('Tempo severo previsto', 'thunderstorm')
        if r.severo is None:
            ui.label('camada desligada ou sem previsão carregada').classes('legenda-texto')
            return
        validade = f' (válida {severo.rotulo(r.severo_validade)})' if r.severo_validade else ''
        if r.severo:
            ui.label('DENTRO de área de risco' + validade).classes('text-body2 text-red-4 text-weight-bold')
        else:
            ui.label('fora das áreas de risco' + validade).classes('legenda-texto')

    @staticmethod
    def _vento(r: sonda.Resultado) -> None:
        secao('Vento em altitude', 'air')
        if not r.vento:
            ui.label('sem arquivos de vento').classes('legenda-texto')
            return
        texto, cor = vento.idade(r.vento_referencia)
        ui.label(f'referência {r.vento_referencia:%d/%m %H:%MZ} · {texto}').classes(f'text-caption text-{cor}')
        with ui.grid(columns=3).classes('w-full gap-x-2 gap-y-0 sonda-vento'):
            for fl, direcao, kt in reversed(r.vento):     # de cima para baixo
                ui.label(f'FL{fl:03d}').classes('legenda-texto')
                with ui.row().classes('items-center gap-1 no-wrap'):
                    # a seta aponta para onde o vento vai (de onde vem + 180°)
                    ui.icon('navigation', size='14px').style(f'transform: rotate({direcao + 180}deg)')
                    ui.label(f'{direcao:03d}°').classes('legenda-texto')
                ui.label(f'{kt} kt').classes('legenda-texto text-weight-bold' if kt >= 50 else 'legenda-texto')

    @staticmethod
    def _aerodromo(r: sonda.Resultado) -> None:
        secao('Aeródromo mais próximo', 'local_airport')
        if not r.aerodromo:
            ui.label('lista de aeródromos ainda não carregada (ligue a camada)').classes('legenda-texto')
            return
        a, d = r.aerodromo
        with ui.row().classes('items-center gap-2 no-wrap'):
            ui.element('span').classes('sonda-cor').style(f'background: {a.cor}; border-radius: 50%')
            ui.label(f'{a.icao} – {a.nome}').classes('text-body2')
        ui.label(f'a {d:.0f} km').classes('legenda-texto')
        if a.texto:
            ui.label(a.texto).classes('sonda-boletim')


# ---------------------------------------------------------------------------
# Impacto
# ---------------------------------------------------------------------------

class PainelImpacto:
    """Lista o que impacto.situacao calculou. Redesenha a cada 15 s, só se a aba estiver aberta."""

    REDESENHO_S = 15
    MAX_AERODROMOS = 30
    MAX_AERONAVES = 60
    MAX_CONFLITOS = 30

    def __init__(self, gaveta: Gaveta, area: AreaDoMapa, prefs: dict,
                 camadas_: camadas.CamadasDaPagina, painel_sonda: PainelSonda) -> None:
        self.gaveta, self.mapa, self.prefs = gaveta, area.mapa, prefs
        self.severo, self.sonda = camadas_.severo, painel_sonda
        with gaveta.aba_impacto:
            self.conteudo = ui.column().classes('w-full gap-1')
        ui.timer(self.REDESENHO_S, self.desenhar)

    def alternar(self) -> None:
        """Botão "FAB.ia" do cabeçalho: abre a aba (ou fecha, se já estava aberta nela)."""
        if self.gaveta.mostrando(IMPACTO):
            self.gaveta.esconder()
        else:
            self.gaveta.mostrar(IMPACTO)
            self.desenhar()

    def _ir_para(self, item: impacto.Impacto) -> None:
        """Clique num item: centra o mapa nele e abre a sonda no ponto."""
        self.mapa.set_center((item.lat, item.lon))
        self.mapa.set_zoom(9 if item.tipo == 'aerodromo' else 8)
        self.sonda.ir(item.lat, item.lon)

    def _item(self, item: impacto.Impacto, extra: list[str]) -> None:
        cor, _ = impacto.NIVEIS[item.nivel]
        with ui.element('div').classes('impacto-item w-full').style(f'border-left-color: {cor}') \
                .on('click', lambda i=item: self._ir_para(i)):
            with ui.row().classes('items-baseline no-wrap gap-1'):
                ui.label(item.ident).classes('text-body2 text-weight-bold')
                ui.label(item.nome).classes('legenda-texto ellipsis')
            ui.label('; '.join([*item.motivos, *extra])).classes('impacto-motivos')

    def desenhar(self) -> None:
        if not self.gaveta.mostrando(IMPACTO):
            return                                   # aba fechada: nada a fazer
        s = impacto.situacao
        self.conteudo.clear()
        with self.conteudo:
            if s.erro:
                ui.label(s.erro).classes('text-caption text-negative')
            if not s.versao:
                ui.label('calculando (a primeira vez leva uns 10 s)…').classes('legenda-texto')
                return
            self._resumo(s)
            self._aerodromos(s)
            self._tmas(s)
            self._conflitos()
            self._trafego()

    @staticmethod
    def _resumo(s: impacto.Situacao) -> None:
        fontes = []
        if s.radar_quando:
            fontes.append(f'radar {s.radar_quando:%H:%MZ}')
        if s.raios_quando:
            fontes.append(f'raios {s.raios_quando:%H:%MZ}')
        ui.label(' · '.join(fontes) + ' · atualiza a cada minuto').classes('legenda-texto')
        with ui.row().classes('gap-2 q-my-xs'):
            for nivel, (cor, texto) in impacto.NIVEIS.items():
                n = sum(1 for i in s.aerodromos + s.tmas if i.nivel == nivel)
                with ui.row().classes('items-center gap-1 no-wrap'):
                    ui.icon('circle', size='10px').style(f'color: {cor}')
                    ui.label(f'{texto}: {n}').classes('legenda-texto')

    def _aerodromos(self, s: impacto.Situacao) -> None:
        secao(f'Aeródromos ({len(s.aerodromos)})', 'local_airport')
        if not s.aerodromos:
            ui.label('nenhum aeródromo afetado').classes('legenda-texto')
        # O tempo severo previsto é de cada usuário (a escolha de modelo e validade é dele):
        # por isso entra aqui, como motivo extra, e não no cálculo comum do servidor.
        severo_ativo = self.prefs['severo_ligado'] and self.severo.feicoes
        raio_km = self.prefs['severo_estilo']['raio_km']
        for item in s.aerodromos[:self.MAX_AERODROMOS]:
            extra = []
            if severo_ativo and sonda.em_area_severa(self.severo.feicoes, self.severo.tipo, raio_km,
                                                     item.lat, item.lon):
                extra.append('em área de tempo severo prevista')
            self._item(item, extra)

    def _tmas(self, s: impacto.Situacao) -> None:
        secao(f'TMAs ({len(s.tmas)})', 'radio_button_checked')
        if not s.tmas:
            ui.label('nenhuma TMA afetada (ou sem o arquivo de TMAs)').classes('legenda-texto')
        for item in s.tmas:
            self._item(item, [])

    def _conflitos(self) -> None:
        """FAB.ia: aeronaves em rota que vão cruzar um núcleo nos próximos 30 min (previsao.py)."""
        s = previsao.situacao
        secao(f'Conflitos previstos ({len(s.conflitos)})', 'route')
        ui.label(f'projeção em linha reta (rumo e velocidade atuais), até {previsao.HORIZONTE_MIN} min; '
                 f'só aeronaves em rota (FL{previsao.FL_MIN:03d}+); linhas tracejadas no mapa') \
            .classes('legenda-texto')
        if s.calculado is None:
            ui.label('aguardando o primeiro cálculo').classes('legenda-texto')
            return
        if not s.conflitos:
            ui.label('nenhum conflito previsto').classes('legenda-texto')
        for c in s.conflitos[:self.MAX_CONFLITOS]:
            cor = previsao.COR_CONFIANCA[c.confianca]
            with ui.element('div').classes('impacto-item w-full').style(f'border-left-color: {cor}') \
                    .on('click', lambda c=c: self._ir_para_conflito(c)):
                with ui.row().classes('items-baseline no-wrap gap-2'):
                    ui.label(c.indicativo).classes('text-body2 text-weight-bold')
                    ui.label('—' if c.fl is None else f'FL{c.fl:03d}').classes('legenda-texto')
                    ui.label(f'em ~{c.minutos} min').classes('text-caption').style(f'color: {cor}')
                ui.label(f'{c.nucleo.descricao()}; {previsao.TEXTO_CONFIANCA[c.confianca]}') \
                    .classes('impacto-motivos')
        r = previsao.resumir(s.placar)
        if r['alertas'] or r['nao_previstos']:
            ui.label(f"desde {s.desde:%d/%m %H:%MZ}: {r['alertas']} alertas, {r['acertos']} confirmados, "
                     f"{r['falsos']} falsos, {r['nao_previstos']} entradas não previstas") \
                .classes('legenda-texto q-mt-xs')

    def _ir_para_conflito(self, c: previsao.Conflito) -> None:
        """Clique: mostra a aeronave e o ponto de encontro juntos."""
        self.mapa.set_center(((c.lat0 + c.lat) / 2, (c.lon0 + c.lon) / 2))
        self.mapa.set_zoom(8)

    def _trafego(self) -> None:
        em_alerta = [(a.indicativo, a.nivel_voo, motivo) for a in aeronaves.trafego.aeronaves.values()
                     if (motivo := impacto.alerta_da_aeronave(a.lat, a.lon))]
        secao(f'Tráfego em área de risco ({len(em_alerta)})', 'flight')
        if not em_alerta:
            ui.label('nenhuma aeronave em área de risco').classes('legenda-texto')
            return
        ui.label('em células com raios (ou vizinhas) ou eco ≥ '
                 f'{impacto.ECO_FORTE_DBZ} dBZ · em âmbar no mapa').classes('legenda-texto')
        with ui.grid(columns=3).classes('w-full gap-x-2 gap-y-0'):
            for indicativo, fl, motivo in sorted(em_alerta, key=lambda x: x[0])[:self.MAX_AERONAVES]:
                ui.label(indicativo).classes('text-caption text-amber')
                ui.label('—' if fl is None else f'FL{fl:03d}').classes('legenda-texto')
                ui.label(motivo).classes('legenda-texto')
