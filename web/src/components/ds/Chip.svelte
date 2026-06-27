<script lang="ts">
  import type { Snippet } from 'svelte';

  // Chip/pílula clicável (top-movers, filtros). Vira botão se receber onclick.
  interface Props {
    onclick?: () => void;
    active?: boolean;
    title?: string;
    children: Snippet;
  }
  let { onclick, active = false, title, children }: Props = $props();

  const base =
    'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs whitespace-nowrap transition-colors';
  const tone = $derived(
    active
      ? 'border-accent-bright bg-accent/15 text-ink'
      : 'border-line bg-field text-muted hover:border-accent-bright hover:text-ink',
  );
</script>

{#if onclick}
  <button type="button" {title} {onclick} class="{base} {tone} cursor-pointer focus-visible:outline-2 focus-visible:outline-accent-bright">
    {@render children()}
  </button>
{:else}
  <span {title} class="{base} {tone}">{@render children()}</span>
{/if}
