# CNPJ alfanumérico — the check-digit algorithm, from the primary source

**Status**: verified against Receita Federal primary sources on 2026-07-28. T-031.

Alphanumeric CNPJs enter production for new registrations on **2026-07-31**
(IN RFB nº 2.229/2024, in force since 2024-10-25). This is a day-one correctness
requirement, not future-proofing, so the algorithm is recorded here from the
published specification rather than inferred from a blog or a library.

## Primary sources

| Document | URL | SHA-256 of the retrieved file |
| --- | --- | --- |
| Perguntas e Respostas — CNPJ alfanumérico (PDF) | <https://www.gov.br/receitafederal/pt-br/centrais-de-conteudo/publicacoes/perguntas-e-respostas/cnpj/cnpj-alfanumerico.pdf> | `c59ab587536ca22f634373cd6a0603afc834f286d3f557e60ad488f4e6264835` |
| Cálculo dos dígitos verificadores de CNPJ alfanumérico (RFB/SERPRO technical manual, PDF) | <https://www.gov.br/receitafederal/pt-br/centrais-de-conteudo/publicacoes/documentos-tecnicos/cnpj/manual-dv-cnpj.pdf> | `7bb839f6c9beb968bd5bb67d31dd5db090d2333be55815759cfb293e639b4754` |
| Official reference implementations (Java, Python, TypeScript, JavaScript) | <https://www.gov.br/receitafederal/pt-br/centrais-de-conteudo/publicacoes/documentos-tecnicos/cnpj/codigos-cnpj.zip> | `c86335cb9251b65aee2b3582df1b77bfc7728b1c5d0f22999a38b964ca6d7136` |
| Technical-document index | <https://www.gov.br/receitafederal/pt-br/centrais-de-conteudo/publicacoes/documentos-tecnicos/cnpj> | — |

Receita Federal publishes not only the specification but **working reference code**.
Our implementation is differential-tested against it, which is a stronger check than
any prose reading of the spec.

## The algorithm

### Layout

> "O CNPJ alfanumérico é composto por doze caracteres alfanuméricos e dois dígitos
> verificadores **numéricos**." — technical manual, §1

Fourteen positions: **12 alphanumeric + 2 numeric check digits**, masked
`AA.AAA.AAA/AAAA-DD`. The permitted alphabet for the first twelve is `0-9` and
uppercase `A-Z`.

**The check digits are never letters.** This is not an inference. The remainder rule
below can only produce `0..9`, and RFB's own reference implementation encodes it in
its format regex, where the body accepts `[A-Z]|\d` but the check-digit group is
`\d{2}`:

```python
# codigos-cnpj.zip :: src/python/cnpj.py
re.match(r'(^([A-Z]|\d){2}\.([A-Z]|\d){3}\.([A-Z]|\d){3}\/([A-Z]|\d){4}(\-\d{2})?$)', _cnpj)
```

### Character value

> "Tomemos a letra 'A' cujo decimal correspondente, no código ASCII, é 65.
> Subtraindo 48 temos o valor 17 para o cálculo do módulo 11." — Q&A PDF

`value(c) = ord(c) - 48`. So `'0'`→0 … `'9'`→9, `'A'`→17 … `'Z'`→42. Note the gap:
the seven ASCII characters between `'9'` (57) and `'A'` (65) are not in the alphabet,
so values 10–16 are unused.

### Weights

> "Distribuir os pesos de 2 a 9 da direita para a esquerda (recomeçando depois do
> oitavo caracter)" — technical manual, §1.1

Weights cycle `2,3,4,5,6,7,8,9` applied **right-to-left**, restarting after the
eighth. Written left-to-right over the operand this is:

- 12-character operand (first check digit): `5 4 3 2 9 8 7 6 5 4 3 2`
- 13-character operand (second check digit, base + DV1): `6 5 4 3 2 9 8 7 6 5 4 3 2`

This is the same weight scheme the legacy numeric CNPJ has always used.

### Remainder rule

```
r = (sum of value × weight) mod 11
digit = 0        if r < 2
digit = 11 - r   otherwise
```

The second check digit is computed over the twelve base characters **with the first
check digit appended**.

### The single official worked example

RFB publishes exactly **one** worked example, `12.ABC.345/01DE-35`. It appears in
both the Q&A PDF and the technical manual, but it is the same example — there is no
second, independent official vector. Any acceptance criterion demanding three
official vectors is unsatisfiable.

```
CNPJ    1  2   A   B    C   3   4   5   0  1   D   E
Valor   1  2  17  18   19   3   4   5   0  1  20  21     (ASCII − 48)
Peso    5  4   3   2    9   8   7   6   5  4   3   2
Produto 5  8  51  36  171  24  28  30   0  4  60  42
Soma = 459 ; 459 mod 11 = 8 ; primeiro dv = 11 − 8 = 3

Repeating with the first check digit appended:
Soma = 424 ; 424 mod 11 = 6 ; segundo dv = 11 − 6 = 5

Resultado final: 12.ABC.345/01DE-35
```

### Backward compatibility

> "Para quem já tem o número do CNPJ nada muda. Os atuais números permanecerão
> válidos assim como os seus dígitos verificadores." — Q&A PDF, question 15

Legacy all-numeric CNPJs validate **identically** under this algorithm — it is a
strict generalization, not a replacement. Both formats are valid simultaneously and
one code path serves both. There is no migration of existing numbers.

## Two consequences that bite the implementation

### 1. Módulo 11 over a 36-character alphabet has collisions

Values span 0–42, which is wider than 11, so different characters share a residue
mod 11. Substituting one character for another **in the same residue class** changes
the weighted sum by a multiple of 11 and therefore leaves **both check digits
unchanged**.

`12.LBC.345/01DE-35` is a **valid** CNPJ, derived from the official example by
`A`→`L` (`value('A')=17`, `value('L')=28`, both ≡ 6 mod 11). RFB's own reference
implementation agrees. So does `A`→`W` and `A`→`6`.

The residue classes are **generated in code**, never transcribed — see
`apps/fiscal/validators.py` and `tests/fiscal/test_validators.py`. A hand-written
table is exactly the artefact that keeps being wrong. Note the classes span **digits
and letters together**, so a mutation generator restricted to `A-Z` would miss half
the collision space.

This means "mutating any single character invalidates the CNPJ" is **mathematically
false** and must not be written as a property test. The true property is: *mutating a
character to one with a different residue mod 11 invalidates the checksum.*

### 2. The arithmetic accepts `00000000000000`

RFB's reference implementation reports `00.000.000/0000-00` as valid — the sum is
zero, the remainder is zero, and both check digits are correctly `0`. A repeated-digit
CNPJ is not excluded by the checksum and must be rejected by an **explicit rule**.
The same applies to CPF. This is an addition to the published algorithm, made
deliberately and documented here so it is not mistaken for a bug.

## Verification performed

An implementation written from this specification alone was differential-tested
against RFB's published reference implementation. They agree on every vector: the
official example, five real legacy numeric CNPJs, six derived alphanumeric vectors,
and the three documented collisions. See `.evidence/T-031-*.txt`.
