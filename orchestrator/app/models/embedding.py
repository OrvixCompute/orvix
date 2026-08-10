"""OpenAI-compatible embeddings request/response models.

Shape follows `POST /v1/embeddings` exactly, because the whole point of this
endpoint is that existing OpenAI clients work by changing a base URL. A response
that differs in field names or ordering is a response those clients cannot read.
"""

from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

# A single request should not be able to park a node indefinitely. The ceiling
# is on inputs, not characters, because that is what maps to work done.
MAX_INPUTS = 256
MAX_INPUT_CHARS = 8192


class EmbeddingRequest(BaseModel):
    model: str
    # OpenAI accepts a string or a list of strings. Token-array inputs
    # (list[int] / list[list[int]]) are part of their spec but are refused here
    # rather than silently mishandled — see the validator.
    input: Union[str, List[str]]
    encoding_format: Literal["float", "base64"] = "float"
    dimensions: Optional[int] = Field(None, gt=0)
    user: Optional[str] = None

    @field_validator("input")
    @classmethod
    def _check_input(cls, v):
        items = [v] if isinstance(v, str) else v
        if not items:
            raise ValueError("input must not be empty")
        if len(items) > MAX_INPUTS:
            raise ValueError(f"input accepts at most {MAX_INPUTS} strings per request")
        for item in items:
            if not isinstance(item, str):
                raise ValueError(
                    "input must be a string or a list of strings; pre-tokenized "
                    "integer input is not supported"
                )
            if not item.strip():
                raise ValueError("input strings must not be empty")
            if len(item) > MAX_INPUT_CHARS:
                raise ValueError(
                    f"each input string must be at most {MAX_INPUT_CHARS} characters"
                )
        return v

    def as_list(self) -> List[str]:
        """The input normalized to a list, preserving caller order."""
        return [self.input] if isinstance(self.input, str) else list(self.input)


class EmbeddingObject(BaseModel):
    object: Literal["embedding"] = "embedding"
    index: int
    # `float` per the default encoding_format. Base64 is encoded at the route.
    embedding: Union[List[float], str]


class EmbeddingUsage(BaseModel):
    prompt_tokens: int
    total_tokens: int


class EmbeddingResponse(BaseModel):
    object: Literal["list"] = "list"
    data: List[EmbeddingObject]
    model: str
    usage: EmbeddingUsage
