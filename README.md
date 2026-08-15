# September sublet tracker

Open `index.html` in any modern browser. No install or server is required. Interested checkboxes and trash state persist in that browser via `localStorage`.

## Search brief

- Dates: September 1–30, 2026; very little move-in/out flexibility.
- Preferred areas: near 39 Saint Felix St, Brooklyn, or 124th St & Manhattan Ave.
- Workplace: 302 W 124th St; radically different areas are okay if the commute is about 45 minutes or less.
- Entire 1-bedroom: $2,500/month or less.
- Shared apartment: ideally below $2,000; roommates must be vetted.
- Guests / someone crashing must be possible.
- Parks are a plus.
- Do **not** reject a lead because dates, guests, roommates, commute, or another criterion is unknown. Reject it only when a listing explicitly contradicts a hard requirement (for example, a 3-month minimum or price above $2,500 with no cheaper option).

## Adding a listing

Edit `listings.js` and append an object inside `window.SUBLET_LISTINGS`. Copy this schema:

```js
{
  id: "source-stable-listing-id", // unique; never change after publishing
  title: "Short descriptive title",
  price: "$1,800/mo",
  location: "123 Example St · Harlem, Manhattan",
  dates: "Sep 1–30 confirmed",
  type: "shared", // exactly "shared" or "entire"
  dateFit: "strong", // "strong" or "verify"
  source: "Source name",
  url: "https://direct-link-to-the-listing.example/123",
  summary: "Two or three informative sentences with key attributes and transit context.",
  unknowns: ["Confirm guest policy.", "Confirm exact move-out date."],
  parks: "Near Morningside Park",
  added: "2026-08-14"
}
```

Use a source's permanent listing number in `id` when possible. IDs connect each card to its saved interested/trash state; changing an existing ID loses that card's saved state. Before adding:

1. Open the direct URL and confirm it is still a real listing (not a search-results page).
2. Record only facts visible in the listing. Put inference and missing facts in `unknowns`.
3. Use `dateFit: "strong"` only when a one-month September stay is explicitly supported or needs at most a one-day adjustment. Otherwise use `"verify"`.
4. Do not duplicate a URL or listing ID already in the file.
5. Keep the advertised base price at or below $2,500. Clearly disclose mandatory fees or a price range in `price`/`unknowns`.
6. After saving, reopen `index.html`, search for the new title, test its link, interest checkbox, delete, and restore actions.

## Files

- `listings.js` — data only; this is normally the only file a future search agent edits.
- `app.js` — rendering, filters, local persistence, trash/restore behavior.
- `styles.css` — presentation.
- `index.html` — page structure.

Research snapshot: August 14, 2026. Listing availability and prices can change without notice; contact hosts before relying on any lead.
