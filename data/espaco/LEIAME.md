# GeoJSONs de espaço aéreo

Atualizar a cada ciclo AIRAC (28 dias).
Abra cada URL num navegador normal e salve com o nome indicado nesta pasta. 
Camada sem arquivo usa as imagens do DECEA.

Base: https://geoaisweb.decea.mil.br/geoserver/ICA/ows?service=WFS&version=1.0.0&request=GetFeature&outputFormat=application/json&typeName=

| Salvar como             | typeName                |
|-------------------------|-------------------------|
| aerovias_altas.geojson  | ICA:vw_aerovia_alta_v2  |
| aerovias_baixas.geojson | ICA:vw_aerovia_baixa_v2 |
| fir.geojson             | ICA:fir                 |
| setor_fir.geojson       | ICA:SETOR_FIR           |
| tma.geojson             | ICA:TMA                 |
| setores_tma.geojson     | ICA:setores_tma         |
| areas_proibidas.geojson | ICA:eac_p               |
| areas_restritas.geojson | ICA:eac_r               |
| areas_perigosas.geojson | ICA:eac_d               |

Observações:

- A validade é lida do próprio arquivo (campos emenda e emenda_futura);
- o painel avisa quando vence.

- As cartas ENRC (ICA:ENRC_H*, ICA:ENRC_L*) são só imagem: o WFS responde
- "Feature type ICA:ENRC_H1 unknown".
  Ficam sempre com as imagens do DECEA.
