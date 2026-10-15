# Axorim R1 manual accessibility checklist (NVDA + Firefox, VoiceOver + iOS Safari)

**Operator gate: OPEN.** No operator run is recorded below. This gate closes only
when an operator completes both scripts on real assistive technology and fills the
results table; the run is then copied to `.evidence/AXR1-025-operator.txt`.

## What this checklist is, and what it is not

The browser suite (`uv run pytest -m browser tests/browser`) runs axe-core on the R1
screens and journeys at 1440x900 and 390x844 and fails on any serious or critical
violation (`tests/browser/test_a11y_axe.py`, the journeys `test_journey_*.py`,
`test_upload_retry_slow_network.py`, `test_focus_after_swap.py`). Automated checks
find a subset of problems. **Passing them is not a WCAG 2.2 AA conformance claim,
and nothing in this product makes one.** The target is WCAG 2.2 AA (plan S5.6); this
checklist is how a person checks the parts a rule engine cannot: what a screen
reader actually announces, whether the order makes sense, and whether a task can be
finished without sight or a pointer.

Run it against the synthetic-data tenant only. Never use real client data.

## Environments

| Script | Assistive technology | Browser | Device |
| --- | --- | --- | --- |
| Staff journey | NVDA (current stable) | Firefox (current or ESR) | Windows 10/11 desktop, 1440 px or wider |
| Client journey | VoiceOver | Safari | iPhone on current iOS, portrait |
| Client journey (repeat) | NVDA | Firefox | Windows, window narrowed to 390 px |

Before starting: record the exact versions of the AT, the browser and the OS in the
results table. Turn off any browser extension that alters pages.

## How to mark a step

- **Pass**: the expected announcement or behaviour happens without workarounds.
- **Fail**: anything else; note what was announced or what blocked you.
- **Blocked**: the step could not be reached (an earlier failure); note which one.

Every failure becomes a tracked issue linked in the Notes column.

## Script 1: staff journey (NVDA + Firefox, keyboard only)

Sign in as a staff member of the synthetic tenant. Do not touch the mouse.

| # | Action | Expected | WCAG 2.2 focus |
| --- | --- | --- | --- |
| S1 | Load Hoje (`/`). Press `Tab` once. | "Pular para o conteúdo" skip link is announced and visible. `Enter` moves focus to the main content. | 2.4.1, 2.4.7 |
| S2 | Navigate by headings (`H`). | Page heading, then "O que precisa de atenção" with the item count. Each item reads as a heading with client, period and title. | 1.3.1, 2.4.6 |
| S3 | Open "Filtrar fila", change the period, press "Aplicar filtros". | While loading, a status is announced. After the update focus is on the queue heading, which NVDA reads. | 4.1.3, 2.4.3 |
| S4 | From a cycle item, follow its action link. | The cycle page title and heading (client and period) are announced. | 2.4.2, 2.4.4 |
| S5 | Tab through the cycle page header, the requests and the pendências. | Order follows the visual order. Every control has a name; status words ("Atrasada", "Recebido do cliente · aguardando conferência") are read as text, not only colour. | 2.4.3, 1.4.1, 4.1.2 |
| S6 | Open a pendência drawer, then close it with `Esc`. | The dialog is announced with its title; focus starts inside it and returns to the link that opened it. | 2.4.3, 4.1.2 |
| S7 | Open a document review, use `R` to reach the reason, `Tab` to "Não aceitar". | Shortcut moves focus to the labelled reason field; the button is announced with its name. Do not submit. | 2.1.1, 2.1.4 |
| S8 | Open an approval card (`/acoes/<id>/`). Read it top to bottom with the arrow keys. | Every effect, recipient, document, amount, source and policy fact is read **before** "Decidir sobre estes efeitos". Empty collections are one line ("Não integram esta versão: ..."). "Identidade técnica desta versão" is a collapsed disclosure announced as such. | 1.3.2, 3.3.2 |
| S9 | Expand "Identidade técnica desta versão" with `Enter`, then collapse it. | Expanded/collapsed state is announced. | 4.1.2 |
| S10 | In a second tab, approve the same card; in the first, press "Aprovar estes efeitos". | An alert is announced with the conflict message and the "Recarregar esta versão" link; no second decision is recorded. | 4.1.3, 3.3.1 |
| S11 | Open Comunicação. Read one message. | State ("Na fila", "Falhou temporariamente", "Cancelado"...) and its code are read as text. The page never says a message was delivered. | 1.4.1, 1.3.1 |
| S12 | Zoom the browser to 200% and then 400% on the approval card. | No horizontal scrolling at 400% (320 CSS px); the decision controls are not hidden behind other content. | 1.4.10, 2.4.11 |
| S13 | Check every interactive control on the approval card and the cycle page. | Each target is at least 24x24 CSS px or has the required spacing. | 2.5.8 |
| S14 | Look for the help and sign-out links on every visited page. | Same place and same order on every page. | 3.2.6 |

## Script 2: client journey (VoiceOver + iOS Safari)

Sign in to the portal as the synthetic client. Use VoiceOver gestures only.

| # | Action | Expected | WCAG 2.2 focus |
| --- | --- | --- | --- |
| C1 | Sign in with password and the second factor. | Every field is labelled; the code field accepts paste or autofill and no cognitive test is required. | 3.3.8, 1.3.5 |
| C2 | On Início, swipe through the page. | Skip link, company name as the heading, then "Solicitações do escritório" with each request title and its status word (for example "Pendente"). | 1.3.1, 1.4.1 |
| C3 | If the account holds two companies, use the "Empresa" picker and "Trocar empresa". | The picker is announced with its label and current value; after switching, the heading reads the new company. | 4.1.2, 3.2.2 |
| C4 | Open a request and choose a file. | The file field is labelled; the accepted types and the size limit are read as its description. | 3.3.2, 1.3.1 |
| C5 | Send the file. While it uploads, listen. | "Enviando…" and the progress are announced; "Cancelar envio" is reachable. | 4.1.3 |
| C6 | Turn on Airplane Mode during a large upload. | A failure message is announced; "Tentar de novo" is reachable; the chosen file is kept. | 4.1.3, 3.3.1, 3.3.7 |
| C7 | Turn Airplane Mode off and choose "Tentar de novo". | "Arquivo enviado." is announced; after the update focus is on "Situação" and the status reads "Recebido". | 4.1.3, 2.4.3 |
| C8 | Open a publication and press "Confirmo o recebimento". | The publication text is readable as text; the confirmation is announced. | 4.1.3, 1.3.1 |
| C9 | Rotate to landscape and back. | Content reflows and nothing is lost. | 1.3.4, 1.4.10 |
| C10 | With an ended engagement (operator prepares it), open the portal. | A message says that access ended, in Portuguese, and no company data is shown. | 3.3.1, 1.3.1 |

## Results (operator run)

Leave a row per step. Copy the finished table, with the versions, to
`.evidence/AXR1-025-operator.txt`.

| Date | Operator | AT + version | Browser + version | OS / device | Script | Step | Result | Notes / issue |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
