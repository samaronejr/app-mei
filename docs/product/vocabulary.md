# Status vocabulary (pt-BR)

The words the product shows to firm staff and to clients for the concepts introduced by
the R1 release. Templates, help text, emails and tests of rendered copy use these labels.
When code and this table disagree, the table is the reference and the code gets fixed.

## Labels

| Concept (code) | pt-BR label | Example sentence |
| --- | --- | --- |
| `ServiceCycle` | Ciclo do mês / Ciclo de {competência} | "Ciclo de março/2026: 3 de 5 itens recebidos" |
| `EvidenceRequest` | Solicitação | "Precisamos das notas de serviço emitidas em março para concluir a apuração." |
| `Exception` | Pendência (firm side) / Precisamos da sua ajuda (portal) | "Pendência: documento enviado é de outra empresa (CNPJ 12.ABC.345/01DE-35). Responsável: Ana. Revisar até 12/04." |
| `provider_accepted` | Aceita pelo servidor de e-mail | "E-mail aceito pelo servidor em 05/04 09:12. Isso não confirma a leitura." |
| manual payment | Pagamento informado (não confirmado na Receita) | "Comprovante enviado pelo cliente em 20/04, aguardando conferência." |
| `unknown` | Sem informação | "Não verificamos esta obrigação: nenhuma fonte conectada." |
| `stale` | Desatualizado | "Última verificação há 9 dias." |
| `outcome_unknown` | Resultado incerto | "O envio pode ter ocorrido. Estamos confirmando antes de tentar de novo." |

## Rules that go with the labels

- **Unknown, stale, manual and verified are four different things**, and every screen
  keeps them apart. "Sem informação" means no source covers the item; it's never shown
  as fine.
- **No success word without verified evidence.** The product never says "entregue",
  "confirmado" or "tudo em dia", and never shows a green state, unless a verified source
  backs it. An SMTP 2xx reply is "Aceita pelo servidor de e-mail", which says nothing
  about reading or delivery to the inbox.
- **Manual stays manual.** Payment or filing evidence recorded by staff, or sent by the
  client and not yet reviewed, keeps a manual label forever. It is never promoted to
  "confirmed by the authority", and a manual observation is never counted as fresh.
- **A message is not a payment and an upload is not a filing.** No status is derived
  across those lines.
- **Colour is never the only signal.** Every state badge pairs its colour with an icon
  and the text label from this table.
- **Portal copy is plain Portuguese and concrete.** It names the document, the company
  and the due date, for example "Precisamos do extrato bancário de março da {empresa}
  para concluir a análise do mês. Prazo: 10/04."
- **Public copy makes no absolute claims.** No "tempo real", no "100%", no compliance
  guarantees, and no metric that wasn't measured.
