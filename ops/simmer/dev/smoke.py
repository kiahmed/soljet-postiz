"""Quick unit smoke for the Simmer wiring — no network, no Postiz."""
import sys
sys.path.insert(0, ".")
from src.lib.config_loader import known_tiers, load_tier
from src.lib.funnel import deep_link_for
from src.lib.recipes import compose_simmer, _simmer_bundle

print("known_tiers:", known_tiers())
t = load_tier("simmer")
print("tier:", t.id, t.name, "| channels:", t.channels,
      "| sources:", [s.type for s in t.sources])
print("source params:", t.sources[0].params)

card_ready = {
    "symbol": "MSTR", "state": "ready", "expiry": "2026-09-19",
    "metrics": {"iv_pct": 68.0, "vrp": 1.32, "em_1sd": 12.4},
    "sentiment": {"score": 0.41}, "headline": "$MSTR ready",
    "url": "https://simmer.facades.trade/?symbol=MSTR",
    "card_id": "SMR-MSTR-260908-260919",
    "entities": [{"name": "MSTR", "x_handle": "$MSTR"}],
}
card_watch = dict(card_ready, state="watch_entered")

print("\nREADY :", compose_simmer(t, card_ready))
print("WATCH :", compose_simmer(t, card_watch))
print("link  :", deep_link_for(t, "simmer_api", card_ready))
b = _simmer_bundle(t, card_ready)
print("bundle:", b.source_type, "| id:", b.source_id)
print("text  :", repr(b.text))
