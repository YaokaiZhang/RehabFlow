# RehabFlow UI Design Contract

## Direction

A bright, hand-made recovery notebook. White paper, black ink, pastel sticky
notes and handwritten headlines. The interface should feel light and friendly
while making the next safe action obvious. The layout does the explaining:
colour says what kind of session something is, size says what matters, and
copy is cut to labels, data and actions.

## Colour

Paper `#FFFFFF`, ink `#111111`, soft fill `#F5F5F5` (fields, secondary
buttons, quiet surfaces), muted text `#5F5F5F`, soft text `#8A8A8A`.

Each kind of care session owns one pastel. Use it wherever that session
appears (rail dot, tag, sticker, page kicker) and nowhere else:

| Session            | Pastel   | Label    | Deep (text) | Stroke  |
|--------------------|----------|----------|-------------|---------|
| AI Triage Intake   | `#FFE8D6` peach | `#FED7AA` | `#9A3412` | `#F97316` |
| AI Daily Rehab     | `#D7F1E1` mint  | `#BBF7D0` | `#166534` | `#10B981` |
| Professional Care  | `#E0F2FE` sky   | `#BAE6FD` | `#075985` | `#0EA5E9` |
| History and memory | `#F3E8FF` lilac | `#E9D5FF` | `#6B21A8` | `#A855F7` |
| Risk / stop        | `#FFE4E6` rose  | `#FECDD3` | `#9F1239` | `#E11D48` |

One exception: the example-prompt stickers on the triage hero mix all four
pastels purely as decoration, because nothing there belongs to a session yet.

Tokens live in `apps/product/app/globals.css` (`--rf-*`). Apply a pastel with
`.tone-peach|mint|sky|lilac|rose` (sets background and tone variables) or
`data-tone="..."` (tone variables only). Tailwind exposes `bg-mint`,
`text-mint-deep`, `bg-peach-label`, `bg-fill`, `shadow-soft`, `rounded-card`,
`font-hand` and so on. Legacy Tailwind names (`slate`, `emerald`, `amber`,
`rose`, `teal`) are remapped to the new palette.

## Typography

- Display: Gaegu 700 (`.display`, `.font-hand`, `.page-header h1`), handwritten.
  Used for page titles, the logo, empty states and margin notes (`.scribble`).
  Never for body text, buttons or form labels.
- Body: Atkinson Hyperlegible Next 400 to 700, kept for low-vision legibility.
- Sentence case everywhere. No all-caps or letter-spaced labels.

## Surfaces and shape

- Cards: white, radius 28px, soft shadow `0 20px 50px rgba(0,0,0,.05)`, no
  heavy borders (`.card`).
- Stickers: pastel cards (`.sticker.tone-*`) that may sit at a slight angle
  (`.tilt-l`, `.tilt-r`, max about 3.5deg) and straighten when hovered. Use them
  for things a patient picks or that summarise a session, not for forms.
- Controls are pills: black primary (`.btn-primary`, optional `.btn-arrow`
  circle inside), soft grey secondary (`.btn-secondary`). One primary per view.
- Fields are soft grey with radius 18px and an ink border on focus.

## Layout

- Floating white top bar: handwritten logo left, short grey links centred, the
  account pill right.
- Signed-in patients get the episode rail on the left: "my episodes", the open
  episode expands to its sessions, each marked by its session colour.
- Pages centre in 1120px (`.page-column`) or 760px for forms (`.page-column-narrow`).
- Below 1000px the links fold into a Menu pill and the rail into a switcher.

## Copy

- Let the layout talk. Prefer a title, the data and an action over a sentence
  explaining them. No leads that restate the title, no helper text that
  restates a label, no "Not sure how to start?" prompts.
- Keep safety copy: the stop-and-get-help list, urgent route messages and
  error messages that say what to do.
- Product terms: `Care Episode`, `AI Triage Intake`, `AI Daily Rehab`,
  `Professional Care`, `History`. Never show raw enum values or field names.

## Motion

One orchestrated moment per screen at most (for example the triage stickers
drifting with the pointer). Hover lifts on stickers and primary buttons.
Everything respects `prefers-reduced-motion`.

## Interaction rules (unchanged)

- Clicking another episode switches the active episode and expands its sessions.
- The episode overview shows one recommended next action from the safety gate
  and rehab progress.
- Loading, empty, error, disabled, hover, focus and reduced-motion states are
  part of every component.
- Guided exercise never starts on an episode that still needs triage.
- Doctors visiting `/` go to the Care Worklist.
