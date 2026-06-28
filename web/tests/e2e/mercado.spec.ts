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

test('Mercado: faceta multi-seleção e favoritos', async ({ page }) => {
  await page.goto('./');
  await expect(page.getByRole('button', { name: /R\$ BRL/ })).toBeVisible();

  // faceta "categoria" (multi-seleção via <details>)
  const facet = page.locator('details', { hasText: 'categoria' });
  await facet.locator('summary').click();
  await facet.getByRole('checkbox').first().check();
  await facet.locator('summary').click(); // fecha o painel (não sobrepor os controles)
  await expect(page.getByRole('button', { name: /limpar/ })).toBeVisible();
  await page.getByRole('button', { name: /limpar/ }).click();

  // favoritar a 1ª linha e filtrar por favoritos
  await page.locator('button[title="favoritar"]').first().click();
  await page.locator('label', { hasText: 'favoritos' }).getByRole('checkbox').check();
  await expect(page.locator('a[href*="/item/"]').first()).toBeVisible();
});

test('Mercado: filtro por atributo adiciona coluna dinâmica', async ({ page }) => {
  await page.goto('./');
  await expect(page.getByRole('button', { name: /R\$ BRL/ })).toBeVisible();

  const facet = page.locator('details', { hasText: 'atributo' });
  await facet.locator('summary').click();
  await facet.locator('label', { hasText: 'Armor' }).getByRole('checkbox').check();
  await facet.locator('summary').click();

  // o atributo selecionado vira coluna (cabeçalho ordenável "Armor")
  await expect(page.getByRole('button', { name: 'Armor', exact: true })).toBeVisible();
});
