---
name: B3S
description: Brand evidence lab with RGB terminal styling, mono typography, dense report grids, and evidence-first interaction.
colors:
  bg-day: "#eeeeee"
  panel-day: "#f5f5f5"
  ink-day: "#161616"
  muted-day: "#77736d"
  soft-day: "#9a958e"
  line-day: "#e4e4e4"
  line-strong-day: "#d7d7d7"
  bg-night: "#0b0d0e"
  panel-night: "#121615"
  ink-night: "#dedbd2"
  muted-night: "#9b968c"
  soft-night: "#706c65"
  line-night: "#28302d"
  line-strong-night: "#39423e"
  rgb-red: "#ff0000"
  rgb-green: "#00ff00"
  rgb-blue: "#0000ff"
  bad-day: "#b84f3f"
  bad-night: "#e07a6d"
typography:
  display:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "58px"
    fontWeight: 700
    lineHeight: 0.95
    letterSpacing: "0"
  headline:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "28px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "0"
  title:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "16px"
    fontWeight: 700
    lineHeight: 1.25
    letterSpacing: "0"
  body:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "0"
  label:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "11px"
    fontWeight: 700
    lineHeight: 1.4
    letterSpacing: "0"
rounded:
  none: "0"
  xs: "2px"
  sm: "4px"
  md: "6px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
  xxl: "32px"
components:
  button-primary:
    backgroundColor: "{colors.rgb-red}"
    textColor: "{colors.bg-day}"
    rounded: "{rounded.none}"
    padding: "10px 18px"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.ink-day}"
    rounded: "{rounded.none}"
    padding: "10px 18px"
  panel:
    backgroundColor: "{colors.panel-day}"
    textColor: "{colors.ink-day}"
    rounded: "{rounded.none}"
    padding: "16px"
  chip:
    backgroundColor: "transparent"
    textColor: "{colors.muted-day}"
    rounded: "{rounded.xs}"
    padding: "1px 8px"
  input:
    backgroundColor: "{colors.panel-day}"
    textColor: "{colors.ink-day}"
    rounded: "{rounded.none}"
    padding: "10px 12px"
---

# Design System: B3S

## 1. Overview

**Creative North Star: "Evidence Observatory"**

B3S is a technical observatory for brand evidence, not a decorative analytics product. The interface should feel like a calibration desk where each number, citation, visual capture, tile, and acquisition warning can be inspected under controlled conditions.

The system is mono-forward, RGB-coded, dense, and skeptical. It inherits the old B3S terminal feel but must stay legible enough for repeated comparison work: scanner rows, SV9 reports, brand profiles, visual moodboards, API health, and scoring lab views all belong to the same operational instrument.

It explicitly rejects generic SaaS dashboards, beige editorial Brand3 styling as the default, glossy AI surfaces, purple gradients, yellow warning semantics, nested cards, decorative charts, and marketing-style hero layouts.

**Key Characteristics:**

- Mono typography across UI, data, controls, reports, and labels.
- Day/night themes using actual RGB red, green, and blue as system colors.
- Rectilinear panels, sharp controls, visible borders, and minimal radius.
- Progressive disclosure: summary on the card, evidence and tile detail in drawers.
- Brand-first report hierarchy: brand name, URL, score, margin, then components.

## 2. Colors

The palette is neutral terminal infrastructure with rare full-intensity RGB signals. Color is semantic, not decorative.

### Primary

- **RGB Red**: Primary action, active diagnostic labels, B3S logo red, score emphasis, selected links, and section labels.
- **RGB Green**: Positive margin, available/ok state, B3S logo green, and score gain.
- **RGB Blue**: B3S logo blue and reserved informational signal. Do not use yellow for "not detected"; if a warning needs emphasis, use text plus RGB/status classes.

### Neutral

- **Day Background**: Cold light base for daytime inspection. It must stay neutral gray, not cream, sand, or parchment.
- **Day Panel**: Slightly lifted gray surface for forms, cards, tables, and reports.
- **Night Background**: Near-black green-tinted terminal base.
- **Night Panel**: Low-contrast dark panel that keeps borders and text readable.
- **Ink / Muted / Soft**: The primary reading ramp. Muted text is for metadata and secondary explanation only; body evidence should not become too faint.
- **Line / Strong Line**: Borders are structural. They define panels, cards, tables, drawers, and controls.

### Named Rules

**The RGB Semantics Rule.** Red, green, and blue are system signals. Use them sparingly and literally; never replace missing evidence with yellow.

**The No Beige Drift Rule.** The historical `design/DESIGN.md` beige/Fraunces/Manrope direction is not the active B3S UI language unless a task explicitly asks to revive that branch.

## 3. Typography

**Display Font:** JetBrains Mono with ui-monospace fallback.
**Body Font:** JetBrains Mono with ui-monospace fallback.
**Label/Mono Font:** JetBrains Mono with ui-monospace fallback.

**Character:** The product speaks in one technical voice. There is no serif/sans editorial pairing in the active UI; precision and consistency matter more than typographic decoration.

### Hierarchy

- **Display** (700, up to 58px, 0.95 line-height): brand names, report hero scores, and brand profile identity only.
- **Headline** (700, 24-28px, 1.2 line-height): drawer titles and major page titles.
- **Title** (700, 13-16px, 1.2-1.25 line-height): component labels, section heads, and compact panel headings.
- **Body** (400, 13px, 1.5-1.62 line-height): evidence summaries, report prose, card descriptions, and operational explanation.
- **Label** (700, 10-12px, uppercase where useful): metadata, table headers, score labels, terminal keys, and chip text.

### Named Rules

**The One Font Rule.** Do not introduce a second family for product UI. If a future brand surface needs a different font, scope it to that surface.

**The No Fluid Product Type Rule.** Avoid viewport-scaled type inside tools, tables, cards, drawers, and forms. Large clamped type is allowed only for brand identity and score hero moments.

## 4. Elevation

B3S is flat by default. Depth is conveyed through borders, tonal layers, panel adjacency, and grid placement. Shadows are allowed only where the UI needs a physical stacking cue: the report drawer and floating visual moodboard cards.

### Shadow Vocabulary

- **Drawer Push** (`box-shadow: -18px 0 42px var(--shadow)`): only for right-side modal drawers.
- **Visual Cloud Card** (`box-shadow: 0 18px 48px rgb(0 0 0 / 45%)`): only inside the moodboard cloud stage.

### Named Rules

**The Border Is Structure Rule.** Use 1px borders and layout rhythm before reaching for shadows.

**The No Card Pile Rule.** Cards can represent repeated report components, but page sections must not become cards inside cards.

## 5. Components

### Buttons

- **Shape:** Sharp rectangle (0px radius), mono 13px bold.
- **Primary:** RGB red background with background-colored text; use for scan/run/save actions.
- **Hover / Focus:** Brightness shift on hover, 2px red focus outline on focus-visible.
- **Secondary:** Transparent surface, structural border, ink text. Use for navigation, optional actions, and report links.

### Chips

- **Style:** Small bordered labels with 2px radius and mono 12px text.
- **State:** `ok` uses RGB green, `info` uses RGB red unless a blue information state is explicitly implemented, `bad` uses the bad red ramp. Do not add yellow chips for missing evidence.

### Cards / Containers

- **Corner Style:** Mostly square (0px) with report cards capped at 4px when separation helps scanning.
- **Background:** Panel or panel-soft on top of bg.
- **Shadow Strategy:** None at rest.
- **Border:** 1px structural line. Dashed border is reserved for muted or not-detected component cards.
- **Internal Padding:** 14-18px for report cards and drawers, 16px for panels.

### Inputs / Fields

- **Style:** Square mono fields with strong line border, panel background, 10px/12px padding.
- **Focus:** Red border plus 2px red outline.
- **Error / Disabled:** Disabled uses reduced opacity. Errors use bad red border and text.

### Navigation

- **Style:** Top header with B3S logo, muted tagline, and compact Day/Night segmented controls.
- **Links:** Red text by default, stronger underline on hover/focus.
- **Local movement:** Use source-link style for utility links and brand/report transitions.

### Report Component Card

Component cards show only the necessary summary: label, score badge, eye action, result, and short verdict. Tile detail, rejected content, citations, and long evidence belong in the drawer.

### Report Drawer

The drawer is a technical reading panel. It uses hidden scrollbar chrome, sticky head, section boxes, and tile rows. It must remain keyboard-operable and close through native dialog behavior.

### Moodboard Cloud

The moodboard is the expressive exception. It may use a dark visual stage, image cloud cards, grain, and RGB glow because it is inspecting visual evidence rather than presenting report data.

## 6. Do's and Don'ts

### Do:

- **Do** preserve the brand-first report hierarchy: brand name, URL, score, immediate margin, then cards.
- **Do** keep report rows and cards comparable by using stable widths, explicit grid spans, and predictable score badges.
- **Do** put long tile explanations, citations, acquisition limitations, and rejected content behind drawers or secondary panels.
- **Do** use visible text labels for evidence status; color alone is not enough.
- **Do** keep day/night themes aligned by role, not by unrelated palettes.

### Don't:

- **Don't** make B3S look like a generic SaaS dashboard, CRM, investor analytics panel, or AI startup landing page.
- **Don't** use beige editorial Brand3 styling as the default active UI.
- **Don't** use yellow for "not detected" or missing evidence.
- **Don't** add purple gradients, glassmorphism, glossy AI surfaces, decorative charts, or marketing hero layouts.
- **Don't** create nested cards, repeated equal-weight card grids, or page sections styled as floating cards.
- **Don't** expose all tile information on the first report view when the drawer can carry the explanation.
