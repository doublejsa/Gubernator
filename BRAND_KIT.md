# Gubernator — Brand & Links Cheat-Sheet

Single source of truth for the marketing site (`gubernator-co` repo) so it stays
in sync with the product app. Copy this file into the website repo.

_Last updated: 2026-06-14 · maintained alongside the app at app.gubernator.co_

---

## Product facts (use these exactly)

- **Name:** Gubernator
- **What it is:** AI Agent Control Panel. You connect your own VPS; **Claude is
  your primary interface** and supervises an **OpenClaw** AI agent running on
  that server (TUI + shell) to do real work — no terminal required after setup.
- **Pricing:** **$29/month USD**, flat. **14-day free trial.** Cancel anytime.
  Billed via **PayPal**.
- **Requirements to state up front:** bring your **own VPS** and your **own
  Anthropic API key**.
- **Company:** QEW Technologies CC, 4 Shengwedzi Road, Emmarentia 2195,
  South Africa.

---

## Canonical links (wire CTAs to these)

| Purpose | URL |
|---|---|
| **Primary CTA — Start free trial / Sign in** | `https://app.gubernator.co` |
| Terms of Service | `https://app.gubernator.co/terms` |
| Privacy Policy | `https://app.gubernator.co/privacy` |
| Acceptable Use Policy | `https://app.gubernator.co/acceptable-use` |

> The current site's "Start Free Trial" / "Try the app" buttons point to a dead
> `href="#"` — repoint them all to `https://app.gubernator.co`.

---

## Contact email

- **Canonical public contact:** `support@gubernator.co` (matches the app + legal docs).
- Also live: `info@gubernator.co`, `abuse@gubernator.co` (security/abuse reports).
- The site currently uses `hello@gubernator.co` — **pick one and use it everywhere.**
  Recommended: `support@gubernator.co` for support, keep `hello@` only if you
  want a separate sales address.

---

## Colour tokens (match the app exactly)

The app ships a light (default) + dark theme. If you align the site to the app,
use these. Accent is the same blue in both.

### Light (app default)
```
--bg:        #ffffff
--surface:   #f7f7f8
--border:    #e4e4e7
--text:      #1f2328
--dim:       #5f6470
--accent:    #2f6fed   /* primary buttons / links */
--accent-text:#ffffff  /* text on accent */
--green:     #1a7f37
--red:       #d1242f
```

### Dark
```
--bg:        #0d1117
--surface:   #161b22
--border:    #30363d
--text:      #e6edf3
--dim:       #8b949e
--accent:    #58a6ff
--green:     #3fb950
```

> The current marketing site is dark + **gold `#c4952a`** + serif. That gold/serif
> identity does NOT exist in the app. Decide (in the website chat) whether to
> align to the tokens above or keep the premium dark/gold look — but either way,
> the **logo and the in-app screenshot must match what users see after click-through.**

---

## Type

- **App UI font:** system sans — `-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif`
- **Monospace (code/terminal only):** `'JetBrains Mono', 'Menlo', monospace`
- Marketing site may use a display font for headings, but body copy should read clean/modern.

---

## Assets

- `assets/images/logo.png` — Gubernator's own logo (✅ use freely).
- `assets/images/screenshot.png` — 3-panel app view (Claude · Agent TUI · VPS terminal).
  ⚠️ **Refresh this** after the app's light-theme redesign so it matches the live product.

---

## Integrations & logos — legal

- **OpenClaw** and **Claude/Anthropic**: refer to them **by name in text**
  ("Works with OpenClaw", "Powered by Claude") — this is fine.
- **Do NOT use their logos** until you've checked each owner's brand/trademark
  guidelines & press kit. Never imply endorsement or partnership.

---

## Must-fix checklist (before paid ads)

- [ ] Trial/app CTAs → `https://app.gubernator.co`
- [ ] `gubernator.co` → GitHub Pages (CNAME file + DNS; drop dead HostGator host)
- [ ] One consistent contact email
- [ ] Footer: legal links + "QEW Technologies CC" company line
- [ ] OpenClaw/Claude = names only (no logos yet)
- [ ] Substantiate or soften hard claims ("150,000+ emails", "$40/day", "$5 server")
- [ ] `<title>` + meta description + OG/Twitter cards + favicon
- [ ] Privacy-friendly analytics + conversion tracking on the trial CTA
