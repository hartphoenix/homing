"""Public, unauthenticated delivery of the homing-agent-kit.

An agent following the bootstrap prompt arrives with no session.  A login wall here would
hand it an HTML login page instead of the package, which is why every route below is
anonymous, cacheable, and free of ``Content-Disposition`` and ``Vary: Cookie``.  Nothing
served is user data, so nothing varies per requester beyond the deployment origin.
"""

from django.http import Http404, HttpResponse, HttpResponseNotModified
from django.views.decorators.http import require_safe

from . import packaging


def _package(request):
    return packaging.build_package(packaging.origin_for(request))


def _respond(request, content, content_type, max_age=packaging.CACHE_SECONDS):
    etag = packaging.etag_for(content)
    if packaging.etag_matches(request.headers.get("If-None-Match"), etag):
        response = HttpResponseNotModified()
    else:
        response = HttpResponse(content, content_type=content_type)
    response["ETag"] = etag
    response["Cache-Control"] = f"public, max-age={max_age}"
    return response


def _serve(request, relpath, max_age=packaging.CACHE_SECONDS):
    """Serve a package file by exact name.  Unknown names 404; no path is ever built."""
    content = _package(request).files.get(relpath)
    if content is None:
        raise Http404(relpath)
    return _respond(request, content, packaging.content_type_for(relpath), max_age)


@require_safe
def index(request):
    return _serve(request, packaging.INDEX_PATH)


@require_safe
def version(request):
    return _serve(request, packaging.VERSION_PATH, max_age=packaging.VERSION_CACHE_SECONDS)


@require_safe
def skill(request):
    return _serve(request, packaging.SKILL_PATH)


@require_safe
def reference(request, name):
    return _serve(request, f"{packaging.REFERENCES_DIR}/{name}.md")


@require_safe
def script(request, name):
    return _serve(request, f"{packaging.SCRIPTS_DIR}/{name}")


@require_safe
def manifest(request):
    package = _package(request)
    return _respond(request, package.manifest_bytes, packaging.JSON_TYPE)


@require_safe
def archive(request, number):
    package = _package(request)
    if number != package.version:
        raise Http404("unknown package version")
    return _respond(request, package.archive_bytes, packaging.ZIP_TYPE)
