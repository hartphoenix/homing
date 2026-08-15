# Web UX contract

The templates in `templates/tracker/` are intentionally thin Django templates. The backend can supply dictionaries or model instances; optional fields use `default`/empty states and do not make unknown lead data disappear.

## URL names

Required names are `login`, `logout`, and the `tracker:` names: `project-list`, `project-create`, `project-detail`, `project-edit`, `project-settings`, `lead-list`, `lead-create`, `lead-detail`, `lead-edit`, `lead-trash`, `lead-interest`, `comment-create`, `comment-edit`, `prompt-edit`, `member-invite`, `profile`, `token-create`, `saved-prompts`, `saved-prompt-create`, `saved-prompt-edit`, and `register`. Positional args are project `slug`, then lead/comment IDs where shown in templates.

## Context contract

All project pages provide `project`, plus permission booleans such as `can_manage_project`, `can_edit_prompt`, `can_edit_leads`, and `can_comment`. Lead list provides `leads`, `filters`, `date_confidence_choices`, `housing_choices`, and `is_trash`; cards expect `title`, `summary`, `url`, `price_display`, `location`, `availability`, `date_confidence`, `unknowns`, `attributes`, `comment_count`, `is_interested`, `interested_members`, and `is_trashed` (optional). Detail provides `lead.facts`, `comments`, and `can_comment`. Settings provides `members` and optional `invitations`. Account pages provide `form` and optional `tokens`/`prompts`.

Forms must include CSRF tokens and preserve invalid submissions. Prompt updates should submit `expected_revision`; lead updates should use the backend's ETag/conflict flow and render a `409` as a visible message while retaining the draft. Filter state is carried in the query string so URLs remain shareable.

## Accessibility and content rules

Use semantic headings, labels, landmarks, keyboard-visible focus, and autoescaped untrusted lead/comment/prompt content. Interest is per-user but member display names may be shown on shared projects. Trash is reversible and should always expose the reason. Unknown values render explicitly as `Unknown` rather than being inferred.
