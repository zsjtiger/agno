import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional, Tuple, Type, Union

import httpx
from pydantic import BaseModel
from typing_extensions import Literal

from agno.exceptions import ModelProviderError
from agno.media import File
from agno.models.base import MessageData, Model, _add_usage_metrics_to_assistant_message
from agno.models.message import Citations, Message, UrlCitation
from agno.models.response import ModelResponse
from agno.utils.log import log_debug, log_error, log_warning
from agno.utils.models.openai_responses import images_to_message
from agno.utils.models.schema_utils import get_response_schema_for_provider

try:
    from openai import APIConnectionError, APIStatusError, AsyncOpenAI, OpenAI, RateLimitError
    from openai.resources.responses.responses import Response, ResponseStreamEvent
except (ImportError, ModuleNotFoundError) as e:
    raise ImportError("`openai` not installed. Please install using `pip install openai -U`") from e


@dataclass
class OpenAIResponses(Model):
    """
    A class for interacting with OpenAI models using the Responses API.

    For more information, see: https://platform.openai.com/docs/api-reference/responses
    """

    id: str = "gpt-4o"
    name: str = "OpenAIResponses"
    provider: str = "OpenAI"
    supports_native_structured_outputs: bool = True

    # Request parameters
    include: Optional[List[str]] = None
    max_output_tokens: Optional[int] = None
    max_tool_calls: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None
    parallel_tool_calls: Optional[bool] = None
    reasoning: Optional[Dict[str, Any]] = None
    store: Optional[bool] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    truncation: Optional[Literal["auto", "disabled"]] = None
    user: Optional[str] = None

    request_params: Optional[Dict[str, Any]] = None

    # Client parameters
    api_key: Optional[str] = None
    organization: Optional[str] = None
    base_url: Optional[Union[str, httpx.URL]] = None
    timeout: Optional[float] = None
    max_retries: Optional[int] = None
    default_headers: Optional[Dict[str, str]] = None
    default_query: Optional[Dict[str, str]] = None
    http_client: Optional[httpx.Client] = None
    client_params: Optional[Dict[str, Any]] = None

    # Parameters affecting built-in tools
    vector_store_name: str = "knowledge_base"

    # OpenAI clients
    client: Optional[OpenAI] = None
    async_client: Optional[AsyncOpenAI] = None

    # The role to map the message role to.
    role_map: Dict[str, str] = field(
        default_factory=lambda: {
            "system": "developer",
            "user": "user",
            "assistant": "assistant",
            "tool": "tool",
        }
    )

    def _get_client_params(self) -> Dict[str, Any]:
        """
        Get client parameters for API requests.

        Returns:
            Dict[str, Any]: Client parameters
        """
        from os import getenv

        # Fetch API key from env if not already set
        if not self.api_key:
            self.api_key = getenv("OPENAI_API_KEY")
            if not self.api_key:
                log_error("OPENAI_API_KEY not set. Please set the OPENAI_API_KEY environment variable.")

        # Define base client params
        base_params = {
            "api_key": self.api_key,
            "organization": self.organization,
            "base_url": self.base_url,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "default_headers": self.default_headers,
            "default_query": self.default_query,
        }

        # Create client_params dict with non-None values
        client_params = {k: v for k, v in base_params.items() if v is not None}

        # Add additional client params if provided
        if self.client_params:
            client_params.update(self.client_params)

        return client_params

    def get_client(self) -> OpenAI:
        """
        Returns an OpenAI client.

        Returns:
            OpenAI: An instance of the OpenAI client.
        """
        if self.client and not self.client.is_closed():
            return self.client

        client_params: Dict[str, Any] = self._get_client_params()
        if self.http_client is not None:
            client_params["http_client"] = self.http_client

        self.client = OpenAI(**client_params)
        return self.client

    def get_async_client(self) -> AsyncOpenAI:
        """
        Returns an asynchronous OpenAI client.

        Returns:
            AsyncOpenAI: An instance of the asynchronous OpenAI client.
        """
        if self.async_client:
            return self.async_client

        client_params: Dict[str, Any] = self._get_client_params()
        if self.http_client:
            client_params["http_client"] = self.http_client
        else:
            # Create a new async HTTP client with custom limits
            client_params["http_client"] = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=1000, max_keepalive_connections=100)
            )

        self.async_client = AsyncOpenAI(**client_params)
        return self.async_client

    def get_request_params(
        self,
        messages: Optional[List[Message]] = None,
        response_format: Optional[Union[Dict, Type[BaseModel]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Returns keyword arguments for API requests.

        Returns:
            Dict[str, Any]: A dictionary of keyword arguments for API requests.
        """
        # Define base request parameters
        base_params: Dict[str, Any] = {
            "include": self.include,
            "max_output_tokens": self.max_output_tokens,
            "max_tool_calls": self.max_tool_calls,
            "metadata": self.metadata,
            "parallel_tool_calls": self.parallel_tool_calls,
            "reasoning": self.reasoning,
            "store": self.store,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "truncation": self.truncation,
            "user": self.user,
        }
        # Set the response format
        if response_format is not None:
            if isinstance(response_format, type) and issubclass(response_format, BaseModel):
                schema = get_response_schema_for_provider(response_format, "openai")
                base_params["text"] = {
                    "format": {
                        "type": "json_schema",
                        "name": response_format.__name__,
                        "schema": schema,
                        "strict": True,
                    }
                }
            else:
                # JSON mode
                base_params["text"] = {"format": {"type": "json_object"}}

        # Filter out None values
        request_params: Dict[str, Any] = {k: v for k, v in base_params.items() if v is not None}

        # Deep research models require web_search_preview tool or MCP tool
        if "deep-research" in self.id:
            if tools is None:
                tools = []

            # Check if web_search_preview tool is already present
            has_web_search = any(tool.get("type") == "web_search_preview" for tool in tools)

            # Add web_search_preview if not present - this enables the model to search
            # the web for current information and provide citations
            if not has_web_search:
                web_search_tool = {"type": "web_search_preview"}
                tools.insert(0, web_search_tool)
                log_debug(f"Added web_search_preview tool for deep research model: {self.id}")

        if tools:
            request_params["tools"] = self._format_tool_params(messages=messages, tools=tools)  # type: ignore

        if tool_choice is not None:
            request_params["tool_choice"] = tool_choice

        # Handle reasoning tools for o3 and o4-mini models
        if (self.id.startswith("o3") or self.id.startswith("o4-mini")) and messages is not None:
            request_params["store"] = True

            # Check if the last assistant message has a previous_response_id to continue from
            previous_response_id = None
            for msg in reversed(messages):
                if (
                    msg.role == "assistant"
                    and hasattr(msg, "provider_data")
                    and msg.provider_data
                    and "response_id" in msg.provider_data
                ):
                    previous_response_id = msg.provider_data["response_id"]
                    log_debug(f"Using previous_response_id: {previous_response_id}")
                    break

            if previous_response_id:
                request_params["previous_response_id"] = previous_response_id

        # Add additional request params if provided
        if self.request_params:
            request_params.update(self.request_params)

        if request_params:
            log_debug(f"Calling {self.provider} with request parameters: {request_params}", log_level=2)
        return request_params

    def _upload_file(self, file: File) -> Optional[str]:
        """Upload a file to the OpenAI vector database."""

        if file.url is not None:
            file_content_tuple = file.file_url_content
            if file_content_tuple is not None:
                file_content = file_content_tuple[0]
            else:
                return None
            file_name = file.url.split("/")[-1]
            file_tuple = (file_name, file_content)
            result = self.get_client().files.create(file=file_tuple, purpose="assistants")
            return result.id
        elif file.filepath is not None:
            import mimetypes
            from pathlib import Path

            file_path = file.filepath if isinstance(file.filepath, Path) else Path(file.filepath)
            if file_path.exists() and file_path.is_file():
                file_name = file_path.name
                file_content = file_path.read_bytes()  # type: ignore
                content_type = mimetypes.guess_type(file_path)[0]
                result = self.get_client().files.create(
                    file=(file_name, file_content, content_type),
                    purpose="assistants",  # type: ignore
                )
                return result.id
            else:
                raise ValueError(f"File not found: {file_path}")
        elif file.content is not None:
            result = self.get_client().files.create(file=file.content, purpose="assistants")
            return result.id

        return None

    def _create_vector_store(self, file_ids: List[str]) -> str:
        """Create a vector store for the files."""
        vector_store = self.get_client().vector_stores.create(name=self.vector_store_name)
        for file_id in file_ids:
            self.get_client().vector_stores.files.create(vector_store_id=vector_store.id, file_id=file_id)
        while True:
            uploaded_files = self.get_client().vector_stores.files.list(vector_store_id=vector_store.id)
            all_completed = True
            failed = False
            for file in uploaded_files:
                if file.status == "failed":
                    log_error(f"File {file.id} failed to upload.")
                    failed = True
                    break
                if file.status != "completed":
                    all_completed = False
            if all_completed or failed:
                break
            time.sleep(1)
        return vector_store.id

    def _format_tool_params(
        self, messages: List[Message], tools: Optional[List[Dict[str, Any]]] = None
    ) -> List[Dict[str, Any]]:
        """Format the tool parameters for the OpenAI Responses API."""
        formatted_tools = []
        if tools:
            for _tool in tools:
                if _tool["type"] == "function":
                    _tool_dict = _tool["function"]
                    _tool_dict["type"] = "function"
                    for prop in _tool_dict["parameters"]["properties"].values():
                        if isinstance(prop["type"], list):
                            prop["type"] = prop["type"][0]

                    formatted_tools.append(_tool_dict)
                else:
                    formatted_tools.append(_tool)

        # Find files to upload to the OpenAI vector database
        file_ids = []
        for message in messages:
            # Upload any attached files to the OpenAI vector database
            if message.files is not None and len(message.files) > 0:
                for file in message.files:
                    file_id = self._upload_file(file)
                    if file_id is not None:
                        file_ids.append(file_id)

        vector_store_id = self._create_vector_store(file_ids) if file_ids else None

        # Add the file IDs to the tool parameters
        for _tool in formatted_tools:
            if _tool["type"] == "file_search" and vector_store_id is not None:
                _tool["vector_store_ids"] = [vector_store_id]

        return formatted_tools

    def _format_messages(self, messages: List[Message]) -> List[Dict[str, Any]]:
        """
        Format a message into the format expected by OpenAI.

        Args:
            messages (List[Message]): The message to format.

        Returns:
            Dict[str, Any]: The formatted message.
        """
        formatted_messages: List[Dict[str, Any]] = []
        for message in messages:
            if message.role in ["user", "system"]:
                message_dict: Dict[str, Any] = {
                    "role": self.role_map[message.role],
                    "content": message.content,
                }
                message_dict = {k: v for k, v in message_dict.items() if v is not None}

                # Ignore non-string message content
                # because we assume that the images/audio are already added to the message
                if message.images is not None and len(message.images) > 0:
                    # Ignore non-string message content
                    # because we assume that the images/audio are already added to the message
                    if isinstance(message.content, str):
                        message_dict["content"] = [{"type": "input_text", "text": message.content}]
                        if message.images is not None:
                            message_dict["content"].extend(images_to_message(images=message.images))

                if message.audio is not None and len(message.audio) > 0:
                    log_warning("Audio input is currently unsupported.")

                if message.videos is not None and len(message.videos) > 0:
                    log_warning("Video input is currently unsupported.")

                formatted_messages.append(message_dict)

            elif message.role == "tool":
                if message.tool_call_id and message.content is not None:
                    formatted_messages.append(
                        {"type": "function_call_output", "call_id": message.tool_call_id, "output": message.content}
                    )
            elif message.tool_calls is not None and len(message.tool_calls) > 0:
                for tool_call in message.tool_calls:
                    formatted_messages.append(
                        {
                            "type": "function_call",
                            "id": tool_call["id"],
                            "call_id": tool_call["call_id"],
                            "name": tool_call["function"]["name"],
                            "arguments": tool_call["function"]["arguments"],
                            "status": "completed",
                        }
                    )
            elif message.role == "assistant":
                # Handle null content by converting to empty string
                content = message.content if message.content is not None else ""
                formatted_messages.append({"role": self.role_map[message.role], "content": content})

        return formatted_messages

    def invoke(
        self,
        messages: List[Message],
        response_format: Optional[Union[Dict, Type[BaseModel]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
    ) -> Response:
        """
        Send a request to the OpenAI Responses API.
        """
        try:
            request_params = self.get_request_params(
                messages=messages, response_format=response_format, tools=tools, tool_choice=tool_choice
            )

            return self.get_client().responses.create(
                model=self.id,
                input=self._format_messages(messages),  # type: ignore
                **request_params,
            )
        except RateLimitError as exc:
            log_error(f"Rate limit error from OpenAI API: {exc}")
            error_message = exc.response.json().get("error", {})
            error_message = (
                error_message.get("message", "Unknown model error")
                if isinstance(error_message, dict)
                else error_message
            )
            raise ModelProviderError(
                message=error_message,
                status_code=exc.response.status_code,
                model_name=self.name,
                model_id=self.id,
            ) from exc
        except APIConnectionError as exc:
            log_error(f"API connection error from OpenAI API: {exc}")
            raise ModelProviderError(message=str(exc), model_name=self.name, model_id=self.id) from exc
        except APIStatusError as exc:
            log_error(f"API status error from OpenAI API: {exc}")
            error_message = exc.response.json().get("error", {})
            error_message = (
                error_message.get("message", "Unknown model error")
                if isinstance(error_message, dict)
                else error_message
            )
            raise ModelProviderError(
                message=error_message,
                status_code=exc.response.status_code,
                model_name=self.name,
                model_id=self.id,
            ) from exc
        except Exception as exc:
            log_error(f"Error from OpenAI API: {exc}")
            raise ModelProviderError(message=str(exc), model_name=self.name, model_id=self.id) from exc

    async def ainvoke(
        self,
        messages: List[Message],
        response_format: Optional[Union[Dict, Type[BaseModel]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
    ) -> Response:
        """
        Sends an asynchronous request to the OpenAI Responses API.
        """
        try:
            request_params = self.get_request_params(
                messages=messages, response_format=response_format, tools=tools, tool_choice=tool_choice
            )

            return await self.get_async_client().responses.create(
                model=self.id,
                input=self._format_messages(messages),  # type: ignore
                **request_params,
            )
        except RateLimitError as exc:
            log_error(f"Rate limit error from OpenAI API: {exc}")
            error_message = exc.response.json().get("error", {})
            error_message = (
                error_message.get("message", "Unknown model error")
                if isinstance(error_message, dict)
                else error_message
            )
            raise ModelProviderError(
                message=error_message,
                status_code=exc.response.status_code,
                model_name=self.name,
                model_id=self.id,
            ) from exc
        except APIConnectionError as exc:
            log_error(f"API connection error from OpenAI API: {exc}")
            raise ModelProviderError(message=str(exc), model_name=self.name, model_id=self.id) from exc
        except APIStatusError as exc:
            log_error(f"API status error from OpenAI API: {exc}")
            error_message = exc.response.json().get("error", {})
            error_message = (
                error_message.get("message", "Unknown model error")
                if isinstance(error_message, dict)
                else error_message
            )
            raise ModelProviderError(
                message=error_message,
                status_code=exc.response.status_code,
                model_name=self.name,
                model_id=self.id,
            ) from exc
        except Exception as exc:
            log_error(f"Error from OpenAI API: {exc}")
            raise ModelProviderError(message=str(exc), model_name=self.name, model_id=self.id) from exc

    def invoke_stream(
        self,
        messages: List[Message],
        response_format: Optional[Union[Dict, Type[BaseModel]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
    ) -> Iterator[ResponseStreamEvent]:
        """
        Send a streaming request to the OpenAI Responses API.
        """
        try:
            request_params = self.get_request_params(
                messages=messages, response_format=response_format, tools=tools, tool_choice=tool_choice
            )

            yield from self.get_client().responses.create(
                model=self.id,
                input=self._format_messages(messages),  # type: ignore
                stream=True,
                **request_params,
            )  # type: ignore
        except RateLimitError as exc:
            log_error(f"Rate limit error from OpenAI API: {exc}")
            error_message = exc.response.json().get("error", {})
            error_message = (
                error_message.get("message", "Unknown model error")
                if isinstance(error_message, dict)
                else error_message
            )
            raise ModelProviderError(
                message=error_message,
                status_code=exc.response.status_code,
                model_name=self.name,
                model_id=self.id,
            ) from exc
        except APIConnectionError as exc:
            log_error(f"API connection error from OpenAI API: {exc}")
            raise ModelProviderError(message=str(exc), model_name=self.name, model_id=self.id) from exc
        except APIStatusError as exc:
            log_error(f"API status error from OpenAI API: {exc}")
            error_message = exc.response.json().get("error", {})
            error_message = (
                error_message.get("message", "Unknown model error")
                if isinstance(error_message, dict)
                else error_message
            )
            raise ModelProviderError(
                message=error_message,
                status_code=exc.response.status_code,
                model_name=self.name,
                model_id=self.id,
            ) from exc
        except Exception as exc:
            log_error(f"Error from OpenAI API: {exc}")
            raise ModelProviderError(message=str(exc), model_name=self.name, model_id=self.id) from exc

    async def ainvoke_stream(
        self,
        messages: List[Message],
        response_format: Optional[Union[Dict, Type[BaseModel]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
    ) -> AsyncIterator[ResponseStreamEvent]:
        """
        Sends an asynchronous streaming request to the OpenAI Responses API.
        """
        try:
            request_params = self.get_request_params(
                messages=messages, response_format=response_format, tools=tools, tool_choice=tool_choice
            )
            async_stream = await self.get_async_client().responses.create(
                model=self.id,
                input=self._format_messages(messages),  # type: ignore
                stream=True,
                **request_params,
            )
            async for chunk in async_stream:  # type: ignore
                yield chunk
        except RateLimitError as exc:
            log_error(f"Rate limit error from OpenAI API: {exc}")
            error_message = exc.response.json().get("error", {})
            error_message = (
                error_message.get("message", "Unknown model error")
                if isinstance(error_message, dict)
                else error_message
            )
            raise ModelProviderError(
                message=error_message,
                status_code=exc.response.status_code,
                model_name=self.name,
                model_id=self.id,
            ) from exc
        except APIConnectionError as exc:
            log_error(f"API connection error from OpenAI API: {exc}")
            raise ModelProviderError(message=str(exc), model_name=self.name, model_id=self.id) from exc
        except APIStatusError as exc:
            log_error(f"API status error from OpenAI API: {exc}")
            error_message = exc.response.json().get("error", {})
            error_message = (
                error_message.get("message", "Unknown model error")
                if isinstance(error_message, dict)
                else error_message
            )
            raise ModelProviderError(
                message=error_message,
                status_code=exc.response.status_code,
                model_name=self.name,
                model_id=self.id,
            ) from exc
        except Exception as exc:
            log_error(f"Error from OpenAI API: {exc}")
            raise ModelProviderError(message=str(exc), model_name=self.name, model_id=self.id) from exc

    def format_function_call_results(
        self, messages: List[Message], function_call_results: List[Message], tool_call_ids: List[str]
    ) -> None:
        """
        Handle the results of function calls.

        Args:
            messages (List[Message]): The list of conversation messages.
            function_call_results (List[Message]): The results of the function calls.
            tool_ids (List[str]): The tool ids.
        """
        if len(function_call_results) > 0:
            for _fc_message_index, _fc_message in enumerate(function_call_results):
                _fc_message.tool_call_id = tool_call_ids[_fc_message_index]
                messages.append(_fc_message)

    def parse_provider_response(self, response: Response, **kwargs) -> ModelResponse:
        """
        Parse the OpenAI response into a ModelResponse.

        Args:
            response: Response from invoke() method

        Returns:
            ModelResponse: Parsed response data
        """
        model_response = ModelResponse()

        if response.error:
            raise ModelProviderError(
                message=response.error.message,
                model_name=self.name,
                model_id=self.id,
            )

        # Store the response ID for continuity
        if response.id:
            if model_response.provider_data is None:
                model_response.provider_data = {}
            model_response.provider_data["response_id"] = response.id

        # Add role
        model_response.role = "assistant"
        for output in response.output:
            if output.type == "message":
                model_response.content = response.output_text

                # Add citations
                citations = Citations()
                for content in output.content:
                    if content.type == "output_text" and content.annotations:
                        citations.raw = [annotation.model_dump() for annotation in content.annotations]
                        for annotation in content.annotations:
                            if annotation.type == "url_citation":
                                if citations.urls is None:
                                    citations.urls = []
                                citations.urls.append(UrlCitation(url=annotation.url, title=annotation.title))
                        if citations.urls or citations.documents:
                            model_response.citations = citations
            elif output.type == "function_call":
                if model_response.tool_calls is None:
                    model_response.tool_calls = []
                model_response.tool_calls.append(
                    {
                        "id": output.id,
                        "call_id": output.call_id,
                        "type": "function",
                        "function": {
                            "name": output.name,
                            "arguments": output.arguments,
                        },
                    }
                )

                model_response.extra = model_response.extra or {}
                model_response.extra.setdefault("tool_call_ids", []).append(output.call_id)

        # i.e. we asked for reasoning, so we need to add the reasoning content
        if self.reasoning is not None:
            model_response.reasoning_content = response.output_text

        if response.usage is not None:
            model_response.response_usage = response.usage

        return model_response

    def _process_stream_response(
        self,
        stream_event: ResponseStreamEvent,
        assistant_message: Message,
        stream_data: MessageData,
        tool_use: Dict[str, Any],
    ) -> Tuple[Optional[ModelResponse], Dict[str, Any]]:
        """
        Common handler for processing stream responses from Cohere.

        Args:
            stream_event: The streamed response from Cohere
            assistant_message: The assistant message being built
            stream_data: Data accumulated during streaming
            tool_use: Current tool use data being built

        Returns:
            Tuple containing the ModelResponse to yield and updated tool_use dict
        """
        model_response = None

        if stream_event.type == "response.created":
            model_response = ModelResponse()
            # Store the response ID for continuity
            if stream_event.response.id:
                if stream_data.response_provider_data is None:
                    stream_data.response_provider_data = {}
                stream_data.response_provider_data["response_id"] = stream_event.response.id
            # Update metrics
            if not assistant_message.metrics.time_to_first_token:
                assistant_message.metrics.set_time_to_first_token()
        elif stream_event.type == "response.output_text.annotation.added":
            model_response = ModelResponse()
            if stream_data.response_citations is None:
                stream_data.response_citations = Citations(raw=[stream_event.annotation])
            else:
                stream_data.response_citations.raw.append(stream_event.annotation)  # type: ignore

            if isinstance(stream_event.annotation, dict):
                if stream_event.annotation.get("type") == "url_citation":
                    if stream_data.response_citations.urls is None:
                        stream_data.response_citations.urls = []
                    stream_data.response_citations.urls.append(
                        UrlCitation(url=stream_event.annotation.get("url"), title=stream_event.annotation.get("title"))
                    )
            else:
                if stream_event.annotation.type == "url_citation":  # type: ignore
                    if stream_data.response_citations.urls is None:
                        stream_data.response_citations.urls = []
                    stream_data.response_citations.urls.append(
                        UrlCitation(url=stream_event.annotation.url, title=stream_event.annotation.title)  # type: ignore
                    )

            model_response.citations = stream_data.response_citations

        elif stream_event.type == "response.output_text.delta":
            model_response = ModelResponse()
            # Add content
            model_response.content = stream_event.delta
            stream_data.response_content += stream_event.delta

            if self.reasoning is not None:
                model_response.reasoning_content = stream_event.delta
                stream_data.response_thinking += stream_event.delta

        elif stream_event.type == "response.output_item.added":
            item = stream_event.item
            if item.type == "function_call":
                tool_use = {
                    "id": item.id,
                    "call_id": item.call_id,
                    "type": "function",
                    "function": {
                        "name": item.name,
                        "arguments": item.arguments,
                    },
                }

        elif stream_event.type == "response.function_call_arguments.delta":
            tool_use["function"]["arguments"] += stream_event.delta

        elif stream_event.type == "response.output_item.done" and tool_use:
            model_response = ModelResponse()
            model_response.tool_calls = [tool_use]
            if assistant_message.tool_calls is None:
                assistant_message.tool_calls = []
            assistant_message.tool_calls.append(tool_use)

            stream_data.extra = stream_data.extra or {}
            stream_data.extra.setdefault("tool_call_ids", []).append(tool_use["call_id"])
            tool_use = {}

        elif stream_event.type == "response.completed":
            model_response = ModelResponse()
            # Add usage metrics if present
            if stream_event.response.usage is not None:
                model_response.response_usage = stream_event.response.usage

            _add_usage_metrics_to_assistant_message(
                assistant_message=assistant_message,
                response_usage=model_response.response_usage,
            )

        return model_response, tool_use

    def process_response_stream(
        self,
        messages: List[Message],
        assistant_message: Message,
        stream_data: MessageData,
        response_format: Optional[Union[Dict, Type[BaseModel]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
    ) -> Iterator[ModelResponse]:
        """Process the synchronous response stream."""
        tool_use: Dict[str, Any] = {}

        for stream_event in self.invoke_stream(
            messages=messages, tools=tools, response_format=response_format, tool_choice=tool_choice
        ):
            model_response, tool_use = self._process_stream_response(
                stream_event=stream_event,
                assistant_message=assistant_message,
                stream_data=stream_data,
                tool_use=tool_use,
            )

            if model_response is not None:
                yield model_response

    async def aprocess_response_stream(
        self,
        messages: List[Message],
        assistant_message: Message,
        stream_data: MessageData,
        response_format: Optional[Union[Dict, Type[BaseModel]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
    ) -> AsyncIterator[ModelResponse]:
        """Process the asynchronous response stream."""
        tool_use: Dict[str, Any] = {}

        async for stream_event in self.ainvoke_stream(
            messages=messages, tools=tools, response_format=response_format, tool_choice=tool_choice
        ):
            model_response, tool_use = self._process_stream_response(
                stream_event=stream_event,
                assistant_message=assistant_message,
                stream_data=stream_data,
                tool_use=tool_use,
            )
            if model_response is not None:
                yield model_response

    def parse_provider_response_delta(self, response: Any) -> ModelResponse:  # type: ignore
        pass
