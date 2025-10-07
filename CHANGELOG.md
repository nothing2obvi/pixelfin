# 📜 Changelog

All notable changes to this project will be documented in this file.  
This project follows [Semantic Versioning](https://semver.org/).

---

## v0.34.6 – 2025-10-07

#### Added
- **Lightbox “Open in New Tab” support:** Users can now right-click or middle-click any image (in both the gallery and lightbox views) to open it directly in a new browser tab instead of downloading it.
- **Version label in GitHub view link:** Added display of the current app version (`v0.34.6`) next to “View on GitHub” in the HTML generation success message.

#### Changed
- **Anchor behavior:** All image anchors now include `target="_blank"` and direct image URLs, preserving lightbox behavior on normal clicks while enabling native browser tab opening.
- **Lightbox markup:** The lightbox image is now wrapped in a clickable `<a id="lightbox-link" href="" target="_blank">` element so full-size images can also be opened directly.

#### Fixed
- **Right-click behavior:** Resolved issue where “Open image in new tab” would download instead of open when triggered from gallery or lightbox.
- **Backdrop labeling:** Single backdrops are now labeled “Backdrop,” while multiple backdrops display as “Backdrop (0),” “Backdrop (1),” etc. for clarity.

---

## v0.34.5 – 2025-10-07

#### Added
- **Backdrops (multi-index support):** Added full detection of `BackdropImageTags`, enabling retrieval and display of every available backdrop (`Backdrop/0`, `Backdrop/1`, etc.).
- **ZIP packaging:** Now includes all backdrop images in each item’s folder as `backdrop01`, `backdrop02`, etc. (or using the overridden base name like `background01`, `background02`).

#### Changed
- **HTML generation:** Backdrops section now iterates through all detected backdrops and displays them sequentially in the right column.
- **Lightbox behavior:** Now automatically includes all backdrops (and other images) in the same slideshow sequence for each item.
- **ZIP naming:** Numeric suffixes in ZIP outputs are now zero-padded (`backdrop01`, `backdrop02`, etc.) for cleaner sorting.

#### Fixed
- **Single-backdrop limitation:** Resolved issue where only one backdrop image was being displayed or downloaded even when multiple existed.
- **Consistency in backdrop handling:** HTML and ZIP logic now share unified multi-tag detection, ensuring both reflect the same image set.

---

## v0.34.4 - 2025-09-07

#### Changed
- In HTML: Titles for series to show production year if title is a duplicate
- In HTML: Titles for movies to always show production year
- In ZIPs: Folder names to show production year for disambiguation

---

## v0.34.3 — 2025-09-01

### Changed
- "height and width" to "width and height"

---

## v0.34.2 — 2025-08-31

### Added
- **View on GitHub** button

---

## v0.34.1 — 2025-08-31

### Changed
- Amount of **spacing** between API Key and the color selectors

---

## v0.34 — 2025-08-31

### Added
- Caption for the **Minimum Resolution / ZIP Filename Override** table explaining the purpose of min width/height and ZIP filename override.  
- Heading next to the **image selection checkboxes**. 
- Preservation of **color picker history** for background, text, and table colors across sessions.  

### Changed
- Link colors in **dark mode**:  
  - Unvisited links are now **light blue**.  
  - Visited links are now **light purple**.

---

## v0.33 — 2025-08-31
### Changed
- **Color selections are now preserved**:
  - Background, text, and table background colors are saved automatically.
  - Selected colors are restored on page reload and when switching between libraries.


---

## v0.32 - 2025-08-31
### Fixed
- **Creation of `history.json`**: the file is now automatically created on first start if it doesn't exist.
- Thanks to [@avassor](https://github.com/avassor) for suggesting improvements related to history handling.

### ⚠ Breaking Change
- `history.json` location updated to `/app/data/history.json`.
  - Old location: `/app/history.json`
  - New location: `/app/data/history.json`
  - Impact: If you want to maintain your existing history, move your old `history.json` file to `/app/data/history.json`.

---

## v0.31 — 2025-08-31
### Changed
- **Main page layout updated**:
  - Centered server URL, library name, and API key in three rows.
  - Pixelfin logo enlarged by 25% and centered.
  - Color pickers (Background, Text, Table Background) arranged in a single row.
  - Image type checkboxes spaced evenly and aligned next to color pickers.
  - Minimum Resolution / ZIP Filename table widened and centered.
  - Bottom buttons (Light/Dark, Generate HTML, Create ZIP) centered and uniform in size.

---

## v0.3 — 2025-08-30
### Added
- **Favicon** now appears in all pages of the app as well as generated HTML files.
- **ZIP file creation**: bundle selected images into a downloadable archive.
- **Filename override**: customize how images are named when included in ZIP files.

---

## v0.2 — 2025-08-30
### Added
- **Minimum resolution threshold** setting and summary table.
- **Low-resolution images** are:
  - Marked with red captions in the gallery.
  - Listed alongside missing images in callouts.
  - Indicated directly in the summary table for quick scanning.

---

## v0.1 — 2025-08-16
### Initial Release
- HTML gallery generation of Jellyfin libraries.
- Downloadable **embedded HTMLs** (with images base64-encoded for sharing/archiving).
- Indicators of **missing images** shown in both the gallery and summary table.
- Removed extraneous `>` from [relevant feature/file]. Thanks to [@LoV432](https://github.com/LoV432) for contributing a fix.
