import { expect, test } from '@playwright/test'

/**
 * Phase 1 smoke coverage. The full teacher, student and anti-cheat journeys
 * described in docs land with the features they exercise (phases 2, 4 and 5).
 */

test('landing page offers teacher sign-in', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { level: 1 })).toContainText('integrity monitoring')
  // 'Sign in' appears twice: the mode tab and the submit button.
  await expect(page.locator('form').getByRole('button', { name: 'Sign in' })).toBeVisible()
  await expect(page.getByLabel('Email')).toBeVisible()
})

test('landing page does not overclaim', async ({ page }) => {
  await page.goto('/')
  const body = (await page.textContent('body')) ?? ''
  // The product is never advertised as uncheatable; this asserts that stays true.
  expect(body.toLowerCase()).not.toContain('impossible to cheat')
  expect(body.toLowerCase()).not.toContain('cheating is impossible')
  expect(body).toContain('It does not make cheating impossible.')
})

test('teacher routes redirect an anonymous visitor to sign-in', async ({ page }) => {
  await page.goto('/teacher/dashboard')
  await expect(page).toHaveURL('/')
})

test('a public quiz link shows the rules before any exam content', async ({ page }) => {
  await page.goto('/q/8F7K2P')
  await expect(page.getByText('8F7K2P')).toBeVisible()
  await expect(page.getByText('Before you begin')).toBeVisible()
})

test('unknown routes render the not-found page', async ({ page }) => {
  await page.goto('/no-such-page')
  await expect(page.getByRole('heading', { name: 'Page not found' })).toBeVisible()
})
