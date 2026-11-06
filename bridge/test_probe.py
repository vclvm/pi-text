import json

import httpx

import probe

DEPLOYMENT = {"model_name": "glm-latest",
              "litellm_params": {"model": "hosted_vllm/zai-org/GLM-5.3-FP8", "api_base": "http://gpu1:8000/v1"},
              "model_info": {"id": "abc", "max_input_tokens": 1000000, "supports_reasoning": True}}


def router(model_info=True, group_info=True, reply_model="zai-org/GLM-5.3-FP8"):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/model/info" and model_info:
            return httpx.Response(200, json={"data": [DEPLOYMENT]})
        if path == "/v1/model_group/info" and group_info:
            return httpx.Response(200, json={"data": [{"model_group": "glm-latest", "providers": ["hosted_vllm"],
                                                       "max_input_tokens": 1000000}]})
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "glm-latest"}]})
        if path == "/v1/chat/completions":
            if json.loads(request.content).get("stream"):
                chunk = {"model": reply_model, "choices": [{"delta": {"content": "OK"}}]}
                return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
                                      headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json={"model": reply_model, "choices": [{"message": {"content": "OK"}}]},
                                  headers={"x-litellm-model-id": "abc", "x-litellm-model-api-base": "http://gpu1:8000/v1"})
        return httpx.Response(404, json={"detail": "Not Found"})
    return handle


def make(handler) -> probe.Probe:
    p = probe.Probe("http://router.test/v1", "k", 0, 5, False)
    p.client = httpx.Client(transport=httpx.MockTransport(handler))
    return p


def test_identity_shows_the_deployment_reply_model_and_headers(capsys):
    assert make(router()).identity(["glm-latest"]) == 0
    text = capsys.readouterr().out
    assert "deployment → hosted_vllm/zai-org/GLM-5.3-FP8   at http://gpu1:8000/v1" in text
    assert "max_input_tokens=1000000" in text and "supports_reasoning=True" in text
    assert "reply's model field: zai-org/GLM-5.3-FP8" in text
    assert "x-litellm-model-api-base: http://gpu1:8000/v1" in text


def test_identity_falls_back_to_model_group_info(capsys):
    make(router(model_info=False)).identity(["glm-latest"])
    text = capsys.readouterr().out
    assert "model_group/info: limits and providers" in text
    assert "providers=['hosted_vllm']" in text and "deployment →" not in text


def test_identity_without_router_info(capsys):
    make(router(model_info=False, group_info=False, reply_model="glm-latest")).identity(["glm-latest"])
    text = capsys.readouterr().out
    assert "doesn't open /model/info or /model_group/info" in text
    assert "reply's model field: glm-latest" in text


def test_models_lists_deployment_and_streamed_reply_model(capsys):
    assert make(router()).models([], [], True, False, False) == 0
    text = capsys.readouterr().out
    assert "→ hosted_vllm/zai-org/GLM-5.3-FP8   (the router's /model/info)" in text
    assert "(answered as zai-org/GLM-5.3-FP8)" in text
