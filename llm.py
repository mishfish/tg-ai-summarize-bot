from abc import ABC, abstractmethod
import config


class LLMProvider(ABC):
    @abstractmethod
    def chat(self, messages: list[dict]) -> str:
        pass

    @property
    @abstractmethod
    def available_models(self) -> list[str]:
        pass

    @abstractmethod
    def set_model(self, model: str) -> None:
        pass

    @abstractmethod
    def current_model(self) -> str:
        pass


class GroqProvider(LLMProvider):
    MODELS = [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "llama3-70b-8192",
        "mixtral-8x7b-32768",
        "gemma2-9b-it",
    ]

    def __init__(self):
        from groq import Groq
        self._client = Groq(api_key=config.GROQ_API_KEY)
        self._model = config.GROQ_MODEL

    def chat(self, messages: list[dict]) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            temperature=config.TEMPERATURE,
            max_tokens=config.MAX_TOKENS,
        )
        return response.choices[0].message.content

    @property
    def available_models(self) -> list[str]:
        return self.MODELS

    def set_model(self, model: str) -> None:
        self._model = model

    def current_model(self) -> str:
        return self._model


class AnthropicProvider(LLMProvider):
    MODELS = [
        "claude-sonnet-4-6",
        "claude-opus-4-6",
        "claude-haiku-4-5-20251001",
    ]

    def __init__(self):
        import anthropic
        self._client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self._model = config.ANTHROPIC_MODEL

    def chat(self, messages: list[dict]) -> str:
        # Anthropic separates system prompt from messages
        system = config.SYSTEM_PROMPT
        non_system = [m for m in messages if m["role"] != "system"]
        response = self._client.messages.create(
            model=self._model,
            system=system,
            messages=non_system,
            temperature=config.TEMPERATURE,
            max_tokens=config.MAX_TOKENS,
        )
        return response.content[0].text

    @property
    def available_models(self) -> list[str]:
        return self.MODELS

    def set_model(self, model: str) -> None:
        self._model = model

    def current_model(self) -> str:
        return self._model


def get_provider() -> LLMProvider:
    if config.LLM_PROVIDER == "anthropic":
        return AnthropicProvider()
    return GroqProvider()
