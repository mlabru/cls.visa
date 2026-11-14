"""
PainelCamadas: a gaveta da esquerda, com um cartão por camada em três grupos.

  Observação            Satélite, Radar, Raios
  Previsão              Tempo severo, WIFS, Vento
  Contexto aeronáutico  Tráfego, Aeródromos, Espaço aéreo

Cada cartão tem a chave liga/desliga no título e os ajustes dentro. Os controles são
ligados (bind_value) às preferências; ao mudar, chamam o método da camada correspondente.
Um método por cartão: para mexer num cartão, procure o método com o nome dele.
"""
from nicegui import ui

import camadas
import config
import espaco
import radar
import raios
import redemet
import wifs
from tela.pecas import cartao, grupo

FONTES = {'api': 'API REDEMET', 'geoserver': 'GeoServer (VPN)'}


class PainelCamadas:

    def __init__(self, prefs: dict, camadas_: camadas.CamadasDaPagina) -> None:
        self.prefs, self.c = prefs, camadas_
        self.gaveta = ui.left_drawer(value=True).props('width=300 bordered').classes('q-pa-xs painel')
        # Botões das estações de radar e dos filtros de status (pintados por _pintar_radares)
        self._botoes_radar: dict[str, ui.button] = {}
        self._dicas_radar: dict[str, ui.tooltip] = {}
        self._filtros_status: dict[str, ui.button] = {}
        self._textos_status: dict[str, ui.label] = {}
        with self.gaveta:
            grupo('Observação')
            self._satelite()
            self._radar()
            self._raios()
            grupo('Previsão')
            self._severo()
            self._wifs()
            self._vento()
            grupo('Contexto aeronáutico')
            self._trafego()
            self._aerodromos()
            self._espaco()

    # ===== OBSERVAÇÃO ==============================================================

    def _satelite(self) -> None:
        p, sat = self.prefs, self.c.satelite
        with cartao('Satélite', 'satellite_alt', p, 'satelite_ligado', sat.aplicar):
            ui.select(redemet.SATELITES, label='Imagem', on_change=sat.aplicar) \
                .bind_value(p, 'satelite_tipo').props('dense').classes('w-full')
            with ui.row().classes('w-full items-center no-wrap'):
                ui.label('Opacidade').classes('text-caption')
                ui.slider(min=0.2, max=1, step=0.1).bind_value(p, 'satelite_opacidade') \
                    .on('change', sat.mudar_opacidade).classes('col')
            sat.rotulo_info = ui.label().classes('text-caption text-grey')

    def _radar(self) -> None:
        p, rad = self.prefs, self.c.radar
        with cartao('Radar', 'radar', p, 'radar_ligado', rad.aplicar):
            ui.toggle(FONTES, on_change=rad.mudar_produto) \
                .bind_value(p, 'radar_fonte').props('dense no-caps size=sm spread').classes('w-full')
            # Produto pela API...
            ui.select({k: f'{r} ({a} km)' for k, (r, a) in redemet.PRODUTOS_RADAR.items()}, label='Produto',
                      on_change=rad.mudar_produto) \
                .bind_value(p, 'radar_produto_api').props('dense').classes('w-full') \
                .bind_visibility_from(p, 'radar_fonte', value='api')
            # ...ou pelo GeoServer (outros nomes; ver DECISOES.md, 2.1)
            with ui.column().classes('w-full gap-0').bind_visibility_from(p, 'radar_fonte', value='geoserver'):
                with ui.row().classes('w-full no-wrap'):
                    ui.select(radar.PRODUTOS, label='Produto', on_change=rad.mudar_produto) \
                        .bind_value(p, 'radar_produto').props('dense').classes('col')
                    ui.select(radar.ALCANCES, label='Alcance', on_change=rad.mudar_produto) \
                        .bind_value(p, 'radar_alcance').props('dense').classes('col')
                ui.select(radar.ALTURAS_CAPPI, label='Altura do CAPPI', on_change=rad.mudar_produto) \
                    .bind_value(p, 'radar_altura').props('dense') \
                    .bind_visibility_from(p, 'radar_produto', value='cappi').classes('w-full')

            with ui.row().classes('w-full items-center no-wrap q-mt-xs'):
                ui.label('Limiar').classes('text-caption')
                limiar = ui.slider(min=0, max=75, step=5).bind_value(p, 'dbz_min').classes('col')
                ui.label().bind_text_from(p, 'dbz_min', lambda v: 'tudo' if v == 0 else f'≥ {v} dBZ') \
                    .classes('text-caption').style('min-width: 56px')
            limiar.on('change', lambda: rad.mudar_limiar())  # 'change' = ao soltar o controle, não a cada passo

            with ui.row().classes('w-full items-center q-mt-xs'):
                ui.label('Estações').classes('text-caption col')
                ui.button('todas', on_click=lambda: self._todos_radares(True)).props('flat dense size=sm')
                ui.button('nenhuma', on_click=lambda: self._todos_radares(False)).props('flat dense size=sm')
            with ui.grid(columns=5).classes('gap-1 w-full'):
                for r in radar.RADARES:
                    with ui.button(r.sigla, on_click=lambda c=r.codigo: self._alternar_radar(c)) \
                            .props('dense size=sm unelevated') as botao:
                        self._dicas_radar[r.codigo] = ui.tooltip(f'{r.nome}/{r.uf}')
                    self._botoes_radar[r.codigo] = botao
            # Filtros por status: clicar (des)seleciona os radares daquele status
            with ui.column().classes('gap-0 w-full'):
                for status, (cor, texto) in radar.STATUS.items():
                    with ui.button(on_click=lambda s=status: rad.alternar_filtro(s)) \
                            .props('flat dense no-caps size=sm align=left').classes('w-full') as botao, \
                            ui.row().classes('items-center no-wrap gap-2'):
                        ui.icon('circle', size='xs').style(f'color: {cor}')
                        self._textos_status[status] = ui.label(texto).classes('text-caption')
                    self._filtros_status[status] = botao
            rad.ao_mudar_status = self._pintar_radares      # a camada avisa quando chega o status
            self._pintar_radares()
            ui.switch('Círculos de alcance', on_change=rad.desenhar_circulos) \
                .bind_value(p, 'radar_circulos').props('dense')

    def _pintar_radares(self) -> None:
        """Cor do botão = selecionado ou não; barra embaixo = status da última imagem."""
        p, rad = self.prefs, self.c.radar
        for codigo, botao in self._botoes_radar.items():
            botao.props(f"color={'primary' if codigo in p['radares'] else 'grey-8'}")
            r = radar.POR_CODIGO[codigo]
            quando = rad.ultimas.get(codigo)
            if rad.status_ok:
                cor = radar.STATUS[rad.status_de(codigo)][0]
                botao.style(f'border-bottom: 3px solid {cor}')
                self._dicas_radar[codigo].set_text(f'{r.nome}/{r.uf} · última imagem: '
                                                   + (f'{quando:%d/%m %H:%MZ}' if quando else 'sem dados'))
            else:
                botao.style('border-bottom: 3px solid transparent')
                self._dicas_radar[codigo].set_text(f'{r.nome}/{r.uf}')
        contagem = dict.fromkeys(radar.STATUS, 0)
        for r in radar.RADARES:
            contagem[rad.status_de(r.codigo)] += 1
        for status, botao in self._filtros_status.items():
            _, texto = radar.STATUS[status]
            pct = round(100 * contagem[status] / len(radar.RADARES))
            self._textos_status[status].set_text(f'{texto} · {pct}%')
            botao.style(f'opacity: {1 if p["radar_status_filtro"][status] else 0.4}')
            botao.set_visibility(rad.status_ok)

    def _alternar_radar(self, codigo: str) -> None:
        escolhidos = set(self.prefs['radares']) ^ {codigo}
        self.prefs['radares'] = [r.codigo for r in radar.RADARES if r.codigo in escolhidos]   # ordem fixa
        self._pintar_radares()
        self.c.radar.aplicar()

    def _todos_radares(self, sim: bool) -> None:
        self.prefs['radares'] = [r.codigo for r in radar.RADARES] if sim else []
        for status in self.prefs['radar_status_filtro']:   # os filtros acompanham
            self.prefs['radar_status_filtro'][status] = sim
        self._pintar_radares()
        self.c.radar.aplicar()

    def _raios(self) -> None:
        p, ra = self.prefs, self.c.raios
        with cartao('Raios', 'bolt', p, 'raios_ligado', ra.aplicar):
            ui.toggle(FONTES, on_change=ra.aplicar) \
                .bind_value(p, 'raios_fonte').props('dense no-caps size=sm spread').classes('w-full')
            ui.label('Células de 0,1° com descargas recentes. A cor diz há quanto tempo a célula '
                     'está ativa: vermelho é convecção nova. Para voltar no tempo, use a animação.') \
                .classes('text-caption text-grey').bind_visibility_from(p, 'raios_fonte', value='api')
            with ui.column().classes('w-full gap-1').bind_visibility_from(p, 'raios_fonte', value='geoserver'):
                ui.select(raios.CLASSIFICACOES, label='Cores por', on_change=ra.atualizar) \
                    .bind_value(p, 'raios_classificacao').props('dense').classes('w-full')
                with ui.row().classes('w-full items-center no-wrap'):
                    ui.label('Raio do círculo').classes('text-caption')
                    raio = ui.slider(min=1, max=10, step=1).bind_value(p, 'raios_raio_km').classes('col')
                    ui.label().bind_text_from(p, 'raios_raio_km', lambda v: f'{v} km').classes('text-caption')
                raio.on('change', lambda: ra.atualizar())
                with ui.row().classes('w-full items-center no-wrap'):
                    ui.button(icon='chevron_left', on_click=lambda: ra.andar(-raios.PASSO_MIN)) \
                        .props('flat dense').tooltip(f'{raios.PASSO_MIN} min antes')
                    ra.rotulo_janela = ui.label().classes('text-caption col text-center')
                    ui.button(icon='chevron_right', on_click=lambda: ra.andar(raios.PASSO_MIN)) \
                        .props('flat dense').tooltip(f'{raios.PASSO_MIN} min depois')
                    ui.button('agora', on_click=ra.ao_vivo).props('flat dense size=sm')

    # ===== PREVISÃO ================================================================

    def _severo(self) -> None:
        p, sev = self.prefs, self.c.severo
        with cartao('Tempo severo', 'thunderstorm', p, 'severo_ligado', sev.ligar):
            # Escolhas em cascata: cada select depende do anterior (CamadaSevero._cascata)
            rotulos = {'origem': 'Modelo', 'resolucao': 'Resolução', 'tipo': 'Tipo', 'rodada': 'Rodada'}
            for linha in (('origem', 'resolucao'), ('tipo', 'rodada')):
                with ui.row().classes('w-full no-wrap'):
                    for nivel in linha:
                        sev.seletores[nivel] = ui.select(
                            {}, label=rotulos[nivel], on_change=lambda n=nivel: sev.mudou(n)) \
                            .props('dense').classes('col')
            with ui.row().classes('w-full items-center no-wrap'):
                ui.button(icon='chevron_left', on_click=lambda: sev.andar(-1)).props('flat dense')
                sev.seletores['validade'] = ui.select(
                    {}, label='Validade', on_change=lambda: sev.mudou('validade')) \
                    .props('dense').classes('col')
                ui.button(icon='chevron_right', on_click=lambda: sev.andar(1)).props('flat dense')
            estilo = p['severo_estilo']
            ui.color_input('Cor', on_change=sev.mudar_estilo) \
                .bind_value(estilo, 'cor').props('dense').classes('w-full')
            # Áreas têm opacidade; pontos (desenhados como círculos) têm raio
            with ui.row().classes('w-full items-center no-wrap') \
                    .bind_visibility_from(sev.escolha, 'tipo', value='area'):
                ui.label('Opacidade').classes('text-caption')
                ui.slider(min=0.1, max=1, step=0.1).bind_value(estilo, 'opacidade') \
                    .on('change', sev.mudar_estilo).classes('col')
            with ui.row().classes('w-full items-center no-wrap') \
                    .bind_visibility_from(sev.escolha, 'tipo', value='ponto'):
                ui.label('Raio').classes('text-caption')
                ui.slider(min=1, max=10, step=1).bind_value(estilo, 'raio_km') \
                    .on('change', sev.mudar_estilo).classes('col')
                ui.label().bind_text_from(estilo, 'raio_km', lambda v: f'{v} km').classes('text-caption')
            sev.rotulo_situacao = ui.label().classes('text-caption')

    def _wifs(self) -> None:
        p, w = self.prefs, self.c.wifs
        with cartao('WIFS em altitude', 'cloud'):      # sem chave geral: cada variável liga sozinha
            ui.select(wifs.NIVEIS, label='Nível de voo', on_change=w.trocar_nivel) \
                .bind_value(p, 'wifs_fl').props('dense').classes('w-full')
            for camada in wifs.CAMADAS:
                with ui.row().classes('w-full items-center no-wrap'):
                    ui.icon(camada.icone, size='xs').classes('text-grey')
                    ui.switch(camada.titulo, on_change=lambda c=camada.chave: w.aplicar(c)) \
                        .bind_value(p['wifs'], camada.chave).props('dense')

    def _vento(self) -> None:
        p, ve = self.prefs, self.c.vento
        with cartao('Vento em altitude', 'air', p, 'vento_ligado', ve.aplicar):
            niveis = {fl: f'FL{fl:03d}' for fl in config.VENTO_FL_PARA_PA}
            with ui.row().classes('w-full no-wrap'):
                ui.select(niveis, label='Nível', on_change=ve.trocar_nivel) \
                    .bind_value(p, 'vento_fl').props('dense').classes('col')
                ve.seletor_horario = ui.select(ve.opcoes_horario(), label='Horário', on_change=ve.aplicar) \
                    .bind_value(p, 'vento_horario').props('dense').classes('col')
            ve.rotulo_aviso = ui.label()

    # ===== CONTEXTO AERONÁUTICO ====================================================

    def _trafego(self) -> None:
        p, ae, ro = self.prefs, self.c.aeronaves, self.c.rotas
        with cartao('Tráfego', 'flight', p, 'aeronaves_ligado', lambda: ae.enviar(forcar=True)):
            ui.switch('Rótulos (indicativo e nível)', on_change=ae.mudar_rotulos) \
                .bind_value(p, 'aeronaves_rotulos').props('dense')
            ui.input('Filtrar indicativos (ex.: GLO, TAM)', on_change=lambda: ae.enviar(forcar=True)) \
                .bind_value(p, 'aeronaves_prefixos').props('dense clearable').classes('w-full')
            ui.label('Faixa de nível (FL)').classes('text-caption')
            ui.range(min=0, max=600, step=10, on_change=lambda: ae.enviar(forcar=True)) \
                .bind_value(p, 'aeronaves_fl').props('label')
            ae.rotulo_contador = ui.label().classes('text-caption')
            ui.label('Clique numa aeronave e em "Ver / ocultar rota" para ver a trajetória.') \
                .classes('text-caption text-grey')
            with ui.row().classes('w-full items-center no-wrap'):
                ro.rotulo_lista = ui.label().classes('text-caption col')
                ro.botao_limpar = ui.button('limpar', on_click=ro.limpar).props('flat dense size=sm')
            ro.mostrar_lista()

    def _aerodromos(self) -> None:
        ad = self.c.aerodromos
        with cartao('Aeródromos', 'local_airport', self.prefs, 'aerodromos_ligado', ad.enviar):
            ad.rotulo_situacao = ui.label().classes('text-caption')

    def _espaco(self) -> None:
        p, es = self.prefs, self.c.espaco
        with cartao('Espaço aéreo', 'route'):
            for definicao in espaco.CAMADAS:
                chave = definicao.chave
                ui.switch(definicao.titulo, on_change=lambda c=chave: es.aplicar(c)) \
                    .bind_value(p['espaco'], chave).props('dense')
                es.rotulos_fonte[chave] = ui.label().bind_visibility_from(p['espaco'], chave)   # "fonte: arquivo local..."
                if not definicao.estilizavel:
                    continue
                estilo = p['estilos'][chave]
                mudar = lambda c=chave: es.mudar_estilo(c)     # um nome curto para 3 controles
                with ui.column().classes('w-full q-pl-lg gap-1').bind_visibility_from(p['espaco'], chave):
                    ui.select(espaco.CORES, label='Cor', on_change=mudar) \
                        .bind_value(estilo, 'cor').props('dense').classes('w-full')
                    ui.toggle(espaco.TRACOS, on_change=mudar).bind_value(estilo, 'traco').props('dense size=sm')
                    with ui.row().classes('w-full items-center no-wrap'):
                        ui.label('Opacidade').classes('text-caption')
                        ui.slider(min=0, max=1, step=0.05).bind_value(estilo, 'opacidade') \
                            .on('change', mudar).classes('col')
