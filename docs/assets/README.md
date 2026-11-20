# Project presentation assets

- `kh-software-factory-cover.png`: conceptual product illustration generated with the built-in `image_gen` tool. It is not a dashboard screenshot.
- `task-cycle.svg`: source-authored diagram of the existing sequential task lifecycle.
- `roles-and-controller.svg`: source-authored diagram of role ownership and controller authority.
- `guarded-task-flow.png` shows the guarded task lifecycle.
- `stopped-task-review.png` shows reviewing a stopped task and starting its next attempt.

The two README diagrams were generated with the built-in ChatGPT ImageGen tool.
Their labels are repeated in the README alternative text.

The SVG diagrams have embedded titles and descriptions, a fixed light background
and no scripts or external resources. Their labels follow the current runtime
and architecture reference. Text in the cover is repeated in the README's
accessible title and alternative text.

## Cover prompt

Use case: ads-marketing. Asset type: a wide GitHub README cover for an existing developer tool. Generate a polished final graphic for KH Software Factory. Canvas: wide panoramic landscape, approximately 3:1, generous white margins, intended to read crisply at 900 px wide. Product: a local software workflow controller that takes reviewed plans through scoped implementation, deterministic checks and independent review. Composition: an understated editorial software-factory metaphor, a single sequential line of five small precision-engineered glass and white ceramic stations, one source-code card moving through successive inspection gates, ending in a neatly sealed source bundle. A fine blue connecting rail is continuous and strictly sequential. White background, pale cool-gray dot grid, deep navy typography, restrained blue and teal accents, refined soft shadows, high-end technical publication aesthetic. Strong visual hierarchy, calm and clear. Exact headline text: "KH Software Factory". Exact secondary text: "Reviewed plans. Verified delivery." Use only these words, set clean large sans-serif type, no small labels. Do not depict a dashboard, terminal output, cloud service, parallel workers, robots, people, provider logos, or deployment. No badges, performance claims, extra text, watermarks, or outer frame. This is a conceptual product illustration, not a screenshot.

## README diagram prompts

### Guarded task lifecycle

```text
Use case: infographic-diagram. Asset type: finished diagram for the README of a real developer tool on GitHub. Editorial technical illustration, bright off-white #f7fafc canvas, deep ink navy typography and highly legible large modern sans-serif labels, restrained azure-blue outlines and teal accent, slight rounded corners. Crisp product documentation rather than a dashboard, flat 2D, light technical grid so faint it does not compete with type. It must be readable when shown at 750 px wide and has generous 6% safe margins. Use a panoramic landscape composition approximately 2.5:1, compact vertical whitespace. Use a single heading with a tidy row of equal-width nodes centered beneath it, linked by straight, continuous left-to-right arrows in only that order. No extra written words, decoration, numbers, logos, simulated UI, visual gradients, shadows, watermarks, or footers. Each exact phrase must appear once. Every node's label has strong contrast. English text only.
Title text exactly: "From ticket to acceptance". Below, five equally weighted workflow checkpoints with distinct small line icons above their labels. Exact node texts in reading order: "Accepted intent", "Reviewed plan", "Staged change", "Checks and review", "Acceptance receipt". Put the final node in slightly stronger teal. Draw exactly four unambiguous arrows from one node to the next, all pointing right. The illustration communicates a local, sequential workflow with five milestones. Do not invent additional paths or production deployment.
```

### Stopped task review

```text
Use case: infographic-diagram. Asset type: finished diagram for the README of a real developer tool on GitHub. Editorial technical illustration, bright off-white #f7fafc canvas, deep ink navy typography and highly legible large modern sans-serif labels, restrained azure-blue outlines and teal accent, slight rounded corners. Crisp product documentation rather than a dashboard, flat 2D, light technical grid so faint it does not compete with type. It must be readable when shown at 750 px wide and has generous 6% safe margins. Use a panoramic landscape composition approximately 2.5:1, compact vertical whitespace. Use a single heading with a tidy row of equal-width nodes centered beneath it, linked by straight, continuous left-to-right arrows in only that order. No extra written words, decoration, numbers, logos, simulated UI, visual gradients, shadows, watermarks, or footers. Each exact phrase must appear once. Every node's label has strong contrast. English text only.
Title text exactly: "Review a stopped task". Below, four workflow checkpoints with distinct small simple line icons for a document, a comment, a settings check and a rerun arrow. Exact node texts in reading order: "Inspect evidence", "Save a reply", "Confirm run setup", "Start a new attempt". Draw exactly three unambiguous arrows pointing right between adjacent nodes only. Put the final node in slightly stronger teal. The new attempt is a rerun, so do not display a success checkmark, approved status, or imply that it passes automatically.
```
