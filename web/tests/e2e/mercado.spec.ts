import { expect, test } from '@playwright/test';

// Paridade do Mercado: toggle tabela/cartões + filtros (busca/limpar).
test('Mercado: toggle de cartões e filtros funcionam', async ({ page }) => {
  await page.goto('./');
  await expect(page.getByRole('button', { name: /R\$ BRL/ })).toBeVisible(); // ilha hidratada

  // layout de cartões (Cubo) — reusa o ItemCard
  await page.getByRole('button', { name: 'Cartões' }).click();
  await expect(page.locator('article').first()).toBeVisible();

  // busca filtra e mostra o botão limpar
  await page.getByPlaceholder('buscar item').fill('a');
  await expect(page.getByRole('button', { name: /limpar/ })).toBeVisible();

  // limpar reseta os filtros (some o botão)
  await page.getByRole('button', { name: /limpar/ }).click();
  await expect(page.getByRole('button', { name: /limpar/ })).toHaveCount(0);
});
