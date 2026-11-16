"""Responses conversion with stable cache keys and optional response cursors."""
from copy import deepcopy
import json


class ResponsesIncompleteError(RuntimeError):
    """Incomplete for a reason that must not enter the output-limit retry loop."""


def read_stream(stream, emit):
    """Read SSE frames, retaining provider events and requiring a terminal response."""
    parts, terminal = [], None
    def consume():
        nonlocal terminal
        if not parts:
            return
        raw = '\n'.join(parts)
        parts.clear()
        if raw == '[DONE]':
            return
        event = json.loads(raw)
        emit(event)
        kind = event.get('type', '')
        if kind == 'error':
            from .provider_recovery import provider_error
            raise provider_error(event)
        if kind in {'response.completed', 'response.incomplete', 'response.failed'}:
            terminal = event['response']
    for raw in stream:
        line = raw.decode('utf-8').rstrip('\r\n')
        if not line:
            consume()
        elif line.startswith('data:'):
            parts.append(line[5:].lstrip(' '))
    consume()
    if terminal is None:
        raise RuntimeError('Responses stream ended without a terminal response; refusing silent replay')
    return terminal


def projected_message(output):
    calls, text = [], []
    for item in output:
        if item.get('type') == 'function_call':
            calls.append({'id': item['call_id'], 'type': 'function',
                          'function': {'name': item['name'], 'arguments': item['arguments']}})
        elif item.get('type') == 'message':
            text.extend(part.get('text', part.get('refusal', '')) for part in item.get('content', [])
                        if part.get('type') in ('output_text', 'refusal'))
    return '\n'.join(text), calls


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ''.join(part.get('text', '') for part in value
                       if isinstance(part, dict) and part.get('type') == 'text')
    return ''


def split_instructions(messages):
    """Move leading system messages to the Responses top-level instructions field."""
    parts, index = [], 0
    while index < len(messages) and messages[index].get('role') == 'system':
        text = _text(messages[index].get('content'))
        if text:
            parts.append(text)
        index += 1
    return '\n\n'.join(parts), messages[index:]


def conversation_items(messages):
    instructions, body = split_instructions(messages)
    return instructions, build_items(body)


def build_items(messages):
    items = []
    for message in messages:
        if message.get('responses_items') is not None:
            text, calls = projected_message(message['responses_items'])
            if text == (message.get('content') or '') and calls == (message.get('tool_calls') or []):
                items.extend(deepcopy(message['responses_items']))
                continue
            # A model-view policy edited this message. Rebuild from that view,
            # without replaying stale text or opaque reasoning tied to it.
        role = message.get('role', 'user')
        if role == 'tool':
            items.append({'type': 'function_call_output', 'call_id': message['tool_call_id'],
                          'output': message.get('content') or ''})
            continue
        content = message.get('content')
        if isinstance(content, list):
            converted = []
            for part in content:
                if part.get('type') == 'text':
                    converted.append({'type': 'output_text' if role == 'assistant' else 'input_text', 'text': part['text']})
                elif part.get('type') == 'image_url':
                    image = part['image_url']
                    converted.append({'type': 'input_image', 'image_url': image['url'],
                                      **({'detail': image['detail']} if 'detail' in image else {})})
                else:
                    raise ValueError('Unsupported Responses content part')
            content = converted
        if content:
            items.append({'role': role, 'content': content})
        for call in message.get('tool_calls') or []:
            items.append({'type': 'function_call', 'call_id': call['id'], **call['function']})
    return items


def request(messages, tools, model, effort=None, output_limit=None, *,
            cache_key=None, previous_response_id=None, store=False,
            input_items=None, prompt_cache_retention=None, reasoning_summary=None):
    instructions, body = split_instructions(messages)
    items = deepcopy(input_items) if input_items is not None else build_items(body)
    payload = {'model': model, 'input': items, 'store': bool(store or previous_response_id),
               'include': ['reasoning.encrypted_content']}
    if instructions:
        payload['instructions'] = instructions
    if cache_key:
        payload['prompt_cache_key'] = cache_key
    if prompt_cache_retention:
        payload['prompt_cache_retention'] = prompt_cache_retention
    if previous_response_id:
        payload['previous_response_id'] = previous_response_id
    if effort is not None:
        payload['reasoning'] = {'effort': effort}
    if reasoning_summary is not None:
        payload.setdefault('reasoning', {})['summary'] = reasoning_summary
    if tools:
        payload['tools'] = sorted(
            [{'type': 'function', **tool['function']} for tool in tools],
            key=lambda item: item.get('name', ''),
        )
        payload['tool_choice'] = 'auto'
    if output_limit is not None:
        payload['max_output_tokens'] = output_limit
    return payload


def response(value):
    output = value.get('output', [])
    calls, text = [], []
    unsupported = []
    for item in output:
        kind = item.get('type')
        if kind == 'function_call':
            calls.append({'id': item['call_id'], 'type': 'function',
                          'function': {'name': item['name'], 'arguments': item['arguments']}})
        elif kind == 'message':
            text.extend(part.get('text', part.get('refusal', '')) for part in item.get('content', [])
                        if part.get('type') in ('output_text', 'refusal'))
        elif kind == 'web_search_call':
            # Built-in web search runs at the provider. The call item carries
            # no local tool result, but it is part of the response history.
            continue
        elif kind != 'reasoning':
            unsupported.append(str(kind or 'unknown'))
    status = value.get('status')
    reason = (value.get('incomplete_details') or {}).get('reason')
    if status == 'incomplete' and reason != 'max_output_tokens':
        raise ResponsesIncompleteError('Responses incomplete: ' + str(reason or 'unspecified'))
    if status in ('failed', 'cancelled') or value.get('error'):
        from .provider_recovery import provider_error
        raise provider_error(value if value.get('error') else {'message': 'Responses request ' + str(status)})
    # Unknown provider extensions are not replayed as input: the provider may
    # reject them, and the model is told to retry without them. Supported
    # built-in search items remain in history. The raw sampling response is
    # still retained by the caller's audit path.
    replayable = [item for item in output if item.get('type') not in unsupported]
    message = {'role': 'assistant', 'content': '\n'.join(text),
               'tool_calls': calls, 'responses_items': deepcopy(replayable)}
    if unsupported:
        message['_unsupported_output_items'] = list(dict.fromkeys(unsupported))
    if value.get('id'):
        message['_response_id'] = value['id']
    return {'choices': [{'message': message,
                         'finish_reason': 'length' if status == 'incomplete' else 'stop'}],
            'usage': value.get('usage')}
