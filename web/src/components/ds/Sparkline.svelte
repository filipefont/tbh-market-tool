<script lang="ts">
  // Mini-gráfico de histórico (SVG), portado do drawSpark do legado.
  interface Props {
    values: number[] | undefined;
    width?: number;
    height?: number;
    color?: string;
    /** cor do preenchimento da área; default = `color` com alpha baixo. */
    fill?: string;
  }
  let { values, width = 96, height = 28, color = '#4fd1a5', fill }: Props = $props();

  // área preenchida sob a linha (assinatura do Cubo): tinte da cor da variação,
  // alpha baixo. Aceita override via prop `fill`; senão deriva de `color` (#rrggbb + 22).
  const area = $derived(fill ?? (/^#[0-9a-f]{6}$/i.test(color) ? `${color}22` : 'transparent'));

  const geo = $derived.by(() => {
    const v = values ?? [];
    if (v.length < 2) return null;
    const pad = 2;
    const min = Math.min(...v);
    const max = Math.max(...v);
    const sx = (i: number) => pad + (i / (v.length - 1)) * (width - 2 * pad);
    const sy = (n: number) => pad + (max === min ? 0.5 : 1 - (n - min) / (max - min)) * (height - 2 * pad);
    const line = v.map((n, i) => (i ? 'L' : 'M') + sx(i).toFixed(1) + ' ' + sy(n).toFixed(1)).join(' ');
    // fecha na base (linha + cantos inferiores) p/ o polígono de área
    const fillPath = `${line} L${sx(v.length - 1).toFixed(1)} ${height} L${sx(0).toFixed(1)} ${height} Z`;
    return { line, fillPath };
  });
</script>

{#if geo}
  <svg {width} {height} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true">
    <path d={geo.fillPath} fill={area} stroke="none" />
    <path d={geo.line} fill="none" stroke={color} stroke-width="1.5" vector-effect="non-scaling-stroke" />
  </svg>
{/if}
