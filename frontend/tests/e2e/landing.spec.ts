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

test('an unknown quiz code shows a plain unavailable message', async ({ page }) => {
  // The public page now fetches the real quiz. With no backend running (or an
  // unknown code) it must fail closed and say nothing about whether the code
  // exists.
  await page.goto('/q/8F7K2P')
  await expect(page.getByRole('heading', { name: 'Quiz unavailable' })).toBeVisible()
  const body = (await page.textContent('body')) ?? ''
  expect(body).not.toContain('correct')
})

test('unknown routes render the not-found page', async ({ page }) => {
  await page.goto('/no-such-page')
  await expect(page.getByRole('heading', { name: 'Page not found' })).toBeVisible()
})
