# 📜 Changelog

All notable changes to this project will be documented in this file.  
This project follows [Semantic Versioning](https://semver.org/).

---

## v0.32 — 2025-08-31
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
