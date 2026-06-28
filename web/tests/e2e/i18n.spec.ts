import { expect, test } from '@playwright/test';

test('PT padrão e EN em /en com a casca traduzida', async ({ page }) => {
  // PT (padrão, sem prefixo)
  await page.goto('./');
  await expect(page.getByRole('heading', { name: 'Mercado' })).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('lang', 'pt-BR');

  // troca p/ EN pelo seletor
  await page.getByRole('link', { name: 'en', exact: true }).click();
  await expect(page).toHaveURL(/\/en\/?(\?|$)/);
  await expect(page.getByRole('heading', { name: 'Market' })).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
});

test('nav EN mantém o locale entre abas', async ({ page }) => {
  await page.goto('./en/');
  await page.getByRole('link', { name: /Effects/ }).click();
  await expect(page).toHaveURL(/\/en\/efeitos/);
  await expect(page.getByRole('heading', { name: 'Effects' })).toBeVisible();
});
