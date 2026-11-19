/*
 * Apoio a camadas do mapa — sem lógica de negócio (quem decide tudo é o Python).
 *
 * 1. setEstilo() nas camadas WMS: pinta e traceja as imagens das aerovias no navegador
 *    (o GeoServer do DECEA devolve imagens prontas e não passa pelo nosso servidor).
 * 2. L.aerodromosLayer: círculos dos aeródromos com o boletim no balão (tooltip).
 * 3. L.espacoVetorLayer: espaço aéreo a partir dos GeoJSONs locais.
 *
 * Quem usa, do Python (visa/camadas/contexto.py):
 *   - CamadasEspaco.aplicar cria a camada WMS (opção `estilo`) ou o 'espacoVetorLayer';
 *     CamadasEspaco.mudar_estilo chama setEstilo nas duas.
 *   - CamadaAerodromos cria o 'aerodromosLayer'; CamadaAerodromos.enviar chama setDados.
 *
 * Rotas, tempo severo e círculos de alcance dos radares são desenhados pelo Python
 * (desenho.py), com as camadas prontas do Leaflet.
 */
(function () {
  const L = window.L;

  // ------------------------------------------------------------------------
  // 1. Estilo das camadas WMS: { cor: '#rrggbb' | 'original', traco: 'continuo' | 'tracejado' | 'pontilhado' }
  // ------------------------------------------------------------------------

  // Filtro CSS que tinge a imagem com a cor pedida (cinza → sépia → gira o matiz).
  // Converte a cor '#rrggbb' para matiz/saturação (HSL) e devolve o texto do filtro;
  // '' para a cor original da imagem.
  function filtroDaCor(hex) {
    if (!hex || hex === 'original') return '';
    const vermelho = parseInt(hex.slice(1, 3), 16) / 255;
    const verde = parseInt(hex.slice(3, 5), 16) / 255;
    const azul = parseInt(hex.slice(5, 7), 16) / 255;
    const max = Math.max(vermelho, verde, azul), min = Math.min(vermelho, verde, azul);
    let matiz = 0, saturacao = 0;   // matiz de 0 a 1 (volta inteira), saturação de 0 a 1
    const luminosidade = (max + min) / 2;
    if (max !== min) {
      const amplitude = max - min;
      saturacao = luminosidade > 0.5 ? amplitude / (2 - max - min) : amplitude / (max + min);
      if (max === vermelho) matiz = ((verde - azul) / amplitude + (verde < azul ? 6 : 0)) / 6;
      else if (max === verde) matiz = ((azul - vermelho) / amplitude + 2) / 6;
      else matiz = ((vermelho - verde) / amplitude + 4) / 6;
    }
    const giro = Math.round(matiz * 360) - 50;   // a sépia fica em ~50°
    return `grayscale(100%) sepia(100%) hue-rotate(${giro}deg) saturate(${Math.max(1, Math.round(saturacao * 200))}%)`;
  }

  // Máscara em listras verticais que simula linha tracejada/pontilhada ('' = linha contínua)
  function mascaraDoTraco(traco) {
    if (traco === 'tracejado') return 'repeating-linear-gradient(90deg, black 0 14px, transparent 14px 20px)';
    if (traco === 'pontilhado') return 'repeating-linear-gradient(90deg, black 0 4px, transparent 4px 8px)';
    return '';
  }

  // `estilo` passa a ser uma opção conhecida (assim não vai como parâmetro para o WMS)
  L.TileLayer.WMS.mergeOptions({ estilo: null });
  // Toda camada WMS aplica o seu estilo ao entrar no mapa
  L.TileLayer.WMS.addInitHook(function () {
    this.on('add', this._aplicarEstilo, this);
  });
  L.TileLayer.WMS.include({
    // Chamado pelo Python (CamadasEspaco.mudar_estilo): troca cor e traço sem baixar de novo.
    setEstilo(estilo) {
      this.options.estilo = estilo;
      this._aplicarEstilo();
    },
    // Põe o filtro de cor e a máscara de traço no elemento que contém as imagens da camada.
    _aplicarEstilo() {
      const container = this.getContainer && this.getContainer();
      const estilo = this.options.estilo;
      if (!container || !estilo) return;
      container.style.filter = filtroDaCor(estilo.cor);
      container.style.maskImage = container.style.webkitMaskImage = mascaraDoTraco(estilo.traco);
    },
  });

  // ------------------------------------------------------------------------
  // 2. Aeródromos: setDados([{lat, lon, cor, aviso, titulo, texto}, ...])
  // ------------------------------------------------------------------------
  L.AerodromosLayer = L.FeatureGroup.extend({
    // Chamado pelo Python (CamadaAerodromos.enviar): apaga tudo e desenha um círculo por aeródromo.
    // Contorno branco e mais grosso quando há aviso.
    setDados(lista) {
      this.clearLayers();
      for (const aerodromo of lista || []) {
        const ponto = L.circleMarker([aerodromo.lat, aerodromo.lon], {
          radius: 5,
          fillColor: aerodromo.cor,
          fillOpacity: 0.9,
          color: aerodromo.aviso ? '#ffffff' : '#000000',
          weight: aerodromo.aviso ? 2 : 1.5,
        });
        ponto.bindTooltip(() => this._balao(aerodromo), {
          direction: 'right', offset: [10, 0], opacity: 1, className: 'aerodromo-balao',
        });
        this.addLayer(ponto);
      }
    },

    // Conteúdo montado com textContent: texto vindo da API nunca vira HTML
    _balao(aerodromo) {
      const div = document.createElement('div');
      const titulo = document.createElement('div');
      titulo.className = 'aerodromo-titulo';
      titulo.textContent = aerodromo.titulo;
      const texto = document.createElement('div');
      texto.className = 'aerodromo-texto';
      texto.textContent = aerodromo.texto;
      div.append(titulo, texto);
      return div;
    },
  });
  // Fábrica: é o nome 'aerodromosLayer' que o Python usa em generic_layer.
  L.aerodromosLayer = (options) => new L.AerodromosLayer([], options);

  // ------------------------------------------------------------------------
  // 3. Espaço aéreo vetorial: L.espacoVetorLayer(url, {estilo: {cor, traco, opacidade}})
  //    Baixa o GeoJSON do nosso servidor e desenha. Clique mostra as propriedades.
  // ------------------------------------------------------------------------
  const TRACOS = { continuo: null, tracejado: '10 6', pontilhado: '2 5' };   // dashArray do Leaflet

  L.EspacoVetorLayer = L.GeoJSON.extend({
    // Construtor: começa vazia e baixa o GeoJSON de `url` (/espaco/<chave>.geojson).
    initialize(url, options) {
      L.GeoJSON.prototype.initialize.call(this, null, options);
      this.options.style = () => this._estiloLeaflet();
      this.options.onEachFeature = (feicao, camada) => camada.bindPopup(() => this._ficha(feicao));
      fetch(url)
        .then((resposta) => resposta.json())
        .then((dados) => this.addData(dados))
        .catch((erro) => console.warn('[espaço aéreo]', url, erro));
    },

    // Chamado pelo Python (CamadasEspaco.mudar_estilo): troca cor, traço e opacidade.
    setEstilo(estilo) {
      this.options.estilo = estilo;
      this.setStyle(this._estiloLeaflet());
    },

    // Traduz o nosso estilo {cor, traco, opacidade, preenchimento} para as opções do Leaflet.
    _estiloLeaflet() {
      const estilo = this.options.estilo || {};
      return {
        color: estilo.cor, weight: 1.5, opacity: estilo.opacidade ?? 0.8,
        dashArray: TRACOS[estilo.traco] || null,
        fillOpacity: estilo.preenchimento ?? 0.04,   // polígonos: leve (FIR, TMA) ou mais forte (áreas P/R/D)
      };
    },

    // Tabela com todas as propriedades da feição (textContent: nunca HTML)
    _ficha(feicao) {
      const div = document.createElement('div');
      const propriedades = feicao.properties || {};
      if (propriedades._titulo) {
        const titulo = document.createElement('div');
        titulo.style.cssText = 'font-weight:700;font-size:13px;margin-bottom:4px';
        titulo.textContent = propriedades._titulo;
        div.appendChild(titulo);
      }
      const tabela = document.createElement('table');
      tabela.style.fontSize = '12px';
      div.appendChild(tabela);
      for (const [chave, valor] of Object.entries(propriedades)) {
        if (chave === '_titulo') continue;
        const linha = tabela.insertRow();
        const celulaChave = linha.insertCell();
        celulaChave.style.fontWeight = '600';
        celulaChave.style.paddingRight = '8px';
        celulaChave.textContent = chave;
        linha.insertCell().textContent = String(valor);
      }
      return div;
    },
  });
  // Fábrica: é o nome 'espacoVetorLayer' que o Python usa em generic_layer.
  L.espacoVetorLayer = (url, options) => new L.EspacoVetorLayer(url, options);
})();
