import { expect, test } from '@playwright/test';

test('home /next carrega e a ilha Svelte hidrata', async ({ page }) => {
  await page.goto('./');

  // título da fundação
  await expect(page.getByRole('heading', { name: /TBH Market/i })).toBeVisible();

  // a ilha interativa precisa hidratar e responder ao clique. client:load hidrata
  // de forma assíncrona, então re-tenta o clique até o estado sair de 0 (cliques
  // disparados antes da hidratação são perdidos — isso tolera a corrida).
  const btn = page.getByRole('button', { name: /ilha svelte ativa/i });
  await expect(btn).toContainText('cliques: 0');
  await expect(async () => {
    await btn.click();
    await expect(btn).not.toContainText('cliques: 0', { timeout: 1000 });
  }).toPass();
});
