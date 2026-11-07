# Terms + privacy pages as reading pages (7 Nov 2026, PR #244)

`templates/terms-and-conditions.html`, `templates/privacy_policy.html`, `static/css/legal.css` (new, `?v=3`).
**The legal text is untouched**; only the frame around it changed. `policy_terms.render_html` output
(`#returns-policy`, h3, ul, p) is styled, not altered; `tests/test_policy_terms.py` still passes.

* Page nav row (Back + Home › Terms and Conditions), title, one-line lead, version chips (updated dates,
  returns policy and terms versions from `policy_terms.current`).
* Tools: Jump to returns policy, **Listen** (browser `speechSynthesis`, hidden where unsupported, no third
  party), Print.
* Contents rail built from the page's `h2`s by a few lines of inline JS (ids added where missing, the
  current section highlighted with IntersectionObserver). Sticky on ≥900 px, a fold on phones.
* Sections are numbered cards (`counter()`), inner lists are plain bullets again (the global `li` card rule
  is reset inside `.ow-legal__doc`). `li#returns` keeps its id; `#returns-policy` stays on the h2.
* The footnote `<li>` ("Limited prescriptions changes…") is styled as a note, not numbered.

Deploy: `DEPLOY_SET` += `static/css/legal.css`, `templates/privacy_policy.html` (terms was already in);
`NEW_IN_RELEASE` += `static/css/legal.css`.
