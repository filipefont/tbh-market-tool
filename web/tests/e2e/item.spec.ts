import { expect, test } from '@playwright/test';

// Página estática por item (Fase 4). 'mystic-topaz' existe nas fixtures (gema com efeitos).
test('/item/[slug]: página do item renderiza stats e efeitos', async ({ page }) => {
  await page.goto('./item/mystic-topaz');

  await expect(page.getByRole('heading', { name: /Mystic Topaz/i })).toBeVisible();
  await expect(page.getByText('Gold (Cubo)')).toBeVisible();
  await expect(page.getByText('Gold / R$')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Efeitos' })).toBeVisible();

  // volta ao Mercado
  await page.getByRole('link', { name: /voltar ao Mercado/i }).click();
  await expect(page.getByRole('heading', { name: 'Mercado' })).toBeVisible();
});

test('link do Mercado abre a página do item', async ({ page }) => {
  await page.goto('./');
  // busca p/ trazer o item à janela virtualizada (independe de dados reais/fixtures)
  await page.getByPlaceholder('buscar item').fill('Mystic Topaz');
  const link = page.getByRole('link', { name: /Mystic Topaz/i }).first();
  await expect(link).toBeVisible();
  await link.click();
  await expect(page).toHaveURL(/\/item\/mystic-topaz/);
});
