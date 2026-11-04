"""Typed SSE events. Never turn failure or a partial tool call into success."""
import copy
import json
import time
from api.responses_protocol import identifier


def response_object(model, response_id=None):
    return {'id': response_id or identifier('resp'), 'object': 'response', 'created_at': int(time.time()),
            'status': 'in_progress', 'model': model, 'output': [], 'error': None,
            'incomplete_details': None, 'usage': None, 'parallel_tool_calls': True,
            'tool_choice': 'auto', 'tools': [], 'store': False}


class EventWriter:
    def __init__(self):
        self.sequence = 0

    def frame(self, kind, **fields):
        event = {'type': kind, 'sequence_number': self.sequence, **fields}
        self.sequence += 1
        return 'event: ' + kind + '\ndata: ' + json.dumps(event, ensure_ascii=False) + '\n\n'

    def item(self, item, index):
        shell = copy.deepcopy(item)
        kind = item['type']
        if kind == 'message':
            shell.update(status='in_progress', content=[])
        elif kind in ('custom_tool_call', 'function_call'):
            shell['status'] = 'in_progress'
            shell['input' if kind == 'custom_tool_call' else 'arguments'] = ''
        yield self.frame('response.output_item.added', output_index=index, item=shell)
        if kind == 'message':
            for part_index, part in enumerate(item['content']):
                yield self.frame('response.content_part.added', item_id=item['id'], output_index=index,
                                 content_index=part_index, part={**part, 'text': ''})
                yield self.frame('response.output_text.delta', item_id=item['id'], output_index=index,
                                 content_index=part_index, delta=part['text'])
                yield self.frame('response.output_text.done', item_id=item['id'], output_index=index,
                                 content_index=part_index, text=part['text'])
                yield self.frame('response.content_part.done', item_id=item['id'], output_index=index,
                                 content_index=part_index, part=part)
        elif kind in ('custom_tool_call', 'function_call'):
            value = item['input' if kind == 'custom_tool_call' else 'arguments']
            prefix = 'response.custom_tool_call_input' if kind == 'custom_tool_call' else 'response.function_call_arguments'
            yield self.frame(prefix + '.delta', item_id=item['id'], output_index=index, delta=value)
            field = 'input' if kind == 'custom_tool_call' else 'arguments'
            yield self.frame(prefix + '.done', item_id=item['id'], output_index=index, **{field: value})
        yield self.frame('response.output_item.done', output_index=index, item=item)

    def replay(self, response):
        yield self.frame('response.created', response={**response, 'status': 'in_progress', 'output': []})
        yield self.frame('response.in_progress', response={**response, 'status': 'in_progress', 'output': []})
        for index, item in enumerate(response['output']):
            yield from self.item(item, index)
        yield self.frame('response.' + response['status'], response=response)
