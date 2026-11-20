"""What Google said when it refused a request, in words a person can act on."""
from __future__ import annotations

# Refusals caused by the project Google bills for the request (the credentials' quota project), not by permissions.
QUOTA_PROJECT_REASONS = ("accountDisabled", "billingNotEnabled", "userProjectAccountProblem", "SERVICE_DISABLED")


def refusal(response) -> str:
    """`(403) <Google's message>`, with a hint when the cause is the quota project the credentials bill to."""
    status = getattr(response, "status_code", "?")
    try:
        error = response.json().get("error") or {}
    except (ValueError, AttributeError):
        return f"({status})"
    message = error.get("message") or ""
    reasons = {e.get("reason") for e in error.get("errors") or []} | {d.get("reason") for d in error.get("details") or [] if isinstance(d, dict)}
    hint = ""
    if reasons & set(QUOTA_PROJECT_REASONS) or "billing" in message.lower():
        hint = (". Your credentials bill requests to a quota project that cannot pay for them; set GOOGLE_CLOUD_QUOTA_PROJECT to a project "
                "with billing, or run: gcloud auth application-default set-quota-project PROJECT")
    return f"({status}) {message}{hint}".rstrip()
