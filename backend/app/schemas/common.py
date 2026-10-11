from typing import Annotated

from fastapi import Path
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

MAX_ID = 2**63 - 1

# Path ids that do not parse or are out of range are "no such resource": the validation
# handler turns any path-parameter error into a 404 (never 400/500).
PathId = Annotated[int, Path(ge=1, le=MAX_ID)]


class CamelModel(BaseModel):
    """Responses: JSON uses camelCase (the contract); Python code uses snake_case names."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)


class RequestModel(CamelModel):
    """Requests accept only the camelCase names the contract defines."""

    model_config = ConfigDict(populate_by_name=False)
