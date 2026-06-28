import { expect, test } from '@playwright/test';

test('home carrega o Mercado e a tabela hidrata', async ({ page }) => {
  await page.goto('./');

  await expect(page.getByRole('heading', { name: 'Mercado' })).toBeVisible();
  // shell do Cubo: sidebar com brand
  await expect(page.getByText('TBH Market Tool')).toBeVisible();

  // a ilha (client:only) hidratou: toggle de moeda + cards (visão padrão = Cubo)
  await expect(page.getByRole('button', { name: /R\$ BRL/ })).toBeVisible();
  await expect(page.locator('article').first()).toBeVisible();

  // alternar p/ tabela mostra o cabeçalho ordenável e ordena
  await page.getByRole('button', { name: 'Tabela' }).click();
  const sortHeader = page.getByRole('button', { name: /Gold\/moeda/ });
  await expect(sortHeader).toBeVisible();
  await sortHeader.click();
  await expect(sortHeader).toContainText(/[▲▼]/);
});
