"""Duplo de teste: somente a geracao do LLM e substituida; Runner e SQLite sao reais."""
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field


class ScriptedModel(BaseLlm):
    model: str = 'scripted-offline-test'
    script: list[dict] = Field(default_factory=list, exclude=True)
    requests: list = Field(default_factory=list, exclude=True)

    def call(self, name, **args):
        self.script.append({'name': name, 'args': args})

    def answer(self, text='Resultado obtido pela ferramenta.'):
        self.script.append({'text': text})

    async def generate_content_async(self, llm_request, stream=False):
        self.requests.append(llm_request)
        if not self.script:
            raise AssertionError('LLM foi invocado sem uma resposta prevista pelo teste.')
        item = self.script.pop(0)
        if 'text' in item:
            part = types.Part(text=item['text'])
        else:
            available = llm_request.tools_dict
            if item['name'] not in available:
                raise AssertionError(f'Ferramenta {item["name"]} ausente de {list(available)}')
            part = types.Part(function_call=types.FunctionCall(name=item['name'], args=item['args']))
        yield LlmResponse(content=types.Content(role='model', parts=[part]))
