/*
 * Apoio a camadas do mapa — sem lógica de negócio (quem decide tudo é o Python).
 *
 * 1. setEstilo() nas camadas WMS: pinta e traceja as imagens das aerovias no navegador
 *    (o GeoServer do DECEA devolve imagens prontas e não passa pelo nosso servidor).
 * 2. L.aerodromosLayer: círculos dos aeródromos com o boletim no balão (tooltip).
 * 3. L.espacoVetorLayer: espaço aéreo a partir dos GeoJSONs locais.
 *
 * Rotas, tempo severo e círculos de alcance dos radares são desenhados pelo Python
 * (desenho.py), com as camadas prontas do Leaflet.
 */
(function () {
  const L = window.L;

  // ------------------------------------------------------------------------
  // 1. Estilo das camadas WMS: { cor: '#rrggbb' | 'original', traco: 'continuo' | 'tracejado' | 'pontilhado' }
  // ------------------------------------------------------------------------

  // Filtro CSS que tinge a imagem com a cor pedida (cinza → sépia → gira o matiz)
  function filtroDaCor(hex) {
    if (!hex || hex === 'original') return '';
    const r = parseInt(hex.slice(1, 3), 16) / 255;
    const g = parseInt(hex.slice(3, 5), 16) / 255;
    const b = parseInt(hex.slice(5, 7), 16) / 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b);
    let h = 0, s = 0;
    const l = (max + min) / 2;
    if (max !== min) {
      const d = max - min;
      s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
      if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
      else if (max === g) h = ((b - r) / d + 2) / 6;
      else h = ((r - g) / d + 4) / 6;
    }
    const giro = Math.round(h * 360) - 50;   // a sépia fica em ~50°
    return `grayscale(100%) sepia(100%) hue-rotate(${giro}deg) saturate(${Math.max(1, Math.round(s * 200))}%)`;
  }

  // Máscara em listras verticais que simula linha tracejada/pontilhada
  function mascaraDoTraco(traco) {
    if (traco === 'tracejado') return 'repeating-linear-gradient(90deg, black 0 14px, transparent 14px 20px)';
    if (traco === 'pontilhado') return 'repeating-linear-gradient(90deg, black 0 4px, transparent 4px 8px)';
    return '';
  }

  // `estilo` passa a ser uma opção conhecida (assim não vai como parâmetro para o WMS)
  L.TileLayer.WMS.mergeOptions({ estilo: null });
  L.TileLayer.WMS.addInitHook(function () {
    this.on('add', this._aplicarEstilo, this);
  });
  L.TileLayer.WMS.include({
    setEstilo(estilo) {
      this.options.estilo = estilo;
      this._aplicarEstilo();
    },
    _aplicarEstilo() {
      const el = this.getContainer && this.getContainer();
      const estilo = this.options.estilo;
      if (!el || !estilo) return;
      el.style.filter = filtroDaCor(estilo.cor);
      el.style.maskImage = el.style.webkitMaskImage = mascaraDoTraco(estilo.traco);
    },
  });

  // ------------------------------------------------------------------------
  // 2. Aeródromos: setDados([{lat, lon, cor, aviso, titulo, texto}, ...])
  // ------------------------------------------------------------------------
  L.AerodromosLayer = L.FeatureGroup.extend({
    setDados(lista) {
      this.clearLayers();
      for (const a of lista || []) {
        const ponto = L.circleMarker([a.lat, a.lon], {
          radius: 5,
          fillColor: a.cor,
          fillOpacity: 0.9,
          color: a.aviso ? '#ffffff' : '#000000',
          weight: a.aviso ? 2 : 1.5,
        });
        ponto.bindTooltip(() => this._balao(a), {
          direction: 'right', offset: [10, 0], opacity: 1, className: 'aerodromo-balao',
        });
        this.addLayer(ponto);
      }
    },

    // Conteúdo montado com textContent: texto vindo da API nunca vira HTML
    _balao(a) {
      const div = document.createElement('div');
      const titulo = document.createElement('div');
      titulo.className = 'aerodromo-titulo';
      titulo.textContent = a.titulo;
      const texto = document.createElement('div');
      texto.className = 'aerodromo-texto';
      texto.textContent = a.texto;
      div.append(titulo, texto);
      return div;
    },
  });
  L.aerodromosLayer = (options) => new L.AerodromosLayer([], options);

  // ------------------------------------------------------------------------
  // 3. Espaço aéreo vetorial: L.espacoVetorLayer(url, {estilo: {cor, traco, opacidade}})
  //    Baixa o GeoJSON do nosso servidor e desenha. Clique mostra as propriedades.
  // ------------------------------------------------------------------------
  const TRACOS = { continuo: null, tracejado: '10 6', pontilhado: '2 5' };

  L.EspacoVetorLayer = L.GeoJSON.extend({
    initialize(url, options) {
      L.GeoJSON.prototype.initialize.call(this, null, options);
      this.options.style = () => this._estiloLeaflet();
      this.options.onEachFeature = (feicao, camada) => camada.bindPopup(() => this._ficha(feicao));
      fetch(url)
        .then((r) => r.json())
        .then((dados) => this.addData(dados))
        .catch((erro) => console.warn('[espaço aéreo]', url, erro));
    },

    setEstilo(estilo) {
      this.options.estilo = estilo;
      this.setStyle(this._estiloLeaflet());
    },

    _estiloLeaflet() {
      const e = this.options.estilo || {};
      return {
        color: e.cor, weight: 1.5, opacity: e.opacidade ?? 0.8,
        dashArray: TRACOS[e.traco] || null,
        fillOpacity: e.preenchimento ?? 0.04,   // polígonos: leve (FIR, TMA) ou mais forte (áreas P/R/D)
      };
    },

    // Tabela com todas as propriedades da feição (textContent: nunca HTML)
    _ficha(feicao) {
      const div = document.createElement('div');
      const props = feicao.properties || {};
      if (props._titulo) {
        const titulo = document.createElement('div');
        titulo.style.cssText = 'font-weight:700;font-size:13px;margin-bottom:4px';
        titulo.textContent = props._titulo;
        div.appendChild(titulo);
      }
      const tabela = document.createElement('table');
      tabela.style.fontSize = '12px';
      div.appendChild(tabela);
      for (const [chave, valor] of Object.entries(props)) {
        if (chave === '_titulo') continue;
        const linha = tabela.insertRow();
        const c1 = linha.insertCell();
        c1.style.fontWeight = '600';
        c1.style.paddingRight = '8px';
        c1.textContent = chave;
        linha.insertCell().textContent = String(valor);
      }
      return div;
    },
  });
  L.espacoVetorLayer = (url, options) => new L.EspacoVetorLayer(url, options);
})();
