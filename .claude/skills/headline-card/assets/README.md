# Channel logos

- `dn_logo.png` — DailyNews channel logo (shipped). Replace to rebrand.
- **EpicFury: no logo** — `channels.yaml` sets `ef: logo_path: ""` on purpose.

A transparent-background PNG looks best. Logos are downscaled to fit a 160×160
box (`logo_size` in card.py) and placed per `logo_position` in
`../channels.yaml`. If a configured logo path is missing, the card still
renders — just without a logo (a `note:` prints to stderr).
