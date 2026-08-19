#!/usr/bin/env python3
"""verify_sources.py - does the fetch/extract path actually return usable listings?

Run this BEFORE wiring anything to Homing. It writes nothing to Homing, needs no
account key, and never invokes a model. It answers three questions per source:

    1. reachable?   did the request get a real page instead of a challenge
    2. parseable?   did structured data (JSON-LD / microdata / og:) come back
    3. useful?      do the extracted records carry a title AND a URL - price and
                    location are measured and shown, but not required, because
                    "price on application" is a real listing

**HTML / structured-page lanes only.** Every URL here is probed as `--channel
html`. A JSON-API candidate needs its `record_path` and field map, which live in
that source's record in sources.json, so verify it with the tool that can read
them:

    python3 sources.py probe --sources sources.json --slug <slug> --channel json --url URL

It also runs each source twice - once presenting as a browser, once as a
self-identified crawler - so the reachability difference is measured on your
own connection rather than asserted.

    python3 verify_sources.py --sources sources.json   # everything Phase 4 found
    python3 verify_sources.py --urls urls.txt      # one URL per line
    python3 verify_sources.py --url https://...    # a single source
    python3 verify_sources.py --both               # A/B the two identities
    python3 verify_sources.py --show 3             # print 3 sample records

Exit code is 0 if at least one source is USEFUL, 1 otherwise.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES = os.path.join(HERE, "sources.py")

# No built-in candidate list. Which sites matter depends entirely on where the
# person is looking, and a default set would quietly steer every search toward
# one city. Phase 4 derives candidates for the actual locale (see sources.md);
# pass them with --url/--urls, or point --sources at a sources.json.


def slug_for(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host.replace(".", "-")


def run(cmd, timeout=120):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timed out"
    out = (proc.stdout or "").strip()
    if not out:
        return None, (proc.stderr or "").strip().splitlines()[-1:] or ["no output"]
    for line in reversed(out.splitlines()):
        try:
            return json.loads(line), ""
        except ValueError:
            continue
    return None, "no JSON on stdout"


def probe(url, ua, egress, samples=0):
    """One HTML-lane probe. The channel is explicit: this tool checks that lane
    and no other, so a JSON API is not silently graded as an empty web page."""
    cmd = [sys.executable, SOURCES, "probe",
           "--url", url, "--slug", slug_for(url), "--channel", "html",
           "--install-probe", "--ua", ua, "--egress-class", egress]
    if samples:
        cmd += ["--samples", str(min(int(samples), 3))]
    rec, err = run(cmd)
    if rec is None:
        return {"status": "ERROR", "reason": err, "vendor": "", "listings": 0,
                "usable_records": 0}
    return rec


BLOCK_FAMILY = ("BLOCKED-EDGE", "BLOCKED-IP", "BLOCKED-JS", "BLOCKED-UNKNOWN",
                "LOGIN-WALL", "GEOFENCED", "SILENT-DEGRADATION", "POISONED")
NO_CONSENT = ("ROBOTS-UNAVAILABLE", "ROBOTS-DISALLOWED")


def summarize(rec):
    status = rec.get("status", "?")
    n = rec.get("listings") or 0
    bits = [status]
    if rec.get("vendor"):
        bits.append(rec["vendor"])
    if rec.get("http_status") and rec["http_status"] != 200:
        bits.append("HTTP %s" % rec["http_status"])
    if rec.get("robots_status") and rec["robots_status"] != 200:
        bits.append("robots=%s" % rec["robots_status"])
    if rec.get("bytes"):
        bits.append("%dKB" % (rec["bytes"] // 1024))
    bits.append("%d listings" % n)
    fields = rec.get("fields_present") or {}
    if n:
        bits.append("%d usable (%d priced)" % (rec.get("usable_records") or 0,
                                               fields.get("price") or 0))
    return " · ".join(str(b) for b in bits), n


def verdict(rec):
    """reachable / parseable / useful, from the probe record.

    'useful' is the only one that means integrate it, and it is checked, not
    assumed: the page came back, the structured-data extractor found listing
    nodes, and at least one of those nodes carries both a title and a URL. A
    page full of nodes that name no listing and link nowhere is parseable and
    useless, and used to be graded USEFUL on the node count alone.

    Price and location are counted and printed but not required - plenty of real
    listings publish "price on application".

    EMPTY-GENUINE is reachable and parseable but not useful for THIS query - it
    means the query matched nothing, not that the source is broken.
    """
    status = (rec.get("status") or "").upper()
    n = rec.get("listings") or 0
    usable = rec.get("usable_records") or 0
    reachable = status not in BLOCK_FAMILY and status not in NO_CONSENT \
        and status not in ("ERROR", "NETWORK-ERROR")
    parseable = reachable and status in ("OK", "EMPTY-GENUINE", "NOT-MODIFIED")
    useful = bool(parseable and n > 0 and usable > 0)
    return reachable, parseable, useful


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", action="append", default=[])
    ap.add_argument("--urls", help="file with one URL per line")
    ap.add_argument("--sources", help="a sources.json; probes each url_template")
    ap.add_argument("--both", action="store_true",
                    help="also probe as a self-identified crawler, for comparison")
    ap.add_argument("--show", nargs="?", type=int, const=3, default=0, metavar="N",
                    help="print up to N sample records (default 3) plus the diagnostic "
                         "excerpt and any redirect for each source")
    ap.add_argument("--egress-class", default=os.environ.get("HOMING_EGRESS_CLASS", "residential"))
    ap.add_argument("--delay", type=float, default=3.0, help="seconds between sources")
    args = ap.parse_args()

    urls = list(args.url)
    if args.urls:
        with open(args.urls) as fh:
            urls += [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    if args.sources:
        try:
            with open(args.sources) as fh:
                document = json.load(fh)
        except (OSError, ValueError) as exc:
            sys.exit("could not read %s: %s" % (args.sources, exc))
        for entry in document.get("sources") or []:
            template = str(entry.get("url_template") or "")
            if template and "{" not in template:
                urls.append(template)
    if not urls:
        sys.exit(
            "No candidate URLs. This tool has no built-in list on purpose - which sites\n"
            "matter depends on where the search is. Give it the candidates Phase 4 found:\n"
            "  verify_sources.py --url https://<a listing search URL>\n"
            "  verify_sources.py --urls candidates.txt\n"
            "  verify_sources.py --sources sources.json")

    if not os.path.exists(SOURCES):
        sys.exit("sources.py not found next to this script: %s" % SOURCES)

    print("Probing %d source(s) as: browser%s" % (len(urls), " and crawler" if args.both else ""))
    print("Egress class: %s" % args.egress_class)
    print("Lane: HTML / structured page only. A JSON-API source needs its record_path and")
    print("field map from sources.json - probe those with sources.py probe --channel json.")
    print("USEFUL means at least one record carried both a title and a URL.\n")

    width = max(len(slug_for(u)) for u in urls) + 2
    useful_count = 0
    rows = []

    for i, url in enumerate(urls):
        slug = slug_for(url)
        rec = probe(url, "browser", args.egress_class, args.show)
        line, n = summarize(rec)
        reachable, parseable, useful = verdict(rec)
        mark = "USEFUL " if useful else ("PARSED " if parseable else
                                         ("REACHED" if reachable else "BLOCKED"))
        print("  %-*s %s  %s" % (width, slug, mark, line))

        if args.both:
            time.sleep(args.delay)
            rec2 = probe(url, "crawler", args.egress_class, 0)
            line2, _ = summarize(rec2)
            _, _, useful2 = verdict(rec2)
            delta = "" if useful == useful2 else "   <-- differs by identity"
            print("  %-*s %s  %s%s" % (width, "", "as crawler:", line2, delta))

        if useful:
            useful_count += 1
        rows.append((slug, url, mark, n, rec))

        for sample in (rec.get("samples") or [])[:args.show]:
            print("        record: %s | %s | %s" % (sample.get("title", "")[:60],
                                                    sample.get("price", "") or "no price",
                                                    sample.get("url", "")[:80]))
        if args.show and rec.get("excerpt"):
            print("        page said: %s" % rec["excerpt"][:200])
        if args.show and rec.get("final_url") and rec["final_url"] != url:
            print("        redirected to: %s" % rec["final_url"][:200])

        if i + 1 < len(urls):
            time.sleep(args.delay)

    print("\n%d of %d source(s) returned usable listings (HTML lane; title + URL required)."
          % (useful_count, len(urls)))
    if useful_count:
        print("Integrate these:")
        for slug, url, mark, n, _ in rows:
            if mark.strip() == "USEFUL":
                print("    %-*s %s" % (width, slug, url))
    else:
        print("None returned usable listings. If every row says BLOCKED, check whether this")
        print("machine is actually on your home connection and not a VPN or a sandbox.")
    return 0 if useful_count else 1


if __name__ == "__main__":
    sys.exit(main())
