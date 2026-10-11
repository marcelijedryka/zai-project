from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """Responses: JSON uses camelCase (the contract); Python code uses snake_case names."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class RequestModel(CamelModel):
    """Requests accept only the camelCase names the contract defines."""

    model_config = ConfigDict(populate_by_name=False)
