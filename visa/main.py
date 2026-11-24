"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

CLS Visa — visualizador meteorológico para quem acompanha o tráfego aéreo.

Para rodar (da raiz do projeto):   ./visa.sh rodar   (ou .venv/bin/python visa/main.py)
e abra  http://localhost:8090

Este arquivo só liga as peças: as rotas HTTP que o navegador chama diretamente (imagens
e GeoJSON, que não passam pelo websocket do NiceGUI), as tarefas de fundo do servidor e
a página. O que cada módulo faz está na tabela "Onde está cada coisa" do README.md, e os
porquês das escolhas, em DECISOES.md.
"""
import asyncio
import logging

from fastapi import HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response
from nicegui import app, ui

import aeronaves
import config
import espaco
import impacto
import interface
import modelo
import radar
import redemet

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')
logging.getLogger('httpx').setLevel(logging.WARNING)  # o httpx loga a URL inteira, com a api_key

app.add_static_files('/static', config.RAIZ / 'static')

# ---------------------------------------------------------------------------
# Rotas HTTP do servidor (o navegador pede as imagens e os GeoJSON aqui)
#
# Rotas `async def` só esperam a rede (o trabalho pesado vai para uma thread lá dentro);
# rotas `def` comuns fazem trabalho de CPU ou disco, e o FastAPI as roda numa thread
# sozinho — assim nenhuma delas trava o laço que atende os outros usuários.
# ---------------------------------------------------------------------------

app.add_middleware(GZipMiddleware, minimum_size=1000)   # GeoJSONs grandes vão comprimidos


@app.get('/espaco/{chave}.geojson')
def espaco_geojson(chave: str) -> Response:
    """Um arquivo de espaço aéreo (TMAs, aerovias...) já preparado para o mapa.

    Parâmetros:
        chave: a chave do arquivo em espaco.POR_CHAVE.

    Retorna:
        O GeoJSON; 404 se a chave não existe ou não há arquivo local.
    """
    caminho = espaco.arquivo_local(chave) if chave in espaco.POR_CHAVE else None
    if caminho is None:
        raise HTTPException(404)
    return Response(espaco.geojson_preparado(caminho), media_type='application/geo+json')


@app.get('/radar/wms')
async def radar_wms(request: Request) -> Response:
    """Proxy do WMS do GeoServer (radar, raios, WIFS): repassa os parâmetros da consulta."""
    status, conteudo, tipo = await radar.buscar_tile(dict(request.query_params))
    return Response(conteudo, status_code=status, media_type=tipo)


@app.get('/redemet/imagem')
async def redemet_imagem(c: str, dbz: int = 0) -> Response:
    """Imagem da API da REDEMET (radar, satélite), já reprojetada e filtrada.

    Os nomes curtos `c` e `dbz` são os parâmetros da URL que o navegador pede: não renomear.

    Parâmetros:
        c: o caminho da imagem, como a API da REDEMET o listou (400 inválido, 404 desconhecido).
        dbz: limiar em dBZ, de 0 (tudo) a 75; só filtra o radar.
    """
    status, conteudo, tipo = await redemet.buscar_imagem(c, dbz)
    cabecalhos = {'Cache-Control': 'max-age=3600'} if status == 200 else {}   # o caminho já tem a data
    return Response(conteudo, status_code=status, media_type=tipo, headers=cabecalhos)


@app.get('/redemet/raios')
def redemet_raios(q: str) -> Response:
    """Um quadro de raios da API da REDEMET desenhado como imagem.

    `q` é o parâmetro da URL que o navegador pede: não renomear.

    Parâmetros:
        q: a chave de um quadro já consultado pela camada (404 se desconhecido).
    """
    status, conteudo, tipo = redemet.imagem_raios(q)
    cabecalhos = {'Cache-Control': 'max-age=3600'} if status == 200 else {}   # um quadro não muda
    return Response(conteudo, status_code=status, media_type=tipo, headers=cabecalhos)



@app.get('/modelo/imagem')
def modelo_imagem(r: str, c: str, h: str, n: int = 0) -> Response:
    """Um campo do modelo WRF (todos os domínios compostos) como imagem.

    Os nomes curtos são os parâmetros da URL que o navegador pede: não renomear. Rota `def`:
    a leitura do netCDF e a imagem rodam numa thread.

    Parâmetros:
        r: a rodada, AAAAMMDDHH.
        c: a chave do campo (modelo.POR_CHAVE).
        h: o horário previsto, AAAAMMDDHH (UTC).
        n: o nível de pressão em hPa (só nos campos por nível).
    """
    status, conteudo, tipo = modelo.buscar_imagem(r, c, h, n)
    cabecalhos = {'Cache-Control': 'max-age=600'} if status == 200 else {}   # uma rodada pode ser regravada
    return Response(conteudo, status_code=status, media_type=tipo, headers=cabecalhos)

async def iniciar() -> None:
    """Tarefas que rodam enquanto o servidor estiver de pé, uma só para todos os usuários.

    As tarefas ficam guardadas em `app.state` de propósito: o asyncio só mantém uma
    referência fraca às tarefas criadas, e uma tarefa sem referência pode ser recolhida
    pelo coletor de lixo no meio da execução (aviso da documentação de create_task).
    """
    app.state.tarefa_pusher = asyncio.create_task(aeronaves.manter_conexao())
    app.state.tarefa_radar = asyncio.create_task(radar.vigiar())
    app.state.tarefa_impacto = asyncio.create_task(impacto.vigiar())


app.on_startup(iniciar)

# reload=False: o recarregamento automático do NiceGUI reiniciaria as tarefas de fundo (e a
# conexão com o Pusher) a cada arquivo salvo; show=False: é um servidor, não abre navegador.
ui.run(interface.pagina, title='CLS Visa', port=config.PORTA, dark=True, reload=False, show=False,
       storage_secret=config.SEGREDO_SESSAO, favicon='🛩')
