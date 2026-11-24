"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

BarraSaude: no canto direito do cabeçalho, a idade e a situação de cada
fonte de dados ligada.

As regras (o que é recente, atrasado, crítico) estão em saude.py; aqui só se
junta o que cada camada sabe e se desenha. Criada pelo Cabecalho (tela/cabecalho.py).
"""
import time
from datetime import UTC, datetime

from nicegui import ui

import aerodromos
import aeronaves
import camadas
import espaco
import radar
import saude


class BarraSaude:
    """Recalcula a cada INTERVALO_S e só redesenha se algum indicador mudou.

    Os indicadores são a "assinatura" deste relógio (DECISOES.md, 5.3).

    Atributos:
        prefs: preferências do usuário.
        camadas: as camadas da página (de onde vêm as idades e os erros).
        barra: a linha onde os indicadores são desenhados.
    """

    INTERVALO_S = 5

    def __init__(self, prefs: dict, camadas_: camadas.CamadasDaPagina) -> None:
        """Cria a linha vazia (é preenchida por `atualizar`).

        Parâmetros:
            prefs: preferências do usuário.
            camadas_: as camadas da página.
        """
        self.prefs = prefs
        self.camadas = camadas_
        self.barra = ui.row().classes('items-center no-wrap gap-1 saude')
        self._mostrados: list[saude.Indicador] = []

    def ligar_relogio(self) -> None:
        """Começa o relógio que chama `atualizar`.

        Fica fora do __init__ porque a barra é criada dentro do cabeçalho, e o relógio não.
        """
        ui.timer(self.INTERVALO_S, self.atualizar)

    def indicadores(self) -> list[saude.Indicador]:
        """Calcula um indicador por fonte de dados ligada, na ordem em que aparecem na barra.

        Retorna:
            A lista de indicadores (vazia se nenhuma fonte estiver ligada).
        """
        agora = datetime.now(UTC)
        candidatos = [
            self._satelite(agora),
            self._radar(agora),
            self._raios(agora),
            self._severo(agora),
            self._wifs(),
            self._vento(agora),
            self._modelo(agora),
            self._trafego(agora),
            self._aerodromos(),
            self._airac(agora),
        ]
        return [indicador for indicador in candidatos if indicador is not None]

    def atualizar(self) -> None:
        """Redesenha a barra, mas só se algum indicador mudou desde o último desenho."""
        lista = self.indicadores()
        if lista == self._mostrados:     # os Indicadores são dataclasses: comparam por valor
            return
        self._mostrados = lista
        self.barra.clear()
        with self.barra:
            for indicador in lista:
                with ui.element('div').classes('saude-item'):
                    ui.element('span').classes('saude-ponto').style(f'background: {indicador.cor}')
                    ui.label(indicador.fonte).classes('saude-fonte')
                    ui.label(indicador.texto).classes('saude-texto')
                    ui.tooltip(indicador.detalhe)

    # --- Um indicador por fonte (None quando a fonte está desligada) ------------------

    def _satelite(self, agora: datetime) -> saude.Indicador | None:
        """Idade da imagem de satélite mostrada."""
        if not self.prefs['satelite_ligado']:
            return None
        camada = self.camadas.satelite
        quando = camada.imagem.quando if camada.imagem else None
        return saude.satelite(quando, camada.erro, agora)

    def _radar(self, agora: datetime) -> saude.Indicador | None:
        """Idade das últimas imagens das estações de radar escolhidas."""
        if not self.prefs['radar_ligado']:
            return None
        camada = self.camadas.radar
        ultimas = [camada.ultimas[codigo] for codigo in self.prefs['radares'] if codigo in camada.ultimas]
        erro = camada.erro if camada.pela_api() else ''
        return saude.radar(ultimas, camada.status_ok, erro, agora)

    def _raios(self, agora: datetime) -> saude.Indicador | None:
        """Raios pela API (idade do quadro) ou pelo GeoServer (fim da janela mostrada)."""
        if not self.prefs['raios_ligado']:
            return None
        camada = self.camadas.raios
        if camada.pela_api():
            quando = camada.quadro.quando if camada.quadro else None
            return saude.raios_api(quando, camada.erro, agora)
        return saude.raios(radar.estado.ok, camada.fim)

    def _severo(self, agora: datetime) -> saude.Indicador | None:
        """Rodada e validade da previsão de tempo severo escolhida."""
        if not self.prefs['severo_ligado']:
            return None
        camada = self.camadas.severo
        escolha = camada.escolha
        return saude.severo(escolha['rodada'], escolha['validade'], camada.erro, agora)

    def _wifs(self) -> saude.Indicador | None:
        """WIFS: depende só do GeoServer estar no ar (uma entrada para todas as variáveis)."""
        if not any(self.prefs['wifs'].values()):
            return None
        nivel = f"FL{self.prefs['wifs_fl']:03d}"
        return saude.geoserver('WIFS', radar.estado.ok, nivel, 'Previsão vigente do WIFS')

    def _vento(self, agora: datetime) -> saude.Indicador | None:
        """Idade do arquivo de vento em altitude."""
        if not self.prefs['vento_ligado']:
            return None
        return saude.vento(self.camadas.vento.referencia, self.prefs['vento_fl'], agora)

    def _modelo(self, agora: datetime) -> saude.Indicador | None:
        """Idade da rodada do modelo WRF e o horário previsto no mapa."""
        if not self.prefs['modelo_ligado']:
            return None
        camada = self.camadas.modelo
        inicio = camada.rodada.inicio if camada.rodada else None
        ultimo = camada.horarios[-1] if camada.horarios else None
        return saude.modelo(inicio, ultimo, camada.horario, camada.erro, agora)

    def _trafego(self, agora: datetime) -> saude.Indicador | None:
        """Conexão com o tráfego aéreo (Pusher) e idade da última mensagem."""
        if not self.prefs['aeronaves_ligado']:
            return None
        trafego = aeronaves.trafego
        return saude.trafego(trafego.conectado, trafego.recebido_em, trafego.situacao, agora)

    def _aerodromos(self) -> saude.Indicador | None:
        """Idade da última consulta dos METAR dos aeródromos."""
        if not self.prefs['aerodromos_ligado']:
            return None
        situacao = aerodromos.situacao
        idade_s = None
        if situacao.consultado_em:
            idade_s = time.monotonic() - situacao.consultado_em
        return saude.aerodromos(idade_s, situacao.erro)

    def _airac(self, agora: datetime) -> saude.Indicador | None:
        """Validade (ciclo AIRAC) dos arquivos de espaço aéreo ligados."""
        ligadas = [chave for chave, ligada in self.prefs['espaco'].items() if ligada]
        if not ligadas:
            return None
        validades = {}
        for chave in ligadas:
            caminho = espaco.arquivo_local(chave)
            validades[chave] = espaco.validade(caminho)[1] if caminho else None
        return saude.airac(validades, agora.date().isoformat())
