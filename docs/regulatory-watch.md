# Regulatory watch

The obligation engine reads every limit and deadline from `obligations_fiscalparameter`
rather than from code, so responding to a change in the law is an `INSERT`, not a
deploy. That property is only worth having if somebody notices the change. This
document is the list of things to notice, and the ritual for noticing them.

## The review ritual

**On the first working day of every month**, and again within two working days of any
reported vote on the items below:

1. Re-check each open item in the table.
2. If an item has been **enacted**, insert a new `FiscalParameter` row with the new
   value and a `valid_from` equal to the date the new limit takes effect. **Do not
   edit or close the existing row** — effective dating here is latest-wins, and the
   old row remains the correct answer for dates before the changeover. Overlapping
   open-ended ranges are expected and permitted.
3. Add the citation to `source_note` and the official URL to `source_url` in the same
   insert. A number nobody can trace back to a statute cannot be re-verified when it
   changes again.
4. Record the check below, even when nothing moved. "We looked and nothing changed" is
   the entry that distinguishes a current answer from a forgotten one.

Nothing here is seeded until it is **law**. A proposed limit written into the table
would tell every firm on the platform that a client is compliant while Receita Federal
desenquadra them retroactively — the one failure mode this product exists to prevent.

## Open items

| Item | Proposal | Status | Effect if enacted |
| --- | --- | --- | --- |
| **PLP 186/2026** (Poder Executivo, sent 29 June 2026) | MEI annual ceiling **R$ 81.000 → R$ 110.000 for calendar year 2027, then R$ 140.000 from 2028**; opening-year proportional rate correspondingly R$ 9.166,67/month in 2027 and R$ 11.666,67/month from 2028; MEI employee limit **1 → 2** | Pending in the Câmara dos Deputados. Expressly conditioned on the corresponding renúncia de receita being carried in the LOA for 2027–2029, so enactment alone is not sufficient — check the budget condition too. | New `mei.annual_ceiling` and `mei.monthly_proportional` rows for **both** categories, `valid_from = 2027-01-01` and again `2028-01-01`; new `mei.max_employees` row |
| **PLP 108/2021** (Senado Federal, Sen. Jayme Campos) | MEI annual ceiling **R$ 81.000 → R$ 130.000**; employee limit 1 → 2 | Pending. Urgência approved 17 March 2026, Comissão Especial constituted 28 April 2026, floor vote postponed to the second half of 2026 while the economic team completes impact studies. The rapporteur has signalled he may absorb the Executive's staged figures instead, and has floated R$ 134.000 with **MEI-Caminhoneiro R$ 251.000 → R$ 321.000** and an automatic inflation-indexing mechanism. | Same rows as above. Note this bill and PLP 186/2026 propose **different** numbers; whichever is enacted is the one that gets seeded |
| Simples Nacional band revision (rides along with the above) | ME R$ 360.000 → R$ 800.000; EPP R$ 4,8 mi → R$ 8 mi | Pending, same vehicle | Out of scope for MEI parameters, but it changes what a desenquadrado client migrates into — relevant to the threshold monitor's advice text, not to its arithmetic |

## Settled, and deliberately not re-researched

These were verified against primary sources and are encoded as given. Re-opening them
without a published change to the underlying norm is wasted work and a chance to
introduce an error.

| Rule | Value | Basis |
| --- | --- | --- |
| MEI annual ceiling, common | R$ 81.000,00, unchanged since January 2018 | LC 123/2006 art. 18-A §1º (LC 155/2016) |
| MEI annual ceiling, caminhoneiro | R$ 251.600,00 | LC 188/2021 |
| Opening-year proportional rule | monthly rate × months from start of activity to year end, **a fraction of a month counting as a whole month** | LC 123/2006 art. 18-A §2º |
| DAS-MEI due date | day 20 of the following month, **postponed forward** to the next business day | Resolução CGSN nº 140/2018 art. 40 §3 |
| DASN-SIMEI due date | **31 May, with no rolling at all**, even when it falls on a weekend | Resolução CGSN nº 140/2018 art. 109 |
| Excess tolerance | 20%; at or below, desenquadramento effective 1 January of the following year. Above, retroactive to 1 January of the current year — or to the opening date, in the year of opening | Resolução CGSN nº 140/2018 art. 115 |
| MEI employee limit | 1 | LC 123/2006 art. 18-C |

## Review log

| Date checked | By | Outcome |
| --- | --- | --- |
| 2026-07-27 | initial seed (T-037) | R$ 81.000 confirmed in force for 2026. PLP 186/2026 and PLP 108/2021 both pending, neither enacted. No 2027 or 2028 row seeded. |
