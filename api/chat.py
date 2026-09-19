"""api.chat — assistente de chat sobre a projeção em tela, via OpenRouter.

O front envia a conversa + um "contexto" (JSON com o que o usuário está vendo:
estudo, item, cards, série, projeção, comparações). Aqui o contexto é embutido no
prompt de sistema e a chamada segue o formato OpenAI-compatível do OpenRouter
(`POST {base}/chat/completions`), o mesmo para qualquer modelo.

Chave da API — duas formas:
  1. `.env` na raiz do projeto (`OPENROUTER_API_KEY=...`), lido a cada chamada;
  2. digitada na própria tela (`PUT /chat/key`): fica só na MEMÓRIA do servidor
     enquanto a aplicação rodar (não vai para disco, log, navegador ou banco).
Se as duas existirem, vale a digitada na tela. A chave nunca é devolvida (só os
4 últimos caracteres, para o usuário reconhecer qual está ativa).

Outras variáveis (opcionais): OPENROUTER_DEFAULT_MODEL, OPENROUTER_MODELS (ids
separados por vírgula, substitui a lista do seletor), OPENROUTER_BASE_URL,
OPENROUTER_TIMEOUT, OPENROUTER_MAX_TOKENS.

Preços e contexto do seletor vêm do catálogo público do OpenRouter (`/models`,
sem chave, cache de 1 h); sem acesso a ele usa-se o retrato salvo abaixo.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import time
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/chat")

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_MODEL = "openai/gpt-4o-mini"

# Modelos do seletor, com o retrato do catálogo do OpenRouter em 19/09/2026:
# id -> (nome, USD por 1M tokens de entrada, USD por 1M de saída, contexto).
# Só é usado quando o catálogo ao vivo não está acessível.
_FEATURED: dict[str, tuple[str, float, float, int]] = {
    "anthropic/claude-opus-5": ("Anthropic: Claude Opus 5", 5.0, 25.0, 1_000_000),
    "moonshotai/kimi-k3": ("MoonshotAI: Kimi K3", 1.7, 8.5, 1_048_576),
    "openai/gpt-5.6-sol": ("OpenAI: GPT-5.6 Sol", 2.0, 10.0, 1_050_000),
    "qwen/qwen3.8-max-0902": ("Qwen: Qwen3.8 Max (0902)", 2.0, 6.0, 1_000_000),
    "deepseek/deepseek-v4-flash": ("DeepSeek: DeepSeek V4 Flash 0423", 0.042, 0.084, 1_048_576),
    "anthropic/claude-opus-4.8": ("Anthropic: Claude Opus 4.8", 5.0, 25.0, 1_000_000),
    "openai/gpt-6-astra": ("OpenAI: GPT-6 Astra", 10.0, 50.0, 1_050_000),
    "z-ai/glm-5.3": ("Z.ai: GLM 5.3", 0.91, 2.86, 1_310_720),
    "x-ai/grok-4.6": ("SpaceXAI: Grok 4.6", 2.0, 6.0, 500_000),
    "deepseek/deepseek-v4-pro": ("DeepSeek: DeepSeek V4 Pro 0423", 0.4223, 0.8446, 1_048_576),
    "openai/gpt-4o-mini": ("OpenAI: GPT-4o-mini", 0.15, 0.6, 128_000),
    "google/gemini-3.1-flash-lite": ("Google: Gemini 3.1 Flash Lite", 0.25, 1.5, 1_048_576),
}

_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._\-:/]{1,120}$")
_MAX_MESSAGES = 20
_MAX_MESSAGE_CHARS = 6000
_MAX_CONTEXT_CHARS = 80_000
_CATALOG_TTL = 3600.0  # catálogo ao vivo válido por 1 h
_CATALOG_RETRY = 300.0  # após falha, só tenta de novo em 5 min
_CATALOG_TIMEOUT = 6.0

# Chave digitada na tela: só em memória, vale enquanto o processo viver.
_runtime_key: str | None = None
_catalog_cache: dict = {"at": 0.0, "failed_at": 0.0, "by_id": {}}
_ssl_ctx: ssl.SSLContext | bool | None = None

SYSTEM_PROMPT = """\
Você é o assistente da Forecasting Tool, uma ferramenta local de projeção de séries \
temporais (vendas/demanda mensais) usada por analistas de BI, SFE e dados. Seu papel é \
ajudar a pessoa a ENTENDER e EXPLICAR o modelo e a projeção que ela está vendo na tela.

Regras:
- Responda em português do Brasil, de forma clara e objetiva; listas curtas; sem enrolação.
- Use SOMENTE os números do bloco "CONTEXTO DA TELA". Nunca invente valores, séries, \
métodos ou resultados. Se algo não estiver no contexto, diga que não está visível na tela \
e sugira onde clicar (aba, filtro, método) para obtê-lo.
- Cite valores com o formato do contexto e diga a unidade quando souber (a medida padrão \
é "unidades").
- A projeção é estatística: extrapola padrões do histórico e NÃO conhece promoções, \
lançamentos, ruptura de estoque, sazonalidade regulatória ou outros eventos externos. \
Diga isso quando for relevante, sem repetir a ressalva em toda resposta.
- Ao comparar métodos, explique a diferença de comportamento (nível, tendência, \
sazonalidade) e o que o WAPE de backtest indica, lembrando que erro passado não garante \
erro futuro.
- Não dê recomendação de negócio como certeza; apresente leituras e riscos.

Glossário da ferramenta:
- MAT: soma dos últimos 12 meses. "Último MAT" usa só o histórico observado.
- Variação MAT (YoY): último MAT contra o MAT de 12 meses antes (exige >= 24 meses).
- CAGR do MAT: crescimento médio anual composto entre o primeiro e o último bloco de 12 \
meses (exige >= 36 meses).
- Total no horizonte: soma da projeção dos próximos meses.
- WAPE (backtest): soma dos erros absolutos / soma do volume real, medido em meses \
passados que o método tentou prever. Menor é melhor (20% = errou, em média, 20% do volume).
- Bias (backtest): erro médio por mês (previsto - real), na unidade da série. Positivo = \
superestima; negativo = subestima; perto de 0 = sem tendência.
- Faixa de 80%: intervalo em que se espera que o valor real caia com ~80% de \
probabilidade; quanto mais larga, maior a incerteza.
- Estatísticas do histórico: máxima, mínima, desvio padrão amostral e erro padrão da \
média (desvio / raiz de n) dos valores mensais.
- Piso zero: nenhuma projeção fica abaixo de zero.
- Método automático: quando nenhum método é marcado, o sistema escolhe, por item, o de \
menor WAPE em 1 fold de validação; níveis agregados somam as séries mais detalhadas.
- Métodos: Naive (repete o último valor), SeasonalNaive (repete o mesmo mês do ano \
anterior), MediaMovel3/6/12 (média dos últimos N meses), HistoricAverage (média de \
tudo), RegLinearDrift (tendência linear), Holt/HoltDamped/ETS_Damped (nível + tendência, \
amortecida ou não), AutoETS (suavização exponencial automática com sazonalidade), \
AutoTheta (Theta), AutoCES, AutoARIMA, AutoTBATS, CrostonSBA e TSB (demanda \
intermitente, muitos zeros), LightGBM/XGBoost (aprendizado global entre séries).
"""


# ---------------------------------------------------------------------------
# Configuração (.env) e chave
# ---------------------------------------------------------------------------
def _read_dotenv() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = _ENV_PATH.read_text(encoding="utf-8-sig")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        values[key] = val
    return values


def _setting(name: str, default: str = "") -> str:
    env = os.environ.get(name)
    if env is not None and env.strip():
        return env.strip()
    return _read_dotenv().get(name, default).strip()


def _api_key() -> tuple[str, str | None]:
    """(chave, origem): a digitada na tela tem precedência sobre ambiente/.env."""
    if _runtime_key:
        return _runtime_key, "runtime"
    key = _setting("OPENROUTER_API_KEY")
    return (key, "env") if key else ("", None)


def _base_url() -> str:
    return (_setting("OPENROUTER_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")


def _int_setting(name: str, default: int) -> int:
    try:
        return max(1, int(_setting(name, str(default))))
    except ValueError:
        return default


def _verify():
    """Verificação TLS: usa o repositório de certificados do SO quando houver
    `truststore` (resolve antivírus/proxy que interceptam HTTPS); senão o padrão.
    Nunca desliga a verificação."""
    global _ssl_ctx
    if _ssl_ctx is None:
        try:
            import truststore

            _ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except ImportError:
            _ssl_ctx = True
    return _ssl_ctx


# ---------------------------------------------------------------------------
# Catálogo de modelos (preço / contexto)
# ---------------------------------------------------------------------------
def _live_catalog() -> dict[str, dict]:
    """Catálogo público do OpenRouter por id, com cache. {} se inacessível."""
    now = time.time()
    if _catalog_cache["by_id"] and now - _catalog_cache["at"] < _CATALOG_TTL:
        return _catalog_cache["by_id"]
    if now - _catalog_cache["failed_at"] < _CATALOG_RETRY:
        return _catalog_cache["by_id"]
    try:
        resp = httpx.get(f"{_base_url()}/models", timeout=_CATALOG_TIMEOUT, verify=_verify())
        resp.raise_for_status()
        by_id = {
            m["id"]: m
            for m in resp.json().get("data", [])
            if isinstance(m, dict) and isinstance(m.get("id"), str)
        }
    except (httpx.HTTPError, ValueError, TypeError):
        _catalog_cache["failed_at"] = now
        return _catalog_cache["by_id"]
    if by_id:
        _catalog_cache.update(at=now, failed_at=0.0, by_id=by_id)
    return by_id


def _per_million(value) -> float | None:
    try:
        return round(float(value) * 1_000_000, 4)
    except (TypeError, ValueError):
        return None


def _featured_ids() -> list[str]:
    raw = _setting("OPENROUTER_MODELS")
    ids: list[str] = []
    for item in raw.split(","):
        item = item.strip()
        mid = item.rpartition("=")[2].strip() if "=" in item else item
        if mid and _MODEL_ID_RE.match(mid) and mid not in ids:
            ids.append(mid)
    return ids or list(_FEATURED)


def _model_rows(catalog: dict[str, dict]) -> list[dict]:
    rows = []
    for mid in _featured_ids():
        live = catalog.get(mid)
        snap = _FEATURED.get(mid)
        if live:
            pricing = live.get("pricing") or {}
            rows.append(
                {
                    "id": mid,
                    "label": live.get("name") or mid,
                    "input": _per_million(pricing.get("prompt")),
                    "output": _per_million(pricing.get("completion")),
                    "context": live.get("context_length"),
                    "available": True,
                }
            )
        elif snap and not catalog:
            rows.append(
                {
                    "id": mid,
                    "label": snap[0],
                    "input": snap[1],
                    "output": snap[2],
                    "context": snap[3],
                    "available": True,
                }
            )
        else:  # catálogo ao vivo carregado e o id não está nele (ou id próprio sem retrato)
            rows.append(
                {
                    "id": mid,
                    "label": snap[0] if snap else mid,
                    "input": None,
                    "output": None,
                    "context": None,
                    "available": not catalog,
                }
            )
    return rows


def _default_model(rows: list[dict]) -> str:
    chosen = _setting("OPENROUTER_DEFAULT_MODEL")
    if chosen and _MODEL_ID_RE.match(chosen):
        return chosen
    ids = [r["id"] for r in rows]
    return _DEFAULT_MODEL if _DEFAULT_MODEL in ids else (ids[0] if ids else _DEFAULT_MODEL)


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------
class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)
    model: str | None = None
    context: dict | None = None


class KeyRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=400)


def _key_status() -> dict:
    key, source = _api_key()
    return {
        "configured": bool(key),
        "key_source": source,  # "runtime" (digitada na tela) | "env" (.env/ambiente) | None
        "key_hint": f"…{key[-4:]}" if key and len(key) >= 8 else None,
    }


@router.get("/config")
def chat_config() -> dict:
    """Estado do chat para a tela. Nunca devolve a chave."""
    catalog = _live_catalog()
    rows = _model_rows(catalog)
    return {
        **_key_status(),
        "models": rows,
        "default_model": _default_model(rows),
        "pricing_source": "live" if catalog else "snapshot",
    }


@router.get("/models")
def chat_catalog() -> dict:
    """Todos os ids do catálogo do OpenRouter (campo "Outro modelo"); melhor esforço."""
    return {"models": sorted(_live_catalog())}


@router.put("/key")
def set_key(req: KeyRequest) -> dict:
    """Guarda a chave digitada na tela, só na memória do servidor.

    Confere com `GET {base}/key` (não consome créditos): 401/403 recusa; falha de
    rede ou outro status aceita a chave e avisa que não foi possível validar.
    """
    global _runtime_key
    key = req.api_key.strip()
    if not key or re.search(r"\s", key):
        raise HTTPException(status_code=422, detail="Chave inválida: não pode ter espaços.")
    verified = False
    try:
        resp = httpx.get(
            f"{_base_url()}/key",
            headers={"Authorization": f"Bearer {key}"},
            timeout=15.0,
            verify=_verify(),
        )
        if resp.status_code in (401, 403):
            raise HTTPException(
                status_code=401, detail="O OpenRouter recusou esta chave. Confira e tente de novo."
            )
        verified = resp.status_code == 200
    except httpx.HTTPError:
        verified = False
    _runtime_key = key
    return {**_key_status(), "verified": verified}


@router.delete("/key")
def forget_key() -> dict:
    """Esquece a chave digitada na tela (a do .env, se houver, continua valendo)."""
    global _runtime_key
    _runtime_key = None
    return _key_status()


def _system_message(context: dict | None) -> str:
    if not context:
        return (
            SYSTEM_PROMPT
            + "\nCONTEXTO DA TELA: o usuário optou por não compartilhar os dados da tela. "
            "Explique conceitos em termos gerais e não comente números específicos."
        )
    ctx = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    if len(ctx) > _MAX_CONTEXT_CHARS:
        raise HTTPException(status_code=413, detail="Contexto da tela grande demais.")
    return (
        SYSTEM_PROMPT
        + "\nCONTEXTO DA TELA (JSON; reflete o que o usuário vê agora e é atualizado "
        "a cada mensagem):\n"
        + ctx
    )


def _upstream_error(resp: httpx.Response) -> HTTPException:
    """Traduz erro do OpenRouter em mensagem em PT-BR (sem eco de segredos)."""
    detail = ""
    try:
        err = resp.json().get("error", {})
        detail = str(err.get("message", "")) if isinstance(err, dict) else str(err)
    except ValueError:
        pass
    detail = detail.strip()[:300]
    code = resp.status_code
    if code == 401:
        msg = "Chave do OpenRouter inválida ou expirada. Informe outra chave."
    elif code == 402:
        msg = "Créditos insuficientes no OpenRouter para este modelo."
    elif code == 429:
        msg = "Limite de requisições do OpenRouter atingido. Tente de novo em instantes."
    elif code in (400, 404):
        msg = "O OpenRouter recusou a requisição (verifique o ID do modelo)."
    else:
        msg = f"Falha no OpenRouter (HTTP {code})."
    if detail:
        msg += f" Detalhe: {detail}"
    return HTTPException(status_code=502, detail=msg)


def _extract_reply(body: dict) -> str:
    if isinstance(body.get("error"), dict):
        raise HTTPException(
            status_code=502,
            detail="O modelo devolveu erro: " + str(body["error"].get("message", ""))[:300],
        )
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise HTTPException(status_code=502, detail="Resposta inesperada do OpenRouter.")
    if isinstance(content, list):  # alguns provedores devolvem partes de texto
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    content = (content or "").strip()
    if not content:
        raise HTTPException(status_code=502, detail="O modelo devolveu uma resposta vazia.")
    return content


@router.post("")
def chat(req: ChatRequest) -> dict:
    api_key, _ = _api_key()
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="Chave do OpenRouter não configurada. Informe-a no painel ou no arquivo .env.",
        )
    model = (req.model or "").strip() or _default_model(_model_rows({}))
    if not _MODEL_ID_RE.match(model):
        raise HTTPException(status_code=422, detail="ID de modelo inválido.")

    history = req.messages[-_MAX_MESSAGES:]
    if history[-1].role != "user":
        raise HTTPException(status_code=422, detail="A última mensagem deve ser do usuário.")
    messages = [{"role": "system", "content": _system_message(req.context)}]
    messages += [
        {"role": m.role, "content": m.content[:_MAX_MESSAGE_CHARS]} for m in history
    ]

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": _int_setting("OPENROUTER_MAX_TOKENS", 1500),
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "Forecasting Tool",
    }
    try:
        resp = httpx.post(
            f"{_base_url()}/chat/completions",
            json=payload,
            headers=headers,
            timeout=float(_int_setting("OPENROUTER_TIMEOUT", 120)),
            verify=_verify(),
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="O modelo demorou demais para responder.")
    except httpx.HTTPError as exc:
        if "CERTIFICATE_VERIFY_FAILED" in str(exc):
            detail = (
                "Falha na verificação do certificado HTTPS (antivírus ou proxy?). "
                "Instale o pacote 'truststore' (pip install truststore) e reinicie."
            )
        else:
            detail = "Não foi possível conectar ao OpenRouter (sem internet?)."
        raise HTTPException(status_code=502, detail=detail)
    if resp.status_code != 200:
        raise _upstream_error(resp)
    try:
        body = resp.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="Resposta inválida do OpenRouter.")
    return {
        "reply": _extract_reply(body),
        "model": body.get("model") or model,
        "usage": body.get("usage"),
    }
