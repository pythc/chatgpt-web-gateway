"""Shared SDK configuration. Keep account tokens on the gateway, never here."""
import os


def settings():
    base_url = os.environ['OPENAI_BASE_URL'].rstrip('/')
    api_key = os.environ['OPENAI_API_KEY']
    model = os.environ.get('CHATGPT_MODEL', 'gpt-6-thinking')
    return base_url, api_key, model


def sync_client():
    from openai import OpenAI, DefaultHttpxClient
    base_url, api_key, model = settings()
    return OpenAI(base_url=base_url, api_key=api_key, max_retries=0,
                  timeout=620, http_client=DefaultHttpxClient(trust_env=False)), model


def async_client():
    from openai import AsyncOpenAI, DefaultAsyncHttpxClient
    base_url, api_key, model = settings()
    return AsyncOpenAI(base_url=base_url, api_key=api_key, max_retries=0,
                       timeout=620, http_client=DefaultAsyncHttpxClient(trust_env=False)), model
