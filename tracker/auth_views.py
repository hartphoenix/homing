"""Browser password-reset views with the same abuse controls as API auth."""

from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth.views import (
    PasswordResetCompleteView,
    PasswordResetConfirmView,
    PasswordResetDoneView,
    PasswordResetView,
)
from django.urls import reverse_lazy

from accounts.services.throttle import consume as consume_auth_attempt
from accounts.services.throttle import request_keys


class ThrottledPasswordResetView(PasswordResetView):
    """Send reset mail while keeping responses generic and rate-limited."""

    template_name = "registration/password_reset_form.html"
    email_template_name = "registration/password_reset_email.txt"
    subject_template_name = "registration/password_reset_subject.txt"
    success_url = reverse_lazy("tracker:password-reset-done")
    form_class = PasswordResetForm

    def form_valid(self, form):
        email = form.cleaned_data.get("email", "")
        blocked, retry_after = consume_auth_attempt(request_keys(self.request, email))
        if blocked:
            # Keep the response body identical to the ordinary form response;
            # only the status/header communicate the throttling decision.
            response = self.render_to_response(self.get_context_data(form=form))
            response.status_code = 429
            response["Retry-After"] = str(max(1, retry_after))
            return response
        return super().form_valid(form)


class TrackerPasswordResetDoneView(PasswordResetDoneView):
    template_name = "registration/password_reset_done.html"


class TrackerPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = "registration/password_reset_confirm.html"
    success_url = reverse_lazy("tracker:password-reset-complete")


class TrackerPasswordResetCompleteView(PasswordResetCompleteView):
    template_name = "registration/password_reset_complete.html"
