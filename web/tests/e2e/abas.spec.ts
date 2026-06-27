import { expect, test } from '@playwright/test';

// As 3 abas portadas na Fase 2 (rotas estáticas). Valida render + navegação.
test('/efeitos lista gemas com efeitos', async ({ page }) => {
  await page.goto('./efeitos');
  await expect(page.getByRole('heading', { name: 'Efeitos' })).toBeVisible();
  const card = page.locator('article').first();
  await expect(card).toBeVisible();
  await expect(card).toContainText('preço'); // card de gema mostra o preço
});

test('/craft lista receitas com veredito', async ({ page }) => {
  await page.goto('./craft');
  await expect(page.getByRole('heading', { name: 'Craft' })).toBeVisible();
  await expect(page.locator('article').first()).toBeVisible();
  await expect(page.getByText('Reagentes').first()).toBeVisible();
});

test('/farm lista estágios com EV', async ({ page }) => {
  await page.goto('./farm');
  await expect(page.getByRole('heading', { name: 'Farm' })).toBeVisible();
  await expect(page.locator('article').first()).toBeVisible();
});

test('nav leva do ranking às abas', async ({ page }) => {
  await page.goto('./');
  await page.getByRole('link', { name: /Craft/ }).click();
  await expect(page).toHaveURL(/\/craft/);
  await expect(page.getByRole('heading', { name: 'Craft' })).toBeVisible();
});
