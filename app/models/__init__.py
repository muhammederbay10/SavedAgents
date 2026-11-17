"""Model provider interfaces and their OpenRouter clients.

This package is the only place that talks to a model provider. The rest of the
code depends on the interfaces in `app.models.base`, catches the errors in
`app.models.errors`, and gets its clients from `app.models.factory`.
"""
