<script lang="ts">
  import { type Lang, useTranslations } from '../../i18n/ui.ts';

  // Frescor dos preços, fiel ao header do Cubo:
  //   [AO VIVO|desatualizado] · somente leitura · preços atualizados há Xh (dd/mm/aaaa hh:mm)
  // Calculado NO CLIENTE (idade relativa sempre correta) e formatado em BRT —
  // o build roda no GitHub Actions (UTC), então formatar no servidor mostrava a
  // hora errada. `epoch` = maior fetchedAt de preço real/encomenda (segundos).
  interface Props {
    epoch: number;
    lang?: Lang;
  }
  let { epoch, lang = 'pt' }: Props = $props();
  const t = useTranslations(lang);

  const STALE_S = 6 * 3600; // 6h — acima disso o dado é "desatualizado" (espelha o legado)

  // idade relativa legível, frase natural por idioma
  function rel(ageS: number): string {
    if (ageS < 90) return lang === 'pt' ? 'agora' : 'just now';
    const suffix = lang === 'pt' ? '' : ' ago';
    const prefix = lang === 'pt' ? 'há ' : '';
    if (ageS < 3600) return `${prefix}${Math.round(ageS / 60)} min${suffix}`;
    if (ageS < 86400) return `${prefix}${Math.round(ageS / 3600)} h${suffix}`;
    return `${prefix}${Math.round(ageS / 86400)} d${suffix}`;
  }

  // tick: na montagem (cliente) e a cada minuto, p/ o relativo não congelar
  let nowS = $state(Math.floor(Date.now() / 1000));
  $effect(() => {
    const id = setInterval(() => (nowS = Math.floor(Date.now() / 1000)), 60_000);
    return () => clearInterval(id);
  });

  const ageS = $derived(Math.max(0, nowS - epoch));
  const stale = $derived(ageS > STALE_S);
  const when = $derived(
    new Date(epoch * 1000).toLocaleString(lang === 'pt' ? 'pt-BR' : 'en-US', {
      day: '2-digit',
      month: '2-digit',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      timeZone: 'America/Sao_Paulo',
    }),
  );
</script>

{#if epoch}
  <span class="flex flex-wrap items-center gap-1.5">
    <span
      class="rounded-full px-1.5 py-0.5 text-[10px] font-bold"
      class:bg-accent={!stale}
      class:text-accent-ink={!stale}
      style:background={stale ? '#e0a86a22' : undefined}
      style:color={stale ? '#e0a86a' : undefined}
      title={stale
        ? 'atualização automática atrasada — pode estar desatualizado'
        : 'dados ao vivo — atualizados automaticamente'}
    >
      {stale ? t('status.stale') : t('status.live')}
    </span>
    <span>{t('status.readonly')} · {t('status.updated')} {rel(ageS)}</span>
    <span class="text-hint">({when})</span>
  </span>
{/if}
