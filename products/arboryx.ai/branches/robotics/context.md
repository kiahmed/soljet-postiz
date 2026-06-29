# Robotics branch — domain context

This file extends `products/arboryx.ai/context.md`. The composer reads parent first (voice + arbor metaphor), then this file for domain-specific vocabulary and audience tuning.

## Branch identity

The Robotics branch is the first deeply-developed module under Arboryx.ai. Domain: **physical AI and embodied robotics**. It will publish under `robotics.arboryx.ai` (subdomain — anchored to the parent site, not a separate product).

The KG behind this branch lives at `../../../../catalyst-knowledge-graph/`. The canonical store is `data/robotics.duckdb`.

## What counts as a catalyst here

A robotics catalyst is a high-signal event with potential to "fruit" — i.e., move the field forward, not just generate noise. Examples:

- **Hardware milestones** — humanoid endurance/speed/manipulation benchmarks (e.g., HR-9 4.2 km/h sustained walking).
- **Capital events** — Series funding for physical-AI labs, M&A by big-tech (NVDA, GOOG, TSLA) into the stack.
- **Software stack shifts** — Isaac Sim adoption, foundation-model releases for robotics, sim-to-real breakthroughs.
- **Deployments** — first-of-kind real-world deployments (warehouse, surgical, mobility).
- **Policy / regulatory** — FAA, NHTSA, FDA decisions affecting deployment paths.
- **Supply chain** — actuator, sensor, compute hardware availability shifts.

Skip:
- Generic "robotics market to grow X%" reports.
- Vendor announcements that are pure marketing.
- Concept videos without a real product or paper behind them.

## Domain vocabulary

| Term | Use it for |
|---|---|
| **Humanoid** | Bipedal, anthropomorphic forms — Figure, 1X, Honor, Tesla Optimus, Maniformer-style. |
| **Embodied AI** | Models running on physical hardware. Distinct from pure-LLM "agents". |
| **Sim-to-real** | The gap between simulator-trained policies and real-world performance. |
| **Foundation model for robotics** | RT-X, OpenVLA, Pi-zero, similar — generalist policies. |
| **Manipulation** | Grasping, dexterous motion, tool use. |
| **Locomotion** | Walking, running, balance — distinct from manipulation. |
| **Field deployment** | Real customer/site, not lab demo. |
| **Stack** | Compute (NVDA), simulation (Isaac, MuJoCo), foundation model, control, hardware. |

## Audience

Robotics-branch posts target a **more technical audience** than parent posts:
- Founders building in the stack (hardware, sim, models, deployment).
- Researchers tracking sim-to-real, manipulation, generalist policies.
- Operators evaluating real-world deployment readiness.
- Investors with a robotics-specific thesis (not generalist VCs).

Tone: more granular than parent. It's OK to name a specific paper, ticker, or mechanism. Still observational, not salesy. Still arbor-metaphor when the moment is right ("HR-9 is the kind of catalyst that buds for two seasons before it fruits") but don't force it.

## Composition rules

When drafting from a Robotics catalyst:
1. **Anchor to one entity** — name the company/lab/paper. No vague "the robotics space".
2. **Pull two relationships from the KG** — supplier, competitor, funder, paper-citation, etc. This is what differentiates Arboryx-flavored posts from a tickertape.
3. **One direct takeaway, one indirect** — what this means for the entity, what it implies for an adjacent entity. Match the `direct` / `indirect` fields produced by the market-research pipeline.
4. **End with a forward read** — what to watch next, not a CTA.
5. **No threads on X** for catalyst posts — single tweet, link to the source. Threads only for sector-spanning digests.

## Catalyst-to-post pipeline (Robotics-specific)

```
catalyst_id ──► duckdb_source.get_catalyst(id)
              └─► entity + linked entities + relationships
              └─► linked findings from parent Firestore (filter category=Robotics)
              └─► compose(parent_context + branch_context + payload)
              └─► postiz_client.create_draft(text, customer=Robotics, channels=[X primary, LinkedIn if enabled])
```

Implementations: `bin/draft.py --tier arboryx.robotics --source-id <catalyst_id>`.
