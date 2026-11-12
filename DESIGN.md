# Haier Evo control window

## Overview
Operate surface. Preserve the existing click-to-open laundry window and Home Assistant theme. One device name and state in Control; configuration entities in Settings. No dashboard is added.

## Colors
Use Home Assistant CSS tokens: --primary-text-color, --secondary-text-color, --primary-color, --error-color, --divider-color, --secondary-background-color, --card-background-color (fallback --primary-background-color). No independent theme or font download.

## Typography
The host uses the Home Assistant body font and optional monospace for code. Local shorthand CSS roles: --haier-type-caption: 400 12px/1.4; --haier-type-body: 400 14px/1.45; --haier-type-group: 600 17px/1.3; --haier-type-title: 600 20px/1.3; --haier-type-status: 600 24px/1.25. Each role includes the inherited font family. Buttons and inputs inherit body; labels and help use caption.

## Layout
Dialog width min(520px, viewport minus 24px); viewport height constrained with scrolling. Card padding 24px (17px at <=380px); content gap 12-24px; control height >=44px; input padding 8-12px. Programme groups retain cloud order, search spans titles, group descriptions and programme hints. Settings collapse to one column at <=380px.

## Elevation & Depth
Use native theme surfaces and divider borders; keep modal backdrop distinct from content. No decorative shadows.

## Shapes
Controls use 9px radius, dialog 18px, appliance glyph a circle. The machine drawing is geometric SVG; all programme and action icons use mdi through ha-icon.

## Components
Start and cancel require confirmation. Pause/resume share one control. Availability derives from backend capabilities, remote control and state, never appearance. Programme selection and parameter staging send no appliance command. Preserve search focus, field drafts and scroll during live updates. Errors and status have accessible live regions; modal traps focus and restores it on close.

## Do's and Don'ts
Keep the existing restrained HA style. Animate only the running drum (7s tumble, 3s drift); pause/offline stop motion. Respect prefers-reduced-motion. A loaded catalogue with no selection shows «Не выбрана»; «Нет доступных программ» is reserved for an empty catalogue. Keep unmapped numeric phase labels out of the primary modal status; native diagnostic values remain available. Never guess a missing time or programme parameter. Never label an unknown idle transition as successful completion.
