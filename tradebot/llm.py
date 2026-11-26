"""Il "trader senior": Claude riceve i dati quantitativi, il diario delle
operazioni e le lezioni passate, e restituisce una direttiva strutturata.

L'LLM NON piazza ordini: orienta (bias, convinzione, moltiplicatore di rischio)
e il codice applica comunque i limiti di rischio rigidi.
"""
import json
import logging

import anthropic

from . import db
from .config import ENV, load_settings

log = logging.getLogger(__name__)

SYSTEM = """Sei un trader professionista con oltre 30 anni di esperienza su azioni, materie prime e \
crypto, sopravvissuto al 1987, al 2000, al 2008, al 2020 e a diversi inverni crypto. Gestisci il \
lato strategico di un sistema di trading automatico su un conto di prova piccolo (circa 100 €), \
senza leva: ogni euro di commissione e di rischio conta.

Il sistema ha tre strategie, e tu decidi quali sono ammesse:
- TREND LONG (tutti gli asset): con bias "long" compra pullback e breakout in trend rialzista.
- SHORT (solo sugli asset elencati in shorts.symbols, vedi i dati): opera SOLO se dai bias \
"short". Vende i rimbalzi falliti in trend ribassista. In un mercato chiaramente ribassista lo short \
è lo strumento principale, non un'eccezione; attenzione però agli squeeze dopo movimenti già estesi.
- RIMBALZO DA CAPITOLAZIONE (tutti gli asset): piccoli long contro trend dopo un crollo con volumi \
climax e una prima inversione, rischio 0,25% e uscita entro 2 giorni. Lo abiliti o lo vieti con \
capitulation_allowed: vietalo quando il crollo ha cause strutturali o è guidato da eventi in corso.
"flat" blocca trend e short ma non i rimbalzi; "short" ed "exit" bloccano anche i rimbalzi su quell'asset.
Nei dati trovi i livelli di prezzo reali (price_levels): usali per i livelli chiave.

Come ragioni:
- Pensi per orizzonti temporali: il trend primario (giornaliero, mesi) decide la direzione, quello \
intermedio (4 ore, settimane) decide se è il momento, il breve (1 ora) serve solo al timing.
- Usi la storia: le "analogie storiche" nei dati dicono cosa è successo dopo situazioni simili; \
le lezioni del diario dicono dove il sistema ha sbagliato in passato. Le prendi sul serio ma ne \
riconosci i limiti (pochi campioni, cambi di regime).
- Prima di chiederti quanto puoi guadagnare ti chiedi quanto puoi perdere. Stare flat è una posizione.
- Riduci il rischio quando la volatilità esplode, quando gli asset diventano tutti correlati o \
quando il sistema sta perdendo; lo aumenti solo con trend chiari e confermati su più orizzonti.
- Sei onesto sull'incertezza: se il quadro è confuso lo dici e abbassi la convinzione.

Rispondi in italiano, in modo conciso e concreto, solo nel formato JSON richiesto."""

DIRECTIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "market_view": {"type": "string", "description": "Visione d'insieme in 3-5 frasi"},
        "risk_posture": {"type": "string", "enum": ["aggressive", "normal", "defensive", "capital_preservation"]},
        "global_risk_multiplier": {"type": "number", "description": "0.0-1.5, moltiplica il rischio di tutte le operazioni"},
        "capitulation_allowed": {"type": "boolean", "description": "true se il modulo rimbalzo può operare"},
        "symbols": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "bias": {"type": "string", "enum": ["long", "short", "flat", "exit"]},
                "conviction": {"type": "number", "description": "0.0-1.0"},
                "risk_multiplier": {"type": "number", "description": "0.0-1.5"},
                "horizon": {"type": "string", "enum": ["short", "swing", "position"]},
                "key_levels": {"type": "string"},
                "reasoning": {"type": "string"},
            },
            "required": ["symbol", "bias", "conviction", "risk_multiplier", "horizon", "key_levels", "reasoning"],
            "additionalProperties": False}},
        "plan_today": {"type": "array", "items": {"type": "string"}, "description": "Cosa farà il sistema nelle prossime ore"},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["market_view", "risk_posture", "global_risk_multiplier", "capitulation_allowed", "symbols", "plan_today", "warnings"],
    "additionalProperties": False,
}

POSTMORTEM_SCHEMA = {
    "type": "object",
    "properties": {
        "trade_reviews": {"type": "array", "items": {
            "type": "object",
            "properties": {"trade_id": {"type": "integer"},
                           "verdict": {"type": "string", "enum": ["good_trade", "good_loss", "bad_trade", "lucky_win"]},
                           "comment": {"type": "string"}},
            "required": ["trade_id", "verdict", "comment"], "additionalProperties": False}},
        "lessons": {"type": "array", "items": {"type": "string"},
                    "description": "0-3 lezioni nuove, generali e riutilizzabili"},
        "parameter_suggestions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["trade_reviews", "lessons", "parameter_suggestions"],
    "additionalProperties": False,
}


DEFAULT_PRICES = {"claude-opus-5-5": [4.0, 20.0], "claude-sonnet-5-5": [2.0, 10.0], "claude-haiku-5-5": [0.10, 0.50]}


def call_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Costo in $ di una chiamata (prezzi per milione di token, configurabili in settings.yaml)."""
    prices = {**DEFAULT_PRICES, **(load_settings().get("llm_prices") or {})}
    pin, pout = prices.get(model) or next((v for k, v in prices.items() if model.startswith(k)), prices["claude-opus-5-5"])
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


class SeniorTrader:
    def __init__(self):
        self.enabled = bool(ENV.anthropic_key)
        self.client = anthropic.Anthropic() if self.enabled else None

    def _ask(self, prompt: str, schema: dict, effort: str = "high", purpose: str = "strategia") -> dict | None:
        if not self.enabled:
            return None
        try:
            resp = self.client.beta.messages.create(
                model=ENV.claude_model,
                max_tokens=16000,
                system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",   # se il modello rifiuta, l'API riprova su un modello di riserva
            )
        except anthropic.RateLimitError:
            log.warning("Claude: rate limit, salto questo giro")
            return None
        except anthropic.APIStatusError as e:
            log.error("Claude: errore API %s %s", e.status_code, e.message)
            return None
        except anthropic.APIConnectionError:
            log.error("Claude: errore di connessione")
            return None
        try:  # registra token e costo reali, letti dalla risposta dell'API
            u = resp.usage
            tin = (u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0)
            cost = call_cost(resp.model, tin, u.output_tokens or 0)
            db.record_llm_usage(purpose, resp.model, tin, u.output_tokens or 0, cost)
            log.info("Claude %s: %d token in, %d out, %.4f $", purpose, tin, u.output_tokens or 0, cost)
        except Exception as e:
            log.warning("registrazione costi fallita: %s", e)
        if resp.stop_reason in ("refusal", "max_tokens"):
            log.warning("Claude: risposta non utilizzabile (%s)", resp.stop_reason)
            return None
        text = next((b.text for b in resp.content if b.type == "text"), None)
        return json.loads(text) if text else None

    def strategy_review(self, context: dict) -> dict | None:
        prompt = ("Ecco lo stato attuale dei mercati e del sistema. Decidi la direttiva per le prossime ore.\n\n"
                  f"```json\n{json.dumps(context, ensure_ascii=False, default=str)}\n```")
        d = self._ask(prompt, DIRECTIVE_SCHEMA)
        if d:   # clamp di sicurezza: i limiti li decide il codice
            d["global_risk_multiplier"] = max(0.0, min(1.5, float(d["global_risk_multiplier"])))
            for s in d["symbols"]:
                s["conviction"] = max(0.0, min(1.0, float(s["conviction"])))
                s["risk_multiplier"] = max(0.0, min(1.5, float(s["risk_multiplier"])))
        return d

    def postmortem(self, trades: list[dict], lessons: list[str], market_context: dict) -> dict | None:
        prompt = ("Fai il postmortem delle operazioni chiuse. Distingui la qualità della decisione dall'esito "
                  "(una perdita può essere un buon trade). Estrai solo lezioni davvero nuove rispetto a quelle "
                  "già note.\n\n"
                  f"Operazioni:\n```json\n{json.dumps(trades, ensure_ascii=False, default=str)}\n```\n"
                  f"Lezioni già note:\n```json\n{json.dumps(lessons, ensure_ascii=False)}\n```\n"
                  f"Contesto di mercato:\n```json\n{json.dumps(market_context, ensure_ascii=False, default=str)}\n```")
        return self._ask(prompt, POSTMORTEM_SCHEMA, effort="high", purpose="postmortem")
