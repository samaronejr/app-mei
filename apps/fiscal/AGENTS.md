# BRAZILIAN FISCAL REFERENCE DATA

## OVERVIEW
Identifier algorithms, MEI categories, and municipality capabilities; score 9, distinct regulatory domain.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| CNPJ/CPF normalization | `validators.py` | Shared `normalize_document` |
| Check digits and validation | `validators.py` | Separate CPF/CNPJ weight ranges |
| Display masks | `formatting.py` | Brazilian document presentation |
| MEI categories | `mei.py` | Domain category choices |
| Geography/capability schema | `models.py` | UF, municipality, emitter metadata |
| Capability lookup | `capabilities.py` | Municipal service knowledge |
| Seed registry | `migrations/` | Reference data, not tenant records |
| Algorithm sources | `docs/fiscal/cnpj-alfanumerico.md` | Repository-root documentation |

## CONVENTIONS
- CNPJ accepts the 36-character uppercase ASCII alphabet in its base positions.
- Its character value is `ord(c) - 48`, not base-36 numeric decoding.
- CNPJ check digits remain numeric even when the base contains letters.
- CPF remains numeric; the CNPJ regulatory change does not extend to it.
- CPF and CNPJ use different weight cycles; keep their generators distinct.
- Mask normalization strips the explicit separator set and uppercases letters.
- Checksum validity alone does not reject degenerate repeated inputs.
- `_reject_degenerate` supplies that additional validation rule deliberately.
- Modulo-11 collisions are part of the published arithmetic, not a bug to repair.
- Municipality data is migration-seeded platform reference data.

## ANTI-PATTERNS
- Do not make CNPJ validation digits-only or give CPF the CNPJ alphabet.
- Do not treat a valid checksum as identity proof or collision-free integrity.
- Do not remove repeated-input rejection because arithmetic tests still pass.
- Do not substitute a guessed algorithm for the cited Receita Federal behavior.
- Do not merge CPF/CNPJ weighting merely because both use modulo 11.

## RELATED CHECKS
- `uv run pytest tests/fiscal tests/regression/test_cnpj_alfanumerico.py`
- Municipality regression tests live alongside the CNPJ suite in `tests/regression/`.
- Client storage/search/export consumes these helpers; inspect those consumers for changes.
