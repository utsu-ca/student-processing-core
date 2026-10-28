"""Portal modules. A module is any object with `name` and `router(ctx) -> APIRouter`; pass extras to `create_app`."""
from __future__ import annotations

from typing import Protocol

from fastapi import APIRouter

from ..context import Context


class Module(Protocol):
    name: str

    def router(self, ctx: Context) -> APIRouter: ...
