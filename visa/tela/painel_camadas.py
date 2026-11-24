"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

PainelCamadas: a gaveta da esquerda, com um cartão por camada em três grupos.
Criado por interface.pagina(), antes das camadas iniciarem (alguns rótulos criados
aqui são preenchidos pelas camadas: `sat.rotulo_info`, `ve.rotulo_aviso`...).

  Observação            Satélite, Radar, Raios
  Previsão              Tempo severo, WIFS, Vento, Modelo WRF
  Contexto aeronáutico  Tráfego, Aeródromos, Espaço aéreo

Cada cartão tem a chave liga/desliga no título e os ajustes dentro.  Os
controles são ligados (bind_value) às preferências; ao mudar, chamam o
método da camada correspondente.  Um método por cartão: para mexer num
cartão, procure o método com o nome dele.
"""
from nicegui import ui

import camadas
import config
import espaco
import modelo
import radar
import raios
import redemet
import wifs
from tela.pecas import cartao, grupo

FONTES = {'api': 'API REDEMET', 'geoserver': 'GeoServer (VPN)'}

# Rótulos das escolhas em cascata do tempo severo, e como ficam arrumadas em linhas
ROTULOS_SEVERO = {'origem': 'Modelo', 'resolucao': 'Resolução', 'tipo': 'Tipo', 'rodada': 'Rodada'}
LINHAS_SEVERO = (('origem', 'resolucao'), ('tipo', 'rodada'))


def _texto_limiar(dbz_min: int) -> str:
    """O limiar do radar como texto ("tudo" quando é zero).

    Parâmetros:
        dbz_min: limiar em dBZ.
    """
    return 'tudo' if dbz_min == 0 else f'≥ {dbz_min} dBZ'


def _texto_km(valor: int) -> str:
    """Um raio em km como texto ("5 km")."""
    return f'{valor} km'


class PainelCamadas:
    """A gaveta da esquerda: um método por cartão (`_satelite`, `_radar`...).

    Atributos:
        prefs: preferências do usuário.
        camadas: as camadas da página.
        gaveta: o ui.left_drawer (o botão ☰ do cabeçalho o mostra e esconde).
    """

    def __init__(self, prefs: dict, camadas_: camadas.CamadasDaPagina) -> None:
        """Cria a gaveta e os cartões, na ordem dos grupos.

        Parâmetros:
            prefs: preferências do usuário.
            camadas_: as camadas da página.
        """
        self.prefs = prefs
        self.camadas = camadas_
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
            self._modelo()
            grupo('Contexto aeronáutico')
            self._trafego()
            self._aerodromos()
            self._espaco()

    # ===== OBSERVAÇÃO ==============================================================

    def _satelite(self) -> None:
        """Cartão Satélite: tipo de imagem e opacidade."""
        prefs = self.prefs
        camada = self.camadas.satelite
        with cartao('Satélite', 'satellite_alt', prefs, 'satelite_ligado', camada.aplicar):
            ui.select(redemet.SATELITES, label='Imagem', on_change=camada.aplicar) \
                .bind_value(prefs, 'satelite_tipo').props('dense').classes('w-full')
            with ui.row().classes('w-full items-center no-wrap'):
                ui.label('Opacidade').classes('text-caption')
                ui.slider(min=0.2, max=1, step=0.1).bind_value(prefs, 'satelite_opacidade') \
                    .on('change', camada.mudar_opacidade).classes('col')
            camada.rotulo_info = ui.label().classes('text-caption text-grey')

    def _radar(self) -> None:
        """Cartão Radar: fonte e produto, limiar, estações, filtros por status e círculos."""
        prefs = self.prefs
        camada = self.camadas.radar
        with cartao('Radar', 'radar', prefs, 'radar_ligado', camada.aplicar):
            self._radar_fonte_e_produto()
            self._radar_limiar()
            self._radar_estacoes()
            self._radar_filtros_status()
            camada.ao_mudar_status = self._pintar_radares      # a camada avisa quando chega o status
            self._pintar_radares()
            ui.switch('Círculos de alcance', on_change=camada.desenhar_circulos) \
                .bind_value(prefs, 'radar_circulos').props('dense')

    def _radar_fonte_e_produto(self) -> None:
        """API ou GeoServer, e o produto de cada fonte (só o da fonte escolhida aparece)."""
        prefs = self.prefs
        camada = self.camadas.radar
        ui.toggle(FONTES, on_change=camada.mudar_produto) \
            .bind_value(prefs, 'radar_fonte').props('dense no-caps size=sm spread').classes('w-full')
        # Produto pela API...
        produtos_api = {chave: f'{rotulo} ({alcance_km} km)'
                        for chave, (rotulo, alcance_km) in redemet.PRODUTOS_RADAR.items()}
        ui.select(produtos_api, label='Produto', on_change=camada.mudar_produto) \
            .bind_value(prefs, 'radar_produto_api').props('dense').classes('w-full') \
            .bind_visibility_from(prefs, 'radar_fonte', value='api')
        # ...ou pelo GeoServer (outros nomes; ver DECISOES.md, 2.1)
        with ui.column().classes('w-full gap-0').bind_visibility_from(prefs, 'radar_fonte', value='geoserver'):
            with ui.row().classes('w-full no-wrap'):
                ui.select(radar.PRODUTOS, label='Produto', on_change=camada.mudar_produto) \
                    .bind_value(prefs, 'radar_produto').props('dense').classes('col')
                ui.select(radar.ALCANCES, label='Alcance', on_change=camada.mudar_produto) \
                    .bind_value(prefs, 'radar_alcance').props('dense').classes('col')
            ui.select(radar.ALTURAS_CAPPI, label='Altura do CAPPI', on_change=camada.mudar_produto) \
                .bind_value(prefs, 'radar_altura').props('dense') \
                .bind_visibility_from(prefs, 'radar_produto', value='cappi').classes('w-full')

    def _radar_limiar(self) -> None:
        """Régua do limiar de dBZ (aplicado ao soltar a régua)."""
        camada = self.camadas.radar
        with ui.row().classes('w-full items-center no-wrap q-mt-xs'):
            ui.label('Limiar').classes('text-caption')
            limiar = ui.slider(min=0, max=75, step=5).bind_value(self.prefs, 'dbz_min').classes('col')
            ui.label().bind_text_from(self.prefs, 'dbz_min', _texto_limiar) \
                .classes('text-caption').style('min-width: 56px')
        limiar.on('change', lambda: camada.mudar_limiar())  # 'change' = ao soltar o controle, não a cada passo

    def _radar_estacoes(self) -> None:
        """Botões "todas"/"nenhuma" e um botão por estação de radar (cores em `_pintar_radares`)."""
        with ui.row().classes('w-full items-center q-mt-xs'):
            ui.label('Estações').classes('text-caption col')
            ui.button('todas', on_click=lambda: self._todos_radares(True)).props('flat dense size=sm')
            ui.button('nenhuma', on_click=lambda: self._todos_radares(False)).props('flat dense size=sm')
        with ui.grid(columns=5).classes('gap-1 w-full'):
            for estacao in radar.RADARES:
                with ui.button(estacao.sigla, on_click=lambda codigo=estacao.codigo: self._alternar_radar(codigo)) \
                        .props('dense size=sm unelevated') as botao:
                    self._dicas_radar[estacao.codigo] = ui.tooltip(f'{estacao.nome}/{estacao.uf}')
                self._botoes_radar[estacao.codigo] = botao

    def _radar_filtros_status(self) -> None:
        """Filtros por status: clicar (des)seleciona os radares daquele status."""
        camada = self.camadas.radar
        with ui.column().classes('gap-0 w-full'):
            for status, (cor, texto) in radar.STATUS.items():
                with ui.button(on_click=lambda status=status: camada.alternar_filtro(status)) \
                        .props('flat dense no-caps size=sm align=left').classes('w-full') as botao, \
                        ui.row().classes('items-center no-wrap gap-2'):
                    ui.icon('circle', size='xs').style(f'color: {cor}')
                    self._textos_status[status] = ui.label(texto).classes('text-caption')
                self._filtros_status[status] = botao

    def _pintar_radares(self) -> None:
        """Pinta os botões das estações e os filtros por status (chamado também pela camada)."""
        self._pintar_estacoes()
        self._pintar_filtros_status()

    def _pintar_estacoes(self) -> None:
        """Cor do botão = selecionado ou não; barra embaixo = status da última imagem."""
        camada = self.camadas.radar
        for codigo, botao in self._botoes_radar.items():
            botao.props(f"color={'primary' if codigo in self.prefs['radares'] else 'grey-8'}")
            estacao = radar.POR_CODIGO[codigo]
            nome = f'{estacao.nome}/{estacao.uf}'
            if not camada.status_ok:
                botao.style('border-bottom: 3px solid transparent')
                self._dicas_radar[codigo].set_text(nome)
                continue
            cor, _ = radar.STATUS[camada.status_de(codigo)]
            botao.style(f'border-bottom: 3px solid {cor}')
            quando = camada.ultimas.get(codigo)
            ultima = f'{quando:%d/%m %H:%MZ}' if quando else 'sem dados'
            self._dicas_radar[codigo].set_text(f'{nome} · última imagem: {ultima}')

    def _pintar_filtros_status(self) -> None:
        """Texto de cada filtro com a % de estações naquele status; apagado se desligado."""
        camada = self.camadas.radar
        contagem = dict.fromkeys(radar.STATUS, 0)
        for estacao in radar.RADARES:
            contagem[camada.status_de(estacao.codigo)] += 1
        for status, botao in self._filtros_status.items():
            _, texto = radar.STATUS[status]
            porcentagem = round(100 * contagem[status] / len(radar.RADARES))
            self._textos_status[status].set_text(f'{texto} · {porcentagem}%')
            botao.style(f'opacity: {1 if self.prefs["radar_status_filtro"][status] else 0.4}')
            botao.set_visibility(camada.status_ok)

    def _alternar_radar(self, codigo: str) -> None:
        """Clique numa estação: entra ou sai da seleção.

        Parâmetros:
            codigo: o código da estação (radar.RADARES).
        """
        escolhidos = set(self.prefs['radares']) ^ {codigo}
        # na ordem fixa de radar.RADARES, não na ordem dos cliques
        self.prefs['radares'] = [estacao.codigo for estacao in radar.RADARES if estacao.codigo in escolhidos]
        self._pintar_radares()
        self.camadas.radar.aplicar()

    def _todos_radares(self, sim: bool) -> None:
        """Botões "todas"/"nenhuma": seleciona ou tira todas as estações (e os filtros acompanham).

        Parâmetros:
            sim: True para todas, False para nenhuma.
        """
        self.prefs['radares'] = [estacao.codigo for estacao in radar.RADARES] if sim else []
        for status in self.prefs['radar_status_filtro']:
            self.prefs['radar_status_filtro'][status] = sim
        self._pintar_radares()
        self.camadas.radar.aplicar()

    def _raios(self) -> None:
        """Cartão Raios: fonte; no GeoServer, classificação, raio do círculo e janela de tempo."""
        prefs = self.prefs
        camada = self.camadas.raios
        with cartao('Raios', 'bolt', prefs, 'raios_ligado', camada.aplicar):
            ui.toggle(FONTES, on_change=camada.aplicar) \
                .bind_value(prefs, 'raios_fonte').props('dense no-caps size=sm spread').classes('w-full')
            ui.label('Células de 0,1° com descargas recentes. A cor diz há quanto tempo a célula '
                     'está ativa: vermelho é convecção nova. Para voltar no tempo, use a animação.') \
                .classes('text-caption text-grey').bind_visibility_from(prefs, 'raios_fonte', value='api')
            with ui.column().classes('w-full gap-1').bind_visibility_from(prefs, 'raios_fonte', value='geoserver'):
                self._raios_geoserver()

    def _raios_geoserver(self) -> None:
        """Os ajustes dos raios pelo GeoServer: cores, raio do círculo e a janela de tempo."""
        prefs = self.prefs
        camada = self.camadas.raios
        ui.select(raios.CLASSIFICACOES, label='Cores por', on_change=camada.atualizar) \
            .bind_value(prefs, 'raios_classificacao').props('dense').classes('w-full')
        with ui.row().classes('w-full items-center no-wrap'):
            ui.label('Raio do círculo').classes('text-caption')
            raio = ui.slider(min=1, max=10, step=1).bind_value(prefs, 'raios_raio_km').classes('col')
            ui.label().bind_text_from(prefs, 'raios_raio_km', _texto_km).classes('text-caption')
        raio.on('change', lambda: camada.atualizar())
        with ui.row().classes('w-full items-center no-wrap'):
            ui.button(icon='chevron_left', on_click=lambda: camada.andar(-raios.PASSO_MIN)) \
                .props('flat dense').tooltip(f'{raios.PASSO_MIN} min antes')
            camada.rotulo_janela = ui.label().classes('text-caption col text-center')
            ui.button(icon='chevron_right', on_click=lambda: camada.andar(raios.PASSO_MIN)) \
                .props('flat dense').tooltip(f'{raios.PASSO_MIN} min depois')
            ui.button('agora', on_click=camada.ao_vivo).props('flat dense size=sm')

    # ===== PREVISÃO ================================================================

    def _severo(self) -> None:
        """Cartão Tempo severo: escolhas em cascata, validade e estilo (cor, opacidade ou raio)."""
        prefs = self.prefs
        camada = self.camadas.severo
        with cartao('Tempo severo', 'thunderstorm', prefs, 'severo_ligado', camada.ligar):
            self._severo_escolhas()
            self._severo_estilo()
            camada.rotulo_situacao = ui.label().classes('text-caption')

    def _severo_escolhas(self) -> None:
        """Escolhas em cascata: cada select depende do anterior (CamadaSevero._cascata)."""
        camada = self.camadas.severo
        for linha in LINHAS_SEVERO:
            with ui.row().classes('w-full no-wrap'):
                for nivel in linha:
                    camada.seletores[nivel] = ui.select(
                        {}, label=ROTULOS_SEVERO[nivel], on_change=lambda nivel=nivel: camada.mudou(nivel)) \
                        .props('dense').classes('col')
        with ui.row().classes('w-full items-center no-wrap'):
            ui.button(icon='chevron_left', on_click=lambda: camada.andar(-1)).props('flat dense')
            camada.seletores['validade'] = ui.select(
                {}, label='Validade', on_change=lambda: camada.mudou('validade')) \
                .props('dense').classes('col')
            ui.button(icon='chevron_right', on_click=lambda: camada.andar(1)).props('flat dense')

    def _severo_estilo(self) -> None:
        """Cor; opacidade para áreas e raio para pontos (desenhados como círculos)."""
        camada = self.camadas.severo
        estilo = self.prefs['severo_estilo']
        ui.color_input('Cor', on_change=camada.mudar_estilo) \
            .bind_value(estilo, 'cor').props('dense').classes('w-full')
        with ui.row().classes('w-full items-center no-wrap') \
                .bind_visibility_from(camada.escolha, 'tipo', value='area'):
            ui.label('Opacidade').classes('text-caption')
            ui.slider(min=0.1, max=1, step=0.1).bind_value(estilo, 'opacidade') \
                .on('change', camada.mudar_estilo).classes('col')
        with ui.row().classes('w-full items-center no-wrap') \
                .bind_visibility_from(camada.escolha, 'tipo', value='ponto'):
            ui.label('Raio').classes('text-caption')
            ui.slider(min=1, max=10, step=1).bind_value(estilo, 'raio_km') \
                .on('change', camada.mudar_estilo).classes('col')
            ui.label().bind_text_from(estilo, 'raio_km', _texto_km).classes('text-caption')

    def _wifs(self) -> None:
        """Cartão WIFS: nível de voo e uma chave por variável."""
        prefs = self.prefs
        camada = self.camadas.wifs
        with cartao('WIFS em altitude', 'cloud'):      # sem chave geral: cada variável liga sozinha
            ui.select(wifs.NIVEIS, label='Nível de voo', on_change=camada.trocar_nivel) \
                .bind_value(prefs, 'wifs_fl').props('dense').classes('w-full')
            for variavel in wifs.CAMADAS:
                with ui.row().classes('w-full items-center no-wrap'):
                    ui.icon(variavel.icone, size='xs').classes('text-grey')
                    ui.switch(variavel.titulo, on_change=lambda chave=variavel.chave: camada.aplicar(chave)) \
                        .bind_value(prefs['wifs'], variavel.chave).props('dense')

    def _vento(self) -> None:
        """Cartão Vento: nível de voo e horário do arquivo."""
        prefs = self.prefs
        camada = self.camadas.vento
        with cartao('Vento em altitude', 'air', prefs, 'vento_ligado', camada.aplicar):
            niveis = {nivel_voo: f'FL{nivel_voo:03d}' for nivel_voo in config.VENTO_FL_PARA_PA}
            with ui.row().classes('w-full no-wrap'):
                ui.select(niveis, label='Nível', on_change=camada.trocar_nivel) \
                    .bind_value(prefs, 'vento_fl').props('dense').classes('col')
                camada.seletor_horario = ui.select(camada.opcoes_horario(), label='Horário', on_change=camada.aplicar) \
                    .bind_value(prefs, 'vento_horario').props('dense').classes('col')
            camada.rotulo_aviso = ui.label()

    def _modelo(self) -> None:
        """Cartão Modelo WRF: campo, nível de voo (só nos campos por nível), horário previsto e opacidade."""
        prefs = self.prefs
        camada = self.camadas.modelo
        campos = {campo.chave: campo.titulo for campo in modelo.CAMPOS}
        niveis = {nivel_voo: f'FL{nivel_voo:03d} · {pa // 100} hPa' for nivel_voo, pa in config.VENTO_FL_PARA_PA.items()}
        with cartao('Modelo WRF', 'model_training', prefs, 'modelo_ligado', camada.aplicar):
            ui.select(campos, label='Campo', on_change=camada.desenhar) \
                .bind_value(prefs, 'modelo_campo').props('dense').classes('w-full')
            ui.select(niveis, label='Nível', on_change=camada.desenhar) \
                .bind_value(prefs, 'modelo_fl').props('dense').classes('w-full') \
                .bind_visibility_from(prefs, 'modelo_campo', backward=lambda chave: modelo.POR_CHAVE[chave].por_nivel)
            with ui.row().classes('w-full items-center no-wrap'):
                ui.button(icon='chevron_left', on_click=lambda: camada.andar(-1)) \
                    .props('flat dense').tooltip('1 h antes').mark('modelo-antes')
                camada.seletor_horario = ui.select(
                    camada.opcoes_horario(), label='Horário previsto',
                    on_change=lambda evento: camada.escolher_horario(evento.value)) \
                    .props('dense').classes('col')
                ui.button(icon='chevron_right', on_click=lambda: camada.andar(1)) \
                    .props('flat dense').tooltip('1 h depois').mark('modelo-depois')
            with ui.row().classes('w-full items-center no-wrap'):
                ui.label('Opacidade').classes('text-caption')
                ui.slider(min=0.2, max=1, step=0.1).bind_value(prefs, 'modelo_opacidade') \
                    .on('change', camada.mudar_opacidade).classes('col')
            camada.rotulo_info = ui.label().classes('text-caption text-grey')

    # ===== CONTEXTO AERONÁUTICO ====================================================

    def _trafego(self) -> None:
        """Cartão Tráfego: rótulos, filtro de indicativos, faixa de nível e as rotas mostradas."""
        prefs = self.prefs
        aeronaves = self.camadas.aeronaves
        rotas = self.camadas.rotas
        with cartao('Tráfego', 'flight', prefs, 'aeronaves_ligado', lambda: aeronaves.enviar(forcar=True)):
            ui.switch('Rótulos (indicativo e nível)', on_change=aeronaves.mudar_rotulos) \
                .bind_value(prefs, 'aeronaves_rotulos').props('dense')
            ui.input('Filtrar indicativos (ex.: GLO, TAM)', on_change=lambda: aeronaves.enviar(forcar=True)) \
                .bind_value(prefs, 'aeronaves_prefixos').props('dense clearable').classes('w-full')
            ui.label('Faixa de nível (FL)').classes('text-caption')
            ui.range(min=0, max=600, step=10, on_change=lambda: aeronaves.enviar(forcar=True)) \
                .bind_value(prefs, 'aeronaves_fl').props('label')
            aeronaves.rotulo_contador = ui.label().classes('text-caption')
            ui.label('Clique numa aeronave e em "Ver / ocultar rota" para ver a trajetória.') \
                .classes('text-caption text-grey')
            with ui.row().classes('w-full items-center no-wrap'):
                rotas.rotulo_lista = ui.label().classes('text-caption col')
                rotas.botao_limpar = ui.button('limpar', on_click=rotas.limpar).props('flat dense size=sm')
            rotas.mostrar_lista()

    def _aerodromos(self) -> None:
        """Cartão Aeródromos: só a chave e a situação da consulta."""
        camada = self.camadas.aerodromos
        with cartao('Aeródromos', 'local_airport', self.prefs, 'aerodromos_ligado', camada.enviar):
            camada.rotulo_situacao = ui.label().classes('text-caption')

    def _espaco(self) -> None:
        """Cartão Espaço aéreo: uma chave por arquivo, com o estilo dos que aceitam estilo."""
        with cartao('Espaço aéreo', 'route'):
            for definicao in espaco.CAMADAS:
                self._espaco_item(definicao)

    def _espaco_item(self, definicao) -> None:
        """Chave, rótulo da fonte e (se estilizável) cor, traço e opacidade de um arquivo.

        Parâmetros:
            definicao: um item de espaco.CAMADAS (chave, título, se é estilizável).
        """
        prefs = self.prefs
        camada = self.camadas.espaco
        chave = definicao.chave
        ui.switch(definicao.titulo, on_change=lambda: camada.aplicar(chave)) \
            .bind_value(prefs['espaco'], chave).props('dense')
        camada.rotulos_fonte[chave] = ui.label().bind_visibility_from(prefs['espaco'], chave)  # "fonte: arquivo..."
        if not definicao.estilizavel:
            return
        estilo = prefs['estilos'][chave]

        def mudar() -> None:
            camada.mudar_estilo(chave)

        with ui.column().classes('w-full q-pl-lg gap-1').bind_visibility_from(prefs['espaco'], chave):
            ui.select(espaco.CORES, label='Cor', on_change=mudar) \
                .bind_value(estilo, 'cor').props('dense').classes('w-full')
            ui.toggle(espaco.TRACOS, on_change=mudar).bind_value(estilo, 'traco').props('dense size=sm')
            with ui.row().classes('w-full items-center no-wrap'):
                ui.label('Opacidade').classes('text-caption')
                ui.slider(min=0, max=1, step=0.05).bind_value(estilo, 'opacidade') \
                    .on('change', mudar).classes('col')
