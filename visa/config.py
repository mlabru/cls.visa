"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Configuração do visualizador.

Valores não sensíveis ficam aqui. Credenciais e endereços internos vêm do arquivo
`.env` (fora do git) — veja `.env.exemplo`. Nada disto é enviado ao navegador.

Usado por quase todos os módulos (`import config`). Para caminhos de arquivos, use
`RAIZ` (raiz do projeto), não `AQUI` (a pasta `visa/`).
"""
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from dotenv import load_dotenv

AQUI = Path(__file__).resolve().parent   # pasta do código (visa/)
RAIZ = AQUI.parent                        # pasta do projeto: .env, static/, data/, wifs/
load_dotenv(RAIZ / '.env')


def _obrigatorio(nome: str) -> str:
    """Lê uma variável de ambiente que não pode faltar; sem ela, encerra o programa.

    Parâmetros:
        nome: nome da variável (ex.: 'VISA_PUSHER_CHAVE'), vinda do ambiente ou do `.env`.

    Retorna:
        O valor da variável (nunca vazio).
    """
    valor = os.environ.get(nome)
    if not valor:
        raise SystemExit(f'Variável {nome} ausente. Copie .env.exemplo para .env e preencha.')
    return valor


# --- Servidor web -----------------------------------------------------------
PORTA = int(os.environ.get('VISA_PORTA', '8090'))
SEGREDO_SESSAO = _obrigatorio('VISA_SEGREDO_SESSAO')  # assina as preferências salvas por usuário

# --- Mapa de fundo ----------------------------------------------------------
# A CARTO (dark_all) passou a exigir chave; o Esri Dark Gray funciona sem chave.
MAPA_FUNDO_URL = os.environ.get(
    'VISA_MAPA_FUNDO_URL',
    'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}',
)
MAPA_FUNDO_ATRIBUICAO = 'Tiles © Esri — Esri, DeLorme, NAVTEQ'
MAPA_CENTRO = (-15.0, -52.0)
MAPA_ZOOM = 4

# --- Radar (GeoServer da REDEMET) --------------------------------------------
GEOSERVER_WMS = _obrigatorio('VISA_GEOSERVER_WMS')  # ex.: http://10.228.12.167:8080/geoserver/redemet/wms
RADAR_JANELA_MIN = 180          # janela de tempo (minutos) pedida ao WMS
RADAR_ATUALIZA_S = 120          # recarrega as imagens a cada 2 min
RADAR_TIMEOUT_S = 20
# Idade da última imagem de cada radar (min): verde, amarelo, vermelho; acima disso, cinza.
# Os mesmos limites valem para os botões, os círculos de alcance e a barra de saúde.
RADAR_VERDE_ATE_MIN = 30
RADAR_AMARELO_ATE_MIN = 60
RADAR_VERMELHO_ATE_MIN = 180
# WFS do mesmo GeoServer (tempo severo). Padrão: o endereço do WMS terminando em /wfs
GEOSERVER_WFS = os.environ.get('VISA_GEOSERVER_WFS', GEOSERVER_WMS.removesuffix('wms') + 'wfs')

# --- Vento (arquivos JSON locais, formato leaflet-velocity) ------------------
VENTO_DIR = Path(os.environ.get('VISA_VENTO_DIR', RAIZ / 'wifs' / 'ventos'))
# Recorte enviado ao navegador (graus). O arquivo é global; só a área abaixo é usada.
VENTO_RECORTE = {'lat_norte': 20.0, 'lat_sul': -60.0, 'lon_oeste': -95.0, 'lon_leste': -20.0}
VENTO_IDADE_ALERTA_H = 6        # até 6 h: verde; até 24 h: amarelo; acima: vermelho
VENTO_IDADE_VELHO_H = 24
# Nível de voo -> nível de pressão do arquivo, em Pa (atmosfera padrão / níveis WAFS)
VENTO_FL_PARA_PA = {
    50: 85000, 100: 70000, 140: 60000, 180: 50000, 240: 40000,
    300: 30000, 340: 25000, 390: 20000, 450: 15000, 530: 10000,
}

# --- Aeronaves (Pusher do DECEA / ciop-tracker) --------------------------------
PUSHER_HOST = os.environ.get('VISA_PUSHER_HOST', 'push.decea.mil.br')
PUSHER_CHAVE = _obrigatorio('VISA_PUSHER_CHAVE')
PUSHER_CANAL = 'private-tracker.aircraft.all'
PUSHER_EVENTO = 'aircraft-update'
TRACKER_AUTH_URL = _obrigatorio('VISA_TRACKER_AUTH_URL')    # inclui o ?apikey=
TRACKER_TOKEN = _obrigatorio('VISA_TRACKER_TOKEN')          # Bearer
# Trajetória do voo: <base>/montreal/<indicativo>/<data>?apikey=... (mesmo servidor e apikey da autenticação)
_partes_auth = urlsplit(TRACKER_AUTH_URL)
TRACKER_BASE = f'{_partes_auth.scheme}://{_partes_auth.netloc}/ciop-tracker'
TRACKER_APIKEY = (parse_qs(_partes_auth.query).get('apikey') or [''])[0]
del _partes_auth                # só servia para as duas contas acima
AERONAVE_EXPIRA_S = 300         # some do mapa se não for vista por 5 min
AERONAVE_ENVIO_S = 2.0          # de quanto em quanto tempo cada navegador recebe as posições

# --- Aeródromos (API da REDEMET) ---------------------------------------------
REDEMET_STATUS_URL = 'https://api-redemet.decea.mil.br/aerodromos/status'
REDEMET_API_KEY = _obrigatorio('VISA_REDEMET_API_KEY')
AERODROMOS_CACHE_S = 300        # o servidor consulta a REDEMET no máximo a cada 5 min
AERODROMOS_ATUALIZA_S = 600     # cada navegador recebe a lista nova a cada 10 min

# --- Produtos da REDEMET (radar e satélite pela API, sem VPN) -------------------
REDEMET_PRODUTOS_URL = 'https://api-redemet.decea.mil.br/produtos'
REDEMET_ESTATICO_URL = 'https://estatico-redemet.decea.mil.br'   # as imagens (públicas)
SATELITE_ATUALIZA_S = 300       # o satélite tem imagem nova a cada 10 min

# --- Modelo numérico (WRF, arquivos netCDF locais) -----------------------------
# Um arquivo por domínio e rodada: wrfd<domínio>_<AAAAMMDDHH>.nc (d1 = maior, d2 = interno). Veja modelo.py.
MODELO_DIR = Path(os.environ.get('VISA_MODELO_DIR', RAIZ / 'data' / 'netcdf'))
MODELO_CACHE_IMAGENS = 150      # imagens prontas em memória (uma rodada tem 61 horários)
MODELO_VENTO_PASSO = 0.25        # graus: grade do vento em partículas (~100 KB por horário)
MODELO_CACHE_VENTOS = 30         # ventos prontos em memória (horários × níveis)
MODELO_PROCURA_S = 600          # de quanto em quanto tempo a camada procura uma rodada nova na pasta
# Idade da rodada (desde o início): uma rodada por dia, que chega ~14 h depois do início. A
# normal fica entre ~14 h e ~38 h; passar de 40 h é uma rodada que faltou; de 64 h, duas.
MODELO_IDADE_ALERTA_H = 40
MODELO_IDADE_VELHO_H = 64

# --- Espaço aéreo (GeoServer público do DECEA / AISWEB) ------------------------
# Este servidor fica atrás do Cloudflare, que bloqueia pedidos feitos por programas.
# Por isso os tiles são buscados direto pelo navegador (não passam pelo Python).
# São dados públicos: nenhuma credencial envolvida.
DECEA_WMS = 'https://geoaisweb.decea.mil.br/geoserver/ICA/wms'
# GeoJSONs de espaço aéreo baixados do WFS (um por camada: <chave>.geojson). Veja espaco.py.
ESPACO_DIR = Path(os.environ.get('VISA_ESPACO_DIR', RAIZ / 'data' / 'espaco'))

# --- FAB.ia: registro dos conflitos previstos e do que aconteceu depois (previsao.py) ----------
# Um arquivo JSON Lines por dia (UTC). É a base para medir acertos e alarmes falsos.
PREVISAO_DIR = Path(os.environ.get('VISA_PREVISAO_DIR', RAIZ / 'logs' / 'previsao'))
