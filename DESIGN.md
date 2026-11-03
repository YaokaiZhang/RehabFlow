# RehabFlow UI Design Contract

## Direction

RehabFlow is an operate-first patient workspace. The interface should make the
next safe action clear, keep one recovery concern coherent, and let patients
move between focused sessions without losing episode context.

## Color

- Champagne Mist: `#F1E0C5` for the application canvas and warm navigation surfaces.
- Khaki Beige: `#C9B79C` for quiet structure, dividers, and secondary emphasis.
- Dusty Olive: `#71816D` for actions, active navigation, progress, and safe-route emphasis.
- Dark Coffee: `#342A21` for primary text, strong contrast, and clinical seriousness.

Use derived tints and shades of these four colors for states. Keep body text at
accessible contrast and reserve the olive family for actions and active state.
A muted brick (`#8B3F31`) is the only extra hue, used for risk, destructive
actions and the "stop and get help" safety list.

Tokens live in `apps/product/app/globals.css` (`--rf-*`) and are mirrored as
Tailwind names in `tailwind.config.ts` (`canvas`, `surface`, `ink`, `line`,
`action`, `danger`, radii `control`/`card`/`feature`).

## Frame and surfaces

- The global header is a solid Dark Coffee band; the workspace inside it is light.
- Three surface levels: Champagne canvas, paper cards (`#FFFAF1`, 1px line, no
  shadow), and one dark feature band.
- Radii grow with importance: controls 8px, cards 12px, the feature band 20px.
- The one bold element per screen is the episode overview's "Next step" band
  (Dark Coffee, large type, a single champagne button). Do not add a second one.

## Typography

- One family: Atkinson Hyperlegible Next (loaded with `next/font`), chosen for
  low-vision legibility. Weights 400 to 700.
- Scale: 13 / 15 / 16 / 18 / 21 / 26 / 32 to 40px. Nothing below 13px.
- Labels and eyebrows are sentence case, never all caps or letter-spaced.
- Every page uses the same header: an olive kicker naming the session, the h1,
  a one-sentence lead, then actions (`.page-header`).

## Layout

- Pages centre in one column: 1120px (`.page-column`) or 760px for forms
  (`.page-column-narrow`).
- Below 1000px the top navigation folds into a Menu button and the episode rail
  collapses into an episode switcher above the content.
- The Care Worklist is master-detail: the queue on the left, the selected
  patient on the right.

## Information Architecture

- The top navigation is global and stays visible across patient and doctor pages.
- The patient episode rail is a folder list, not an episode index or generic app navigation.
- The active episode expands to show `Episode overview`, `AI Triage Intake`, `AI Daily Rehab`, `Professional Care`, and `History`.
- Episode-level context, safety status, and progress live on the episode overview.
- Focused sessions contain one care task and do not repeat the full episode context fold.
- Patient Memory and settings remain global destinations, outside the episode folder.

## Interaction Rules

- Clicking another episode folder switches the active episode and expands its sessions.
- The episode overview presents one explicit recommended next action based on the
  safety gate and current rehab progress.
- Session pages should not show a generic next-step panel before the route is known.
- Loading, empty, error, disabled, hover, focus, and reduced-motion states are
  part of the shared component contract.
- One primary (olive) button per view. Secondary actions are outlined;
  destructive ones are quiet brick text, never as heavy as the primary.
- Show one empty state per missing thing, with what to do next. Do not repeat
  "nothing yet" in several cards.
- The episode rail is shown only to signed-in patients. Doctors visiting `/`
  go to the Care Worklist.
- Guided exercise never starts on an episode that still needs triage.

## Surface Language

Prefer direct product terms: `Care Episode`, `AI Triage Intake`, `AI Daily Rehab`,
`Professional Care`, and `History`. Never show raw enum values, field names or
internal labels (`self_serve`, `continuation`, `preferred_activity`, "MVP",
"landmarks") to patients or doctors; map them to plain words. Use calm, actionable safety language. Avoid
diagnostic claims, vague calls to action, and marketing-style hero treatment in
the authenticated workspace.
