// Marketing screenshot generator for the gubernator-co website.
//
// Logs into the live app with YOUR credentials (read from env vars, never
// hard-coded), forces LIGHT theme + the 3-panel view (Claude · Agent TUI ·
// VPS Console), waits for the terminals to settle, and saves a crisp 2x PNG.
//
// ── Setup (one time) ─────────────────────────────────────────────────────────
//   cd /Users/james/Dropbox/dev/Gubernator
//   npm i -D playwright && npx playwright install chromium
//
// ── Run ──────────────────────────────────────────────────────────────────────
//   GOV_EMAIL='you@example.com' GOV_PASSWORD='yourpassword' \
//     node scripts/marketing_screenshot.mjs
//
// Output: scripts/out/screenshot.png  (1680×1000 @2x → 3360×2000 px)
// Then copy it into the website repo as assets/images/screenshot.png
//
// Optional env:
//   GOV_BASE   (default https://app.gubernator.co)
//   GOV_WIDTH  (default 1680)   GOV_HEIGHT (default 1000)
//   GOV_WAIT   (default 7000 ms — time to let terminals/WS draw before capture)
//   GOV_HEADED=1  (watch it run in a visible browser)

import { chromium } from 'playwright';
import { mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const EMAIL = process.env.GOV_EMAIL;
const PASSWORD = process.env.GOV_PASSWORD;
const BASE = (process.env.GOV_BASE || 'https://app.gubernator.co').replace(/\/$/, '');
const W = parseInt(process.env.GOV_WIDTH || '1680', 10);
const H = parseInt(process.env.GOV_HEIGHT || '1000', 10);
const WAIT = parseInt(process.env.GOV_WAIT || '7000', 10);
const HEADED = process.env.GOV_HEADED === '1';

if (!EMAIL || !PASSWORD) {
  console.error('Set GOV_EMAIL and GOV_PASSWORD env vars (your app login).');
  process.exit(1);
}

const __dirname = dirname(fileURLToPath(import.meta.url));
const outDir = resolve(__dirname, 'out');
mkdirSync(outDir, { recursive: true });
const outFile = resolve(outDir, 'screenshot.png');

const log = (...a) => console.log('•', ...a);

const browser = await chromium.launch({ headless: !HEADED });
const ctx = await browser.newContext({
  viewport: { width: W, height: H },
  deviceScaleFactor: 2,
});
const page = await ctx.newPage();

try {
  log('Opening login…', BASE);
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });

  // Some setups land on /app if a cookie exists; only log in if the form is here.
  if (await page.locator('#login-btn').count()) {
    log('Signing in…');
    await page.fill('#email', EMAIL);
    await page.fill('#password', PASSWORD);
    await Promise.all([
      page.waitForLoadState('networkidle').catch(() => {}),
      page.click('#login-btn'),
    ]);
  }

  // Make sure we are in the app shell.
  await page.waitForSelector('#app', { timeout: 20000 });

  // Force the marketing-friendly state, then reload so the app boots into it.
  log('Forcing light theme + 3-panel view…');
  await page.evaluate(() => {
    localStorage.setItem('gov_theme', 'light');
    localStorage.setItem('gov_terminals', 'open');   // show Agent + Console
    localStorage.setItem('gov_console', 'open');      // show bottom shell
    localStorage.setItem('gov_sidebar', 'open');      // full sidebar w/ labels
  });
  await page.reload({ waitUntil: 'domcontentloaded' });

  // Guard: if the paywall took over, the account isn't entitled.
  await page.waitForSelector('#app', { timeout: 20000 });
  const paywalled = await page.evaluate(() =>
    !!document.querySelector('.pw-price, .pw-benefits'));
  if (paywalled) {
    throw new Error('Account is not subscribed — paywall is showing. Use an entitled account.');
  }

  // Wait for the right panel (terminals) to be visible.
  await page.waitForSelector('#right-panel', { state: 'visible', timeout: 20000 }).catch(() => {});

  log(`Letting terminals/WebSockets draw for ${WAIT}ms…`);
  await page.waitForTimeout(WAIT);

  // Tidy: hide the "click to type" hover hint for a clean shot.
  await page.addStyleTag({ content: '.terminal-wrap::after{display:none !important;}' });

  log('Capturing…');
  await page.screenshot({ path: outFile });
  console.log('\n✓ Saved', outFile);
  console.log('  Copy it into the website repo as assets/images/screenshot.png');
  console.log('  (Review it first — make sure no terminal output is sensitive.)');
} catch (err) {
  console.error('\n✗ Failed:', err.message);
  process.exitCode = 1;
} finally {
  await browser.close();
}
