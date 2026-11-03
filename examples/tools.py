"""Standard tool_calls -> client execution -> role=tool -> final answer."""
import json
from common import sync_client

TOOLS = [{'type': 'function', 'function': {
    'name': 'add', 'description': 'Add two integers.', 'parameters': {
        'type': 'object', 'properties': {'a': {'type': 'integer'}, 'b': {'type': 'integer'}},
        'required': ['a', 'b'], 'additionalProperties': False}}}]


def main():
    client, model = sync_client()
    messages = [{'role': 'user', 'content': 'Use add to compute 17 + 25, then report its result.'}]
    with client:
        for turn in range(4):
            reply = client.chat.completions.create(model=model, messages=messages, tools=TOOLS,
                tool_choice={'type': 'function', 'function': {'name': 'add'}} if turn == 0 else 'auto',
                parallel_tool_calls=False, max_tokens=1024)
            message = reply.choices[0].message
            if not message.tool_calls:
                print(message.content)
                return
            messages.append(message.model_dump(exclude_none=True))
            for call in message.tool_calls:
                if call.function.name != 'add':
                    raise ValueError('Undeclared function')
                args = json.loads(call.function.arguments)
                if set(args) != {'a', 'b'} or any(type(args[k]) is not int for k in args):
                    raise ValueError('Invalid function arguments')
                result = {'sum': args['a'] + args['b']}
                messages.append({'role': 'tool', 'tool_call_id': call.id, 'content': json.dumps(result)})
    raise RuntimeError('Tool loop exceeded four model turns')


if __name__ == '__main__':
    main()
