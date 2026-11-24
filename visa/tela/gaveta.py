"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

A gaveta da direita, com duas abas:
  FAB.ia    (impacto) aeródromos, TMAs e tráfego afetados agora (calculado no servidor, impacto.py)
            e os conflitos previstos de aeronaves com núcleos de tempestade (previsao.py)
  Sonda     tudo sobre um ponto do mapa (sonda.py)

Gaveta só abre, fecha e troca de aba; cada painel desenha o seu conteúdo.
Criados por interface.pagina(); o Cabecalho chama `PainelImpacto.alternar` e `PainelSonda.alternar_modo`.
"""
from nicegui import background_tasks, ui

import aeronaves
import camadas
import impacto
import modelo
import previsao
import radar
import redemet
import severo
import sonda
import vento
from tela.mapa import AreaDoMapa
from tela.pecas import secao

IMPACTO, SONDA = 'impacto', 'sonda'

# Categorias de turbulência e gelo do modelo, curtas para caber na tabela da sonda
ABREVIA_CATEGORIA = {'leve': 'leve', 'moderada': 'mod', 'moderado': 'mod', 'forte': 'forte'}

# Círculo amarelo no ponto sondado (opções do circleMarker do Leaflet)
ESTILO_MARCA_SONDA = {'radius': 8, 'color': '#fbbf24', 'weight': 2, 'fillOpacity': 0.15, 'interactive': False}


def _texto_dbz(dbz: int | None) -> str:
    """O dBZ como texto ("sem eco" quando não há eco).

    Parâmetros:
        dbz: refletividade em dBZ, ou None (sem eco).
    """
    return 'sem eco' if dbz is None else f'{dbz} dBZ'


def _texto_fl(nivel_voo: int | None) -> str:
    """O nível de voo como "FL350" (ou "—" quando desconhecido).

    Parâmetros:
        nivel_voo: nível de voo em centenas de pés, ou None.
    """
    return '—' if nivel_voo is None else f'FL{nivel_voo:03d}'


class Gaveta:
    """O painel da direita (escondido até ser pedido) e as suas abas.

    Atributos:
        gaveta: o ui.right_drawer.
        paineis: os ui.tab_panels (o valor é a aba mostrada).
        aba_impacto, aba_sonda: os contêineres que cada painel preenche.
    """

    def __init__(self) -> None:
        """Cria a gaveta, fechada, com as abas FAB.ia e Sonda vazias."""
        self.gaveta = ui.right_drawer(value=False).props('width=350 bordered').classes('q-pa-sm painel')
        with self.gaveta:
            with ui.tabs().props('dense align=justify narrow-indicator').classes('w-full text-grey-5') as abas:
                ui.tab(IMPACTO, label='FAB.ia', icon='warning')
                ui.tab(SONDA, label='Sonda', icon='ads_click')
            with ui.tab_panels(abas, value=IMPACTO).classes('w-full bg-transparent') as self.paineis:
                self.aba_impacto = ui.tab_panel(IMPACTO).classes('q-pa-none q-pt-sm')
                self.aba_sonda = ui.tab_panel(SONDA).classes('q-pa-none q-pt-sm')

    def mostrando(self, aba: str) -> bool:
        """A gaveta está aberta nesta aba?

        Parâmetros:
            aba: IMPACTO ou SONDA.
        """
        return bool(self.gaveta.value) and self.paineis.value == aba

    def mostrar(self, aba: str) -> None:
        """Abre a gaveta na aba pedida.

        Parâmetros:
            aba: IMPACTO ou SONDA.
        """
        self.paineis.set_value(aba)
        self.gaveta.show()

    def esconder(self) -> None:
        """Fecha a gaveta."""
        self.gaveta.hide()


# ---------------------------------------------------------------------------
# Sonda
# ---------------------------------------------------------------------------

class PainelSonda:
    """Clique direito no mapa (sempre) ou clique com a sonda ligada: mostra o ponto na aba Sonda.

    Atributos:
        ligada: o modo "clique esquerdo sonda" está ligado.
        marca: o círculo amarelo no ponto sondado (None se não há ponto).
        conteudo: a coluna redesenhada a cada sondagem.
        dica: o texto inicial, escondido depois da primeira sondagem.
    """

    def __init__(self, gaveta: Gaveta, area: AreaDoMapa, prefs: dict, camadas_: camadas.CamadasDaPagina) -> None:
        """Preenche a aba Sonda e escuta os cliques no mapa.

        Parâmetros:
            gaveta: a gaveta da direita.
            area: a área do mapa (o mapa e o contêiner que recebe o cursor em cruz).
            prefs: preferências do usuário.
            camadas_: as camadas da página (a sonda usa a de tempo severo e a do modelo).
        """
        self.gaveta = gaveta
        self.area = area
        self.mapa = area.mapa
        self.prefs = prefs
        self.severo = camadas_.severo
        self.modelo = camadas_.modelo
        self.ligada = False
        self.marca = None
        self._pedido = 0             # o último clique vence (ver DECISOES.md, 5.4)
        with gaveta.aba_sonda:
            self.conteudo = ui.column().classes('w-full gap-1')
            self.dica = ui.label('Clique direito no mapa (ou ligue "Sonda" no cabeçalho e clique) para ver '
                                 'tudo sobre um ponto.').classes('legenda-texto')
        self.mapa.on('map-click', self._no_clique)
        self.mapa.on('map-contextmenu', self._no_clique_direito)

    def alternar_modo(self) -> bool:
        """Liga/desliga o modo em que o clique esquerdo sonda.

        Retorna:
            True se o modo ficou ligado.
        """
        self.ligada = not self.ligada
        if self.ligada:
            self.area.area.classes(add='sonda-ligada')       # cursor em cruz (interface.css)
        else:
            self.area.area.classes(remove='sonda-ligada')
        return self.ligada

    def _no_clique(self, evento) -> None:
        """Clique esquerdo no mapa: sonda só se o modo estiver ligado."""
        if self.ligada:
            self._no_clique_direito(evento)

    def _no_clique_direito(self, evento) -> None:
        """Clique direito no mapa: sonda o ponto clicado."""
        ponto = evento.args['latlng']
        self.ir(ponto['lat'], ponto['lng'])

    def ir(self, lat: float, lon: float) -> None:
        """Sonda o ponto em segundo plano (a consulta pode levar alguns segundos).

        Parâmetros:
            lat, lon: o ponto, em graus.
        """
        background_tasks.create(self.sondar(lat, lon), name='sonda')

    def fechar(self) -> None:
        """Botão ✕: tira a marca do mapa e fecha a gaveta."""
        self._pedido += 1                # uma resposta ainda a caminho não reabre o painel
        if self.marca:
            self.mapa.remove_layer(self.marca)
            self.marca = None
        self.gaveta.esconder()

    async def sondar(self, lat: float, lon: float) -> None:
        """Marca o ponto, mostra "carregando", consulta tudo sobre ele e desenha o resultado.

        Fluxo:
            1. numera o pedido (o último clique vence);
            2. marca o ponto no mapa e abre a aba com o título e um spinner;
            3. espera sonda.sondar;
            4. desenha, se a pessoa não clicou em outro ponto nesse meio-tempo.

        Parâmetros:
            lat, lon: o ponto, em graus.
        """
        self._pedido += 1
        pedido = self._pedido
        self._marcar_ponto(lat, lon)
        self._mostrar_carregando(lat, lon)
        camada_severo = self.severo
        com_severo = self.prefs['severo_ligado'] and camada_severo.feicoes
        camada_modelo = self.modelo
        com_modelo = self.prefs['modelo_ligado']      # ligada: o perfil do horário que está no mapa
        resultado = await sonda.sondar(
            lat, lon, self.prefs['radar_produto_api'],
            camada_severo.feicoes if com_severo else None, camada_severo.tipo,
            self.prefs['severo_estilo']['raio_km'], camada_severo.escolha['validade'],
            camada_modelo.rodada if com_modelo else None, camada_modelo.horario if com_modelo else None)
        if pedido == self._pedido:       # a pessoa não clicou em outro ponto nesse meio-tempo
            self._desenhar(resultado)

    def _marcar_ponto(self, lat: float, lon: float) -> None:
        """Troca o círculo amarelo do ponto sondado anterior pelo do novo ponto."""
        if self.marca:
            self.mapa.remove_layer(self.marca)
        with self.mapa:
            self.marca = self.mapa.generic_layer(name='circleMarker', args=[[lat, lon], ESTILO_MARCA_SONDA])

    def _mostrar_carregando(self, lat: float, lon: float) -> None:
        """Abre a aba Sonda com o título do ponto e um spinner enquanto a consulta não volta."""
        self.conteudo.clear()
        with self.conteudo:
            self._titulo(sonda.Resultado(lat, lon).titulo)
            ui.spinner(size='lg').classes('self-center q-mt-lg')
        self.dica.set_visibility(False)
        self.gaveta.mostrar(SONDA)

    def _titulo(self, titulo: str) -> None:
        """O título da aba: "Sonda", o ponto (coordenadas) e o botão de fechar.

        Parâmetros:
            titulo: o ponto, já formatado (sonda.Resultado.titulo).
        """
        with ui.row().classes('w-full items-center no-wrap'):
            ui.icon('ads_click', size='sm').classes('text-amber')
            with ui.column().classes('col gap-0'):
                ui.label('Sonda').classes('text-subtitle2 text-weight-bold')
                ui.label(titulo).classes('text-caption text-grey')
            ui.button(icon='close', on_click=self.fechar).props('flat dense round size=sm color=grey-5')

    def _desenhar(self, resultado: sonda.Resultado) -> None:
        """Desenha o resultado da sonda, uma seção por fonte.

        Parâmetros:
            resultado: o que sonda.sondar encontrou no ponto.
        """
        self.conteudo.clear()
        with self.conteudo:
            self._titulo(resultado.titulo)
            self._radar(resultado)
            self._raios(resultado)
            self._severo(resultado)
            self._vento(resultado)
            self._modelo(resultado)
            self._aerodromo(resultado)
            ui.label('WIFS no ponto (turbulência, gelo) depende da VPN: ainda não disponível.') \
                .classes('legenda-texto q-mt-md')

    @staticmethod
    def _radar(resultado: sonda.Resultado) -> None:
        """Seção Radar: o dBZ agora no ponto e o histórico quadro a quadro."""
        secao('Radar', 'radar')
        if resultado.radar_erro:
            ui.label(resultado.radar_erro).classes('legenda-texto')
            return
        atual = resultado.radar_agora
        if not atual:
            return
        with ui.row().classes('items-center gap-2'):
            ui.element('span').classes('sonda-cor').style(f'background: {radar.cor_dbz(atual.dbz)}')
            ui.label(_texto_dbz(atual.dbz)).classes('text-body2 text-weight-bold')
            ui.label(f'{atual.estacao} · {atual.quando:%H:%MZ}').classes('legenda-texto')
        PainelSonda._historico_radar(resultado.radar_historico)

    @staticmethod
    def _historico_radar(historico: list) -> None:
        """Uma barrinha por quadro, altura e cor pelo dBZ, com o horário do primeiro e do último.

        Parâmetros:
            historico: pares (quando, dBZ ou None), do mais antigo ao mais novo.
        """
        with ui.row().classes('sonda-barras items-end no-wrap gap-0'):
            for quando, dbz in historico:
                altura_px = 3 if dbz is None else 6 + dbz * 0.6
                with ui.element('div').classes('col').style(
                        f'height: {altura_px:.0f}px; background: {radar.cor_dbz(dbz)}; margin: 0 1px'):
                    ui.tooltip(f'{quando:%H:%MZ}: {_texto_dbz(dbz)}')
        if not historico:
            return
        primeiro_quando = historico[0][0]
        ultimo_quando = historico[-1][0]
        with ui.row().classes('w-full justify-between'):
            ui.label(f'{primeiro_quando:%H:%MZ}').classes('legenda-texto')
            ui.label('dBZ no ponto, quadro a quadro').classes('legenda-texto')
            ui.label(f'{ultimo_quando:%H:%MZ}').classes('legenda-texto')

    @staticmethod
    def _raios(resultado: sonda.Resultado) -> None:
        """Seção Raios: células com descargas por distância, a mais próxima e as novas."""
        secao('Raios', 'bolt')
        if resultado.raios_erro:
            ui.label(resultado.raios_erro).classes('legenda-texto')
            return
        raios = resultado.raios
        if not raios:
            return
        contagem = raios.contagem
        por_distancia = [f'{contagem[distancia_km]} até {distancia_km} km'
                         for distancia_km in sonda.RAIOS_DISTANCIAS_KM]
        ui.label(' · '.join(por_distancia)).classes('text-body2')
        ui.label(f'células de 0,1° com descargas · quadro de {resultado.raios_quando:%H:%MZ}') \
            .classes('legenda-texto')
        if raios.mais_perto:
            distancia_km, minutos = raios.mais_perto
            ui.label(f'mais próxima a {distancia_km:.0f} km, ativa há {minutos} min').classes('legenda-texto')
        if raios.novas:
            ui.label(f'{raios.novas} célula(s) nova(s) (< {redemet.CELULA_NOVA_MIN} min) até '
                     f'{sonda.RAIO_CELULAS_NOVAS_KM} km: convecção começando ou crescendo') \
                .classes('text-caption text-red-4')

    @staticmethod
    def _severo(resultado: sonda.Resultado) -> None:
        """Seção Tempo severo: o ponto está dentro ou fora das áreas de risco previstas."""
        secao('Tempo severo previsto', 'thunderstorm')
        if resultado.severo is None:
            ui.label('camada desligada ou sem previsão carregada').classes('legenda-texto')
            return
        validade = ''
        if resultado.severo_validade:
            validade = f' (válida {severo.rotulo(resultado.severo_validade)})'
        if resultado.severo:
            ui.label('DENTRO de área de risco' + validade).classes('text-body2 text-red-4 text-weight-bold')
        else:
            ui.label('fora das áreas de risco' + validade).classes('legenda-texto')

    @staticmethod
    def _vento(resultado: sonda.Resultado) -> None:
        """Seção Vento: direção e velocidade por nível de voo, de cima para baixo."""
        secao('Vento em altitude', 'air')
        if not resultado.vento:
            ui.label('sem arquivos de vento').classes('legenda-texto')
            return
        texto, cor = vento.idade(resultado.vento_referencia)
        ui.label(f'referência {resultado.vento_referencia:%d/%m %H:%MZ} · {texto}').classes(f'text-caption text-{cor}')
        with ui.grid(columns=3).classes('w-full gap-x-2 gap-y-0 sonda-vento'):
            for nivel_voo, direcao, velocidade_kt in reversed(resultado.vento):     # de cima para baixo
                ui.label(f'FL{nivel_voo:03d}').classes('legenda-texto')
                with ui.row().classes('items-center gap-1 no-wrap'):
                    # a seta aponta para onde o vento vai (de onde vem + 180°)
                    ui.icon('navigation', size='14px').style(f'transform: rotate({direcao + 180}deg)')
                    ui.label(f'{direcao:03d}°').classes('legenda-texto')
                classes_velocidade = 'legenda-texto text-weight-bold' if velocidade_kt >= 50 else 'legenda-texto'
                ui.label(f'{velocidade_kt} kt').classes(classes_velocidade)

    @staticmethod
    def _modelo(resultado: sonda.Resultado) -> None:
        """Seção Perfil previsto (WRF): rodada e horário, isoterma de 0 °C, superfície e os níveis."""
        secao('Perfil previsto (WRF)', 'model_training')
        perfil = resultado.perfil
        if perfil is None:
            ui.label(resultado.perfil_erro or 'sem perfil').classes('legenda-texto')
            return
        ui.label(f'rodada {perfil.rodada.inicio:%d/%m %H}Z · '
                 f'{modelo.rotulo_horario(perfil.horario, perfil.rodada.inicio)} · '
                 f'grade de {perfil.resolucao_km:.0f} km').classes('legenda-texto')
        ui.label(f'Isoterma de 0 °C: {modelo.texto_isoterma(perfil.niveis)}').classes('text-body2')
        ui.label(PainelSonda._texto_superficie(perfil)).classes('legenda-texto')
        with ui.grid(columns=5).classes('w-full gap-x-2 gap-y-0 sonda-vento'):
            for titulo in ('nível', 'T / Td °C', 'UR', 'vento', 'turb / gelo'):
                ui.label(titulo).classes('legenda-texto text-grey-6')
            for nivel in reversed(perfil.niveis):          # de cima para baixo, como o vento
                ui.label(f'FL{nivel.fl:03d}').classes('legenda-texto').tooltip(f'{nivel.hpa} hPa')
                orvalho = '—' if nivel.orvalho is None else f'{nivel.orvalho:.0f}'
                ui.label(f'{nivel.temperatura:.0f} / {orvalho}').classes('legenda-texto')
                ui.label('—' if nivel.umidade is None else f'{nivel.umidade:.0f}%').classes('legenda-texto')
                if nivel.direcao is None:
                    ui.label('—').classes('legenda-texto')
                else:
                    ui.label(f'{nivel.direcao:03d}°/{nivel.velocidade_kt} kt').classes('legenda-texto')
                PainelSonda._riscos(nivel)
        ui.label(f'previsão do modelo, não observação · turbulência (Ellrod, de '
                 f'{modelo.TURBULENCIA_HPA_MAX} hPa para cima) e gelo (água super-resfriada): '
                 'experimentais, limiares não validados').classes('legenda-texto')

    @staticmethod
    def _riscos(nivel: modelo.NivelPerfil) -> None:
        """Célula 'turb / gelo' de um nível: as categorias, na cor da legenda do mapa, e os valores no tooltip."""
        partes = []
        detalhes = []
        for campo, valor in ((modelo.POR_CHAVE['turbulencia'], nivel.turbulencia),
                             (modelo.POR_CHAVE['gelo'], nivel.gelo)):
            nome = modelo.categoria(campo, valor)
            partes.append(ABREVIA_CATEGORIA.get(nome, '—'))
            if valor is not None:
                detalhes.append(f'{campo.titulo}: {valor:.2f} {campo.unidade}'.replace('.', ','))
        rotulo = ui.label(' / '.join(partes)).classes('legenda-texto')
        if detalhes:
            rotulo.tooltip(' · '.join(detalhes))

    @staticmethod
    def _texto_superficie(perfil: modelo.Perfil) -> str:
        """CAPE, refletividade simulada e chuva da última hora no ponto, numa linha."""
        partes = []
        if perfil.cape is not None:
            partes.append(f'CAPE {perfil.cape:.0f} J/kg')
        if perfil.refletividade is not None:
            partes.append(f'refletividade simulada {perfil.refletividade:.0f} dBZ')
        if perfil.chuva_1h is not None:
            partes.append(f'chuva {perfil.chuva_1h:.1f} mm na última hora'.replace('.', ','))
        return ' · '.join(partes) or 'sem valores de superfície'

    @staticmethod
    def _aerodromo(resultado: sonda.Resultado) -> None:
        """Seção Aeródromo: o mais próximo do ponto, a distância e o boletim."""
        secao('Aeródromo mais próximo', 'local_airport')
        if not resultado.aerodromo:
            ui.label('lista de aeródromos ainda não carregada (ligue a camada)').classes('legenda-texto')
            return
        aerodromo = resultado.aerodromo.aerodromo
        with ui.row().classes('items-center gap-2 no-wrap'):
            ui.element('span').classes('sonda-cor').style(f'background: {aerodromo.cor}; border-radius: 50%')
            ui.label(f'{aerodromo.icao} – {aerodromo.nome}').classes('text-body2')
        ui.label(f'a {resultado.aerodromo.km:.0f} km').classes('legenda-texto')
        if aerodromo.texto:
            ui.label(aerodromo.texto).classes('sonda-boletim')


# ---------------------------------------------------------------------------
# Impacto
# ---------------------------------------------------------------------------

class PainelImpacto:
    """Lista o que impacto.situacao calculou. Redesenha a cada 15 s, só se a aba estiver aberta.

    Ordem das seções: resumo → conflitos previstos (FAB.ia) → aeródromos → TMAs → tráfego.
    """

    MAX_AERODROMOS = 30
    MAX_AERONAVES = 60

    def __init__(self, gaveta: Gaveta, area: AreaDoMapa, prefs: dict,
                 camadas_: camadas.CamadasDaPagina, painel_sonda: PainelSonda) -> None:
        """Prepara a aba FAB.ia e o relógio de redesenho.

        Parâmetros:
            gaveta: a gaveta da direita.
            area: a área do mapa (para centrar no item clicado).
            prefs: preferências do usuário.
            camadas_: as camadas da página (tempo severo e linhas dos conflitos).
            painel_sonda: a sonda, aberta ao clicar num aeródromo ou TMA.
        """
        self.gaveta = gaveta
        self.mapa = area.mapa
        self.prefs = prefs
        self.severo = camadas_.severo
        self.sonda = painel_sonda
        self.linhas_conflitos = camadas_.conflitos
        with gaveta.aba_impacto:
            self.conteudo = ui.column().classes('w-full gap-1')
        ui.timer(previsao.REDESENHO_S, self.desenhar)

    def alternar(self) -> None:
        """Botão "FAB.ia" do cabeçalho: abre a aba (ou fecha, se já estava aberta nela)."""
        if self.gaveta.mostrando(IMPACTO):
            self.gaveta.esconder()
        else:
            self.gaveta.mostrar(IMPACTO)
            self.desenhar()

    def desenhar(self) -> None:
        """Redesenha a aba inteira (nada a fazer se ela estiver fechada)."""
        if not self.gaveta.mostrando(IMPACTO):
            return
        situacao = impacto.situacao
        self.conteudo.clear()
        with self.conteudo:
            if situacao.erro:
                ui.label(situacao.erro).classes('text-caption text-negative')
            if not situacao.versao:
                ui.label('calculando (a primeira vez leva uns 10 s)…').classes('legenda-texto')
                return
            self._resumo(situacao)
            self._conflitos()
            self._aerodromos(situacao)
            self._tmas(situacao)
            self._trafego()

    # --- Itens (aeródromos e TMAs) ----------------------------------------------------

    def _ir_para(self, item: impacto.Impacto) -> None:
        """Clique num item: centra o mapa nele e abre a sonda no ponto."""
        self.mapa.set_center((item.lat, item.lon))
        self.mapa.set_zoom(9 if item.tipo == 'aerodromo' else 8)
        self.sonda.ir(item.lat, item.lon)

    def _item(self, item: impacto.Impacto, extra: list[str]) -> None:
        """Um aeródromo ou TMA afetado: borda na cor do nível, nome e motivos. Clicável.

        Parâmetros:
            item: o aeródromo ou TMA.
            extra: motivos a mais, calculados nesta página (ex.: tempo severo previsto).
        """
        cor, _ = impacto.NIVEIS[item.nivel]
        with ui.element('div').classes('impacto-item w-full').style(f'border-left-color: {cor}') \
                .on('click', lambda item=item: self._ir_para(item)).mark(f'impacto-{item.ident}'):
            with ui.row().classes('items-baseline no-wrap gap-1'):
                ui.label(item.ident).classes('text-body2 text-weight-bold')
                ui.label(item.nome).classes('legenda-texto ellipsis')
            ui.label('; '.join([*item.motivos, *extra])).classes('impacto-motivos')

    @staticmethod
    def _resumo(situacao: impacto.Situacao) -> None:
        """Horário dos dados, aviso de fonte atrasada e a contagem por nível de impacto."""
        fontes, atrasadas = [], []
        for nome, chave, quando in (('radar', previsao.RADAR, situacao.radar_quando),
                                    ('raios', previsao.RAIOS, situacao.raios_quando)):
            if not quando:
                continue
            fontes.append(f'{nome} {quando:%H:%MZ}')
            if chave not in situacao.fontes:
                atrasadas.append(nome)
        ui.label(' · '.join(fontes) + ' · atualiza a cada minuto').classes('legenda-texto')
        if atrasadas:
            ui.label(f"{' e '.join(atrasadas)} atrasado(s): fora do tráfego em área de risco e do FAB.ia") \
                .classes('text-caption text-warning')
        afetados = situacao.aerodromos + situacao.tmas
        with ui.row().classes('gap-2 q-my-xs'):
            for nivel, (cor, texto) in impacto.NIVEIS.items():
                quantidade = sum(1 for item in afetados if item.nivel == nivel)
                with ui.row().classes('items-center gap-1 no-wrap'):
                    ui.icon('circle', size='10px').style(f'color: {cor}')
                    ui.label(f'{texto}: {quantidade}').classes('legenda-texto')

    def _aerodromos(self, situacao: impacto.Situacao) -> None:
        """Seção Aeródromos afetados (até MAX_AERODROMOS)."""
        secao(f'Aeródromos ({len(situacao.aerodromos)})', 'local_airport')
        if not situacao.aerodromos:
            ui.label('nenhum aeródromo afetado').classes('legenda-texto')
        for item in situacao.aerodromos[:self.MAX_AERODROMOS]:
            self._item(item, self._motivos_severo(item))

    def _motivos_severo(self, item: impacto.Impacto) -> list[str]:
        """O motivo extra "em área de tempo severo prevista", se for o caso.

        O tempo severo previsto é de cada usuário (a escolha de modelo e validade é dele):
        por isso entra aqui, como motivo extra, e não no cálculo comum do servidor.

        Retorna:
            Lista com o motivo, ou vazia (camada desligada ou item fora das áreas).
        """
        camada_severo = self.severo
        if not (self.prefs['severo_ligado'] and camada_severo.feicoes):
            return []
        raio_km = self.prefs['severo_estilo']['raio_km']
        if sonda.em_area_severa(camada_severo.feicoes, camada_severo.tipo, raio_km, item.lat, item.lon):
            return ['em área de tempo severo prevista']
        return []

    def _tmas(self, situacao: impacto.Situacao) -> None:
        """Seção TMAs afetadas."""
        secao(f'TMAs ({len(situacao.tmas)})', 'radio_button_checked')
        if not situacao.tmas:
            ui.label('nenhuma TMA afetada (ou sem o arquivo de TMAs)').classes('legenda-texto')
        for item in situacao.tmas:
            self._item(item, [])

    # --- Conflitos previstos (FAB.ia) -----------------------------------------------

    def _conflitos(self) -> None:
        """FAB.ia: aeronaves em rota que vão cruzar um núcleo nos próximos 30 min (previsao.py)."""
        secao('Conflitos previstos', 'route')
        ui.label(f'projeção em linha reta (rumo e velocidade atuais), até {previsao.HORIZONTE_MIN} min; '
                 f'só aeronaves em rota (FL{previsao.FL_MIN:03d}+); linhas tracejadas no mapa') \
            .classes('legenda-texto')
        # A chave fica fora da lista: refazer a lista ao mudá-la não apaga a própria chave
        # value= em vez de bind_value: o on_change já grava a preferência, e assim não há mais uma
        # ligação conferida a cada 0,1 s (DECISOES 5.2)
        ui.switch('mostrar confiança baixa', value=self.prefs['fabia_baixa'], on_change=self._mudou_baixa) \
            .props('dense').classes('legenda-texto')
        self.lista_conflitos = ui.column().classes('w-full gap-1')
        self._listar_conflitos()

    def _listar_conflitos(self) -> None:
        """Refaz a lista de conflitos: situação do FAB.ia, contagem, um item por conflito e o placar."""
        situacao = previsao.situacao
        self.lista_conflitos.clear()
        with self.lista_conflitos:
            if situacao.pausa:
                ui.label(f'FAB.ia parado: {situacao.pausa}').classes('text-caption text-warning')
                return
            if situacao.calculado is None:
                ui.label('aguardando o primeiro cálculo').classes('legenda-texto')
                return
            mostrados = previsao.para_a_tela(situacao.conflitos, self.prefs['fabia_baixa'])
            escondidos = len(situacao.conflitos) - len(mostrados)
            texto = f'{len(mostrados)} conflito(s)' if mostrados else 'nenhum conflito previsto'
            if escondidos:
                texto += f' ({escondidos} de confiança baixa escondido(s))'
            ui.label(texto).classes('legenda-texto')
            for conflito in mostrados[:previsao.MAX_CONFLITOS]:
                self._item_conflito(conflito)
            self._placar(situacao)

    def _item_conflito(self, conflito: previsao.Conflito) -> None:
        """Um conflito: indicativo, nível, minutos até o encontro e o núcleo. Clicável."""
        cor = previsao.COR_CONFIANCA[conflito.confianca]
        with ui.element('div').classes('impacto-item w-full').style(f'border-left-color: {cor}') \
                .on('click', lambda conflito=conflito: self._ir_para_conflito(conflito)):
            with ui.row().classes('items-baseline no-wrap gap-2'):
                ui.label(conflito.indicativo).classes('text-body2 text-weight-bold')
                ui.label(_texto_fl(conflito.fl)).classes('legenda-texto')
                ui.label(f'em ~{conflito.minutos} min').classes('text-caption').style(f'color: {cor}')
            ui.label(f'{conflito.nucleo.descricao()}; {previsao.TEXTO_CONFIANCA[conflito.confianca]}') \
                .classes('impacto-motivos')

    @staticmethod
    def _placar(situacao: previsao.Situacao) -> None:
        """Acertos e erros do FAB.ia desde que o servidor subiu (só se já houve algum alerta)."""
        placar = previsao.resumir(situacao.placar)
        if not (placar['alertas'] or placar['nao_previstos']):
            return
        ui.label(f"desde {situacao.desde:%d/%m %H:%MZ}: {placar['alertas']} alertas, "
                 f"{placar['acertos']} confirmados, "
                 f"{placar['falsos']} falsos, {placar['nao_previstos']} entradas não previstas") \
            .classes('legenda-texto q-mt-xs')

    def _mudou_baixa(self, evento) -> None:
        """Chave "mostrar confiança baixa": refaz a lista e as linhas do mapa na hora."""
        self.prefs['fabia_baixa'] = evento.value
        self._listar_conflitos()
        self.linhas_conflitos.desenhar()

    def _ir_para_conflito(self, conflito: previsao.Conflito) -> None:
        """Clique: mostra a aeronave e o ponto de encontro juntos."""
        lat_meio = (conflito.lat0 + conflito.lat) / 2
        lon_meio = (conflito.lon0 + conflito.lon) / 2
        self.mapa.set_center((lat_meio, lon_meio))
        self.mapa.set_zoom(8)

    # --- Tráfego em área de risco -----------------------------------------------------

    @staticmethod
    def _aeronaves_em_alerta() -> list[tuple[str, int | None, str]]:
        """As aeronaves que estão agora em área de risco (impacto.alerta_da_aeronave).

        Retorna:
            Tuplas (indicativo, nível de voo ou None, motivo), na ordem do dicionário de tráfego.
        """
        em_alerta = []
        for aeronave in aeronaves.trafego.aeronaves.values():
            motivo = impacto.alerta_da_aeronave(aeronave.lat, aeronave.lon)
            if motivo:
                em_alerta.append((aeronave.indicativo, aeronave.nivel_voo, motivo))
        return em_alerta

    def _trafego(self) -> None:
        """Seção Tráfego em área de risco: uma linha por aeronave, em ordem de indicativo."""
        em_alerta = self._aeronaves_em_alerta()
        secao(f'Tráfego em área de risco ({len(em_alerta)})', 'flight')
        if not em_alerta:
            ui.label('nenhuma aeronave em área de risco').classes('legenda-texto')
            return
        ui.label('em células com raios (ou vizinhas) ou eco ≥ '
                 f'{impacto.ECO_FORTE_DBZ} dBZ · em âmbar no mapa').classes('legenda-texto')
        em_ordem = sorted(em_alerta, key=lambda alerta: alerta[0])
        with ui.grid(columns=3).classes('w-full gap-x-2 gap-y-0'):
            for indicativo, nivel_voo, motivo in em_ordem[:self.MAX_AERONAVES]:
                ui.label(indicativo).classes('text-caption text-amber')
                ui.label(_texto_fl(nivel_voo)).classes('legenda-texto')
                ui.label(motivo).classes('legenda-texto')
