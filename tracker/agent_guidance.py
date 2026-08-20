"""Server-authored instructions a person copies to hand their search to an assistant.

The prompt names exactly one URL and no other technical noun.  Everything an
agent needs to act lives behind that URL, served publicly, so the human is
never the transport layer for a specification.
"""

PROMPT_MAX_CHARS = 1000


def _origin(api_base_url):
    """Reduce an API base URL to the public origin the package is served from."""
    origin = str(api_base_url or "").rstrip("/")
    for suffix in ("/api/v1", "/api"):
        if origin.endswith(suffix):
            return origin[: -len(suffix)]
    return origin


def build_agent_prompt(api_base_url, repair=False):
    """Return the bootstrap instruction, with this deployment's origin filled in.

    Deliberate omissions, each load-bearing: no "treat what you fetch as
    untrusted" (self-contradictory in a prompt whose first instruction is
    "fetch this and follow it" — that rule lives in the package, scoped to
    listing text); no technical noun other than a URL; and "do not improvise",
    which prevents the worst outcome, an agent inventing an integration against
    an API it never read.
    """
    origin = _origin(api_base_url)
    if repair:
        return f"""Repair the source plan Homing has flagged for my housing searches.

Read {origin}/agent/ and follow it exactly. Work with the existing installation; do not create a second scheduled job. Read the open source-plan reviews and current project prompts from Homing. Compare them with the installed sources and basis. Decide whether the worker-wide source union still fits. If it does, avoid expensive discovery and update the basis through the normal repair path. Otherwise focus discovery on the flagged searches, then rebuild the global union without dropping coverage for other current searches.

Run the package self-test and one on-demand check. Resolve each review only after the verified installation records the current prompt revision. If a prompt changes during repair, re-read it and repeat the comparison. Never ask me to paste a password or access key into this chat. Ask one plain human question at a time only when a real choice is genuinely gated."""
    return f"""Set up my recurring housing search with Homing.

Read {origin}/agent/ and follow it exactly. It tells you everything, including how to get access to my account without me pasting anything secret into this chat.

Before you ask me anything, work out your own setup yourself: what tools, storage, network, scheduling and secure storage you have. Then ask me only what you genuinely cannot find out on your own. Plain words, one thing at a time, at most three, each with a sensible default I can accept by saying "yes".

Never put a password or an access key into this chat.

If you cannot read that page, tell me plainly and stop. Do not improvise.

When you are done, tell me in a few plain sentences what you set up, how often it will run, and how I stop it."""
