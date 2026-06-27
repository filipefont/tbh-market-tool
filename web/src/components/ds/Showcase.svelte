<script lang="ts">
  import type { HistorySeries, MarketItem } from '../../lib/contract/index.ts';
  import type { Currency } from '../../lib/format.ts';
  import CurrencyToggle from './CurrencyToggle.svelte';
  import ItemCard from './ItemCard.svelte';
  import PriceChart from './PriceChart.svelte';

  // Parte interativa do showcase: a moeda é o estado compartilhado e propaga
  // para os cards e o gráfico (prova a reatividade entre componentes do DS).
  interface Props {
    items: MarketItem[];
    series: HistorySeries;
  }
  let { items, series }: Props = $props();

  let currency = $state<Currency>('brl');
</script>

<section class="space-y-4">
  <div class="flex items-center gap-3">
    <span class="text-xs text-muted">Moeda:</span>
    <CurrencyToggle value={currency} onchange={(c) => (currency = c)} />
  </div>

  <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
    {#each items as item, i (item.name)}
      <ItemCard {item} rank={i + 1} {currency} />
    {/each}
  </div>

  <div class="rounded-[10px] border border-white/10 bg-white/[0.04] p-3">
    <PriceChart {series} {currency} />
  </div>
</section>
