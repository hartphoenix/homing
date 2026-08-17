"""Build, substitute and cache the public homing-agent-kit package.

The package source of truth is ``agentkit/package/``.  Files ship verbatim except for one
substitution: ``__HOMING_ORIGIN__`` becomes the deployment's public origin at serve time.
The manifest and the zip are derived in memory from the same substituted bytes the file
routes return, so there is no committed build artifact and therefore no way for a manifest
digest to disagree with what was served.
"""

from dataclasses import dataclass
from pathlib import Path
import hashlib
import io
import json
import zipfile

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

PACKAGE_NAME = "homing-agent-kit"
PACKAGE_ROOT = Path(__file__).resolve().parent / "package"
ORIGIN_PLACEHOLDER = "__HOMING_ORIGIN__"

INDEX_PATH = "index.md"
VERSION_PATH = "VERSION"
SKILL_PATH = "SKILL.md"
REFERENCES_DIR = "references"
SCRIPTS_DIR = "scripts"

MAX_FILE_BYTES = 256 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024
CACHE_SECONDS = 300
VERSION_CACHE_SECONDS = 3600

# Deterministic zip metadata.  The archive digest must depend on file contents alone, never on
# a checkout's mtimes or umask, or the same package would hash differently in CI and in the
# image.  Whatever installs these files sets its own modes.
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
ZIP_MODE = 0o644
ZIP_CREATE_SYSTEM_UNIX = 3
ZIP_COMPRESS_LEVEL = 6

MARKDOWN_TYPE = "text/markdown; charset=utf-8"
JSON_TYPE = "application/json"
ZIP_TYPE = "application/zip"
# Scripts are served as plain text on purpose: nothing we hand out should arrive labelled as
# something a browser or a shell would treat as executable.
TEXT_TYPE = "text/plain; charset=utf-8"


def _ignored(relative: Path) -> bool:
    return any(part.startswith(".") or part == "__pycache__" for part in relative.parts)


def _scan():
    """Walk the package once -> ``{relative posix path: (size, mtime_ns)}``, sorted."""
    entries = {}
    if PACKAGE_ROOT.is_dir():
        for path in PACKAGE_ROOT.rglob("*"):
            relative = path.relative_to(PACKAGE_ROOT)
            if not path.is_file() or _ignored(relative):
                continue
            stat = path.stat()
            entries[relative.as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return dict(sorted(entries.items()))


# The allowlist is fixed when the process starts.  Request input is only ever compared against
# these strings -- it never reaches the filesystem -- so path traversal has no surface here.
_IMPORT_SCAN = _scan()
ALLOWED_PATHS = frozenset(_IMPORT_SCAN)

_CACHE = {}


@dataclass(frozen=True)
class Package:
    origin: str
    version: int
    files: dict  # every package file, substituted; includes index.md
    manifest: dict
    manifest_bytes: bytes
    archive_name: str
    archive_bytes: bytes

    @property
    def manifest_paths(self):
        return [entry["path"] for entry in self.manifest["files"]]


def origin_for(request) -> str:
    """Public origin with no trailing slash.  Mirrors ``tracker.views._agent_api_base_url``."""
    public_base = getattr(settings, "PUBLIC_BASE_URL", "") or ""
    if public_base:
        return public_base.rstrip("/")
    return request.build_absolute_uri("/").rstrip("/")


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def etag_for(content: bytes) -> str:
    return f'"{sha256_hex(content)}"'


def etag_matches(header, etag: str) -> bool:
    """True when an ``If-None-Match`` header covers ``etag``."""
    if not header:
        return False
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate.startswith("W/"):
            candidate = candidate[2:].strip()
        if candidate == "*" or candidate == etag:
            return True
    return False


def content_type_for(relpath: str) -> str:
    if relpath.endswith(".md"):
        return MARKDOWN_TYPE
    if relpath.endswith(".json"):
        return JSON_TYPE
    return TEXT_TYPE


def is_routable(relpath: str) -> bool:
    """Whether a package path is reachable over HTTP under ``/agent/pkg/``."""
    if relpath in (VERSION_PATH, SKILL_PATH):
        return True
    parts = relpath.split("/")
    if len(parts) != 2:
        return False
    if parts[0] == REFERENCES_DIR:
        return parts[1].endswith(".md") and len(parts[1]) > len(".md")
    return parts[0] == SCRIPTS_DIR and bool(parts[1])


def read_source_files():
    """Raw, un-substituted bytes keyed by package path.  For validation, not for serving."""
    return {relpath: (PACKAGE_ROOT / relpath).read_bytes() for relpath in _scan()}


def build_package(origin: str) -> Package:
    """Return the substituted package for one origin, rebuilding only when the tree changes."""
    scan = _scan() if settings.DEBUG else _IMPORT_SCAN
    fingerprint = hashlib.sha256(repr(scan).encode("utf-8")).hexdigest()
    cached = _CACHE.get(origin)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    package = _build(origin, scan)
    _CACHE[origin] = (fingerprint, package)
    return package


def build_fresh(origin: str) -> Package:
    """Build straight from disk, bypassing the cache.  For validation tooling and tests."""
    return _build(origin, _scan())


def _build(origin: str, scan) -> Package:
    replacement = origin.encode("utf-8")
    placeholder = ORIGIN_PLACEHOLDER.encode("ascii")
    files = {
        relpath: (PACKAGE_ROOT / relpath).read_bytes().replace(placeholder, replacement)
        for relpath in scan
    }
    version = _version_of(files)
    archive_name = f"{PACKAGE_NAME}-{version}.zip"
    # index.md is the bootstrap page, served at /agent/ and read before anything else.  Leaving
    # it out of the manifest and the archive keeps one property true: every manifest path is
    # fetchable at <origin>/agent/pkg/<path>.
    members = [relpath for relpath in sorted(files) if relpath != INDEX_PATH]
    archive_bytes = _archive(files, members)
    manifest = {
        "package": PACKAGE_NAME,
        "version": version,
        "generated_for_origin": origin,
        "files": [
            {
                "path": relpath,
                "bytes": len(files[relpath]),
                "lines": len(files[relpath].splitlines()),
                "sha256": sha256_hex(files[relpath]),
            }
            for relpath in members
        ],
        "archive": {
            "path": archive_name,
            "bytes": len(archive_bytes),
            "sha256": sha256_hex(archive_bytes),
        },
    }
    manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    return Package(origin, version, files, manifest, manifest_bytes, archive_name, archive_bytes)


def _archive(files, members) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for relpath in members:
            info = zipfile.ZipInfo(relpath, date_time=ZIP_TIMESTAMP)
            info.create_system = ZIP_CREATE_SYSTEM_UNIX
            info.external_attr = ZIP_MODE << 16
            # Both are passed per entry on purpose: a ZipInfo carries its own compression
            # settings and would otherwise silently override the ZipFile's and store raw.
            archive.writestr(
                info,
                files[relpath],
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=ZIP_COMPRESS_LEVEL,
            )
    return buffer.getvalue()


def _version_of(files) -> int:
    raw = files.get(VERSION_PATH)
    if raw is None:
        raise ImproperlyConfigured("agentkit/package/VERSION is missing")
    try:
        version = int(raw.decode("utf-8").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise ImproperlyConfigured("agentkit/package/VERSION must be a single integer") from exc
    if version < 1:
        raise ImproperlyConfigured("agentkit/package/VERSION must be a positive integer")
    return version
