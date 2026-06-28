// Rótulos e metadados portados do build.py (aba Craft) + humanização de stats.

/** Tipo de equipamento -> rótulo PT (TYPE_LABEL do legado). */
export const TYPE_LABEL: Record<string, string> = {
  MainWeapon: 'Arma princ.',
  SubWeapon: 'Arma sec.',
  Helmet: 'Elmo',
  Armor: 'Armadura',
  Gloves: 'Luvas',
  Boots: 'Botas',
  Accessory: 'Acessório',
};

export const typeLabel = (t: string | null | undefined): string => (t && TYPE_LABEL[t]) || t || '?';

export type Verdict = 'craft' | 'gamble' | 'sell' | 'unknown';

/** Veredito do craft -> selo + dica (VERDICT_META do legado). */
export const VERDICT_META: Record<Verdict, { variant: Verdict; txt: string; tip: string }> = {
  craft: {
    variant: 'craft',
    txt: '✅ vale craftar',
    tip: 'o valor médio de revenda da pull é maior que o custo dos reagentes',
  },
  gamble: {
    variant: 'gamble',
    txt: '🎲 aposta',
    tip: 'existe item na pull que vale mais que os reagentes, mas na média (EV) você perde — é aposta',
  },
  sell: {
    variant: 'sell',
    txt: '💰 venda os reagentes',
    tip: 'nenhum item possível da pull vale mais que os reagentes — melhor vendê-los',
  },
  unknown: {
    variant: 'unknown',
    txt: '— sem preço',
    tip: 'algum material não tem preço de mercado agora; não dá p/ decidir',
  },
};

export const verdictMeta = (v: string | null | undefined) =>
  VERDICT_META[(v as Verdict) in VERDICT_META ? (v as Verdict) : 'unknown'];

/** Humaniza uma chave de stat: "LightningDamagePercent" -> "Lightning Damage %". */
export function attrLabel(stat: string | null | undefined): string {
  if (!stat) return '';
  return stat
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/\bPercent\b/, '%')
    .trim();
}
