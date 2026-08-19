# Design QA

- Source visual truth: `reference/rolling-budget-table.png`
- Source pixels: 1412 × 498
- Implementation URL: `http://127.0.0.1:4173/`
- Intended desktop viewport: 1452 × 720 CSS px at device scale factor 1
- Intended comparison crop: `.budget-widget`, approximately 1400 × 515 CSS px
- State: API success with five populated categories as of 2026-08-19
- Implementation screenshot: unavailable in the current session

## Evidence available

- The source reference image was opened and inspected.
- The production Vite build completed successfully.
- The local preview returned HTTP 200 on the loopback interface.
- The preview proxy returned the live five-category dashboard JSON.
- The in-app browser automation interface required by the browser workflow is
  not available in this session.

## Findings

- [P1] Browser-rendered visual comparison is unavailable.
  Location: full dashboard widget.
  Evidence: the source is available, but there is no browser-rendered
  implementation screenshot to place beside it.
  Impact: typography, exact column alignment, responsive behavior, and console
  cleanliness cannot be accepted from source code or HTTP checks alone.
  Fix: after approval, capture the loopback preview with local Playwright at the
  intended viewport, create a side-by-side comparison with the source, inspect
  console errors, and iterate on any P0/P1/P2 differences.

## Required fidelity surfaces

- Fonts and typography: Inter 400/500/600 is bundled, but rendered metrics are
  not yet visually confirmed.
- Spacing and layout rhythm: desktop and responsive grid rules are implemented,
  but the rendered crop is not yet available.
- Colors and visual tokens: teal, gray, and over-budget red tokens were matched
  from the source by inspection; browser rendering is not yet compared.
- Image and icon fidelity: visible icons use the Phosphor icon library rather
  than handmade SVG or CSS drawings; their rendered size and optical alignment
  are not yet compared.
- Copy and content: all five rows, rolling windows, totals, limits, and
  remaining/over labels are driven by the live API response.

## Comparison history

- No visual comparison iteration has run because the implementation screenshot
  is blocked. No P0/P1/P2 visual fix has been claimed.

## Implementation checklist

- Capture the rendered desktop widget and a narrow responsive state.
- Check the browser console and the retry/error interaction.
- Compare source and implementation in one side-by-side image.
- Fix any P0/P1/P2 differences and repeat the comparison.

## Follow-up polish

- Decide whether the host dashboard should expose pending/refund details on hover
  or keep the first version visually identical to the compact table.

final result: blocked
