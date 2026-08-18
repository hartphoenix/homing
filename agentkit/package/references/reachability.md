# Reachability

Answering, refusing, or genuinely empty. The classifier also runs on every scheduled fetch —
**in code, never in the model**.

All measurements dated **2026-08-17**, from **datacenter egress, US Northeast**, honest
User-Agent. Every block below was a real HTTP response from the site's edge carrying that
vendor's own headers.

**Tooling.** `scripts/verify_sources.py --help` first, then run it against candidate listing
URLs (`--url`, repeatable, or `--urls file.txt`). It reports REACHED / PARSED / USEFUL per
source, optionally A/B-tests the browser and crawler identities with `--both`, writes nothing to
Homing, needs no account key, and invokes no model — the tool for deciding whether a source is
worth integrating before it goes into `sources.json`.

---

## 1. The classifier, two layers

Runtimes differ: Tier A (curl/fetch/requests) gives status + headers + body; Tier B (markdown
fetch tools, most MCP fetchers) gives body only, headers never; Tier C (a real browser) gives a
rendered DOM, no headers. **Header rules when available; body-marker rules always.** Never write
a check that assumes headers exist.

**Header tells — first match wins.**

| Vendor | Deterministic tell | Class |
|---|---|---|
| PerimeterX / HUMAN | `x-px-blocked: 1`; `set-cookie: _px3`/`_pxhd`/`_pxvid` | BLOCKED-EDGE |
| DataDome | `server: DataDome`; `x-datadome: protected`; `x-datadome-cid`; `set-cookie: datadome=` | BLOCKED-EDGE |
| AWS WAF / CloudFront | `x-amzn-waf-action: challenge`\|`captcha` — **regardless of status** | BLOCKED-EDGE |
| Cloudflare | `cf-mitigated:` anything; or `server: cloudflare` + `cf-ray` + 4xx; `set-cookie: __cf_bm=` | BLOCKED-EDGE |
| Akamai Bot Manager | `server: AkamaiGHost`; `akamai-grn:`; `_abck=`; `bm_sz=`; `akaalb_*` | BLOCKED-EDGE |
| Kasada | `set-cookie: KP_UIDz` / `KP_UIDz-ssn` | BLOCKED-EDGE |
| Imperva / Incapsula | `x-iinfo:`; `incap_ses_*`; `visid_incap_*` | BLOCKED-EDGE |
| F5 BIG-IP / Shape | `set-cookie: TS<8-hex>=`; `BIGipServer*` | BLOCKED-EDGE |
| Generic CloudFront deny | `x-cache: Error from cloudfront` + `<TITLE>ERROR</TITLE>` + <2 KB (919 B on redfin.com, remax.com) | BLOCKED-EDGE |
| Real rate limit | 429/503 **with** `retry-after` or `ratelimit-*` **and no vendor tell** | RATE-LIMITED |
| Geofence | `Location` to a country path nobody asked for; a `Country`/geo cookie being set; 451 | GEOFENCED |

**Body markers — case-insensitive substring, always run.** `just a moment` ·
`/cdn-cgi/challenge-platform` · `enable javascript and cookies to continue` · `px-captcha` ·
`_pxAppId` · `_pxOnError` · `geo.captcha-delivery.com` · `datadome` · `errors.edgesuite.net` ·
`access denied` + `reference #<hex>.<hex>` · `window.KPSDK` · `_Incapsula_Resource` ·
`incapsula incident id` · `request unsuccessful` · `pardon our interruption` ·
`verify you are human` · `are you a robot` · `unusual traffic`

**Other classes.** *BLOCKED-IP*: 403, tiny body, **no** vendor tell, and robots.txt from the same
host returns 200 permitting the path — policy says yes, network says no. That combination is an
IP-reputation block and the strongest possible signal to move the lane to the user's machine.
*BLOCKED-JS*: 200 `text/html`, 1–60 KB, zero JSON-LD, zero `og:title`, zero currency matches,
plus a `<div id="root">`/`<div id="__next">`/`<script type="module">`/`loading` in `<title>`
(century21.com: 4,154 B, `<title>C21 loading...</title>`). *LOGIN-WALL*: only when a real
`type="password"` field is present. *POISONED/TARPIT*: identical normalized body hash for two
structurally different queries.

---

## 2. The liar statuses

Status is not the signal. Four measured shapes that fool every naive check:

| Measured | Looks like | Actually |
|---|---|---|
| `realtor.com` → **429**, no `Retry-After`, `set-cookie: KP_UIDz` | rate limiting; retry later | Kasada block. Retrying deepens it. |
| `immobilienscout24.de` → **401**, `Robot` ×6 in body, no login form anywhere | auth required; go get credentials | BLOCKED-EDGE wearing a 401. There is no login to do. |
| `compass.com` → **202**, `content-length: 0`, `x-amzn-waf-action: challenge` | success — `response.ok` is `true` | A block wearing a 2xx. Also on `huduser.gov`, a US government site. |
| `rightmove.co.uk` → 307 → **200**, 145 KB of chromed HTML titled *"We couldn't find the place you were looking for"* | a working page with no matches | Soft-404. Only the final URL and the absent listing objects give it away. |

```
429 + (retry-after or ratelimit-*) + no vendor tell   -> RATE-LIMITED (back off, one retry)
429 + vendor tell                                     -> BLOCKED-EDGE (zero retries)
429 + no retry-after + no ratelimit-* + body < 25 KB  -> BLOCKED-EDGE (assume the worst)
401/403 + no password field + (vendor tell or tiny)   -> BLOCKED-EDGE, not LOGIN-WALL
any status + x-amzn-waf-action                        -> BLOCKED-EDGE
```

Challenge-page sizes measured today: 0 B, 392, 414, 744, 771, 773, 919, 2,399, 3,382,
5,571–5,887, and one 19,633 B Kasada page. Real listing pages: 440 KB – 1.42 MB. Use ~25 KB as
"suspiciously small" and **always pair it with "zero listing objects"** — never size alone.

---

## 3. EMPTY vs BLOCKED — the calibration that matters most

A silent zero is a lie to someone looking for a home. "There's nothing out there for you" and
"I was turned away at the door" look identical as `0 leads`, and only one is true.

**Never infer emptiness from the absence of listings. Infer it from the presence of the site's
own empty-state shell.** Measured on Zumper:

| | Brooklyn (real results) | Sunflower KS (truly empty) | Compass (blocked) |
|---|---|---|---|
| status | 200 | 200 | **202** |
| bytes | 1,416,756 | 83,904 | **0** |
| `"@type":"RealEstateListing"` | 50 | **0** | 0 |
| `"@type":"BreadcrumbList"` | present | **present** | absent |
| `application/ld+json` blocks | 4 | **1** | 0 |
| `<title>` | "…2,205 Rentals Updated Daily \| Zumper" | "Apartments for Rent - Zumper" | — |

Columns 2 and 3 both yield zero listings. Column 2 is reportable; column 3 is not. The
distinguishing evidence is the `BreadcrumbList` and the surviving JSON-LD block — the site's own
shell, still there, telling you it answered and had nothing.

**Calibration, once per source at install, re-run on `probe_generation` bump:**

1. Fetch a **control query known to return many results** (the densest locality in the search
   region) and a **control query known to return none** (an absurdly tiny locality, or an
   impossible filter such as a rural county at a luxury price floor).
2. Record into `fingerprint`:
   - `shell_markers` — 2–4 strings present in **both** responses and absent from any challenge
     page. Good: `"@type":"BreadcrumbList"`, footer text, a nav link path, the
     `<link rel="canonical">` prefix. Bad: anything that only appears when there are results.
   - `listing_selector` — the counting rule: `"@type":"RealEstateListing"` count, an
     `itemtype=".../Offer"` count, or an RSS `<item>` count.
   - `min_ok_bytes` — ~50% of the **empty** page's size. Zumper: 83,904 → ~42,000.
3. Store it with the `egress_class` it was measured in.

**Classify every run, in this order:**

```
vendor_header or vendor_body_marker              -> BLOCKED-EDGE
final_url outside the requested URL path family  -> SILENT-DEGRADATION (soft-404)
bytes < min_ok_bytes                             -> BLOCKED (unknown flavor)
none of shell_markers present                    -> BLOCKED (unknown flavor)
all shell_markers present and listings == 0      -> EMPTY-GENUINE
body_hash == last run's and the query changed    -> POISONED/TARPIT
otherwise                                        -> OK
```

**`EMPTY-GENUINE` is the only zero reportable as "nothing new." Every other zero reports as
"couldn't check this source."** These must never collapse into one state. The run's `complete`
payload carries per-source `ok | empty | blocked | skipped`, and the UI says "3 sources checked,
1 unreachable" — never an unqualified "0 new leads."

---

## 4. Legitimate fallback ladder

Rung 0, every class: read `robots.txt` and `/llms.txt` and obey them, per RFC 9309 §2.3.1. A 4xx
on the robots.txt request itself — including a CDN's own 403, e.g. apartments.com — means
**unavailable**: no restrictions apply, proceed at the normal polite rate. A 5xx, a network
failure, or a 200 whose body isn't `text/plain`/`text/x-robots` (a challenge page standing in for
robots.txt, e.g. hotpads.com) means **unreachable**: treat it as a temporary full disallow and
skip the source until it is re-probed. Neither shape is the publisher expressing a policy — both
are the edge filtering the fetch before it ever reaches the publisher's own rules.

| Class | In preference order |
|---|---|
| **BLOCKED-EDGE** | 1. A machine path the site explicitly permits (`llms.txt`, a sanctioned sitemap or feed) — a site may block `/search` and still want its sitemap crawled. 2. Sitemap with `lastmod`; gunzip `.xml.gz` in code. 3. **The site's own saved-search email alert** — the highest-value rung and the one a naive setup never reaches for; it converts a blocked source into a consented, ToS-clean push source. 4. Legitimate syndication elsewhere: an open aggregator, the managing agency's site, the landlord's site. 5. A licensed data provider that states its data source — not a reseller of scraped data. 6. A publisher-provided MCP server the user installs. 7. **Tell the user this source is not reachable from here**, with a prepared deep link and a 20-second instruction. |
| **BLOCKED-IP** | 1. Run the lane from the user's machine — only an origin change fixes a reputation block. 2. Email alerts. 3. Tell the user this source is not reachable from here. |
| **BLOCKED-JS** | 1. JSON-LD or `og:` tags the SPA still ships server-side. 2. Detail pages from the sitemap — often server-rendered when search is not. 3. The user's own browser on their own machine at human pace (no stealth plugins; if the rendered page still shows a challenge, stop). 4. Email alerts. 5. Tell the user. **Never** call the internal JSON API — robots-disallowed on every major site checked. |
| **LOGIN-WALL** | 1. Stop. Do not authenticate, collect a password, or harvest browser cookies. 2. Ask the user to set up that site's email alert while logged in — the sanctioned way for a logged-out system to receive logged-in inventory. 3. A proper OAuth flow if the site publishes a consumer API. 4. Mark `requires-user` permanently and tell the user. |
| **GEOFENCED** | 1. Run from the user's machine if they are in-region — a genuine fix, not a workaround. 2. Use the region's local portals instead. 3. Tell the user. **Never** a VPN or proxy on the user's behalf. |
| **RATE-LIMITED** | 1. Honour `Retry-After` exactly; if absent, back off from 60 s, at most one further attempt this run. 2. Permanently drop that host to concurrency 1 and raise its interval; persist it. 3. Cut volume: `lastmod` diffing instead of page fetches; detail pages only for candidates that survived the filter. |

Never on any rung: CAPTCHA solving or bypass, proxy/VPN/IP rotation, fingerprint spoofing, a
browser UA from a non-browser client, replaying harvested challenge cookies, or paying a service
whose product is one of those.

---

## 5. Retirement ladder

```
per source, per run:
  1 attempt.
  +1 retry ONLY if RATE-LIMITED-real (honour Retry-After) or a bare network error.
  0 retries for BLOCKED-EDGE / BLOCKED-IP / LOGIN-WALL / GEOFENCED / BLOCKED-JS.

per source, across runs:
  1st block -> mark blocked, record vendor + date, skip 7 days
  2nd block -> skip 30 days, surface once, in plain words:
               "<site> doesn't allow automated checking. Want their email alerts
                instead, or should I just remind you to look?"
  3rd block -> retire. Only a user action re-enables it.
  never probe a blocked source more than once per scheduled run.
```

Persist `{slug, status, vendor, last_checked, next_eligible, consecutive_blocks,
egress_class_measured}` in `<state>/sources-state.json`, seeded at install from the matching
fields in `sources.json` — which is `0400` and is never rewritten by a run.
**This table is the single highest-leverage token optimization
in the system** — it turns a permanently failing source from 365 recurring costs a year into one
skipped line.

**Probe results are stored with the egress class they were measured in, and a runtime whose
egress class differs re-probes.** A source unreachable from a cloud egress may answer fine from
the user's home IP, and the reverse happens too. Never carry a datacenter verdict onto a laptop.

---

## 6. Egress self-test

Why the same request succeeds from a laptop and 403s from a cloud VM: the refusal is a
**reputation decision made before your request is read**. Edge vendors label every IP prefix
`hosting`/`vpn`/`proxy`/`residential`/`mobile`, and the default policy on a high-value listings
site is to challenge or block every hosting ASN. A cloud egress IP also carries the scraping
history of everyone else on that platform — measured today, `huduser.gov`, a US government
dataset page with no commercial reason to block anyone, returned `202 x-amzn-waf-action:
challenge`. Nothing about that request was suspicious except where it came from. A home
connection wins on ASN class, shared-IP history and client fingerprint at once, honestly and for
free. **A cloud agent cannot be made to look residential without lying**, so the question is
never "how do I unblock the cloud worker" — it is "which work belongs where."

Test the runtime, not a housing site:

```bash
curl -s --max-time 10 https://ipinfo.io/json
# .privacy.hosting == true                                          -> DATACENTER (definitive)
# .org/.asn matches Amazon|Google|Microsoft|DigitalOcean|Hetzner|
#   OVH|Linode|Oracle|Vultr|Scaleway|Cloudflare|Akamai|Fastly        -> DATACENTER
# .org matches a consumer ISP (Comcast|Spectrum|Verizon|AT&T|Cox|
#   BT|Virgin|Telekom|Orange|Vodafone|Jio|Airtel|…)                  -> RESIDENTIAL
# unreachable or ambiguous                                           -> UNKNOWN
```

If `ipinfo.io` is unavailable, check the egress IP's PTR record for hosting-provider tokens;
failing that record `unknown`. **Do not use a listing site as a canary.** Repeatedly probing a
host that already refused you is exactly the behaviour that deepens the block, and the
per-source probe yields the same information as a side effect of work you were doing anyway.

Store `egress_class` in `config.json` and re-test **weekly**, never per run — reputation drifts
and providers reassign IPs, but not hourly. Treat `unknown` as `datacenter` for planning: it is
the conservative assumption and costs nothing if wrong.

---

## 7. Anti-patterns that waste tokens

| Anti-pattern | Why it costs |
|---|---|
| Retrying a challenge | A PerimeterX 403 is deterministic on the same IP + fingerprint. Ten retries = ten identical 5,878-byte challenge pages, ~15 K tokens if any reach context, and a worse reputation score. One attempt, classify, stop. |
| Re-fetching known-blocked sources every run | Without the block table, a daily job burns six dead sources 365 times a year. |
| Feeding challenge HTML to the model | The classifier is a substring match. **Never ask a model "is this blocked?"** Truncate any non-OK body to 200 characters before logging; never let it into context. |
| Fetching whole pages when a feed exists | Brooklyn on Zumper is 1.42 MB of HTML; the 50 JSON-LD objects inside it are tens of KB; a sitemap `lastmod` diff is smaller still. |
| LLM-parsing HTML that carries JSON-LD | Extraction order is **`application/ld+json` → microdata (`itemtype`) → `og:`/`twitter:` meta → RSS/Atom fields → only then a model, on a trimmed fragment.** Raw megabyte HTML into a model is the single largest avoidable cost here, and it is *less* accurate than reading the JSON the site published for exactly this purpose. |
| Re-extracting unchanged pages | Store ETag/Last-Modified and a body hash per URL; send the conditional headers; a 304 costs almost nothing. |
| Fetching detail pages for leads that will be filtered out | Filter on search-result JSON-LD (price, beds, geo) first; fetch detail only for genuinely new survivors. |
| Treating a 2xx as success | `202` + 0 bytes + `x-amzn-waf-action` passes `response.ok`; so does a 145 KB soft-404. Always run §3. |
| Ignoring `Content-Encoding` on sitemaps | `sitemap.xml.gz` arrives as `application/x-gzip`. Gunzip in code; never hand binary to a model. |
| Hitting the internal JSON API because it is easier | Robots-disallowed on every major site checked. Short-term win, ToS violation, first thing to get banned. |
| Rediscovering the internet every run | Reachability is established **once, at install**, stored per `(source, runtime)`. The runtime reads a table; it does not explore. |
| Reporting "0 new leads" when sources were unreachable | Not a token failure — a product failure. See §3. |
