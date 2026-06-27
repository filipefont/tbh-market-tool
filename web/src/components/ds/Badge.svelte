<script lang="ts">
  import type { Snippet } from 'svelte';

  // Selo genérico do design system. Variantes portadas dos selos do legado:
  // live (AO VIVO), new (NOVO), lock (intradável), e os verdicts do Craft.
  type Variant = 'live' | 'new' | 'lock' | 'craft' | 'gamble' | 'sell' | 'neutral';

  interface Props {
    variant?: Variant;
    title?: string;
    children: Snippet;
  }
  let { variant = 'neutral', title, children }: Props = $props();

  const STYLES: Record<Variant, string> = {
    live: 'text-accent-ink bg-accent border-accent',
    new: 'text-[#1c1f26] bg-[#5fd38d] border-transparent font-bold',
    lock: 'text-[#b9a0e0] border-[#b9a0e055] bg-[#b9a0e01a]',
    craft: 'text-[#9ff0bf] border-[#2e7d4f] bg-[#1c3a2a]',
    gamble: 'text-[#f4d58a] border-[#7d6a2e] bg-[#3a341c]',
    sell: 'text-[#8fc9ff] border-[#2e5a7d] bg-[#1c2c3a]',
    neutral: 'text-muted border-line bg-surface',
  };
</script>

<span
  {title}
  class="inline-flex items-center gap-1 rounded-md border px-1.5 py-px text-[10px] font-semibold whitespace-nowrap {STYLES[variant]}"
>
  {#if variant === 'live'}<i class="inline-block size-1.5 animate-pulse rounded-full bg-current"></i>{/if}
  {@render children()}
</span>
