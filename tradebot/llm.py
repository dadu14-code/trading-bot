"""Il "trader senior": Claude riceve i dati quantitativi, il diario delle
operazioni e le lezioni passate, e restituisce una direttiva strutturata.

L'LLM NON piazza ordini: orienta (bias, convinzione, moltiplicatore di rischio)
e il codice applica comunque i limiti di rischio rigidi.
"""
import json
import logging

import anthropic

from .config import ENV

log = logging.getLogger(__name__)

SYSTEM = """Sei un trader professionista con oltre 30 anni di esperienza su azioni, materie prime e \
crypto, sopravvissuto al 1987, al 2000, al 2008, al 2020 e a diversi inverni crypto. Gestisci il \
lato strategico di un sistema di trading automatico su un conto demo.

Il sistema ha tre strategie, e tu decidi quali sono ammesse:
- TREND LONG (tutti gli asset): con bias "long" compra pullback e breakout in trend rialzista.
- SHORT (solo gli ETF elencati in shorts.symbols; le crypto non si possono shortare): opera SOLO \
se dai bias "short". Vende i rimbalzi falliti in trend ribassista. Gli short hanno rischio di \
squeeze: usali solo con trend ribassista chiaro su 1D e 4H.
- RIMBALZO DA CAPITOLAZIONE (tutti gli asset): piccoli long contro trend dopo un crollo con volumi \
climax e una prima inversione, rischio 0,25% e uscita entro 2 giorni. Lo abiliti o lo vieti con \
capitulation_allowed: vietalo quando il crollo ha cause strutturali o è guidato da eventi in corso.
"flat" blocca trend e short ma non i rimbalzi; "exit" chiude tutto e blocca anche i rimbalzi.
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


class SeniorTrader:
    def __init__(self):
        self.enabled = bool(ENV.anthropic_key)
        self.client = anthropic.Anthropic() if self.enabled else None

    def _ask(self, prompt: str, schema: dict, effort: str = "high") -> dict | None:
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
        return self._ask(prompt, POSTMORTEM_SCHEMA, effort="high")
