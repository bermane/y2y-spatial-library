"""Numbered, one-shot SQLite schema/data migrations.

Each migration is a standalone script; once applied it is recorded in the
``schema_migrations`` table by ``version`` (e.g. ``'001'``). Migrations
are append-only — never edit a migration after it has been applied to a
real database.
"""
