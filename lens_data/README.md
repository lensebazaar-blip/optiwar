# Lens import sheets

One directory per product, holding exactly what `scripts/import_contact_lenses.py`
was given: `products.csv` (the commercial record) and `rules.csv` or
`variants.csv` (what may be ordered). They are kept so a production row can be
traced to the sheet that produced it and re-imported identically.

`param_source` names who asserted the orderable values. `owner-<date>` means the
catalogue owner stated them in writing on that date; a manufacturer chart would
be recorded as such and carried as `variants.csv` instead.

Prices are EUR, the `.com` selling currency. INR is derived at import time from
`--eur-inr-rate` and recorded on the profile; it is never typed here.
