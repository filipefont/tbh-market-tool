<script lang="ts">
  // Mini-gráfico de histórico (SVG), portado do drawSpark do legado.
  interface Props {
    values: number[] | undefined;
    width?: number;
    height?: number;
    color?: string;
  }
  let { values, width = 96, height = 28, color = '#4fd1a5' }: Props = $props();

  const path = $derived.by(() => {
    const v = values ?? [];
    if (v.length < 2) return '';
    const pad = 2;
    const min = Math.min(...v);
    const max = Math.max(...v);
    const sx = (i: number) => pad + (i / (v.length - 1)) * (width - 2 * pad);
    const sy = (n: number) => pad + (max === min ? 0.5 : 1 - (n - min) / (max - min)) * (height - 2 * pad);
    return v.map((n, i) => (i ? 'L' : 'M') + sx(i).toFixed(1) + ' ' + sy(n).toFixed(1)).join(' ');
  });
</script>

{#if path}
  <svg {width} {height} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true">
    <path d={path} fill="none" stroke={color} stroke-width="1.5" vector-effect="non-scaling-stroke" />
  </svg>
{/if}
