from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, field_validator

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
Money = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=2, allow_inf_nan=False)]


class TransactionInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "transaction_id": "txn_try_001",
                    "source_account": "acc_user_789",
                    "destination_account": "acc_merchant_456",
                    "amount": 1500,
                    "currency": "INR",
                }
            ]
        },
    )

    transaction_id: Identifier
    source_account: Identifier
    destination_account: Identifier
    amount: Money
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class Acknowledgment(BaseModel):
    transaction_id: str
    status: Literal["ACCEPTED"] = "ACCEPTED"
    duplicate: bool


class TransactionOutput(BaseModel):
    transaction_id: str
    source_account: str
    destination_account: str
    # The custom response writes Decimal directly as a JSON number (no float round-trip).
    amount: Annotated[Decimal, WithJsonSchema({"type": "number"}, mode="serialization")]
    currency: str
    status: Literal["PROCESSING", "PROCESSED"]
    created_at: datetime
    processed_at: datetime | None


class HealthOutput(BaseModel):
    status: Literal["HEALTHY", "UNHEALTHY"]
    current_time: datetime
