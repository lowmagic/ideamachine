"""One request to a model, for a JSON answer to a schema: Claude directly, or any other model through OpenRouter.

A model named with a provider prefix (`openai/gpt-6.1-sol`, `z-ai/glm-5.3`) goes to OpenRouter; a bare `claude-*`
name goes to Anthropic, or under zero data retention (`[egress] zdr`) to OpenRouter's endpoints that keep nothing
(decision 0008, amended). Both come back as a `Reply`, so every caller logs and costs every request the same way,
through `egress.ask`. OpenRouter reports what each request cost, so its models need no entry in PRICES.
"""

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

FALLBACK_MODEL = "claude-opus-4-8"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# A request's output budget, reasoning included where the provider counts it there (OpenRouter does). 16,000 left
# DeepSeek V4.1 Flash at medium effort no room for its answer on 9 of 150 trial episodes (2026-11-08), so OpenRouter
# models get 32,000. Claude keeps 16,000: the SDK refuses, unsent, a request that isn't streamed and could run past 10
# minutes, which it takes to be over CLAUDE_CEILING. Only what's spent is billed, and the budget isn't part of a
# prompt's version, so changing it re-reads nothing.
MAX_TOKENS = 32000
CLAUDE_MAX_TOKENS = 16000
CLAUDE_CEILING = 21333   # anthropic._base_client: 3600 s * max_tokens / 128,000 must stay within 600 s
# $ per million tokens (input, output) for Anthropic's models; a refusal fallback may answer on another model.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-opus-4-8": (5.0, 25.0), "claude-sonnet-5-5": (2.0, 10.0),
          "claude-haiku-5-5": (0.10, 0.50)}   # Haiku 5.5: prompts up to 100K tokens, which every episode is
# OpenRouter's finish reasons, in Anthropic's stop-reason words (the egress log's vocabulary).
FINISH = {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal", "tool_calls": "tool_use"}


class Stop(Exception):
    """Send nothing more this run, for the reason it says: the stage stops there, and the run goes on."""


class Unavailable(Stop):
    """The provider couldn't answer now (connection, rate limit, server error): try again on a later run."""

    def __init__(self, why: str):
        super().__init__(f"API unavailable, will retry next run: {why}")


def _later(status: int | None) -> bool:
    """An HTTP status worth retrying on a later run: a rate limit or a server error."""
    return status is not None and (status == 429 or status >= 500)


def _account(status: int | None) -> bool:
    """An HTTP status every request would get alike: the key rejected, not allowed, or out of credit."""
    return status in (401, 402, 403)


# Seconds to wait before each retry of a request that failed for now (no connection, a timeout, a rate limit, a server
# error), before giving up until a later run. A request that timed out may have been answered and billed all the same:
# at these prices a second charge beats a stage that stops at the first hiccup of a long run.
RETRY_DELAYS = (2, 8, 30)


def _with_retries(send):
    """`send()`, again after each of RETRY_DELAYS while it raises Unavailable; the last Unavailable is raised."""
    for delay in (*RETRY_DELAYS, None):
        try:
            return send()
        except Unavailable:
            if delay is None:
                raise
            time.sleep(delay)


@dataclass(frozen=True)
class Reply:
    text: str | None              # the JSON answer; None when refused, or when no answer was written
    stop_reason: str
    served_by: str
    request_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None

    @property
    def refused(self) -> bool:
        return self.stop_reason == "refusal"


def via_openrouter(model: str) -> bool:
    return "/" in model


def version(*parts) -> str:
    """A short hash of what a request is asked under (its prompt, schema, model, effort...): what it's logged and
    its answers kept under. The same parts give the same version, whichever stage asks."""
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:12]


def price(model: str) -> tuple[float, float] | None:
    """$ per million tokens (input, output) for an Anthropic model, also under a dated id (claude-opus-5-5-20261015)."""
    return PRICES.get(model) or PRICES.get(re.sub(r"-\d{8}$", "", model))


def cost(model: str, usage) -> float | None:
    """What an Anthropic request cost, from its usage. A model that answered with no known price (a fallback added
    later, say) is counted at the dearest known price: the cap errs toward stopping early, never toward $0."""
    if usage is None:
        return None
    p = price(model) or max(PRICES.values())
    tokens_in = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * 1.25 \
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * 0.1
    return round(tokens_in * p[0] / 1e6 + (usage.output_tokens or 0) * p[1] / 1e6, 6)


def client_for(model: str, zdr: bool = False):
    """The client a model is asked through: OpenRouter for a provider-prefixed model, and for Claude too under zero
    data retention (`[egress] zdr`), since Anthropic's own API keeps requests; Anthropic's SDK otherwise."""
    if via_openrouter(model) or zdr:
        return OpenRouter(zdr=zdr)
    import anthropic
    return anthropic.Anthropic()


def openrouter_id(model: str) -> str:
    """OpenRouter's name for a model: a Claude id as Anthropic names it (claude-opus-5-5, also dated) becomes
    anthropic/claude-opus-5.5; a provider-prefixed id stays as it is."""
    m = re.fullmatch(r"claude-([a-z]+)-(\d+)-(\d+)(?:-\d{8})?", model)
    return f"anthropic/claude-{m.group(1)}-{m.group(2)}.{m.group(3)}" if m else model


def max_tokens_for(model: str) -> int:
    return MAX_TOKENS if via_openrouter(model) else CLAUDE_MAX_TOKENS


def complete(client, model: str, effort: str, system: str, schema: dict, text: str,
             max_tokens: int | None = None) -> Reply:
    """Ask once, with `max_tokens` of output (None: max_tokens_for the model). Opus asked through Anthropic falls back
    to FALLBACK_MODEL on a refusal; other models, and Claude through OpenRouter, don't, so their refusal counts as
    one. Raises Unavailable when it's worth retrying later, and Stop when no request would get through (the
    credentials, the account's credit, a model id that doesn't exist, a budget Claude would need streamed). An
    exception raised after an earlier attempt was paid for carries that cost as `cost_usd`, for the egress log."""
    if via_openrouter(model) or isinstance(client, OpenRouter):
        # Claude under zero data retention: at its list price, which leaves out the regional endpoints listed 10%
        # above it.
        return client.complete(openrouter_id(model), effort, system, schema, text, max_tokens or MAX_TOKENS,
                               max_price=price(model))
    max_tokens = max_tokens or CLAUDE_MAX_TOKENS
    if max_tokens > CLAUDE_CEILING:
        raise Stop(f"max_tokens {max_tokens} is over {CLAUDE_CEILING}, past which Claude needs a streamed request: "
                   "nothing sent")
    import anthropic
    fallback = {"betas": ["server-side-fallback-2026-06-01"], "fallbacks": [{"model": FALLBACK_MODEL}]} \
        if model.startswith("claude-opus") else {}
    try:
        r = client.beta.messages.create(
            # The system prompt is the same for every request of a stage, so it's cached: a read costs a tenth of the
            # input price and a write 1.25 times (cost). One shorter than the model's minimum just isn't cached.
            model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": text}],
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}}, **fallback)
    except anthropic.APIConnectionError as e:
        raise Unavailable(f"{type(e).__name__}: {e}") from e
    except anthropic.NotFoundError as e:
        raise Stop(f"Claude has no model {model}: {e}") from e
    except anthropic.APIStatusError as e:
        if _later(e.status_code):
            raise Unavailable(f"{type(e).__name__}: {e}") from e
        kind = ((e.body if isinstance(e.body, dict) else {}).get("error") or {}).get("type")
        if _account(e.status_code) or kind == "billing_error":
            raise Stop(f"Claude credentials or billing rejected: {e}") from e
        raise
    served = getattr(r, "model", model) or model
    answer = None if r.stop_reason == "refusal" else next((b.text for b in r.content if b.type == "text"), None)
    return Reply(answer, r.stop_reason, served, getattr(r, "_request_id", None), r.usage.input_tokens,
                 r.usage.output_tokens, cost(served, r.usage))


def post_json(url: str, key: str, body: dict, what: str, timeout: int, headers: dict | None = None,
              transient=lambda answer: None) -> dict:
    """POST a JSON body and read the JSON answer, retried (RETRY_DELAYS) while it fails for now: a rate limit, a server
    error or no connection, or an answer `transient(answer)` names an error worth retrying. Then that's Unavailable; a
    rejected key or no credit is Stop; any other HTTP error is a RuntimeError."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", **(headers or {})})

    def once() -> dict:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                answer = json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if _later(e.code):
                raise Unavailable(f"{what}, HTTP {e.code}: {detail}") from None
            if _account(e.code):
                raise Stop(f"{what}, HTTP {e.code}: {detail}") from None
            raise RuntimeError(f"{what}, HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise Unavailable(f"{what}: {e}") from None
        if why := transient(answer):
            raise Unavailable(f"{what}: {why}")
        return answer
    return _with_retries(once)


def _transient_error(answer: dict) -> str | None:
    """An error OpenRouter reported in an answer's body (a provider's rate limit or outage) worth retrying, or None."""
    error = answer.get("error") if "choices" not in answer else None
    code = (error or {}).get("code")
    return f"error {code}: {str(error.get('message'))[:300]}" if isinstance(code, int) and _later(code) else None


NO_SCHEMA: set[str] = set()   # models found, in this process, to take no JSON schema: asked with it in the prompt


class OpenRouter:
    """OpenRouter's chat completions, asking for strict JSON-schema output and the request's cost. An answer that
    isn't JSON is asked for once more: models differ in how well they keep to a schema. A model with no provider
    that takes a schema is asked again with the schema in its instructions instead. With `zdr`, only endpoints that
    keep nothing are used (Bedrock and Vertex, for Claude). (Jev isn't a chat model: it has its own client, im.jev.)"""

    def __init__(self, key: str | None = None, post=None, zdr: bool = False):
        self.key, self.zdr = key or os.environ.get("OPENROUTER_API_KEY"), zdr
        if not self.key and post is None:
            raise SystemExit("OPENROUTER_API_KEY isn't set: OpenRouter models need it")
        self.post = post or self._post

    def _post(self, body: dict) -> dict:
        return post_json(OPENROUTER_URL, self.key, body, "OpenRouter", 600, {"X-Title": "Idea Machine"},
                         transient=_transient_error)

    def complete(self, model, effort, system, schema, text, max_tokens, max_price=None) -> Reply:
        """`max_price`: ($/M in, $/M out) above which no endpoint is used."""
        if model not in NO_SCHEMA:
            try:
                return self._complete(model, effort, system, schema, text, max_tokens, False, max_price)
            except RuntimeError as e:
                if "data policy" in str(e):
                    raise RuntimeError(f"{model}: no provider that keeps nothing sent ({e})") from None
                if max_price and "No endpoints found" in str(e):   # not a model that takes no schema
                    raise RuntimeError(f"{model}: no endpoint at its list price {max_price} ({e})") from None
                if "No endpoints found" not in str(e):
                    raise
                NO_SCHEMA.add(model)
        return self._complete(model, effort, system, schema, text, max_tokens, True, max_price)

    def _complete(self, model, effort, system, schema, text, max_tokens, in_prompt: bool, max_price=None) -> Reply:
        """The answer, or after a second answer that isn't JSON either, that one as it came (both paid for): the
        caller logs it, and it fails there."""
        if in_prompt:
            system += ("\n\nAnswer with only a JSON object, no other text, matching this JSON Schema:\n"
                       + json.dumps(schema))
        # Claude caches only what's marked, as through Anthropic: the system prompt, the same for every request of a
        # stage. Other models cache on their own, or not at all.
        cached = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        body = {"model": model, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": cached if model.startswith("anthropic/") else system},
                             {"role": "user", "content": text}],
                "reasoning": {"effort": effort}, "usage": {"include": True},
                # Only providers that honor the schema, and none that keep or train on what's sent.
                "provider": {"require_parameters": not in_prompt, "data_collection": "deny",
                             **({"zdr": True} if self.zdr else {}),
                             **({"max_price": {"prompt": max_price[0], "completion": max_price[1]}}
                                if max_price else {})}}
        if not in_prompt:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "answer", "strict": True, "schema": schema}}
        spent, tokens_in, tokens_out = 0.0, 0, 0
        for attempt in (1, 2):
            try:
                r = self.post(body)
                if "error" in r and "choices" not in r:
                    code = r["error"].get("code")
                    msg = f"OpenRouter error {code}: {str(r['error'].get('message'))[:300]}"
                    if isinstance(code, int) and _later(code):
                        raise Unavailable(msg)
                    raise (Stop if isinstance(code, int) and _account(code) else RuntimeError)(msg)
                usage = r.get("usage") or {}
                spent += usage.get("cost") or 0.0
                tokens_in += usage.get("prompt_tokens") or 0
                tokens_out += usage.get("completion_tokens") or 0
                choice = r["choices"][0]
            except Exception as e:   # the attempts before this one were paid for all the same
                e.cost_usd = round(spent, 6)
                raise
            stop = FINISH.get(choice.get("finish_reason"), choice.get("finish_reason") or "end_turn")
            answer = _unfenced((choice.get("message") or {}).get("content"))
            if stop == "refusal" or (choice.get("message") or {}).get("refusal"):
                answer, stop = None, "refusal"
            elif not _is_json(answer) and attempt == 1:
                continue
            return Reply(answer, stop, r.get("model") or model, r.get("id"), tokens_in, tokens_out,
                         round(spent, 6))
        raise AssertionError("unreachable")


def _unfenced(text):
    """A JSON answer without the ```json fence some models wrap it in when asked in the prompt."""
    if isinstance(text, str) and (m := re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", text, re.S)):
        return m.group(1)
    return text


def _is_json(text) -> bool:
    try:
        json.loads(text)
        return True
    except (TypeError, ValueError):
        return False
