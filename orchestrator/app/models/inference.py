"""Pydantic models matching the OpenAI chat-completions request/response shape."""

from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field

# Chat models supported by the inference layer.
SUPPORTED_MODELS = ("qwen-2.5-7b", "mistral-7b", "llama-3.1-8b-quantized")

# Public model catalog served by GET /v1/models.
MODEL_CATALOG = [
    {"id": "qwen-2.5-7b", "type": "chat", "context_window": 32768},
    {"id": "mistral-7b", "type": "chat", "context_window": 32768},
    {"id": "llama-3.1-8b-quantized", "type": "chat", "context_window": 8192},
    {"id": "flux-schnell", "type": "image", "max_size": "1536x1536"},
    {"id": "orvix-image-1", "type": "image", "max_size": "1024x1024"},
    # Embeddings. `dimensions` is advertised so a client can size a vector store
    # before it ever calls the endpoint.
    {"id": "orvix-embed-1", "type": "embedding", "dimensions": 768},
]

# Image models supported via the /v1/images/generations endpoint. Derived from
# the catalog so the two cannot drift — every accepted model is guaranteed to
# have a max_size to validate against.
IMAGE_MODELS = tuple(e["id"] for e in MODEL_CATALOG if e["type"] == "image")


def image_model_max_size(model_id: str) -> tuple[int, int]:
    """Largest (width, height) the given image model will generate.

    The catalog is the single source of truth here: a node that loads
    ``orvix-image-1`` on a 20 GB card fits one 1024x1024 pass next to a chat
    engine and OOMs above that, so dispatching a larger request would only
    burn a job slot to fail on the node.
    """
    for entry in MODEL_CATALOG:
        if entry["id"] == model_id and entry["type"] == "image":
            width, height = entry["max_size"].split("x")
            return int(width), int(height)
    raise KeyError(f"{model_id!r} is not an image model in the catalog")


class ToolCallFunction(BaseModel):
    name: str
    # OpenAI sends the arguments as a JSON *string*, not an object, so callers
    # can stream them and parse once complete. Kept verbatim.
    arguments: str


class ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: ToolCallFunction


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    # Null on an assistant turn that only calls tools — the model produced no
    # prose, just the call.
    content: Optional[str] = None
    # Set on an assistant message that requests tool calls.
    tool_calls: Optional[List[ToolCall]] = None
    # Set on a role="tool" message, linking the result back to the call.
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


class FunctionDef(BaseModel):
    name: str
    description: Optional[str] = None
    # JSON Schema. Passed through untouched; validating it here would only
    # diverge from whatever the serving engine actually accepts.
    parameters: Optional[dict] = None


class Tool(BaseModel):
    type: Literal["function"] = "function"
    function: FunctionDef


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage] = Field(..., min_length=1)
    max_tokens: int = Field(512, ge=1, le=4096)
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    stream: bool = False
    tools: Optional[List[Tool]] = None
    # "auto" | "none" | "required" | {"type":"function","function":{"name":...}}
    tool_choice: Optional[Union[str, dict]] = None


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: Optional[str]


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionChoice]
    usage: Usage
