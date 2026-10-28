"""Best effort request-scoped NanoGPT usage headers."""

import json
import math

from nanogpt_client import request_usage


def usage_header(calls):
    models = {}
    prompt = completion = 0
    costs = []
    for call in calls:
        model = models.setdefault(call["model"], {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0})
        model["prompt_tokens"] += call["prompt_tokens"]
        model["completion_tokens"] += call["completion_tokens"]
        model["calls"] += 1
        prompt += call["prompt_tokens"]
        completion += call["completion_tokens"]
        cost = call["cost"]
        costs.append(float(cost) if cost is not None else None)
    cost = sum(costs) if all(c is not None and math.isfinite(c) for c in costs) else None
    summary = {"calls": len(calls), "prompt_tokens": prompt, "completion_tokens": completion,
               "cost": cost, "models": models}
    encoded = json.dumps(summary, separators=(",", ":"), ensure_ascii=True)
    # Keep aggregate totals when the model breakdown cannot fit in the header.
    while len(encoded) >= 2000 and models:
        models.pop(next(reversed(models)))
        encoded = json.dumps(summary, separators=(",", ":"), ensure_ascii=True)
    return encoded.encode("ascii") if len(encoded) < 2000 else None


class LLMUsageMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        calls = []
        token = request_usage.set(calls)

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                try:
                    if calls:
                        value = usage_header(calls)
                        if value is not None:
                            message = dict(message)
                            message["headers"] = list(message.get("headers", [])) + [(b"x-c4-llm-usage", value)]
                except Exception:
                    pass
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            request_usage.reset(token)
