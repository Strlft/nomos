# Nomos · UX Redesign Notes

## Diagnosis of current portals
1. **Chrome-heavy dashboards.** Both portals present as dense dashboards on first load — sidebar, KPI row, tables. Works for a power user on day 90, fails the first 90 seconds.
2. **§-refs bleed into UX language.** Clients see "§3(b) verified," "§5(a)(ii) escalation." For a corporate treasurer this is noise. For the lawyer it's signal. Same UI, two audiences.
3. **No narrative.** A contract has a life — proposed, negotiated, signed, in-flight, maturing, closed out. The current UI is a filing cabinet, not a timeline.
4. **Alerts are flat.** 10 alert cards of similar weight. No "today you must do X."
5. **Onboarding is invisible.** Pick role → land in portal. There's no welcome, no first-contract guide, no trust-building.
6. **Two separate portals, no shared language.** Advisor and client don't visibly share context. The "dual portal" promise needs to be seen.

## Design principles for v2
- **Plain language first, jargon as metadata.** "Representations verified" is the label; "§3" sits in a muted mono tag beside it.
- **One document, two lenses.** Same contract object; advisor sees clause-level edit, client sees obligations + impact. Shared comment thread.
- **Narrative layout.** Each contract has a spine — a horizontal timeline — that is the primary navigation for that contract.
- **Editorial, not enterprise.** Serif display for structural headings, sans for body, mono for legal refs and IDs. Warm off-white paper. Accents muted.
- **Hero moments get motion.** Signing, payment-today, breach — the rest is calm.
- **Trust through transparency.** Every automated step shows what it did (oracle waterfall, calculation breakdown, audit entry).

## Type system
- Display: "Fraunces" substitute → using "Instrument Serif" / Cormorant / fallback Georgia. Actually picking **"Fraunces"** from G-Fonts — but wait, system prompt flags overused. Use **"Newsreader"** for display (editorial, unusual, reads well at 48px+) and **"Söhne"**-adjacent alt. Since no Söhne, pair Newsreader with **"Geist"** (Vercel's sans; open-source, geometric-humanist). Mono: **"Geist Mono"**.
- Actually: Newsreader + Geist + Geist Mono via Google Fonts. Clean, not cliché.

## Color
- Paper: `oklch(97% 0.008 80)` (warm off-white)
- Ink: `oklch(18% 0.01 80)`
- Hairline: `oklch(90% 0.01 80)`
- Accent: single warm ochre `oklch(58% 0.09 65)` — used sparingly for action + lawyer surfaces
- Green (health): `oklch(58% 0.08 140)`
- Red (breach): `oklch(55% 0.14 25)`
- Violet eliminated — was AI-slop gradient. Replaced with monochrome + ochre accent.

## IA (new)
- **Shell**: role-aware. Same chrome, different lens.
- **Landing after login**: not a dashboard. A "today" page — one paragraph of what matters, links out.
- **Contracts**: index → detail. Detail uses the spine/timeline as its top-level nav.
- **Counterparties / People** (advisor only)
- **Library** (clause/template bank, advisor only)
- **Audit** (both, but advisor sees deeper)

## Hero moments
1. Onboarding: advisor invites client → client walks through 3-step intake → first contract draft appears.
2. Signing ceremony: two parties, side-by-side, live-updating.
3. Payment Tuesday: "Today, you owe €7,200 net to Party A. Here's why."
4. Breach unfold: "Part 3 statement was due 6 days ago. Clock to §5(a)(ii) EoD rec: 24 days."

## Workflow flow (primary path)
1. Advisor logs in → Dashboard shows "1 pending intake from Novus Corp"
2. Advisor drafts contract → sends for client review
3. Client gets invite email → creates account → reviews in plain language + PDF side-by-side
4. Client comments inline → advisor resolves → client signs
5. Advisor countersigns → contract becomes LIVE with animated timeline reveal
6. Ongoing: payment dates auto-calc → advisor approves PI → client sees "paid"
