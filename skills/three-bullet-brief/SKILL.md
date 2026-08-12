---
schema_version: "1"
name: three-bullet-brief
version: "1.0.0"
description: >-
  Turns any topic, text, or document into exactly three crisp bullets plus
  a one-line takeaway. Use when the user asks for a brief, a TLDR, a quick
  summary, or "give me the short version".
when_to_use: >-
  Use when the user asks for a brief, a summary in bullets, a TLDR, or the
  short version of a topic, message, article, or document.
category: productivity
tags: [summary, brief, writing]
author: rubenluetke10-beep
license: MIT
---

# Three Bullet Brief

When this skill runs, produce EXACTLY this shape — no more, no less:

1. Three bullets, each a single complete sentence of at most 20 words.
2. Each bullet carries ONE distinct point: what it is, why it matters,
   what to do next.
3. A final line starting with `Takeaway:` — one sentence, the single thing
   to remember.

Rules:
- Never add headers, greetings, or closing remarks around the brief.
- If the source material is too thin for three distinct points, say which
  point is missing instead of padding.
- Keep the user's language: answer in the language they asked in.
