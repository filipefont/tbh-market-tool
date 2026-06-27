<script lang="ts">
  import type { Currency } from '../../lib/format.ts';

  // Segmented control BRL/USD (controlado). Estilo do .seg do legado.
  interface Props {
    value: Currency;
    onchange: (cur: Currency) => void;
  }
  let { value, onchange }: Props = $props();

  const OPTS: { id: Currency; label: string }[] = [
    { id: 'brl', label: 'R$ BRL' },
    { id: 'usd', label: '$ USD' },
  ];
</script>

<div class="inline-flex overflow-hidden rounded-md border border-line" role="group" aria-label="moeda">
  {#each OPTS as opt (opt.id)}
    <button
      type="button"
      aria-pressed={value === opt.id}
      onclick={() => onchange(opt.id)}
      class="tabular px-3 py-1.5 text-xs transition-colors focus-visible:outline-2 focus-visible:outline-accent-bright
             {value === opt.id ? 'bg-accent text-accent-ink font-semibold' : 'bg-field text-muted hover:bg-row-hover'}"
    >
      {opt.label}
    </button>
  {/each}
</div>
