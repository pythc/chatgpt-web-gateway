"""MCP tools/list -> OpenAI tools -> MCP tools/call -> role=tool."""
import asyncio
import json
from pathlib import Path
import sys
from mcp import Client, StdioServerParameters
from common import async_client


async def main():
    params = StdioServerParameters(command=sys.executable,
        args=[str(Path(__file__).with_name('mcp_server.py'))])
    async with Client(params, read_timeout_seconds=120) as mcp:
        listed = await mcp.list_tools()
        names = {'mcp__example__' + entry.name: entry.name for entry in listed.tools}
        tools = [{'type': 'function', 'function': {'name': 'mcp__example__' + entry.name,
                  'description': entry.description or entry.name, 'parameters': entry.input_schema}}
                 for entry in listed.tools]
        client, model = async_client()
        messages = [{'role': 'user', 'content': 'Use fixture_lookup with key mcp_sample. Return its exact value.'}]
        async with client:
            for turn in range(4):
                reply = await client.chat.completions.create(model=model, messages=messages,
                    tools=tools, tool_choice='required' if turn == 0 else 'auto',
                    parallel_tool_calls=False, max_tokens=1024)
                message = reply.choices[0].message
                if not message.tool_calls:
                    print(message.content)
                    return
                messages.append(message.model_dump(exclude_none=True))
                for call in message.tool_calls:
                    if call.function.name not in names:
                        raise ValueError('Undeclared MCP function')
                    result = await mcp.call_tool(names[call.function.name], json.loads(call.function.arguments))
                    # MCP text blocks and structured content travel as tool data.
                    payload = {'is_error': result.is_error, 'structured_content': result.structured_content,
                               'content': [part.model_dump(mode='json') for part in result.content]}
                    messages.append({'role': 'tool', 'tool_call_id': call.id,
                                     'content': json.dumps(payload, ensure_ascii=False)})
    raise RuntimeError('MCP loop exceeded four model turns')


if __name__ == '__main__':
    asyncio.run(main())
