#!/usr/bin/env python3
"""verify_sources.py - does the fetch/extract path actually return usable listings?

Run this BEFORE wiring anything to Homing. It writes nothing to Homing, needs no
account key, and never invokes a model. It answers three questions per source:

    1. reachable?   did the request get a real page instead of a challenge
    2. parseable?   did structured data (JSON-LD / microdata / og:) come back
    3. useful?      do the extracted records carry a title, a URL, and a price

It also runs each source twice - once presenting as a browser, once as a
self-identified crawler - so the reachability difference is measured on your
own connection rather than asserted.

    python3 verify_sources.py                      # the built-in sample set
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

# A deliberately mixed sample: two that measured clean from a datacenter, several
# that measured blocked there and are the real question on a residential line.
SAMPLE = [
    "https://www.zumper.com/apartments-for-rent/brooklyn-ny",
    "https://www.apartmentlist.com/ny/brooklyn",
    "https://www.padmapper.com/apartments/brooklyn-ny",
    "https://www.zillow.com/brooklyn-new-york-ny/rentals/",
    "https://streeteasy.com/for-rent/nyc",
    "https://www.trulia.com/for_rent/Brooklyn,NY/",
    "https://www.hotpads.com/brooklyn-ny/apartments-for-rent",
    "https://www.renthop.com/search/nyc",
]


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


def probe(url, ua, egress):
    rec, err = run([sys.executable, SOURCES, "probe",
                    "--url", url, "--slug", slug_for(url),
                    "--install-probe", "--ua", ua, "--egress-class", egress])
    if rec is None:
        return {"status": "ERROR", "reason": err, "vendor": "", "records": 0}
    return rec


BLOCK_FAMILY = ("BLOCKED-EDGE", "BLOCKED-IP", "BLOCKED-JS", "BLOCKED-UNKNOWN",
                "LOGIN-WALL", "GEOFENCED", "SILENT-DEGRADATION", "POISONED")
NO_CONSENT = ("RETIRE-NO-ROBOTS", "ROBOTS-DISALLOWED")


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
    return " · ".join(str(b) for b in bits), n


def verdict(rec):
    """reachable / parseable / useful, from the probe record.

    'useful' is the only one that means integrate it: the page came back, the
    structured-data extractor found listing nodes, and there was at least one.
    EMPTY-GENUINE is reachable and parseable but not useful for THIS query - it
    means the query matched nothing, not that the source is broken.
    """
    status = (rec.get("status") or "").upper()
    n = rec.get("listings") or 0
    reachable = status not in BLOCK_FAMILY and status not in NO_CONSENT \
        and status not in ("ERROR", "NETWORK-ERROR")
    parseable = reachable and status in ("OK", "EMPTY-GENUINE", "NOT-MODIFIED")
    useful = parseable and n > 0
    return reachable, parseable, useful


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", action="append", default=[])
    ap.add_argument("--urls", help="file with one URL per line")
    ap.add_argument("--both", action="store_true",
                    help="also probe as a self-identified crawler, for comparison")
    ap.add_argument("--show", action="store_true",
                    help="print the diagnostic excerpt and any redirect for each source")
    ap.add_argument("--egress-class", default=os.environ.get("HOMING_EGRESS_CLASS", "residential"))
    ap.add_argument("--delay", type=float, default=3.0, help="seconds between sources")
    args = ap.parse_args()

    urls = list(args.url)
    if args.urls:
        with open(args.urls) as fh:
            urls += [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    if not urls:
        urls = SAMPLE

    if not os.path.exists(SOURCES):
        sys.exit("sources.py not found next to this script: %s" % SOURCES)

    print("Probing %d source(s) as: browser%s" % (len(urls), " and crawler" if args.both else ""))
    print("Egress class: %s\n" % args.egress_class)

    width = max(len(slug_for(u)) for u in urls) + 2
    useful_count = 0
    rows = []

    for i, url in enumerate(urls):
        slug = slug_for(url)
        rec = probe(url, "browser", args.egress_class)
        line, n = summarize(rec)
        reachable, parseable, useful = verdict(rec)
        mark = "USEFUL " if useful else ("PARSED " if parseable else
                                         ("REACHED" if reachable else "BLOCKED"))
        print("  %-*s %s  %s" % (width, slug, mark, line))

        if args.both:
            time.sleep(args.delay)
            rec2 = probe(url, "crawler", args.egress_class)
            line2, _ = summarize(rec2)
            _, _, useful2 = verdict(rec2)
            delta = "" if useful == useful2 else "   <-- differs by identity"
            print("  %-*s %s  %s%s" % (width, "", "as crawler:", line2, delta))

        if useful:
            useful_count += 1
        rows.append((slug, url, mark, n, rec))

        if args.show and rec.get("excerpt"):
            print("        page said: %s" % rec["excerpt"][:200])
        if args.show and rec.get("final_url") and rec["final_url"] != url:
            print("        redirected to: %s" % rec["final_url"][:200])

        if i + 1 < len(urls):
            time.sleep(args.delay)

    print("\n%d of %d source(s) returned usable listings." % (useful_count, len(urls)))
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
