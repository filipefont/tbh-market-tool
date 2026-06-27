<script lang="ts">
  import uPlot from 'uplot';
  import 'uplot/dist/uPlot.min.css';
  import type { HistorySeries } from '../../lib/contract/index.ts';
  import { type Currency, currencySymbol } from '../../lib/format.ts';

  // Gráfico de histórico de preço com uPlot (substitui o drawSpark SVG do legado).
  interface Props {
    series: HistorySeries;
    currency?: Currency;
    height?: number;
  }
  let { series, currency = 'brl', height = 120 }: Props = $props();

  const sym = $derived(currencySymbol(currency));
  const xs = $derived(series.map((p) => p[0]));
  const ys = $derived(series.map((p) => p[1]));
  const lo = $derived(ys.length ? Math.min(...ys) : 0);
  const hi = $derived(ys.length ? Math.max(...ys) : 0);
  const last = $derived(ys.length ? ys[ys.length - 1] : null);

  let el: HTMLDivElement;

  $effect(() => {
    if (!el || series.length < 2) return;
    const accent = '#4fd1a5';
    const chart = new uPlot(
      {
        width: el.clientWidth || 320,
        height,
        cursor: { show: true, points: { size: 5 } },
        legend: { show: false },
        scales: { x: { time: true } },
        axes: [
          { stroke: '#5b6378', grid: { stroke: '#ffffff10' }, ticks: { stroke: '#ffffff10' } },
          { stroke: '#5b6378', grid: { stroke: '#ffffff10' }, ticks: { stroke: '#ffffff10' }, size: 44 },
        ],
        series: [
          {},
          {
            stroke: accent,
            width: 1.5,
            fill: 'rgba(79,209,165,.12)',
            points: { show: false },
          },
        ],
      },
      [xs, ys],
      el,
    );
    const ro = new ResizeObserver(() => chart.setSize({ width: el.clientWidth, height }));
    ro.observe(el);
    return () => {
      ro.disconnect();
      chart.destroy();
    };
  });
</script>

<div class="w-full">
  <div class="mb-1 flex items-center justify-between text-xs">
    <h3 class="font-semibold text-ink">Histórico de preço ({sym})</h3>
  </div>
  {#if series.length < 2}
    <div class="text-xs text-hint">histórico insuficiente</div>
  {:else}
    <div bind:this={el} class="w-full"></div>
    <div class="tabular mt-1 flex justify-between text-[11px] text-muted">
      <span>mín {sym} {lo.toFixed(2)}</span>
      <span>máx {sym} {hi.toFixed(2)}</span>
      <span>último {sym} {last?.toFixed(2)}</span>
    </div>
  {/if}
</div>
