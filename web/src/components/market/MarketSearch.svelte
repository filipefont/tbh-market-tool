<script lang="ts">
  import { qA, wireUrlSync } from '../../lib/stores/market.ts';
  import { ms } from '../../lib/stores/marketState.svelte.ts';

  // Busca do Mercado, no TOPO da sidebar (como no Cubo). Escreve no mesmo atom
  // `qA` que a tabela lê — outro island, mesmo estado compartilhado.
  interface Props {
    placeholder: string;
  }
  let { placeholder }: Props = $props();
  $effect(() => wireUrlSync());
</script>

<input
  type="search"
  value={ms.q}
  oninput={(e) => qA.set(e.currentTarget.value)}
  {placeholder}
  class="w-full rounded-md border border-line bg-field px-2.5 py-2 text-xs text-ink focus-visible:outline-2 focus-visible:outline-accent-bright"
/>
