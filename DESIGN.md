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

## Surface Language

Prefer direct product terms: `Care Episode`, `AI Triage Intake`, `AI Daily Rehab`,
`Professional Care`, and `History`. Use calm, actionable safety language. Avoid
diagnostic claims, vague calls to action, and marketing-style hero treatment in
the authenticated workspace.
