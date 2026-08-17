"""Verify the agent kit package and print the manifest it would serve.

This writes nothing.  The manifest and the zip are computed per request from the package
tree, so there is no artifact to regenerate -- what CI needs is a gate that fails on a
package which would be broken, leaky, or unreachable once served.
"""

import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from agentkit import packaging

FORBIDDEN_HOST = "homing.hartphoenix.com"
REQUIRED_PATHS = (packaging.INDEX_PATH, packaging.VERSION_PATH, packaging.SKILL_PATH)

# ``secrets.token_urlsafe(32)`` is 43 URL-safe base64 characters.  Anything of that shape in a
# file we publish is treated as a live credential until proven otherwise.
_LONG_RUN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{40,}(?![A-Za-z0-9_-])")
_HEX_ONLY = re.compile(r"[0-9a-fA-F]+\Z")


def _token_shaped(text):
    """Runs with the character mix of a real token; documented hex digests are exempt."""
    hits = []
    for run in _LONG_RUN.findall(text):
        if _HEX_ONLY.match(run):
            continue
        if re.search(r"[a-z]", run) and re.search(r"[A-Z]", run) and re.search(r"\d", run):
            hits.append(run[:8] + "...")
    return hits


class Command(BaseCommand):
    help = "Validate agentkit/package/ and print the manifest that would be served."

    def add_arguments(self, parser):
        parser.add_argument(
            "--origin",
            default=None,
            help="Origin substituted into the printed manifest (default: PUBLIC_BASE_URL).",
        )
        parser.add_argument(
            "--quiet", action="store_true", help="Validate only; do not print the manifest."
        )

    def handle(self, *args, **options):
        origin = (
            options["origin"] or getattr(settings, "PUBLIC_BASE_URL", "") or "http://localhost:8000"
        ).rstrip("/")

        problems = []
        sources = packaging.read_source_files()
        if not sources:
            raise CommandError(f"no package files found under {packaging.PACKAGE_ROOT}")

        for required in REQUIRED_PATHS:
            if required not in sources:
                problems.append(f"{required}: missing from the package")

        for relpath, raw in sources.items():
            if len(raw) > packaging.MAX_FILE_BYTES:
                problems.append(f"{relpath}: {len(raw)} bytes exceeds {packaging.MAX_FILE_BYTES}")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                problems.append(f"{relpath}: not valid UTF-8")
                continue
            # Checked against the raw source, never the substituted output: substitution is
            # supposed to write the deployment's own host into the served bytes.
            if FORBIDDEN_HOST in text:
                problems.append(f"{relpath}: hardcodes {FORBIDDEN_HOST}; use __HOMING_ORIGIN__")
            for hit in _token_shaped(text):
                problems.append(f"{relpath}: contains a token-shaped string ({hit})")

        try:
            package = packaging.build_fresh(origin)
        except Exception as exc:
            problems.append(str(exc))
            raise CommandError(self._report(problems))

        for relpath in package.manifest_paths:
            if not packaging.is_routable(relpath):
                problems.append(f"{relpath}: in the manifest but not reachable under /agent/pkg/")

        for relpath, content in package.files.items():
            if packaging.ORIGIN_PLACEHOLDER.encode("ascii") in content:
                problems.append(f"{relpath}: {packaging.ORIGIN_PLACEHOLDER} survived substitution")

        archive_bytes = package.manifest["archive"]["bytes"]
        if archive_bytes > packaging.MAX_ARCHIVE_BYTES:
            problems.append(
                f"{package.archive_name}: {archive_bytes} bytes exceeds "
                f"{packaging.MAX_ARCHIVE_BYTES}"
            )

        if problems:
            raise CommandError(self._report(problems))

        if not options["quiet"]:
            self.stdout.write(package.manifest_bytes.decode("utf-8").rstrip("\n"))
        self.stderr.write(
            f"ok: {len(package.manifest_paths)} files, archive {archive_bytes} bytes, "
            f"version {package.version}, origin {origin}"
        )

    def _report(self, problems):
        return "agent kit package is invalid:\n  " + "\n  ".join(problems)
