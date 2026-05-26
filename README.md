![Pixelfin](assets/Pixelfin.png)

<a href="https://ko-fi.com/yeahnoforsure_" target="_blank" rel="noopener noreferrer"><img src="assets/support_me_on_kofi_blue.png" alt="Support me on Ko-fi" width="240"></a>

<!-- ALL-CONTRIBUTORS-BADGE:START - Do not remove or modify this section -->
[![All Contributors](https://img.shields.io/badge/all_contributors-4-orange.svg?style=flat-square)](#contributors-)
<!-- ALL-CONTRIBUTORS-BADGE:END -->

# Pixelfin: Jellyfin Artwork Inspector

I’m a bit obsessive about Jellyfin.

One thing I’m especially obsessive about is artwork. Posters, backdrops, logos, thumbnails, ClearArt, all of it. To me, a big part of what makes a media library feel “premium” is the artwork. Consistent, high-quality images can make a collection of files feel curated, browsable, and actually enjoyable to explore. Not boasting, but I’ve gotten a lot of great feedback about, and donations for, my Jellyfin server, and I think I owe a lot of that to curating my images well.

And that last bit is thanks to Pixelfin.

Pixelfin is an app that helps you audit and review the artwork in your Jellyfin libraries. It can generate galleries of your media so you can quickly see what artwork each media item has, what it’s missing, and what might need improvement. It checks image types like Primary, Backdrop, Logo, ClearArt, Banner, Thumb, and more, and can flag images that fall below your minimum resolution threshold or exceed your maximum resolution threshold.

**The main point is pretty simple: I wanted a faster way to look at my library’s artwork and make judgment calls.**

Things like:

- This poster is too low-res.
- This movie deserves a better poster than what Jellyfin grabbed automatically.
- This backdrop technically exists, but it doesn’t represent the movie well.
- This show’s logo is missing.
- This anime has a poster, but none of the characters are on it.
- This thumbnail doesn’t have a logo on it.
- This poster doesn’t go with this backdrop.

Pixelfin gives you a summary table, and you can click a title to jump directly to that item’s gallery section. From there, you can click the gallery title to open the item in Jellyfin and make your changes.

It can also generate HTML galleries that embed your images, export a library’s artwork to ZIP files, and restore artwork back to Jellyfin from ZIP, complete with dry-run and a side-by-side comparison.

You might be thinking, why not just save images alongside the media files? That’s totally valid, but personally I don’t do that. I like having Jellyfin store images on my NVMe SSD for faster access, and the ZIP backup and restore workflow is also useful if you’re migrating servers or rebuilding things later.

The backup and restore features are nice, but the main reason Pixelfin exists is still the artwork review process. I wanted a clear, visual way to scan through a library and spot what needs attention.

**Disclaimer:** Pixelfin is vibecoded with Codex. I use it, it works well for me, and other people have found it useful too. That said, it’s still a local tool built by someone solving a very specific Jellyfin artwork problem, not a hardened production app.

**Security Note:** Pixelfin is not built with security in mind. Run it locally only, or on a trusted private network. Don’t expose it to the internet.

## What Changed in v1.0.0

Pixelfin v0.35.8 was mainly a Flask app around Python scripts that generated static HTML reports.

Pixelfin v1.0.0 turns Pixelfin into a proper local, server-backed web app. It keeps the core Jellyfin and Python logic, but adds cached scan state, dynamic library views, settings, restore review screens, and per-item updates.

You can now scan, filter, inspect, export, restore, and adjust rules from one interactive UI, while still keeping the classic HTML and ZIP exports when you want portable files.

Breaking or migration notes:

- The new app is now the default page at `http://localhost:1280`.
- The older classic interface is still available at `/classic`.
- v1.0.0 stores its server, library, scan, and settings state in `data/fresh.db`.
- Older v0.35.8 files like `data/history.json`, old generated HTML files, and old ZIP exports are not deleted.
- Existing exports under `output/<Library>/` stay where they are. Pixelfin can still list and download generated files from `output/`, but the new app uses its own settings and scan cache.

## Screenshots

### Light Mode
![Light Mode](assets/screenshots/pixelfin-light.png)

### Dark Mode
![Dark Mode](assets/screenshots/pixelfin-dark.png)

### Table
![Table](assets/screenshots/pixelfin-table.png)

### All Images: Compact, Missing, Low Resolution, and High Resolution Images
![All Images: Compact, Missing, Low Resolution, and High Resolution Images](assets/screenshots/pixelfin-all-images-compact-missing-low-high.png)

### All Images: Full, Missing, Low Resolution, and High Resolution Images
![All Images: Full, Missing, Low Resolution, and High Resolution Images](assets/screenshots/pixelfin-all-images-full-missing-low-high.png)

### Select Images: Compact, Low Resolution and High Resolution Images
![Select Images: Compact, Low Resolution and High Resolution Images](assets/screenshots/pixelfin-select-images-compact-low-high.png)

### Select Images: Full, Missing, Low Resolution, and High Resolution Images
![Select Images: Full, Missing, Low Resolution, and High Resolution Images](assets/screenshots/pixelfin-select-images-full-missing-low-high.png)

### Select Images: Full, Complete
![Select Images: Full, Complete](assets/screenshots/pixelfin-select-images-full-complete.png)

### Season Posters
![Season Posters](assets/screenshots/pixelfin-season-posters.png)

### Lightbox
![Lightbox](assets/screenshots/pixelfin-lightbox.png)

### Restore
![Restore](assets/screenshots/pixelfin-restore.png)

### Restore Review (Dry-Run)
![Restore Review](assets/screenshots/pixelfin-restore-review.png)

### Comparison (Dry-Run)
![Comparison (Dry-Run)](assets/screenshots/pixelfin-comparison.png)

## Features

### Local Web App

- Add one or more Jellyfin servers.
- Sync Jellyfin libraries.
- Hide libraries you don’t want Pixelfin to show.
- View libraries as cards with Jellyfin thumbnails.
- See how many media items need attention.
- Click a library to review every scanned media item.
- Click a count to view only media items with missing or low-resolution artwork.
- Click a media item title to open that item in Jellyfin and take action.
- Use light or dark mode.

### Artwork Review

Pixelfin can show a summary table and visual gallery for each media item. It can flag missing images and images below your minimum resolution rules.

Supported image types include:

- Art
- Backdrop
- Banner
- Box
- BoxRear
- Disc
- Logo
- Menu
- Primary
- Season Posters
- Thumb

You can use the Full layout when you want the classic big visual review, or Compact layout when you want to scan quickly.

### Supported Jellyfin Library Types

Based on the current code, Pixelfin supports these Jellyfin library types:

- Series
- Movies
- Music
- Music Videos
- Collections

Live TV and Playlists are intentionally hidden because Pixelfin can’t work with those images correctly yet.

### Scanning and Updates

Pixelfin uses cached scan state so it doesn’t have to request every image from Jellyfin constantly. That keeps the app faster and helps avoid hammering Jellyfin while real clients are using it.

Because of that, changes made in Jellyfin don’t appear instantly. After changing artwork in Jellyfin, use one of these:

- `Update` on a single media item.
- `Refresh` while viewing a library.
- `Scan` from a library card.
- `Scan All Libraries` from the main screen.

### Image Rules

You can choose which image types matter for each library and set minimum resolution thresholds. There are global defaults, and you can override rules per library.

Season Posters are treated as a special image type. Pixelfin checks each season poster, and season posters inherited from the series primary image are counted as missing.

Pixelfin can also opt into extra checks:

- High Resolution, useful if you want to catch oversized images.

### Keyboard Shortcuts

- `\`, show keyboard shortcuts.
- `t`, scroll to top.
- `a`, open All Tasks.
- `9`, toggle Full or Compact layout.
- `0`, toggle light or dark mode.
- `c`, hide or show complete items when a library listing is open.
- `s`, toggle sorting by title or date added when a listing is open.
- `Esc`, close popups and lightboxes.
- `Left` / `Right`, move between images in lightbox.
- `Up` / `Down`, move between media items in lightbox.

### JellyTag-Plus

If you use [JellyTag-Plus](https://github.com/nothing2obvi/jellyfin-plugins/tree/main/Jellytag), Pixelfin can request original images with `jellytag=off`. This applies to the main app display, scans, exports, and restore comparison previews.

### HTML Gallery Generation

Pixelfin can still generate classic-style HTML galleries. This is useful when you want a portable report outside the live app.

The generated HTML includes:

- A summary table.
- Gallery views of each media item.
- Missing image placeholders.
- Low-resolution warnings.
- Links back to Jellyfin items.
- Embedded image download support, so the HTML can be saved as a self-contained file.

### ZIP Export

Pixelfin can export selected artwork to ZIP. When you download a ZIP, Pixelfin also saves it internally under `output/<Library>/`.

ZIP exports are useful if you want to:

- Back up artwork outside Jellyfin.
- Move artwork to another server.
- Keep a snapshot before rebuilding.
- Restore artwork later.
- Share a set of images without saving artwork next to media files.

Some users save artwork alongside media files, and that’s totally valid. I personally don’t, because I like having Jellyfin store images on my NVMe SSD for faster access. ZIP backup and restore gives me a separate way to preserve artwork without changing how my media folders are organized.

### ZIP Restore

Pixelfin can restore artwork from a Pixelfin ZIP or an external ZIP.

The restore process includes:

- Dry-run mode.
- Match scores.
- Manual match selection for uncertain matches.
- Per-item include checkboxes.
- Per-image-type restore toggles.
- Side-by-side comparison in the Pixelfin UI.
- Filename override fields if your ZIP uses custom filenames.
- A full restore step after review.

Dry run means Pixelfin reviews what it would do without uploading anything to Jellyfin.

## Running Locally Without Docker

Requirements:

- Python 3.9 or newer.
- Pip.
- A Jellyfin server.
- A Jellyfin API key.

Install dependencies:

```bash
pip install -r requirements.txt
```

Run Pixelfin:

```bash
python app.py
```

Open:

```text
http://localhost:1280
```

If that doesn’t work, try:

```text
http://<local-ip>:1280
```

## Running with docker-compose-build.yml

Use this when you want Docker to build the image locally from this repository.

```bash
docker compose -f docker-compose-build.yml up -d --build
```

Then open:

```text
http://localhost:1280
```

The compose file uses relative folders:

- `./data:/app/data`
- `./output:/app/output`
- `./assets:/app/assets`

`data/` stores Pixelfin settings and scan state. `output/` stores generated HTML and ZIP files.

## Running with docker-compose.yml

Use this when you want the published Docker image from GitHub Container Registry.

```bash
docker compose up -d
```

The default image is:

```text
ghcr.io/nothing2obvi/pixelfin:latest
```

The current release image is:

```text
ghcr.io/nothing2obvi/pixelfin:v1.0.4
```

Open:

```text
http://localhost:1280
```

## Docker Images

The current Docker release publishes multi-architecture images for:

- `linux/amd64`
- `linux/arm64`

Tags:

- `ghcr.io/nothing2obvi/pixelfin:v1.0.4`
- `ghcr.io/nothing2obvi/pixelfin:latest`

## Useful Notes

- Pixelfin talks to Jellyfin through the Jellyfin API.
- Large libraries can take time to scan.
- Full library scans request many images from Jellyfin, so don’t run them constantly if your server is busy.
- The app is designed for one local user.
- The classic page is still available at `/classic` for now.
- `output/` can grow if you generate a lot of exports. The Auto settings can limit how many ZIPs are kept per library.

## Want to Contribute?

Pixelfin is a practical little tool that grew out of me wanting my Jellyfin artwork to look better. If you see a bug, have a better way to structure something, or want to improve the UI, pull requests and issues are welcome.

Some ideas:

- Better bulk review workflows.
- Better progress reporting.
- More per-library defaults.
- Smarter artwork quality checks.

## License

MIT, feel free to use, modify, and share.

## Contributors ✨

Thanks goes to these wonderful people ([emoji key](https://allcontributors.org/docs/en/emoji-key)):

<!-- ALL-CONTRIBUTORS-LIST:START -->
<!-- prettier-ignore-start -->
<!-- markdownlint-disable -->
<table>
  <tbody>
    <tr>
      <td align="center" valign="top" width="14.28%"><a href="https://github.com/LoV432"><img src="https://avatars.githubusercontent.com/u/60856741?v=4?s=100" width="100px;" alt="LoV432"/><br /><sub><b>LoV432</b></sub></a><br /><a href="https://github.com/nothing2obvi/pixelfin/commits?author=LoV432" title="Code">💻</a></td>
      <td align="center" valign="top" width="14.28%"><a href="https://github.com/avassor"><img src="https://avatars.githubusercontent.com/u/10287940?v=4?s=100" width="100px;" alt="avassor"/><br /><sub><b>avassor</b></sub></a><br /><a href="#ideas-avassor" title="Ideas, Planning, & Feedback">🤔</a> <a href="https://github.com/nothing2obvi/pixelfin/pulls?q=is%3Apr+reviewed-by%3Aavassor" title="Reviewed Pull Requests">👀</a></td>
      <td align="center" valign="top" width="14.28%"><a href="https://github.com/KingOfBadgers"><img src="https://avatars.githubusercontent.com/KingOfBadgers?s=100" width="100px;" alt="KingOfBadgers"/><br /><sub><b>KingOfBadgers</b></sub></a><br /><a href="https://github.com/nothing2obvi/pixelfin/commits?author=KingOfBadgers" title="Code">💻</a></td>
      <td align="center" valign="top" width="14.28%"><a href="https://github.com/pnthach95"><img src="https://avatars.githubusercontent.com/pnthach95?s=100" width="100px;" alt="pnthach95"/><br /><sub><b>pnthach95</b></sub></a><br /><a href="https://github.com/nothing2obvi/pixelfin/commits?author=pnthach95" title="Code">💻</a></td>
    </tr>
  </tbody>
</table>

<!-- markdownlint-restore -->
<!-- prettier-ignore-end -->

<!-- ALL-CONTRIBUTORS-LIST:END -->

This project follows the [all-contributors](https://github.com/all-contributors/all-contributors) specification. Contributions of any kind are welcome.
