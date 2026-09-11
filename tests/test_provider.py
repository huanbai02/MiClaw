from langchain_anthropic import ChatAnthropic
from langchain_ollama import ChatOllama

from miclaw.core.llm.provider import get_provider


def test_ollama_provider_uses_declared_adapter_without_connecting():
    """Ollama provider 应能仅构造 adapter，不要求测试环境存在服务。"""
    provider = get_provider(
        provider_name="ollama",
        model_name="local-test-model",
        base_url="http://127.0.0.1:11434",
    )

    assert isinstance(provider, ChatOllama)
    assert provider.model == "local-test-model"


def test_anthropic_provider_uses_declared_adapter_without_connecting():
    """Anthropic provider 应能仅构造 adapter，不要求测试发起网络请求。"""
    provider = get_provider(
        provider_name="anthropic",
        model_name="claude-test-model",
        api_key="test-key",
    )

    assert isinstance(provider, ChatAnthropic)
    assert provider.model == "claude-test-model"
