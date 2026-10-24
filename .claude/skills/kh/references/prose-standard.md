# Prose standard

A compact writing standard for documentation and agent-facing prose. Concepts
drawn from public documentation practice (Diátaxis structure, Google developer
style, plain-language rules) and from pstack's unslop/technical-writing skills
(cursor/plugins, MIT) — own wording, no copied text.

## Sentences

- Short declarative sentences. One thought per sentence, ended with a period.
- No decorative em dashes. A filename and its description are a sentence
  ("main.js owns persistence"), not a dash-joined pair.
- No filler transitions ("moreover", "furthermore", "it is worth noting").
- No hype or vague praise ("seamless", "robust", "powerful"). Say what the
  thing does; the reader decides what it is worth.

## Content

- State facts traceable to the artifact: what changed, where it lives, how to
  use or verify it. No speculation about intent, no roadmaps, no future work.
- Name a file only if it is in the change list or the diff.
- Terms are defined before use; code identifiers stay in code formatting.

## Structure

- Pick the document's job first: tutorial, how-to, reference or explanation.
  One document does one job.
- Headings name the content, not the document's outline.
- Lists for enumerations, tables for comparisons, prose for reasoning.
