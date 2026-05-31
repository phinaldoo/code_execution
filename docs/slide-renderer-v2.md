# How V2 Rendering Works in Principle

This document explains how the current `v2` renderer works at a design level, based on the implementation in [`v2/index.js`](./v2/index.js) and the backend orchestration in [`backend/app/render_service.py`](./backend/app/render_service.py).

The short version is:

- `v2` does not try to "understand HTML semantically" in the abstract.
- It lets a real browser fully lay out the slide HTML first.
- It then reads the rendered DOM, classifies what can be represented as editable PowerPoint primitives
- Finally, it rebuilds each slide in PowerPoint using `PptxGenJS`.

That makes `v2` a hybrid renderer:

- vector/editable where possible: `v2` renders content as vector/editable where fidelity and editability can be preserved, and falls back to rasterized/bitmap rendering when elements cannot be represented as vectors (for example complex effects, embedded images, or performance-constrained cases).

This is the core principle behind the whole pipeline: maximize editable/vector output while gracefully degrading to raster when necessary.


1. The `v2` renderer opens the page in Chromium via Playwright.
2. The renderer extracts each slide into an intermediate scene description.
3. The renderer converts that description into a `.pptx`.



## 1. The Fundamental Rendering Strategy

The most important thing to understand is that `v2` is not a DOM-to-PPTX serializer that tries to map every HTML tag directly.

Instead, it follows this strategy:

1. Let Chromium compute layout, styles, fonts, sizes, and positions.
2. Walk through the already-rendered slide DOM.
3. For each visible region, decide whether it is best represented as:
   - a PowerPoint shape
   - a PowerPoint text box with rich text runs
   - a PowerPoint image
   - or a rasterized screenshot of a complex region
4. Rebuild the slide in z-order.

That means the browser is the layout engine, while `PptxGenJS` is only the output construction layer.

This separation is the main reason the approach works.

## 2. Fixed Slide Geometry

The renderer assumes slides are authored at exactly:

- `1920 x 1080` pixels
- `96 DPI`

From that it derives the PowerPoint page size:

- width: `1920 / 96 = 20` inches
- height: `1080 / 96 = 11.25` inches

This is encoded directly in the script:

- `SLIDE_W_PX = 1920`
- `SLIDE_H_PX = 1080`
- `DPI = 96`

PowerPoint coordinates are then computed by converting browser pixel measurements into inches. Border widths and font sizes are converted into points where required.

This fixed geometry is a key simplifying assumption. It means the renderer can treat browser layout coordinates as the master coordinate system and translate them directly into PPTX positions.

## 3. Page Load and Render Stabilization

Before extracting anything, the renderer waits for the page to settle.

It does the following:

1. launches Chromium headless
2. creates a large viewport:
   - width `1920`
   - height `1080 * 20`
3. loads the input page with `waitUntil: "networkidle"`
4. waits for `document.fonts.ready`
5. waits for two animation frames
6. waits an additional `1500ms`

This extra waiting is not cosmetic. It tries to reduce timing-related mismatches caused by:

- font loading
- async layout shifts
- CSS animations finishing initial setup
- browser paint timing

The renderer therefore extracts from a stable rendered page, not from a partially loaded one.

## 4. Slide Discovery

Slides are identified entirely by the CSS selector:

- `.slide`

Every element matching `.slide` becomes one PowerPoint slide.
