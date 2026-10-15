from __future__ import annotations

from types import SimpleNamespace

import pytest
from openai.types.responses.response_output_message import ResponseOutputMessage

from agent_hub.models.responses import ResponsesContractError, parse_response
from agent_hub.models.types import ModelCapability, ModelRequest, ToolDefinition
from tests.contracts.test_responses_client import message, request, response

FINAL = '{"verdict":"approve"}'


def sdk_message(text: str, phase: str | None) -> ResponseOutputMessage:
    return ResponseOutputMessage.model_validate({
        'id': 'msg_synthetic', 'type': 'message', 'role': 'assistant',
        'status': 'completed', 'phase': phase,
        'content': [{'type': 'output_text', 'text': text, 'annotations': []}],
    })


def tool() -> SimpleNamespace:
    return SimpleNamespace(type='function_call', status='completed',
                           call_id='synthetic_call', name='read_context', arguments='{}')


def tool_request() -> ModelRequest:
    return request(
        required_capabilities=frozenset({
            ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOL_CALLING,
        }),
        tools=(ToolDefinition(name='read_context', description='Read context',
                              parameters={'type': 'object'}),),
    )


@pytest.mark.parametrize('phase', [None, 'final_answer'])
@pytest.mark.parametrize('native', [False, True])
def test_commentary_does_not_count_as_another_final(phase: str | None, native: bool) -> None:
    progress = sdk_message('Checking inputs.', 'commentary') if native else message(
        'Checking inputs.', phase='commentary',
    )
    final = sdk_message(FINAL, phase) if native else message(FINAL, phase=phase)
    result = parse_response(response(output=[progress, final]), request())
    assert result.text == FINAL
    assert result.tool_calls == ()


def test_commentary_can_precede_a_tool_call_without_becoming_final_text() -> None:
    result = parse_response(response(output=[
        sdk_message('Checking project context.', 'commentary'), tool(),
    ]), tool_request())
    assert result.text is None
    assert [call.name for call in result.tool_calls] == ['read_context']
    assert dict(result.tool_calls[0].arguments) == {}


def test_commentary_even_if_valid_json_cannot_complete_the_turn() -> None:
    with pytest.raises(ResponsesContractError) as caught:
        parse_response(response(output=[sdk_message(FINAL, 'commentary')]), request())
    assert caught.value.evidence is not None
    assert caught.value.evidence.status == 'unknown'
    assert caught.value.evidence.reason == 'invalid_output'
    assert not caught.value.evidence.correction_eligible


@pytest.mark.parametrize('phase', ['private_phase', 7, True, ['final_answer']])
def test_unknown_message_phase_is_not_silently_treated_as_final(phase: object) -> None:
    with pytest.raises(ResponsesContractError):
        parse_response(response(output=[message(FINAL, phase=phase)]), request())


@pytest.mark.parametrize('phase', [None, 'final_answer'])
def test_real_final_text_still_cannot_mix_with_tools(phase: str | None) -> None:
    with pytest.raises(ResponsesContractError):
        parse_response(response(output=[message(FINAL, phase=phase), tool()]), tool_request())


def test_multiple_real_finals_remain_rejected() -> None:
    with pytest.raises(ResponsesContractError):
        parse_response(response(output=[message(FINAL), message(FINAL, phase='final_answer')]),
                       request())


def test_commentary_cannot_hide_incomplete_or_refusal() -> None:
    for progress in (
        message('Working.', phase='commentary', status='incomplete'),
        message(phase='commentary', content=[{'type': 'refusal', 'refusal': 'No.'}]),
    ):
        with pytest.raises(ResponsesContractError):
            parse_response(response(output=[progress, message(FINAL)]), request())


def test_commentary_is_counted_toward_the_output_byte_budget() -> None:
    with pytest.raises(ResponsesContractError) as caught:
        parse_response(response(output=[message('x' * 65_537, phase='commentary'), tool()]),
                       tool_request())
    assert caught.value.evidence is not None
    assert caught.value.evidence.reason == 'output_limit'
    assert not caught.value.evidence.correction_eligible
