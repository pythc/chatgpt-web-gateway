"""A standard OpenAI SDK Chat Completions request."""
from common import sync_client


def main():
    client, model = sync_client()
    with client:
        reply = client.chat.completions.create(model=model, max_tokens=128,
                messages=[{'role': 'user', 'content': 'Reply with exactly OK.'}])
        print(reply.choices[0].message.content)


if __name__ == '__main__':
    main()
