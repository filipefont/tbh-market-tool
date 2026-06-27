<script lang="ts">
  // Ilha de validação da Fase 0: prova que Svelte 5 (runes) + Tailwind + TS
  // hidratam no cliente. Some quando a Fase 1 trouxer os componentes reais.
  let count = $state(0);
</script>

<button
  onclick={() => count++}
  class="tabular rounded-md border border-line bg-field px-3 py-2 text-sm text-ink
         transition-colors hover:border-accent-bright focus-visible:outline-2
         focus-visible:outline-accent-bright"
>
  ilha svelte ativa — cliques: {count}
</button>
