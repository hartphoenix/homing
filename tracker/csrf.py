"""Small, non-bypass CSRF failure responses for browser requests."""

from django.http import HttpResponseForbidden
from django.shortcuts import render
from django.urls import reverse


def failure(request, reason=""):
    """Explain how to recover from a stale browser form without accepting it.

    A login form can outlive its CSRF cookie in browser history (for example,
    after signing in in another tab).  The rejected POST must remain a 403;
    this page only gives the person a safe GET that obtains a fresh token.
    Other CSRF failures get a generic response and do not expose Django's
    diagnostic reason to the browser.
    """
    if request.path == reverse("login"):
        response = render(request, "tracker/csrf_failure.html", status=403)
    else:
        response = HttpResponseForbidden(
            "This request could not be verified. Reload the page and try again."
        )
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response
