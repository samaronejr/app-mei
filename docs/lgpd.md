# LGPD — controller/operator split, lawful bases, and the rights channel

This document exists because three things are required from day one and are very hard
to retrofit: knowing which role we occupy for each kind of data, knowing which lawful
basis each workflow rests on, and having a named person and a working channel when a
data subject or the ANPD gets in touch.

## 1. Who we are, per data category

The platform occupies **two different roles** simultaneously, and conflating them is
the most common way to get this wrong.

| Data | Role | Reasoning |
| --- | --- | --- |
| A firm's client registry, CNPJs, CPFs, revenue, obligations, fiscal artifacts | **Operator** (*operador*) | The accounting firm decides why and how this data is processed. We process it on their documented instructions, to deliver the service they contracted. |
| Firm-user accounts, authentication, MFA enrolment, sessions | **Controller** (*controlador*) | We decide these purposes ourselves — they exist to secure the platform, not to serve any one firm's mandate. |
| Access logs, audit trail, security telemetry | **Controller** | Retained under our own legal obligation and our own security interest. A firm cannot instruct us to stop keeping them. |
| Billing and subscription data | **Controller** | Our commercial relationship with the firm. |

**Consequence:** a rights request about *client* data is answered **by the firm**, with
us assisting as operator. A rights request about *platform account or security* data is
answered **by us**. The intake form captures enough to route this; the encarregado
decides.

## 2. Lawful basis per workflow

Consent is deliberately rare here. Most of what this product does is compelled by tax
law or necessary to perform a contract, and asking for consent implies a right to
withdraw it that does not actually exist — which is worse than not asking.

| Workflow | Lawful basis (LGPD art. 7) | Note |
| --- | --- | --- |
| Storing a client's CNPJ/CPF and fiscal data | **Legal obligation** (art. 7 II) + **contract performance** (art. 7 V) | Bookkeeping and tax filing are statutory duties of the firm |
| Generating DAS/DASN obligations and due dates | Legal obligation (art. 7 II) | |
| Retaining fiscal artifacts for the statutory period | Legal obligation (art. 7 II) | Survives an erasure request — see §5 |
| Firm-user accounts and authentication | Contract performance (art. 7 V) | |
| MFA enrolment data (TOTP secret) | Contract performance + **legitimate interest** (art. 7 IX) | Security of the service |
| Marco Civil access logs | Legal obligation (art. 7 II) | Lei 12.965/2014 art. 15 — six months, then purged |
| Business audit trail | Legitimate interest (art. 7 IX) | The firm's own evidence in a dispute; append-only |
| Product analytics or marketing | **Consent** (art. 7 I) | None collected in Phases 0–1 |

## 3. The encarregado (DPO)

| | |
| --- | --- |
| **Name** | *(to be filled before the pilot — this row is a deployment blocker, not a placeholder to ship)* |
| **Contact** | `LGPD_ENCARREGADO_EMAIL` (see `.env.example`); published on the public site and in the intake form's confirmation |
| **Duties** | Receive requests from data subjects and from the ANPD, advise on processing, and own the incident runbook in §4 |

Every submission through `/lgpd/solicitacao/` is delivered to this address and recorded
as a `PlatformEvent` with `action="dsr_submitted"`. The event carries the request's
identifier — never the CPF.

## 4. Incident runbook

The clock starts at **detection**, not at confirmation. LGPD art. 48 requires
notification of the ANPD and of affected subjects within a *reasonable* period; the
ANPD's guidance treats **2 business days** as the working expectation.

1. **Contain.** Revoke the credential or connection involved. If a tenant boundary may
   have been crossed, record which tenants and which tables.
2. **Preserve.** Do not purge or rotate the access log, and do not attempt to edit the
   audit trail — it is append-only at the database and any attempt is itself an
   anomaly. Snapshot `audit_accesslog` for the window before the six-month purge can
   reach it.
3. **Assess.** Determine categories of data, number of subjects, and whether the data
   was intelligible (encrypted at rest? pseudonymised?). This determines whether
   notification is required at all.
4. **Notify the encarregado immediately** — they own the decision, not engineering.
5. **Notify the ANPD** via the *Comunicação de Incidente de Segurança* form at
   <https://www.gov.br/anpd/>, within 2 business days of detection. Include: nature of
   the data, affected subjects, technical and security measures in place, risks, and
   mitigation already taken.
6. **Notify affected subjects** in the same window when the risk to their rights is
   relevant. For operator-role data, notify the **firm**, which notifies its clients.
7. **Record.** File the timeline, the decision, and the ANPD protocol number. This
   record is itself evidence of diligence.

## 5. Erasure requests and why some data cannot be deleted

Response template for a deletion request that touches retained data:

> Recebemos sua solicitação de exclusão e ela foi registrada sob o identificador
> `{id}`.
>
> Parte dos dados solicitados **não pode ser excluída neste momento**. A LGPD (art. 16,
> incisos I e II) autoriza a conservação de dados pessoais quando necessária para o
> cumprimento de obrigação legal ou regulatória e para o exercício regular de direitos.
> No seu caso, isso alcança:
>
> - documentos e apurações fiscais, sujeitos a prazo legal de guarda;
> - registros de acesso, mantidos por 6 meses conforme o art. 15 do Marco Civil da
> Internet (Lei 12.965/2014);
> - a trilha de auditoria, mantida para o exercício regular de direitos em eventual
> processo.
>
> Os demais dados sob nossa responsabilidade foram excluídos ou anonimizados. Ao final
> de cada prazo de guarda, os dados remanescentes são eliminados automaticamente.
>
> Você pode peticionar à ANPD caso discorde desta avaliação.

**This is why `Event.metadata` stores references and never document numbers.** The
audit trail is append-only at the database — `UPDATE` and `DELETE` are rejected for
every role, including the table owner. A CPF written there would be genuinely
unerasable, and no legal basis would justify holding it forever. Events therefore carry
`object_type` and `object_id` and nothing more, and a test asserts that no value in any
audit `metadata` column is CPF- or CNPJ-shaped.

`PlatformEvent.subject` does hold an email address, deliberately: a failed-login record
that does not name the address attempted is useless for exactly the investigation it
exists to support. That is held under legitimate interest in the security of the
service (art. 7 IX), which is an independent basis from the account itself.

`DataSubjectRequest` **does** hold a CPF — it has to, to identify the subject — and is
correspondingly **not** append-only, so it can be erased once the request is closed and
any dispute window has passed.
