# Facades · Reflex — context

Reflex (facades-news-reactor) reads market headlines as they land, has an LLM call
SPY's direction within seconds, and waits for price to confirm the call. When a
confirmed call is followed by a real move (at least 10 bps within 10 minutes),
Reflex renders one finished post per move: its signal card beside SPY's 1-minute
candles with entry and peak marked, plus a caption and hashtags.

The publisher does not compose anything for this tier. The wording, tags and image
are Reflex's; changes to them are made in news-reactor (`webapp/sentiment/posts.py`,
`config/signal.yaml → posts:`), not here.

Brand tags, always kept: `#Reflex #FacadesReflex`.
