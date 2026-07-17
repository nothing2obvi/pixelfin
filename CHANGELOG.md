# 📜 Changelog

All notable changes to this project will be documented in this file.  
This project follows [Semantic Versioning](https://semver.org/).

---

## Unreleased

- Nothing yet.

---

## v1.0.5 - 2026-07-17

#### Added
- **Jellyfin v12 compatibility:** Pixelfin now sends Jellyfin v12-compatible authentication headers while keeping older token headers for Jellyfin 10.11 compatibility.
- **Theme picker:** Added a theme picker with more built-in themes and a keyboard shortcut to cycle themes.
- **Existing ZIP controls:** Existing Auto ZIPs can now be deleted from Settings, with matching button styling and toast feedback.
- **Multiple backdrop support:** Pixelfin now treats multiple Jellyfin backdrops as distinct images in scans, listings, ZIP restore comparisons, and restore previews.

#### Changed
- **Server testing:** Server Test now validates authenticated Jellyfin API access and reports `Connection successful` instead of returning the Jellyfin server name.
- **Admin user selection:** Server Test refreshes the Admin User dropdown after a successful authenticated connection.
- **Server saving:** Save Server now stays in Settings and immediately tests the saved credentials instead of closing the modal with a page reload.
- **ZIP and scan status:** Scan and ZIP status messages now show active work in the page header, including specific library names for single-library work.
- **ZIP status wording:** Multi-library ZIP work now displays `Zipping libraries...`.

#### Fixed
- **Scheduled ZIP exports:** Fixed scheduled ZIP export jobs so they actually create ZIPs and respect ZIP retention.
- **Auto scan scheduling:** Improved scheduled auto-scan reliability and audit logging.
- **Background scans:** Scan completion now refreshes the page while preserving where you were looking.
- **ZIP completion position:** Single-library ZIP downloads and scheduled ZIP runs now refresh without dragging the page back to an old gallery position.
- **Duplicate image rows:** Scans now upsert duplicate image rows instead of failing on repeated media item/image type/label combinations.
- **Image refresh:** Scans refresh image cache state so changed Jellyfin artwork appears without a hard browser refresh.
- **Attention badge colors:** Attention counts now use theme-appropriate badge colors.

#### Notes
- **Jellyfin API keys:** If server tests or scans still fail with authentication errors, generate a new Jellyfin API key and save the server again in Pixelfin.

---

## v1.0.4 - 2026-05-25

#### Fixed
- **Scanned image refresh:** Update, Scan, and Scan All now refresh image URLs with a scan timestamp so the browser does not keep showing stale Jellyfin artwork from cache.
- **Image proxy cache:** Pixelfin's image proxy now tells browsers not to store proxied artwork, which makes newly uploaded Jellyfin images appear after scans without requiring a hard refresh.

---

## v1.0.3 - 2026-05-25

#### Changed
- **Readable output folders:** Generated HTML and ZIP files now save under readable library folders like `output/4K Asian Series/` instead of legacy folders like `output/4K_Asian_Series/`.
- **Automatic legacy output migration:** Pixelfin now silently moves files from legacy underscore-style output folders into readable library folders on startup, then deletes the old empty folders.

#### Fixed
- **Restore comparison links:** Clicking a media item in restore results now scrolls to the correct comparison gallery section.
- **Lightbox keyboard navigation:** Pressing up or down in the lightbox now keeps the background page anchored to the media item you moved to.
- **Jellyfin links in lightbox:** The media item title in the lightbox now links to that item in Jellyfin.
- **Screenshot metadata:** Cleaned metadata from the comparison screenshot.

---

## v1.0.2 - 2026-05-25

#### Added
- **Admin sync user setting:** Added a server setting to choose which enabled Jellyfin admin user Pixelfin uses when syncing and scanning libraries.

#### Changed
- **Server settings layout:** Tightened the server settings row so Admin User and API Key no longer overlap, and shortened the automatic admin option to `Auto`.
- **Restore comparisons:** Restore review comparisons now align current Jellyfin images and ZIP images by image type, with clear missing placeholders when either side is absent.

#### Fixed
- **Table position:** Toggling complete items now keeps the view at the table instead of jumping to the first listed media item.

---

## v1.0.1 - 2026-05-23

#### Changed
- **Develop reset:** Reset `develop` to match `main` so future work starts from the released Pixelfin v1 codebase.
- **Docker Compose naming:** Updated both Compose files to use the `pixelfin` container name.
- **README screenshots:** Added the dry-run comparison screenshot and cleaned screenshot metadata.
- **Asset cleanup:** Removed old README screenshots that are no longer referenced by the project or README.

#### Verified
- New server saves already refresh the page after a successful save.
- `main` and `develop` were checked before release so `develop` could be synced cleanly back to `main`.

---

## v0.35.8 - 2026-05-21

#### Added
- **JellyTag-Plus original-image option:** Added a JellyTag-Plus toggle under Library Name that appends `jellytag=off` to Jellyfin image requests, allowing Pixelfin HTML reports, embedded downloads, ZIP exports, and auto jobs to use pre-badge original images when JellyTag-Plus supports the bypass parameter

#### Changed
- **Develop Docker compose:** The build compose file now runs the development container as `pixelfin-develop` on host port `2180`

#### Notes
- The JellyTag-Plus toggle is saved per library and persists across container restarts through Pixelfin's `data/history.json`

## v0.35.7 - 2026-05-04

#### Changed
- **Specials poster naming (ZIP export):** Season 0 artwork is now saved as `specials-poster.jpg` instead of `season00-poster.jpg` to better align with common Jellyfin naming conventions

#### Fixed
- **Specials poster restore detection:** Restore engine now correctly recognizes `specials-poster.jpg` (in addition to `season00-poster.jpg`) and maps it to Season 0

#### Notes
- Backwards compatibility preserved: existing `season00-poster.jpg` files continue to be supported during restore
- No changes required for existing libraries or previously generated ZIPs

## v0.35.6 - 2026-05-03

#### Changed
- **Season poster naming (ZIP export):** Season artwork is now saved using `season01-poster.jpg` format instead of `season01.jpg` for better compatibility with common media naming conventions

#### Fixed
- **Season poster restore detection:** Restore engine now correctly recognizes `season01-poster.jpg` (in addition to legacy `season01.jpg`) when matching and applying season artwork

#### Notes
- Backwards compatibility preserved: existing `season01.jpg` files continue to be supported during restore
- No changes required for existing libraries or previously generated ZIPs

## v0.35.5 - 2026-04-28

#### Fixed
- **Windows encoding crash:** Removed non-ASCII characters (emoji and Unicode arrows) from console `print()` output to prevent `charmap` encoding errors on Windows environments

#### Changed
- **Console output standardization:** Replaced Unicode arrow (`→`) with ASCII (`->`) in log messages for cross-platform compatibility
- **Logging portability:** Ensured all backend/script console output uses ASCII-only characters for consistent behavior across Windows, Docker, and subprocess environments

#### Notes
- HTML output remains UTF-8 encoded and continues to safely use UI symbols (arrows, buttons, etc.) without compatibility issues

## v0.35.4 - 2026-04-18

#### Fixed
- **ClearArt restore mapping:** Fixed an issue where ClearArt images were not restoring correctly by mapping ClearArt to Art in restore.py, ensuring compatibility with Jellyfin’s image type handling

## v0.35.3 - 2026-04-14

#### Fixed
- **ClearArt retrieval:** Fixed an issue where ClearArt images were not being pulled from Jellyfin by correcting the mapping to `Art` (@KingOfBadgers)

#### Added
- **Gunicorn dependency:** Added `gunicorn` to `requirements.txt` to prevent Docker container startup failures due to missing executable (@KingOfBadgers)

#### Notes
- Gunicorn timeout may need to be increased for large libraries (tested with 2500+ items)

## v0.35.2 - 2026-03-31

#### Fixed
- Sizes of box, boxrear, and disc

## v0.35.1 - 2026-03-28

#### Fixed
- Reverted use of gunicorn

## v0.35.0 – 2026-03-28

#### Added
- **New sorting functionality:** Added ability to sort by date added (descending)
- **Auto feature (cron-based automation):** Introduced an automated workflow for generating HTML reports and ZIP archives on a scheduled basis using cron.
- **Restore feature:** Added the ability to restore library images directly to Jellyfin, including:
  - Dry run mode for safe previewing
  - Comparison HTML showing before-and-after images for verification
- **Keep option for outputs:** Added the ability to mark specific HTML and ZIP files as “kept,” preventing them from being deleted even when exceeding the limits set in the Auto tab.

#### Changed
- **Generate page layout:** Switched the positions of the API Key and Library fields for improved usability and flow.

#### Fixed
- **Downloaded HTML rendering:** Resolved an issue where downloaded HTML files displayed unintended extra information.
- **Development server warning:** Resolved Flask development server warning ([#7](https://github.com/nothing2obvi/pixelfin/issues/7)).

---

## v0.34.7 – 2025-10-07

#### Changed
- **Lightbox interaction:** Removed `<a>` wrapper around the lightbox image to prevent automatic downloads or opening in a new tab.
- **Click handling:** Added `e.preventDefault()` and `e.stopPropagation()` on image clicks inside the lightbox to stop default browser actions while preserving next-image cycling.

#### Fixed
- **Image click behavior:** Clicking an image in the lightbox no longer triggers a file download or opens a new browser tab.
- **Lightbox navigation:** Maintained full support for next/previous image cycling, arrow-key navigation, and closing with `Esc` or outside clicks.

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
