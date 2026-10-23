"""Keep JSON money literals exact before Pydantic ever sees them."""

import json
from collections.abc import Callable
from decimal import Decimal

from fastapi import Request, Response
from fastapi.routing import APIRoute


class DecimalRequest(Request):
    async def json(self) -> object:
        if not hasattr(self, "_json"):

            def reject_constant(value: str) -> None:
                raise json.JSONDecodeError("Non-finite numbers are not valid JSON", value, 0)

            self._json = json.loads(
                await self.body(), parse_float=Decimal, parse_constant=reject_constant
            )
        return self._json


class DecimalRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            return await original(DecimalRequest(request.scope, request.receive))

        return handler
